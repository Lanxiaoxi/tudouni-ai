"""系统提示词与步数提示。

这一组测试守的是一条很容易被无声破坏的边界：**什么进会话文件，什么只进这一次请求。**

  - 系统提示词的静态部分来自 prompts/system.zh.md；
  - 动态部分（目前只有操作系统）在新建会话时拼在静态部分**后面**；
  - 步数提示每轮临时拼进载荷，从不落盘。

三者的归属一旦搞混，后果不是"结果不对"，而是会话文件被污染、或者缓存前缀被逐轮
打断 —— 而后者正是这个项目实测出来最贵的东西（未命中的输入比命中贵约 50 倍）。
"""

import platform
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from agent_runtime.agents import Agent
from agent_runtime.agents import StepLimitExceeded
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state import session as session_module
from agent_runtime.state.session import SYSTEM_PROMPT_PATH, load_system_prompt
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.jobs import JobBoard
from agent_runtime.tools.tool import RiskLevel
from agent_runtime.tools.builtin.webfetch import WebFetch
from agent_runtime.tools.builtin.websearch import Findings, Hit, WebSearch

from fakes import ScriptedModel, tool_call, usage


# 提示词与工具描述之间，连续重合多少个字才算「抄过去了」。
# 中文里十几个字连着一模一样，不可能是巧合 —— 正常的措辞碰撞到不了这个长度。
MIN_SHARED_PHRASE = 12


def system_content(session: Session) -> str:
    """会话里那条 system 消息的正文。"""
    assert session.messages[0]["role"] == "system"
    return session.messages[0]["content"]


def test_static_prompt_comes_from_the_file():
    """提示词住在文件里，改文件就改行为 —— 不需要动 Python，也看得见 diff。

    顺带钉住一条缓存性质：发出去的正文以文件内容打头，**逐字节相同**。
    """
    on_disk = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert on_disk
    assert system_content(Session.new("s")).startswith(on_disk)


def test_prompt_file_has_no_meta_commentary():
    """文件里不能有维护者注释 —— 整份内容会原样发给模型并按 token 计费。

    给维护者看的说明写在 state/session.py 的 docstring 里。
    """
    assert "<!--" not in SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def test_dynamic_part_is_appended_after_the_static_part():
    """动态部分挂在**末尾**，而且目前只有操作系统这一行。

    工作区路径是**刻意不放**的（理由见 state/session.py 里 _env_block 的 docstring）。
    这条测试把它钉住，因为它是整份提示词里最容易被"顺手加回去"的一行 —— 所有参照
    实现都会告诉模型 cwd：Claude Code 的 <env>、Codex 的 <environment_context>。

    插在前面也不行：动态内容一旦混进前缀，它后面的每个 token 每轮都按未命中计费。
    """
    content = system_content(Session.new("s"))

    assert content.startswith(load_system_prompt())
    assert content.split("## 运行环境")[1].strip() == f"- 操作系统：{platform.system()}"


def test_editing_the_prompt_file_does_not_touch_already_saved_sessions(workdir, monkeypatch):
    """改提示词只影响**此后新建**的会话，已落盘的会话保留它创建时的那一份。

    这不是实现细节，是操作上的事实：.sessions/ 里的旧会话不会因为你改了
    prompts/system.zh.md 就换掉提示词 —— 想让它们用新的，只能开新会话。
    """
    store = JsonSessionStore(workdir)
    store.save(Session.new("old"))
    saved = system_content(store.load("old"))

    new_prompt = workdir / "system.zh.md"
    new_prompt.write_text("全新的提示词", encoding="utf-8")
    monkeypatch.setattr(session_module, "SYSTEM_PROMPT_PATH", new_prompt)

    assert system_content(Session.new("fresh")).startswith("全新的提示词")
    assert system_content(store.load("old")) == saved


def overlapping_phrases(text: str, other: str, n: int = MIN_SHARED_PHRASE) -> list[str]:
    """找出 text 与 other 之间所有长度为 n 的字面重合片段。"""
    return [
        other[start:start + n]
        for start in range(max(len(other) - n + 1, 0))
        if other[start:start + n] in text
    ]


