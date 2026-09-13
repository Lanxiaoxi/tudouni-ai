"""从审计日志里把「一轮任务花了多久、花在哪」数出来。

这是性能分析的上半段（**自顶向下、用真实数据**）：不重新计时、不猜，
只读 `.tudouni/logs/*.jsonl` 里那些已经存在的埋点。

## 为什么这条路的数字是可信的

耗时归因的口径**不在这个脚本里**，而在 `frontends/cli/__init__.py` 的
`summarize_time()` —— 那个函数是 `/status` 里那行汇总用的，也是唯一被测试
（`tests/test_timing_report.py`）盯着的口径。这里直接 import 它按 run 分组调用，
所以：

  * 报告里的数字和用户在界面上看到的那行汇总是**同一个算法**，不存在
    "分析脚本算出来一套、产品显示另一套"；
  * 口径将来变了（比如又加了一种埋点），这个脚本跟着变，不用改。

口径本身只有一句话：**每一项都是互不重叠的一段**，所以能直接相加，
剩下的就是 `未归因`——会话落盘、事件写入、载荷拼装、策略判定那些没被单独埋点的部分。
而 `未归因` 正是"这段程序的执行耗时"里**我们自己的那一份**。

## 怎么读结果

    轮（run）  = 用户说一句话到给出最终回复
    步（step） = 轮内部的一次模型往返（+ 它要求的那些工具调用）

用户要的那个场景（自动模式、没人审批）在日志里的判据是
`permission.outcome == "autopilot"` —— 见 security/gate.py 里那张来路表。
本脚本按这个把会话分成两类，因为"有没有人在场"会让耗时结构完全不同。

用法：

    .venv\\Scripts\\python.exe scripts\\perf_audit_report.py
    .venv\\Scripts\\python.exe scripts\\perf_audit_report.py --log .tudouni/logs/xxx.jsonl
    .venv\\Scripts\\python.exe scripts\\perf_audit_report.py --out .perf-tmp/audit-report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR.parent))

from agent_runtime.frontends.cli import summarize_time  # noqa: E402

DEFAULT_LOG_DIR = PROJECT_DIR / ".tudouni" / "logs"


# --- 读日志 -----------------------------------------------------------------

def read_events(path: Path) -> list[dict]:
    """读一份 jsonl。**半截行直接跳过** —— 那是进程被杀的产物，属于设计内情形。

    和 `audit/jsonl.py` 的 `read()` 同一条规矩；这里是脚本侧的第二份实现，
    因为那个方法是挂在 JsonlSink 的**实例**上的（要先知道 session_id），
    而这里要按文件路径读一堆日志。
    """
    events: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def find_logs(explicit: list[str]) -> list[Path]:
    if explicit:
        return [Path(p) for p in explicit]
    return sorted(DEFAULT_LOG_DIR.glob("*.jsonl"))


# --- 按轮切分 ---------------------------------------------------------------

class Run:
    """一轮任务：一次 run_started 到它对应的 run_finished。"""

    __slots__ = ("session_id", "run_id", "events")

    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.events: list[dict] = []

    # -- 基本形状 --

    @property
    def finished(self) -> bool:
        """有没有收尾事件。**没收尾的轮要单独说**：它可能是"进程被杀在半路"，
        把它混进统计里会让平均值失真（run_finished 缺失 → run_ms 记 0）。
        """
        return any(e.get("kind") == "run_finished" for e in self.events)

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("kind") == kind]

    @property
    def steps(self) -> int:
        """这一轮走了几步 = model_call 里出现过的最大 step。

        不数 `len(model_call)`：重试会让一步产生多条 model_call，而那一条
        "重试也算一步"是个假话（它没产生任何历史）。step 字段才是"第几步"。
        """
        calls = self.of("model_call")
        return max((e.get("step", 0) for e in calls), default=0)

    @property
    def stop_reason(self) -> str:
        fin = self.of("run_finished")
        return fin[-1].get("stop_reason", "?") if fin else "（未收尾）"

    @property
    def tool_calls(self) -> int:
        return len(self.of("tool_call"))

    @property
    def tool_result_chars(self) -> int:
        """这一轮回灌给模型的字符数 —— 它是**上下文增长的来源**。

        审计里没有 messages 的大小，但工具结果就是那个增量的绝大部分
        （assistant 正文和工具参数都是百字节量级，read_file 的结果是几十万）。
        所以它是"上下文涨得多快"最好的可得代理量。
        """
        return sum(e.get("chars", 0) for e in self.of("tool_result"))

    @property
    def max_tool_result_chars(self) -> int:
        return max((e.get("chars", 0) for e in self.of("tool_result")), default=0)

    @property
    def approval_outcomes(self) -> Counter:
        return Counter(e.get("outcome", "?") for e in self.of("permission"))

    @property
    def human_waits(self) -> int:
        """这一轮真的问过人几次（审批 + 提问）。

        它是"自动模式"的判据：0 次就是没人被打断过。
        """
        approvals = sum(1 for e in self.of("permission") if e.get("waited_ms"))
        questions = sum(1 for e in self.of("tool_result") if e.get("human_wait_ms"))
        return approvals + questions

    @property
    def retries(self) -> int:
        return sum(1 for e in self.of("model_call") if e.get("attempt", 1) > 1)

    def timing(self):
        """交给产品自己的那份口径去算 —— 见模块 docstring。"""
        return summarize_time(self.events)

    def user_input(self) -> str:
        started = self.of("run_started")
        return (started[0].get("user_input", "") if started else "")[:60]


def split_runs(events: list[dict]) -> list[Run]:
    """把一条事件流切成一轮一轮。

    键取 (session_id, run_id) 而不是只有 run_id：run_id 是 8 位十六进制，
    跨会话撞上的概率不为零（而且日志本来就可能被拼接在一起，那正是 session_id
    每行都带的原因）。
    """
    runs: dict[tuple[str, str], Run] = {}
    order: list[tuple[str, str]] = []
    for e in events:
        key = (e.get("session_id", "?"), e.get("run_id", "?"))
        if key not in runs:
            runs[key] = Run(*key)
            order.append(key)
        runs[key].events.append(e)
    return [runs[k] for k in order]


# --- 统计 -------------------------------------------------------------------

def pct(values: list[int], q: float) -> int:
    """分位数。样本少的时候用 nearest-rank，不插值 —— 报"p90 = 5312.4ms"
    会让人以为有小数点那么精确，而我们手上只有 33 个样本。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def bucket_runs(runs: list[Run]) -> dict[str, list[Run]]:
    """按"这一轮有没有人被问到"分类。

    这两类的耗时结构**不可比**：等人那一项可以是任意大，而用户问的正是
    "没有人工审批时间的情况下"那一类。混在一起平均等于用一个不存在的场景
    去描述真实场景。
    """
    groups: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        groups["unattended" if r.human_waits == 0 else "attended"].append(r)
    return dict(groups)


