"""安静模式（`--quiet` / `/quiet`）：**一次工具调用一行、思考只留一行。**

这个开关和 `/autopilot` 长得像，但它改的是**这个界面怎么画**，不是 runtime 的行为
（理由写在 `view_state.ViewState.quiet` 里）。所以这一份测试盯的是三件事：

  1. **一行**：调用、结果、静默那几种放行各占几行 —— 以及结果**回到调用那一行**上
     （`anchor` + `merge_anchored`），而不是另起一行；
  2. **不丢事实**：被拒绝、被策略禁止、等人按键那几条照旧显示；被截断的参数载荷
     也要挑得出"它对谁做了什么"；
  3. **动效**：思考那一行在跑的时候转圈 + 字符数往上走，收尾时定格（`Ctrl+T` 照旧
     能展开）。

第三条是"没有卡死"的证据，所以它是**逻辑**断言（转圈在不在、字符数对不对），
不是观感断言 —— 这一版的颜色和间距交给用户看（项目规范，见 AGENT.md）。
"""

import pytest

from agent_runtime.frontends.tui import view_state, widgets
from test_tui import (FakeClient, _build_app, _delta, _events, _init_message,
                      _log_text, _settle)


# `run_test()` 是 async 的，而 anyio 默认会跑所有后端（含没装的 trio）—— 和
# `test_tui.py` 一样钉死成 asyncio。放在这个文件里是因为 fixture 不跨文件继承。
@pytest.fixture
def anyio_backend():
    return "asyncio"


def _build_quiet_app(monkeypatch, **kwargs):
    """一个 `--quiet` 起来的 App（协议客户端同样被替成 `FakeClient`）。"""
    from agent_runtime.frontends.tui import app as app_module

    monkeypatch.setattr(app_module, "ProtocolClient", FakeClient)
    return app_module.TuiApp(session="tui-test", quiet=True, **kwargs)


def _call(tool: str, arguments: str, *, call_id: str = "c1", index: int = 0) -> dict:
    return {"kind": "tool_call", "run_id": "r1", "step": 1, "tool": tool,
            "tool_index": index, "call_id": call_id, "arguments": arguments}


def _result(tool: str, *, call_id: str = "c1", index: int = 0, status: str = "ok",
            chars: int = 16, duration_ms: int = 1) -> dict:
    return {"kind": "tool_result", "run_id": "r1", "step": 1, "tool": tool,
            "tool_index": index, "call_id": call_id, "status": status,
            "chars": chars, "duration_ms": duration_ms}


def _thinking_line(app) -> str:
    """屏幕上那一行思考过程（安静模式下它只有一行）。"""
    lines = [line for line in _log_text(app).splitlines() if "思考过程" in line]
    assert len(lines) == 1, f"安静模式下思考过程只该占一行：{lines}"
    return lines[0]


# --- 第一层：渲染（纯函数）------------------------------------------------------

def test_a_quiet_tool_call_is_one_line_and_the_result_lands_on_it():
    """`→ [write_file] hello.c`，结果回来时接在**同一行**上。

    这条钉的是安静模式的全部价值：原来的一次调用占三行（调用 / 权限 / 结果），
    现在一行。而"结果接上去"不是排版偏好 —— 如果结果另起一行，用户看到的还是两行，
    而且对不上是"哪一次调用"的结果。
    """
    state = view_state.ViewState(quiet=True)
    state.tool_risks = {"write_file": "medium", "read_file": "low"}
    arguments = '{"path": "hello.c", "content": "#include <stdio.h>\\nint main(void) {}"}'

    call = view_state.render_event(state, _call("write_file", arguments))
    assert len(call) == 1, "一次调用只占一行"
    line = call[0]
    assert str(line) == "  → [write_file] hello.c", str(line)
    assert line.role == view_state.ROLE_TOOL_BRIEF
    # **身份**：结果回来时靠它找回这一行（`call_id`，不是顺序 —— 见决策 4）。
    assert line.anchor == "c1"
    # 风险**靠颜色不靠文字**（和 `_risk_suffix` 同一条取向）。
    assert "风险" not in str(line)
    assert view_state.ROLE_RISK_MEDIUM in {role for _text, role in line.segments}
    # 整段参数不再贴出来。
    assert "content" not in str(line) and "#include" not in str(line)

    done = view_state.render_event(state, _result("write_file"))[0]
    assert done.role == view_state.ROLE_TOOL_BRIEF_DONE
    assert done.anchor == "c1"
    assert view_state.same_anchored_line(done, line) is True

    merged = view_state.merge_anchored(line, done)
    assert str(merged) == "  → [write_file] hello.c   ✓ 16 字符   1ms", str(merged)
    assert merged.role == view_state.ROLE_TOOL_BRIEF_DONE


