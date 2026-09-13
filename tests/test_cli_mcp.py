"""CLI 那一侧的 `/mcp`：**行式支路**（列清单 / 挂一个 / 卸一个）。

TUI 那一侧是面板（见 `tests/test_tui_commands.py`），而这一侧只有行 —— 两个前端
共享的是**数据口径**（`runtime.mcp.rows()` 和宿主拼的那几句话），不共享交互。这条
分工在这个文件里有两处直接证据：

  * 这里断言的输出里，"哪几个在跑"和"为什么没连上"都不是 CLI 拼的，而是宿主给的；
  * `/mcp load <名字>` 走的是 `host.load()` —— 和 TUI 面板里那一下**同一个方法**。

所以这里用一个只有 `.mcp` 的哑 runtime（`_print_mcp` 只读这一格）：那既是"这个函数
不认识 Runtime 的其余部分"的证明，也免掉了起一个真 runtime 的代价。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from typing import Any

import pytest

from agent_runtime.frontends.cli import _handle_slash_command, _print_mcp
from agent_runtime.runtime.composition import MCP_FAILED, MCP_LOADED, McpHost
from agent_runtime.tools.mcp import McpServer
from agent_runtime.tools.tool import ToolRegistry


class FakeChannel:
    """一条按脚本回应的通道（和 `tests/test_mcp_host.py` 同一手法）。"""

    def __init__(self, *, tools: int = 2, error: Exception | None = None):
        self.tools = tools
        self.error = error
        self.closed = False

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "initialize":
            if self.error is not None:
                raise self.error
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"}}
        if method == "tools/list":
            return {"tools": [{"name": f"t{index}", "description": "x",
                               "inputSchema": {"type": "object"}}
                              for index in range(self.tools)]}
        raise AssertionError(method)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class StubRuntime:
    """/mcp` 那一支只用得上 `.mcp` —— 那就只给这一格。"""

    def __init__(self, host: McpHost | None):
        self.mcp = host


def host_for(workdir, servers, channel: FakeChannel) -> McpHost:
    config = workdir / "mcp.json"
    config.write_text('{"servers": {}}', encoding="utf-8")
    return McpHost(tuple(servers), ToolRegistry(),
                   channel_factory=lambda _server: channel, config_path=config)


def run(argv: list[str], host: McpHost | None) -> str:
    """跑一条 `/mcp`，把 stderr 收回来当文本。

    **stderr 是这一支的出口**：`--list` / `/tools` / `/status` 都打在 stderr 上
    （stdout 留给"干净的答案"，见 `frontends/cli/__init__.py` 顶上那段），所以这里
    收的是它而不是 stdout。
    """
    sink = io.StringIO()
    with redirect_stderr(sink):
        assert _handle_slash_command(StubRuntime(host), " ".join(argv)) is True
    return sink.getvalue()


def test_no_servers_configured_points_at_the_file(workdir):
    text = run(["/mcp"], host_for(workdir, [], FakeChannel()))

    assert "没有配置任何 MCP server" in text
    assert "mcp.json" in text
    # 两种配法都要说（本地给 command、远程给 url）—— 那是这一屏唯一能教人的地方。
    assert "command" in text and "url" in text


def test_the_listing_spells_out_each_state(workdir):
    """三种状态三种字：在跑的（带工具数）、没跑的、试过没成的（带原因）。

    这正是 CLI 那份清单和 TUI 那份**共用同一个来源**的地方（`host.rows()`）。
    """
    host = host_for(workdir, [McpServer(name="ok", command="x"),
                              McpServer(name="off", command="x"),
                              McpServer(name="bad", command="x")],
                    FakeChannel(tools=3))
    host.load("ok")
    host.load("bad")                      # 先让它成功，再换成失败那一档
    host._states["bad"].state = MCP_FAILED
    host._states["bad"].error = "McpError: 起不来 npx"
    host._states["bad"].tools = 0

    text = run(["/mcp"], host)

    assert "1 个在跑 / 共 3 个" in text
    assert "● ok" in text and "3 个工具" in text
    assert "○ off" in text and "未加载" in text
    assert "✗ bad" in text and "没连上" in text and "起不来 npx" in text
    # 改的写法也要说清（否则这一屏只是"知道了但动不了"）。
    assert "/mcp load" in text and "/mcp unload" in text


def test_load_prints_the_hosts_own_sentence_and_lists_again(workdir):
    """`/mcp load x` = 真改 + 把宿主那句话原样打出来 + 再列一遍。

    **那句话不许 CLI 自己拼**：它含"为什么没成"这类只有宿主知道的事实，而两个前端
    各拼一份就会漂（TUI 贴的是同一句）。
    """
    host = host_for(workdir, [McpServer(name="fake", command="x")], FakeChannel(tools=2))
    try:
        text = run(["/mcp", "load", "fake"], host)
        assert "[MCP] server `fake` 挂上了：2 个工具" in text
        # 再列一遍：挂完之后"工具数是多少"是紧接着会想知道的。
        assert "● fake" in text and "2 个工具" in text
    finally:
        host.close()


def test_unload_is_idempotent_and_says_so(workdir):
    host = host_for(workdir, [McpServer(name="fake", command="x")], FakeChannel())
    try:
        assert "本来就没在跑" in run(["/mcp", "unload", "fake"], host)
        run(["/mcp", "load", "fake"], host)
        assert "卸下了" in run(["/mcp", "unload", "fake"], host)
    finally:
        host.close()


def test_a_bad_argument_does_not_guess(workdir):
    """`/mcp 全部打开` / `/mcp load`（缺名字）：**说认不出来，然后列清单**。

    不猜是有意的：挂上一个别的 server 的后果是"它开始用外面的东西"，比"没挂上"贵
    得多（和 `/model` 打错名字不就近匹配同一条）。
    """
    host = host_for(workdir, [McpServer(name="fake", command="x")], FakeChannel())
    try:
        for argv in (["/mcp", "全部打开"], ["/mcp", "load"], ["/mcp", "load", "nope"]):
            text = run(argv, host)
            assert "认不出这个写法" in text or "清单里没有 server" in text
        # 一个都没被挂上。
        assert host.loaded_names() == []
    finally:
        host.close()


def test_the_cli_speaks_the_same_states_the_host_reports(workdir):
    """清单里那一格和宿主说的是同一件事（**不是 CLI 自己的判断**）。

    这条钉的是分工：`state` / `error` / `where` 全部由宿主算好，CLI 只负责排版。
    所以这里直接拿 `rows()` 和打印出来的文本对一遍。
    """
    host = host_for(workdir, [McpServer(name="fake", command="x")], FakeChannel(tools=1))
    try:
        row = host.rows()[0]
        assert row["state"] != MCP_LOADED
        text = run(["/mcp"], host)
        assert json.dumps(row["name"])[1:-1] in text
    finally:
        host.close()