def without_tool_names(text: str, names: list[str]) -> str:
    """把工具名抠掉再比 —— **工具名不是"被复述的事实"**。

    提示词必须点名工具（"用 grep 别用 shell"），而工具描述里当然也有自己的名字。
    于是 `shell_background` 这 16 个字本身就够长，会把 12 字那道门槛直接顶穿：
    " shell_backg"、"shell_backgr"…… 一路报下去，而它们一个字的信息都没重复。

    抠掉是安全的：两边同时少掉同一个名字，不会凭空造出一段新的重合（剩下的碎片
    比 12 字短得多）。
    """
    for name in names:
        text = text.replace(name, "")
    return text


def test_overlap_checker_rejects_a_pasted_phrase():
    """先证明检查器本身有效 —— 否则下面那条可能是在跑一个永远为真的断言。"""
    pasted = "整个文件会被替换，不是追加"
    assert overlapping_phrases(pasted, pasted)
    assert overlapping_phrases("完全无关的一句话", pasted) == []


def test_prompt_does_not_restate_tool_descriptions():
    """提示词里不许复述工具自己的行为 —— 同一份事实只写一遍。

    这两处是最容易走岔的一对：工具行为改了、描述跟着改，提示词里那句旧话还留着，
    于是模型在同一个请求里同时收到两份互相矛盾的说法。这里按「长片段的字面重合」
    判 —— 十几个字连着一模一样，只可能是拷贝过去的。

    删掉重复**不等于删掉信息**：工具描述在请求的 tools 数组里，提示词在 system
    消息里，两者每次都一起发出去。事实只换了个位置，而且换到了模型决定要不要调这
    个工具时更近的地方。

    **量的是 `full_registry()` 而不是 `create_tool_registry(".")`**（后台命令那四个
    加进来时这里就漏了一次：裸注册表里没有它们，于是新加的那一节提示词**根本没被
    比对过**）。一条只管一部分工具的检查，和没有这条检查的区别只是"让人以为有"。
    """
    prompt = load_system_prompt()
    with full_registry() as registry:
        names = [tool.name for tool in registry.all()]
        bare_prompt = without_tool_names(prompt, names)
        violations = [
            f"  {tool.name}: {phrase!r}"
            for tool in registry.all()
            for phrase in overlapping_phrases(
                bare_prompt, without_tool_names(tool.description, names)
            )
        ]

    assert not violations, "提示词复述了工具描述：\n" + "\n".join(violations)


