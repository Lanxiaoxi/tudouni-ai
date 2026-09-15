"""启动态：**`init` 到之前那一屏不许说"空闲"。**

## 这份测试守的是什么

父进程一起子进程就把界面画出来了，而子进程那条 `init` 实测约 **1.9 秒**之后才到
（几乎全是 import `openai` 那棵树，数字和量法在 `protocol/serve.py` 那段注释里）。
在这段时间里 `agent.phase` 是初始的 `IDLE`，于是状态栏原本写的是
`○ 空闲 · 说出一句话后才开始` —— **那不是"还没就绪"的委婉说法，是一句假话**：
会话 id、模型、工具清单、权限范围此刻一个字都还没到，而用户这时候提问也发不出去。

所以这一份钉四件事：

  1. **空窗那一屏**：写着"正在启动"、转圈在转、欢迎屏**还没出现**（它要等 `init`）；
  2. **收尾干净**：`init` 一到就变回原来的"空闲 · 说出一句话后才开始"，欢迎屏接着
     出现 —— 也就是说启动态**不会**赖着不走（那是这个改动最容易留下的毛病）；
  3. **换会话不回头**：`/new` / `/resume` 也会来一条 `init`，但那时候子进程活着、
     屏幕上本来就有内容，退回一屏"正在启动"只会把已有的会话遮掉；
  4. **等太久要改口**：超过阈值换成"还没回应"，而**转圈照转**（"没回应"不等于
     "死了"，屏幕上一动不动才是我们想避免的观感）。

第三、四两条是"只让它在正确的时候出现"的判据，比第一条更要紧 —— 一个永远亮着的
"正在启动"和一个永远说的"空闲"是同一类错误。
"""

import time

import pytest

from agent_runtime import i18n
from agent_runtime.frontends.tui import view_state, widgets
from test_tui import _build_app, _init_message, _settle


# `run_test()` 是 async 的，而 anyio 默认会跑所有后端（含没装的 trio）—— 和
# `test_tui.py` 一样钉死成 asyncio。放在这个文件里是因为 fixture 不跨文件继承。
@pytest.fixture
def anyio_backend():
    return "asyncio"


def _status_line(app) -> str:
    """状态栏左边**现在画出来的那一行**（转圈那一帧也在这份字符串里）。

    `boot_slow` 那一格**按 App 自己的判据算**（和 `_refresh_chrome` 一字不差地
    用墙上时钟），而不是写死 False —— 写死的话这一层就把"等太久要改口"那条路
    整个掐掉了，而它正是这份测试要守的四条之一。
    """
    from agent_runtime.frontends.tui import app as app_module

    slow = app.state.booting and \
        time.monotonic() - app._boot_started >= app_module._BOOT_SLOW_SECONDS
    bar = app.query_one("#status", widgets.StatusBar)
    left, _right = bar.render_parts(app.state, app.palette,
                                    (1234.5, 120, app._waiting_for_human(), slow))
    return str(left)


# --- 第一层：空窗那一屏 ---------------------------------------------------------

def test_the_boot_line_is_a_plain_pure_render():
    """启动态那一行**不依赖任何 agent 事实**：`booting` 一置就成立。

    它是纯函数那一层的第一条 —— 界面还没挂载、一条协议消息都没收到时也能算出来。
    """
    state = view_state.ViewState(booting=True)
    line = state.status_left("⠋")
    assert line.startswith("⠋"), line
    assert "正在启动" in line, line
    # 那一格**不能**同时出现"空闲"（那正是它要替掉的那句话）。
    assert "空闲" not in line, line


def test_the_boot_line_says_slow_when_the_app_says_so():
    """超时那一档由 App 判（纯函数不取时间），这里只认它传进来的那个布尔。"""
    state = view_state.ViewState(booting=True)
    normal = state.status_left("⠋")
    slow = state.status_left("⠋", boot_slow=True)
    assert slow != normal, "阈值前后必须是两句话"
    assert "还没回应" in slow, slow
    # **不传转圈时它也是一句完整的话**（那句话自己说的是什么，不靠转圈撑）。
    assert state.status_left("").startswith("正在启动"), state.status_left("")