def test_two_identical_calls_are_kept_apart_by_their_call_id():
    """同一批里两条一模一样的调用**不许互相顶掉**。

    这是"按身份回填"真正的理由：`read_file a.py` 连着两次在屏幕上长得一模一样，
    而结果只能接回它自己那一次上。判据是 `call_id`。
    """
    state = view_state.ViewState(quiet=True)
    first = view_state.render_event(
        state, _call("read_file", '{"path": "a.py"}', call_id="c1"))[0]
    second = view_state.render_event(
        state, _call("read_file", '{"path": "a.py"}', call_id="c2", index=1))[0]

    done = view_state.render_event(
        state, _result("read_file", call_id="c2", index=1, chars=312))[0]
    assert view_state.same_anchored_line(done, first) is False, "那是另一次调用"
    assert view_state.same_anchored_line(done, second) is True


def test_a_replayed_result_does_not_get_appended_twice():
    """同一条 `tool_result` 重放时**不许接第二遍**（协议两端会分别升级，重放不是不可能）。

    方向是单向的：`…_DONE` 只顶"还没结果的调用行"，而屏幕上那一行已经不是了。
    少了这一条，接两遍的症状是 `✓ 8 字符 1.2s   ✓ 8 字符 1.2s` —— 看起来像工具跑了两次。
    """
    state = view_state.ViewState(quiet=True)
    line = view_state.render_event(state, _call("shell", '{"command": "echo hi"}'))[0]
    done = view_state.render_event(state, _result("shell", chars=8))[0]
    filled = view_state.merge_anchored(line, done)

    assert view_state.same_anchored_line(done, filled) is False, "它已经填过了"


@pytest.mark.parametrize(("tool", "arguments", "expected"), [
    ("read_file", '{"path": "src/a.py"}', "src/a.py"),
    ("shell", '{"command": "python -m pytest -q", "timeout_seconds": 120}',
     "python -m pytest -q"),
    ("grep", '{"pattern": "def foo", "path": "src", "include": "*.py"}', "def foo"),
    ("fetch_web", '{"url": "https://example.com/a"}', "https://example.com/a"),
    ("web_search", '{"query": "claude code tui"}', "claude code tui"),
    ("ask_user", '{"question": "要删哪一个？", "options": ["a", "b"]}', "要删哪一个？"),
    ("todo_write", '{"todos": [{"content": "甲"}, {"content": "乙"}]}', "2 条任务"),
])
def test_the_brief_line_picks_the_primary_argument(tool, arguments, expected):
    """每一行要说的是"它对谁做了什么"，而那是**工具自己**的参数语义。

    表在 `view_state._BRIEF_KEYS` 里（前端点名的），所以这里逐个钉住 —— 表旧掉的
    症状是"这一行显示了一个没用的参数"，而它看起来完全正常。
    """
    assert view_state.tool_brief(tool, arguments) == expected


def test_a_truncated_payload_still_names_its_target():
    """**被截断的载荷**（`write_file` 的 content 动辄几千字符）也要挑得出 path。

    事件里的 `arguments` 是审计预览（200 字符 + `…(共 N 字符)`），于是它**不是合法
    JSON** —— 直接 `json.loads` 会失败，而"绝大多数写文件的调用"都走这一条。
    捞不到时就原样贴（宁可难看，也不要一行空的）。
    """
    cut = '{"path": "hello.c", "content": "' + "x" * 200 + '…(共 9021 字符)'
    assert view_state.tool_brief("write_file", cut) == "hello.c"

    # 表里没有的工具（MCP 那些）：按偏爱顺序找。
    assert view_state.tool_brief("mcp__kb__search", '{"query": "协议" }') == "协议"

    # 完全认不出来 → 原样（截短 + 压平），**不抛**。
    junk = "not json at all " + "y" * 200
    brief = view_state.tool_brief("mcp__x__y", junk)
    assert brief.startswith("not json at all") and brief.endswith("…")
    assert len(brief) == view_state.BRIEF_LIMIT + 1
    # 空载荷 → 空串（那一行就只剩 `[工具名]`，不是 `[工具名] ` 带个尾巴）。
    assert view_state.tool_brief("job_list", "") == ""