def summarize(runs: list[Run]) -> dict:
    timings = [r.timing() for r in runs]
    steps = [r.steps for r in runs]

    def total(attr: str) -> int:
        return sum(getattr(t, attr) for t in timings)

    return {
        "runs": len(runs),
        "steps": sum(steps),
        "wall_ms": total("run_ms"),
        "model_ms": total("model_ms"),
        "tool_ms": total("tool_ms"),
        "waited_ms": total("waited_ms"),
        "human_ms": total("human_ms"),
        "backoff_ms": total("backoff_ms"),
        "unattributed_ms": total("unattributed_ms"),
        "tool_calls": sum(r.tool_calls for r in runs),
        "tool_result_chars": sum(r.tool_result_chars for r in runs),
        "retries": sum(r.retries for r in runs),
        "max_steps": max(steps, default=0),
    }


def model_latency_stats(runs: list[Run]) -> dict:
    """模型往返的分布。**它是整个耗时的主项，所以要先把它量出来。**
    分开成功与失败：一次失败的重试往返和一次成功往返对用户意义不同。
    """
    ok, failed, backoff = [], [], 0
    for r in runs:
        for e in r.of("model_call"):
            (ok if e.get("status") == "ok" else failed).append(e.get("duration_ms", 0))
            backoff += e.get("backoff_ms", 0) or 0
    return {
        "ok_count": len(ok),
        "ok_sum": sum(ok),
        "ok_mean": int(statistics.fmean(ok)) if ok else 0,
        "ok_p50": pct(ok, 0.50),
        "ok_p90": pct(ok, 0.90),
        "ok_max": max(ok, default=0),
        "failed_count": len(failed),
        "failed_sum": sum(failed),
        "backoff_ms": backoff,
    }


