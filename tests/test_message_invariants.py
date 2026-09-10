"""会话一致性的不变式。

这几条一旦被破坏，后果不是"结果不对"，而是**会话永久损坏**：API 会直接 400 拒绝，
之后每一轮都发不出去。在写下这些测试之前，它们只存在于一次对话里。

    An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'.
"""

import json

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.tool import RiskLevel

from fakes import Collector, ScriptedModel, tool_call, usage


def assert_consistent(messages) -> None:
    """每条带 tool_calls 的 assistant，后面必须紧跟**全部**对应 id 的 tool 结果。"""
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            need = [c["id"] for c in msg["tool_calls"]]
            got, j = [], i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                got.append(messages[j]["tool_call_id"])
                j += 1
            assert got == need, f"assistant 要 {need}，后面只有 {got}"
            i = j
        else:
            i += 1


def test_checker_rejects_broken_history():
    """先证明检查器本身有效 —— 否则下面那些 assert_consistent 可能是空断言。"""
    broken = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
    ]
    with pytest.raises(AssertionError):
        assert_consistent(broken)


def test_batched_tool_calls_all_get_results(registry):
    """一步里批了多个调用，就得有同样多的结果 —— 少一个都会让会话不可用。"""
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("list_files", {}, "c1"),
                                  tool_call("list_files", {}, "c2")],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}), asker=lambda t, a: False)
    session = Session.new("s")
    agent.run(session, "列两次目录")
    assert_consistent(session.messages)


def test_checkpoint_never_sees_inconsistent_history(workdir, registry):
    """落盘点只能在一整个 step 之后 —— 半截状态一旦写出去，会话就再也发不出去。"""
    seen: list[int] = []
    store = JsonSessionStore(workdir)

    def on_checkpoint(session):
        assert_consistent(session.messages)     # 每次落盘都自检
        seen.append(len(session.messages))
        store.save(session)

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_checkpoint=on_checkpoint)
    agent.run(Session.new("s"), "列目录")

    # 2 = 用户消息入库后；4 = 工具批次执行完的 ★；5 = 最终答案
    assert seen == [2, 4, 5]


def test_event_sink_failure_does_not_corrupt_session(registry):
    """审计写入失败必须被吞掉。

    on_event 的调用点有些落在 messages **不一致**的窗口里（tool_call 事件就夹在
    assistant 消息和它的 tool 结果之间），抛出去会留下悬空的 tool_calls。
    实测过：内存会话损坏、之后每轮 400。
    """

    def exploding_sink(record):
        if record["kind"] == "tool_call":
            raise OSError("No space left on device")

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_event=exploding_sink)
    session = Session.new("s")

    assert agent.run(session, "列目录") == "完成"      # 不该崩
    assert_consistent(session.messages)                # 不该坏


def test_checkpoint_failure_does_not_crash(registry):
    """落盘失败同样不该让 run() 崩 —— 它的调用点都在一致时刻，会话本身是好的。"""
    model = ScriptedModel([ModelResponse(content="完成", usage=usage())])

    def exploding(session):
        raise OSError("磁盘满")

    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_checkpoint=exploding)
    assert agent.run(Session.new("s"), "hi") == "完成"


def test_events_are_json_serializable(registry):
    """每条事件都必须能直接 json.dumps —— 这是它能逐行 append 的前提。"""
    collector = Collector()
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_event=collector)
    agent.run(Session.new("s"), "列目录")

    for event in collector.events:
        json.dumps(event, ensure_ascii=False)       # 不抛就算过

    assert set(collector.kinds()) >= {
        "run_started", "model_call", "tool_call", "permission", "tool_result", "run_finished",
    }
