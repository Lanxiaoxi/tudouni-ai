"""命令面板**装得下全部命令** —— 一条回归测试，来自一个真实的截图。

用户按 `/` 打开面板，看到的是 `/new` … `/status` 九条，而后四条（`/tools` `/model`
`/thinking` `/effort`）**根本不在屏幕上**：没有滚动条、没有省略号、没有"还有 N 条"。

原因是一行 CSS：`max-height: 12` 是命令只有 8 条时手写的，而命令长到 13 条之后它把
最后四条整个切掉了。TUI 的 `#palette` 是**屏幕纵向布局里的一行**（不是浮层），所以它
溢出时不会自己滚，也不会报错 —— 只是少了几行。

这一条的教训值得记下来：**"面板里看得见"不是排版问题，是发现命令的唯一入口。**
看不见命令 ≈ 那条命令不存在，而两者在屏幕上长得一模一样。
"""

import re

import pytest

from agent_runtime.frontends.tui import view_state
from test_tui import _build_app, _settle


def _palette_max_height() -> int:
    """CSS 里 `#palette` 的 `max-height`。**从真的 CSS 里读**，不另抄一个数。"""
    from agent_runtime.frontends.tui.app import TuiApp

    match = re.search(r"#palette \{.*?max-height: (\d+)", TuiApp.CSS, re.S)
    assert match, "CSS 里找不到 #palette 的 max-height —— 这条测试要跟着改"
    return int(match.group(1))


def test_the_palette_can_show_every_command():
    """面板的高度上限**必须装得下全部命令**（标题 1 行 + 上下边框各 1 行）。

    上限写死过一个 12，而命令长到 13 条时它只能显示 9 条 —— 剩下四条静默消失。
    所以这里比的不是一个具体的数，而是"够不够"：以后再加命令时，只要上限是从条数算
    出来的（`_PALETTE_MAX_ROWS`），这条测试就一直成立。
    """
    needed = len(view_state.COMMANDS) + 3
    assert _palette_max_height() >= needed, (
        f"命令 {len(view_state.COMMANDS)} 条需要 {needed} 行，"
        f"而面板上限只有 {_palette_max_height()} 行 —— 多出来的命令会被静默吃掉"
    )


def test_the_options_list_can_scroll_as_a_last_resort():
    """万一哪天命令多到上限之外，**滑块是唯一能把它们翻出来的东西**。

    高度算对了就不该发生，但"算对了"这件事只由上面那条测试守着；真发生时的后果是
    "命令不见了"（不是报错），所以这里再要一道兜底：选项列表可滚。
    """
    from agent_runtime.frontends.tui.app import TuiApp

    match = re.search(r"#palette-options \{(.*?)\}", TuiApp.CSS, re.S)
    assert match, "CSS 里找不到 #palette-options —— 这条测试要跟着改"
    rules = match.group(1)
    assert "overflow-y: auto" in rules, "选项列表不能滚 = 多出来的命令翻不到"
    assert "scrollbar-size-vertical" in rules


@pytest.mark.anyio
async def test_the_last_command_is_actually_on_screen(monkeypatch):
    """端到端那一条：打开面板之后，**最后一条命令真的画在屏幕上**。

    上面两条读的是 CSS，而 CSS 对不对和"屏幕上有没有"是两件事（值没代入、控件没挂上、
    坐标算错都能让它们分家 —— 实测过一次：`{_PALETTE_MAX_ROWS}` 原样留在了 CSS 里，
    而读 CSS 的那条测试当时还没写）。所以这里渲染一次，在屏幕文本里找它。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 45)) as pilot:
        palette = app.query_one("#palette")
        palette.show("/", app.palette)
        palette.display = True
        await _settle(app, pilot)

        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        missing = [c.name for c in view_state.COMMANDS if c.name not in rendered]
        assert not missing, f"这些命令没画出来：{missing}"