def tool_stats(runs: list[Run]) -> list[tuple[str, int, int, int, int]]:
    """每个工具：次数 / 总耗时 / 最大耗时 / 总回灌字符 / 最大回灌字符。

    **回灌字符和耗时并列**，因为它们各自解释一件事：耗时是"这一步卡在哪"，
    字符是"上下文因此涨了多少"（后者才是后面每一步都变贵的那个原因）。
    """
    agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "ms": 0, "max_ms": 0, "chars": 0, "max_chars": 0, "errors": 0}
    )
    for r in runs:
        for e in r.of("tool_result"):
            a = agg[e.get("tool", "?")]
            a["n"] += 1
            a["ms"] += e.get("duration_ms", 0)
            a["max_ms"] = max(a["max_ms"], e.get("duration_ms", 0))
            a["chars"] += e.get("chars", 0)
            a["max_chars"] = max(a["max_chars"], e.get("chars", 0))
            if e.get("status") != "ok":
                a["errors"] += 1
    rows = [
        (name, a["n"], a["ms"], a["max_ms"], a["chars"], a["max_chars"], a["errors"])
        for name, a in agg.items()
    ]
    return sorted(rows, key=lambda row: row[2], reverse=True)


# --- 输出 -------------------------------------------------------------------

def _fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}min"


