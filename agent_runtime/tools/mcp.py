"""外部 MCP server 接入（stdio 与 Streamable HTTP 两种传输）。

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

挂载一个 server ＝ 同意这段代码用你的权限跑起来。这件事**发生在任何审批之前**，审批
机制根本没机会参与这个决定；而且 MCP server 是另一个进程，工作区边界（FileSystem 的
safe_path）、控制面拒绝写（`.tudouni/`）、shell 的命令前缀规则，对它**一条都不成立**。
"什么时候挂"因此只有一条路：**人在 `/mcp` 里按一下**（`runtime/composition.py` 的
`McpHost`），**启动时一个都不自动挂** —— 读一下配置文件就等于授权是这一整块最不能
出现的行为。

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

**两种传输，一份配置形状。** 一个 server 要么给 `command`（本地，stdio：我们起一个
子进程，见 `StdioChannel`），要么给 `url`（远程，Streamable HTTP：我们连一个地址，
见 `HttpChannel`）—— **恰好给一个**。这条和 `Tool` 里"schema 来源恰好一个"是同一条
手法（见 tools/tool.py 的 `__post_init__`）：两个都给就会让"到底连的是哪个"变成一个
看运气的问题。

为什么值得在 docstring 里单列一节：这两种 server 的**信任形态不一样**。stdio 是
"配置里那行 `command` 是一段要执行的代码"——它在本机、用你的权限跑；HTTP 是"工作区
里的数据会被发给一个别人在跑的服务，而凭据在 `headers` 里"。加一条远程 server 因此
不是在"加一个工具"，而是在**给模型开一条出口**。但它们对模型的暴露面完全一样
（`mcp__` 前缀、风险一律 HIGH、每次都要按键），所以从审批往上的每一层都不需要知道
对面是哪种 —— 那是这一层的事，不是 Agent 的事。

`mcp.json` 只从**用户级**目录读（`~/.tudouni/mcp.json`，见 config.McpConfig）：
工作区里那份不读。理由是同一个 —— 工作区级配置随仓库走的那一天（.gitignore 里明确
留着这个开关），它就变成了"clone 一个仓库就自动执行任意命令"（对 `url` 那一档则是
"clone 一个仓库就把你的数据往外发"）。
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
from urllib.parse import urlparse

import httpx

from agent_runtime import i18n
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
    """一个 server 的配置。**形状和 mcp.json 里的那一项一一对应**。

    两种传输各有各的那几个字段，而**恰好一组**（见 `is_remote`）：

      * 本地（stdio）：`command` / `args` / `env` —— 我们要起一个子进程，
        `env` 是**追加**到继承来的环境变量之上的（PATH 之类的必须留着，否则
        `npx` 自己都找不到）；
      * 远程（Streamable HTTP）：`url` / `headers` —— 我们连一个地址，
        `headers` 就是凭据（`Authorization: Bearer …`）和任何 server 要的头。

    两组字段都留在这个 dataclass 上（而不是拆成两个类）：对**上面每一层**来说它们
    是同一个东西（一个"要挂载的 MCP server"），拆开只会让 `McpHost`、
    `McpToolset.counts`、`/mcp` 面板各写两遍分支，而它们根本不关心对面是哪种。
    """

    name: str
    # -- 本地（stdio）--
    command: str = ""
    args: tuple[str, ...] = ()
    # 密钥走这里，而它来自用户级文件 —— 不进版本库，和 .env 的取向一致。
    env: Mapping[str, str] = field(default_factory=dict)
    # -- 远程（Streamable HTTP）--
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    # -- 两种都有 --
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def is_remote(self) -> bool:
        """连远程地址还是起本地子进程。`parse_servers` 保证两者恰好有一个。"""
        return bool(self.url)

    def where(self) -> str:
        """给人看的一句话：这个 server 在哪。

        **远程只写出处，不带路径和查询串**：URL 里可能有令牌（`?key=…`），而这句话
        会进面板、进通知、进 `--audit` 那种给人看的地方。所以这里只留
        `scheme://host[:port]`。
        """
        if not self.url:
            return " ".join((self.command, *self.args))
        parts = urlparse(self.url)
        host = parts.netloc or parts.path
        return f"{parts.scheme}://{host}" if parts.scheme else self.url


# 认识的**全部** server 键。多一个不认识的键就报错 —— 和 permissions.json、
# ToolArgs 的 extra="forbid" 是同一条：写错一个键名而它静默不生效是最坏的失败形态。
_KNOWN_SERVER_KEYS = ("command", "args", "env", "url", "headers", "timeout_seconds")

# 只认这两种 scheme：`http://` 和 `https://`。写别的（`ftp://`、漏了 scheme 的
# `example.com/mcp`）在**解析期**就报出来，而不是等连接的时候抛一个
# `UnsupportedProtocol` —— 那时候用户看到的是一句 python 异常，而这条配置错误
# 该说的话是"这里要写完整地址"。
_URL_SCHEMES = ("http", "https")


def parse_servers(raw: Mapping[str, Any]) -> tuple[McpServer, ...]:
    """把 mcp.json 的内容读成 server 列表。

    形状（两种传输各一例）：

        {"servers": {
            "github": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-github"],
                       "env": {"GITHUB_TOKEN": "..."},
                       "timeout_seconds": 60},
            "remote": {"url": "https://example.com/mcp",
                       "headers": {"Authorization": "Bearer ..."}}
        }}

    每一种毛病都变成一句能照着改的话（和 config.py 的 _read_json_object 同一个取向）。
    """
    unknown = [key for key in raw if key != "servers"]
    if unknown:
        raise McpConfigError(i18n.t("mcp.cfg.unknown_top_keys",
                                    names=", ".join(sorted(unknown))))

    servers_raw = raw.get("servers", {})
    if not isinstance(servers_raw, Mapping):
        raise McpConfigError(i18n.t("mcp.cfg.servers_not_object"))

    servers: list[McpServer] = []
    for name, spec in servers_raw.items():
        if not isinstance(name, str) or not SERVER_NAME_RE.match(name):
            raise McpConfigError(i18n.t("mcp.cfg.bad_name", name=repr(name),
                                        prefix=NAME_PREFIX))
        if not isinstance(spec, Mapping):
            raise McpConfigError(i18n.t("mcp.cfg.server_not_object", name=name))

        bad_keys = [key for key in spec if key not in _KNOWN_SERVER_KEYS]
        if bad_keys:
            raise McpConfigError(i18n.t(
                "mcp.cfg.unknown_server_keys", name=name,
                names=", ".join(sorted(bad_keys)),
                known=", ".join(_KNOWN_SERVER_KEYS)))

        command = spec.get("command", "")
        if command and not isinstance(command, str):
            raise McpConfigError(i18n.t("mcp.cfg.command_not_string", name=name))
        command = (command or "").strip()

        url = spec.get("url", "")
        if url and not isinstance(url, str):
            raise McpConfigError(i18n.t("mcp.cfg.url_not_string", name=name))
        url = (url or "").strip()

        # **恰好给一个**：两个都给是"到底连哪个"看运气，两个都没给是"连什么都不知道"。
        # 前者比后者更坏（它看起来是配好的），所以两种都当场说清。
        if bool(command) == bool(url):
            given = i18n.t("mcp.cfg.both_given" if command
                           else "mcp.cfg.neither_given")
            raise McpConfigError(i18n.t("mcp.cfg.both_or_neither", name=name,
                                        given=given))

        if url:
            scheme = urlparse(url).scheme.lower()
            if scheme not in _URL_SCHEMES:
                raise McpConfigError(i18n.t(
                    "mcp.cfg.bad_url", name=name,
                    scheme=scheme or i18n.t("mcp.cfg.empty_scheme"), url=repr(url)))
            for local_only in ("args", "env"):
                if local_only in spec:
                    raise McpConfigError(i18n.t(
                        "mcp.cfg.local_only_key", name=name, key=local_only))

        args = spec.get("args", [])
        if isinstance(args, str) or not isinstance(args, (list, tuple)) or not all(
            isinstance(item, str) for item in args
        ):
            raise McpConfigError(i18n.t("mcp.cfg.args_not_list", name=name))

        env = spec.get("env", {})
        if not isinstance(env, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise McpConfigError(i18n.t("mcp.cfg.env_not_map", name=name))

        headers = spec.get("headers", {})
        if not isinstance(headers, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise McpConfigError(i18n.t("mcp.cfg.headers_not_map", name=name))
        # 头名必须是**可发送的 ASCII**：httpx 会在真要发的时候抛一句
        # `LocalProtocolError`，而那时候错误信息里没有"是哪个 server"。
        for header in headers:
            if not header.isascii() or not header.strip():
                raise McpConfigError(i18n.t("mcp.cfg.bad_header_name", name=name,
                                            header=repr(header)))

        timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        # bool 是 int 的子类，所以要单独挡一次：`true` 落在这里会变成一个 1 秒的超时。
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not (MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS)
        ):
            raise McpConfigError(i18n.t(
                "mcp.cfg.bad_timeout", name=name,
                low=f"{MIN_TIMEOUT_SECONDS:g}", high=f"{MAX_TIMEOUT_SECONDS:g}"))

        servers.append(
            McpServer(
                name=name,
                command=command,
                args=tuple(args),
                env=dict(env),
                url=url,
                headers=dict(headers),
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
            raise McpError(i18n.t("mcp.spawn_failed", command=" ".join(argv),
                                  problem=exc)) from None

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
                i18n.t("mcp.timeout", method=method,
                       seconds=f"{self.server.timeout_seconds:g}")
            )

        # 通道自己坏了（进程没了）优先于"server 拒绝了这个请求"：两者给模型看的话
        # 完全不同 —— 一个该去查那个 server 为什么挂了，一个该换参数。
        if pending.failure is not None:
            raise pending.failure

        # 回应的判定和 HTTP 那条路**共用一份**（见 `_result_or_raise`）：分头写的话，
        # "哪种错算 invalid_args"会在两条路上漂。
        return _result_or_raise(
            pending.message or {}, method, f"server `{self.server.name}`",
        )

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
            self._dead = i18n.t("mcp.write_failed", name=self.server.name,
                                problem=exc)
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
                        i18n.t("mcp.stdout.not_json", name=self.server.name),
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
                i18n.t("mcp.stdout.closed_with_code", name=self.server.name,
                       code=code)
                if code is not None
                else i18n.t("mcp.stdout.closed", name=self.server.name)
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
                i18n.t("mcp.close_failed", name=self.server.name),
                file=sys.stderr,
            )

        self._dead = self._dead or i18n.t("mcp.closed", name=self.server.name)
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


# --- 传输层之二：Streamable HTTP ------------------------------------------

# 远程 server（Streamable HTTP）的路径。**和 stdio 那条是同一份信任模型的两种形态**：
# 都由用户级配置在装配时决定，都对模型露出 `mcp__` 前缀的 HIGH 风险工具。
#
# **它是"我们这一版唯一支持的远程传输"** —— 2024-11-05 那版规范里的 HTTP+SSE
# （两个端点、先 GET 拿 endpoint）不带会话、现在也基本被 Streamable HTTP 取代了，
# 所以不实现、不猜：连不上就说连不上（见 HttpChannel）。
MCP_PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
MCP_SESSION_ID_HEADER = "Mcp-Session-Id"

# 一次远程请求的响应体上限。**没有这一条就没有上限**：对面可以一直吐，而我们在
# 内存里一直攒 —— 那是一个"它说还有就永远读下去"的形状，和 `MAX_LIST_PAGES` 防的
# 是同一类东西（那里防的是分页循环，这里防的是单条响应）。
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024


class HttpChannel:
    """连一个远程 MCP server（Streamable HTTP），把 JSON-RPC 走 POST。

    规范里这个传输的一句话是"server 作为一个独立进程运行、可以服务多个客户端
    连接"，客户端连的是一个 URL。所以和 stdio 有两个关键差别，也正是这个类存在的
    理由：

      * **没有要收的子进程。** `close()` 只是"我不再问了"，对面的服务是别人的，
        我们既不起它也不关它；
      * **每一条消息一次 HTTP 请求**，所以没有读线程、没有写锁、没有待回应表 ——
        httpx 的一次请求本来就是同步的，回包就在这一层拿到。stdio 那条之所以复杂，
        全是因为"一个管道上多条消息要自己分派"。

    ## 回应体：两种都要认

    规范允许 server 对一条请求回 `Content-Type: application/json`（一个 JSON 对象）
    或 `text/event-stream`（SSE：若干 `data:` 行，最后才是那条回应）。**两种都认**：
    只认 JSON 的话，正好是"功能更全的 server"（会发进度通知的那些）连不上 —— 而
    报出来的话会是"回的不是 JSON"，指向完全错误的方向。

    ## 会话与版本

    握手时 server 可能回一个 `Mcp-Session-Id`，之后每条请求都要带上它（不带的话
    规范说 server 该回 400）。协议版本那条也一样：握手谈定之后，之后每条请求都带
    `MCP-Protocol-Version`。

    ## 认证

    `headers` 原样带上（`Authorization: Bearer …` 就是这么写的）。**它不进审计、
    不进工具描述**：`McpHost.rows()` 给的 `where` 只有 `scheme://host`，而这里的
    `str(exc)` 里也不会出现它（httpx 的异常里带的是 URL，不含头）。
    """

    def __init__(self, server: McpServer, client: httpx.Client | None = None):
        self.server = server
        self._session_id: str | None = None
        self._version: str | None = None
        self._closed = False
        self._next_id = 0
        self._lock = threading.Lock()
        # 客户端由外面注入（`runtime/composition.py` 传进程级那一个，连接复用）；
        # 测试可以传一个指向本地假 server 的。**自己造的那个要自己收**，见 close()。
        self._own_client = client is None
        self._client = client or httpx.Client(
            # trust_env=False：和联网抓取那条一致 —— 环境变量里的代理不该悄悄改掉
            # 这个程序往哪发数据。要代理就显式构造一个 client 传进来。
            trust_env=False,
            follow_redirects=False,
            timeout=server.timeout_seconds,
        )

    # -- 发 ---------------------------------------------------------------

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        with self._lock:
            if self._closed:
                raise McpServerDown(i18n.t("mcp.closed", name=self.server.name))
            self._next_id += 1
            request_id = self._next_id
        message = {
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": dict(params or {}),
        }
        response = self._post(message)
        payload = _decode_body(response, self.server)
        result = _result_or_raise(payload, method, f"server `{self.server.name}`")
        # **握手谈定的版本要记下来。** 规范要求之后每条请求都带
        # `MCP-Protocol-Version`；而这个字段只在 `initialize` 的回应里，通道自己
        # 记一份最直接 —— `McpConnection.open()` 也记，但那是**协议层**的账，
        # 而发请求的是传输层（它读不到对面那个对象）。实测踩过：只让协议层记的话，
        # 版本头一次都没发出去，而这件事在"本地假 server 什么都收"的环境里无声无息。
        if method == "initialize" and isinstance(result, Mapping):
            version = result.get("protocolVersion")
            if isinstance(version, str) and version:
                self._version = version
        return result

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            if self._closed:
                raise McpServerDown(i18n.t("mcp.closed", name=self.server.name))
        # 通知**没有 id、也没有回应**：规范说 server 收到就回 202 无正文。
        # 所以这里不看正文、也不等结果 —— 只确认它收下了。
        self._post({"jsonrpc": "2.0", "method": method, "params": dict(params or {})})

    def _post(self, message: Mapping[str, Any]) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            # 两种回应都要收（见类 docstring）。
            "Accept": "application/json, text/event-stream",
            **self.server.headers,
        }
        if self._session_id:
            headers[MCP_SESSION_ID_HEADER] = self._session_id
        if self._version:
            headers[MCP_PROTOCOL_VERSION_HEADER] = self._version

        try:
            response = self._client.post(
                self.server.url, json=dict(message), headers=headers,
            )
        except httpx.ConnectTimeout:
            # **连接超时归"连不上"，不归"等回应超时"。** 两者的下一步完全不同：
            # 一个该去查地址/网络/对面是不是活着，一个该去查对面为什么处理得慢。
            # 而 `httpx.ConnectTimeout` 两个基类都继承（`TimeoutException` +
            # `ConnectError`），所以顺序在这里是语义 —— 放错一支，一句"等 initialize
            # 超过 2 秒没有回应"会指向完全错误的方向（实测踩到过：端口没人听）。
            raise McpError(
                i18n.t("mcp.http.connect_timeout", name=self.server.name,
                       problem=self.server.where())
            ) from None
        except httpx.TimeoutException:
            raise McpTimeout(
                i18n.t("mcp.http.timeout", method=message.get("method"),
                       seconds=f"{self.server.timeout_seconds:g}")
            ) from None
        except httpx.HTTPError as exc:
            # 连不上/DNS/TLS/读中断都在这一支。**不把 URL 原样带出去**（可能有令牌
            # 或内网地址），但 server 名要有 —— 用户配置里就是按名字认的。
            raise McpError(
                i18n.t("mcp.http.connect_failed", name=self.server.name,
                       problem=f"{type(exc).__name__}: {exc}")
            ) from None

        # 会话 id 只在握手那一条上出现。**认它，但不因为它缺席就退出** ——
        # 无状态 server 是合法的。
        session_id = response.headers.get(MCP_SESSION_ID_HEADER)
        if session_id:
            self._session_id = session_id

        if response.status_code >= 400:
            raise McpError(
                i18n.t("mcp.http.status", name=self.server.name,
                       status=response.status_code,
                       body=_shorten(_body_text(response)))
            )
        return response

    # -- 收摊 -------------------------------------------------------------

    def close(self) -> None:
        """**只是"我不再问了"**：对面的服务不是我们起的，所以这里不关它。

        规范说客户端不再需要这个会话时 SHOULD 发一条 `DELETE`（带会话 id）——
        那是"告诉对面可以回收了"，不是"关掉服务"。它失败也无所谓（405 = 对面
        不支持，规范允许），所以这里吞掉异常：收摊失败不该盖住任务本身的结果
        （和 `McpToolset.close()` 同一条）。
        """
        if self._closed:
            return
        self._closed = True

        if self._session_id:
            try:
                self._client.delete(
                    self.server.url,
                    headers={
                        MCP_SESSION_ID_HEADER: self._session_id,
                        **self.server.headers,
                    },
                )
            except httpx.HTTPError:
                pass

        if self._own_client:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - 收摊失败不往上抛
                pass


def _body_text(response: httpx.Response) -> str:
    """回应的正文，**带上限**（见 `MAX_HTTP_BODY_BYTES`）。

    `iter_bytes` 逐块读、够了就停：`response.text` 会把整个正文拉进内存，
    而对面是谁我们并不知道。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_HTTP_BODY_BYTES:
            raise McpError(
                i18n.t("mcp.http.body_too_big",
                       mb=MAX_HTTP_BODY_BYTES // (1024 * 1024))
            )
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _decode_body(response: httpx.Response, server: McpServer) -> Mapping[str, Any]:
    """把回应体解成那一个 JSON-RPC 对象（两种 Content-Type 都认）。

    ## 一个我们有意的取舍：SSE 那一支一次只认最后一条

    真要完整支持 SSE，得在一条流的中间接上"server 主动发来的请求/通知"（sampling、
    roots、`list_changed`）—— 而这一版**一个都不支持**（见 `StdioChannel._dispatch`
    里的同一段话）。所以这里取的是**那条回应**，其余的行按"不认识就跳过"处理。
    换句话说：一个只回 `application/json` 的 server 完全可用，一个会发通知的 server
    也能用（通知被忽略），但**要我们应答 server 的请求时做不到** —— 那和 stdio 那条
    路的限制一模一样，不是这个传输独有的。
    """
    body = _body_text(response).strip()
    if not body:
        # 202 无正文（通知的正常回应）。调用方（notify）不看这个值。
        return {}

    content_type = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        payload: Mapping[str, Any] | None = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                candidate = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, Mapping) and ("result" in candidate or "error" in candidate):
                payload = candidate
        if payload is None:
            raise McpError(
                i18n.t("mcp.http.no_sse_response", name=server.name)
            )
        return payload

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        raise McpError(
            i18n.t("mcp.http.not_json", name=server.name, body=_shorten(body))
        ) from None
    if not isinstance(decoded, Mapping):
        raise McpError(i18n.t("mcp.http.not_object", name=server.name))
    return decoded


