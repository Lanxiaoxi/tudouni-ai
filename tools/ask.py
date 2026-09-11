"""ask_user 工具：让模型向人提一个结构化的问题。

这是项目里**第二个**人机通道，第一个是 security/asker.py 的审批。两者的分工必须
一直分得清，否则审计里"谁批准了什么"就没有答案了：

    审批   Agent 内部对一次工具调用的**关卡**，模型绕不过；答案改变权限（还能写 memory）
    提问   **模型主动发起**；答案只是内容 —— 不写 memory、不进 gate、不影响任何裁决

所以它们是两个可调用对象、两种返回类型，而不是一个东西的两种用法。**拿到"用户同意了"
不会让下一次 shell 调用免审** —— 这一条不是风格问题，是安全边界：能靠提问换放行的话，
模型自己发明一句"我已经征得同意"就成了绕过审批的路。

端口（Questioner）住在 tools/ 而不是 security/：审批是权限设施，提问不是。它和
FileSystem(workspace) / Shell(workspace) 才是同一层 —— 一个工具一个文件，文件里带着
它需要的协作方实现。
"""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from .tool import ToolArgs, ToolResult

# 一次提问的时钟。和 agents/agent.py 里的 Clock 是同一件事，但 tools 不能依赖
# agents（依赖方向是单向的），所以在这里再说一遍。
Clock = Callable[[], float]

# 回答的字符上限。它会**永久留在会话历史里**，此后每一轮请求都要重发一次（未命中
# 的输入比命中贵约 50 倍），而人可以粘贴一整篇文档 —— 这笔钱由之后每一次请求付。
# 截断时在文本里说明被截了，模型才知道自己看到的不是全部。
MAX_ANSWER_CHARS = 4000

# 三种结局。**它们不是三种措辞，而是三件不同的事**：谁回答了、谁跳过了、有没有人
# 在场。第三种的失败方向是"没有人回答"，绝不等于默许（见 _render）。
ANSWERED = "answered"
SKIPPED = "skipped"
UNAVAILABLE = "unavailable"


class AskUserArgs(ToolArgs):
    """ask_user 的参数。

    **options 写成 list[str] 而不是嵌套的模型。** 嵌套的 Pydantic 模型会在 schema 里
    生成 $defs + $ref，而这份 schema 每一轮都要发给 provider —— 那种形状这个项目
    从来没有真的发出去过，也没有一条测试能证明对方接得住。v1 用扁平类型：编号 +
    一句"标签：说明"就让界面和模型都够用了。真需要结构化选项（GUI 要单独渲染说明）
    时再换成嵌套模型，那时候和一次真实的 API 调用一起验。

    v1 也只接受**一个问题**。要一次问多个得把 question 换成数组，那一步只会动这个类
    （schema 和校验都从它推导），以及渲染和 CLI 的读输入。
    """

    question: str = Field(min_length=1, description="要问的问题，一句话说清")
    header: str = Field(default="", description="不超过 12 字的标签，供界面显示")
    options: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
        description="可选项，界面按编号显示；留空表示让用户自由作答。"
                    "要说明含义就写成「标签：一句话说明」",
    )
    multi_select: bool = Field(
        default=False, description="是否允许选多个（只在给了 options 时有意义）"
    )


@dataclass(frozen=True, slots=True)
class Answer:
    """一次提问的结果。

    status 用 ANSWERED / SKIPPED / UNAVAILABLE 三个常量，而不是布尔 —— 布尔表达不了
    "没有人可问"和"人跳过了"的区别，而这两件事给模型的指令完全不同。

    waited_ms 由**通道**测量并带出来（而不是在这里用时钟包一层）：它要进审计、还要从
    工具耗时里减出来，所以只能有一个来源，见 agents/agent.py 里的 ToolResult。
    """

    text: str
    status: str
    waited_ms: int


# 提问端口：给它一个问题，回答一个答案。
#
# 形状和 ApprovalAsker 刻意相似（"把沟通交给注入的实现"），但**返回类型不同**：
# 它不返回布尔，因为这里没有"批准"这件事，只有内容和"有没有人在场"。
Questioner = Callable[[AskUserArgs], Answer]


def unavailable_questioner(question: AskUserArgs) -> Answer:
    """没有人可问：`--autopilot`、stdin 被接走、CI、将来 Web 上客户端已经断开。

    **返回的既不是空串、也不是"用户没有意见"。** 后者会被模型读成默许，而它根本不知道
    有没有人在看 —— 和 gate 里 `outcome=autopilot` 不肯记成 `approved` 是同一条原则：
    没人做过的事不许记成有人做过。
    """
    return Answer("", UNAVAILABLE, 0)


# 编号与选项之间那条分隔符：`1,3` 和 `1，3` 都要能用（中文输入法下逗号是常有的事）。
_SEPARATORS = str.maketrans({"，": ",", "、": ","})


