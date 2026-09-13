"""协议的形状：**schema 是权威，这份测试保证它不漂。**

## 它挡的是什么

`protocol/schema/*.schema.json` 是"字段名和类型"的唯一来源：Python 运行时从它读，
将来的 TypeScript 类型从它生成，`doc/protocol.md` 只讲语义（**不许抄字段表**）。

可是"唯一的来源"这句承诺本身需要一个看守者 —— 否则真实的消息慢慢会长出 schema 里
没有的字段，而症状是"TS 那一侧永远少一个键"，直到 Web 前端上线才炸。所以这里两头都钉：

  1. **schema ↔ `messages.py` 的常量**：枚举值和常量对得上；
  2. **schema ↔ 真实发出的消息**：`init` / `permission_request` / `question_request`
     这些由 runtime 构造的，逐字段比对（多一个或少一个都红）。

比对的是**结构**，不是语义 —— 语义那一半在 `doc/protocol.md` 的散文里，
没法自动比对，只能靠"不抄字段表"这条纪律。
"""

import json

import pytest

from agent_runtime.protocol import messages


def _schema(name: str) -> dict:
    return messages.load_schema(name)


# --- schema 和 messages.py 的常量 ---------------------------------------------

def test_every_message_kind_in_the_constants_has_a_schema():
    """`messages.py` 里列的每一种消息，schema 里都得有它的形状。

    两边各写一半的下场是"有一种消息没有任何地方声明过它的字段" —— 而它照样能跑，
    只是没人知道它该长什么样。
    """
    inbound = _schema("inbound")
    outbound = _schema("outbound")

    for name in messages.INBOUND:
        assert name in inbound, f"入站 {name} 没有 schema"
    for name in messages.OUTBOUND:
        assert name in outbound, f"出站 {name} 没有 schema"


def test_the_decision_enum_matches_the_constants():
    """审批的四个答案：schema 的 enum 就是 `DECISIONS`。

    **这条特别值得钉**：那四个字符串是两端的共同语言，而它们只以字符串的形式
    在协议上走。任何一边多一个、少一个、或者拼错一个，症状都是"回应被忽略了"
    —— 而 runtime 那边刻意**不报错**（不认识的 id / decision 一律忽略，见
    `protocol/channels.py` 的 `_dispatch`），所以它连一行日志都不会留下。
    """
    enum = _schema("inbound")["permission_response"]["fields"]["decision"]["enum"]
    assert set(enum) == set(messages.DECISIONS)
    # 顺序也钉住：它决定前端菜单里按钮的排布，而"y 在最左"是个约定。
    assert enum == ["allow", "deny", "always", "always_group"]


def test_the_question_status_enum_excludes_unavailable():
    """`unavailable` **不在**前端能回的枚举里 —— 那是刻意的。

    "没有人可问"是 runtime 自己的判断（界面断连、CI、`--autopilot`），不是前端
    回的一个选项。把它放进 enum 就等于邀请前端谎报"没人可问"（而那是默许的另一种
    写法）。
    """
    enum = _schema("inbound")["question_response"]["fields"]["status"]["enum"]
    assert set(enum) == {"answered", "skipped"}
    assert "unavailable" not in enum


def test_the_event_kinds_in_schema_are_exactly_what_the_agent_emits():
    """`event` 的 kind 白名单 == `audit` 那一层真实会发的那些。

    这条挡的是"新加了一种事件而协议没跟上"。那种情况下前端会收到一个它不认识的
    kind —— 按协议它该忽略（不许崩），但**你会永远看不到那一类信息**，
    而且不会有任何东西告诉你。
    """
    import re
    from pathlib import Path

    agent_py = Path(messages.__file__).resolve().parent.parent / "agents" / "agent.py"
    source = agent_py.read_text(encoding="utf-8")
    emitted = set(re.findall(r'self\._emit\(\s*"(\w+)"', source))

    declared = set(_schema("outbound")["event"]["fields"]["kind"]["enum"])
    assert declared == emitted, (
        f"schema 里的 kind 和 agents/agent.py 实际发的不一致："
        f"只声明没发={declared - emitted}，只发没声明={emitted - declared}"
    )


# --- schema 和真实发出的消息 ---------------------------------------------------