@pytest.mark.anyio
async def test_the_blank_window_says_starting_and_has_no_welcome_yet(monkeypatch):
    """空窗那一屏：状态栏在转、写着"正在启动"，而**欢迎屏还没挂上**。

    最后那半句是这个改动的一条边界：欢迎屏只能由 `init` 触发（它是"runtime 说
    它准备好了"的证据），启动态不许提前把它画出来 —— 提前画就是拿一个假界面
    去顶那 1.9 秒。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        await _settle(app, pilot)
        assert app.state.booting is True, "起来就该是启动态"
        line = _status_line(app)
        assert "正在启动" in line, line
        assert any(frame in line for frame in view_state.SPINNER_FRAMES), line
        assert not app.query(widgets.WelcomeBlock), "欢迎屏要等 init，不许提前出现"


# --- 第二层：收尾 -----------------------------------------------------------------

@pytest.mark.anyio
async def test_init_ends_the_boot_state_and_the_welcome_screen_takes_over(monkeypatch):
    """`init` 一到：启动态收掉、状态栏回到"空闲 · 说出一句话后才开始"、欢迎屏出现。

    三条断言缺一不可 —— 只钉"欢迎屏出现了"的话，一个赖着不走的启动态照样能过
    （它被欢迎屏盖住了，但状态栏那一行还是假的）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        await _settle(app, pilot)
        assert "正在启动" in _status_line(app)

        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)

        assert app.state.booting is False, "init 就是'启动完了'的证据"
        line = _status_line(app)
        assert "空闲" in line, line
        assert "正在启动" not in line, line
        assert app.query(widgets.WelcomeBlock), "空态那一屏该接着出现"


@pytest.mark.anyio
async def test_a_second_init_does_not_go_back_to_booting(monkeypatch):
    """`/new` / `/resume` 也会来一条 `init`，但**不许退回启动态**。

    那时候子进程活着，屏幕上还有上一个会话的内容 —— 退回一屏"正在启动"会把它遮掉，
    而它遮掉的恰恰是用户按 `/resume` 之后等着看的那一屏。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", _init_message("s1", resumed=True)))
        await _settle(app, pilot)
        assert app.state.booting is False

        app._inbox.put(("message", _init_message("s2", resumed=True)))
        await _settle(app, pilot)
        assert app.state.booting is False, "换会话不该重新进入启动态"
        assert "正在启动" not in _status_line(app)


# --- 第三层：等太久 ---------------------------------------------------------------

@pytest.mark.anyio
async def test_waiting_too_long_changes_the_words_but_keeps_the_spinner(monkeypatch):
    """超过阈值只说"还没回应"，**转圈不停**。

    它给的是一条能查的线索（"原因见终端 stderr"）：子进程在 `init` 之前死掉时
    （配置报错、import 炸了），界面只会一直停在启动态，而那是唯一能把它说出来
    的地方（真去探测"子进程已经退出"要动 `ProtocolClient`，那个今天没有回调）。
    """
    from agent_runtime.frontends.tui import app as app_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        await _settle(app, pilot)
        # 正常范围内（实测 1.9 秒，阈值 10 秒）说的还是"正在启动"。
        app._boot_started -= app_module._BOOT_SLOW_SECONDS / 2
        app._refresh_chrome()
        assert "正在启动" in _status_line(app), _status_line(app)

        app._boot_started -= app_module._BOOT_SLOW_SECONDS
        app._refresh_chrome()
        line = _status_line(app)
        assert "还没回应" in line, line
        assert "stderr" in line, "要指出去哪看原因"
        assert any(frame in line for frame in view_state.SPINNER_FRAMES), \
            "没回应不等于死了，转圈不能停"


# --- 第四层：语言 -----------------------------------------------------------------

@pytest.mark.anyio
async def test_the_boot_line_speaks_english_when_the_ui_does(monkeypatch):
    """英文界面下这一行**一个汉字都没有** —— 它和别的文案一样由 i18n 给。"""
    with i18n.with_language(i18n.EN):
        assert not i18n.missing()
        state = view_state.ViewState(booting=True)
        line = state.status_left("⠋")
        assert "Starting the runtime" in line, line
        assert "No response from the runtime" in state.status_left("⠋", boot_slow=True)
