"""会话持久化的正确性与安全底线。

这里的每一条都对应一个实测过的故障：路径穿越、半截文件、schema 漂移。
"""

import json
from pathlib import Path

import pytest

from agent_runtime.agents import Agent
from agent_runtime.audit import JsonlSink
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.tool import RiskLevel

from fakes import ScriptedModel, usage


def test_round_trip(workdir):
    store = JsonSessionStore(workdir)
    session = Session.new("s")
    session.messages.append({"role": "user", "content": "你好"})
    session.metadata["note"] = "随便一个值"
    store.save(session)

    back = store.load("s")
    assert back.messages == session.messages
    assert back.metadata == session.metadata


def test_no_file_until_the_first_turn(workdir, registry):
    """新建会话本身不该产生文件 —— 说了第一句话才落盘。

    这个行为不是特意写的，而是从设计里掉出来的：第一次 _checkpoint 发生在
    run() 把用户消息追加进 messages 之后。所以"聊了才存"和"开了不用不留垃圾"
    是同一个机制保证的。
    """
    store = JsonSessionStore(workdir)
    session = Session.new("s")
    agent = Agent(ScriptedModel([ModelResponse(content="你好", usage=usage())]),
                  registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_checkpoint=store.save)

    assert not store.exists("s")
    agent.run(session, "在吗")
    assert store.exists("s")


@pytest.mark.parametrize("bad_id", ["../../evil", "..\\..\\evil", "a/b", "", "x" * 65, "a b"])
def test_store_rejects_unsafe_session_id(workdir, bad_id):
    """session_id 会被拼进文件名，实测 "../../evil" 能把文件写到目录外。"""
    store = JsonSessionStore(workdir)
    with pytest.raises(ValueError):
        store.save(Session(session_id=bad_id))


@pytest.mark.parametrize("bad_id", ["../../evil", "a/b", ""])
def test_sink_rejects_unsafe_session_id(workdir, bad_id):
    """审计 sink 同样把 session_id 拼进文件名 —— 校验规则必须和 store 是同一份。"""
    sink = JsonlSink(workdir)
    with pytest.raises(ValueError):
        sink({"kind": "x", "session_id": bad_id})


def test_save_is_atomic(workdir, monkeypatch):
    """写到一半被杀，目标文件必须还是旧的完整版。

    状态持久化的意义就是扛崩溃 —— 它不能在最需要它的那一刻毁掉自己的数据。
    """
    store = JsonSessionStore(workdir)
    store.save(Session("s", [{"role": "user", "content": "旧"}]))
    target = workdir / "s.json"
    before = target.read_text(encoding="utf-8")

    real_write_text = Path.write_text

    def half_then_die(self, data, **kwargs):
        real_write_text(self, data[: len(data) // 2], **kwargs)
        raise OSError("模拟进程被杀")

    monkeypatch.setattr(Path, "write_text", half_then_die)
    with pytest.raises(OSError):
        store.save(Session("s", [{"role": "user", "content": "新"}]))

    assert target.read_text(encoding="utf-8") == before
    assert store.load("s").messages[0]["content"] == "旧"


def test_load_tolerates_unknown_fields(workdir):
    """会话文件躺在硬盘上，比代码活得久 —— 多出来的字段不能让它打不开。"""
    store = JsonSessionStore(workdir)
    store.save(Session("s"))
    path = workdir / "s.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["created_at"] = "2026-01-01T00:00:00"
    raw["a_field_from_the_future"] = {"nested": [1, 2]}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    assert store.load("s").session_id == "s"


def test_sink_appends_and_skips_torn_line(workdir):
    """审计日志只追加。进程被杀会留下半截行，读的时候跳过它就行。"""
    sink = JsonlSink(workdir)
    sink({"kind": "a", "session_id": "s"})
    sink({"kind": "b", "session_id": "s"})
    with (workdir / "s.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"kind": "torn", "ts": "2026')       # 半截
    assert [e["kind"] for e in sink.read("s")] == ["a", "b"]


def test_new_session_ids_are_unique_and_sortable(workdir):
    store = JsonSessionStore(workdir)
    first = store.new_session_id()
    store.save(Session(first))
    assert store.new_session_id() != first            # 撞名不许覆盖
    assert first[:9].isdigit() or "-" in first        # 时间戳形态，保证 --list 按时间排


def test_step_count_is_derived(workdir):
    """步数不落盘：存成字段就有了两份，而且语义会从"这一轮"变成"整个会话"。"""
    assert set(Session.__dataclass_fields__) == {"session_id", "messages", "metadata"}
    session = Session("s", [{"role": "assistant", "content": "a"},
                            {"role": "user", "content": "u"},
                            {"role": "assistant", "content": "b"}])
    assert session.step_count() == 2
