import json
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from agent_runtime.agents.retry import Attempt, call_with_retry
from agent_runtime.audit import event
from agent_runtime.context.budget import MESSAGE_OVERHEAD
from agent_runtime.context.processor import (
    ToolExecution,
    ToolResultProcessor,
    default_processor,
)
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import DeltaSink, ModelFatalError, TokenUsage
from agent_runtime.security.asker import ApprovalAsker
from agent_runtime.security.gate import check_permission
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.security.policy import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.state.model import SessionModel
from agent_runtime.state.reasoning import DEFAULT_EFFORT, DEFAULT_THINKING
from agent_runtime.tools.tool import InvalidArgsError, Tool, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from agent_runtime.context.manager import ContextManager
    from agent_runtime.context.renderer import ContextRenderer
    from agent_runtime.models.types import ModelResponse


# debug 输出里每条内容的最大预览长度。工具结果（读文件、列目录）可能很长，
# 全打出来会把终端刷掉。
DEBUG_PREVIEW_LIMIT = 200

# 落盘回调：Agent 在「messages 一致」的时刻调用它，通知外部现在可以安全保存了。
# 存到哪、什么格式、要不要存，都由注入进来的实现决定。
Checkpoint = Callable[[Session], None]

# 审计事件回调：Agent 报告「发生了什么」，注入的实现决定记到哪、什么格式。
EventSink = Callable[[dict[str, Any]], None]

# 会话状态提示：给它会话的 metadata，返回一段要拼进这次请求末尾的文本（None = 不拼）。
#
# 为什么参数是 metadata 而不是 Session 本体：需要这段文本的是**工具层**（任务列表是
# todo_write 的状态），而 `tools` 不能 import `state` —— README 里那条依赖方向
# （tools 无内部依赖）就是这么走的。会话里能被工具层看见的、又要跨回合留存的那一块
# 正好就是 metadata，所以这个签名既是最小的，也没有把 Session 整体交出去。
SessionNotes = Callable[[Mapping[str, Any]], str | None]

# 流式增量：模型每吐一块，Agent 调它一次。**它是第六个注入点**，和 asker /
# questioner / on_event 同一条原则 —— Agent 知道"现在吐出来的是正文还是思考链"，
# 而"送给谁、怎么送"由注入的实现决定（协议版发 `t:"delta"`，将来 Web 版发 SSE）。
#
# 两个参数都是关键字（见 models/types.py 的 DeltaSink）：按位置传一次就会把思考链
# 和正文对调，而那个错误看起来像"答案里混进了一段自言自语"。
#
# 它**不进审计**（`on_event` 那条路）：一次两千 token 的回答是上千块，而
# `JsonlSink` 每条事件一次 open/write/close —— 抄进去等于把日志变成第二个会话文件。
# 审计里记的是汇总（`model_call.streamed_chars` / `stream_chunks`）。
DeltaCallback = DeltaSink

# 计时的时钟。**注入而不是直接调 time.perf_counter**，理由和 retry.py 里 sleep 可注入
# 一样：时间没法断言。测试里换成一个由假模型/假 handler 推进的假时钟，duration_ms 才能
# 被钉成精确值；换成真实时钟，这类断言只能在 CI 上随机红。
#
# 必须是单调时钟：time.time() 会被 NTP 调整，测出来的"耗时"可能是负数。
Clock = Callable[[], float]

# 审计事件里参数预览的最大长度。参数可能很长（write_file 的 content），
# 也可能含敏感内容，所以审计日志只留预览、从不记全文。
AUDIT_PREVIEW_LIMIT = 200

# 一个批次里最多同时跑几个工具。
#
# 它挡的是"模型一口气给出 30 个调用"那种极端形状：read_file 会把整份文件读进内存、
# 结果再整份进入下一轮请求，并发度等于批次大小的话，句柄和内存都会出现一个没有必要的
# 尖峰。实测最常见的批是 2~5 个，所以这个上限在正常形状上永远不会碰到。
MAX_PARALLEL = 8

# 工具在正常工作流程里会抛的异常 —— 它们代表"这次调用没成功"，不是 bug。
# 其余异常一律当疑似 bug：给模型的消息照旧，但 stderr 要留下完整 traceback。
_TOOL_LEVEL_ERRORS = (
    FileNotFoundError,
    PermissionError,
    NotADirectoryError,
    IsADirectoryError,
    json.JSONDecodeError,
)


# --- 一次请求的载荷 ---------------------------------------------------------

# 哪些消息属于 **stable 区**（"尽量不变"的那一半），以及它们的优先级。
#
# 它有明确的判据，而不是"看着办"：
#
#   * **系统提示词**（第一条 system）—— 整段请求里唯一逐字节稳定的部分。它 pinned
#     是必须的：预算再紧也不能把行为准则挤掉。
#   * **本回合的用户任务**（第一条 user）—— 整个回合都在为它干活。它 pinned 的
#     理由和系统提示词一样，而且更硬：模型看不到任务就没法做对任何事。
#
# 其余一律 dynamic。**为什么后续轮次里的用户消息不算 stable**：`run()` 每被调一次
# 就追加一条 user，而一条**新**消息加在中间会让它后面的一切都变 —— 那是缓存的
# 代价，不是我们能选的（它就是用户刚说的话）。
STABLE_ZONE = "stable"
DYNAMIC_ZONE = "dynamic"
SYSTEM_ZONE = "system"


