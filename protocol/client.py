"""协议客户端：**前端那一侧的公共层**。

## 为什么它在 `protocol/` 而不在 `frontends/`

因为它是"协议怎么走"的知识，不是"界面怎么画"的知识。两个前端（TUI、ansi 冒烟
渲染器）要做的事**一模一样**：起子进程、读 JSONL、把 `permission_request` 交给
界面的回调、把回答写回去。如果这一层住在 `frontends/`，那么：

  * 每个前端都要自己 `import agent_runtime.protocol.messages` 去认那些消息种类
    —— 而前端只该认识**回调**；
  * 换一个前端就得把这段重写一遍（或者从别的前端 import，那就变成"前端之间共享
    代码"，而 `frontends/` 的规矩是各前端互不依赖）。

放在这里，前端的接口就缩成四个回调：**收到消息、要审批、要提问、子进程没了**。

## 同步而不是 async

协议服务端是同步的（一个读循环 + 一个回合线程），客户端这一侧也就没必要 async：
一个读线程把 stdout 拆成消息，主线程做界面。`Textual` 有它自己的事件循环，到时候
在回调里 `call_from_thread` 即可 —— 那是 Textual 的事，不是这一层的事。

**这一层不认识任何界面**：它不知道 ANSI 还是 Textual 还是 Web，只认识那四个回调。
（`tests/test_imports.py` 里有一条测试盯着"protocol 不许 import 前端"。）
"""

import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agent_runtime.protocol import codec, messages


class ClientHooks(Protocol):
    """前端的接口。**四个回调，没有别的。**

    做成 Protocol 而不是基类，是为了让前端**不必** import 任何东西就能实现它 ——
    Textual 那个应用和 200 行的 ANSI 渲染器都可以是普通函数。
    """

    def on_message(self, message: dict[str, Any]) -> None:
        """收到一条出站消息（`init` / `session_load` / `event` / `ui` / `notice`）。

        人机交互那两条**不走这里** —— 它们有专门的钩子，因为它们的处理方式不同：
        必须回一句话，而且会阻塞对方。
        """

    def on_permission(self, request: dict[str, Any]) -> str | None:
        """要审批。**返回 `messages.DECISIONS` 里的一个字符串，或者 `None`。**

        `None` 的意思是 **"我的界面会稍后自己回"** —— 这条通道是给 TUI 那种
        异步界面的：它没法在**读线程**上等人点按钮（那会把读线程钉住，而读线程还要
        负责收别的消息）。

        **不返回 `None` 的界面必须当场给出答案**（ANSI/CLI 那种行式界面就是）。
        子进程那一侧本来就会一直等（`ProtocolServer.wait`），所以"等"发生在它那儿
        —— 而这正是 `None` 能成立的原因：不回答 = 它继续等，而不是它按默认值走。

        ## 一个实测踩到的坑

        第一版 TUI 这里 `return messages.DENY` 当兜底 —— 想的是"我稍后会用真答案覆盖
        它"。**那不可能成立**：客户端立刻就把这个 DENY 发出去了，子进程据此拒绝并
        继续往下跑，等用户点 [允许] 时那条回应已经没人要了（而且更糟：中间那次
        拒绝会进审计，记成 `user_denied`）。所以"稍后回答"必须在协议层就表达出来，
        而不是靠一个会被抢先送出的兜底值。
        """

    def on_question(self, request: dict[str, Any]) -> tuple[str, str] | None:
        """要提问。返回 `(status, text)`，或者 `None`（同 `on_permission`）。

        `status` ∈ `answered` / `skipped` —— `unavailable` 是 runtime 自己产生的，
        界面永远不回它。
        """


def runtime_entrypoint() -> Path:
    """`main.py` 的绝对路径。

    `__file__` 是 `<包>/protocol/client.py`，所以包目录是**上两级**、仓库根是再上一级
    （实测踩过一次：少算一级会让子进程去找 `C:\\...\\repo\\main.py`，而报错是
    "can't open file" —— 看起来像路径写错了，其实是层级算错了）。
    """
    return Path(__file__).resolve().parent.parent / "main.py"


def repo_root() -> Path:
    return runtime_entrypoint().parent.parent


