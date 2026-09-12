"""交互循环末尾那句统计：累计用量 + 本轮耗时。

两样都和 `--audit` 末尾那份汇总**共用同一批事件** —— 两边回答的是同一个问题，所以
这里钉住的是四件事：

  1. 只算成功的调用（重试里失败的尝试没有用量，计进去会把命中率算低）；
  2. "没查过缓存"和"查了一个都没命中"必须能分辨 —— 前者是 "—"，不是 "0%"；
  3. 一次成功调用都没有时，那一行宁可什么都不说，也不硬凑一个数字；
  4. **本轮耗时按 run_id 配对取**，而且措辞要写明"本轮" —— 旁边那几个数都是会话累计。
"""

import pytest

from agent_runtime.audit import JsonlSink
from agent_runtime.frontends.cli import (
    _context_note,
    _stats_note,
    _tokens_text,
    _turn_note,
    last_prompt_tokens,
    last_turn_ms,
    summarize,
)
from agent_runtime.state import Session


def call(prompt: int, cached: int, completion: int = 10, status: str = "ok") -> dict:
    return {
        "kind": "model_call", "status": status,
        "prompt_tokens": prompt, "cached_tokens": cached,
        "miss_tokens": prompt - cached, "completion_tokens": completion,
    }


def turn(run_id: str, duration_ms: int | None) -> list[dict]:
    """一个回合的两条边界事件；duration_ms 为 None 表示这次没记（收尾前就崩了）。"""
    events = [{"kind": "run_started", "run_id": run_id}]
    if duration_ms is not None:
        events.append({"kind": "run_finished", "run_id": run_id, "duration_ms": duration_ms})
    return events


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
    assert _stats_note(JsonlSink(workdir), Session.new("s")) == ""


def test_usage_note_works_without_a_sink():
    """sink 是可选的 —— 缺了只是少一行统计，不该让交互循环崩掉。"""
    assert _stats_note(None, Session.new("s")) == ""


def test_usage_note_reports_the_whole_session(workdir):
    """按会话累计，和它旁边那个 step_count() 一致 —— 累计值才是真实花费。"""
    sink = JsonlSink(workdir)
    for record in (call(100, 80), call(300, 300)):
        sink({**record, "session_id": "s"})

    note = _stats_note(sink, Session.new("s"))

    assert "400 token" in note      # 两轮之和，不是最后一轮
    assert "380" in note            # 累计命中
    assert "95%" in note            # 380 / 400


# --- 本轮耗时 -------------------------------------------------------------
#
# 它旁边那几个数都是**会话累计**，只有这个是刚结束的那一轮。所以两组断言：数字取对了，
# 以及措辞把"本轮"说出来（否则读者会以为它也累计）。

def test_turn_note_says_this_turn_not_the_session():
    assert _turn_note(turn("a", 12340)) == "；本轮 12.3s"


def test_turn_note_keeps_milliseconds_below_a_second():
    """一轮常常只有几百毫秒，写成 "0.4s" 等于没说。"""
    assert _turn_note(turn("a", 850)) == "；本轮 850ms"


def test_turn_note_uses_the_duration_agent_recorded():
    """取的是 run_finished 里那个数，**不在这里重新计时** —— 否则就有了两份事实，
    REPL 报的会和 --audit 报的对不上。"""
    events = [{"kind": "run_started", "run_id": "a"},
              {"kind": "run_finished", "run_id": "a", "duration_ms": 4321}]

    assert last_turn_ms(events) == 4321


def test_turn_note_pairs_by_run_id_not_by_the_last_run_finished():
    """崩在收尾之前的回合：日志里最后那条 run_finished 属于**上一轮**。

    直接取它当"本轮"报出来，是在报一个跟这次无关的数字 —— 而它看起来完全合理。
    """
    events = turn("上一轮", 60_000) + turn("本轮", None)

    assert last_turn_ms(events) is None
    assert _turn_note(events) == ""


