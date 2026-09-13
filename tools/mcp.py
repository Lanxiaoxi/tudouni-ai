"""外部 MCP server 接入（stdio 传输）。

**为什么是手写的同步客户端，而不是官方 `mcp` SDK。** 三条，每条都具体：

  1. **依赖。** 本项目的直接依赖只有 4 个（见 pyproject.toml），而 SDK 会拖进
     anyio / httpx-sse / pydantic-settings / jsonschema / starlette 一串。这个项目
     对每个依赖都写过理由，一次性加五个是它承担不起的形状。
  2. **同步。** Agent 循环是同步的、并行靠线程池（见 agents/agent.py），而 SDK 的
     客户端是 async 的。要接进来就得在 handler 里把 async 会话桥回同步线程 ——
     那层桥本身就是新的失败点，而且它桥的正是"进程死了/卡住了"这类最需要看得清的事。
  3. **用到的协议面很小。** initialize / notifications/initialized / tools/list /
     tools/call。resources、prompts、sampling、roots、通知这些这一版都不碰，而它们
     恰好是 SDK 存在的理由。

**信任边界（最要紧的一节，读代码前先读它）。**

装配一个 server ＝ 同意这段代码用你的权限跑起来。这件事**发生在任何审批之前**，审批
机制根本没机会参与这个决定；而且 MCP server 是另一个进程，工作区边界（FileSystem 的
safe_path）、控制面拒绝写（`.tudouni/`）、shell 的命令前缀规则，对它**一条都不成立**。

所以审批关挡不住一个恶意 server —— 它启动那一刻就能读 `.env`、往外发数据，一次工具
调用都不需要。审批真正挡的是**诚实但强大的 server 被模型误用**，尤其是被注入的内容
（网页正文、技能正文）引导：github server 的"改文件"、数据库 server 的任意 SQL，如果
全都免审批，那么"网页里写一句去调它"就是一条完整的、无人签字的利用链。这是 README
「联网工具」那条威胁模型（不可信输入 + 危险动作永远走审批）在 MCP 上的延续。

由此定下三条，都是 fail-closed：

  * 风险等级一律 **HIGH**，且**不提供任何配置去改写它**（等级是"这个 handler 有没有
    副作用"的声明，而外部工具的副作用运行时无法验证）；
  * `parallel_safe` 一律 **False**（同上；而且 HIGH 本来就过不了注册期那条校验）；
  * 放行只有一条路：**人在审批时按键**。`t` 放行这一个工具，`a` 一次性放行这个 server
    **此刻**的全部工具（见 security/asker.py 的 TrustGroup）。server 以后新加的工具
    不在那次点名里，仍然会问 —— 这正是"点名一批工具"和"放行所有 high"的区别，理由
    和 config.py 里禁止 `"high"` 写进 `auto_approve` 一字不差：**等级/成员会自己变宽，
    而写规则的人从没听说过那个新东西**。

`mcp.json` 只从**用户级**目录读（`~/.tudouni/mcp.json`，见 config.McpConfig）：
工作区里那份不读。理由是同一个 —— 工作区级配置随仓库走的那一天（.gitignore 里明确
留着这个开关），它就变成了"clone 一个仓库就自动执行任意命令"。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_runtime.process import terminate_tree
from agent_runtime.tools.tool import InvalidArgsError, RiskLevel, Tool

# 暴露给模型的工具名一律长这样：`mcp__<server>__<tool>`。
#
# 前缀不是为了好看，是为了**命名空间隔离**：ToolRegistry.register 撞名直接抛异常
# （启动就炸），而外部工具名是别人定的，和 read_file / shell 共用一个扁平空间。
# `mcp__` 这个形状和 DSH 自己的约定一致，也一眼看得出"这些不在工作区里"。
NAME_PREFIX = "mcp__"
SEPARATOR = "__"

# server 名的合法形状。它要拼进给模型看的工具名，所以这里收得比 tool 名紧：
# OpenAI 的 function name 上限是 64 字符，而 server 名占掉的那一段是先扣的。
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

# OpenAI-compatible API 对 function name 的硬限制（`^[a-zA-Z0-9_-]{1,64}$`）。
TOOL_NAME_MAX = 64

# 这一版实现的协议面。**请求旧版本、接受 server 回的任何版本**：tools/list 与
# tools/call 的载荷在这几个版本之间没有差别，而旧版本号是所有 server 都认的。
PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "agent-runtime"
CLIENT_VERSION = "0.1"

# JSON-RPC 的"参数不合法"。它和"工具执行失败"是两回事：前者模型自己改得对。
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601

DEFAULT_TIMEOUT_SECONDS = 60.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 600.0

# tools/list 分页的页数上限。没有上限的分页循环是"server 说还有下一页"就能把进程
# 挂在这里的形状 —— 而它连一条错误都报不出来。
MAX_LIST_PAGES = 50

# 关进程时的宽限时间：先关 stdin（MCP 里"我这边完事了"的正规信号），等不到再收树。
SHUTDOWN_GRACE_SECONDS = 5.0


class McpError(RuntimeError):
    """MCP 这一层出的错。落到 Agent 那里就是 status=error 的一条工具结果。"""


class McpTimeout(McpError):
    """等 server 的回应超时了。"""


class McpServerDown(McpError):
    """server 的进程没了（写不进去 / stdout 关了）。"""


class McpToolError(McpError):
    """server 说这次调用失败了（`isError: true`）—— 工具跑了，但没成功。"""


class McpConfigError(ValueError):
    """mcp.json 里的形状问题。交给 config.py 翻译成 ConfigError（"用户得先做点事"）。"""


# --- 配置形状 ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServer:
    """一个要启动的 server。**形状和 mcp.json 里的那一项一一对应**。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    # 追加/覆盖到继承来的环境变量之上（PATH 之类的必须留着，否则 npx 都找不到）。
    # 密钥走这里，而它来自用户级文件 —— 不进版本库，和 .env 的取向一致。
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


