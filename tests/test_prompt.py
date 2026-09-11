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

    assert "需要审批的工具会被运行时拦下来问用户" in prompt       # 批准机制
    assert "文件工具只能访问工作区目录" in prompt                 # 权限范围（限定在文件工具）
    assert "不要用 shell 代替" in prompt                          # 工具之间的分工
    assert "搜文本用专用工具" in prompt                           # 分工的枚举里不能漏掉后加的那类操作
    assert "改完文件后" in prompt                                 # 跨工具的收尾动作


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
