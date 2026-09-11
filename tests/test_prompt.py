"""系统提示词与步数提示。

这一组测试守的是一条很容易被无声破坏的边界：**什么进会话文件，什么只进这一次请求。**

  - 系统提示词的静态部分来自 prompts/system.zh.md；
  - 动态部分（目前只有操作系统）在新建会话时拼在静态部分**后面**；
  - 步数提示每轮临时拼进载荷，从不落盘。

三者的归属一旦搞混，后果不是"结果不对"，而是会话文件被污染、或者缓存前缀被逐轮
打断 —— 而后者正是这个项目实测出来最贵的东西（未命中的输入比命中贵约 50 倍）。
"""

import platform

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state import session as session_module
from agent_runtime.state.session import SYSTEM_PROMPT_PATH, load_system_prompt
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.tool import RiskLevel

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
    """
    prompt = load_system_prompt()
    violations = [
        f"  {tool.name}: {phrase!r}"
        for tool in create_tool_registry(".").all()
        for phrase in overlapping_phrases(prompt, tool.description)
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
    # 「改一次措辞就红一次」的测试只会被顺手改掉，保护不了下面这五件事 —— 而它们没有
    # 别的地方可住（每一条的理由见 docstring）。
    assert "需要审批的工具由 runtime 拦截并询问用户" in prompt      # 批准机制
    assert "文件工具只能访问工作区" in prompt                      # 权限范围（限定在文件工具）
    assert "不要用 shell 替代" in prompt                           # 工具之间的分工
    assert "读写" in prompt                                        # 分工的枚举
    assert "写/编辑后必须验证" in prompt                           # 跨工具的收尾动作
    # 提问的用法约束。它只能住在这里，因为它是**几个东西之间**的关系：ask_user 的描述
    # 说得出"什么时候别用我"，但说不出"审批那一关不该由你来问"—— 那句话讲的是提问和
    # 审批关卡之间的分工。少了它，模型会把 ask_user 当成征求意见的万金油，每做一步都
    # 停下来问一次（而且提问换不来放行，见 tools/builtin.py 里那条注册说明）。
    assert "不要用 ask_user 去问能不能做" in prompt                 # 提问 ≠ 审批
    assert "不要拿提问省事" in prompt                               # 先自己查
    # 任务列表那两条也只能住在这里：「标已完成的依据是工具结果」讲的是**列表和工具结果
    # 之间**的关系（todo_write 的描述说得出"怎么维护列表"，说不出"凭什么算做完"）；
    # 「不要念给用户听」讲的是**列表和最终输出之间**的关系。少了它们，列表会退化成
    # 一份自我感觉良好的对勾清单 —— 而"不要用已完成掩盖未做到的事"正是同一个担心的
    # 另一半。
    assert "标「已完成」的依据是工具结果" in prompt
    assert "不要念给用户听" in prompt


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
}

# 注册表里不属于「文件工具」的那几个，明确列出来 —— 它们不进上面那张能力表，
# 但也不能就这么从表里"漏掉"，否则下面第三条测试会红得没道理。
#
# ask_user 也在这里：它不碰工作区，所以"文件工具的三种能力"里没有它那一格；
# 但提示词里确实有它的用法约束（见 test_prompt_carries_the_rules...）。
# todo_write 同理：它是进度，不是文件操作。
_NON_FILE_TOOLS = {"get_current_time", "shell", "ask_user", "todo_write"}


def registered_tools() -> set[str]:
    return {tool.name for tool in create_tool_registry(".").all()}


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


def test_missing_prompt_file_gives_an_actionable_error(workdir):
    """文件缺失时要报"那是什么"，不能只丢一个路径出来。"""
    with pytest.raises(FileNotFoundError) as exc:
        load_system_prompt(workdir / "nope.md")

    assert "系统提示词文件不存在" in str(exc.value)


def test_budget_reminder_counts_the_current_step_in():
    """剩余步数是**含本次**的 —— 差一会让模型提前收工。"""
    assert Agent._budget_reminder(20, 0)["content"] == "剩余步数：20（含本次）"
    assert Agent._budget_reminder(20, 19)["content"] == "剩余步数：1（含本次）"


def test_reminder_reaches_the_model_but_never_the_session(registry):
    """步数提示逐轮变化，所以它只该活在请求里。"""

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False)
    session = Session.new("s")
    agent.run(session, "列目录", max_steps=5)

    # 模型每轮都看到了，而且数字在减少
    sent = [
        message["content"]
        for payload in model.seen_messages
        for message in payload
        if message["role"] == "user" and str(message["content"]).startswith("剩余步数")
    ]
    assert sent == ["剩余步数：5（含本次）", "剩余步数：4（含本次）"]

    # 会话里一个字都没有 —— 否则文件变胖、--history 全是碎话，
    # 而且「一条 assistant = 一步」这个派生规则会被 user 消息稀释
    assert not any("剩余步数" in str(m.get("content")) for m in session.messages)
