"""`--audit` 的渲染：它读的是硬盘上的日志，所以必须扛得住不完整的行。

这件事**比渲染本身重要**。`--audit` 的用途是事后排查，而最需要它的场景恰好是
日志最不干净的时候：被复制拼接、被手工编辑过、进程被杀留下半截行。`JsonlSink.read`
只保证"跳过解析失败的行"，不保证每行都有 `kind` / `ts` / `step` —— 那几个键的缺失
必须让那一行显示成占位符，而不是让整条命令崩掉。

（`summarize` / `summarize_time` 早就用了 `.get` 并写了理由；渲染层以前用下标，
所以同一个缺陷在"汇总那一半"被防住了、在"渲染这一半"没有。）
"""

import json

import pytest

from agent_runtime.audit import JsonlSink
from agent_runtime.frontends.cli import print_audit


def write_log(workdir, *records: dict) -> JsonlSink:
    sink = JsonlSink(workdir)
    path = workdir / "s.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return sink


def full(kind: str, step: int, **extra) -> dict:
    return {
        "ts": "2026-01-01T00:00:00.000", "kind": kind, "session_id": "s",
        "run_id": "a", "step": step, **extra,
    }


# --- 不完整的行 -----------------------------------------------------------

def test_a_line_missing_kind_does_not_crash(workdir):
    """修复前这里是 `KeyError: 'kind'` —— 一条破损行毁掉整次排查。"""
    sink = write_log(
        workdir,
        full("run_started", 0),
        {"session_id": "s", "run_id": "a"},                 # 缺 kind / ts / step
        full("tool_result", 1, status="ok"),
    )

    print_audit(sink, "s")                                  # 不抛就算过


def test_the_incomplete_line_is_shown_with_placeholders(workdir, capsys):
    """那一行仍然要打得出来 —— 重点是**缺什么显示成什么**，而不是整行消失。

    让缺字段的行走掉等于隐瞒"这里有一条读不懂的记录"，而排查时最需要知道的就是
    "日志里有东西我没看明白"。
    """
    sink = write_log(workdir, {"session_id": "s"}, full("tool_result", 1, status="ok"))

    print_audit(sink, "s")
    out = capsys.readouterr().out

    assert "?" in out
    assert "tool_result" in out                             # 好行照常渲染


@pytest.mark.parametrize("missing", ["ts", "kind", "step"])
def test_each_rendered_field_is_optional(workdir, capsys, missing):
    """ts / kind / step 各自都可能缺 —— 逐项钉住，免得只修了最常见的那一个。"""
    record = full("model_call", 2, status="ok")
    del record[missing]

    print_audit(write_log(workdir, record), "s")            # 不抛就算过
    assert capsys.readouterr().out


def test_a_torn_json_line_is_skipped_and_the_rest_still_renders(workdir, capsys):
    """半截 JSON 行由 read 跳过（设计内的情形），它不该影响别的行。"""
    sink = JsonlSink(workdir)
    (workdir / "s.jsonl").write_text(
        json.dumps(full("run_started", 0)) + "\n"
        + '{"kind": "torn", "ts": "2026' + "\n"             # 进程被杀留下的半截
        + json.dumps(full("run_finished", 1, stop_reason="answered")) + "\n",
        encoding="utf-8",
    )

    print_audit(sink, "s")
    out = capsys.readouterr().out

    assert "run_started" in out
    assert "run_finished" in out
    assert "torn" not in out


# --- 汇总那一半跟着一起扛 ------------------------------------------------

def test_the_summary_also_survives_incomplete_lines(workdir, capsys):
    """渲染和汇总读的是同一批事件 —— 两边都不能被同一条破损行带崩。"""
    sink = write_log(
        workdir,
        {"session_id": "s"},                                # 缺 kind
        full("tool_result", 1, status="ok"),
    )

    print_audit(sink, "s")
    out = capsys.readouterr().out

    assert "工具调用 1 次" in out
    assert "ok=1" in out


def test_no_events_still_reports_plainly(workdir, capsys):
    """空日志（含"文件不存在"）走的是另一条分支 —— 顺手钉住它没被改坏。"""
    print_audit(JsonlSink(workdir), "nope")
    assert "没有审计记录" in capsys.readouterr().out