# 认识的**全部** server 键。多一个不认识的键就报错 —— 和 permissions.json、
# ToolArgs 的 extra="forbid" 是同一条：写错一个键名而它静默不生效是最坏的失败形态。
_KNOWN_SERVER_KEYS = ("command", "args", "env", "timeout_seconds")


def parse_servers(raw: Mapping[str, Any]) -> tuple[McpServer, ...]:
    """把 mcp.json 的内容读成 server 列表。

    形状：

        {"servers": {"github": {"command": "npx",
                                "args": ["-y", "@modelcontextprotocol/server-github"],
                                "env": {"GITHUB_TOKEN": "..."},
                                "timeout_seconds": 60}}}

    每一种毛病都变成一句能照着改的话（和 config.py 的 _read_json_object 同一个取向）。
    """
    unknown = [key for key in raw if key != "servers"]
    if unknown:
        raise McpConfigError(
            f"不认识的键：{', '.join(sorted(unknown))}；最外层只有 servers 一个键"
        )

    servers_raw = raw.get("servers", {})
    if not isinstance(servers_raw, Mapping):
        raise McpConfigError('"servers" 必须是一个对象：{"名字": {"command": ...}}')

    servers: list[McpServer] = []
    for name, spec in servers_raw.items():
        if not isinstance(name, str) or not SERVER_NAME_RE.match(name):
            raise McpConfigError(
                f"服务器名 {name!r} 不合法：只能用字母、数字、下划线、连字符，"
                f"长度 1~24 —— 它要拼进给模型看的工具名（{NAME_PREFIX}<名字>__<工具>）"
            )
        if not isinstance(spec, Mapping):
            raise McpConfigError(f'servers["{name}"] 必须是一个对象')

        bad_keys = [key for key in spec if key not in _KNOWN_SERVER_KEYS]
        if bad_keys:
            raise McpConfigError(
                f'servers["{name}"] 里有不认识的键：{", ".join(sorted(bad_keys))}\n'
                f'  认识的只有：{", ".join(_KNOWN_SERVER_KEYS)}'
            )

        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError(f'servers["{name}"] 缺 command（要启动的程序名）')

        args = spec.get("args", [])
        if isinstance(args, str) or not isinstance(args, (list, tuple)) or not all(
            isinstance(item, str) for item in args
        ):
            raise McpConfigError(f'servers["{name}"].args 必须是字符串数组，例如 ["-y", "包名"]')

        env = spec.get("env", {})
        if not isinstance(env, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise McpConfigError(f'servers["{name}"].env 必须是"字符串 → 字符串"的对象')

        timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        # bool 是 int 的子类，所以要单独挡一次：`true` 落在这里会变成一个 1 秒的超时。
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not (MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS)
        ):
            raise McpConfigError(
                f'servers["{name}"].timeout_seconds 必须在 '
                f"{MIN_TIMEOUT_SECONDS:g}~{MAX_TIMEOUT_SECONDS:g} 之间"
            )

        servers.append(
            McpServer(
                name=name,
                command=command.strip(),
                args=tuple(args),
                env=dict(env),
                timeout_seconds=float(timeout),
            )
        )

    return tuple(servers)


