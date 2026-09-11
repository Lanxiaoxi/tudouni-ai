"""`--audit` 末尾那行的耗时汇总：从事件里数出来，不在别处再记一份。

Agent 那头**怎么把时间记准**（口径互不重叠、时钟可注入）在 test_timing.py；这里只盯
汇总这一侧的两件事：数得对不对、什么时候不该说话。
"""

from agent_runtime.cli import _print_timing, summarize_time


def test_summarize_time_reads_every_part_from_the_events():
    events = [
        {"kind": "model_call", "duration_ms": 1200},
        {"kind": "permission", "waited_ms": 30000},
        {"kind": "tool_result", "duration_ms": 250},
        {"kind": "model_call", "status": "error", "duration_ms": 800, "backoff_ms": 500},
        {"kind": "run_finished", "duration_ms": 33000},
    ]
    timing = summarize_time(events)

    assert (timing.model_ms, timing.tool_ms) == (2000, 250)     # 失败的那次也是真实请求
    assert (timing.waited_ms, timing.backoff_ms) == (30000, 500)
    assert timing.run_ms == 33000
    assert timing.explained_ms == 32750
    assert timing.unattributed_ms == 250                        # 落盘、事件写入、判定……


def test_unattributed_never_goes_negative():
    """从别处复制来的日志、毫秒取整都可能让分项之和超过总数 —— 负数没有解释价值。"""
    timing = summarize_time([
        {"kind": "tool_result", "duration_ms": 5000},
        {"kind": "run_finished", "duration_ms": 1000},
    ])

    assert timing.unattributed_ms == 0


def test_the_line_shows_every_part(capsys):
    _print_timing(summarize_time([
        {"kind": "model_call", "duration_ms": 4200},
        {"kind": "permission", "waited_ms": 12000},
        {"kind": "tool_result", "duration_ms": 812},
        {"kind": "model_call", "duration_ms": 900, "backoff_ms": 3000},
        {"kind": "run_finished", "duration_ms": 21000},
    ]))

    line = capsys.readouterr().out
    assert "模型 5.1s" in line
    assert "工具 812ms" in line             # 秒以下是整数毫秒：几十毫秒写成 0.0s 等于没说
    assert "等人审批 12.0s" in line
    assert "重试退避 3.0s" in line
    assert "未归因" in line and "回合总 21.0s" in line


def test_nothing_is_printed_when_the_log_has_no_timing_at_all(capsys):
    """旧日志没有耗时字段 —— 硬凑一行 0ms 只是噪声。"""
    _print_timing(summarize_time([{"kind": "model_call", "status": "ok"}]))

    assert capsys.readouterr().out == ""


def test_parts_that_never_happened_are_left_out(capsys):
    """没人被问过审批、也没重试过，那两项就不出现（0ms 的项是纯噪声）。"""
    _print_timing(summarize_time([
        {"kind": "model_call", "duration_ms": 1500},
        {"kind": "tool_result", "duration_ms": 40},
    ]))

    line = capsys.readouterr().out
    assert "等人审批" not in line
    assert "重试退避" not in line
