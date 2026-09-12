"""Textual 客户端：**跑在父进程里**，通过 stdio 协议驱动一个 runtime 子进程。

## 三件事必须说清楚，否则读这段代码会以为哪里写错了

### 1. 协议回调和 Textual 的界面更新**不在同一个线程**

`ProtocolClient` 有一个读线程在拆 stdout；Textual 的界面在它自己的事件循环里。
第一版想用 `App.call_from_thread` 直接跨过去，实测撞上两个问题：它在 **None 屏幕**
上抛 `ScreenError`（`init` 到达时界面还没挂载），而 Textual 的线程检查又不一定认得出
我们的读线程。

所以改成一个**消息泵**：协议回调只往一个 `queue.Queue` 里放东西（什么线程都能放，
这是 `queue` 的契约），界面用一个 50ms 的定时器去排空它。

代价说明白：**最多 50ms 的排版延迟**。对一个秒级往返的 agent 界面，这换来的确定性
更值钱 —— 而且它是"要么全到、要么晚到 50ms"，不会丢、不会乱序（`queue` 保序）。

### 2. "转圈"必须自己造

没有流式（决策 1），所以模型往返和工具执行期间**界面上不会有任何新东西**。
一次往返是秒级 —— 一个完全静止的界面会被当成卡死。所以状态栏那一行在
`working` 时会显示模型/工具**正在做什么**（`protocol/state.py` 的 `activity`），
而这需要界面自己随事件更新。这是 v1 的验收项（R6 第 1 条），不是打磨。

### 3. 审批是**非阻塞**的（对读线程而言）

`on_permission` 被读线程调用，它只把请求塞进队列、**立刻返回** —— 否则读线程就
卡在人身上了。真正的回答由界面在用户点按钮之后调 `client.answer_permission(...)`。
子进程那一侧本来就会一直等（`ProtocolServer.wait`），所以"等"发生在**它**那儿，
而不是在我们的读线程上。
"""

import queue
import sys
from typing import Any

from textual.app import App
from textual.widgets import Footer, Input, RichLog, Static

from agent_runtime.frontends.tui import view_state, widgets
from agent_runtime.protocol import messages
from agent_runtime.protocol import state as agent_state
from agent_runtime.protocol.client import ProtocolClient


