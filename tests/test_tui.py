"""TUI 前端。

两层分开测，因为它们能测到的程度差得很远：

  1. **`view_state.py` 的纯函数** —— 事件怎么变成给人看的行。这是这个界面里最值得
     测的部分（全是判断，没有布局），而且它**不需要 Textual**；
  2. **`TuiApp` 的骨架** —— 用 Textual 自己的测试台（`run_test`）：能挂载、`init`
     能画出来、斜杠命令有反应。**布局好不好看测不了**，所以不去测它。
"""

import asyncio

import pytest

# `run_test()` 是 async 的，而项目里**不装 pytest-asyncio** —— anyio 的 pytest 插件
# 已经够用（它是 textual 的传递依赖，不额外增加负担）。anyio 默认会跑所有后端
# （包括 trio，而 trio 没装），所以钉死成 asyncio。
@pytest.fixture
def anyio_backend():
    return "asyncio"

from agent_runtime.frontends.tui import view_state
from agent_runtime.protocol import state as agent_state


# --- 第一层：纯函数 -----------------------------------------------------------

def test_clip_reports_whether_it_truncated():
    """它只回答"截没截"，不加省略号 —— 加什么是调用方的排版决定。"""
    assert view_state.clip("abc", 10) == ("abc", False)
    text, cut = view_state.clip("a" * 10, 4)
    assert text == "aaaa" and cut is True
    # limit <= 0 表示"不截"
    assert view_state.clip("a" * 100, 0) == ("a" * 100, False)


def test_indent_keeps_every_line_inside_the_box():
    assert view_state.indent("a\nb", "| ") == "| a\n| b"


def test_run_started_shows_what_the_user_said():
    state = view_state.ViewState()
    lines = view_state.render_event(state, {"kind": "run_started", "user_input": "你好"})
    assert any("你好" in line for line in lines)


def test_a_denied_tool_says_it_did_not_run():
    """**拒绝和出错必须分开说。**

    两者在 `chars` 上看不出来（都是 0 附近），而"没执行"和"执行了但坏了"是
    完全不同的事 —— 用户据此决定要不要换个做法。
    """
    state = view_state.ViewState()
    lines = view_state.render_event(state, {
        "kind": "tool_result", "tool": "shell", "status": "denied",
        "chars": 0, "duration_ms": 1,
    })
    assert any("没有执行" in line for line in lines)


def test_step_limit_looks_different_from_answered():
    """**这是 `StepLimitExceeded` 那个类存在的全部理由。**

    不许让人分不清"答完了"和"被砍断了" —— 所以两边的行数/措辞都必须不同。
    """
    state = view_state.ViewState()
    answered = view_state.render_event(state, {"kind": "run_finished",
                                               "stop_reason": "answered"})
    limited = view_state.render_event(state, {"kind": "run_finished",
                                              "stop_reason": "max_steps"})
    assert answered and limited
    assert len(limited) > len(answered)
    assert any("没有" in line and "收尾" in line for line in limited)
    assert not any("收尾" in line for line in answered)


def test_thinking_is_collapsed_by_default_and_the_count_is_ours():
    """决策 17：思维链默认折叠成一行。

    字符数**由前端 `len()` 出来** —— 不让子进程多发一个计数字段（那是同一份事实的
    第二个来源，两侧的口径未必一致）。
    """
    state = view_state.ViewState()
    message = {"kind": "model_call", "status": "ok", "run_id": "r1",
               "reasoning": "想" * 300, "duration_ms": 5}
    lines = view_state.render_event(state, message)

    assert any("思考过程（300 字符" in line for line in lines)
    assert not any("想" * 10 in line for line in lines), "默认不许铺开"
    assert state.thinking["r1"] == ("想" * 300, False)


def test_toggling_shows_the_whole_thinking_text():
    """展开之后**一字不差、不截断** —— 它别处看不到（审计里有，但界面要能读）。"""
    state = view_state.ViewState()
    message = {"kind": "model_call", "status": "ok", "run_id": "r1",
               "reasoning": "想" * 300, "duration_ms": 5}
    view_state.render_event(state, message)
    state.toggle_thinking("r1")

    lines = view_state.render_event(state, {**message, "reasoning": "想" * 300})
    assert any("想" * 300 in line for line in lines)


