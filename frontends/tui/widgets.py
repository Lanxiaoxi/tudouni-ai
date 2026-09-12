"""界面的零件。**这个文件和 `app.py` 是唯一两个准 import textual 的地方。**

那条规矩由 `tests/test_imports.py` 里的一条测试盯着，而它的意义是具体的：
老 CLI（`uv run main.py`）、协议子进程（`--runtime-stdio`）、四个不需要模型的
子命令**都不该加载一个 TUI 框架**。破掉它的症状不是报错，是启动变慢、以及在
一个没装 textual 的环境里直接崩 —— 而 `--list` 那种查会话的子命令不该有这种依赖。
"""

from typing import Any

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from agent_runtime.frontends.tui import view_state


class SessionHeader(Static):
    """顶上一行：会话是谁、模型、工作区。"""

    def show(self, state: view_state.ViewState) -> None:
        parts = [f"会话 {state.session_id}"]
        if state.model:
            parts.append(f"模型 {state.model}")
        if state.max_steps:
            parts.append(f"最多 {state.max_steps} 步")
        if state.workspace:
            parts.append(state.workspace)
        self.update("  ·  ".join(parts))


class ConversationLog(RichLog):
    """会话正文。

    **用 `RichLog` 而不是自己维护一个行列表**：它的语义正好是"只追加的日志"
    （和 `audit/jsonl.py` 那个 JsonlSink 同一个形状），而重绘、滚动、裁切这些事
    它都做好了 —— 那正是"用 Textual 而不是自己写 ANSI"买来的东西。

    代价说明白：`RichLog` 默认按 **Rich markup** 解析写进去的字符串，而模型和工具
    的输出里出现 `[` 是家常便饭（路径、列表、代码）。不关掉的话，一段普通文本会被
    吞掉半行或者直接报错 —— 这是"渲染别人的文本"时最经典的坑。

    关掉的**正确做法**是传一个 `rich.text.Text` 对象：`write()` 拿到 `Text` 就不再
    解析 markup。我第一版试过给 `write()` 传 `markup=False`，而 Textual 8 的
    `write()` **没有这个参数**（实测：`TypeError: unexpected keyword argument`）。
    """

    def write_line(self, text: str, *, style: str | None = None) -> None:
        """写一行。**外来文本一律走 `Text`，所以 markup 不会被解析。**"""
        self.write(Text(text, style=style) if style else Text(text))

    def write_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.write_line(line)


class StatusBar(Static):
    """底部那一行：agent 在干什么 + 会话规模 + 权限范围。

    它是**投影**（见 `view_state.ViewState.status_line`），不是第二份事实。
    """

    def show(self, state: view_state.ViewState) -> None:
        self.update(state.status_line())


class PromptInput(Static):
    """输入行。**包一层 `Static` 只是为了有个稳定的容器** —— 真正的 `Input` 由
    `app.py` 在 compose 里放进来，因为它的 `Submitted` 消息要由 App 处理。

    这里不放 `Input` 的原因很实在：`prompt` 前缀（`> `）要写在 stderr/界面之外
    的地方，而 App 需要拿到焦点管理。少一层间接就少一处"焦点跑到别的控件上"。
    """


class PermissionPanel(ModalScreen):
    """审批面板。**它是一个 `ModalScreen`，不是一个容器。**

    我第一版把它做成 `Vertical`，然后 `push_screen(panel)` —— Textual 8 直接拒了：
    `push_screen requires a Screen instance or str`。做成 Screen 还有第二个好处：
    `Esc` 关闭、焦点锁定、`dismiss(结果)` 这些由它保证，而"盖住下面一层"正是
    审批该有的样子。
    """

    BINDINGS = [("escape", "dismiss_deny", "拒绝")]

    def __init__(self, request: dict[str, Any], **kwargs: Any):
        super().__init__(**kwargs)
        self.request = request

    def compose(self):
        tool = self.request.get("tool", "?")
        risk = self.request.get("risk", "?")
        with Vertical(id="permission-body"):
            yield Static(f"╭─ 需要审批 ─ {tool}   风险 {risk}")
            for name, value in (self.request.get("arguments") or {}).items():
                yield Static(f"│ {name} = {value}")
            # **后果那句话原样显示，一个字都不改**（决策 16）：它是"按下去会发生
            # 什么"唯一的说明，出自 security/asker.py 的 `_remember_hint`。
            hint = self.request.get("remember_hint")
            if hint:
                yield Static(f"│ t = {hint}")
            trust = self.request.get("trust_all_hint")
            if trust:
                yield Static(f"│ a = {trust}")

            with Horizontal():
                yield Button("允许 (y)", id="allow", variant="success")
                yield Button("拒绝 (n)", id="deny", variant="error")
                # 只有后端说"这次能记住"时才给按钮。**不补一个"总是允许整个
                # shell"** —— 那正是 runtime 刻意堵掉的东西（推不出命令前缀时它
                # 宁可不提供这个键）。
                if self.request.get("remember_hint"):
                    yield Button("总是允许 (t)", id="always")
                if self.request.get("allow_trust_all"):
                    yield Button("都允许 (a)", id="always_group")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_dismiss_deny(self) -> None:
        """`Esc` = **拒绝**，不是"关掉再说"。

        fail-closed：和 `cli_asker` 读不到输入那一支同一个方向（默认拒绝才是安全的
        失败方向）。
        """
        self.dismiss("deny")


class QuestionPanel(ModalScreen):
    """提问面板。和审批面板是两件事（见 tools/builtin/ask.py 的分工）。"""

    BINDINGS = [("escape", "dismiss_skip", "跳过")]

    def __init__(self, request: dict[str, Any], **kwargs: Any):
        super().__init__(**kwargs)
        self.request = request
        self._options: list[str] = list(request.get("options") or [])

    def compose(self):
        with Vertical(id="question-body"):
            yield Static(f"╭─ 提问 ─ {self.request.get('question', '')}")
            header = self.request.get("header")
            if header:
                yield Static(f"│ （{header}）")
            for index, option in enumerate(self._options, 1):
                yield Button(f"{index}) {option}", id=f"option-{index}")
            # **跳过必须是显式的一个键**，不能只靠"关掉面板"：回车在连续交互里
            # 是最容易做的动作，而"回车即同意"会把最危险的那条路改成手滑也能过。
            yield Button("跳过 (Esc)", id="skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or "skip"
        if button_id == "skip":
            self.dismiss(("skipped", ""))
            return
        index = int(button_id.split("-")[1])
        # 回**选项原文**而不是编号：编号是界面的表示法，而后端要的是内容
        # （`tools/builtin/ask.py` 的 `_choose` 做的就是同一件事）。
        text = self._options[index - 1] if 1 <= index <= len(self._options) else ""
        self.dismiss(("answered", text))

    def action_dismiss_skip(self) -> None:
        self.dismiss(("skipped", ""))


def scroll_container(*children: Any) -> VerticalScroll:
    """会话区的容器。做成函数而不是类，是因为它没有任何行为 —— 一个空子类只会
    多一处要维护的继承。"""
    return VerticalScroll(*children)