def _share(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def render(lines: list[str], groups: dict[str, list[Run]], logs: list[Path]) -> None:
    def out(text: str = "") -> None:
        lines.append(text)

    out("# 审计日志耗时归因（真实数据）")
    out()
    out(f"日志文件：{len(logs)} 份")
    total_runs = sum(len(v) for v in groups.values())
    out(f"可用回合：{total_runs}")
    out()

    for name, label in (("unattended", "自动模式（全程没有人被问到）"),
                        ("attended", "有人参与的回合（含等待，仅供参考）")):
        runs = groups.get(name, [])
        if not runs:
            continue
        s = summarize(runs)
        out(f"## {label}")
        out()
        out(f"回合 {s['runs']} 个，累计 {s['steps']} 步"
            f"（平均每回合 {s['steps'] / s['runs']:.1f} 步，最长 {s['max_steps']} 步），"
            f"工具调用 {s['tool_calls']} 次，重试 {s['retries']} 次。")
        out()
        out("| 分项 | 合计 | 占墙上时间 | 说明 |")
        out("|---|---:|---:|---|")
        for key, desc in (
            ("model_ms", "模型往返（网络 + 生成）"),
            ("tool_ms", "工具执行"),
            ("backoff_ms", "重试退避（纯等待）"),
            ("waited_ms", "等人审批"),
            ("human_ms", "等人回答提问"),
            ("unattributed_ms", "**未归因 = 本程序的自身开销**"),
        ):
            v = s[key]
            out(f"| {desc} | {_fmt_ms(v)} | {_share(v, s['wall_ms'])} | |")
        out(f"| **合计** | **{_fmt_ms(s['wall_ms'])}** | 100% | |")
        out()
        if s["steps"]:
            self_ms = s["unattributed_ms"]
            out(f"- **每一步的自身开销**：{self_ms / s['steps']:.1f}ms"
                f"（未归因合计 ÷ 步数）")
            out(f"- **每次模型往返**：平均 {s['model_ms'] / max(1, s['steps']):.0f}ms")
            if s["tool_calls"]:
                out(f"- 每次工具调用：平均 {s['tool_ms'] / s['tool_calls']:.1f}ms")
            out(f"- 工具回灌字符合计：{s['tool_result_chars']:,}")
        out()

        lat = model_latency_stats(runs)
        out(f"模型往返分布（成功 {lat['ok_count']} 次）："
            f"均值 {lat['ok_mean']}ms，p50 {lat['ok_p50']}ms，p90 {lat['ok_p90']}ms，"
            f"最大 {lat['ok_max']}ms。")
        if lat["failed_count"]:
            out(f"失败往返 {lat['failed_count']} 次，合计 {_fmt_ms(lat['failed_sum'])}，"
                f"退避合计 {_fmt_ms(lat['backoff_ms'])}。")
        out()

        rows = tool_stats(runs)
        if rows:
            out("| 工具 | 次数 | 合计耗时 | 最大单次 | 合计回灌字符 | 最大单次字符 | 失败 |")
            out("|---|---:|---:|---:|---:|---:|---:|")
            for (n, cnt, ms, mx, chars, mxc, errs) in rows:
                out(f"| {n} | {cnt} | {_fmt_ms(ms)} | {_fmt_ms(mx)} | "
                    f"{chars:,} | {mxc:,} | {errs} |")
            out()

        out("### 逐回合明细")
        out()
        out("| 会话 | run | 步 | 工具 | 墙上时间 | 模型 | 工具 | 未归因 | 停止原因 |")
        out("|---|---|---:|---:|---:|---:|---:|---:|---|")
        for r in sorted(runs, key=lambda r: r.timing().run_ms, reverse=True):
            t = r.timing()
            out(f"| {r.session_id} | {r.run_id} | {r.steps} | {r.tool_calls} | "
                f"{_fmt_ms(t.run_ms)} | {_fmt_ms(t.model_ms)} | {_fmt_ms(t.tool_ms)} | "
                f"{_fmt_ms(t.unattributed_ms)} | {r.stop_reason} |")
        out()


def main() -> int:
    parser = argparse.ArgumentParser(description="审计日志耗时归因")
    parser.add_argument("--log", action="append", default=[],
                        help="指定日志文件，可重复；默认读 .tudouni/logs/*.jsonl")
    parser.add_argument("--out", default="", help="报告写到哪个文件（UTF-8）；默认打到 stdout")
    args = parser.parse_args()

    logs = find_logs(args.log)
    if not logs:
        print(f"没找到审计日志（看过 {DEFAULT_LOG_DIR}）", file=sys.stderr)
        return 2

    # 显式指定的文件不存在时**说清楚是哪几个**，而不是让 `path.open()` 抛裸的
    # FileNotFoundError —— 那个 traceback 里的路径会被 PowerShell 的报错包装吃掉一半，
    # 看起来像脚本自己坏了。（这条是实测踩出来的：日志被清空之后 `--log` 指向旧文件。）
    missing = [p for p in logs if not p.exists()]
    if missing:
        for path in missing:
            print(f"日志文件不存在：{path}", file=sys.stderr)
        return 2

    events: list[dict] = []
    for path in logs:
        events.extend(read_events(path))

    runs = [r for r in split_runs(events) if r.finished]
    unfinished = len(split_runs(events)) - len(runs)

    lines: list[str] = []
    render(lines, bucket_runs(runs), logs)
    if unfinished:
        lines.append(f"> 另有 {unfinished} 个回合没有 run_finished（进程被杀在半路），"
                     "已排除在统计之外。")
        lines.append("")

    text = "\n".join(lines)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"报告已写入 {out_path}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
