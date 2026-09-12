"""外部 MCP 接入：契约、协议、装配、审批，以及真起一个子进程的那条端到端。

这个文件盯的几件事，按"坏了会怎样"排序：

  1. **schema 与名字必须原样透传/可还原。** 模型看到的 schema 是外部 server 给的，
     我们一个字节都不改（改了就失真）；而调 server 时必须用**它给的原名**，不是
     净化后的暴露名（用错了就是 "unknown tool"）。
  2. **外部工具一律 HIGH、默认每条都要审批。** 这是唯一的边界，退化成"装配即放行"
     就等于把"装配一个 server"偷偷升级成"授权模型自主使用它的每一个工具"。
  3. **一个 server 起不来只该少一批工具**，不该让整个运行时起不来；但必须说出来。
  4. **a 键放行的是一份快照**，不是"这个 server 以后随便用"。
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.agents import Agent
from agent_runtime.config import ConfigError, McpConfig
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import ApprovalMemory, PermissionPolicy, TrustGroup
from agent_runtime.security.asker import cli_asker
from agent_runtime.tools.mcp import (
    McpConfigError,
    McpError,
    McpServer,
    McpTimeout,
    McpToolError,
    McpToolset,
    StdioChannel,
    exposed_name,
    parse_servers,
    render_content,
)
from agent_runtime.tools.tool import (
    InvalidArgsError,
    RiskLevel,
    Tool,
    ToolRegistry,
)
from agent_runtime.state import Session

from fakes import Collector, ScriptedModel, tool_call

TESTS_DIR = Path(__file__).resolve().parent
FAKE_SERVER = TESTS_DIR / "fake_mcp_server.py"


# --- 替身：一条脚本化的通道 -------------------------------------------------


class FakeChannel:
    """按脚本回应的 MCP 通道（手写，不用 mock 库 —— 和 tests/fakes.py 同一条）。

    协议那一层要断言的是"发了什么、收到什么之后怎么变"，那件事不需要真起进程；
    真起进程那条路由文件末尾的端到端测试管。
    """

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        results: dict[str, Any] | None = None,
        start_error: Exception | None = None,
        protocol_version: str = "2024-11-05",
        pages: list[list[dict[str, Any]]] | None = None,
        capabilities: dict[str, Any] | None = None,
    ):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[str] = []
        self.closed = False
        self._tools = tools if tools is not None else [self.tool("echo")]
        self._pages = pages
        self._results = results or {}
        self._start_error = start_error
        self._protocol_version = protocol_version
        self._capabilities = {"tools": {}} if capabilities is None else capabilities

    @staticmethod
    def tool(name: str, description: str = "工具", schema: dict | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": schema or {"type": "object", "properties": {}},
        }

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))

        if method == "initialize":
            if self._start_error is not None:
                raise self._start_error
            return {
                "protocolVersion": self._protocol_version,
                "capabilities": self._capabilities,
                "serverInfo": {"name": "fake", "version": "1"},
            }

        if method == "tools/list":
            if self._pages is not None:
                index = 0 if not (params or {}).get("cursor") else 1
                page = self._pages[index]
                payload: dict[str, Any] = {"tools": page}
                if index + 1 < len(self._pages):
                    payload["nextCursor"] = f"page-{index + 2}"
                return payload
            return {"tools": self._tools}

        if method == "tools/call":
            name = (params or {}).get("name")
            if name in self._results:
                outcome = self._results[name]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return {"content": [{"type": "text", "text": f"{name} 的结果"}]}

        raise AssertionError(f"假通道收到了没人预期的请求：{method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications.append(method)

    def close(self) -> None:
        self.closed = True

    # 断言用的便捷读法

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def params_of(self, method: str) -> dict[str, Any]:
        return next(params for name, params in self.calls if name == method)


def connect_one(
    channel: FakeChannel,
    *,
    name: str = "fake",
    problems: list[str] | None = None,
) -> McpToolset:
    """连一个假 server，返回 toolset。problems 收集那些"说出来了但不拦启动"的话。"""
    seen = problems if problems is not None else []
    return McpToolset.connect(
        [McpServer(name=name, command="fake")],
        channel_factory=lambda server: channel,
        on_problem=seen.append,
    )


# --- 工具契约：schema 与调用约定 -------------------------------------------


def test_external_schema_is_passed_through_verbatim():
    """外部工具的 schema 一个字节都不改。

    它是模型唯一的依据，而"翻译成 pydantic 模型"会在 $ref / anyOf / 非法标识符上
    静默失真 —— 失真等于让模型照着一份不存在的契约去调用。
    """
    schema = {
        "type": "object",
        "properties": {
            "foo-bar": {"type": "string"},
            "nested": {"$ref": "#/$defs/x"},
        },
        "anyOf": [{"required": ["foo-bar"]}],
        "$defs": {"x": {"type": "integer"}},
        "additionalProperties": False,
    }
    channel = FakeChannel(tools=[FakeChannel.tool("weird", schema=schema)])
    toolset = connect_one(channel)
    tool = toolset.tools[0]

    assert tool.parameters == schema
    assert tool.to_openai_schema()["function"]["parameters"] == schema
    # 也确认它不是"恰好长得像"：字典是相等而不是同一个对象。
    assert tool.parameters is not schema


def test_external_tool_arguments_go_through_as_a_mapping_not_kwargs():
    """参数整个传进来，**不展开成关键字参数**。

    展开要求每个名字都是合法标识符，而外部 schema 里 `foo-bar` 完全合法 —— 展开就是
    凭空给 JSON Schema 加了一条它没写的约束。
    """
    channel = FakeChannel()
    toolset = connect_one(channel)
    tool = toolset.tools[0]

    tool.execute({"foo-bar": 1, "baz": [2]})

    assert channel.params_of("tools/call") == {
        "name": "echo",
        "arguments": {"foo-bar": 1, "baz": [2]},
    }


def test_the_original_tool_name_is_what_reaches_the_server():
    """暴露名是净化过的，而 server 收到的是它给的原名。"""
    channel = FakeChannel(tools=[FakeChannel.tool("weird.name with/slash")])
    toolset = connect_one(channel)

    assert toolset.tools[0].name == "mcp__fake__weird_name_with_slash"
    toolset.tools[0].execute({})

    assert channel.params_of("tools/call")["name"] == "weird.name with/slash"


def test_a_too_long_exposed_name_is_truncated_and_still_unique():
    """超长的名字截断，**不丢掉这个工具**（丢掉是静默的能力缺失）。"""
    long_a = "x" * 80
    long_b = "x" * 79 + "y"
    channel = FakeChannel(tools=[FakeChannel.tool(long_a), FakeChannel.tool(long_b)])
    toolset = connect_one(channel, name="a" * 24)

    names = [tool.name for tool in toolset.tools]
    assert len(names) == 2
    assert all(len(name) <= 64 for name in names)
    assert len(set(names)) == 2
    # 截断后仍然调得回原名。
    toolset.tools[0].execute({})
    assert channel.params_of("tools/call")["name"] == long_a


def test_two_tools_that_sanitize_to_the_same_name_are_reported_not_silently_dropped():
    """`foo.bar` 和 `foo_bar` 净化后撞名：装一个、说一句。"""
    channel = FakeChannel(tools=[FakeChannel.tool("foo.bar"), FakeChannel.tool("foo_bar")])
    problems: list[str] = []
    toolset = connect_one(channel, problems=problems)

    assert len(toolset.tools) == 1
    assert len(problems) == 1 and "撞了" in problems[0]


def test_a_tool_needs_a_schema_source():
    """两个都没给 → 坏配置，构造时就炸。"""
    with pytest.raises(ValueError, match="恰好给出一个"):
        Tool(name="x", description="d", risk=RiskLevel.LOW, handler=lambda args: "")


def test_both_schema_sources_is_an_error():
    """两个都给了 → 也是坏配置：两份来源会漂移（模型看到 A、执行按 B 校验）。"""
    from agent_runtime.tools.builtin.filesystem import ListFilesArgs

    with pytest.raises(ValueError, match="两个都给了"):
        Tool(
            name="x", description="d", risk=RiskLevel.LOW,
            handler=lambda **kwargs: "", args_model=ListFilesArgs,
            external_schema={"type": "object"},
        )


def test_args_that_are_not_a_json_object_are_invalid_args():
    """参数不是对象 → **invalid_args**，不是"工具执行失败"（模型自己改得对）。"""
    channel = FakeChannel()
    tool = connect_one(channel).tools[0]

    with pytest.raises(InvalidArgsError):
        tool.execute(["不是对象"])


def test_an_invalid_args_error_from_a_server_is_recorded_as_invalid_args():
    """server 说"参数不合法" → 审计里是 invalid_args（和 pydantic 那条同一档）。

    状态分类不是装饰：落进 error 会让模型以为"这个工具坏了"、白白换一条路，而它其实
    只需要把参数改对。
    """
    channel = FakeChannel(results={"echo": InvalidArgsError("缺少必填参数 text")})
    registry = ToolRegistry()
    registry.register(connect_one(channel).tools[0])

    events = Collector()
    agent = Agent(
        ScriptedModel([
            ModelResponse(content=None, tool_calls=[tool_call("mcp__fake__echo", {})]),
            ModelResponse(content="完事了"),
        ]),
        registry,
        PermissionPolicy(),
        asker=lambda tool, arguments: True,
        on_event=events,
    )
    agent.run(Session.new("s"), "跑一下")

    results = events.of("tool_result")
    assert len(results) == 1
    assert results[0]["status"] == "invalid_args"
    assert results[0]["tool"] == "mcp__fake__echo"


# --- 风险等级：外部工具一律 HIGH，默认每条都问 ------------------------------


def test_external_tools_are_high_risk_and_never_parallel_safe():
    channel = FakeChannel(tools=[FakeChannel.tool("a"), FakeChannel.tool("b")])
    toolset = connect_one(channel)

    assert {tool.risk for tool in toolset.tools} == {RiskLevel.HIGH}
    assert not any(tool.parallel_safe for tool in toolset.tools)


def test_high_risk_means_the_default_policy_asks():
    """(这是整个功能的边界：默认策略只自动放行 low。)"""
    registry = ToolRegistry()
    toolset = connect_one(FakeChannel(tools=[FakeChannel.tool("a")]))
    registry.register(toolset.tools[0])

    asked: list[str] = []

    def asker(tool, arguments):
        asked.append(tool.name)
        return False

    agent = Agent(
        ScriptedModel([
            ModelResponse(content=None, tool_calls=[tool_call("mcp__fake__a", {})]),
            ModelResponse(content="完事了"),
        ]),
        registry, PermissionPolicy(), asker=asker,
    )
    agent.run(Session.new("s"), "跑一下")

    assert asked == ["mcp__fake__a"]


def test_high_risk_tools_still_register_fine():
    """HIGH 也能注册（注册期只挡"标了并行却不是 LOW"）。"""
    registry = ToolRegistry()
    toolset = connect_one(FakeChannel(tools=[FakeChannel.tool("a")]))
    registry.register(toolset.tools[0])

    assert registry.get("mcp__fake__a").risk == RiskLevel.HIGH
    assert "mcp__fake__a" in json.dumps(registry.schemas(), ensure_ascii=False)


# --- 协议：握手、分页、版本、内容渲染 ---------------------------------------


def test_handshake_happens_before_listing_and_announces_initialized():
    channel = FakeChannel()
    connect_one(channel)

    assert channel.methods() == ["initialize", "tools/list"]
    assert channel.notifications == ["notifications/initialized"]
    # 一个能力都不声明：声明了 sampling / roots 却答不上来，比不声明坏得多。
    assert channel.params_of("initialize")["capabilities"] == {}


def test_the_servers_protocol_version_is_accepted():
    """server 回一个别的版本号不该让整条连接作废（我们只用 tools/*）。"""
    channel = FakeChannel(protocol_version="2025-06-18")
    toolset = connect_one(channel)
    assert toolset.counts == {"fake": 1}


def test_pagination_is_followed():
    channel = FakeChannel(pages=[
        [FakeChannel.tool("a")],
        [FakeChannel.tool("b")],
    ])
    toolset = connect_one(channel)

    assert [tool.name for tool in toolset.tools] == ["mcp__fake__a", "mcp__fake__b"]
    assert channel.methods() == ["initialize", "tools/list", "tools/list"]
    assert channel.calls[-1][1] == {"cursor": "page-2"}


def test_a_server_that_paginates_forever_is_given_up_on():
    """没有尽头就报错，**不挂在这里**：那是连一条错误都报不出来的形状。"""

    class Forever(FakeChannel):
        def request(self, method, params=None):
            if method == "tools/list":
                self.calls.append((method, dict(params or {})))
                return {"tools": [self.tool("a")], "nextCursor": "还有"}
            return super().request(method, params)

    problems: list[str] = []
    toolset = connect_one(Forever(), problems=problems)

    assert toolset.tools == ()
    assert len(problems) == 1 and "还没到底" in problems[0]


def test_render_joins_text_blocks():
    assert render_content([
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]) == "第一段\n第二段"


def test_non_text_blocks_become_a_visible_placeholder():
    """图片/二进制不是"悄悄丢掉"，而是留一句 —— 否则模型以为自己看过那张图。"""
    text = render_content([
        {"type": "image", "mimeType": "image/png", "data": "..."},
        {"type": "resource", "resource": {"uri": "file:///x", "text": "资源正文"}},
        {"type": "resource", "resource": {"uri": "file:///y", "blob": "..."}},
    ])

    assert "图片 image/png" in text
    assert "资源正文" in text
    assert "二进制内容已省略" in text


def test_structured_content_is_used_when_there_are_no_blocks():
    text = render_content(None, {"answer": 42})
    assert json.loads(text) == {"answer": 42}


def test_empty_content_says_so():
    assert "没有返回任何内容" in render_content([], None)
    assert "没有返回任何内容" in render_content(None, None)


def test_is_error_becomes_a_tool_error_with_the_servers_own_words():
    """工具跑了但失败 → McpToolError，**带着 server 的说明**（模型改参数要靠它）。"""
    channel = FakeChannel(results={"echo": {
        "content": [{"type": "text", "text": "炸了：参数不对"}],
        "isError": True,
    }})
    tool = connect_one(channel).tools[0]

    with pytest.raises(McpToolError, match="炸了：参数不对"):
        tool.execute({})


def test_a_jsonrpc_error_from_a_server_is_a_plain_error():
    channel = FakeChannel(results={"echo": McpError("server 拒绝了 tools/call：坏了")})
    tool = connect_one(channel).tools[0]

    with pytest.raises(McpError):
        tool.execute({})


# --- 装配：一个 server 坏了不影响别的 ---------------------------------------


def test_a_server_that_fails_to_start_only_costs_its_own_tools():
    good = FakeChannel()
    bad = FakeChannel(start_error=McpError("起不来 `npx`：没有这个文件"))
    problems: list[str] = []

    toolset = McpToolset.connect(
        [McpServer(name="bad", command="x"), McpServer(name="good", command="y")],
        channel_factory=lambda server: bad if server.name == "bad" else good,
        on_problem=problems.append,
    )

    assert [tool.name for tool in toolset.tools] == ["mcp__good__echo"]
    assert len(problems) == 1
    assert "bad" in problems[0] and "没连上" in problems[0]
    # 起不来的那个也要收掉（进程可能是起了一半才失败的）。
    assert bad.closed is True


def test_a_server_with_no_tools_connects_and_reports_zero():
    toolset = connect_one(FakeChannel(tools=[]), name="empty")
    assert toolset.tools == ()
    assert toolset.counts == {"empty": 0}


def test_a_server_that_declares_no_tools_capability_says_so_instead_of_failing():
    """只提供 resources 的 server 是合法的：说清"它没有工具"，而不是让它去撞 -32601。"""
    channel = FakeChannel(capabilities={"resources": {}})
    problems: list[str] = []
    toolset = connect_one(channel, name="files", problems=problems)

    assert toolset.tools == () and toolset.counts == {"files": 0}
    assert len(problems) == 1 and "没有 tools" in problems[0]
    assert "tools/list" not in channel.methods()      # 压根没去问
    assert channel.closed is False                     # 但仍然连着，等着被关


def test_a_server_without_capabilities_is_still_asked_for_tools():
    """没声明 capabilities 的 server 现实里存在，它们照样能答 tools/list —— 不卡它们。"""
    channel = FakeChannel(capabilities={})
    toolset = connect_one(channel)

    assert len(toolset.tools) == 1
    assert "tools/list" in channel.methods()


def test_group_is_this_servers_snapshot_and_nothing_else():
    channel = FakeChannel(tools=[FakeChannel.tool("a"), FakeChannel.tool("b")])
    toolset = connect_one(channel, name="one")

    assert toolset.group("mcp__one__a") == ("one", frozenset({"mcp__one__a", "mcp__one__b"}))
    assert toolset.group("read_file") is None


def test_close_closes_every_channel():
    first, second = FakeChannel(), FakeChannel()
    toolset = McpToolset.connect(
        [McpServer(name="a", command="x"), McpServer(name="b", command="y")],
        channel_factory=lambda server: first if server.name == "a" else second,
        on_problem=lambda message: None,
    )

    toolset.close()

    assert first.closed and second.closed


# --- 审批：t 与 a -----------------------------------------------------------


def _typed(monkeypatch, text: str) -> None:
    monkeypatch.setattr("builtins.input", lambda: text)


def mcp_tool(name: str = "mcp__fake__echo") -> Tool:
    return Tool(
        name=name, description="外部工具", risk=RiskLevel.HIGH,
        external_schema={"type": "object"}, handler=lambda args: "",
    )


def group_of(*names: str) -> TrustGroup:
    return TrustGroup(label=f"MCP server fake 的 {len(names)} 个工具", tools=frozenset(names))


def test_a_is_offered_when_there_is_a_group(monkeypatch, capsys):
    _typed(monkeypatch, "n")
    group = group_of("mcp__fake__a", "mcp__fake__b")

    cli_asker(mcp_tool("mcp__fake__a"), {}, memory=ApprovalMemory(),
              trust_group=lambda name: group)

    err = capsys.readouterr().err
    assert "a = " in err
    assert "/a]" in err


def test_a_is_not_offered_without_a_group(monkeypatch, capsys):
    _typed(monkeypatch, "n")

    cli_asker(mcp_tool(), {}, memory=ApprovalMemory(), trust_group=lambda name: None)

    err = capsys.readouterr().err
    assert "a = " not in err
    assert "[y/N/t]" in err


def test_a_is_not_offered_when_it_could_not_be_remembered(monkeypatch, capsys):
    """没有 memory 就不给 a —— 答应了却记不住比拒绝更坏（和 t 同一条）。"""
    _typed(monkeypatch, "a")

    assert cli_asker(mcp_tool(), {}, trust_group=lambda name: group_of("a", "b")) is False

    err = capsys.readouterr().err
    assert "a = " not in err
    assert "[y/N]" in err


def test_a_grants_the_whole_group_in_a_single_write(monkeypatch):
    """一次按键 = **一次**落盘（不是循环 grant 写 N 遍）。"""
    _typed(monkeypatch, "a")
    writes: list[frozenset[str]] = []
    memory = ApprovalMemory(on_change=lambda tools, prefixes: writes.append(tools))
    group = group_of("mcp__fake__a", "mcp__fake__b", "mcp__fake__c")

    assert cli_asker(mcp_tool(), {}, memory=memory, trust_group=lambda name: group) is True

    assert memory.tools() == group.tools
    assert writes == [group.tools]


def test_the_a_hint_says_the_group_is_a_snapshot(monkeypatch, capsys):
    """提示必须说清"server 以后新加的工具仍然会问你" —— 不说就是让人以为全放开了。"""
    _typed(monkeypatch, "n")

    cli_asker(mcp_tool(), {}, memory=ApprovalMemory(label="permissions.json"),
              trust_group=lambda name: group_of("mcp__fake__a", "mcp__fake__b"))

    hint = [line for line in capsys.readouterr().err.splitlines() if "a = " in line][0]
    assert "MCP server fake 的 2 个工具" in hint
    assert "新加的工具仍然会问你" in hint
    assert "permissions.json" in hint


def test_grant_all_is_idempotent_and_reports_only_what_is_new():
    memory = ApprovalMemory()
    assert memory.grant_all(["a", "b"]) == frozenset({"a", "b"})
    assert memory.grant_all(["b", "c"]) == frozenset({"c"})
    assert memory.grant_all(["a", "b", "c"]) == frozenset()
    assert memory.tools() == frozenset({"a", "b", "c"})


# --- 配置：mcp.json 的形状 --------------------------------------------------


def test_parse_servers_reads_the_shape():
    servers = parse_servers({"servers": {"github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "x"},
        "timeout_seconds": 90,
    }}})

    assert servers == (McpServer(
        name="github", command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env={"GITHUB_TOKEN": "x"}, timeout_seconds=90.0,
    ),)


def test_parse_servers_defaults():
    servers = parse_servers({"servers": {"a": {"command": "x"}}})
    assert servers[0].args == () and servers[0].env == {}
    assert servers[0].timeout_seconds > 0


def test_an_empty_config_means_no_servers():
    assert parse_servers({}) == ()
    assert parse_servers({"servers": {}}) == ()


@pytest.mark.parametrize("raw, expected", [
    ({"servers": {"a": {"command": "x"}}, "extra": 1}, "不认识的键"),
    ({"servers": {"a": {"command": "x", "typo": 1}}}, "不认识的键"),
    ({"servers": {"a b": {"command": "x"}}}, "不合法"),
    ({"servers": {"": {"command": "x"}}}, "不合法"),
    ({"servers": {"a": {}}}, "缺 command"),
    ({"servers": {"a": {"command": "  "}}}, "缺 command"),
    ({"servers": {"a": {"command": "x", "args": "-y"}}}, "args 必须是字符串数组"),
    ({"servers": {"a": {"command": "x", "args": [1]}}}, "args 必须是字符串数组"),
    ({"servers": {"a": {"command": "x", "env": {"K": 1}}}}, "env 必须是"),
    ({"servers": {"a": {"command": "x", "timeout_seconds": 0}}}, "timeout_seconds"),
    ({"servers": {"a": {"command": "x", "timeout_seconds": 10_000}}}, "timeout_seconds"),
    ({"servers": {"a": {"command": "x", "timeout_seconds": True}}}, "timeout_seconds"),
    ({"servers": []}, "servers"),
    ({"servers": {"a": "npx"}}, "必须是一个对象"),
])
def test_bad_configs_are_rejected_with_a_readable_reason(raw, expected):
    with pytest.raises(McpConfigError, match=expected):
        parse_servers(raw)


def test_a_missing_file_means_no_servers():
    assert McpConfig.from_file(TESTS_DIR / "这个文件不存在.json").servers == ()


def test_a_malformed_file_is_a_config_error(workdir):
    """形状问题归到 ConfigError：那是"用户得先做点事"，要在开会话之前停下。"""
    path = workdir / "mcp.json"
    path.write_text(json.dumps({"servers": {"a": {"command": ""}}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="mcp.json 有问题"):
        McpConfig.from_file(path)


def test_a_broken_json_file_is_a_config_error(workdir):
    path = workdir / "mcp.json"
    path.write_text("{ 不是 JSON", encoding="utf-8")

    with pytest.raises(ConfigError):
        McpConfig.from_file(path)


# --- 端到端：真的起一个子进程 -----------------------------------------------


def real_server(*extra: str, timeout: float = 30.0) -> McpServer:
    return McpServer(
        name="fake",
        command=sys.executable,
        args=(str(FAKE_SERVER), *extra),
        timeout_seconds=timeout,
    )


def test_end_to_end_over_stdio():
    """真子进程、真管道、真 JSON-RPC：连上、列工具、调工具、关掉。"""
    toolset = McpToolset.connect([real_server()])
    try:
        names = sorted(tool.name for tool in toolset.tools if "echo" in tool.name)
        assert names == ["mcp__fake__echo"]

        echo = next(tool for tool in toolset.tools if tool.name == "mcp__fake__echo")
        # 连 schema 都是 server 那份（required 里有 text）。
        assert echo.parameters["required"] == ["text"]
        assert json.loads(echo.execute({"text": "你好"})) == {"text": "你好"}

        # 参数不是标识符也照走（这就是不展开成 kwargs 的理由）。
        assert "foo-bar" in echo.execute({"foo-bar": 1})

        # 非文本内容：留下可见的占位。
        nontext = next(tool for tool in toolset.tools if tool.name == "mcp__fake__nontext")
        rendered = nontext.execute({})
        assert "图片 image/png" in rendered and "只有这一句是文本" in rendered

        # isError → 带着 server 自己的话报错。
        boom = next(tool for tool in toolset.tools if tool.name == "mcp__fake__boom")
        with pytest.raises(McpToolError, match="总是失败"):
            boom.execute({})

        # -32602 → InvalidArgsError（模型自己改得对那一档）。
        bad = next(tool for tool in toolset.tools if tool.name == "mcp__fake__bad_args")
        with pytest.raises(InvalidArgsError, match="缺少必填参数 text"):
            bad.execute({})
    finally:
        toolset.close()

    # 收摊之后进程真的没了（不这样断言的话，孤儿进程会一直留在那里）。
    assert all(connection.channel._proc.poll() is not None for connection in toolset.connections)


def test_end_to_end_with_pagination():
    toolset = McpToolset.connect([real_server("--paginate")])
    try:
        assert len(toolset.tools) == 5          # 两页加起来的那个数
    finally:
        toolset.close()


def test_a_server_that_refuses_the_handshake_is_reported():
    problems: list[str] = []
    toolset = McpToolset.connect([real_server("--no-initialize")], on_problem=problems.append)
    try:
        assert toolset.tools == ()
        assert len(problems) == 1 and "没连上" in problems[0]
    finally:
        toolset.close()


def test_a_server_that_dies_at_startup_is_reported_not_raised():
    problems: list[str] = []
    toolset = McpToolset.connect([real_server("--exit-now")], on_problem=problems.append)
    try:
        assert toolset.tools == ()
        assert len(problems) == 1 and "没连上" in problems[0]
    finally:
        toolset.close()


def test_a_hanging_server_hits_the_timeout_instead_of_blocking_forever():
    """卡住的 server 只能变成一条超时错误 —— Agent 里**没有任何工具级超时**。"""
    channel = StdioChannel(real_server("--hang", timeout=1.0))
    try:
        with pytest.raises(McpTimeout, match="超过"):
            channel.request("initialize", {})
    finally:
        channel.close()


def test_a_dead_server_wakes_waiters_with_a_server_down_error():
    """进程死了要让还在等的人立刻醒来，而不是各自等满超时。"""
    channel = StdioChannel(real_server("--exit-now", timeout=30.0))
    try:
        # McpServerDown 是 McpError 的一支；具体命中哪一支取决于"读线程先看见 EOF"
        # 还是"我们先写进一条死管道"，两者都是同一件事：这个 server 没了。
        with pytest.raises(McpError, match="fake"):
            channel.request("initialize", {})
    finally:
        channel.close()


def test_the_servers_stderr_noise_does_not_break_the_channel():
    """server 往 stderr 写日志是允许的，而且我们**不接管**它（接管了没人读会写满卡住）。"""
    toolset = McpToolset.connect([real_server("--stderr-noise")])
    try:
        assert len(toolset.tools) == 5
    finally:
        toolset.close()


def test_exposed_name_matches_the_documented_shape():
    assert exposed_name("github", "create_file") == "mcp__github__create_file"


# --- 入口装配：真的走一遍 main() --------------------------------------------


class _FakeModelConfig:
    """一个不需要密钥的模型配置。main() 只在装配时读它，不会真的发请求。"""

    api_key = "sk-test"
    base_url = "http://127.0.0.1:1"
    model = "fake-model"
    context_tokens = None

    @classmethod
    def from_env(cls, env_file=None):
        return cls()


def test_the_entry_point_wires_mcp_tools_and_closes_them(monkeypatch, workdir, capsys):
    """入口那一层：读配置 → 连 server → 注册工具 → 报告 → 关掉。

    这是**唯一**能证明"配了 mcp.json 之后真的能用"的地方：协议、工具构造、审批各自
    都有单测，但它们之间的接线、以及"谁来关这些子进程"，只有走一遍入口才看得见。
    """
    import main

    monkeypatch.setattr(sys, "argv", ["main.py"])
    # 会话和日志落到临时目录，别污染这个项目自己的 .tudouni/。
    monkeypatch.setattr(main, "SESSIONS_DIR", workdir / "sessions")
    monkeypatch.setattr(main, "LOGS_DIR", workdir / "logs")
    monkeypatch.setattr(main, "ModelConfig", _FakeModelConfig)
    monkeypatch.setattr(main, "print_banner", lambda: None)
    monkeypatch.setattr(
        main.McpConfig, "from_file",
        classmethod(lambda cls, path=None: McpConfig(servers=(real_server(),))),
    )

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        main, "run_repl",
        lambda agent, session, session_id, logs, tokens: seen.update(agent=agent),
    )

    closed: list[bool] = []

    class Recording(McpToolset):
        def close(self) -> None:
            closed.append(True)
            super().close()

    monkeypatch.setattr(main, "McpToolset", Recording)

    assert main.main() == 0

    tool = seen["agent"].tools.get("mcp__fake__echo")
    assert tool.risk == RiskLevel.HIGH
    assert closed == [True], "入口必须把这些子进程收掉"

    captured = capsys.readouterr()
    assert "server fake：连上了，提供 5 个工具" in captured.err
    assert "每次调用都要你批准" in captured.err
    # "已注册工具"那份清单里也有它（那是人核对权限文件时对照的那一份）。
    assert "mcp__fake__echo" in captured.out