def message_marks(messages: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """给一批消息算出它们的 zone / priority / pinned。

    **按位置算，不按内容算**：`run()` 每回合都会 append 一条 user，而"哪一条才是
    这个回合的任务"只有位置知道（第一次调用之前就在历史里的那条）。按内容算
    （比如"最长的那条"）在同一个会话里聊上十轮之后必然会指错。
    """
    marks: dict[int, dict[str, Any]] = {}
    first_system = _index_of_role(messages, "system")
    if first_system is not None:
        marks[first_system] = {"zone": SYSTEM_ZONE, "pinned": True, "priority": 100}
    # 第一条 user —— 它在第一次 run() 里被 append，早于任何工具结果。
    first_user = _index_of_role(messages, "user")
    if first_user is not None:
        marks[first_user] = {"zone": STABLE_ZONE, "pinned": True, "priority": 50}
    return marks


def _index_of_role(messages: list[dict[str, Any]], role: str) -> int | None:
    for index, message in enumerate(messages):
        if message.get("role") == role:
            return index
    return None


def _arguments_of(call: dict[str, Any]) -> dict[str, Any]:
    """模型给的参数（JSON 字符串）解析成 dict。**解析不了就返回空 dict。**

    `_prepare` 走的是同一次解析，失败时会被记成"工具执行失败"。这里再解析一次是
    为了给 Artifact 的 metadata 带上一份参数 —— 而**这里不许抛**：一条参数坏掉的
    调用只是没有 metadata 可记，它不该让"把结果写进历史"这件事也失败（那会让
    会话停在一个带 `tool_calls` 却没有结果的半截状态上，API 直接 400）。
    """
    raw = call.get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _last_tool_index(messages: list[dict[str, Any]]) -> int:
    """最后一条 tool 结果在第几条。**没有就返回 `-1`。**

    步数警报要挂在它后面（见 `Agent._payload`），所以"最后一条在哪"需要一个确定的
    答案 —— 而"往末尾找第一条 tool"（`reversed`）和"从头扫一遍"在这里必须给出同一
    个结果，否则同一个回合的两次请求会把警报插到不同位置，而那会让前缀缓存整个作废。
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "tool":
            return index
    return -1


def _is_fatal(exc: BaseException) -> bool:
    """区分"重试也没用"和"重试用完了"。

    两者在审计里必须是不同的 stop_reason：前者要用户改配置，后者可以直接再试。
    """
    return isinstance(exc, ModelFatalError)


@dataclass(frozen=True, slots=True)
class _Outcome:
    """一次工具调用的结局：给模型的文本 + 审计用的状态和耗时。

    它取代了原来"直接从 `_execute_tool` 返回一个字符串"：状态和耗时只有那一层
    知道，而**事件由主线程发**（见 `_run`），所以判定和上报之间需要一个能带过线程
    边界的载体。
    """

    text: str
    status: str                     # ok / denied / invalid_args / error
    duration_ms: int
    # 工具抛出的**非预期**异常的完整栈（疑似我们自己的 bug）。
    #
    # 它不在工作线程里直接打印，是因为 traceback.print_exc() 是一行一次写 —— 两个
    # 线程同时打会交错成一段读不懂的东西。带回主线程由 _report_tool_bug 打。
    traceback: str | None = None
    # 工具自己带回来的审计字段（`ToolResult.audit`）。**排在最后**：前四个字段有位置
    # 参数的调用点（`_prepare` / `_run` 里那几个 _Outcome(...)），插在中间会静默地把
    # traceback 挪到 audit 上去。
    #
    # 为什么要留这么一条通道：ask_user 的 duration_ms **含等人的时间**（它就阻塞在
    # 人的输入上），而 cli 那边要把那一段减出来单列成"等人回答" —— 否则"我看了 30 秒
    # 才回答"会显示成"这个工具花了 30 秒"。审批没有这个问题，因为裁决和计时都在
    # `_prepare` 里、本来就单独计时；提问发生在 handler 里，只有工具自己知道等了多久。
    #
    # 它**不是**给模型的：回灌进对话历史的只有 text，答案正文也在那里，不记第二遍。
    audit: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Prepared:
    """一次工具调用的前半段：裁决已经做完，执行随时可以开始。

    **"裁决"和"执行"必须能分开**，因为并行的只有后半段：asker 走 stdin，两条审批
    同时问会互相抢输入，所以裁决一律留在主线程、按原顺序发生（见 security/asker.py）。
    """

    call: dict[str, Any]
    tool: Tool | None               # None：准备阶段就已经定局，见 settled
    arguments: dict[str, Any] | None
    parallel_safe: bool
    settled: _Outcome | None        # 有值表示不需要再执行（准备失败、或被拒）


class RunCancelled(BaseException):
    """有人要求停止这一轮，而我们在一个**安全点**停下来了。

    **它必须继承 `BaseException`（像 `KeyboardInterrupt` 那样），这不是风格问题。**
    继承 `Exception` 的话它会先撞上 `_run` 里那个 `except Exception`（工具执行那一段）
    或 `_prepare` 里那个，被吞成一条"工具执行失败：RunCancelled"的结果 —— 于是模型
    拿到一句看不懂的话继续跑，而用户以为已经停下了。这种"按了没用但也不报错"是最坏
    的失败形态，所以类型上就必须绕开那两个兜底。

    它继承 BaseException 还有第二个后果要记住：**调用方必须显式接住它**。`main.py`
    和协议循环都接；测试里也是。漏接的后果是整轮异常穿透到顶层 —— 那比静默继续好，
    所以这个方向是对的。

    **什么时候能取消，有三个安全点**（见 `Agent.run`）：

      * **两步之间** —— messages 一致（上一步的工具结果全 append 完、★ 也落过盘），
        唯一一个"不管有没有流式都成立"的位置。`should_stop` 就在那里被问；
      * **流式收到下一块之前**（`_DeltaRelay`）—— 只在开了流式时存在。它的安全性
        和上面那个不同：此刻 assistant 消息**还没有 append**，所以半截正文根本没
        进历史，同一个位置天然一致；
      * **中途取消不了的两段**：非流式的那一次 `complete()`（同步阻塞）和工具执行
        （同步 handler）。工具那一段永远打断不了（没有天然的打断点）；模型那一段
        在开了流式之后就有了，这就是决策 1 认下的第三笔代价被还掉的地方。
    """

    def __init__(self, step: int):
        # 措辞**故意不说"之后"**：这个异常现在从两个地方抛出来 —— 两步之间
        # （循环顶部）、以及流式收到下一块之前（`_DeltaRelay`，那是在一步**中途**）。
        # 两种情况对用户来说是同一件事（"我让它停，它停了，会话还在"），所以
        # 那句话只承诺真正成立的那部分。具体停在哪由审计里的 step 说。
        super().__init__("已停止，会话是完好的，可以直接接着跑。")
        self.step = step


class StepLimitExceeded(RuntimeError):
    """步数预算用尽：任务既没失败，也没收尾。

    单独一个类型、而不是返回一句"超过最大步数"的文本，理由是返回值和真正的答案
    在同一条出口上：cli.py 会把它 print 到 **stdout**，于是 `> 对话.txt` 拿到的东西
    里混进一句"已停止"，而用户从输出里分不出"答完了"和"被砍断了"。

    它也不该混进 ModelError：处置方式完全不同 —— 会话是完好的、之前的工作都还在，
    用户接着跑就行，不需要重试、也不需要改配置。

    抛之前 run_finished 和 checkpoint 都已经发生（见 run 末尾），所以日志里能看出
    是哪一轮撞的墙，会话也能原样续上。消息里把"可以接着跑"说出来，是因为那是它和
    其它失败最大的区别。
    """

    def __init__(self, step: int, tools: list[str] | None = None):
        detail = f"，最后一步仍在调用 {', '.join(tools)}" if tools else ""
        super().__init__(
            f"已达最大步数 {step}，任务未收尾{detail}。会话是完好的，可以直接接着跑。"
        )
        self.step = step
        self.tools = list(tools or ())


class _DeltaRelay:
    """把模型吐出来的块转给注入的 `on_delta`，并兼两个职责。

    ## 1. 流式下"随时取消"的那个打断点

    `RunCancelled` 的 docstring 里原本写的是"只有两个安全点"，而**流式让第三个
    安全点成立**：每收到一块就问一次 `should_stop`。这不是顺手加的 —— 没有它，
    按 Esc 之后还要等模型把整段回答说完（几秒到几十秒），而用户按那个键的意思
    正是"别说了"。

    在这里抛是**安全的**：此刻 messages 里什么都没有（assistant 消息要等
    `complete()` 返回才 append），所以半截正文既不会进历史，也不会留下一条带
    tool_calls 却没有结果的悬空消息。

    **代价是界面和历史会对不上**：屏幕上出现过的那半句不在会话里，下次
    `--session` 恢复时它就不见了。这是认下的取舍 —— 另一条路（把半截答案当
    assistant 消息存下来）更坏：恢复会话时那条被砍断的答案看起来和一次正常的
    回答一模一样，而模型接下来会拿它当自己说过的话。收尾由
    `Agent._complete_with_retry` 做（那里离抛出点最近），它保证 `run_finished`
    和落盘都发生 —— 少了那一步，界面会一直等一条永远不来的事件。

    ## 2. "上一次尝试吐的作废了"那一声

    `reset()` 由 `on_attempt_started` 触发（重试、或者适配层因 400 自己重发）。
    `_complete_with_retry` 拿 `take_reset_mark()` 决定要不要在审计里留一条
    `delta_reset`：没有这个标记就发的话，一次**没来得及吐任何字**的重试会在
    日志里留下一条"清空过正文"，而那句话是假的。
    """

    __slots__ = ("_sink", "_should_stop", "_step", "_streamed", "_reset_mark")

    def __init__(self, sink: DeltaCallback | None,
                 should_stop: Callable[[], bool] | None,
                 step: int) -> None:
        self._sink = sink
        self._should_stop = should_stop
        self._step = step
        # 从**上一次 reset 之后**到现在吐过东西没有。它是 reset 那一声的判据。
        self._streamed = False
        # "有一次吐出去的东西被作废了、而且那件事还没记过账"。见 `take_reset_mark`。
        self._reset_mark = False

    @property
    def streaming(self) -> bool:
        """有没有一个真的 sink。**没有就别把它交给模型层。**

        `call_with_retry` 拿到的是 `None` 还是这个对象，决定了模型层走哪条路
        （`on_delta=None` = 一次返回完整响应）。而"没有 sink 的空 relay"必须
        表现得和 `None` 一模一样 —— 不这么做的症状是 `--no-stream` **静默失效**：
        请求里照样带着 `stream: true`，流也真的流了，只是没有任何人收到 delta
        （实测踩过这一条：`--no-stream` 下 `init.stream=false` 而请求体里
        `stream: true`）。
        """
        return self._sink is not None

    def text(self, value: str) -> None:
        self._check_cancelled()
        if not value or self._sink is None:
            return
        self._streamed = True
        self._sink(text=value)

    def reasoning(self, value: str) -> None:
        self._check_cancelled()
        if not value or self._sink is None:
            return
        self._streamed = True
        self._sink(reasoning=value)

    def __call__(self, *, text: str = "", reasoning: str = "",
                 reset: bool = False) -> None:
        """模型层调的就是这一个（`models/types.py` 的 `DeltaSink` 契约）。

        **两个参数都必须按关键字传。** 它们是两个字符串，按位置传一次就会把思考链
        和正文对调，而那个错误在界面上的症状是"答案里混进了一段自言自语" ——
        看起来像模型的问题，不像调用点写错了。所以这个签名把关键字定死。
        """
        if reset:
            self.reset()
            return
        if text:
            self.text(text)
        if reasoning:
            self.reasoning(reasoning)

    def reset(self) -> None:
        """一次新的尝试开始了：上一次吐出去的东西作废。

        **重复调用是安全的、也是必要的**：一次重试会同时经过 `agents/retry.py`
        的 `on_retry` 和适配层自己的 `on_attempt_started`（两边说的是同一件事），
        而界面收到两条 `delta_reset` 的效果和收到一条完全一样 —— 而漏掉一条的
        后果是屏幕上留着一段错位的半截正文。
        """
        if self._streamed:
            self._reset_mark = True
        self._streamed = False
        if self._sink is not None:
            self._sink(reset=True)

    def take_reset_mark(self) -> bool:
        """"刚才有一次作废、而且还没记过账"？取一次就清掉。

        **一次重试只该在审计里留一条 `delta_reset`。** 而 `retry` 这个回调在一次
        重试里会被调**两次**（`call_with_retry` 的 `on_retry` 一次、适配层自己的
        `on_attempt_started` 又调它一次 —— 那是两个不同层的"我要重来了"），
        记两次就是同一份事实写两遍，读日志的人会以为重试了两次。

        这个"取一次就清"的记账放在**这个对象里**，不放调用方：调用方那段流程是
        一个闭包，闭包里改一个外层布尔就得 `nonlocal`，而那个写法一旦漏了就是
        `UnboundLocalError`（实测踩过，而且它是从"重试"这条路上抛出来的，
        症状看起来像模型错了）。
        """
        marked, self._reset_mark = self._reset_mark, False
        return marked

    def _check_cancelled(self) -> None:
        if self._should_stop is not None and self._should_stop():
            raise RunCancelled(self._step)


class Agent:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        policy: PermissionPolicy,
        asker: ApprovalAsker | None = None,
        memory: ApprovalMemory | None = None,
        on_checkpoint: Checkpoint | None = None,
        on_event: EventSink | None = None,
        on_delta: DeltaCallback | None = None,
        session_notes: SessionNotes | None = None,
        debug: bool = False,
        clock: Clock = time.perf_counter,
        autopilot: bool = False,
        should_stop: "Callable[[], bool] | None" = None,
        session_model: SessionModel | None = None,
        context: "ContextManager | None" = None,
        processor: "ToolResultProcessor | None" = None,
    ):
        # 前三个是【能力】：每个应用构造一次，长期复用、可以跨会话共享。
        self.model = model
        self.tools = tools
        # policy 必填、没有默认值：这样就不可能出现「忘了配策略，于是权限检查静默
        # 消失」的 Agent。默认成「不检查」是最坏的 fail-open。
        self.policy = policy

        # 下面两个是【注入的协作方】，都不属于 Agent 自己。
        #
        # asker 可以为空：若策略把所有等级都放进 auto_approve，ASK 永远不会发生，
        # 就不必塞一个用不到的询问函数。但真要 ASK 而没有 asker 时按拒绝处理。
        self.asker = asker
        # memory 可以为空：不传就是「这一次的批准只管这一次」（测试、无人值守都用
        # 得上）。它和 asker 是一对 —— asker 问出答案，memory 记住人说过「别再问」，
        # 而它是不是存在，决定了审批提示里有没有那个 t。
        self.memory = memory
        # on_checkpoint 可以为空：不传就是不持久化（测试、一次性任务都用得上）。
        # 方向很重要 —— Agent 只知道「什么时候存是安全的」，不知道「存到哪、什么
        # 格式、要不要存」。这和 asker 是同一条原则：判定留在内部，执行交给注入的
        # 实现。万一外面直接改成 run() 之后再存，就会丢掉回合内部的落盘点。
        self.on_checkpoint = on_checkpoint

        # on_event 可以为空：不传就是不记审计日志（测试、一次性任务）。
        # 契约和上面两个完全一样 —— Agent 知道「发生了什么、什么时候发生」，
        # 注入的实现决定「记到哪、什么格式」。所以它也不该自己拼路径、开文件。
        self.on_event = on_event

        # on_delta 可以为空：不传就是**不做流式**（模型层一次返回完整响应，
        # 也就是加流式之前的行为）。它和 `session_notes` 一样是**第七个注入点**，
        # 但和前面几个有一处不同 ——
        # **它没有"不注入时的等价物"**：不注入就是没有流，而不是"永远不说话的流"。
        # 这和 asker 可以为 None 是同一条：不注入就是没有这个能力。
        #
        # 为什么不把它挂在 on_event 上（"事件里多一种 kind 就行了"）：delta 的
        # 数量级和事件完全不同（一次回答上千块），而 on_event 的实现（JsonlSink）
        # 是每条一次 open/write/close —— 抄进去等于把审计日志变成第二个会话文件，
        # 而且它会把"审计 = 一份可以事后完整回放的记录"这件事稀释掉。
        self.on_delta = on_delta

        # session_notes 可以为空：不传就是"没有任何需要每轮重新贴上去的会话状态"。
        #
        # 它是这个类的第五个注入点，但和 session_notes 打交道的**不是 Agent 自己**：
        # 那些文本由工具层提供（任务列表长什么样是 tools/builtin/todo.py 的知识），Agent 只
        # 负责在每次请求的末尾把当前状态重新贴一遍。判定留在内部、沟通交给注入的实现
        # —— 和 asker / questioner 同一条原则，只不过这一份注入的是"怎么说"而不是
        # "去问谁"。
        self.session_notes = session_notes

        self.debug = debug

        # 时钟也是注入的：审计里的每个 duration_ms 都出自它，所以测试要能把它换成假的
        # （见上面 Clock 那段）。
        self.clock = clock

        # autopilot：这一轮没有人在键盘前，所以一律不问。它**只管审批** —— 工作区边界、
        # 控制面写入、拒绝名单都不归它管（那些是"不许做"，不是"要不要问"）。审计里每次
        # 放行会记成 outcome=autopilot，好回答"这次会话到底有没有人看着"。
        self.autopilot = autopilot

        # should_stop 是第六个注入点：**"要不要停"由外面决定，怎么停是这里的事。**
        #
        # 它是一个返回布尔的纯查询（不是"抛异常的回调"）：外面只管回答"现在该停了吗"，
        # 而"在哪个位置问、问了之后做什么"（落盘 + 记审计 + 抛 RunCancelled）留在这里 ——
        # 因为那个位置是 messages 一致性的知识，只有 Agent 有。
        #
        # 为 None 表示"这一轮没有取消这回事"（CLI、测试、一次性任务）。这和 asker 可以为
        # None 是同一条：不注入就是没有这个能力，而不是"永远返回 False 的桩"。
        self.should_stop = should_stop

        # 第八个注入点：**这个会话选的是哪个模型**（`/model` 的结果）。
        #
        # 为什么它必须进来，而不是让 Agent 直接读 `session.metadata`：读哪个键、块长什么
        # 样是 `state/model.py` 的知识，而 Agent 该知道的只有两件事 —— "这一轮开头要不要
        # 留一句'模型换了'"、以及"把'这一轮用过的模型'记下来"。两件事都在这个对象上。
        #
        # 为 None 表示"这个 Agent 不关心会话级模型选择"（测试、一次性任务）。代价是
        # 那种会话里换模型不会在历史里留痕 —— 而那正是 None 的含义，不是漏了什么。
        self.session_model = session_model

        # 第九个注入点：**Context 系统**。
        #
        # 它为 None 时行为逐字节回到重构之前：工具结果原样进 messages，载荷就是
        # messages 加尾部那条提醒。这不是兼容层，而是这个类**唯一说得出道理的
        # 默认值** —— Agent 不该自己造一个 ArtifactStore（它不知道工作区在哪、
        # 也不知道会话 id），而"没有 Context"这件事本身是成立的（测试、一次性
        # 任务、`--history` 那种根本不发请求的路径都用得上）。
        #
        # 有它时，工具结果的正文进 ArtifactStore，messages 里只留一句引用，而发给
        # 模型的载荷由 ContextRenderer 按档位现渲染（见 `_payload`）。
        self.context = context
        # 工具执行 → Artifact 的那一层。**跟 context 一起注入**：没有 context 时
        # 它没有落点（Artifact 收进哪儿去？），所以那时它一定不被用到。
        self.processor = processor or default_processor()

    @property
    def model_name(self) -> str:
        """现在这个模型叫什么（`/status` 和"换了模型"那句话都用它）。

        **从适配器上读，不存第二个字段。** 存一份的话，`Agent.model` 和那份副本会在
        某条路上分家（"换了模型但状态栏还写着旧的"），而那种症状看起来像模型没换成功。
        适配器不认识 `model` 属性时返回空串（`ChatModel` 的契约里没有它 —— 只有
        OpenAI 兼容那一支有），调用方按"不知道"处理。
        """
        return str(getattr(self.model, "model", "") or "")

    def switch_model(self, name: str, *, provider: str = "", api_key: str = "",
                     base_url: str = "") -> bool:
        """换这个会话用哪条路由上的哪个模型。返回"换成了吗"。

        ## 两种换法，判据是"端点或密钥变了没有"

          * **同一条路由上换模型**（`/model deepseek-v4-pro`）：只改请求里那个字段
            （`ChatModel.switch_model`）；
          * **换到另一条路由**（`/model acme`，或者两条路由有同名模型而用户写了
            `acme/xxx`）：密钥和端点都变了，而它们是 SDK 客户端的构造参数 —— 所以
            走 `install()`，它会重造客户端并收掉旧的。

        判据取"变了没有"而不是"调用方想走哪条路"：调用方知道的是**目标**（哪条路由），
        而"要不要重造客户端"是适配器的知识。让调用方选方法就会出现"换了路由却没重造
        客户端"这种半吊子状态，而它的症状是请求带着旧密钥发到新地址上。

        ## 为什么"记下这个选择"也在这里

        换模型是两步：**适配器上换**（下一个请求用新名字）和**会话里记**（下一轮开头
        留一句"换过"、恢复会话时还是它）。分给两个方法、让调用方记得两步都走，是一条
        迟早会漏的约定 —— 而漏掉第二步的症状尤其难看：模型确实换了，但历史里一句话
        都没有，于是后半段那些回答看起来像是同一个模型写的。

        所以顺序钉在这里：**先让适配器换，成功了才记**。反过来的话，一个不支持换模型
        的适配器会留下一条"选了 pro"的记录，而请求照旧发给 flash。

        **只吞 `NotImplementedError`**（那是"这个能力不存在"的准确信号，基类的默认
        实现抛的就是它）。别的异常原样穿出去：一个写坏了、自己抛 `TypeError` 的适配器
        不该被当成"不支持"，那会把一个真 bug 变成一句轻描淡写的提示。
        """
        same_route = (not provider or provider == self.model_provider) and (
            api_key == "" or api_key == getattr(self.model, "_api_key", None)
        )
        try:
            if same_route:
                self.model.switch_model(name)
            else:
                self.model.install(api_key=api_key, base_url=base_url,
                                   model=name, provider=provider)
        except NotImplementedError:
            return False
        if self.session_model is not None:
            self.session_model.select_route(provider=provider, model=name)
        return True

    def set_reasoning(self, *, thinking: bool | None = None,
                      effort: str | None = None) -> bool:
        """改思考开关 / 强度。返回"改了吗"。**和换模型同一条时序：下一次请求生效。**

        两个参数都可以为 None = "不动它" —— 这样 `/thinking` 和 `/effort` 各改一格，
        而"顺手把另一格也重置了"是那种用户没要求、事后也查不出来的行为。

        记进会话的那一步和 `switch_model` 一样钉在这里（理由见那里）：适配器上改、
        会话里记，两件事必须在同一处发生。
        """
        try:
            self.model.set_reasoning(
                thinking=self.thinking if thinking is None else thinking,
                effort=self.effort if effort is None else effort,
            )
        except NotImplementedError:
            return False
        if self.session_model is not None:
            if thinking is not None:
                self.session_model.select_thinking(thinking)
            if effort is not None:
                self.session_model.select_effort(effort)
        return True

    @property
    def model_provider(self) -> str:
        """现在这条路由叫什么（空串 = 适配器不说 / 只有一条内置的）。"""
        return str(getattr(self.model, "provider", "") or "")

    @property
    def thinking(self) -> bool:
        """现在开着思考没有。**以适配器为准**（它才是请求里那个值）。"""
        return bool(getattr(self.model, "thinking", DEFAULT_THINKING))

    @property
    def effort(self) -> str:
        """现在的思考强度。**以适配器为准。**"""
        return str(getattr(self.model, "effort", DEFAULT_EFFORT) or DEFAULT_EFFORT)

    def _emit(self, kind: str, session: Session, run_id: str, step: int, **data: Any) -> None:
        """报告一条审计事件。

        每条事件都是纯 JSON 可序列化的 —— 所以这里只传字符串、数字、布尔，
        不传 datetime / Path / 异常对象。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(event(
                kind,
                session_id=session.session_id,
                run_id=run_id,
                step=step,
                **data,
            ))
        except Exception as exc:
            # 观测量出了问题，绝不能影响被观测的过程。而且 on_event 的调用点有些
            # 恰好落在 messages **不一致**的窗口里（tool_call 事件就夹在 assistant
            # 消息和它的 tool 结果之间），抛出去会留下悬空的 tool_calls，会话从此
            # 每轮都 400 —— 这个后果实测过。
            self._warn(f"审计事件写入失败（已忽略）: {type(exc).__name__}: {exc}")

    def _finish_run(
        self,
        session: Session,
        run_id: str,
        step: int,
        run_started: float,
        stop_reason: str,
    ) -> None:
        """收尾：记一条 run_finished，并带上这一回合的墙上时间。

        三个收尾点（答完 / 步数用尽 / 模型失败）都走这里 —— 事件长什么样只写一遍，
        省得三处各写一份、日后漏掉 duration_ms 这种字段。

        duration_ms 是**回合总时长**，和逐条 model_call / tool_result 的口径不重叠，
        所以 CLI 那边可以直接把它们相减去算"未归因"。口径重叠过一次的代价这里记得：
        工具耗时原本是从函数入口起表的，把等人审批也算了进去。
        """
        self._emit(
            "run_finished", session, run_id, step,
            stop_reason=stop_reason,
            duration_ms=int((self.clock() - run_started) * 1000),
        )

    def _checkpoint(self, session: Session) -> None:
        """在 messages 一致的时刻通知外部保存。

        调用点必须选在 messages **一致**的时刻 —— 见 run() 里标了 ★ 的那处注释。
        不落盘会丢会话；落盘了一个半截状态则更糟：重新加载后直接发不出去。
        """
        if self.on_checkpoint is None:
            return
        # 落盘之前把 Context 状态挂上。**"挂上"而不是"每次深拷一份"**：两者指向
        # 同一个对象，而 store 只在 version 变了之后才写 ctx 记录（见 state/store.py），
        # 所以正常路径上这一次赋值是零成本的。
        #
        # 为什么要在这里再赋一次（run() 开头已经赋过）：工具结果刚好在这个时刻
        # 进了 Context（`_tool_message` 里那个 `add`），而那正是落盘点要记住的东西。
        if self.context is not None:
            session.context = self.context.state
        try:
            self.on_checkpoint(session)
        except Exception as exc:
            # 和 on_event 不同：落盘点的调用位置**全都在 messages 一致的时刻**，
            # 所以异常抛出去不会损坏会话（实测过）。但它会掩盖「这次没存上」，
            # 而用户会以为存好了 —— 所以吞掉，但必须大声说出来。
            self._warn(f"会话落盘失败（已忽略）: {type(exc).__name__}: {exc}")

    @staticmethod
    def _warn(message: str) -> None:
        """警告始终可见，不受 debug 开关控制。

        被吞掉的失败如果不吭声，就变成了静默失败 —— 那比直接崩还难查。
        """
        print(f"[warn] {message}", file=sys.stderr)

    def _debug(self, message: str) -> None:
        """调试输出走 stderr。

        不混进 stdout，是因为 Agent 的最终结果才是这个程序的"输出"（可以被管道
        接走），中间过程是诊断信息。想关掉就 Agent(..., debug=False)。
        """
        if self.debug:
            print(f"[debug] {message}", file=sys.stderr)

    def _debug_lazy(self, build: Callable[[], str]) -> None:
        """`_debug` 的惰性版：debug 关着时**连字符串都不去拼**。

        什么时候必须用它：消息里含 `_preview(...)`、或者要把一段可能很长的正文拼进去
        时。两个理由，都不是洁癖：

          1. `_preview` 不是 O(1) —— 它先把换行压平再截断，也就是把整段文本扫一遍；
          2. f-string 的实参**在进 _debug 之前就求值了**，所以 debug 关着的时候，那次
             扫描的产物会被立刻丢掉。

        实测：一个 8MB 的 read_file 结果白扫约 10ms，一批五个就是 50ms —— 而这条路径
        对**默认不开 debug** 的每一次工具调用都成立。也就是说：不开 debug 的会话一直在
        替 debug 买单。

        参数是 `Callable[[], str]` 而不是字符串，是为了让上面那句话在签名上就成立：
        传进来的东西**只有真要打的时候才求值**。短消息（几个字段拼一拼）直接用
        `_debug` 就好，套一层 lambda 只是噪声。
        """
        if self.debug:
            self._debug(build())

    @staticmethod
    def _preview(text: str, limit: int = DEBUG_PREVIEW_LIMIT) -> str:
        """把多行长文本压成单行预览，过长则截断并标注真实长度。"""
        flat = text.replace("\n", "\\n")
        if len(flat) <= limit:
            return flat
        return f"{flat[:limit]}…(共 {len(flat)} 字符)"

    @staticmethod
    def _usage_fields(usage: TokenUsage | None) -> dict[str, int]:
        """把 token 用量摊成审计字段。

        拿不到 usage 就返回空 dict —— 宁可不写这几个键，也不要往日志里塞一串
        null，那会让后面做成本汇总时处处要判空。
        """
        if usage is None:
            return {}
        return {
            "prompt_tokens": usage.prompt_tokens,
            "cached_tokens": usage.cached_tokens,
            "miss_tokens": usage.miss_tokens,
            "completion_tokens": usage.completion_tokens,
        }

    def _complete_with_retry(
        self,
        session: Session,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict],
        run_started: float,
    ) -> "ModelResponse":
        """调模型，并保证每一次尝试都留下一条审计记录。

        重试策略本身在 agents/retry.py 里 —— 它不碰会话、工具、权限，所以待在
        那边更合适。这里只负责两件本层才知道的事：怎么把尝试记进审计，以及
        失败时补一条 run_finished。

        为什么必须补 run_finished：否则日志里只剩一条悬空的 run_started，事后
        分不清这一轮是"模型调用失败了"还是"进程被杀在半路"，而这两种情况的
        处置完全不同。

        ## 这一层还多担一件流式的事：重试/重发前那一声 reset

        `on_retry` 是**审计 + 通知**两件事：记一条 `delta_reset`（这样"屏幕上那段
        被丢掉了"在日志里查得到），以及把"作废"传给界面（`_DeltaRelay.reset`）。
        两次尝试之间可能什么都没吐（比如第一次就 401），那时候界面那边照发
        （它要清的东西本来就没有，无害），但**审计里不记** —— 记了就是一条假的
        "清空过正文"。
        """
        relay = _DeltaRelay(self.on_delta, self.should_stop, step)

        def retry() -> None:
            """一次新的尝试开始了：把上一次吐出去的东西作废。

            审计里只记一次 —— 判据在 `_DeltaRelay.take_reset_mark` 里，因为
            "这一次重试有没有已经记过账"是那个对象的记账，不是这一段流程的状态
            （在闭包里改一个外层布尔得 `nonlocal`，漏了就是 `UnboundLocalError`，
            而它是从"重试"这条路上抛出来的，症状看起来像模型错了 —— 实测踩过）。

            **这个回调在一次重试里会被调两次**：`call_with_retry` 的 `on_retry`
            一次、适配层自己的 `on_attempt_started` 又调它一次（那是两个不同层的
            "我要重来了"）。界面那边两次都是对的（清两遍等于清一遍），
            审计那边靠 `take_reset_mark` 只记一条。
            """
            if relay.take_reset_mark():
                self._emit("delta_reset", session, run_id, step)
            relay.reset()

        try:
            return call_with_retry(
                self.model, messages, tool_schemas,
                on_attempt=self._attempt_reporter(session, run_id, step),
                # 流式那两样原样透传：retry 既不需要认识它们，也不该认识 ——
                # 它只管"再来一次"，"再来一次对界面意味着什么"是这一层的事。
                #
                # **没有 sink 时传 None**（不是传这个空 relay）：模型层是靠
                # "`on_delta` 是不是 None"选路的，传一个永不发声的 relay 过去
                # 会让 `--no-stream` 静默失效（请求照样走流式）。`on_retry` 不受
                # 影响 —— 它只被"重试之前"用，而不流式时重试本来也没什么要作废的。
                on_delta=relay if relay.streaming else None,
                on_retry=retry,
                # 同一个时钟传下去：一次回合里所有 duration_ms 必须出自同一把尺子，
                # 否则"模型 1.2s + 工具 0.3s"这种加法没有意义。
                clock=self.clock,
            )
        except RunCancelled:
            # **流式让"随时取消"第一次成立**（见 `_DeltaRelay`）：这一次取消发生在
            # 模型往返**中途**，而它同样要给用户一个交代 —— 收尾那两件事在别处都
            # 不会发生（`run()` 里那两个安全点都在循环边界上，我们已经不在那儿了）。
            #
            # 顺序和其他两处（步数用尽、`run()` 顶部的取消）一字不差：审计和落盘
            # 都在 raise 之前。此刻 messages 是一致的（assistant 消息还没 append），
            # 所以这个落盘点安全，而且"半截正文没进历史"这件事也在审计里看得出来
            # —— 上面那条 `delta_reset` 就是它的痕迹。
            self._finish_run(session, run_id, step, run_started, "cancelled")
            self._checkpoint(session)
            raise
        except Exception as exc:
            self._finish_run(
                session, run_id, step, run_started,
                "model_fatal" if _is_fatal(exc) else "model_error",
            )
            raise

    def _attempt_reporter(self, session: Session, run_id: str, step: int):
        """把 retry 模块交出来的 Attempt 记成审计事件。"""

        def report(attempt: Attempt) -> None:
            data: dict[str, Any] = {
                "status": attempt.status,       # ok / error / fatal
                "attempt": attempt.number,
                "duration_ms": attempt.duration_ms,
            }
            # **这一次用的是哪个模型。** `/model` 能中途换模型，而换完之后"这条回答是谁
            # 生成的"必须查得出来 —— 会话历史里只有一句 `[model changed: …]`（那是给
            # 模型读的），而这里每条调用各记一次，所以事后能把一段对话按模型切开。
            #
            # 只有适配器报得出名字时才写（`ChatModel` 的契约里没有 `model` 属性）：
            # 一个恒为空串的键会让读日志的人以为"那次没有模型"，而不是"这个适配器
            # 不说"。和 `reasoning` / `streamed` 同一条规矩。
            if self.model_name:
                data["model"] = self.model_name
            if attempt.error is not None:
                data["error"] = self._preview(attempt.error, AUDIT_PREVIEW_LIMIT)
            # 退避时长（只在这条失败的尝试还会重试时才有）：不记它的话，"这一轮为什么
            # 慢了 3 秒"在日志里是看不出来的 —— 那段等待不属于任何一次请求。
            if attempt.backoff_ms is not None:
                data["backoff_ms"] = attempt.backoff_ms
            if attempt.response is not None:
                data["tool_calls"] = len(attempt.response.tool_calls)
                data.update(self._usage_fields(attempt.response.usage))
                # 流式的**汇总**（不是每一块 —— 那会让日志变成第二个会话文件）。
                #
                # 两个字段各回答一个问题，缺一个就读不出来：
                #   * `streamed` —— 这一次到底是逐字出现的还是整段蹦出来的。
                #     没有它，两种情况的记录一模一样（同样 token、同样耗时），
                #     而"用户报告说没有逐字效果"就只能靠猜；
                #   * `stream_chunks` —— 报了多少块。有的兼容网关会先缓冲整段再
                #     一口气吐出来，那时候 `streamed=true` 但块数是 1，一眼就能
                #     看出"这个网关的流是假的"。
                #
                # 只在真的流了的时候写这两个键（和 `reasoning` 同一条规矩）：
                # 一个恒为 false 的键会让后面做统计的人处处判空。
                if attempt.response.streamed:
                    data["streamed"] = True
                    data["stream_chunks"] = attempt.response.stream_chunks
                    data["streamed_chars"] = len(attempt.response.content or "")
                # 思维链**整段**进审计（决策 5）。
                #
                # 它是审计里第一个"内容型"字段 —— 在此之前审计只有数字、枚举和
                # 200 字符预览，所以读日志的人会下意识以为它很小。三笔代价写在
                # doc/TUI-design.md 的 D2 里，这里只说最要紧的一条：它里面会原样
                # 出现模型读到的代码、路径、以及 ask_user 的答案，所以审计文件从
                # "元数据"变成了"可能含工作区内容"。
                #
                # 键只在真的有思维链时才写（空串和 None 都不写）：一个恒为 null 的
                # 键会让后面做统计的人处处判空，而这一条本来就是可选的。
                if attempt.response.reasoning:
                    data["reasoning"] = attempt.response.reasoning
            self._emit("model_call", session, run_id, step, **data)

            if attempt.status == "error":
                self._debug(f"   ↻ 模型调用失败（第 {attempt.number} 次），准备重试")

        return report

        # 循环只会因为 return 或 raise 退出，走不到这里；留着是为了让"函数总有返回
        # 值"这件事在类型上成立，而不是靠读者推理。
        raise ModelError("模型调用重试逻辑异常：未预期的出路")

    def _status_note(self, session: Session) -> dict[str, str] | None:
        """这次请求尾部那条临时消息：会话状态（注入的那几段）。

        ## 步数不在这里 —— 一次都不在

        它以前是载荷末尾那句逐轮递减的 `剩余步数：80（含本次）`，后来改成"剩 5 步时
        给一次警报"，现在**整个去掉了**。理由是一条比"怎么措辞更好"更基本的判据：

            **模型拿这个数没办法。** 没有任何动作能让它变大，它也无从知道步数花在
            哪儿了 —— 所以逐轮报它是一个只能让人焦虑、不能改变行为的信息。

        真正能改变行为的东西是**策略**，而策略是静态的：提示词里那句"你当前有有限的
        执行预算，请优先完成用户目标，避免过度的工具调用"（`prompts/system.zh.md`）
        一次说清，此后每一轮都成立、且**逐字节不变**（于是它在缓存前缀里命中）。

        代价说清楚：模型**看不到**自己还剩几步，所以它不会为"最后 3 步"改变打法。
        换来的是：不再有噪音、不再有那个冒充 user 的第二条消息、而"该不该收尾"的
        判据回到它本来就该是的那一个 —— 任务做完没有。硬上限仍然由循环兜着
        （`StepLimitExceeded`，而且它可续：会话是完好的）。

        **合成一条，而不是各挂一条。** 载荷尾部因此只有一条临时消息，形状固定 ——
        连续几条 user 是没必要去赌 provider 宽容度的形状。

        整条都不进 `session.messages`：会话文件会平白多出几十条 user 消息，而
        `step_count()` 靠"一条 assistant = 一步"派生，掺进 user 之后"聊了多少轮"
        的语义就糊了。而且那几段逐轮变化，本来就不该被持久化。
        """
        if self.session_notes is None:
            return None
        note = self.session_notes(session.metadata)
        if not note:
            return None
        return {"role": "user", "content": note}

    # --- Context：把历史渲染成这次请求的载荷 --------------------------------

    def _renderer(self, session: Session) -> "ContextRenderer | None":
        """造一个把 `session.messages` 渲染成载荷的渲染器。**没有 Context 就是 None。**

        造一个新的而不是缓存一个：渲染器只有两个字段（store / manager），无状态，
        而缓存它就得处理"会话被换掉了"这件事 —— 那是上一版 `Session` 从 Agent
        里搬出去时踩过的同一个坑。每步造一次的代价是两次属性赋值。
        """
        if self.context is None:
            return None
        from agent_runtime.context.renderer import ContextRenderer

        return ContextRenderer(self.context.store, self.context)

    def _note_text(self, session: Session) -> str | None:
        """载荷末尾那条会话状态。**逐轮的，所以不进历史。**

        没有可注入的会话状态时返回 `None` —— 那时载荷末尾一条临时消息都不加。
        """
        note = self._status_note(session)
        return note["content"] if note is not None else None

    def _calibrate(self, session: Session, usage: TokenUsage | None) -> None:
        """把估算比例对着实测值修一下。**没有 Context 或没有用量就什么都不做。**

        为什么只修比例、不把实测值当成"当前 Context 有多大"：实测值说的是**上一次
        请求**，而两次请求之间 Context 变了（这一步的工具结果刚进来）。拿它当当前
        值会在工具结果很大的时候严重低估 —— 而低估是危险的那一侧。
        """
        if self.context is None or usage is None or not usage.prompt_tokens:
            return
        self.context.calibrate(usage.prompt_tokens)

    def _payload(self, session: Session, *, run_id: str = "") -> list[dict[str, Any]]:
        """这次请求真正发出去的 messages。

        **两条路，形状一样**：

          * 有 Context ⇒ 历史按档位渲染（tool 消息的内容从 ArtifactStore 现取）；
          * 没有 Context ⇒ 历史原样。

        两条路的**顺序和形状逐字节一致**，区别只在 tool 消息的正文从哪来。这一点
        是刻意的：Context 是"内容的来源"的替换，不是载荷形状的替换（见
        `context/renderer.py` 的模块 docstring）。

        载荷末尾那条会话状态（任务列表 / 技能 / 后台任务）**不进
        `session.messages`**：它逐轮变化，本来就不该被持久化。它进 Context 的账本
        （见下面的 `set_notes`）—— 它也有可能是唯一让这次请求超预算的那部分。

        **步数不在载荷里**（见 `_status_note`）：那是静态策略，写在系统提示词里。
        """
        renderer = self._renderer(session)
        note = self._note_text(session)
        notes = [note] if note else []

        messages: list[dict[str, Any]] = []
        for message in session.messages:
            if renderer is None or message.get("role") != "tool":
                messages.append(message)
            else:
                rendered = dict(message)
                rendered["content"] = renderer.render_tool_content(message)
                messages.append(rendered)

        if renderer is None:
            if note:
                messages.append({"role": "user", "content": note})
            return messages

        # 预算：先按当前档位算出这次要花多少，超了就降级（只降不升，见 budget.py）。
        # **每步都调**，但只有真的超了才会动 —— 动过之后版本号变了，那条
        # `context_degraded` 事件里会记下来。
        self.context.set_notes(notes)
        # 载荷里降不动的那一半（系统提示词、历史消息、tool 消息的引用行）先算出来
        # 交给预算 —— 少了它，"还塞得下"这个判断是假的，见 _fixed_payload_tokens。
        fixed = self._fixed_payload_tokens(session)
        degraded = self.context.fit(renderer.render_item, extra=fixed)
        if degraded:
            self._emit(
                "context_degraded", session, run_id, 0,
                items=len(degraded),
                estimated=self.context.last_estimate,
                limit=self.context.budget.effective_limit,
                # 固定开销单独报一个数：事后要能分清这一轮是被**历史**挤的，还是被
                # Artifact 挤的。两者的处置完全不同 —— 前者该换会话或清历史，后者
                # 才是降级本身在正常工作。
                fixed=fixed,
                changes=[f"{d.artifact_id}:{d.before.value}->"
                         f"{d.after.value if d.after else 'removed'}"
                         for d in degraded][:20],
            )
        session.context = self.context.state
        if note:
            messages.append({"role": "user", "content": note})
        return messages

    def _fixed_payload_tokens(self, session: Session) -> int:
        """一次请求里**降不动的那部分**占多少 token。

        ## 它为什么必须存在

        预算那一层只能降 Context 里的 Artifact（`state.items`），而载荷里还有一整半
        不是 Artifact 的东西：系统提示词、用户和助手的每一句话、以及每条 tool 消息
        那行 `[artifact art_x · 12480 字符 · read_file]` 引用。它们**同样占窗口**，
        却谁也不计量。

        症状在长会话里必然出现：Artifact 全降到 0 之后预算仍说"塞不下"，却无处可降
        —— 真正超窗的是那堆历史。provider 回一个 400，看起来像"上下文太长"，但翻遍
        Context 的账本都看不出是谁超的。

        ## 三个口径上的讲究

          * **tool 消息只算引用行，不算正文。** 正文在渲染时才从 ArtifactStore 取
            出来，那一份由 `ContextBudget.estimate_items` 计量 —— 这里再算一遍就是
            同一个东西记两笔。**例外是带不上 `artifact_id` 的 tool 消息**（老会话，
            或者 `hydrate` 没覆盖到的那些）：渲染时它们是**原样透传**的
            （`renderer.render_tool_content` 的 case 4），所以那种必须按全文计 ——
            漏了就是低估，而低估是危险的那一侧。
          * **`tool_calls` 的参数要算。** 助手上一步说"我要调 write_file"时带的那份
            JSON 会一路留在历史里（此后每一轮都重发），它可能很长（一份文件正文）；
            只算文本内容会把它整个漏掉。
          * **偏大是安全的。** 估高了只是提前降级一点，估低了是请求直接失败
            （见 `budget.py` 模块 docstring 里那条"估低 ⇒ 400"）。

        开销上它是每步一次的线性扫描，量级和 `estimate_items` 扫全部 Artifact 正文
        是同一档 —— 后者本来就在做同一件事，所以这里没有引入新的量级。
        """
        assert self.context is not None, "_fixed_payload_tokens 只在有 Context 时被调用"
        budget = self.context.budget
        total = 0
        for message in session.messages:
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            if message.get("role") == "tool":
                # 这条消息本身的固定开销已经由 estimate_items 记过，这里只补引用行。
                if self._artifact_of(message) is not None:
                    total += budget.tokens(text)
                else:
                    total += MESSAGE_OVERHEAD + budget.tokens(text)
                continue
            total += MESSAGE_OVERHEAD + budget.tokens(text)
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    total += budget.tokens(str(function.get("name") or ""))
                    total += budget.tokens(str(function.get("arguments") or ""))
        return total

    def mark_context_messages(self, session: Session) -> None:
        """把"哪几条消息属于 stable 区"标进 Context。**每个回合开头调一次。**

        为什么不在 `add()` 的时候标：一条消息进历史的那一刻还不知道它将来是不是
        "这个回合的任务"（第一条 user 在第一次 `run()` 里才出现，而那时
        `session.messages` 里已经有 system 了）。位置的判据只有整份历史在手时才算
        得出来，所以它在回合开头统一算一次（`message_marks`）。

        **它只对有 Artifact 的消息生效**（tool 消息），因为 stable/system 那两条
        判据（系统提示词、第一条 user）**各自都不是 Artifact**：

          * 系统提示词就是那条 `role="system"` 消息，渲染时原样通过，没有档位可言；
          * 第一条 user 也只是历史里的一条消息 —— 它会被挤掉吗？**不会**：预算那一层
            只看 Context 里的条目，而历史里的普通消息根本不参与降级。

        所以「本回合的任务不许被挤掉」这条保证在这里是**天然成立**的，不需要一条
        ContextItem 去表达它。真正需要 pinned 的是**工具结果里那些来自任务本身的
        Artifact**（比如一开始 `read_file` 读进来的项目说明）—— 它们才是会被降级的
        东西，而 `mark_context_messages` 会把它们标成 stable+pinned。

        **它不覆盖已经进过 Context 的条目**：`add` 是幂等的、但会把档位重置成
        full —— 那正是"只降不升"要防的抖动。所以已经在里面的原样不动。
        """
        if self.context is None:
            return
        for index, mark in message_marks(session.messages).items():
            message = session.messages[index]
            artifact_id = self._artifact_of(message)
            if artifact_id is None:
                # 这一条不是 Artifact（系统提示词、第一条 user）—— 它**已经**不会被
                # 挤掉（见 docstring），所以这里什么都不必做。
                continue
            if self.context.item(artifact_id) is None:
                self.context.add(
                    artifact_id,
                    zone=mark["zone"],
                    priority=mark["priority"],
                    pinned=mark["pinned"],
                    notify=False,
                )

    @staticmethod
    def _artifact_of(message: dict[str, Any]) -> str | None:
        from agent_runtime.context import ref as _ref

        return _ref.artifact_id_of(message)

    def run(self, session: Session, user_input: str, max_steps: int = 120) -> str:
        # 回合的起点：run_finished 里的 duration_ms 从这里算起。放在最前面（而不是从
        # 第一次模型请求算起）是因为"这一轮花了多久"要含上追加消息、落盘这些开销 ——
        # 它们没被单独埋点，交给 CLI 那行汇总里的"未归因"去吸收，比假装它们不存在诚实。
        run_started = self.clock()

        # messages 不再每次新建，而是复用传入会话里已有的那份 —— 它让两次 run()
        # 之间、乃至两个进程之间，对话得以延续。会话是参数而不是 Agent 的身份，
        # 所以同一个 Agent 可以服务多个会话。
        # 系统提示词由 Session.new() 在会话创建时写一次，这里不再插入。
        messages = session.messages

        # **换过模型就先留一句话，再记下"这一轮用的是什么"。**
        #
        # 顺序是这个功能唯一的讲究，两条都不能换：
        #
        #   * 那句话要在**用户这句话之后、第一个请求之前**进历史 —— 它说的是"从这个点
        #     开始用谁"，而这个点就是这一轮。插在用户消息之前的话，模型读到的顺序是
        #     "换了 → 用户说了话"，那也说得通，但和"谁回答了上一轮"就对不上了；
        #   * `notice()` 要在 `record_use()` **之前**调："上一轮是谁回答的"这个问题的
        #     答案在 `last_used` 里，而 `record_use()` 当场就把它盖成新的了。
        #
        # 判据是"选中的 ≠ 上一轮用过的"，不是"刚刚有人调过 /model"（见 SessionModel）：
        # 一轮正跑着的时候按 `/model`，那一轮已经用旧模型发出去了，所以这句话留到下一轮。
        # 换了又换回来时两者相等，于是不留 —— 中间那次没有产生任何回答，说"换过"是假的。
        if self.session_model is not None and self.session_model.notice_needed():
            messages.append(self.session_model.notice())
            self.session_model.record_use()
        # 用户这句话**排在换模型那句话之后**：见上面第一条。
        messages.append({"role": "user", "content": user_input})

        # **Context 与历史对齐，然后才落盘。**
        #
        # 两件事，顺序不能换：
        #
        #   1. `mark_context_messages` 给系统提示词和**第一条** user 立 stable/pinned
        #      标记（见 `message_marks`：判据是位置，而位置整份历史在手时才算得出来）；
        #   2. `session.context` 指向 manager 的状态 —— 落盘那条路读的是 Session，
        #      而它不该知道 ContextManager 存在（见 state/store.py）。
        #
        # 对齐放在 `_checkpoint` **之前**：那样第一次落盘就带着完整的 Context 状态，
        # 中途被杀也能恢复成"当时真的发生过什么"。
        if self.context is not None:
            self.mark_context_messages(session)
            session.context = self.context.state
        self._checkpoint(session)

        # 一次 run 的所有事件共用同一个 run_id。没有它，日志里就分不清哪几条事件
        # 属于同一次提问 —— 而"这一轮为什么失败"恰恰是按轮次问的。
        run_id = uuid4().hex[:8]
        self._emit("run_started", session, run_id, 0,
                   user_input=self._preview(user_input, AUDIT_PREVIEW_LIMIT))

        # 工具定义在一次 run 里不会变，所以取一次就够；下行 debug 也就顺手用同一份。
        tool_schemas = self.tools.schemas()

        # 最后一步调了哪些工具 —— 撞上限时它进异常消息，是"卡在什么上面"唯一的线索。
        last_tools: list[str] = []

        for step in range(max_steps):
            # 取消检查点。**位置是它唯一的讲究**：这里 messages 是一致的（上一步的
            # 工具结果全 append 完了，★ 也落过盘），所以从这里退出不会留下一条带
            # tool_calls 却没有对应结果的 assistant 消息。
            #
            # 「什么时候不能取消」写在 `RunCancelled` 的 docstring 里。
            if self.should_stop is not None and self.should_stop():
                self._debug("!! 收到取消，停止")
                # 顺序和步数用尽那条一样：审计和落盘都必须在 raise **之前**完成，
                # 否则日志里只剩一条悬空的 run_started。
                self._finish_run(session, run_id, step, run_started, "cancelled")
                self._checkpoint(session)
                raise RunCancelled(step)

            self._debug(
                f"── step {step + 1}/{max_steps}  "
                f"消息数={len(messages)} "
                f"[{', '.join(m['role'] for m in messages)}]  "
                f"工具数={len(tool_schemas)}"
            )

            response = self._complete_with_retry(
                session, run_id, step + 1,
                # 载荷尾部那条会话状态是按本次请求拼的，不写回 messages —— 见 _status_note
                self._payload(session, run_id=run_id),
                tool_schemas,
                run_started,
            )

            self._debug(
                f"   ← 模型返回  content={'有' if response.content else '无'}  "
                f"tool_calls={len(response.tool_calls)}"
            )
            # **用 provider 实测的输入 token 数校准预算的估算。**
            # 它放在这里（而不是收尾时）：`response.usage` 是唯一一次实测机会，
            # 而"下一步要不要降级"就靠这次校准。没有它，估算器只能靠猜 —— 而
            # 猜偏的方向决定后果（估高了白降级，估低了请求 400）。
            self._calibrate(session, response.usage)
            # 思维链**不在这里打了** —— 它现在整段进审计（上面 `_attempt_reporter`），
            # 而同一份正文有两条出口正是 README 第 3 条设计原则反对的事。想读它就去
            # `--audit` 或者直接读 `.tudouni/logs/<id>.jsonl` 里那条 model_call。
            if response.content:
                self._debug_lazy(
                    lambda: f"      content: {self._preview(response.content)}"
                )

            assistant_message = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in response.tool_calls
                ]

            messages.append(assistant_message)
            # 注意：这里**不能**落盘。此刻 messages 处于半截状态 —— assistant 记下了
            # 要调哪些工具，但结果还没跟上。API 会直接 400 拒绝这种历史，
            # 所以半截状态一旦存下来，会话就再也发不出去了。

            if not response.tool_calls:
                self._debug("   无工具调用 → 返回最终结果")
                self._finish_run(session, run_id, step + 1, run_started, "answered")
                self._checkpoint(session)  # 完整：这条 assistant 消息没有任何 tool_call
                return response.content or ""

            last_tools = [call["name"] for call in response.tool_calls]

            # 一批走完（可能并发、也可能逐条，见 _run_batch），结果一一对应地回到
            # messages 里 —— 顺序永远是模型给出的顺序。
            outcomes = self._run_batch(response.tool_calls, session, run_id, step + 1)

            for call, outcome in zip(response.tool_calls, outcomes):
                messages.append(self._tool_message(call, outcome, session, step + 1))

            # ★ 唯一的常规落盘点：到这里 assistant 的每个 tool_call 都有了对应的
            #   tool 结果，messages 重新一致。崩溃恢复最多退回上一个 ★，不会拿到
            #   一个发不出去的历史。
            self._checkpoint(session)

        # 步数用尽：任务既没失败、也没收尾，所以它既不是返回值（会被 print 进
        # stdout，装成答案），也不是 ModelError（那意味着重试或改配置）。
        #
        # 顺序要紧 —— 审计和落盘必须在 raise 之前完成。否则日志里只剩一条悬空的
        # run_started，事后分不清这一轮是"步数用尽"还是"进程被杀在半路"；而异常
        # 消息里承诺的"会话是完好的"，也得先真的存下去才算数。
        # 撞上限的位置在循环顶部，那里 messages 一致（上一步的结果全 append 完、
        # ★ 也落过盘了），所以这个 raise 不损坏会话。
        self._debug(f"!! 达到最大步数 {max_steps}，停止")
        self._finish_run(session, run_id, max_steps, run_started, "max_steps")
        self._checkpoint(session)
        raise StepLimitExceeded(max_steps, last_tools)

    # --- 一批工具调用 -------------------------------------------------------

    def _tool_message(
        self,
        call: dict[str, Any],
        outcome: _Outcome,
        session: Session,
        step: int,
    ) -> dict[str, Any]:
        """一条工具结果该长什么样 —— **这是它唯一的构造点**。

        有 Context 时，正文收进 ArtifactStore，消息里只留一句引用（见
        `context/ref.py`）；没有 Context 时，正文原样留在消息里（重构之前的行为）。

        **`tool_call_id` 一条都不能少。** provider 要求每个 `tool_calls` 都有对应的
        结果，少一条就 400 —— 而且那个错误看起来像"上下文太长"。所以无论走哪条
        路，这个方法都必须返回一条完整的 tool 消息。
        """
        if self.context is None:
            return {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": outcome.text,
            }

        from agent_runtime.context import ref as _ref

        execution = ToolExecution(
            tool=call["name"],
            arguments=_arguments_of(call),
            result=ToolResult(outcome.text, outcome.audit),
            status=outcome.status,
        )
        artifact = self.processor.process(self.context.store, execution)[0]
        self.context.add(artifact.artifact_id, notify=False)
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": _ref.build(artifact.artifact_id, artifact.chars, call["name"]),
            # **显式的 id 字段**：程序不该靠解析一段人话找 Artifact（见 ref.py）。
            "artifact_id": artifact.artifact_id,
        }

    def _parallel_safe(self, call: dict) -> bool:
        """这条调用所在的工具声明了"能并行"吗。

        用 `.get` 是为了让"工具名不认识"落到 False（回退串行）而不是抛出来 ——
        真正该报的那个错由 _prepare 在里面按原来的方式报，这里只负责选路径。
        """
        try:
            return self.tools.get(call["name"]).parallel_safe
        except KeyError:
            return False

    def _run_batch(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """跑完一批工具调用，返回与 batch **一一对应**（同序）的结局。

        **串行是默认，并行是例外**，而例外只在一种形状下成立：整批都是
        `parallel_safe`（也就是只读）且不止一条。任何一个不能并行的调用都会把整批
        按老路逐条跑 —— 也就是今天的行为逐字节不变。

        为什么是"整批"而不是"把能并行的挑出来并行"：一个批次里的调用隐含了顺序
        语义。最常见的形状是 `[read_file(a), write_file(a), read_file(a)]`（读、改、
        读回验证 —— 提示词里明确要求写完之后读回来确认）。把两个读挑出来并发、把写
        留在串行，读回验证就可能发生在写之前：模型会读到旧内容，然后报告"已确认改好
        了"。**那是静默错误，而且它伪装成验证通过。** 整批一起退回去，这个偏序问题
        就不存在了。
        """
        if len(batch) >= 2 and all(self._parallel_safe(call) for call in batch):
            return self._run_parallel(batch, session, run_id, step)
        return self._run_serial(batch, session, run_id, step)

    def _report_tool_call(
        self, call: dict, session: Session, run_id: str, step: int, index: int = 0
    ) -> None:
        """两条路径共用的"模型要调什么"那一句（debug + 审计）。

        `call_id` / `tool_index` 是给**跨进程的消费者**用的（决策 4）：同一批里两个
        `read_file`（a.py 和 b.py）在事件流里长得一模一样，只能靠"按顺序发"这个隐式
        约定配对 —— 而"顺序一致"是 `_run_parallel` 的实现细节，不是契约。
        前端要把调用和结果配成一条，就得有一个稳定的身份。
        """
        # 惰性：write_file 的 content 可以很长，而 _preview 要把它扫一遍。
        self._debug_lazy(
            lambda: f"   → 工具调用 {call['name']}"
                    f"({self._preview(call['arguments'], 120)})"
        )
        self._emit(
            "tool_call", session, run_id, step,
            tool=call["name"],
            call_id=call["id"],
            tool_index=index,
            arguments=self._preview(call["arguments"], AUDIT_PREVIEW_LIMIT),
        )

    def _report_tool_result(
        self, call: dict, outcome: _Outcome, session: Session, run_id: str, step: int,
        parallel: bool = False, index: int = 0,
    ) -> None:
        """两条路径共用的"这条调用结果如何"那一句（审计 + debug）。"""
        self._emit(
            "tool_result", session, run_id, step,
            tool=call["name"],
            call_id=call["id"],
            tool_index=index,
            status=outcome.status,      # ok / denied / invalid_args / error
            chars=len(outcome.text),    # 只记长度，不记全文 —— 全文已经在会话文件里了
            duration_ms=outcome.duration_ms,
            # 工具自己知道、而这一层推不出来的字段（ask_user 的 question_status /
            # human_wait_ms）。键名由工具负责不撞上面那几个 —— 它们已经在这里了。
            **outcome.audit,
            # 只有真并发了才写这个键：没有它，事后从日志里分不出这一批是并发还是逐条，
            # 而"这一批为什么快/慢"正是拿着日志要回答的问题。
            **({"parallel": True} if parallel else {}),
        )
        # 惰性 —— 这一行是那笔白工的主项：工具结果动辄几十万字符（read_file 不分页），
        # 而 _preview 会把整段扫一遍。见 _debug_lazy。
        self._debug_lazy(
            lambda: f"   ← 工具结果 ({len(outcome.text)} 字符): {self._preview(outcome.text)}"
        )

    def _run_serial(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """逐条：报调用 → 裁决 → 执行 → 报结果。这就是原来那个 for 循环。

        **裁决和执行在同一条调用上相邻，这一点不能改**：人是在看到上一条的结果之后
        才被问到下一条的，而上一条的结果正是他判断"这条该不该放行"的依据之一。
        """
        outcomes: list[_Outcome] = []
        for index, call in enumerate(batch):
            self._report_tool_call(call, session, run_id, step, index)
            outcome = self._run(self._prepare(call, session, run_id, step))
            self._report_tool_bug(outcome)
            self._report_tool_result(call, outcome, session, run_id, step, index=index)
            outcomes.append(outcome)
        return outcomes

    def _run_parallel(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """整批只读工具：先把裁决做完，再并发执行。

        **事件一律由这里（主线程）发**，顺序和串行路径完全一样 —— 先按原顺序报完
        整批 tool_call，再按原顺序报 tool_result。所以 on_event 不需要是线程安全的：
        那是注入进来的实现，"它必须自己加锁"会是一条没人想得到的隐式契约。

        代价是这一批的事件在整批跑完之后才出现。对只读批次来说可以接受（它们本来就
        是秒级以下的活），换来的确定性更值钱 —— 按完成顺序发事件的话，同一个会话
        两次跑出来的审计顺序会不一样。
        """
        for index, call in enumerate(batch):
            self._report_tool_call(call, session, run_id, step, index)

        # 裁决（含问人）仍在主线程、仍按原顺序。这里比串行路径多了一点：整批先问完
        # 再执行。只读工具在默认策略下不会问人（LOW 自动放行），所以这个差别平时看
        # 不见；真问到人时（.tudouni.json 里把 auto_approve 写成了 []），它也是可以
        # 接受的 —— 这一批都是只读的，人的判断不依赖前一条的结果。
        prepared = [self._prepare(call, session, run_id, step) for call in batch]

        started = self.clock()
        with ThreadPoolExecutor(
            max_workers=min(len(prepared), MAX_PARALLEL),
            thread_name_prefix="tool",
        ) as pool:
            # map 按**输入顺序**返回，所以调度不影响 messages 里 tool 结果的顺序，
            # 也不影响事件的顺序：同一个会话跑两次，历史是一样的。
            outcomes = list(pool.map(self._run, prepared))
        wall_ms = int((self.clock() - started) * 1000)

        for index, (call, outcome) in enumerate(zip(batch, outcomes)):
            self._report_tool_bug(outcome)
            self._report_tool_result(
                call, outcome, session, run_id, step, parallel=True, index=index,
            )

        # 这一批实际占了多长墙上时间。**必须单独记一笔**：并发时逐条 duration_ms
        # 相加大于墙上时间（两个 5 秒的工具并行，和是 10 秒），而 cli.py 那个
        # "未归因 = 回合总 - 各项之和"依赖的是互不重叠的几段 —— 不记它，那几项一
        # 相加就会超过回合总，差值被 max(0, ...) 悄悄吞掉，报出一行恒为 0 的"未归因"。
        self._emit(
            "tool_batch", session, run_id, step,
            calls=len(outcomes),
            wall_ms=wall_ms,
            tools=",".join(call["name"] for call in batch),
        )
        return outcomes

    def _prepare(self, call: dict, session: Session, run_id: str, step: int) -> _Prepared:
        """一条调用的前半段：认工具、解析参数、过权限关。

        这是原来 `_execute_tool` 的前半段，异常映射一个字都没改（工具名不认识是
        KeyError、参数不是 JSON 是 JSONDecodeError，都变成"工具执行失败"）。

        ValidationError **不在这里**：参数校验发生在 `tool.execute()` 里面，也就是
        `_run` 那一段 —— 见那里的注释。
        """
        started = self.clock()

        try:
            tool = self.tools.get(call["name"])
            arguments = json.loads(call["arguments"])

            # 权限关卡。插在这里是因为 tool.execute() 是全项目唯一让副作用发生的
            # 地方（它内部才调到 handler），所以这是天然的收口点。
            denial = self._check_permission(tool, arguments, session, run_id, step)
            if denial is not None:
                # denied 那一支什么都没执行：记 0 而不是"从函数入口算起的耗时"，否则
                # 这个数字会变成审批开销的同义词（它属于 permission 事件）。
                return _Prepared(call, None, None, False, _Outcome(denial, "denied", 0))

            return _Prepared(call, tool, arguments, tool.parallel_safe, None)

        except Exception as exc:
            return _Prepared(
                call, None, None, False,
                _Outcome(
                    f"工具执行失败：{type(exc).__name__}: {exc}",
                    "error",
                    int((self.clock() - started) * 1000),
                    self._bug_report(exc),
                ),
            )

    def _run(self, prepared: _Prepared) -> _Outcome:
        """一条调用的后半段：真的执行它。**能并发的只有这一段。**

        四条出口都收敛成一个 _Outcome，事件交给调用方（主线程）发 —— 状态分类只有
        这里知道（校验失败、被拒、还是运行出错），但"事件长什么样"只写一遍。
        """
        if prepared.settled is not None:
            return prepared.settled

        tool, arguments = prepared.tool, prepared.arguments
        assert tool is not None and arguments is not None, "settled 为空时 tool/arguments 必然有值"

        started = self.clock()          # ← 从这里才算"执行"
        try:
            result = tool.execute(arguments)

            # ToolResult 是"文本 + 审计字段"的那种返回值（目前只有 ask_user）。放在
            # json.dumps 那一步**之前**：它不是要序列化的数据，而是已经渲染好的文本，
            # 只是多带了几个只有工具自己知道的字段。
            if isinstance(result, ToolResult):
                return _Outcome(
                    result.text, "ok",
                    int((self.clock() - started) * 1000),
                    audit=result.audit,
                )

            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return _Outcome(text, "ok", int((self.clock() - started) * 1000))

        except ValidationError as exc:
            # 这一支必须放在 except Exception 之前，否则永远不会命中。
            # 参数校验失败是「模型可以自己改对」的错误，所以要明确告诉它
            # 哪个字段、什么毛病；这跟「工具运行出错」是两回事。
            return _Outcome(
                f"参数校验失败：{self._format_args_error(exc)}", "invalid_args",
                int((self.clock() - started) * 1000),
            )

        except InvalidArgsError as exc:
            # 同一件事的另一个来路：**校验方不是 pydantic**。外部工具（MCP）的 schema
            # 权威在 server 那一侧，参数不合法是 server 回的一句话（见 tools/tool.py）。
            # 状态分类必须和上面那一支一样 —— 对模型来说都是"我自己能改对"，
            # 而落进下面那支 "error" 会让它以为工具坏了、白白换策略。
            return _Outcome(
                f"参数校验失败：{exc}", "invalid_args",
                int((self.clock() - started) * 1000),
            )

        except Exception as exc:
            return _Outcome(
                f"工具执行失败：{type(exc).__name__}: {exc}", "error",
                int((self.clock() - started) * 1000),
                self._bug_report(exc),
            )

    @staticmethod
    def _bug_report(exc: BaseException) -> str | None:
        """非预期异常才要完整栈；工具自己抛的是预期内的，带回来也没人打。

        工具自己抛的（文件不存在、路径越界、JSON 坏）都是预期内的，模型拿到一句话
        就够了。其余异常很可能是**我们自己的 bug** —— 把它伪装成「工具执行失败」喂给
        模型，模型会老老实实去改参数，于是你在调试一个根本不存在的问题。所以这些要
        在 stderr 留下完整 traceback。
        """
        if isinstance(exc, _TOOL_LEVEL_ERRORS):
            return None
        return "".join(traceback.format_exception(exc))

    def _report_tool_bug(self, outcome: _Outcome) -> None:
        """把 _bug_report 攒下的栈打出来。**只在主线程调用。**

        traceback.print_exc() 是一行一次写，而 print 本身只保证"一次调用"是原子的 ——
        两个工作线程同时打，栈就会交错成一段读不懂的东西。所以栈是带回主线程打的。
        """
        if outcome.traceback is None:
            return
        self._warn("工具抛出未预期的异常，完整栈如下（给模型的文本已简化）：")
        print(outcome.traceback, file=sys.stderr, end="")


    def _check_permission(
        self,
        tool: Tool,
        arguments: Mapping[str, Any],
        session: Session,
        run_id: str,
        step: int,
    ) -> str | None:
        """走一遍权限关卡，并把裁决记进审计。

        裁决本身在 security/gate.py 里（纯函数、可单测）；这里只负责两件本层才
        知道的事：怎么把结果记进审计，以及怎么在 debug 上说出来。

        permission 事件是审计最要紧的一项 —— "谁批准了什么"除了这里没有别的地方
        知道。无论放行还是拒绝都要发。
        """
        result = check_permission(tool, arguments, self.policy, self.asker, self.memory,
                                  clock=self.clock, autopilot=self.autopilot)

        _DEBUG_BY_OUTCOME = {
            "auto_allowed": "   ✓ 自动放行",
            # 文案不写"这一轮没人在看"：`/autopilot`（决策 25）让这个模式也能在有人
            # 看着的时候打开 —— 它说的是"不问审批"，不是"没有人在"。
            "autopilot": "   ✓ 自动放行（autopilot：不问审批）",
            "rule_allowed": "   ✓ 自动放行（你之前按过 t）",
            "command_allowed": "   ✓ 自动放行（命中命令规则）",
            "approved": "   ✓ 用户批准",
            "user_denied": "   ✗ 用户拒绝",
            "policy_denied": "   ✗ 权限拒绝（策略禁止）",
            "no_asker": "   ✗ 需要审批但未配置 asker，按拒绝处理",
        }
        self._debug(f"{_DEBUG_BY_OUTCOME[result.outcome]} {tool.name}")

        self._emit(
            "permission", session, run_id, step,
            tool=tool.name,
            risk=tool.risk.value,
            decision=result.decision.value,
            outcome=result.outcome,
            # 参数只留预览：write_file 的 content 可能含敏感内容，审计日志不该抄全文
            arguments=self._preview(json.dumps(arguments, ensure_ascii=False), AUDIT_PREVIEW_LIMIT),
            # 等待审批的时长只在真的问过人才有意义
            **({} if result.waited_ms is None else {"waited_ms": result.waited_ms}),
            # 这一次批准**顺带**改了将来的行为（人按了 t）。省掉它的话，日志里那次
            # 批准看起来就只是"批准了一次"，而后面几十次 shell 无人问津的原因只能靠猜。
            **({} if not result.remembered else {"remembered": sorted(result.remembered)}),
            # 凭哪条命令规则放过的。没有它，日志只能看出"这条命令没问过"，看不出凭哪一条
            # —— 而"撤掉哪条规则能把它变回要问"正是事后最想知道的事。
            **({} if result.rule is None else {"rule": " ".join(result.rule)}),
        )
        return result.denial

    @staticmethod
    def _format_args_error(exc: ValidationError) -> str:
        """把 Pydantic 的报错压成一行：「哪个字段: 什么毛病」。

        直接 str(exc) 有 200+ 字符，还带一个 errors.pydantic.dev 文档链接 ——
        那是给人看的。这段文本会进入对话历史，此后每轮请求都重复计费，所以只
        保留模型能用来修正参数的部分。
        """
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<参数>'}: {err['msg']}"
            for err in exc.errors()
        )
