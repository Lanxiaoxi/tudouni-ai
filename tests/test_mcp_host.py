"""`/mcp` 的内核那一半：**宿主（谁在跑）、两种传输、以及注册表的摘除**。

这个文件盯的四件事，按"坏了会怎样"排序：

  1. **摘干净**。卸载之后 `ToolRegistry` 里不能留下那个 server 的任何工具，而且
     `mcp__<名字>__` 这个前缀必须和 `exposed_name` 算出来的**逐字一致** —— 差一个
     下划线，卸载就会变成静默的空操作（注册表里那些工具照旧在，而面板上写着"未加载"）；
  2. **重复加载是幂等的**。连着按两下不该起两个进程，更不该在注册表上撞名（那会抛
     `Tool already exists`，而用户看到的是一个他没法理解的异常）；
  3. **失败要留痕**。`load` 失败之后 `rows()` 里那一格必须是 `failed` + 原因，而且
     `on_problem` 那条通道要说话（CLI 靠它打在 stderr 上）；
  4. **配置形状决定传输**：给 `command` 就是本地子进程，给 `url` 就是远程 HTTP ——
     不给 `transport` 这种开关（两个可以互相矛盾的地方比没有更坏）。

它和 `tests/test_mcp.py` 的分工：那边是"一个 server 本身"（协议、工具契约、审批），
这边是"会话中途改挂载"那件事。真起子进程和真起 HTTP server 的两条端到端在
`tests/test_mcp.py` / `tests/test_mcp_http.py`。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.runtime.composition import (
    MCP_FAILED,
    MCP_LOADED,
    MCP_UNLOAD,
    McpHost,
    mcp_prefix,
)
from agent_runtime.runtime.config import McpConfig
from agent_runtime.tools.mcp import (
    McpConfigError,
    McpServer,
    exposed_name,
    parse_servers,
)
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

# --- 替身：一条脚本化的通道（和 tests/test_mcp.py 的 FakeChannel 同一手法）--------


class FakeChannel:
    """按脚本回应的 MCP 通道。**加载与卸载那两条路不需要真起进程。**

    它记下 `closed`：那是"卸载真的断了连接"唯一的证据 —— 只断言"工具没了"的话，
    一个"摘了工具但把子进程留着的实现"照样能骗过测试。
    """

    def __init__(self, *, tools: list[dict[str, Any]] | None = None,
                 error: Exception | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._tools = tools if tools is not None else [self.tool("echo")]
        self._error = error

    @staticmethod
    def tool(name: str) -> dict[str, Any]:
        return {"name": name, "description": "工具",
                "inputSchema": {"type": "object"}}

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method == "initialize":
            if self._error is not None:
                raise self._error
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"}}
        if method == "tools/list":
            return {"tools": self._tools}
        if method == "tools/call":
            name = (params or {}).get("name")
            return {"content": [{"type": "text", "text": f"{name} 的结果"}]}
        raise AssertionError(f"假通道收到了它没脚本化的方法：{method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((method, dict(params or {})))

    def close(self) -> None:
        self.closed = True


class Factory:
    """一条"要造哪条通道"的记录 + 预先备好的那些通道，方便事后断言。"""

    def __init__(self, channels: list[FakeChannel] | None = None):
        self.made: list[FakeChannel] = list(channels or [])
        self.servers: list[McpServer] = []
        # 没预先备好时用它（每个 server 共用一条就够了 —— 测试只关心"造了几次"）。
        self._default = FakeChannel()

    def __call__(self, server: McpServer) -> FakeChannel:
        self.servers.append(server)
        if self.made:
            return self.made.pop(0)
        return self._default


def server(name: str = "fake", **kwargs: Any) -> McpServer:
    return McpServer(name=name, command="x", **kwargs)


def host_for(workdir, servers=(), *, channel=None, factory=None,
             tools: ToolRegistry | None = None, problems: list[str] | None = None):
    """造一个 `McpHost`，配置路径指在一个**空的临时文件**上。

    `config_path` 必须给：`_reread()` 会去读 `~/.tudouni/mcp.json`，而跑测试那台机器
    上恰好有一份的话，"加载一个不存在的 server 该回什么"这件事就取决于它了。

    目录用 `workdir` 夹具（**不是 `tmp_path`**）：后者走系统临时目录，在受限环境里
    直接 `PermissionError`（这个项目里已经踩过，见 `tests/conftest.py` 那段）。
    """
    config = workdir / "mcp.json"
    config.write_text('{"servers": {}}', encoding="utf-8")
    # `channel=X` 和 `factory=...` 是同一件事的两种写法：前者是"只有一条通道，
    # 就用它"，后者是"每个 server 各给一条"。合成一个入口是为了让看的人不用猜
    # 哪个参数优先 —— `Factory([...])` 会在用完预先备好的那些之后自己造。
    return McpHost(
        tuple(servers),
        tools if tools is not None else ToolRegistry(),
        on_problem=(problems.append if problems is not None else None),
        channel_factory=(factory or Factory([channel] if channel is not None else [])),
        config_path=config,
    )


# --- 前缀：它必须和 exposed_name 的开头逐字一致 ------------------------------


def test_the_prefix_matches_what_exposed_name_produces():
    """**差一个下划线，卸载就是空操作。**

    `load_server` 造工具名走 `exposed_name`（要净化 + 可能截断），而卸载走
    `mcp_prefix`（简单拼接）。两处写着同一个形状，所以这里钉死它们相等 ——
    否则"卸载成功了"和"注册表里那些工具照旧在"会同时成立，而没有任何东西会报错。
    """
    for name in ("github", "kb", "a-b_c"):
        assert exposed_name(name, "anything").startswith(mcp_prefix(name))
    # 前缀本身：`mcp__<名字>__`，和 tools/mcp.py 里那两个常量拼出来的一样。
    assert mcp_prefix("github") == "mcp__github__"


# --- 注册表：摘除 ------------------------------------------------------------


def test_unregister_takes_a_tool_out_and_is_idempotent():
    registry = ToolRegistry()
    tool = Tool(name="read_file", description="x", risk=RiskLevel.LOW,
                external_schema={"type": "object"}, handler=lambda args: "")
    registry.register(tool)

    assert registry.unregister("read_file") is tool
    assert registry.all() == []
    # **第二次不算错**：卸载流程重跑一次是正常的（见 ToolRegistry.unregister）。
    assert registry.unregister("read_file") is None


def test_unregister_prefix_takes_exactly_that_servers_tools():
    """按前缀摘**只**摘那一个 server 的，别的（包括另一个 server 的）不动。"""
    registry = ToolRegistry()
    for name in ("mcp__github__a", "mcp__github__b", "mcp__kb__search", "read_file"):
        registry.register(Tool(name=name, description="x", risk=RiskLevel.HIGH,
                               external_schema={"type": "object"},
                               handler=lambda args: ""))

    removed = registry.unregister_prefix(mcp_prefix("github"))

    assert sorted(tool.name for tool in removed) == ["mcp__github__a", "mcp__github__b"]
    assert sorted(tool.name for tool in registry.all()) == ["mcp__kb__search", "read_file"]
    # 摘一个不存在的前缀：空列表，不抛。
    assert registry.unregister_prefix(mcp_prefix("nope")) == []


# --- 宿主：加载 / 卸载 / 状态 ------------------------------------------------


def test_a_load_registers_the_tools_and_marks_it_loaded(workdir):
    registry = ToolRegistry()
    channel = FakeChannel(tools=[FakeChannel.tool("echo"), FakeChannel.tool("boom")])
    host = host_for(workdir, [server("fake")], channel=channel, tools=registry)

    message = host.load("fake")

    assert "挂上了：2 个工具" in message
    assert sorted(tool.name for tool in registry.all()) == [
        "mcp__fake__boom", "mcp__fake__echo"]
    row = host.rows()[0]
    assert row["state"] == MCP_LOADED
    assert row["tools"] == 2 and row["error"] == ""
    assert row["where"] == "x"
    # 握手在列工具之前（协议规定的第一步）。
    assert [method for method, _ in channel.calls] == [
        "initialize", "notifications/initialized", "tools/list"]


def test_loading_twice_does_not_start_a_second_channel(workdir):
    """幂等：`Enter` 按两下不该起两个进程、也不该在注册表上撞名。"""
    factory = Factory()
    registry = ToolRegistry()
    host = host_for(workdir, [server("fake")], factory=factory, tools=registry)

    host.load("fake")
    second = host.load("fake")

    assert "已经挂上了" in second
    assert len(factory.servers) == 1, "第二次不该再让工厂造一条通道"
    assert len(registry.all()) == len(factory._default._tools)


def test_a_failed_load_is_recorded_with_its_reason(workdir):
    problems: list[str] = []
    host = host_for(
        workdir, [server("fake")],
        channel=FakeChannel(error=RuntimeError("起不来 npx")),
        problems=problems,
    )

    message = host.load("fake")

    assert "没连上" in message and "起不来 npx" in message
    row = host.rows()[0]
    assert row["state"] == MCP_FAILED and "起不来 npx" in row["error"]
    # `on_problem` 那条通道要说话：CLI 靠它打在 stderr 上。
    assert any("没连上" in text for text in problems)


def test_a_retry_after_a_failure_clears_the_error(workdir):
    """失败的那一格再按一次就是重试 —— 成功之后 `error` 必须被清掉。

    不清的话，`_ServerState.row()` 会把一句**过期的原因**继续报给用户看，而那时候
    server 好好的。
    """

    class Flaky(Factory):
        def __init__(self):
            super().__init__()
            self.round = 0

        def __call__(self, srv):
            self.round += 1
            if self.round == 1:
                return FakeChannel(error=RuntimeError("第一次不行"))
            return FakeChannel()

    host = host_for(workdir, [server("fake")], factory=Flaky())

    host.load("fake")
    assert host.rows()[0]["state"] == MCP_FAILED
    host.load("fake")
    row = host.rows()[0]
    assert row["state"] == MCP_LOADED and row["error"] == ""


def test_unload_takes_the_tools_away_and_closes_the_channel(workdir):
    registry = ToolRegistry()
    channel = FakeChannel()
    host = host_for(workdir, [server("fake")], channel=channel, tools=registry)
    host.load("fake")

    message = host.unload("fake")

    assert "卸下了" in message and "摘掉 1 个工具" in message
    assert registry.all() == []
    assert channel.closed is True, "卸载必须真的断开（不只是从清单里划掉）"
    assert host.rows()[0]["state"] == MCP_UNLOAD
    assert host.rows()[0]["tools"] == 0


def test_unload_keeps_the_last_failure_reason(workdir):
    """**卸载不清 `error`**：上一次为什么没成，在下一次尝试之前仍然是对的事实。

    清掉它，用户再打开面板就只能看见一个光秃秃的 `unload` —— 而"我明明试过"
    这件事就没有痕迹了。
    """
    host = host_for(workdir, [server("fake")],
                    channel=FakeChannel(error=RuntimeError("端口不通")))
    host.load("fake")

    host.unload("fake")

    assert host.rows()[0]["state"] == MCP_UNLOAD
    assert "端口不通" in host.rows()[0]["error"]


def test_unloading_something_that_never_ran_is_not_an_error(workdir):
    host = host_for(workdir, [server("fake")])

    message = host.unload("fake")

    assert "本来就没在跑" in message
    assert host.rows()[0]["state"] == MCP_UNLOAD


def test_an_unknown_name_says_what_the_choices_are(workdir):
    """打错名字时**把清单一起回给用户** —— "下一步该干什么"就是看着清单重打。"""
    host = host_for(workdir, [server("github"), server("kb")])

    message = host.load("githbu")

    assert "清单里没有 server" in message
    assert "github" in message and "kb" in message


# --- 宿主：审批里那个 a 的查询口 ---------------------------------------------


def test_the_trust_group_lookup_follows_what_is_mounted(workdir):
    """`a`（信任整个 server）必须**跟着挂载状态走**，否则提示里会写一句假话。

    卸载之后那个组必须消失：不然提示里还是"以后 MCP server fake 的 N 个工具都直接
    执行"，而其中几个已经摘掉了。
    """
    host = host_for(
        workdir, [server("fake")],
        channel=FakeChannel(tools=[FakeChannel.tool("a"), FakeChannel.tool("b")]),
    )

    assert host.group("mcp__fake__a") is None, "没挂载就没有这一组"

    host.load("fake")
    pair = host.group("mcp__fake__a")
    assert pair is not None and pair[0] == "fake"
    assert pair[1] == frozenset({"mcp__fake__a", "mcp__fake__b"})

    host.unload("fake")
    assert host.group("mcp__fake__a") is None


def test_the_group_is_this_mounts_snapshot(workdir):
    """**一次挂载一份快照**：`a` 放行的是"此刻这一批"，不是"这个 server 以后随便用"。

    server 后来新加的工具不在那次快照里 —— 这正是 `TrustGroup` 那句提示里"快照"
    两个字的全部内容（和 tools/mcp.py 的 `group()` 那条同源）。
    """
    channel = FakeChannel(tools=[FakeChannel.tool("a")])
    host = host_for(workdir, [server("fake")], channel=channel)
    host.load("fake")
    pair = host.group("mcp__fake__a")
    assert pair is not None and pair[1] == frozenset({"mcp__fake__a"})


# --- 宿主：清单一格一格 ------------------------------------------------------


def test_rows_tell_the_three_states_apart(workdir):
    """`loaded` / `unload` / `failed` **必须是三行不同的话**。

    合成两种的话，用户没法区分"我没开它"和"我开了、它坏了"—— 而这两种情况下一步
    完全不同。顺带：`tools` 在非 `loaded` 时必须是 0（显示一个残留的数会被读成
    "它还在跑"）。
    """
    host = host_for(workdir, [server("ok")], channel=FakeChannel())
    host.load("ok")
    host._states["bad"] = type(host._states["ok"])(
        server("bad"), state=MCP_FAILED, error="npx 不在 PATH 上")

    rows = {row["name"]: row for row in host.rows()}

    assert rows["ok"]["state"] == MCP_LOADED and rows["ok"]["tools"] == 1
    assert rows["bad"]["state"] == MCP_FAILED and rows["bad"]["tools"] == 0
    assert rows["bad"]["error"] == "npx 不在 PATH 上"


def test_closing_the_host_closes_every_mounted_channel(workdir):
    """`close()` 得把挂着的都收掉（stdio 那些是我们起的子进程）。"""
    first, second = FakeChannel(), FakeChannel()
    factory = Factory([first, second])
    host = host_for(workdir, [server("a"), server("b")], factory=factory)
    host.load("a")
    host.load("b")

    host.close()

    assert first.closed and second.closed
    # 幂等：再收一次不炸、也不重复关。
    host.close()


# --- 宿主：重读配置（"我刚加了一个，不想重启"）--------------------------------


def test_loading_rereads_the_config_so_a_new_server_shows_up(workdir):
    """`/mcp load 新名字` 要在**不重启**的前提下认得出来。

    这条是那个功能的全部内容：用户往 `mcp.json` 里加了一行，然后在界面里打
    `/mcp load 它`。所以 `load` 先重读一次配置。
    """
    config = workdir / "mcp.json"
    config.write_text('{"servers": {"old": {"command": "x"}}}', encoding="utf-8")
    host = McpHost((server("old"),), ToolRegistry(),
                   channel_factory=Factory(), config_path=config)

    config.write_text(
        '{"servers": {"old": {"command": "x"}, "new": {"command": "y"}}}',
        encoding="utf-8",
    )
    message = host.load("new")

    assert "挂上了" in message
    assert [row["name"] for row in host.rows()] == ["old", "new"]


def test_a_broken_config_file_does_not_break_loading(workdir):
    """配置文件被写坏时：**报一句，然后照常干活**（不抛、不影响已经挂着的）。

    两条都要：装配期的 `McpConfig.from_file` 对同一个坏文件是抛 `ConfigError`（拦启动，
    因为那时候"清单是空的"和"清单坏了"必须分开）；而**运行中重读**不能抛 —— 那时候
    用户正在用这个会话，一个手滑写坏的 JSON 不该让挂载功能整个失效。

    所以这里两半都钉：坏文件下 `load` 说不出"清单里没有"以外的错，而已经挂上的那个
    **一点都没受影响**（它甚至不会被重读打扰 —— 幂等那条在重读之前就返回了）。
    """
    config = workdir / "mcp.json"
    config.write_text('{"servers": {"old": {"command": "x"}}}', encoding="utf-8")
    problems: list[str] = []
    registry = ToolRegistry()
    host = McpHost((server("old"),), registry,
                   channel_factory=Factory(), config_path=config,
                   on_problem=problems.append)
    host.load("old")

    config.write_text("{ 不是 JSON", encoding="utf-8")
    missing = host.load("new")

    assert "清单里没有 server" in missing
    assert any("重读" in text for text in problems)
    # 已经挂着的那个：状态和工具都还在（坏文件不该把它掀翻）。
    assert host.rows()[0]["state"] == MCP_LOADED
    assert [tool.name for tool in registry.all()] == ["mcp__old__echo"]
    # 幂等那条**不重读**：所以它不会再产生一句"重读出错"—— 这是"重读失败只影响
    # 新增"那句话最直接的证据。（它自己那句"已经挂上了"照旧从同一条通道出去，
    # 顺带说明这个通道同时也是"动作的后果"那条路。）
    before = sum(1 for text in problems if "重读" in text)
    assert "已经挂上了" in host.load("old")
    assert sum(1 for text in problems if "重读" in text) == before


# --- 配置形状：远程那一档 ----------------------------------------------------


def test_the_config_shape_decides_the_transport():
    """**不给 `transport` 这种开关。** 写了 http 却给了 command 该听谁的？

    "哪个字段存在"已经足够确定，所以 `is_remote` 是**推出来的**。
    """
    local, remote = parse_servers({"servers": {
        "l": {"command": "npx", "args": ["-y", "x"]},
        "r": {"url": "https://e.test/mcp", "headers": {"Authorization": "Bearer t"}},
    }})

    assert local.is_remote is False and remote.is_remote is True
    assert remote.where() == "https://e.test"      # 令牌不进那一格


@pytest.mark.parametrize("raw", [
    {"servers": {"a": {}}},
    {"servers": {"a": {"command": "x", "url": "https://e.test/mcp"}}},
])
def test_a_server_must_have_exactly_one_way_to_connect(raw):
    with pytest.raises(McpConfigError, match="恰好给出一种连接方式"):
        parse_servers(raw)


def test_the_config_reads_both_kinds_from_one_file():
    """一个文件里两种 server 并存 —— 这是这个功能落地时最该成立的一件事。"""
    config = McpConfig(parse_servers({"servers": {
        "local": {"command": "npx"},
        "remote": {"url": "https://e.test/mcp"},
    }}))

    assert [item.name for item in config.servers] == ["local", "remote"]
    assert [item.is_remote for item in config.servers] == [False, True]