def default_argv(session: str | None = None, *, autopilot: bool = False,
                 debug: bool = False, stream: bool = True) -> list[str]:
    """起 runtime 子进程的命令行。

    **三件事都是踩过才知道的**（完整理由见 `doc/protocol.md` 和
    `protocol/transport_stdio.py`）：

      1. **`sys.executable`，不是 `"python"`** —— 父进程跑在哪个解释器里（venv、
         uv 管的那个），子进程就必须是同一个。写 `"python"` 会走到系统 PATH 上另一个
         解释器，而那个里面**没装 openai / pydantic**，症状是子进程立刻退出、
         父进程读到 EOF；
      2. **`-u`** —— stdout 接管道时 Python 用块缓冲，不关掉就会出现"事件攒在缓冲区
         里、界面几秒不动"；
      3. **绝对路径 + `cwd=仓库根`，不能用 `python -m agent_runtime.main`** ——
         项目是 `package = false`，`agent_runtime` 根本没被安装，`-m` 找不到它。
         `main.py` 里那句 `sys.path.insert` 在当脚本跑时生效、在 `-m` 下不生效。

    `stream` 默认**开**：这个入口只服务界面（TUI / ansi），而界面要的就是逐字。
    `--no-stream` 显式传一个 `--stream` 过去关掉它 —— **两个方向都写出来**，
    因为子进程的默认值不需要和父进程的意图一致：这里说了才算。
    """
    argv = [sys.executable, "-u", str(runtime_entrypoint()), "--runtime-stdio"]
    if session is not None:
        argv += ["--session", session]
    if autopilot:
        argv.append("--autopilot")
    if debug:
        argv.append("--debug")
    argv.append("--stream" if stream else "--no-stream")
    return argv