def _choose(line: str, options: list[str]) -> str:
    """把 `2`（或 `1,3`）换成选项原文；换不了就原样当自由文本。

    **只做保守翻译，不试图理解人说了什么。** 判据和 security/commands.py 里那条
    "看不懂就问"一样：拿不准的输入不该被猜。所以只要有一段不是合法编号，整行都当
    自由文本 —— 混着来的时候猜一半比不猜更坏（`1, 换个别的` 会被翻译成"选项 1"，
    而人多说的那半句就丢了）。
    """
    if not options:
        return line

    picked: list[str] = []
    for part in line.translate(_SEPARATORS).split(","):
        part = part.strip()
        if not part.isdigit():
            return line
        index = int(part)
        if not 1 <= index <= len(options):
            return line
        if options[index - 1] not in picked:
            picked.append(options[index - 1])
    return "、".join(picked)


def cli_questioner(question: AskUserArgs, clock: Clock = time.perf_counter) -> Answer:
    """在终端上问一个问题。

    和 cli_asker 逐条对齐的四件事（理由见 security/asker.py，这里只说结论）：

    1. **提示走 stderr。** stdout 只留 Agent 的产出，`> 对话.txt` 拿到的才是干净的
       对话正文；提问是 runtime 的交互，不是答案的一部分。
    2. **读不到输入按"没有人可问"处理**，不是空答案。OSError 也算 —— stdin 被接走时
       input() 抛的就是它，冒出去会穿过工具层变成"工具执行失败"，把一次提问伪装成
       一个坏掉的工具。
    3. **问题与选项不截断。** 它们是人唯一的判断依据。
    4. **回车 = 跳过，不是同意。** 连续交互里最容易做的动作就是一路回车。
    """
    print(f"[提问] {question.question}", file=sys.stderr)
    if question.header:
        print(f"[提问] （{question.header}）", file=sys.stderr)
    for index, option in enumerate(question.options, 1):
        print(f"[提问]   {index}) {option}", file=sys.stderr)

    hint = "，多个用逗号分隔" if question.multi_select and question.options else ""
    print(f"[提问] 你的回答{hint}（直接回车 = 跳过）: ", end="", file=sys.stderr, flush=True)

    started = clock()
    try:
        line = input().strip()
    except (EOFError, OSError):
        print(file=sys.stderr)      # 补个换行，免得后续输出接在提示后面
        return Answer("", UNAVAILABLE, int((clock() - started) * 1000))

    waited_ms = int((clock() - started) * 1000)
    if not line:
        return Answer("", SKIPPED, waited_ms)
    return Answer(_choose(line, question.options), ANSWERED, waited_ms)


def _render(question: AskUserArgs, answer: Answer) -> str:
    """把答案变成回灌给模型的文本。

    **前缀「用户回答：」不能省。** 模型看不到工具结果的来源，不标出来的话，它会把自己
    编的答案当成用户给的 —— 而那正是死循环式提问的起点。

    另外两句分别对应"没人可问"和"跳过了"，它们的共同点是**都不给模型任何许可**：
    措辞落在"自己决定并说明假设"上，而不是"没有反对意见"。危险动作该不该做由 gate
    裁定，和这里说了什么无关。
    """
    if answer.status == UNAVAILABLE:
        return (
            "没有人可以回答这个问题，这次调用没有拿到任何答案。"
            "请基于最合理的默认继续，并在最终答复里说明你假设了什么 —— "
            "不要把它当成默许，也不要在同一个问题上重复调用 ask_user。"
        )
    if answer.status == SKIPPED:
        return (
            "用户跳过了这个问题，没有给答案。"
            "自行选一个最合理的做法并在最终答复里说明，不要再问一遍。"
        )

    text = answer.text
    if len(text) > MAX_ANSWER_CHARS:
        text = f"{text[:MAX_ANSWER_CHARS]}…（回答被截断，共 {len(text)} 字符）"
    return f"用户回答：{text}"


class AskUser:
    """ask_user 的 handler：**只是端口的一层包装**。

    它自己不读 stdin、不认识终端、不知道有没有人在 —— 那是注入进来的 Questioner 的事。
    "判定留在内部，沟通交给注入的实现"这条原则在这里的形态是：**这个类里没有一句关于
    "怎么问"的代码**，所以测试、CI、将来的 Web 各注入各的，工具层一个字都不用改。
    """

    def __init__(self, questioner: Questioner | None = None):
        # 没配提问方式（questioner 为 None）时落到 unavailable：**要问却没配通道，按
        # "没有人可问"处理**，和 gate 里 `no_asker` 那一支同一个失败方向。默认成
        # "可以问、问出来算同意"是最坏的 fail-open。
        self._questioner = questioner or unavailable_questioner

    def __call__(self, **arguments: Any) -> ToolResult:
        question = AskUserArgs(**arguments)
        answer = self._questioner(question)
        return ToolResult(
            text=_render(question, answer),
            # 这两项**只给审计**，不进对话历史：答案正文已经在 messages 里了，再记一遍
            # 就是同一份事实写两处。human_wait_ms 的名字里带 human，是为了让 cli 那边
            # 的减法不必认识 "ask_user" 这个名字（减的是"等人的时间"这个类别）。
            audit={"question_status": answer.status, "human_wait_ms": answer.waited_ms},
        )