def test_the_answer_is_recorded_by_run_id_not_by_arrival_order():
    """`event(run_finished)` 和 `ui(run_finished)` 是**两条**消息，顺序不保证。

    所以配对只能靠 `run_id`。靠到达顺序的话，一旦两条消息的次序变了（这在
    工作线程 + 主循环之间是可能的），界面会把答案贴到错误的一轮上 ——
    而那看起来完全正常。
    """
    state = view_state.ViewState()
    lines = view_state.render_ui_answer(state, {"run_id": "r9", "answer": "答案"})
    assert any("答案" in line for line in lines)
    assert state.answers["r9"] == "答案"

    # 第二次同 run_id（重放）覆盖，而不是追加出第二条。
    view_state.render_ui_answer(state, {"run_id": "r9", "answer": "答案2"})
    assert state.answers["r9"] == "答案2"


def test_an_empty_answer_draws_nothing():
    """模型失败时 `answer` 是空串 —— 那时**不该**画一个空的 `[agent]` 气泡。"""
    state = view_state.ViewState()
    assert view_state.render_ui_answer(state, {"run_id": "r", "answer": ""}) == []


def test_status_line_is_a_projection_not_a_second_truth():
    """状态栏那一行完全由 `agent.state` + 会话规模推出来。

    这里只钉一件事：**`activity` 说的是"最近发生了什么"**，不是界面自己编的词。
    没有流式，"模型在想"和"工具在跑"分不出更细的粒度 —— 硬分只能用时间间隔去猜。
    """
    state = view_state.ViewState(session_id="s1", model="m", max_steps=80)
    assert "空闲" in state.status_line()

    state.agent = agent_state.reduce(
        agent_state.initial(), {"t": "event", "kind": "run_started", "step": 0}
    )
    assert "准备中" in state.status_line()

    # **`step` 只在 `run_started` 上更新**，后面的事件带的是同一个 step。
    # 而 step 的语义是"第几次模型往返"，`run_started` 那条是 **0** ——
    # 所以真实的第 2 步长这样（实测踩过：我第一版在这里传了 step=1，
    # 于是断言写成 `第 2/80 步` 而实际是 `第 1/80 步`）。
    state.agent = agent_state.reduce(state.agent, {
        "t": "event", "kind": "run_started", "step": 0,
    })
    state.agent = agent_state.reduce(state.agent, {
        "t": "event", "kind": "tool_call", "step": 1, "tool": "read_file",
        "tool_index": 1,
    })
    line = state.status_line()
    assert "read_file" in line and "第 2 个" in line
    assert "第 1/80 步" in line
    assert "s1" in line and "m" in line


def test_status_line_shows_permissions_only_when_runtime_sent_them():
    """决策 14：**runtime 发什么显示什么**，界面不硬编码默认值。

    默认时 `permissions` 是空 dict，于是那一行什么都不加。
    """
    state = view_state.ViewState(session_id="s")
    assert "权限：" not in state.status_line()

    state.permissions = {"auto_approve_tools": ["shell"]}
    assert "权限：" in state.status_line()
    assert "shell" in state.status_line()


# --- 第二层：Textual 应用骨架 -------------------------------------------------

