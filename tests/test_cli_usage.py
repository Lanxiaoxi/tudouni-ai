"""交互循环末尾那句统计里的用量部分。

它和 `--audit` 末尾那份汇总**共用同一个 `summarize`** —— 两边回答的是同一个问题
（"这批事件花了多少"），所以这里钉住的是三件事：

  1. 只算成功的调用（重试里失败的尝试没有用量，计进去会把命中率算低）；
  2. "没查过缓存"和"查了一个都没命中"必须能分辨 —— 前者是 "—"，不是 "0%"；
  3. 一次成功调用都没有时，那一行宁可什么都不说，也不硬凑一个数字。
"""

from agent_runtime.audit import JsonlSink
from agent_runtime.cli import _usage_note, summarize
from agent_runtime.state import Session


def call(prompt: int, cached: int, completion: int = 10, status: str = "ok") -> dict:
    return {
        "kind": "model_call", "status": status,
        "prompt_tokens": prompt, "cached_tokens": cached,
        "miss_tokens": prompt - cached, "completion_tokens": completion,
    }


def test_usage_only_counts_successful_calls():
    usage = summarize([call(100, 80), call(0, 0, status="error")])

    assert usage.calls == 2          # 试了两次
    assert usage.ok_calls == 1       # 只有一次有账可算
    assert (usage.prompt, usage.cached, usage.miss) == (100, 80, 20)


def test_tool_events_are_not_usage():
    """只认 model_call —— 别的事件碰巧有相似字段也不该被算进来。"""
    usage = summarize([call(100, 80), {"kind": "tool_result", "chars": 999}])
    assert usage.prompt == 100


def test_hit_rate_is_dash_when_nothing_was_ever_asked():
    """一次都没查过缓存，和"查了但一个都没命中"是两回事，不能都显示成 0%。"""
    assert summarize([]).hit_rate == "—"
    assert summarize([call(100, 0)]).hit_rate == "0%"


def test_usage_note_is_silent_when_nothing_succeeded(workdir):
    """第一轮就鉴权失败之类：没有用量可报，就别报。"""
    assert _usage_note(JsonlSink(workdir), Session.new("s")) == ""


def test_usage_note_works_without_a_sink():
    """sink 是可选的 —— 缺了只是少一行统计，不该让交互循环崩掉。"""
    assert _usage_note(None, Session.new("s")) == ""


def test_usage_note_reports_the_whole_session(workdir):
    """按会话累计，和它旁边那个 step_count() 一致 —— 累计值才是真实花费。"""
    sink = JsonlSink(workdir)
    for record in (call(100, 80), call(300, 300)):
        sink({**record, "session_id": "s"})

    note = _usage_note(sink, Session.new("s"))

    assert "400 token" in note      # 两轮之和，不是最后一轮
    assert "380" in note            # 累计命中
    assert "95%" in note            # 380 / 400