def test_prompt_carries_the_rules_no_tool_description_can_carry():
    """有些事只能写在提示词里，因为没有别的地方会告诉模型。

    权限审批就是标准例子：risk 等级**故意不进 schema**（有测试盯着），所以「需要
    审批的工具会被拦下来问用户」是模型唯一能得知批准机制的途径。

    权限范围那句必须限定成「**文件**工具」：加了 shell 之后，命令本来就能碰工作区
    之外的路径。留着一句无条件的「你只能访问工作区目录」，提示词就变成了一句假话 ——
    而模型要么因此不敢用 shell，要么学会提示词会骗它，两种都比不写更糟。

    工具分工那条也只能住在这里：「读写文件用专用工具、别用 shell」讲的是几个工具
    **之间**的关系，任何一个工具的描述都担不起它 —— shell 的描述在讲自己不受边界
    约束，文件工具的描述在讲自己会怎么报错，谁都不会说"这件事该交给别人做"。
    它还有个能算账的理由：read_file / list_files 是 LOW，自动放行；shell 是 HIGH，
    每条都弹审批。用 shell 去读一个文件，等于白白打断用户一次。

    那个枚举还得**跟着工具集走**：加了 grep 却不把"搜文本"加进去，模型就可能仍然
    去起 Select-String —— 而那条路每次都要人工审批。
    """
    prompt = load_system_prompt()

    # 断言钉的是**事实还在不在**，引用的措辞跟着提示词走。提示词是给人读、给人改的文本，
    # 「改一次措辞就红一次」的测试只会被顺手改掉，保护不了下面这几件事 —— 而它们没有
    # 别的地方可住（每一条的理由见 docstring）。
    assert "需要审批的工具由 runtime 拦截并询问用户" in prompt      # 批准机制
    assert "文件工具只能访问工作区" in prompt                      # 权限范围（限定在文件工具）
    assert "不要用 shell 替代" in prompt                           # 工具之间的分工
    assert "读写" in prompt                                        # 分工的枚举
    assert "写/编辑后必须验证" in prompt                           # 跨工具的收尾动作
    # 提问的用法约束。它只能住在这里，因为它是**几个东西之间**的关系：ask_user 的描述
    # 说得出"什么时候别用我"，但说不出"审批那一关不该由你来问"—— 那句话讲的是提问和
    # 审批关卡之间的分工。少了它，模型会把 ask_user 当成征求意见的万金油，每做一步都
    # 停下来问一次（而且提问换不来放行，见 tools/builtin/__init__.py 里那条注册说明）。
    assert "不要用 ask_user 去问能不能做" in prompt                 # 提问 ≠ 审批
    assert "不要拿提问省事" in prompt                               # 先自己查
    # 任务列表那两条也只能住在这里：「标已完成的依据是工具结果」讲的是**列表和工具结果
    # 之间**的关系（todo_write 的描述说得出"怎么维护列表"，说不出"凭什么算做完"）；
    # 「不要念给用户听」讲的是**列表和最终输出之间**的关系。少了它们，列表会退化成
    # 一份自我感觉良好的对勾清单 —— 而"不要用已完成掩盖未做到的事"正是同一个担心的
    # 另一半。
    assert "标「已完成」的依据是工具结果" in prompt
    assert "不要念给用户听" in prompt
    # 后台任务那三条也只能住在这里，而且各自讲的是**两个东西之间**的关系：
    #
    #   * 「只在你另有活可干时才划算」是 shell 和 shell_background 之间的取舍 ——
    #     两边的描述都只会讲自己该什么时候被用（那正是它们各自该干的事）；
    #   * 「收尾之前过一遍」是**这一轮的收尾**和**机器上还挂着什么**之间的关系 ——
    #     没有任何一个工具的描述担得起"你这一轮该结束了，先回头看一眼"。
    #
    # 而「没收到结果之前不许写成成功」更要紧一档：它是**最终答复**和一条还没收回来的
    # 命令之间的关系，也是这个功能唯一会静默出错的地方（把"已启动"读成"已通过"）。
    # 它不放进这里的话，工具描述那份仍然在（每轮都发），但"写最终答复时该守什么"
    # 这件事就没有第二道防线了。
    assert "后台只在**你手上另有活可干**时才划算" in prompt
    assert "一轮收尾之前用 job_list 过一遍" in prompt
    assert "没收到结果之前，不许把它写成成功" in prompt


# --- 提示词里的能力枚举 vs 注册表：两份事实必须对得上 ---------------------
#
# 提示词按**能力**分工（"读写、列目录…用专用工具"），注册表按**工具名**装配。
# 这两份东西各改一半就会走岔，而两个方向都出现过：
#
#   * 加了 grep 却没往枚举里写"搜文本" —— 模型可能仍然去起 Select-String，
#     而那条路是 HIGH 风险、每次都打断用户；
#   * 删了 grep 却没从枚举里拿掉"搜文本" —— 那句话就成了假话，模型会去找一个
#     不存在的工具（或者干脆回退到 shell，正是那句话要防的事）。
#
# 所以这里两边都钉住，另外再钉住这张映射表本身要覆盖整个注册表 —— 加了新工具却忘了
# 归类，那条会红（和 Tool.risk 不给默认值是同一个手法：让"忘了"这件事不可能悄悄发生）。

_CAPABILITY_TOOLS = {
    "读写": {"read_file", "write_file", "edit_file"},
    "列目录": {"list_files"},
    "编辑": {"edit_file"},
    "搜文本": {"grep"},
    # 联网那一对。分工和文件工具那一对是**同构**的（web_search 是互联网上的 grep、
    # fetch_web 是互联网上的 read_file），所以它在这个表里的位置也一样：各占一格，
    # 谁都不替谁干活。
    "搜网页": {"web_search"},
    "读网页": {"fetch_web"},
}