class FakeClient:
    """替掉真的 `ProtocolClient`：**不起子进程**。

    为什么必须替掉而不是"起了再说"：这层要测的是**界面**，而起一个真子进程会让
    每条测试慢几百毫秒、还依赖环境变量和设备上的 `.tudouni`。**而且第一版我是
    在 `run_test()` 之前给 `app._client` 赋值的 —— 那没用**：`run_test()` 会跑
    完整的生命周期（`on_mount` 在里面），真 client 会把假的覆盖掉，
    于是断言恒为空（实测踩过：`_client is fake` 打出来是 False）。
    所以替的是类，不是属性。
    """

    exit_code = 0

    def __init__(self, hooks, *, session=None, autopilot=False, stderr_to=None):
        self.hooks = hooks
        self.session = session
        self.sent: list[dict] = []
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def user_message(self, text: str) -> None:
        self.sent.append({"t": "user_message", "text": text})

    def answer_permission(self, request_id: str, decision: str) -> None:
        self.sent.append({"t": "permission_response", "id": request_id,
                          "decision": decision})

    def answer_question(self, request_id: str, status: str, text: str) -> None:
        self.sent.append({"t": "question_response", "id": request_id,
                          "status": status, "text": text})

    def close(self) -> None:
        self.closed = True

    def wait(self) -> int:
        return 0


def _build_app(monkeypatch):
    """一个 App 实例，**协议客户端已被替成 `FakeClient`**。"""
    from agent_runtime.frontends.tui import app as app_module

    monkeypatch.setattr(app_module, "ProtocolClient", FakeClient)
    return app_module.TuiApp(session="tui-test")


async def _settle(app, pilot, rounds: int = 4) -> None:
    """等消息泵把队列排空、并且界面处理完。

    ## 为什么不能只 `await pilot.pause()`

    泵是一个 **50ms 的定时器**（`set_interval`），而 `pause()` 只让出一轮事件循环
    —— 它**不保证**等过一个定时器周期。症状是**偶发红**：同一个测试这次过、下次
    挂在断言上，而重跑一次又好了（实测：两条面板测试交替红，打印一行调试输出就
    变成了绿的 —— 典型的时序依赖）。

    所以这里主动把泵推一下（`app._pump()` 可以直接调），再让出事件循环去处理它
    引发的屏幕切换（`push_screen` 是排进消息队列的，必须等）。
    """
    for _ in range(rounds):
        app._pump()
        await pilot.pause()


@pytest.mark.anyio
async def test_the_app_mounts_and_draws_an_init(monkeypatch):
    """`init` 到了之后，界面该有的东西都在。

    这条用 Textual 自己的测试台跑一个**真的** App 实例（真挂载、真布局、真渲染），
    但**不连子进程** —— 直接把 `init` 塞进它的消息队列。这样测到的是"界面能不能
    把一条协议消息画出来"，而不是"子进程能不能起来"（那是 `test_protocol.py` 的事）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "protocol": 1,
            "session_id": "tui-test", "resumed": False,
            "model": "fake", "workspace": "C:/w", "max_steps": 80,
            "tools": [{"name": "read_file", "risk": "low",
                       "parallel_safe": True, "interactive": False}],
            "permissions": {}, "audit_path": "C:/w/.tudouni/logs/tui-test.jsonl",
            "notices": [{"level": "info", "code": "skills", "text": "可用 1 个"}],
        }))
        await _settle(app, pilot)

        # 状态是"界面的第二份事实"里最要紧的那几个字段 —— 它们直接决定状态栏。
        assert app.state.session_id == "tui-test"
        assert app.state.model == "fake"
        assert app.state.max_steps == 80
        assert app.state.audit_path.endswith(".jsonl")
        assert app.state.tool_risks == {"read_file": "low"}

        # 顶栏确实被更新了。**`Static` 上没有 `.renderable`**（Textual 8 实测），
        # 要拿它现在的文本得走 `render()`。
        header = app.query_one("#header")
        assert "tui-test" in str(header.render())


@pytest.mark.anyio
async def test_a_permission_request_opens_the_panel_with_exactly_the_buttons(monkeypatch):
    """**审批面板的按钮集合必须跟着后端给的字段走。**

    三种情况（决策 16 的推论）：
      * 有 `remember_hint` → 才有 [总是允许]；
      * 有 `allow_trust_all` → 才有 [都允许]；
      * 都没有 → 只有 [允许] [拒绝]。

    **不许自己补一个"总是允许整个 shell"** —— 那正是 runtime 刻意堵掉的东西。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p1", "call_id": "c1",
            "tool": "shell", "risk": "high", "arguments": {"command": "rm -rf x"},
            # **两条都为空**：这次既不提供 t 也不提供 a。
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"allow", "deny"}, f"不该出现别的按钮，实际 {ids}"


