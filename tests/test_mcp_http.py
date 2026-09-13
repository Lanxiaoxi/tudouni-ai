"""远程 MCP server（Streamable HTTP）：**真起一个本地 HTTP server**。

为什么非要真起：`HttpChannel` 里每一行代码都在处理"对面真的回了什么"—— 两种
`Content-Type`、会话 id 在那一个响应头上、HTTP 状态码的错误映射、`DELETE` 收摊。
用 mock 顶替 httpx 的 `post`，测出来的只是"我们调了 post"，而那些头、那些状态码、
那段 SSE 分帧一个都没被验过 —— 和 `tests/fake_mcp_server.py` 那条真子进程同一个理由。

**零新依赖**：假 server 用标准库的 `http.server`（正是"这一版只做 stdio + HTTP"
那条取舍的收益 —— HTTP 那边我们只需要 httpx，而它已经在了）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_runtime.runtime.composition import MCP_FAILED, MCP_LOADED, McpHost
from agent_runtime.tools.mcp import HttpChannel, McpError, McpServer, parse_servers
from agent_runtime.tools.tool import ToolRegistry

TOOLS = [
    {"name": "echo", "description": "把参数原样回给你",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "boom", "description": "总是失败", "inputSchema": {"type": "object"}},
]


class Handler(BaseHTTPRequestHandler):
    """一个最小的 Streamable HTTP MCP server。

    它同时是**这一版实现的验收清单**：initialize / notifications/initialized /
    tools/list / tools/call，外加会话 id、协议版本头、DELETE 收摊。
    """

    protocol_version = "HTTP/1.1"
    # 由 `make_server()` 灌进去。
    session_id: str | None = None
    sse: bool = False
    seen_headers: list[dict[str, str]] = []
    methods: list[str] = []

    # -- 工具 ---------------------------------------------------------------

    def log_message(self, *args: Any) -> None:  # 别往 stderr 刷访问日志
        pass

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _respond(self, payload: dict[str, Any] | None, *,
                 status: int = 200, sse: bool | None = None,
                 extra_headers: dict[str, str] | None = None) -> None:
        use_sse = self.sse if sse is None else sse
        headers = dict(extra_headers or {})
        if payload is None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        header = "text/event-stream" if use_sse else "application/json"
        # **SSE 那一支要把事件包成 `data: <json>`**，而且流里可以先有别的行
        # （通知之类）—— 这里刻意先塞一条无关的行，钉住"认最后那条回应"。
        if use_sse:
            body = (
                'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", header)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 ---------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - 标准库的命名
        Handler.methods.append("POST")
        Handler.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
        message = self._body()
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            self._respond(
                {"jsonrpc": "2.0", "id": request_id,
                 "result": {"protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-http", "version": "1"}}},
                extra_headers=(
                    {"Mcp-Session-Id": self.session_id} if self.session_id else {}
                ),
            )
            return

        if method == "notifications/initialized":
            # 通知：**没有 id，也没有正文**（规范说 202）。
            self._respond(None, status=202)
            return

        if method == "tools/list":
            self._respond({"jsonrpc": "2.0", "id": request_id,
                           "result": {"tools": TOOLS}})
            return

        if method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") == "boom":
                self._respond({"jsonrpc": "2.0", "id": request_id,
                               "result": {"isError": True, "content": [
                                   {"type": "text", "text": "炸了：这个工具总是失败"}]}})
                return
            self._respond({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(params.get("arguments") or {},
                                                ensure_ascii=False)}]}})
            return

        if method == "tools/bad_args":
            self._respond({"jsonrpc": "2.0", "id": request_id,
                           "error": {"code": -32602, "message": "缺少必填参数 text"}})
            return

        self._respond({"jsonrpc": "2.0", "id": request_id,
                       "error": {"code": -32601, "message": f"没有 {method} 这个方法"}})

    def do_DELETE(self) -> None:  # noqa: N802
        Handler.methods.append("DELETE")
        self._respond(None, status=204)

    def do_GET(self) -> None:  # noqa: N802
        Handler.methods.append("GET")
        self._respond(None, status=405)


def make_server(**attrs: Any) -> tuple[ThreadingHTTPServer, str]:
    cls = type("BoundHandler", (Handler,), dict(attrs))
    server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/mcp"


@pytest.fixture
def http_server():
    """一个真的 HTTP MCP server，用完就关。"""
    Handler.seen_headers = []
    Handler.methods = []
    servers: list[ThreadingHTTPServer] = []

    def start(**attrs: Any) -> str:
        server, url = make_server(**attrs)
        servers.append(server)
        return url

    try:
        yield start
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def channel_for(url: str, **kwargs: Any) -> HttpChannel:
    return HttpChannel(McpServer(name="remote", url=url, **kwargs))


# --- 通道那一层 --------------------------------------------------------------


def test_a_remote_channel_handshakes_lists_and_calls(http_server):
    """握手 → 列工具 → 调工具，一次走通（回应是 `application/json`）。"""
    url = http_server()
    channel = channel_for(url)
    try:
        from agent_runtime.tools.mcp import McpConnection

        connection = McpConnection(McpServer(name="remote", url=url), channel)
        connection.open()
        # **server 回的版本被接受**（不因为它和请求的不一样就退出）。
        assert connection.protocol_version == "2025-06-18"

        listed = connection.list_tools()
        assert [item.name for item in listed] == ["echo", "boom"]
        assert listed[0].schema["required"] == ["text"]

        assert json.loads(connection.call("echo", {"text": "你好"})) == {"text": "你好"}
    finally:
        channel.close()


def test_sse_responses_are_understood(http_server):
    """`text/event-stream` 那条路：取流里**那一条回应**，其余的行跳过。

    只认 `application/json` 的话，正好是"功能更全的 server"连不上 —— 而报出来的话
    会是"回的不是 JSON"，指向完全错误的方向。
    """
    url = http_server(sse=True)
    channel = channel_for(url)
    try:
        from agent_runtime.tools.mcp import McpConnection

        connection = McpConnection(McpServer(name="remote", url=url), channel)
        connection.open()
        assert [item.name for item in connection.list_tools()] == ["echo", "boom"]
    finally:
        channel.close()


def test_the_session_id_and_version_ride_on_every_request(http_server):
    """会话 id 和谈定的协议版本**必须**出现在之后的每一条请求上。

    不带会话 id 时规范说 server 该回 400 —— 那会让"握手之后所有调用都失败"变成
    一句看不懂的 HTTP 错误。
    """
    url = http_server(session_id="s-123")
    channel = channel_for(url)
    try:
        from agent_runtime.tools.mcp import McpConnection

        McpConnection(McpServer(name="remote", url=url), channel).open()
        channel.request("tools/list", {})
    finally:
        channel.close()

    # 第一条（initialize）没有会话 id，之后每一条都有；版本头从第二条起（那是
    # 握手谈定之后的请求，而 `notifications/initialized` 就是第一条）。
    handshake = Handler.seen_headers[0]
    later = Handler.seen_headers[-1]
    assert "mcp-session-id" not in handshake
    assert later["mcp-session-id"] == "s-123"
    assert later["mcp-protocol-version"] == "2025-06-18"
    # 两种回应都要收（规范要求客户端两个都列）。
    assert "text/event-stream" in later["accept"]


def test_the_configured_headers_are_sent(http_server):
    """凭据走 `headers`，原样带上（`Authorization: Bearer …` 就是这么写的）。"""
    url = http_server()
    channel = channel_for(url, headers={"Authorization": "Bearer secret"})
    try:
        from agent_runtime.tools.mcp import McpConnection

        McpConnection(McpServer(name="remote", url=url,
                                headers={"Authorization": "Bearer secret"}),
                      channel).open()
    finally:
        channel.close()

    assert Handler.seen_headers[0]["authorization"] == "Bearer secret"


def test_closing_a_remote_channel_tells_the_server_it_is_done(http_server):
    """收摊发一条 `DELETE`（带会话 id）—— **不是关掉对面的服务**（它是别人的）。"""
    url = http_server(session_id="s-9")
    channel = channel_for(url)
    from agent_runtime.tools.mcp import McpConnection

    McpConnection(McpServer(name="remote", url=url), channel).open()
    channel.close()

    assert "DELETE" in Handler.methods


def test_a_server_that_is_not_listening_is_a_readable_error():
    """连不上（端口没人听）：一条人话，而且**带 server 名**（用户是按名字认它的）。

    它还必须说"连不上"而不是"超时"：`httpx.ConnectTimeout` 同时是
    `TimeoutException` 和 `ConnectError`，所以分错支就会报一句"等 initialize 超过
    N 秒没有回应" —— 而那句话把人指向"对面慢"，真正的事实是"对面不在"。
    """
    channel = channel_for("http://127.0.0.1:1/mcp", timeout_seconds=2.0)
    try:
        with pytest.raises(McpError, match="连不上 server `remote`"):
            channel.request("initialize", {})
    finally:
        channel.close()


def test_a_handshake_that_errors_is_reported_not_raised(http_server):
    """对面回 `error` 时是一条 McpError，而加载那一层把它记成 `failed`（不是崩）。"""
    url = http_server()
    channel = channel_for(url)
    try:
        with pytest.raises(McpError, match="没有 什么 这个方法"):
            channel.request("什么", {})
    finally:
        channel.close()


# --- 装配那一层：一个远程 server 走完整条路 ----------------------------------


def test_a_remote_server_mounts_like_a_local_one(workdir, http_server):
    """**验收**：配一个 `url` 就能当 server 用 —— 工具挂上、调得动、卸得掉。

    这条是"远程 URL + token 那一档"落地的全部内容：宿主那一层完全不知道对面是
    HTTP 还是子进程（`rows()`、`load`、`unload`、`group` 用的是同一段代码）。
    """
    url = http_server()
    config = workdir / "mcp.json"
    config.write_text('{"servers": {}}', encoding="utf-8")
    registry = ToolRegistry()
    host = McpHost(
        parse_servers({"servers": {"remote": {"url": url}}}),
        registry, config_path=config,
    )
    try:
        message = host.load("remote")
        assert "挂上了：2 个工具" in message
        row = host.rows()[0]
        assert row["state"] == MCP_LOADED and row["tools"] == 2
        # 给人看的那一句里**没有令牌**（这里是本地地址，但它只到 host:port）。
        assert row["where"].startswith("http://127.0.0.1")

        # 真的调一次（工具名是暴露名，走的是 server 给的原名）。
        echo = registry.get("mcp__remote__echo")
        assert json.loads(echo.execute({"text": "hi"})) == {"text": "hi"}
        # 审批里那个 a：这个 server 的两个工具同属一组。
        pair = host.group("mcp__remote__echo")
        assert pair is not None and pair[0] == "remote"

        assert "卸下了" in host.unload("remote")
        assert registry.all() == []
        assert host.rows()[0]["state"] != MCP_LOADED
    finally:
        host.close()


def test_a_remote_server_that_is_down_shows_up_as_failed(workdir):
    """连不上时那一格是 `failed` + 原因，而不是"未加载"。"""
    config = workdir / "mcp.json"
    config.write_text('{"servers": {}}', encoding="utf-8")
    host = McpHost(
        parse_servers({"servers": {
            "down": {"url": "http://127.0.0.1:1/mcp", "timeout_seconds": 2}}}),
        ToolRegistry(), config_path=config,
    )
    try:
        host.load("down")
        row = host.rows()[0]
        assert row["state"] == MCP_FAILED
        # **"连不上"而不是"超时"**：这两种情况的下一步完全不同，而 `httpx` 的
        # `ConnectTimeout` 两个基类都继承 —— 分错支就会报一句指向错误方向的话。
        assert "连不上" in row["error"]
        # 失败之后按一次就是重试（这里仍然是同一条 URL，所以还是失败）。
        assert "没连上" in host.load("down")
    finally:
        host.close()


def test_the_token_never_reaches_the_display_name(http_server):
    """URL 里带凭据时，给人看的那句话**不带它**（它会进面板、进日志、进快照）。

    `where()` 只留 `scheme://host`：查询串里放令牌是常见写法，而这一格是面板、
    左栏和 CLI 都在读的。凭据真正要出现的地方只有一个 —— `headers`。
    """
    url = http_server()
    server = parse_servers({"servers": {
        "r": {"url": f"{url}?key=SECRET", "headers": {"Authorization": "Bearer T"}},
    }})[0]

    assert "SECRET" not in server.where()
    assert "Bearer" not in server.where()
    # 但它仍然是那个能连的地址（`where()` 只影响显示，不影响请求）。
    assert server.url.endswith("?key=SECRET")
    assert server.headers == {"Authorization": "Bearer T"}