@pytest.fixture
def real_messages(workdir, monkeypatch):
    """起一个**真的** runtime，把 `init` / `permission_request` / `question_request`
    这三条由 runtime 构造的消息抓出来。

    为什么不用手写的样本：手写的样本永远和实现一致（因为是人照着自己写的代码写的），
    而这条测试要抓的正是"实现和 schema 不一致"。所以必须问真的对象要。
    """
    from agent_runtime.protocol.channels import ProtocolServer
    from agent_runtime.protocol.transport_stdio import StdioTransport
    from agent_runtime.runtime.channels import cli_channels
    from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
    from agent_runtime.runtime.config import McpConfig, ModelConfig, PermissionConfig, WebConfig
    from agent_runtime.protocol import codec
    from agent_runtime.tools.builtin.ask import AskUserArgs
    from agent_runtime.tools.tool import RiskLevel

    class _Sink:
        """一个把消息收下来的假传输。"""
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, message: dict) -> None:
            self.sent.append(message)

    class _Transport(StdioTransport):
        def __init__(self) -> None:      # 不碰真的 stdin/stdout
            self.reader = None
            self.writer = None
            self._lines = None
            self.sent: list[dict] = []

        def send(self, message: dict) -> None:
            self.sent.append(message)

        def recv(self):
            return iter(())

        def close(self) -> None:
            pass

    booted = boot()
    session_id, session, resumed = resolve_session(booted.store, None)
    transport = _Transport()
    server = ProtocolServer(transport)

    runtime = open_runtime(
        booted=booted, session_id=session_id, session=session,
        channels=cli_channels(), resumed=resumed,
        model_config=ModelConfig(api_key="sk-x", base_url="http://127.0.0.1:1",
                                 model="fake"),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(),
    )
    try:
        server.attach(runtime)
        captured: dict[str, dict] = {"init": server._init_message()}

        # 审批：造一个 HIGH 的工具，走一遍 asker（它会把请求发到 transport 上）。
        from agent_runtime.protocol.channels import ProtocolAsker
        from agent_runtime.tools.tool import Tool, ToolArgs

        class _Args(ToolArgs):
            command: str = ""

        tool = Tool(name="shell", description="x", risk=RiskLevel.HIGH,
                    args_model=_Args, handler=lambda **kw: "")
        asker = ProtocolAsker(server)

        import threading
        def answer_later() -> None:
            """等请求发出来之后，替"人"回一个 deny —— 这条测试只看请求长什么样。"""
            for _ in range(500):
                if transport.sent:
                    break
                threading.Event().wait(0.01)
            server.pending.resolve(transport.sent[-1]["id"], messages.DENY)

        threading.Thread(target=answer_later, daemon=True).start()
        asker(tool, {"command": "echo hi"})
        captured["permission_request"] = transport.sent[-1]

        # 提问：同理。
        transport.sent.clear()
        threading.Thread(target=answer_later, daemon=True).start()
        server.channels().questioner(AskUserArgs(question="选哪个？", header="选",
                                                 options=["a", "b"]))
        captured["question_request"] = transport.sent[-1]

        # 流式那两条：它们是**纯出站**的（前端不回任何东西），所以直接调那一层的
        # 接口 —— 而它拼出来的形状必须是 schema 里那一份。
        #
        # `_last_step` 手工设一下理由：真跑时它由 `model_call` 那类事件记下来，
        # 而这里没有回合在跑。
        transport.sent.clear()
        server._last_step = 1
        server._last_run_id = "r-schema"
        server.on_delta(text="一", reasoning="想")
        captured["delta"] = transport.sent[0]
        assert [m["channel"] for m in transport.sent] == ["text", "reasoning"]
        server.on_delta(reset=True)
        captured["delta_reset"] = transport.sent[-1]

        # 编解码往返：一条真消息必须能原样过一遍管道。
        for name, message in captured.items():
            assert codec.decode(codec.encode(message)) == json.loads(
                json.dumps(message, ensure_ascii=False)
            ), f"{name} 过不了编解码往返"
        return captured
    finally:
        runtime.close()


@pytest.mark.parametrize("name", ["init", "permission_request", "question_request",
                                  "delta", "delta_reset"])
def test_real_messages_match_their_schema(name, real_messages):
    """**两头都钉**：schema 里声明的字段和真实消息的字段必须一致。

    多一个字段 = schema 漏了（TS 那一侧会少一个键）；少一个字段 = 实现漏了
    （前端会拿到 `undefined`）。两种都会在 Web 前端上线时才炸，所以现在就要红。
    """
    message = real_messages[name]
    declared = messages.field_names("outbound", name)

    actual = set(message)
    # `_init_message` 是内部方法，返回的就是那条消息本身（没有额外包装）。
    assert actual == declared, (
        f"{name} 的字段和 schema 对不上："
        f"实现多了 {actual - declared}，实现少了 {declared - actual}"
    )

    required = messages.required_fields("outbound", name)
    assert required <= actual, f"{name} 少了必填字段 {required - actual}"