@pytest.mark.anyio
async def test_a_trust_all_request_shows_the_fourth_button(monkeypatch):
    """有 `allow_trust_all` 时才出现 [都允许] —— 而且它的说明原样显示。"""
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        hint = "以后 MCP server github 的 12 个工具都直接执行（快照：以后新加的仍然会问）"
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p2", "call_id": "c2",
            "tool": "mcp__github__x", "risk": "high", "arguments": {},
            "remember": {"tool": "mcp__github__x"}, "remember_hint": "以后别再问",
            "allow_trust_all": True, "trust_all_hint": hint,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"allow", "deny", "always", "always_group"}

        # 那句说明**原样**在界面上（一个字都没改）。
        # `Static` 上没有 `.renderable`（Textual 8 实测），文本走 `render()`。
        rendered = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert hint in rendered


@pytest.mark.anyio
async def test_the_ui_answers_permission_itself_instead_of_a_fallback(monkeypatch):
    """`on_permission` **必须返回 `None`**（"界面稍后自己回"），不许给兜底答案。

    这条测试是**为一个真实的 bug** 写的，而它的症状很误导：审批面板正常弹出、
    按钮也点了，但工具结果是"用户拒绝"。

    原因是第一版让 `on_permission` 返回一个兜底 `DENY`，想的是"稍后用真答案覆盖"。
    而客户端**立刻**就把那个 DENY 发出去了 —— 子进程据此拒绝并继续往下跑；等用户
    点 [允许] 时那条回应已经没人要（更糟：中间那次拒绝进了审计，记成
    `user_denied`，也就是**伪造了一条"用户拒绝过"的记录**）。

    所以这里钉的是那个返回值本身。它看起来像个细节，但它是"谁有权回答"这件事的
    全部 —— 协议层那边是配套的另一半（`ClientHooks.on_permission` 的 docstring、
    `client._read_loop` 里那个 `if decision is not None`）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test():
        request = {"t": "permission_request", "id": "p", "tool": "shell"}
        assert app.on_permission(request) is None, \
            "不许给兜底答案 —— 它会抢在用户前面发出去"
        assert app.on_question({"t": "question_request", "id": "q"}) is None

        # 而且请求真的进了队列（不是把它丢掉了）。
        assert app._inbox.qsize() == 2


@pytest.mark.anyio
async def test_clicking_allow_sends_allow_not_deny(monkeypatch):
    """点 [允许] 之后，发出去的是 `allow`。

    和上一条配套：一个保证"不由界面之外的人回答"，这一个保证"界面回答的是对的那个"。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p9", "call_id": "c9",
            "tool": "shell", "risk": "high", "arguments": {"command": "echo hi"},
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        app.screen.query_one("#allow", Button).press()
        await _settle(app, pilot)

        assert {"t": "permission_response", "id": "p9",
                "decision": "allow"} in app._client.sent


@pytest.mark.anyio
async def test_slash_commands_do_not_reach_the_runtime(monkeypatch):
    """`/` 命令由界面处理，**不进 runtime**。

    这条挡的是一个很容易犯的错：把 `/help` 当成一句话发给模型 —— 于是它花钱去回答
    一个本地就能答的问题，而且用户看不出区别。

    它调的是 `app.submit(...)` 而不是伪造一条 `Input.Submitted`：后者测的是
    Textual 的消息路由（那是它的事），而这里要测的是我们的判断。
    """
    app = _build_app(monkeypatch)

    async with app.run_test():
        client = app._client
        app.submit("/help")
        app.submit("   ")
        assert client.sent == [], "斜杠命令和空行都不该发给 runtime"

        app.submit("你好")
        assert client.sent == [{"t": "user_message", "text": "你好"}]

        app.submit("  /exit  ")
        assert len(client.sent) == 1, "带空格的命令也要认得出来"