# 注册表里不属于「文件工具」的那几个，明确列出来 —— 它们不进上面那张能力表，
# 但也不能就这么从表里"漏掉"，否则下面第三条测试会红得没道理。
#
# ask_user 也在这里：它不碰工作区，所以"文件工具的三种能力"里没有它那一格；
# 但提示词里确实有它的用法约束（见 test_prompt_carries_the_rules...）。
# todo_write 同理：它是进度，不是文件操作。
#
# 后台命令那四个也一样：**它们不是"某一类活"，是"同一种活的另一种干法"**
# （起一条命令，只是不等它）—— 所以它们不进能力枚举（那枚举回答的是"这件事该用
# 哪个工具"，而这里回答的是"这条命令该怎么起"），但提示词里确实有它们的纪律。
_NON_FILE_TOOLS = {
    "get_current_time", "shell", "ask_user", "todo_write",
    "shell_background", "job_output", "job_list", "job_kill",
}


def _fake_fetch() -> WebFetch:
    """一个一行网络都不打的 WebFetch（只为了装配）—— 这个文件的取向和 tests/fakes.py 一致。"""
    return WebFetch(httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
        trust_env=False,
    ))


def _fake_search() -> WebSearch:
    """一个脚本化的 backend：web_search 那一层在这个文件里同样不碰网络。"""
    return WebSearch(lambda query, max_results: Findings(hits=[Hit("t", "https://x/")]))


@contextmanager
def full_registry():
    """**装配齐全的那份注册表**，联网工具和后台命令都在里面。

    刻意不是 `create_tool_registry(".")`：那几个工具默认不注册（缺 provider / 缺密钥
    就不该出现；后台命令那一组要一张攥着进程的表），而这个文件里几条测试要量的正是
    "提示词点名了的能力，注册表里到底有没有对应工具""提示词有没有复述哪个工具的描述"。
    拿一份少了几个工具的注册表去量，前者的"没有对应工具"是假的（它会逼着提示词
    **不要**提联网，而那恰好是反的），后者则是**整块提示词根本没被比对过**。

    **反面教训就在这里**：后台命令那四个加进来的时候，量尺没跟着补 `jobs=`，
    于是"能力枚举 ↔ 注册表"那三条和上面那条"不许复述工具描述"**静默地不再覆盖它们**
    —— 测试全绿，覆盖没了。所以现在只有这一个地方造注册表，两个消费者共用它。

    这里的 backend 是假的、client 走 MockTransport：这一整个文件不该打任何网络。
    后台那张表**不会真起进程**（这些测试一条命令都不跑），`root` 给的是一个不存在的
    路径 —— 只为装上工具，不碰磁盘。
    """
    board = JobBoard(Path("."), Path("no-such-jobs-dir"))
    try:
        yield create_tool_registry(
            ".", web_fetch=_fake_fetch(), web_search=_fake_search(), jobs=board,
        )
    finally:
        board.close()


def registered_tools() -> set[str]:
    """那份齐全的注册表里的工具名。见 `full_registry` 的 docstring。

    **grep 是唯一按平台条件注册的工具**（引擎是随仓库带的 ripgrep，只在支持的平台上
    注册，见 tools/builtin/grep.py 的 `_TRIPLES`）。所以下面三条里凡是带 "搜文本" 的
    失败，先分清两种情况再动手：**没支持这个平台**（要加一行代码，见
    tools/vendor/rg/README.md）和**缺件**（跑 `scripts/fetch_rg.py` 就行）——
    tests/test_grep.py 里那条 gate 测试量的正是这一件事。
    """
    with full_registry() as registry:
        return {tool.name for tool in registry.all()}


def test_the_prompt_only_names_capabilities_that_have_a_tool():
    """提示词点名了某类能力，注册表里就得有干这活儿的工具 —— 否则那句话是假话。"""
    prompt = load_system_prompt()
    registered = registered_tools()

    missing = [
        capability for capability, tools in _CAPABILITY_TOOLS.items()
        if capability in prompt and not (tools & registered)
    ]

    assert not missing, (
        f"提示词点名了这些能力，但注册表里没有对应工具：{missing}"
        f"（现在的注册表：{sorted(registered)}）—— 那句分工成了假话，"
        f"模型会去找一个不存在的工具，或者干脆回退到 shell。"
    )