def test_quiet_silences_automatic_allows_but_never_a_denial_or_a_wait():
    """四种"没有人参与"的放行不画，另外四种照旧。

    静默那四种（等级自动 / autopilot / 按过 t / 命中命令规则）在安静模式下是纯噪声；
    而**被拒绝、被策略禁止、要问你**必须留着 —— 它们是"这一轮为什么停在这儿"唯一的
    出口（审批面板关掉之后，回翻记录只剩这一行）。
    """
    state = view_state.ViewState(quiet=True)
    for outcome in ("auto_allowed", "autopilot", "rule_allowed", "command_allowed"):
        assert view_state.render_event(state, {
            "kind": "permission", "tool": "shell", "outcome": outcome,
            "rule": ["git", "add"] if outcome == "command_allowed" else None,
        }) == [], outcome

    for outcome in ("approved", "user_denied", "policy_denied", "no_asker"):
        lines = view_state.render_event(state, {
            "kind": "permission", "tool": "shell", "outcome": outcome})
        assert len(lines) == 1, outcome
        assert "权限" in str(lines[0]), (outcome, str(lines[0]))


def test_the_verbose_path_is_untouched():
    """**开关关着时，一个字都不变**（它就是"当前的现状"）。

    这条是这一版最要紧的回归断言：安静模式那条路加了新 role、新 anchor，而老那条
    必须照旧是"一次调用三行"。
    """
    state = view_state.ViewState()
    assert state.quiet is False, "默认关（`--quiet` 才开）"

    call = view_state.render_event(state, _call("read_file", '{"path": "a.py"}'))
    assert str(call[0]) == '  → [1] read_file({"path": "a.py"})'
    assert call[0].anchor == "", "老模式那些行不参与回填"

    denied = view_state.render_event(state, _result("read_file", status="denied", chars=0))
    assert len(denied) == 2, "老模式：结果一行，外加一句「没有执行」"
    assert str(denied[0]).startswith("  ← [1] ✗")
    assert "没有执行" in str(denied[1])


def test_quiet_survives_a_session_switch():
    """`/new` / `/resume` 之后它还在（它是这个界面的偏好，不是某个会话的属性）。"""
    state = view_state.ViewState(quiet=True)
    state.reset_for_session()
    assert state.quiet is True


def test_the_spinner_only_moves_while_the_agent_is_working():
    """那个记号**只在回合真的在跑时**换成转圈 —— 空闲/收尾时它一动都不动。

    注意协议里**没有**"正在等你按键"这个 phase（`protocol/state.py` 那张表只认事件，
    而 `permission_request` 不是事件）：等人在屏幕上的样子是**审批面板压在最上面**，
    所以"等人时不转"这件事由 App 判（`_waiting_for_human`），不在这里。
    """
    from agent_runtime.protocol import state as agent_state

    state = view_state.ViewState(quiet=True)
    idle = state.status_left(spin="⠋")
    assert idle.startswith("○"), idle

    state.agent = agent_state.reduce(agent_state.initial(), {
        "t": "event", "kind": "run_started", "step": 0})
    assert state.status_left(spin="⠋").startswith("⠋")
    assert state.status_left().startswith("●"), "不传转圈时还是那个静态记号"

    # 收尾之后它停下（`run_finished` 是唯一改 phase 的那条）。
    state.agent = agent_state.reduce(state.agent, {
        "t": "event", "kind": "run_finished", "stop_reason": "answered"})
    assert state.status_left(spin="⠋").startswith("✓")


def test_the_spinner_frame_is_a_function_of_the_clock():
    """帧号只由时刻算出来 —— 两处同时画转圈时才会一致（见 `spinner_frame`）。"""
    assert view_state.spinner_frame(0.0) == view_state.SPINNER_FRAMES[0]
    assert view_state.spinner_frame(0.0) == view_state.spinner_frame(0.0)
    assert view_state.spinner_frame(0.09) == view_state.SPINNER_FRAMES[1]
    # 转满一圈回到第一帧。
    cycle = view_state.SPINNER_SECONDS * len(view_state.SPINNER_FRAMES)
    assert view_state.spinner_frame(cycle) == view_state.SPINNER_FRAMES[0]