def _result_or_raise(
    message: Mapping[str, Any], method: str, who: str,
) -> Any:
    """一条 JSON-RPC 回应 → 结果或异常。**两种传输共用这一份判断。**

    stdio 和 HTTP 分头写一份的话，"哪种错算 invalid_args"就会在两条路上漂 ——
    而那正是模型能不能自己改对参数的分界线（见 `McpChannel.request` 的说明）。
    """
    if "error" in message:
        error = message.get("error") or {}
        code = error.get("code")
        text = str(error.get("message") or i18n.t("mcp.result.no_message"))
        if method == "tools/call" and code == INVALID_PARAMS:
            raise InvalidArgsError(text)
        raise McpError(i18n.t("mcp.result.refused", who=who, method=method,
                              text=text, code=code))
    return message.get("result")


def _shorten(text: str, limit: int = 200) -> str:
    """一句给日志/报错用的短文本。超了就截断 —— 对面回一页 HTML 时不该把它全打进
    stderr。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
                raise McpError(i18n.t("mcp.list_tools.not_object"))

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

        raise McpError(i18n.t("mcp.list_tools.too_many_pages",
                              pages=MAX_LIST_PAGES))

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> str:
        """调一次工具，把结果渲染成给模型看的文本。

        `tool_name` 必须是 **server 给的原名**（不是 `mcp__…` 那个净化过的暴露名）。
        """
        result = self.channel.request(
            "tools/call", {"name": tool_name, "arguments": dict(arguments)}
        )
        if not isinstance(result, Mapping):
            raise McpError(i18n.t("mcp.call.not_object"))

        text = render_content(result.get("content"), result.get("structuredContent"))
        if result.get("isError"):
            # 工具跑了、但失败了。这不是协议错误，而是"这次调用的结果" —— 所以它
            # 带着 server 自己的说明（通常正是模型改参数需要的依据）。
            raise McpToolError(text or i18n.t("mcp.call.failed", name=tool_name))
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
                    i18n.t("mcp.close_error", name=connection.server.name,
                           problem=f"{type(exc).__name__}: {exc}"),
                    file=sys.stderr,
                )

    @classmethod
    def connect(
        cls,
        servers: Sequence[McpServer],
        *,
        channel_factory: Callable[[McpServer], McpChannel] | None = None,
        on_problem: Callable[[str], None] | None = None,
    ) -> "McpToolset":
        """逐个连 server、列工具、造 Tool。**一个 server 起不来不拦启动。**

        和"缺 TAVILY_API_KEY 只是少一个 web_search"同一条：外部 server 是可选能力，
        它坏了不该让整个运行时起不来。但**绝不能不说** —— 每一条失败都从 on_problem
        出去（main.py 把它打到 stderr 上）。

        **每个 server 那一段住在 `load_server()` 里**（模块级的那个函数），因为
        `/mcp load` 要的是同一件事：连一个、列工具、造 Tool。两处各写一遍的话，
        "撞名怎么办""capabilities 里没有 tools 怎么算"这类判断就会漂 —— 而它们
        正是"配了却不生效"的三种形态。
        """
        factory = channel_factory or default_channel
        tools: list[Tool] = []
        groups: dict[str, tuple[str, frozenset[str]]] = {}
        counts: dict[str, int] = {}
        connections: list[McpConnection] = []

        for server in servers:
            outcome = load_server(server, factory, on_problem=on_problem)
            if outcome.connection is not None:
                connections.append(outcome.connection)
            if outcome.error:
                # 没连上：不进 counts（"没连上"和"连上了但 0 个工具"是两件事，
                # 而 counts 只能表达后者）。理由写在 load_server 的 docstring 里。
                continue
            tools.extend(outcome.tools)
            groups.update(outcome.groups)
            counts[server.name] = len(outcome.tools)

        return cls(
            tools=tuple(tools),
            groups=groups,
            counts=counts,
            connections=tuple(connections),
        )


@dataclass(frozen=True, slots=True)
class _Loaded:
    """`load_server()` 的结果：连上的那一半，或者失败的那一句话。

    **`error` 和 `connection` 是两条独立的轴**，这是有意的：

      * `connection is not None and not error` —— 连上了（哪怕一个工具都没有）；
      * `error` 非空 —— 没连上，`connection` 那半是 None（通道已经在这儿关掉了）。

    那为什么不用一个 `None` 表示失败、再另开一个"错误文本"的字典？因为调用方要的
    正是这两样东西，而把它们塞进两个平行的容器里，总有一处会忘掉其中一个。
    """

    connection: McpConnection | None = None
    tools: tuple[Tool, ...] = ()
    groups: Mapping[str, tuple[str, frozenset[str]]] = field(default_factory=dict)
    error: str = ""


def load_server(
    server: McpServer,
    channel_factory: Callable[[McpServer], McpChannel],
    *,
    on_problem: Callable[[str], None] | None = None,
) -> _Loaded:
    """连**一个** server：起通道 → 握手 → 列工具 → 造 Tool。见 `_Loaded`。

    **它不注册任何东西**（不知道注册表存在）：调用方拿到 `tools` 自己装。这条分界
    让这个函数能被 `McpToolset.connect`（装配期，装进一个全新的注册表）和
    `runtime.composition.McpHost.load`（运行中，装进那个已经在用的注册表）共用。

    失败**不抛**，而是把那句话放进 `_Loaded.error`：
      * 装配期要的是"一个 server 坏了不拦启动"（`connect` 那条）；
      * 运行中要的是"`/mcp load x` 失败了，面板那一行写清为什么"。

    两种情况的共同点是不该有异常往上飞 —— 而"哪个 server 出错了"只有这里有。
    """
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
                i18n.t("mcp.no_tools_capability", name=server.name),
            )
            return _Loaded(connection=connection)
        listed = connection.list_tools()
    except Exception as exc:
        _report(
            on_problem,
            i18n.t("mcp.load_failed", name=server.name,
                   problem=f"{type(exc).__name__}: {exc}"),
        )
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        return _Loaded(error=f"{type(exc).__name__}: {exc}")

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
                i18n.t("mcp.name_clash", name=server.name, tool=item.name,
                       other=name),
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
    groups = {tool.name: (server.name, names) for tool in servers_tools}
    return _Loaded(
        connection=connection, tools=tuple(servers_tools), groups=groups,
    )


def default_channel(server: McpServer) -> McpChannel:
    """按配置形状挑传输：给了 `url` 就连远程，否则起本地子进程。

    **这是装配处的默认值，不是配置里的一个开关。** 让用户写 `"transport": "http"`
    就等于给了两个可以互相矛盾的地方（写了 http 却给了 command 该听谁的），而
    "哪个字段存在"本来就已经足够确定 —— `parse_servers` 保证两者恰好有一个。
    """
    return HttpChannel(server) if server.is_remote else StdioChannel(server)


def _report(on_problem: Callable[[str], None] | None, message: str) -> None:
    """报一条问题。**on_problem 为 None 时不静默吞掉，而是打到 stderr。**

    收口在这里的理由：调用方（main.py）注入的报法必须和别处一致（stderr，见
    report_permissions 那几条），而在测试里换成一个收集列表就能断言"这条说出来了"。
    """
    if on_problem is None:
        print(message, file=sys.stderr)
        return
    on_problem(message)