class TuiApp(App[None]):
    """那个界面。实现 `ClientHooks`（四个回调）。"""

    CSS = """
    #header { dock: top; height: 1; background: $panel; }
    #status { dock: bottom; height: 1; background: $panel; }
    #input  { dock: bottom; }
    #log    { height: 1fr; }
    """

    BINDINGS = [
        ("ctrl+c", "quit_app", "退出"),
        ("ctrl+t", "toggle_thinking", "展开/折叠思考"),
        ("ctrl+r", "resume_last", "重开会话"),
    ]

    def __init__(self, session: str | None = None, *, autopilot: bool = False):
        super().__init__()
        self._session = session
        self._autopilot = autopilot
        self.state = view_state.ViewState()
        # 协议回调往这里放（**任何线程都能放**），界面定时排空它。
        self._inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._client: ProtocolClient | None = None
        # **名字不能叫 `_ready`**：`App` 自己有一个 `_ready()` 方法，而 `run_test()`
        # 会去调它 —— 被一个 bool 盖住之后报的是 `TypeError: 'bool' object is not
        # callable`，从栈上看完全指不到这里。实测踩过。
        self._greeted = False

    # -- 组装 ------------------------------------------------------------------

    def compose(self):
        yield widgets.SessionHeader(id="header")
        yield widgets.ConversationLog(id="log")
        yield widgets.StatusBar(id="status")
        yield Input(placeholder="说点什么，回车发送（/help 看命令）", id="input")

    def on_mount(self) -> None:
        self._client = ProtocolClient(self, session=self._session,
                                      autopilot=self._autopilot)
        self._client.start()
        # 消息泵。见模块 docstring 第 1 条：**不用 call_from_thread**。
        self.set_interval(0.05, self._pump)
        self.query_one("#input", Input).focus()

    # -- ClientHooks（**在读线程里被调用，只许入队**） -------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        self._inbox.put(("message", message))

    def on_permission(self, request: dict[str, Any]) -> str | None:
        """把请求塞给界面，**返回 None 表示"界面稍后自己回"**。

        **绝不能在这里返回一个兜底答案。** 第一版返回了 `messages.DENY`，想的是
        "稍后用真答案覆盖" —— 而客户端立刻就把那个 DENY 发出去了，子进程据此拒绝、
        继续往下跑；等用户点 [允许] 时那条回应已经没人要（更糟：中间那次拒绝进了
        审计，记成 `user_denied`）。实测的症状是"面板弹出来了、按钮也点了，
        但工具结果是「用户拒绝」"。

        所以这个回调**只负责转交**，回答是 `_ask_permission` 的 `answered` 回调
        通过 `client.answer_permission` 发的。
        """
        self._inbox.put(("permission", request))
        return None

    def on_question(self, request: dict[str, Any]) -> tuple[str, str] | None:
        """同 `on_permission`：转交，返回 `None`，回答由面板发。"""
        self._inbox.put(("question", request))
        return None

    # -- 消息泵 ----------------------------------------------------------------

    def _pump(self) -> None:
        """把队列里的东西画到界面上。**这是唯一改界面的地方。**

        ## 三条防御，都是实测出来的

        `set_interval` 的回调会在**界面还没挂载完**以及**界面已经开始拆**的时候也被
        调用（定时器不跟着 DOM 走）。而 `query_one` 在那两个时刻都抛 `NoMatches` ——
        症状是启动/退出时偶发一个 traceback，看起来像别的地方坏了。

        所以：每次拿控件都用 `_widget`（拿不到就跳过），而且消息**只弹一次** ——
        界面已经在拆了还把队列排空，只会往一个死 DOM 上写。
        """
        if not self.is_running:
            return
        while True:
            try:
                kind, payload = self._inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "message":
                self._on_protocol_message(payload)
            elif kind == "permission":
                self._ask_permission(payload)
            elif kind == "question":
                self._ask_question(payload)

        status = self._widget("#status", widgets.StatusBar)
        if status is not None:
            status.show(self.state)

    def _widget(self, selector: str, expect: type):
        """拿一个控件；**现在还没有就返回 None**（而不是抛 `NoMatches`）。

        见 `_pump` 的 docstring：定时器会在 DOM 没准备好的时候也被调用。
        """
        found = self.query(selector)
        if not found:
            return None
        widget = found.first()
        return widget if isinstance(widget, expect) else None

    def _log(self):
        return self._widget("#log", RichLog)

    def _say(self, text: str, *, style: str | None = None) -> None:
        """往会话里说一句界面自己的话（命令回显、提示）。

        **不抛**：拿不到控件就当没说 —— 它只可能在界面还没挂载完或正在拆的时候发生，
        而那两个时刻没有"用户看不到这条提示"之外的后果。
        """
        log = self._log()
        if log is not None:
            log.write_line(text, style=style)

    def _on_protocol_message(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        log = self._log()
        if log is None:
            return

        if kind == messages.OUT_INIT:
            self._on_init(message)
        elif kind == messages.OUT_SESSION_LOAD:
            self._on_session_load(message)
        elif kind == messages.OUT_EVENT:
            self.state.agent = agent_state.reduce(self.state.agent, message)
            log.write_lines(view_state.render_event(self.state, message))
        elif kind == messages.OUT_UI:
            self.state.agent = agent_state.reduce(self.state.agent, message)
            log.write_lines(view_state.render_ui_answer(self.state, message))
        elif kind == messages.OUT_NOTICE:
            level = message.get("level", "info")
            log.write_line(
                f"[{level}] {message.get('text', '')}",
                style="yellow" if level == "warn" else None,
            )

    def _on_init(self, message: dict[str, Any]) -> None:
        self.state.session_id = message.get("session_id", "")
        self.state.model = message.get("model", "")
        self.state.max_steps = message.get("max_steps", 0)
        self.state.workspace = message.get("workspace", "")
        self.state.audit_path = message.get("audit_path", "")
        self.state.permissions = dict(message.get("permissions") or {})
        self.state.tool_risks = {
            tool["name"]: tool["risk"] for tool in message.get("tools") or []
        }
        header = self._widget("#header", widgets.SessionHeader)
        if header is not None:
            header.show(self.state)

        log = self._log()
        assert log is not None, "init 到达时 #log 必然已经挂载（调用方刚查过）"
        resumed = "（继续）" if message.get("resumed") else "（新的）"
        log.write_line(f"会话 {self.state.session_id}{resumed}")
        for notice in message.get("notices") or []:
            log.write_line(
                f"[{notice.get('code')}] {notice.get('text')}",
                style="yellow" if notice.get("level") == "warn" else None,
            )
        # **把续聊的办法说出来**：新会话在 CLI 那边是靠启动那行提示的，
        # 而 TUI 里没有那一行 —— 不说的话用户不知道怎么回来。
        if not message.get("resumed"):
            log.write_line(f"想回来继续它：main.py --tui --session {self.state.session_id}")
        log.write_line("")
        self._greeted = True

    def _on_session_load(self, message: dict[str, Any]) -> None:
        """恢复会话时重建画面。

        **它只画用户和 agent 说过的话，不画工具卡片**（决策 3：v1 不渲染工具卡片）。
        工具结果在 `messages` 里是全文（`role=="tool"`），想看就 `/history`。
        """
        log = self._log()
        assert log is not None
        restored = [
            m for m in (message.get("messages") or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if not restored:
            return
        log.write_line(f"（恢复 {len(message.get('messages') or [])} 条历史，"
                       f"下面是你说过的和 agent 答过的）")
        for msg in restored:
            who = "你" if msg["role"] == "user" else "agent"
            log.write_line(f"[{who}] {msg['content']}")
            log.write_line("")

    # -- 人机交互（非阻塞：塞回给子进程，而不是在这里等） ----------------------

    def _ask_permission(self, request: dict[str, Any]) -> None:
        panel = widgets.PermissionPanel(request, id="permission")

        def answered(decision: str | None) -> None:
            if self._client is None:
                return
            # `dismiss(None)`（Esc / 关掉）按**拒绝**处理：fail-closed，
            # 和 `cli_asker` 读不到输入那一支同一个方向。
            self._client.answer_permission(
                request.get("id", ""), decision or messages.DENY
            )

        self.push_screen(panel, answered)

    def _ask_question(self, request: dict[str, Any]) -> None:
        panel = widgets.QuestionPanel(request, id="question")

        def answered(result: Any) -> None:
            if self._client is None:
                return
            status, text = result if isinstance(result, tuple) else ("skipped", "")
            self._client.answer_question(request.get("id", ""), status, text)

        self.push_screen(panel, answered)

    # -- 输入 ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.value = ""
        self.submit(event.value)

    def submit(self, text: str) -> None:
        """处理一句用户输入。**不碰 Textual 的消息对象** —— 所以它能被直接单测。

        第一版把这段写在 `on_input_submitted` 里，于是测试只能去伪造一个
        `Input.Submitted` 再 `post_message` —— 而那条消息在 Textual 8 里怎么派发
        取决于焦点和订阅，测出来的是"消息路由对不对"，不是"我们的逻辑对不对"
        （实测：那条断言一直拿到空列表）。抽出来之后两件事分开测：这一段直接调，
        消息路由交给 Textual 自己。
        """
        text = text.strip()
        if not text:
            return
        if self._handle_slash(text):
            return
        if self._client is not None:
            self._client.user_message(text)

    def _handle_slash(self, text: str) -> bool:
        """`/` 命令。v1 只有这几条（决策 15）。

        `/new` 和 `/resume` 都是**重开进程**：`TodoBoard` / `SkillBoard` 绑在
        `session.metadata` 上，同进程换会话要重新装配整张注册表。所以它们在这里
        只是"告诉用户怎么做"，而不是假装能做到 —— 一个按了没反应的服务比没有更坏。
        """
        if not text.startswith("/"):
            return False
        command, _, rest = text.partition(" ")
        command = command.lower()

        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            log = self._log()
            if log is None:
                return True
            log.write_line("命令：")
            log.write_line("  /exit            退出")
            log.write_line("  /audit           审计日志在哪")
            log.write_line("  /new             换一个新会话（重开后加 --session）")
            log.write_line("  /resume <id>     接着某个会话（重开：--tui --session <id>）")
            log.write_line("  /list            列会话要另开一个终端：main.py --list")
            log.write_line("  Ctrl+T           展开/折叠思考过程")
        elif command == "/audit":
            self._say(f"审计日志：{self.state.audit_path}")
        elif command == "/new":
            self._say("换会话要重开：main.py --tui")
        elif command == "/resume":
            self._say(f"接着聊要重开：main.py --tui --session {rest.strip() or '<id>'}")
        elif command == "/list":
            self._say("会话列表要另开一个终端看：main.py --list")
        else:
            self._say(f"没有这个命令：{command}（/help 看有哪些）")
        return True

    # -- 动作 ------------------------------------------------------------------

    def action_toggle_thinking(self) -> None:
        """展开/折叠最近一段思维链。**纯界面操作**，不改变任何 agent 的事实。"""
        if not self.state.thinking:
            self._say("（这一轮还没有思考过程）")
            return
        run_id = list(self.state.thinking)[-1]
        text, expanded = self.state.thinking[run_id]
        self.state.toggle_thinking(run_id)
        log = self._log()
        if log is None:
            return
        if expanded:
            log.write_line("  （已折叠）")
        else:
            log.write_line("  ┌ 思考过程")
            log.write_line(view_state.indent(text, "  │ "))
            log.write_line("  └")

    def action_resume_last(self) -> None:
        """`Ctrl+R`：把"怎么接着聊"再说一遍。

        这里是**有意不做成"重启进程"**的：一次误触就把会话换掉，而用户以为只是
        刷新了一下。所以它只提示。
        """
        self._say(f"接着聊要重开：main.py --tui --session {self.state.session_id}")

    def action_quit_app(self) -> None:
        self.exit()

    # -- 收摊 ------------------------------------------------------------------

    def on_unmount(self) -> None:
        """界面关了就把子进程收掉。

        `ProtocolClient.close()` 是**先请求停止再等**（见那里的 docstring）——
        直接 kill 会让子进程死在半个回合上，留下一个此后发不出去的会话。
        """
        if self._client is not None:
            self._client.close()
            self._client = None


def run_tui(session: str | None = None, *, autopilot: bool = False) -> int:
    """`main.py --tui` 走这里。

    **配置错时子进程会以退出码 2 结束、并把原因打在 stderr 上。** 那一支由界面
    自然显示（stderr 是继承的，所以那几行会出现在终端上）—— 这条路径刻意不特殊
    处理"还没起来就失败"，因为它的表现是"界面闪一下就退"，而 stderr 上的原因是
    看得见的。
    """
    app = TuiApp(session=session, autopilot=autopilot)
    app.run()
    client = app._client
    return 0 if client is None or client.exit_code in (None, 0) else client.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_tui(sys.argv[1] if len(sys.argv) > 1 else None))