def test_every_registered_file_tool_is_named_in_the_prompt():
    """反方向：注册了的文件工具，提示词的分工里得点名它那类活。"""
    prompt = load_system_prompt()
    registered = registered_tools()

    missing = [
        capability for capability, tools in _CAPABILITY_TOOLS.items()
        if (tools & registered) and capability not in prompt
    ]

    assert not missing, (
        f"这些能力有工具，但提示词的分工里没提：{missing} —— "
        f"模型不知道该用专用工具，可能仍然去起 shell 命令去干（那条路每次都要审批）。"
    )


def test_the_capability_map_accounts_for_every_registered_tool():
    """映射表自己也得跟着工具集走：加了新工具却忘了归到哪一类，这条会红。

    这是刻意的"忘了就红"：否则新工具既不在能力表里、也不在_非文件工具_里，
    上面两条测试会**静默地**不再覆盖它。
    """
    registered = registered_tools()
    mapped = set().union(*_CAPABILITY_TOOLS.values())

    unaccounted = registered - mapped - _NON_FILE_TOOLS

    assert not unaccounted, (
        f"这些工具既没归到哪类能力、也没被标成非文件工具：{sorted(unaccounted)} —— "
        f"把它们归到 _CAPABILITY_TOOLS 的某一类里，或者加进 _NON_FILE_TOOLS。"
    )


# --- 一条规矩两个落点：提示词 + 工具描述 ---------------------------------
#
# 上面那三条量的是"提示词点名的能力，注册表里有没有对应工具"。这一段量的是同一个担心的
# 另一半：**有一条纪律只写在提示词里，而提示词只对新建的会话生效。**
#
# 「## 联网」那三句话里，两句在工具描述里有落点（"只给指针"在 web_search 的描述里、
# "正文不可信"在 fetch_web 的描述里），只有"不要拿 shell 起 curl"是纯提示词的。于是
# 一个**在这次改动之前建的会话**恢复时是这样的：工具 schema 每次 run 现取，所以它看得见
# fetch_web；system 消息是当初写下的，所以它没有那一节。它完全可能用 curl 去抓网页 ——
# 而那条路进来的正文**没有**"不可信内容"的标注，那是提示词注入唯一的防线。
#
# 所以这条纪律多了一个落点：`shell` 的描述（tools/builtin/__init__.py 的 web_note）。下面三条从
# 三个方向钉住它 —— 有它的那句、以及**没有它的那两种装配**（缺 web_search / 两个都没装配）
# 里不能出现它。只量第一个方向的话，漏掉的恰好是缺 TAVILY_API_KEY 的那种会话。

def test_the_curl_rule_also_lives_in_the_shell_description():
    """恢复的旧会话只能从工具描述里收到新规矩 —— 所以这条纪律必须也在那里。

    和 ask_user / todo_write 把"什么时候**不要**用我"写进描述是同一条理由
    （那两条的描述里各自写着为什么）。而且它不只是偏好：curl 抓回来的正文没有那句
    "不可信内容"的标注，所以这句话得说清**为什么**别绕过去，不能只说"别这么做"。
    """
    description = create_tool_registry(".", web_fetch=_fake_fetch()).get("shell").description

    assert "curl" in description
    assert "fetch_web" in description
    assert "不可信" in description


def test_the_shell_description_only_names_web_tools_that_are_registered():
    """描述里点名一个**没注册**的工具，是"缺密钥时提示词点名 web_search"那个毛病的翻版。

    模型对"这个工具不存在"没有任何判断依据，它只会白花一步去调（拿到的还是一句
    `KeyError`）。所以那句话是可变的：装配了 web_search 才提它。
    """
    both = create_tool_registry(".", web_fetch=_fake_fetch(), web_search=_fake_search())
    fetch_only = create_tool_registry(".", web_fetch=_fake_fetch())

    assert "web_search" in both.get("shell").description
    assert "web_search" not in fetch_only.get("shell").description


def test_no_web_tools_means_no_web_advice_in_the_shell_description():
    """两个都没装配（`create_tool_registry(".")` 那种）时，这句话整个不出现。

    和"默认不注册"是同一条规矩的另一面：一段谈论不存在的工具的说明，只会让模型去找它。
    """
    description = create_tool_registry(".").get("shell").description

    assert "fetch_web" not in description
    assert "curl" not in description