# --- 传输层：一条会阻塞的 JSON-RPC 通道 ----------------------------------


class McpChannel(Protocol):
    """一条 MCP 会话的传输。

    **判定留在内部，沟通交给注入的实现**（README 设计原则 1）：StdioChannel 走子进程，
    测试用一个脚本化的假通道 —— 于是协议那一层（握手、列工具、调工具、错误映射、
    内容渲染）不需要真的起进程就能测，而起进程那条路只剩"读写一行 JSON"。
    """

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """发一条请求并等回应；server 报错就抛（见 StdioChannel.request）。"""

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """发一条通知，不等回应。"""

    def close(self) -> None:
        """收掉这条通道（子进程之类）。可以重复调用。"""


class _Pending:
    """一条已经发出去、还在等回应的请求。"""

    __slots__ = ("event", "message", "failure")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict[str, Any] | None = None
        # 通道自己坏掉时走这一支（进程没了、关了），而不是走 message 里那条 error
        # —— 那条的语义是"server 拒绝了这个请求"，两者给模型看的话完全不同：
        # 一个该换参数，一个该去查那个 server 为什么挂了。
        self.failure: BaseException | None = None

    def deliver(self, message: dict[str, Any]) -> None:
        self.message = message
        self.event.set()

    def break_with(self, exc: BaseException) -> None:
        self.failure = exc
        self.event.set()