def test_the_quiet_badge_is_absent_when_it_is_off():
    """「安静」那一格**只在开着时占位**（和 autopilot 那一枚的取舍不同，有理由）。"""
    assert view_state.ViewState().quiet_badge() is None
    badge = view_state.ViewState(quiet=True).quiet_badge()
    assert badge is not None and str(badge) == "安静"


# --- 第二层：界面（真挂载）------------------------------------------------------

@pytest.mark.anyio
async def test_slash_quiet_toggles_locally_and_the_result_lands_on_the_call_line(monkeypatch):
    """`/quiet` 当场生效、**一条协议消息都不发**，之后的结果回填到调用那一行上。

    "不发协议消息"是这条的要点：安静模式改的只是这个界面怎么画，而 `/autopilot`
    必须等 runtime 回话（那一格决定工具要不要问人）。两者混在一个形状里，就会有人
    给这一格也加上"等确认"——那时候按一下 `/quiet` 要等半拍才有反应，而它根本
    没有第二份事实可等。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", resumed=True)))
        await _settle(app, pilot)
        assert app.state.quiet is False
        sent = list(app._client.sent)

        app.submit("/quiet")
        await _settle(app, pilot)
        assert app.state.quiet is True
        assert "安静模式" in _log_text(app)
        assert app._client.sent == sent, "本地开关不该发任何协议消息"

        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "写个文件"})
        _events(app, _call("write_file",
                           '{"path": "hello.c", "content": "int main(void) {}"}'))
        await _settle(app, pilot)
        text = _log_text(app)
        assert "[write_file] hello.c" in text
        assert "int main" not in text, "安静模式不贴整段参数"

        _events(app, _result("write_file"))
        await _settle(app, pilot)
        call_lines = [line for line in _log_text(app).splitlines()
                      if "[write_file]" in line]
        assert len(call_lines) == 1, f"结果该回填到那一行上，而不是另起一行：{call_lines}"
        assert "✓ 16 字符" in call_lines[0], call_lines[0]

        # 再切回去：**之后**画的东西恢复逐条显示（已经画出来的不动，回声里说了）。
        app.submit("/quiet off")
        await _settle(app, pilot)
        assert app.state.quiet is False
        assert "安静模式" in _log_text(app)

        # 认不出来的写法不猜，也不改状态。
        app.submit("/quiet 也许")
        await _settle(app, pilot)
        assert app.state.quiet is False
        assert "认不出这个写法" in _log_text(app)


@pytest.mark.anyio
async def test_the_thinking_line_spins_while_it_grows_then_settles(monkeypatch):
    """安静模式下思考只占一行：**边想边转圈、字符数往上走**，收尾时定格。

    "转圈"和"字符数"是这一条要的两样证据（`doc/TUI-design.md` 第八节末尾那条：
    一次模型往返是秒级，而完全静止的界面会被当成卡死）。定格之后再按 `Ctrl+T`
    仍然能展开 —— 那一行和 `toggle_thinking` 认的是同一行（role 相同、还带身份）。
    """
    app = _build_quiet_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "想一想"})
        await _settle(app, pilot)

        _delta(app, "我先看看", channel="reasoning")
        await _settle(app, pilot)
        live = _thinking_line(app)
        assert "4 字符" in live, live
        assert any(frame in live for frame in view_state.SPINNER_FRAMES), live
        assert "我先看看" not in _log_text(app), "安静模式下思考正文不铺开"

        _delta(app, "，再想想。", channel="reasoning")
        await _settle(app, pilot)
        assert "9 字符" in _thinking_line(app)

        # 真跑时 `model_call` 那条事件在流完之后到，它带着完整的一份 reasoning。
        _events(app, {"kind": "model_call", "run_id": "r1", "step": 1, "status": "ok",
                      "duration_ms": 5, "reasoning": "我先看看，再想想。"})
        await _settle(app, pilot)
        assert "9 字符" in _thinking_line(app), "别为同一段思考再画一行"

        _events(app, {"kind": "run_finished", "run_id": "r1", "step": 1,
                      "stop_reason": "answered", "duration_ms": 100})
        await _settle(app, pilot)
        settled = _thinking_line(app)
        assert "Ctrl+T 展开" in settled, settled
        assert not any(frame in settled for frame in view_state.SPINNER_FRAMES), settled

        # 定格之后 `Ctrl+T` 照旧管用（它认的是那一行的 role）。
        app.action_toggle_thinking()
        await _settle(app, pilot)
        assert "我先看看" in _log_text(app)


@pytest.mark.anyio
async def test_the_status_mark_spins_in_quiet_mode_only(monkeypatch):
    """状态栏那个记号：**安静模式 + 正在跑**才转，其余一切照旧。"""
    app = _build_quiet_app(monkeypatch)

    async with app.run_test(size=(120, 30)) as pilot:
        app._inbox.put(("message", _init_message("s", resumed=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        await _settle(app, pilot)
        bar = app.query_one("#status", widgets.StatusBar)
        left, right = bar.render_parts(app.state, app.palette, (1234.5, 120))
        assert any(frame in str(left) for frame in view_state.SPINNER_FRAMES), str(left)
        assert "安静" in str(right), "开着的模式要在状态栏留一格"

    loud = _build_app(monkeypatch)
    async with loud.run_test(size=(120, 30)) as pilot:
        loud._inbox.put(("message", _init_message("s", resumed=True)))
        _events(loud, {"kind": "run_started", "run_id": "r1", "step": 0,
                       "user_input": "你好"})
        await _settle(loud, pilot)
        bar = loud.query_one("#status", widgets.StatusBar)
        left, right = bar.render_parts(loud.state, loud.palette, (1234.5, 120))
        assert str(left).startswith("●"), str(left)
        assert "安静" not in str(right), "关着时那一格一个字符都不占"


@pytest.mark.anyio
async def test_the_spinner_stops_while_it_is_the_humans_turn(monkeypatch):
    """审批面板压在最上面时那个转圈**停下**（思考那一行也一样）。

    协议里没有"正在等你按键"这个 phase（`protocol/state.py` 那张表只认事件），
    所以这件事由界面判：`App._waiting_for_human`（只认审批/提问那两个面板 ——
    `/theme` 那种选择面板不算，那是你在操作界面，agent 该跑还是跑）。
    """
    app = _build_quiet_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "跑一条命令"})
        _delta(app, "我先想想", channel="reasoning")
        await _settle(app, pilot)
        assert any(frame in _thinking_line(app) for frame in view_state.SPINNER_FRAMES)

        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p1", "call_id": "c1",
            "tool": "shell", "risk": "high", "arguments": {"command": "rm -rf x"},
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)
        assert app._waiting_for_human() is True, "面板已经压上来了"

        bar = app.query_one("#status", widgets.StatusBar)
        left, _right = bar.render_parts(app.state, app.palette, (1234.5, 120, True))
        assert not any(frame in str(left) for frame in view_state.SPINNER_FRAMES), str(left)
        # 思考那一行也停下（字符数留着 —— 它说的还是事实：已经想了这么多）。
        waiting = _thinking_line(app)
        assert not any(frame in waiting for frame in view_state.SPINNER_FRAMES), waiting
        assert "字符" in waiting


# --- 第三层：入口 ---------------------------------------------------------------

def test_the_quiet_flag_parses_and_reaches_the_tui(monkeypatch):
    """`--quiet` 从命令行一路到界面（它是"起手就安静"的唯一入口）。

    解析那一半和 `--theme` 那条同形；接线那一半要真的走一遍 `main()` —— 少写一个
    关键字参数的话，`--quiet` 会被静默吃掉，而"参数被吃掉"和"这个功能不存在"
    在用户眼里一模一样。
    """
    import sys

    from agent_runtime import main as main_module
    from agent_runtime.frontends.cli import build_parser
    from agent_runtime.frontends.tui import app as app_module

    assert build_parser().parse_args(["--tui", "--quiet"]).quiet is True
    assert build_parser().parse_args(["--tui"]).quiet is False

    seen: dict = {}

    def fake_run_tui(session=None, **kwargs):
        seen["session"] = session
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(app_module, "run_tui", fake_run_tui)
    monkeypatch.setattr(sys, "argv", ["tudouni", "--tui", "--quiet"])
    assert main_module.main() == 0
    assert seen["quiet"] is True