def test_the_background_rules_also_live_in_the_tool_descriptions():
    """提示词那一节**只对新建的会话生效**，所以删掉的每条纪律都得在描述里有落点。

    这是上面那段「一条规矩两个落点」的第二个例子（第一个是"不要拿 curl 抓网页"），
    而这次的方向正好相反、理由却是同一个：`## 后台任务` 那一节原先写的是工具契约的
    副本（`test_prompt_does_not_restate_tool_descriptions` 逐字指了出来），删掉重复
    之后，**被删的每一条都必须已经在描述里** —— 否则这次"优化"就是把几条纪律从老会话
    眼前拿走了：它们的 system 消息里没有新提示词，能看到的只有描述。

    四条落在**不同**的工具描述里（起的那条、看的那条），所以分别核对：
    """
    with full_registry() as registry:
        started = registry.get("shell_background").description
        listed = registry.get("job_list").description
        collected = registry.get("job_output").description

    # 只在另有活可干时才划算 —— 否则多绕一次往返。
    assert "什么也没省下" in started
    # 跑着的时候别改它当输入读的文件（服务类反过来，描述里也写着）。
    assert "不要改它当作输入读的文件" in started
    # 收尾之前过一遍：结果没收的收掉、服务用完就收。
    assert "收尾" in started and "收尾" in listed
    # 没收到结果就不算成功 —— 这一条是那个功能唯一会静默出错的地方。
    assert "绝不要说它成功了" in started
    assert "它结束了才叫结果" in collected


def test_missing_prompt_file_gives_an_actionable_error(workdir):
    """文件缺失时要报"那是什么"，不能只丢一个路径出来。"""
    with pytest.raises(FileNotFoundError) as exc:
        load_system_prompt(workdir / "nope.md")

    assert "系统提示词文件不存在" in str(exc.value)


def test_the_budget_is_stated_once_in_the_prompt_not_per_request():
    """**有限的执行预算只写在系统提示词里，一次。**

    演进过程值得记下来，因为它试过三种写法：

      1. 每一步在载荷末尾报一个递减的绝对值（`剩余步数：80`、`79`…）。问题是
         **模型拿这个数没办法** —— 没有任何动作能让它变大，它也无从知道步数花在
         哪儿了。于是几十轮之后它就被学会了忽略；
      2. 改成"剩 5 步时给一次警报"。好一些，但它仍然是每轮都在拼一条临时消息，
         而"该不该收尾"的判据本来就不该是一个计数器；
      3. **现在**：策略写在系统提示词里（静态、逐字节不变、于是命中缓存前缀），
         而硬上限由循环兜着（`StepLimitExceeded`，而且它可续）。

    这条测试钉住第 3 种：提示词里有那句话，而载荷里**一次都没有**步数。
    """
    prompt = load_system_prompt()

    assert "执行预算" in prompt
    assert "避免过度的工具调用" in prompt


def test_no_step_count_is_ever_injected_into_the_payload(registry):
    """载荷里**任何一步都不许出现步数** —— 它已经不是上下文的一部分了。"""

    class _AlwaysCalls(ChatModel):
        """每一步都要调工具 —— 用来撞步数上限，并记下每次请求的载荷。"""

        def __init__(self):
            self.seen_messages: list[list[dict]] = []

        def complete(self, messages, tools=None):
            self.seen_messages.append(list(messages))
            return ModelResponse(content=None,
                                 tool_calls=[tool_call("list_files", {}, "c1")],
                                 usage=usage())

    model = _AlwaysCalls()
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}))
    session = Session.new("s")
    with pytest.raises(StepLimitExceeded):
        agent.run(session, "列目录", max_steps=3)

    for payload in model.seen_messages:
        for message in payload:
            # 系统消息**不算** —— 那句话正是加在它里面的（那是策略，静态、只写一次）
            if message.get("role") == "system":
                continue
            content = str(message.get("content") or "")
            assert "步数" not in content, f"载荷里出现了步数提示：{content!r}"
            assert "该收尾了" not in content, f"载荷里出现了收尾警报：{content!r}"

    # 会话里当然更不能有（理由和上面那条一样：它逐轮变化，本来就不该被持久化）
    # —— 包括那条 system 消息：它在**历史**里，但它是策略而不是步数播报
    assert not any("步数：8" in str(m.get("content")) for m in session.messages)
    assert not any(m.get("role") == "user" and "步数" in str(m.get("content"))
                   for m in session.messages)