class StdioChannel:
    """把一个子进程的 stdin/stdout 当成 JSON-RPC 通道。

    传输格式是 MCP 规定的 **newline-delimited JSON**：一行一条消息、UTF-8、消息里
    不含裸换行。所以这里不需要 Content-Length 之类的分帧。

    **stderr 故意不接管**（`stderr=None`，直接继承我们的 stderr）。两个理由，第二个
    是硬的：不读它的管道会被写满，然后**子进程自己卡死** —— 它不知道我们不打算读；
    而 server 自己的报错正是排查时最需要看的东西。
    """

    def __init__(self, server: McpServer):
        self.server = server
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._dead: str | None = None
        self._closed = False

        argv = _resolve_argv(server)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # 见类 docstring：不接管 stderr。
                stderr=None,
                cwd=None,
                env={**os.environ, **server.env},
                text=True,
                encoding="utf-8",
                # 行缓冲：MCP 的消息是按行的，而我们写一条就要它立刻出去。
                bufsize=1,
            )
        except OSError as exc:
            raise McpError(f"起不来 `{' '.join(argv)}`：{exc}") from None

        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{server.name}", daemon=True
        )
        self._reader.start()

    # -- 发 ---------------------------------------------------------------

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """发一条请求并等回应。

        server 回了 `error` 就抛：
          * `tools/call` + code=-32602 → **InvalidArgsError**（Agent 会归成
            invalid_args：模型自己改得对，和内置工具的 pydantic 校验失败同一档）；
          * 其余 → McpError（归成 error）。
        """
        pending = _Pending()
        with self._write_lock:
            if self._dead is not None:
                raise McpServerDown(self._dead)
            self._next_id += 1
            request_id = self._next_id
            with self._lock:
                self._pending[request_id] = pending
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})

        # **等待在锁外面**：占着写锁等人回话，会让读线程连"进程死了"都发不出来。
        if not pending.event.wait(self.server.timeout_seconds):
            with self._lock:
                self._pending.pop(request_id, None)
            raise McpTimeout(
                f"等 {method} 超过 {self.server.timeout_seconds:g} 秒没有回应"
            )

        # 通道自己坏了（进程没了）优先于"server 拒绝了这个请求"：两者给模型看的话
        # 完全不同 —— 一个该去查那个 server 为什么挂了，一个该换参数。
        if pending.failure is not None:
            raise pending.failure

        message = pending.message or {}
        if "error" in message:
            error = message.get("error") or {}
            code = error.get("code")
            text = str(error.get("message") or "（server 没有给出说明）")
            if method == "tools/call" and code == INVALID_PARAMS:
                raise InvalidArgsError(text)
            raise McpError(f"server 拒绝了 {method}：{text}（code={code}）")

        return message.get("result")

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        with self._write_lock:
            if self._dead is not None:
                raise McpServerDown(self._dead)
            self._send({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def _send(self, message: Mapping[str, Any]) -> None:
        """写一条消息。**调用方负责持写锁**（两条消息交错写进去就是两条坏 JSON）。"""
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._dead = f"往 server `{self.server.name}` 写数据失败：{exc}"
            raise McpServerDown(self._dead) from None

    # -- 收 ---------------------------------------------------------------

    def _read_loop(self) -> None:
        """读线程：把每一行派发给等它的人。

        它**是唯一读 stdout 的地方**，而且只在 EOF 时退出 —— 退出即代表这个 server
        不能再用了，所以要把所有还在等的人叫醒（否则他们会各自等满超时，用户看到的
        是一串"超时"而不是一句"进程没了"）。
        """
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # server 往 stdout 打了非协议的东西（有些实现会混日志）。这不是
                    # 我们能修的事，也不能因此丢掉整条通道 —— 说一句，继续读。
                    print(
                        f"[MCP] server `{self.server.name}` 的 stdout 上有一行不是 JSON，已跳过",
                        file=sys.stderr,
                    )
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            # 收摊时调用方可能正在关这个管道，而迭代一个被关掉的文件对象抛的就是
            # 这两个。那不是"server 出问题了"，只是我们自己关的 —— 不报。
            pass
        finally:
            code = self._proc.poll()
            reason = (
                f"MCP server `{self.server.name}` 的 stdout 关了（进程退出码 {code}）"
                if code is not None
                else f"MCP server `{self.server.name}` 的 stdout 关了"
            )
            self._dead = reason
            self._fail_all(McpServerDown, reason)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            # server → client 的请求/通知（sampling、roots、list_changed…）。这一版
            # 一个都不支持：通知直接忽略，请求回一条"没有这个方法"——**不能让 server
            # 那边干等**，它的超时行为我们管不着。
            if message.get("id") is not None:
                try:
                    with self._write_lock:
                        self._send({
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": METHOD_NOT_FOUND, "message": "这个客户端不支持该方法"},
                        })
                except McpError:
                    pass
            return

        request_id = message.get("id")
        if request_id is None:
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            # 迟到的回应：它对应的请求已经超时、没人等了。丢掉 —— 按 id 找回来的
            # 唯一意义就是"这条是给谁的"，没人在等就没有意义。
            return
        pending.deliver(message)

    def _fail_all(self, exc_type: type[McpError], message: str) -> None:
        """把还在等的请求全部叫醒。

        **每个 waiter 给一个新异常对象**：同一个实例从两个线程同时 raise 会把
        traceback 互相盖掉，那种日志没人看得懂。
        """
        with self._lock:
            waiting, self._pending = self._pending, {}
        for pending in waiting.values():
            pending.break_with(exc_type(message))

    # -- 收摊 -------------------------------------------------------------

    def close(self) -> None:
        """关掉通道。可以重复调用（main.py 的 finally 和异常路径都会叫它）。

        顺序是刻意的：

          1. **先关 stdin** —— 这是 MCP 里"我这边完事了"的正规信号，规矩的 server
             会自己收尾（放掉资源、退出）。这一步同时让读线程能自然结束：进程一退出，
             stdout 就是 EOF，那个 `for line` 循环自己就停了。
          2. 等不上就**强杀进程树**。等在这里的价值不只是"收干净"：读线程只在进程真的
             退出之后才可能结束，而"关掉一个读线程正阻塞在上面的管道"在 Windows 上是
             会挂住或抛到那个线程里的。
          3. 只有确认它已经不在了，才去关 stdout。收不掉的（taskkill 都失败）就**不关**
             —— 那一下可能把读线程或这次退出流程挂住，而"留一个句柄到进程结束"是更小的
             代价。但要大声说出来。
        """
        if self._closed:
            return
        self._closed = True

        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass

        if not self._exited(SHUTDOWN_GRACE_SECONDS):
            _terminate_tree(self._proc)

        if self._exited(SHUTDOWN_GRACE_SECONDS):
            try:
                if self._proc.stdout is not None:
                    self._proc.stdout.close()
            except OSError:
                pass
        else:
            print(
                f"[MCP] server `{self.server.name}` 没能收掉（进程仍在），"
                f"它可能继续占着管道",
                file=sys.stderr,
            )

        self._dead = self._dead or f"MCP server `{self.server.name}` 已关闭"
        self._fail_all(McpServerDown, self._dead)

    def _exited(self, timeout: float) -> bool:
        """等它退出；到点还没退就返回 False（**只有这里会 wait 那个进程**）。"""
        try:
            self._proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False


def _resolve_argv(server: McpServer) -> list[str]:
    """把配置里的 command 解析成一个真正能启动的路径。

    Windows 上这一步不是可选的：`npx` 实际是 `npx.CMD`，而 CreateProcess 不会替你
    补 `.CMD` 这个后缀（它只补 `.exe`），于是 `["npx", ...]` 直接 FileNotFoundError
    ——而 `npx -y <包名>` 恰好是 MCP 世界最主流的一条配置。`shutil.which` 按 PATHEXT
    找全，找得到就用全路径（含空格的名字也因此不用额外加引号）。
    """
    resolved = shutil.which(server.command)
    return [resolved or server.command, *server.args]


def _terminate_tree(proc: subprocess.Popen) -> None:
    """强杀，而且**连子进程树一起收**。

    实现搬去了 `agent_runtime/process.py` —— 后台命令（`tools/builtin/jobs.py`）要的是
    同一件事：Windows 上 `npx` 会再起一个 node、PowerShell 会再起一个子 shell，只杀
    直接子进程留下的是孤儿，而它们还占着管道。**第四处要出现同一段 taskkill 时，它就该
    只有一个来源**（tools/text.py 的 truncate 是同一个手法）。

    这里留的是**门面**：名字没变（`close()` 直接 import 它，测试也是），而 MCP 这条
    Popen 没带 `start_new_session`，所以 POSIX 那侧 process.py 会认出"它和我们同组"、
    老老实实退回 `proc.kill()` —— 行为与搬家之前逐字节一致。
    """
    terminate_tree(proc)


# --- 协议层：一个 server 之上的会话 --------------------------------------


@dataclass(frozen=True, slots=True)
class McpTool:
    """server 报的一个工具定义。**name / description / schema 原样留着。**"""

    name: str
    description: str
    schema: Mapping[str, Any]


class McpConnection:
    """一个 server 之上的协议会话：握手、列工具、调工具。"""

    def __init__(self, server: McpServer, channel: McpChannel):
        self.server = server
        self.channel = channel
        self.server_info: Mapping[str, Any] = {}
        # server 声明它支持什么（tools / resources / prompts …）。只在它**明确声明了
        # 没有 tools** 时用来把话说清楚，见 McpToolset.connect。
        self.capabilities: Mapping[str, Any] = {}
        self.protocol_version = PROTOCOL_VERSION

    def open(self) -> None:
        """握手。MCP 规定的第一步，而且必须在 tools/list 之前。"""
        result = self.channel.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # 一个能力都不声明：sampling / roots 都是"server 反过来要求客户端"，
                # 而我们这一版没有能应答它们的东西（见 StdioChannel._dispatch）。
                # 声明了却答不上来，比不声明坏得多。
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        # 通知，不等回应 —— 协议这么定的（initialize 的回应之后必须发它）。
        self.channel.notify("notifications/initialized", {})

        if isinstance(result, Mapping):
            self.server_info = result.get("serverInfo") or {}
            capabilities = result.get("capabilities")
            self.capabilities = capabilities if isinstance(capabilities, Mapping) else {}
            version = result.get("protocolVersion")
            # **接受 server 回的版本，不因为它和请求的不一样就退出。** 我们只用
            # tools/list 与 tools/call，这两个在这几个版本之间没有差别；而按版本号
            # 拒绝一个实际能用的 server，是纯粹的可用性损失。版本记下来只是为了
            # 报错时能说清对面是谁。
            if isinstance(version, str) and version:
                self.protocol_version = version

    def list_tools(self) -> list[McpTool]:
        """列工具，跟着 nextCursor 翻页。"""
        found: list[McpTool] = []
        cursor: str | None = None

        for _ in range(MAX_LIST_PAGES):
            result = self.channel.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, Mapping):
                raise McpError("tools/list 的回应该是一个对象")

            for raw in result.get("tools") or []:
                if not isinstance(raw, Mapping):
                    continue
                name = raw.get("name")
                if not isinstance(name, str) or not name:
                    continue
                description = raw.get("description")
                schema = raw.get("inputSchema")
                found.append(
                    McpTool(
                        name=name,
                        description=description if isinstance(description, str) else "",
                        # 没有 inputSchema 的 server 不合规，但"少一个键"不该让整个
                        # server 不可用：给一个最宽松的空对象 schema。
                        schema=dict(schema) if isinstance(schema, Mapping) else {"type": "object"},
                    )
                )

            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return found
            cursor = next_cursor

        raise McpError(f"tools/list 翻了 {MAX_LIST_PAGES} 页还没到底，放弃")

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> str:
        """调一次工具，把结果渲染成给模型看的文本。

        `tool_name` 必须是 **server 给的原名**（不是 `mcp__…` 那个净化过的暴露名）。
        """
        result = self.channel.request(
            "tools/call", {"name": tool_name, "arguments": dict(arguments)}
        )
        if not isinstance(result, Mapping):
            raise McpError("tools/call 的回应该是一个对象")

        text = render_content(result.get("content"), result.get("structuredContent"))
        if result.get("isError"):
            # 工具跑了、但失败了。这不是协议错误，而是"这次调用的结果" —— 所以它
            # 带着 server 自己的说明（通常正是模型改参数需要的依据）。
            raise McpToolError(text or f"{tool_name} 执行失败（server 没有给出说明）")
        return text


def render_content(content: Any, structured: Any = None) -> str:
    """把 MCP 的内容块渲染成一段文本。

    **本运行时的工具结果只有文本**（见 agents/agent.py：字符串、ToolResult 或可
    JSON 序列化的对象）。所以图片、音频、二进制资源这一版都只能留一句占位 ——
    这是决定，不是欠账：把它悄悄丢掉会让模型以为自己"看过"那张图。
    """
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "image":
                parts.append(f"[图片 {block.get('mimeType') or ''}：本运行时的工具结果只支持文本，已省略]")
            elif kind == "audio":
                parts.append(f"[音频 {block.get('mimeType') or ''}：本运行时的工具结果只支持文本，已省略]")
            elif kind == "resource":
                resource = block.get("resource")
                resource = resource if isinstance(resource, Mapping) else {}
                if isinstance(resource.get("text"), str):
                    parts.append(resource["text"])
                else:
                    parts.append(f"[资源 {resource.get('uri') or '?'}：二进制内容已省略]")
            else:
                parts.append(f"[{kind or '未知'} 内容块已省略]")

    text = "\n".join(part for part in parts if part)

    if not text and structured is not None:
        # 2025-06-18 起 server 可以只回结构化内容。有它就当正文用，总比回一句
        # "（空）"、让模型以为工具什么都没干要好。
        text = json.dumps(structured, ensure_ascii=False, indent=2)

    return text or "（server 没有返回任何内容）"


# --- 装配层：连 server、把工具装进注册表 ---------------------------------


def exposed_name(server_name: str, tool_name: str) -> str:
    """算出给模型看的工具名：`mcp__<server>__<净化过的 tool 名>`。

    **净化只发生在我们这一侧**：调用 server 时用的仍然是它原来的名字（见
    `_call_handler`）。理由：OpenAI 的 function name 只接受 `[A-Za-z0-9_-]`，而 MCP
    的 tool 名不受这个约束（`foo.bar`、`foo/bar` 都合法）。模型必须看到一个 schema
    和 name 都合法的工具定义，而 server 必须收到它自己认得的名字。
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    name = f"{NAME_PREFIX}{server_name}{SEPARATOR}{safe}"
    if len(name) <= TOOL_NAME_MAX:
        return name

    # 超长就截断，**不丢掉这个工具**：丢掉是静默的能力缺失，而截断后名字仍然唯一
    # （尾巴挂一小段原名的哈希）。真实 server 里超长名字并不罕见。
    digest = hashlib.sha1(tool_name.encode("utf-8")).hexdigest()[:6]
    head = TOOL_NAME_MAX - len(NAME_PREFIX) - len(server_name) - len(SEPARATOR) - len(digest) - 1
    return f"{NAME_PREFIX}{server_name}{SEPARATOR}{safe[:head]}_{digest}"


def _call_handler(connection: McpConnection, original_name: str) -> Callable[[Mapping[str, Any]], str]:
    """把一次工具调用接到 server 上。

    闭包里存的是 **server 给的原名**。它是 Tool.handler 的"外部工具调用约定"：
    整个参数对象一次传进来，而不是展开成关键字参数（见 tools/tool.py）。
    """
    def handler(arguments: Mapping[str, Any]) -> str:
        return connection.call(original_name, arguments)

    return handler


@dataclass(frozen=True, slots=True)
class McpToolset:
    """已连上的一组 server，以及它们提供的工具。

    **由 main.py 持有并负责关闭**（它管的是进程生命周期，而 create_tool_registry 是
    一个纯装配函数、不碰 I/O）。
    """

    tools: tuple[Tool, ...] = ()
    # 暴露名 → (server 名, 这个 server 此刻的全部暴露名)。审批里那个 a 用它。
    groups: Mapping[str, tuple[str, frozenset[str]]] = field(default_factory=dict)
    # server 名 → 连上的工具数。启动时报告用。
    counts: Mapping[str, int] = field(default_factory=dict)
    connections: tuple[McpConnection, ...] = ()

    def group(self, tool_name: str) -> tuple[str, frozenset[str]] | None:
        """这个工具所属的那一组（目前＝同一个 server 的全部工具）。

        **它是一次连接时的快照。** server 以后新加的工具不在里面，所以"信任整个
        server"按一次之后，新工具仍然会问 —— 这正是它和"放行所有 high"的区别
        （见模块 docstring 最后那一段）。
        """
        return self.groups.get(tool_name)

    def close(self) -> None:
        for connection in self.connections:
            try:
                connection.channel.close()
            except Exception as exc:  # 收摊失败不该盖住"任务本身"的结果
                print(
                    f"[MCP] 关闭 server `{connection.server.name}` 时出错："
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    @classmethod
    def connect(
        cls,
        servers: Sequence[McpServer],
        *,
        channel_factory: Callable[[McpServer], McpChannel] = StdioChannel,
        on_problem: Callable[[str], None] | None = None,
    ) -> "McpToolset":
        """逐个连 server、列工具、造 Tool。**一个 server 起不来不拦启动。**

        和"缺 TAVILY_API_KEY 只是少一个 web_search"同一条：外部 server 是可选能力，
        它坏了不该让整个运行时起不来。但**绝不能不说** —— 每一条失败都从 on_problem
        出去（main.py 把它打到 stderr 上）。
        """
        tools: list[Tool] = []
        groups: dict[str, tuple[str, frozenset[str]]] = {}
        counts: dict[str, int] = {}
        connections: list[McpConnection] = []

        for server in servers:
            channel: McpChannel | None = None
            try:
                channel = channel_factory(server)
                connection = McpConnection(server, channel)
                connection.open()
                # 合规的 server 会在 capabilities 里声明它支持什么。**只在它明确声明了
                # 却没有 tools 时才跳过**（而不是"没声明就不试"）：一个只提供 resources
                # 的 server 是合法的，而"它没有工具"该说成一句话，不该让 tools/list 返回
                # 一个 -32601 让人去猜；反过来，没声明 capabilities 的 server 现实里存在，
                # 它们照样能答 tools/list，按声明卡死它们纯属可用性损失。
                if connection.capabilities and "tools" not in connection.capabilities:
                    _report(
                        on_problem,
                        f"[MCP] server `{server.name}` 的 capabilities 里没有 tools，"
                        f"它不提供任何工具（已连上，工具数为 0）",
                    )
                    connections.append(connection)
                    counts[server.name] = 0
                    continue
                listed = connection.list_tools()
            except Exception as exc:
                _report(
                    on_problem,
                    f"[MCP] server `{server.name}` 没连上（{type(exc).__name__}: {exc}）；"
                    f"它提供的工具这次都不可用",
                )
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass
                continue

            connections.append(connection)
            servers_tools: list[Tool] = []
            taken: set[str] = set()

            for item in listed:
                name = exposed_name(server.name, item.name)
                if name in taken:
                    # 两个不同的原名净化后撞在一起（`foo.bar` 和 `foo_bar` 都变
                    # `foo_bar`）。注册表撞名会直接抛异常，而这里要的是"能连上就尽量
                    # 都用上，但撞了的那一个必须说出来"。
                    _report(
                        on_problem,
                        f"[MCP] server `{server.name}` 的 `{item.name}` 暴露名和另一个工具"
                        f"撞了（{name}），这一个这次装不上",
                    )
                    continue
                taken.add(name)
                servers_tools.append(
                    Tool(
                        name=name,
                        description=f"[外部服务 {server.name}] {item.description or item.name}",
                        # 外部工具的副作用**运行时无法验证**，所以一律 HIGH，而且没有
                        # 任何配置能改写它：等级是声明，不是旋钮（见模块 docstring）。
                        risk=RiskLevel.HIGH,
                        external_schema=item.schema,
                        handler=_call_handler(connection, item.name),
                    )
                )

            names = frozenset(tool.name for tool in servers_tools)
            for tool in servers_tools:
                groups[tool.name] = (server.name, names)
            tools.extend(servers_tools)
            counts[server.name] = len(servers_tools)

        return cls(
            tools=tuple(tools),
            groups=groups,
            counts=counts,
            connections=tuple(connections),
        )


def _report(on_problem: Callable[[str], None] | None, message: str) -> None:
    """报一条问题。**on_problem 为 None 时不静默吞掉，而是打到 stderr。**

    收口在这里的理由：调用方（main.py）注入的报法必须和别处一致（stderr，见
    report_permissions 那几条），而在测试里换成一个收集列表就能断言"这条说出来了"。
    """
    if on_problem is None:
        print(message, file=sys.stderr)
        return
    on_problem(message)