def test_turn_note_is_silent_without_any_run():
    assert last_turn_ms([]) is None
    assert _turn_note([]) == ""


def test_the_footer_carries_both_notes(workdir):
    """末尾那一句：累计用量和本轮耗时都得出现，且各说各的口径。"""
    sink = JsonlSink(workdir)
    for record in (call(100, 80), *turn("a", 12340), call(300, 300)):
        sink({**record, "session_id": "s"})

    note = _stats_note(sink, Session.new("s"))

    assert "累计输入 400 token" in note
    assert "本轮 12.3s" in note


# --- 上下文用量 -----------------------------------------------------------
#
# 分子是实测的（上一次成功请求的 prompt_tokens），分母是声明的（模型窗口表）。
# 所以这里两组断言：取的是"最后那条、且成功的那条"，以及表里没名字时不报占比。

def test_context_note_shows_used_over_the_window():
    events = [call(2_035, 1_792)]

    assert _context_note(events, 1_000_000) == "；上下文 2.0k/1M（0.2%）"


@pytest.mark.parametrize("used,expected", [
    (2_035, "0.2%"),            # 一位小数 —— 窗口是百万级，一位就够
    (59_000, "5.9%"),
    (1_200_000, "120.0%"),      # 超过 100% 照实报，不夹平
])
def test_the_percentage_keeps_one_decimal_and_never_clamps(used, expected):
    """超过 100% 也要照原样报：那一轮就是发不出去了，抹平会让人以为"刚好卡住"。"""
    assert expected in _context_note([call(used, 0)], 1_000_000)


def test_context_note_reads_the_latest_call_not_the_first_or_a_sum():
    """上下文只增不减，所以要最后那条 —— 不是第一条，也不是各次之和。"""
    events = [call(1_000, 0), call(2_035, 1_792), call(9_000, 8_000)]

    assert last_prompt_tokens(events) == 9_000
    assert _context_note(events, 1_000_000) == "；上下文 9.0k/1M（0.9%）"


def test_context_note_ignores_attempts_without_usage():
    """失败的尝试没有用量字段（_usage_fields 拿不到 usage 就什么都不写）——
    所以"有 prompt_tokens"本身就等于"成功"，不用另外判 status。"""
    events = [call(2_035, 1_792), {"kind": "model_call", "status": "error"}]

    assert last_prompt_tokens(events) == 2_035


def test_context_note_drops_the_denominator_when_the_model_is_not_in_the_table():
    """错的百分比比没有百分比更坏 —— 表里没有的模型名只报用量，也没有百分比。"""
    assert _context_note([call(2_035, 1_792)], None) == "；上下文 2.0k"
    assert _context_note([call(2_035, 1_792)], 0) == "；上下文 2.0k"


def test_context_note_is_silent_without_a_successful_call():
    """一次成功调用都没有（比如第一轮就鉴权失败）：没有分子，就别报。"""
    assert last_prompt_tokens([]) is None
    assert _context_note([], 1_000_000) == ""
    assert _context_note([{"kind": "model_call", "status": "fatal"}], 1_000_000) == ""


@pytest.mark.parametrize("count,expected", [
    (850, "850"),            # 小数目给精确值
    (2_035, "2.0k"),
    (9_999, "10.0k"),
    (12_345, "12k"),
    (1_000_000, "1M"),
])
def test_token_text_keeps_both_magnitudes_readable(count, expected):
    """一轮的用量可能是几千、窗口是百万级 —— 两种量级都要一眼看得懂。"""
    assert _tokens_text(count) == expected


def test_the_footer_carries_the_context_too(workdir):
    sink = JsonlSink(workdir)
    for record in (call(2_035, 1_792), *turn("a", 12_340)):
        sink({**record, "session_id": "s"})

    note = _stats_note(sink, Session.new("s"), 1_000_000)

    assert "本轮 12.3s" in note
    assert "上下文 2.0k/1M（0.2%）" in note