class ProtocolClient:
    """一个协议客户端。用 `with` 或显式 `close()`。"""

    def __init__(
        self,
        hooks: ClientHooks,
        *,
        session: str | None = None,
        autopilot: bool = False,
        debug: bool = False,
        stream: bool = True,
        stderr_to: Any = None,
    ):
        self.hooks = hooks
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._closed = False
        self._started = False
        # 写锁：审批回调从读线程来、用户输入从主线程来，两边都可能写。
        self._lock = threading.Lock()
        # 子进程以非 0 退出时的那句话（父进程要把它显示出来，而不是当成崩溃）。
        self.exit_code: int | None = None
        self._spawn(session, autopilot=autopilot, debug=debug, stream=stream,
                    stderr_to=stderr_to)

    def _spawn(self, session, *, autopilot, debug, stream, stderr_to) -> None:
        self._process = subprocess.Popen(
            default_argv(session, autopilot=autopilot, debug=debug, stream=stream),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr **继承**（None）：子进程的 traceback 和 `[warn]` 直接落在终端上。
            # 这是刻意选的失败方向 —— 一个什么都不显示的 traceback 比花屏更坏。
            # 想收进面板就传一个流进来（第二期的 Textual 客户端会这么做）。
            stderr=stderr_to,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root()),
            env=_child_env(),
        )

    # -- 发 --------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        """写一条。**加锁**：审批回调在读线程里、用户输入在主线程里。"""
        if self._closed or self._process is None or self._process.stdin is None:
            return
        line = codec.encode({"v": messages.VERSION, **message})
        with self._lock:
            try:
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (BrokenPipeError, ValueError):
                # 子进程已经走了。**不抛** —— 界面接下来会发现（读线程会结束、
                # `wait()` 会给出退出码），由它决定怎么显示。
                self._closed = True

    def user_message(self, text: str) -> None:
        self.send({"t": messages.IN_USER_MESSAGE, "text": text})

    def answer_permission(self, request_id: str, decision: str) -> None:
        assert decision in messages.DECISIONS, f"非法 decision：{decision!r}"
        self.send({"t": messages.IN_PERMISSION_RESPONSE, "id": request_id,
                   "decision": decision})

    def answer_question(self, request_id: str, status: str, text: str) -> None:
        self.send({"t": messages.IN_QUESTION_RESPONSE, "id": request_id,
                   "status": status, "text": text})

    def interrupt(self) -> None:
        """请 runtime **停下当前这一轮**（Esc）。

        它和 `shutdown()` 是两件事，别合并：`shutdown` 只是收摊，当前这一轮会跑完
        —— 而"我改主意了，别跑了"要的恰恰是停下这一轮、但会话留着。

        非阻塞：它只写一行，runtime 在下一个**安全点**（两步之间）停下，然后照常
        发 `run_finished(stop_reason=cancelled)`。所以界面不该在这里就把状态改成
        "已停止" —— 那会是第二份事实（真正停下由那条事件说）。
        """
        self.send({"t": messages.IN_INTERRUPT})

    def switch_session(self, session_id: str | None = None) -> None:
        """**原地换一个会话**（TUI 的 `/new` 和 `/resume`）。

        `session_id=None` = 新会话（id 由 runtime 分配）。**它不是"重启"**：子进程
        活着，runtime 换掉自己那一半，然后重发 `init` / `session_load` / `ui state`
        三连 —— 界面按那一组消息刷新即可（见 `protocol/channels.py` 的
        `_session_switch`）。

        它是非阻塞的：这里只写一行。成功与失败都由回来的消息说 —— 成功是新的
        `init`，失败是一条 `notice`（换不过去时旧会话**原样保留**）。
        """
        self.send({"t": messages.IN_SESSION_SWITCH, "session_id": session_id})

    def list_sessions(self) -> None:
        """请 runtime 回一份会话清单（出站 `sessions`）。**同样是发一条就走。**"""
        self.send({"t": messages.IN_SESSION_LIST})

    def set_autopilot(self, on: bool) -> None:
        """运行中开关 autopilot（TUI 的 `/autopilot`）。

        **发的是绝对状态，不是"切一下"**：重发同一条是幂等的，界面也不需要先知道
        runtime 现在是什么状态 —— 所以不存在"两条消息各切一次"的竞态。

        和 `switch_session` / `interrupt` 一样**非阻塞、也不许乐观更新**：真正生效的
        证据是 runtime 回来的那条 `ui` / `kind=state` 快照（里面带 `autopilot`）。
        界面在这之前就改显示的话，"点了没生效"会以"灯亮着但还在问我"的形式出现 ——
        而那件事在 autopilot 上格外要紧：它意味着"我以为它不问，其实它还在问"。
        """
        self.send({"t": messages.IN_SET_AUTOPILOT, "on": bool(on)})

    def set_model(self, model: str) -> None:
        """换这个会话用哪个模型（TUI 的 `/model`）。

        和 `set_autopilot` 同一条规矩：非阻塞、**不许乐观更新**。真正生效的证据是
        runtime 回来的那条 `ui` / `kind=state`（里面带 `model`），而名字认不认识由
        runtime 判（目录是它的知识）—— 认不出来的话回来的是一条 `notice`，界面照着说。

        **一轮正跑着的时候也可以发**：本轮已经用旧模型发出去了，所以本轮不受影响，
        "模型换了"那句话留到下一轮开头（见 `state/model.py` 的 `SessionModel`）。
        """
        self.send({"t": messages.IN_SET_MODEL, "model": str(model)})

    def set_thinking(self, on: bool) -> None:
        """开关思考模式（TUI 的 `/thinking`）。

        **发的是绝对状态**（和 `set_autopilot` / `set_model` 同一条）：重发幂等，界面
        也不必先知道现在是什么。它**不动强度** —— `/effort` 设过的那个记着，再打开时
        还是它。

        生效的时序和换模型一样：**下一次请求**。正在跑的那一轮参数已经发出去了。
        """
        self.send({"t": messages.IN_SET_THINKING, "on": bool(on)})

    def set_effort(self, effort: str) -> None:
        """改思考强度（TUI 的 `/effort`）。

        强度值由 runtime 校验（只认 `low` / `high` / `max`，外加几个等价写法）——
        界面只管把用户写的那个字符串发过去：**"哪几档是合法的"是 domain 的知识**，
        前端自己抄一份清单就会在下一次加档位时漂掉。
        """
        self.send({"t": messages.IN_SET_EFFORT, "effort": str(effort)})

    def ask_status(self) -> None:
        """请 runtime 回一份状态（TUI 的 `/status`）。

        **回包是 `ui` / `kind=status`**，不是一条专门的消息类型：它是"给界面看的
        东西"，和面板快照同一条通道。要的账（tokens）是 runtime 读审计日志数出来的，
        所以这条命令**必须走 runtime** —— 前端自己去读 `.tudouni/logs/` 会让目录布局
        变成前端也认识的一件事实（和 `list_sessions` 那条理由一样）。
        """
        self.send({"t": messages.IN_STATUS})

    def ask_tools(self) -> None:
        """请 runtime 回一份工具清单（TUI 的 `/tools`，回包 `ui` / `kind=tools`）。

        按需发：那份清单有几十行，挂在每一次状态快照上就是白付的带宽和渲染。
        权限那一列也由 runtime 给（策略是它的知识），前端不做判定。
        """
        self.send({"t": messages.IN_TOOLS})

    def mcp(self, action: str, servers: tuple[str, ...] | list[str] = ()) -> None:
        """看/改 MCP server 的挂载（TUI 与 CLI 的 `/mcp`，回包 `ui` / `kind=mcp`）。

        `action` ∈ `messages.MCP_ACTIONS`（`list` / `load` / `unload`），`servers` 是
        要动的那几个名字。**一次一个**：不做 `all` 这种批量写法 —— 它和面板里按一次
        开关是同一件事，而批量会把"哪几个成了、哪几个没成"揉成一句话。

        ## 两条和别处一样的规矩

          * **非阻塞、不乐观更新。** 这里只写一行，真正生效的证据是回来的
            `ui(kind=mcp)` 快照（每一格的状态由 runtime 写）。界面在那之前就把
            "已加载"画上去的话，`npx` 起不来时会变成一句假话；
          * **只有人按键才发它。** 它是唯一能改"模型看得到什么工具"的入口，所以
            前端**不许**在启动、回合结束、收到消息时顺手发一条 —— 那会让"配置自己
            变宽"成为可能（理由写在 `messages.IN_MCP` 那段）。
        """
        self.send({
            "t": messages.IN_MCP,
            "action": str(action),
            "servers": [str(name) for name in servers],
        })

    def refresh_state(self) -> None:
        """请 runtime **现在**重算一份面板快照（回包 `ui` / `kind=state`）。

        它存在的理由只有一个：**后台任务会在没人在看的时候改变状态**。`ui(state)`
        本来只在几条由交互触发的时刻发，而一段安静时间里一条命令跑完了，面板上还写着
        "在跑" —— 那句话是假的。

        **它不该被当成心跳。** 调用方只在"自己知道有东西悬着"时才发（TUI：`state.jobs`
        里有 `running` / `uncollected`），而且要节流：这条消息本身不贵，但**没有理由**
        的轮询会把"协议上每一条消息都有原因"这件事稀释掉。
        """
        self.send({"t": messages.IN_REFRESH_STATE})

    def shutdown(self) -> None:
        self.send({"t": messages.IN_SHUTDOWN})

    # -- 收 --------------------------------------------------------------------

    def start(self) -> None:
        """起读线程。它把每一条消息交给 `hooks`，**并在需要回应时自己回**。

        为什么要自己回（而不是把 `permission_request` 交给 `on_message` 让前端回）：
        因为"回了什么"必须是**一个**决定，而前端只该回答"选哪个"。让前端自己拼那条
        `permission_response` 就等于让它认识协议 —— 那正是这一层存在的理由。
        """
        self._reader = threading.Thread(target=self._read_loop, name="protocol-client",
                                        daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                try:
                    message = codec.decode(line)
                except Exception:
                    # 坏行跳过（和子进程那边同一条规矩）。**不静默**：这一层没法
                    # 打印（可能是 Textual 在管终端），所以交给上层 —— 用一条 notice
                    # 形状的东西，前端至少有地方显示它。
                    self.hooks.on_message({
                        "v": messages.VERSION, "t": messages.OUT_NOTICE,
                        "level": "warn", "code": "protocol",
                        "text": "[协议] 丢弃了一行读不懂的输出",
                    })
                    continue

                kind = message.get("t")
                if kind == messages.OUT_PERMISSION_REQUEST:
                    decision = self.hooks.on_permission(message)
                    # `None` = 界面会稍后自己回（见 `ClientHooks.on_permission`）。
                    # **这里绝不能用兜底值替它回** —— 那会抢在用户前面把答案发出去。
                    if decision is not None:
                        self.answer_permission(message.get("id", ""), decision)
                elif kind == messages.OUT_QUESTION_REQUEST:
                    answered = self.hooks.on_question(message)
                    if answered is not None:
                        status, text = answered
                        self.answer_question(message.get("id", ""), status, text)
                else:
                    self.hooks.on_message(message)
        finally:
            self.exit_code = self._process.wait()

    def wait(self) -> int:
        """等子进程结束，返回退出码。"""
        if self._process is None:
            return -1
        if self._reader is not None:
            self._reader.join()
        self.exit_code = self._process.wait()
        return self.exit_code

    def close(self) -> None:
        """收摊。**先请求停止，再等**，最后才强杀。

        顺序要紧：直接 kill 会让子进程死在半个回合上 —— 而 `messages` 的一致性
        只在"两步之间"成立（一条带 `tool_calls` 却没有对应结果的 assistant 消息
        会让那个会话此后每一轮都发不出去）。
        """
        if self._closed or self._process is None:
            return
        self._closed = True
        self.shutdown()
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 正常路径不会走到
            self._process.kill()

    def __enter__(self) -> "ProtocolClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _child_env() -> dict[str, str]:
    from agent_runtime.protocol.transport_stdio import child_env

    return child_env()
