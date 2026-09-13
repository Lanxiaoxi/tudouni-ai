"""零延迟基准：把 runtime **自身**的开销从模型网络往返里剥出来。

这是性能分析的下半段（**自底向上、可复现**）。

## 为什么需要它

审计日志（见 `perf_audit_report.py`）能告诉你"这一轮花在哪"，但它有一个死角：
墙上时间里 95%+ 是模型往返，而那是**网络和 provider 的事，改代码改不动**。
真正属于这份代码、也真正能被优化的是剩下那一小块 —— 而它小到会被
毫秒取整、被网络抖动整个淹掉。

所以这个脚本换一把尺子：**把模型换成一个零延迟的假模型**，于是墙上时间里
只剩下这份程序自己做的事。

## 分段口径：**互不重叠**，这是这份报告能相加的前提

`summarize_time()` 踩过的那个坑（工具耗时一度把等人的时间也算了进去，于是
"模型 + 工具"相加会重复计费）在这里同样成立，所以分段一律取"顶层的那三段"：

    模型阶段    Agent._complete_with_retry   —— 拼载荷、发请求、收响应、记审计
    工具阶段    Agent._run_batch             —— 裁决 + 执行 + 记审计
    落盘阶段    Agent._checkpoint            —— 整份会话重新序列化 + 原子替换
    其余        wall − 上面三段             —— 轮首轮尾、消息追加、事件构造……

`审计写入` / `工具执行` / `载荷尾部拼装` 是**嵌套在**这三段里的细项，单独列出来
是为了回答"那一段为什么贵"，**不能和上面三段相加**。

于是：**自身开销 = 墙上时间 − 工具执行**。减去它是因为它是真实工作，
不是这份程序的开销；而它在 autopilot 下也没有等人那一项（见下）。

## 它量的是哪个场景

用户问的那个：**自动模式、没有人工审批**。所以 Agent 的 `autopilot=True`，
`asker=None` —— 走 security/gate.py 里那条 `outcome="autopilot"` 的短路，
一次审批都不会弹。权限策略给**空名单**（`PermissionPolicy()`，最严的一档），
而 autopilot 排在它前面：所以这个组合恰好证明"路过了裁决、但一次都没问人"
（报告里会打印实际出现的 outcome，`autopilot` 就是证据）。

**这不是微基准。** 跑的是真的 `Agent.run()`、真的工具注册表、真的 pydantic 校验、
真的会话落盘和审计写入。假掉的只有网络那一跳。

## 怎么读结果

要看的是 **每步自身开销随上下文增长的那条曲线**。它是超线性的，原因很具体：
`JsonSessionStore.save()` 每次 checkpoint 都把**整份会话**重新序列化一遍
（`json.dumps(..., indent=2)`），而 checkpoint 每步一次（agent.py 里那个 ★）。
于是历史越长，"存一次"越贵 —— 而历史只会越来越长。

`--payload` 控制每步工具回灌多少字节，就是在控制这条曲线有多陡。

用法：

    .venv\\Scripts\\python.exe scripts\\perf_selfcost.py
    .venv\\Scripts\\python.exe scripts\\perf_selfcost.py --payload 1024,65536 --steps 12
    .venv\\Scripts\\python.exe scripts\\perf_selfcost.py --profile
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR.parent))

from agent_runtime.agents.agent import Agent  # noqa: E402
from agent_runtime.audit.jsonl import JsonlSink  # noqa: E402
from agent_runtime.models.base import ChatModel  # noqa: E402
from agent_runtime.models.types import ModelResponse, TokenUsage  # noqa: E402
from agent_runtime.security.policy import PermissionPolicy  # noqa: E402
from agent_runtime.state.session import Session  # noqa: E402
from agent_runtime.state.store import JsonSessionStore  # noqa: E402
from agent_runtime.tools.builtin import create_tool_registry  # noqa: E402

TMP_DIR = PROJECT_DIR / ".perf-tmp"
WORK_DIR = TMP_DIR / "work"
STATE_DIR = TMP_DIR / "state"
LOG_DIR = TMP_DIR / "logs"

# 三个顶层分段的名字。**它们互不重叠**，所以能相加、也能拿墙上时间去减。
PHASE_REQUEST = "模型阶段"
PHASE_TOOLS = "工具阶段"
PHASE_PERSIST = "落盘阶段"
PHASES = (PHASE_REQUEST, PHASE_TOOLS, PHASE_PERSIST)


# --- 假模型 -----------------------------------------------------------------

def tool_call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> dict[str, str]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


class InstantModel(ChatModel):
    """按剧本返回，**不做任何网络、不做任何等待**。

    签名里必须有 `on_delta` / `on_attempt_started`：`agents/retry.py` 的
    `_accepts_streaming_kwargs` 是按**签名**判断"这个模型认不认流式参数"的，
    少了它们，基准里就永远走不到流式那条路 —— 而那是一条真实存在的路
    （TUI 默认就开着它）。

    `chunks` 不为 0 时按块吐正文，用来量 `_DeltaRelay` 那一层的开销
    （每块一次 `should_stop` 查询 + 一次 sink 调用）。
    """

    def __init__(self, script: list[ModelResponse], *, chunks: int = 0) -> None:
        self.script = list(script)
        self.chunks = chunks
        self.calls = 0
        self.seen_message_counts: list[int] = []

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        self.calls += 1
        self.seen_message_counts.append(len(messages))
        response = self.script.pop(0) if self.script else ModelResponse(content="(剧本用尽)")

        if on_delta is not None and self.chunks and response.content:
            text = response.content
            size = max(1, len(text) // self.chunks)
            for start in range(0, len(text), size):
                on_delta(text=text[start:start + size])
            response.streamed = True
        return response


def build_script(rounds: int, steps: int, payload_file: str) -> list[ModelResponse]:
    """造剧本：每轮前 steps-1 步调 read_file，最后一步给出正文收尾。

    **每步都读同一个文件**是有意的：它模拟的是真实会话里那种形状
    （read_file 的结果是上下文增量的绝大部分），而且让"上下文涨多快"变成一个
    可以直接控制的旋钮（--payload）。
    """
    script: list[ModelResponse] = []
    for _ in range(rounds):
        for _ in range(max(1, steps - 1)):
            script.append(ModelResponse(
                content=None,
                tool_calls=[tool_call("read_file", {"path": payload_file})],
                usage=TokenUsage(prompt_tokens=1000, cached_tokens=900, completion_tokens=20),
            ))
        script.append(ModelResponse(
            content="任务完成。" + "x" * 400,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=900, completion_tokens=300),
        ))
    return script


# --- 探针 -------------------------------------------------------------------

class Probe:
    """累加各分段的耗时。单位统一成毫秒 —— 报告里没有第二把尺子。"""

    def __init__(self) -> None:
        self.ms: dict[str, float] = defaultdict(float)
        self.count: dict[str, int] = defaultdict(int)
        self.bytes_written = 0        # Σ 每次 checkpoint 落盘的文件大小
        self.checkpoints = 0

    def add(self, name: str, seconds: float) -> None:
        self.ms[name] += seconds * 1000.0
        self.count[name] += 1

    def phase_total(self) -> float:
        """三个顶层分段之和。`其余` 由墙上时间减它得到。"""
        return sum(self.ms[name] for name in PHASES)


class InstrumentedAgent(Agent):
    """只加计时，不改一行行为。

    用**子类**而不是 monkeypatch：覆盖的都是本来就被 `self.` 调用的钩子，
    所以"测的就是跑的那份代码"这句话在类型上也成立。
    """

    def __init__(self, *args: Any, probe: Probe, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.probe = probe

    def _complete_with_retry(self, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return super()._complete_with_retry(*args, **kwargs)
        finally:
            self.probe.add(PHASE_REQUEST, time.perf_counter() - started)

    def _run_batch(self, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return super()._run_batch(*args, **kwargs)
        finally:
            self.probe.add(PHASE_TOOLS, time.perf_counter() - started)

    def _checkpoint(self, session: Session) -> None:
        started = time.perf_counter()
        try:
            super()._checkpoint(session)
        finally:
            self.probe.add(PHASE_PERSIST, time.perf_counter() - started)

    def _status_note(self, *args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return super()._status_note(*args, **kwargs)
        finally:
            self.probe.add("载荷尾部拼装", time.perf_counter() - started)


# --- 装配 -------------------------------------------------------------------

def make_payload(path: Path, size: int) -> int:
    """造一份**恰好 size 字节**的合法 UTF-8 文本文件，返回实际字节数。

    按整行拼、再拿 ASCII 补齐，而不是 `encode()[:size]` —— 后者会切在多字节
    字符中间，`read_file` 当场抛 UnicodeDecodeError。那个失败很隐蔽：
    工具"执行失败"仍然是一步、仍然有结果文本，基准照跑不误，
    于是量出来的是一条**没有大 payload** 的曲线。
    """
    line = "def handler(request, context):  # 处理一次请求，返回纯文本\n"
    line_bytes = len(line.encode("utf-8"))
    data = (line * (size // line_bytes)).encode("utf-8")
    if len(data) < size:
        data += b"x" * (size - len(data))
    path.write_bytes(data[:size])
    return len(data[:size])


def run_scenario(
    *,
    rounds: int,
    steps: int,
    payload_bytes: int,
    checkpoint: bool = True,
    audit: bool = True,
    stream: bool = False,
    chunks: int = 0,
    label: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """跑一个场景，返回它的耗时剖面。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    payload_name = "payload.txt"
    actual_bytes = make_payload(WORK_DIR / payload_name, payload_bytes)

    # store 先建：**状态文件的路径要从 store 自己问**（`_path`），不能在脚本里
    # 手拼一个后缀 —— 那样磁盘格式一改（比如这次 `.json` → `.jsonl`），
    # 脚本就会静默地量一个**根本不存在的文件**，所有落盘指标变成 0。
    store = JsonSessionStore(STATE_DIR)
    session = Session.new(session_id or "perf", workspace=str(WORK_DIR))
    state_path = store._path(session.session_id)
    # 这个基准跨轮复用同一个 state 目录和同一批 session id，所以要先把上一轮
    # 留下的同名文件清掉。不清的话 store 会正确地报"消息从 N 条变成了 M 条"
    # —— 那是它的校验在干活，不是它坏了。
    state_path.unlink(missing_ok=True)

    probe = Probe()
    collected: list[dict] = []

    # -- 工具：每个 handler 单独计时（工具执行是"真实工作"，不算自身开销） --
    registry = create_tool_registry(str(WORK_DIR))
    for tool in registry.all():
        inner = tool.handler

        def timed(*args: Any, _name: str = tool.name, _inner=inner, **kwargs: Any):
            started = time.perf_counter()
            try:
                return _inner(*args, **kwargs)
            finally:
                probe.add(f"工具执行 {_name}", time.perf_counter() - started)

        registry._tools[tool.name] = replace(tool, handler=timed)

    original_schemas = registry.schemas

    def timed_schemas(*args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return original_schemas(*args, **kwargs)
        finally:
            probe.add("工具 schema 生成", time.perf_counter() - started)

    registry.schemas = timed_schemas  # type: ignore[method-assign]

    # -- 落盘：计时 + 记录每次写出去多少字节 --
    def on_checkpoint(sess: Session) -> None:
        # **必须取增量，不能取文件大小。** 落盘现在是只追加的（state/store.py），
        # 所以 `st_size` 说的是"这份会话现在多大"，不是"这一次写了多少"。
        # 取错的话这个指标会**照着整份重写时代的公式**继续报几十倍的放大 ——
        # 也就是一个只看得见旧世界、对新世界完全失明的度量。
        before = state_path.stat().st_size if state_path.exists() else 0
        store.save(sess)
        probe.checkpoints += 1
        try:
            probe.bytes_written += state_path.stat().st_size - before
        except OSError:
            pass

    # -- 审计：计时 --
    sink = JsonlSink(LOG_DIR)

    def on_event(record: dict) -> None:
        started = time.perf_counter()
        try:
            sink(record)
        finally:
            probe.add("审计写入", time.perf_counter() - started)
        collected.append(record)

    model = InstantModel(build_script(rounds, steps, payload_name), chunks=chunks)

    agent = InstrumentedAgent(
        model=model,
        tools=registry,
        # 空名单 = 最严的策略（什么都要问）；而 autopilot 排在它前面短路，
        # 所以这个组合同时证明了两件事：一次都没问人，判定确实走的是 autopilot。
        policy=PermissionPolicy(),
        asker=None,
        memory=None,
        on_checkpoint=on_checkpoint if checkpoint else None,
        on_event=on_event if audit else None,
        on_delta=(lambda **_: None) if stream else None,
        autopilot=True,
        probe=probe,
    )

    round_ms: list[float] = []
    wall_started = time.perf_counter()
    for index in range(rounds):
        started = time.perf_counter()
        agent.run(session, f"第 {index + 1} 轮：看看这个文件。", max_steps=steps)
        round_ms.append((time.perf_counter() - started) * 1000.0)
    wall_ms = (time.perf_counter() - wall_started) * 1000.0

    handler_ms = sum(v for k, v in probe.ms.items() if k.startswith("工具执行 "))
    tool_chars = sum(e.get("chars", 0) for e in collected if e.get("kind") == "tool_result")
    final_bytes = state_path.stat().st_size if state_path.exists() else 0

    return {
        "label": label,
        "rounds": rounds,
        "steps": steps,
        "payload_bytes": actual_bytes,
        "wall_ms": wall_ms,
        "round_ms": round_ms,
        "model_calls": model.calls,
        "message_counts": model.seen_message_counts,
        "probe_ms": dict(probe.ms),
        "self_ms": wall_ms - handler_ms,
        "handler_ms": handler_ms,
        "residual_ms": wall_ms - probe.phase_total(),
        "checkpoints": probe.checkpoints,
        "bytes_written": probe.bytes_written,
        "final_bytes": final_bytes,
        "tool_chars": tool_chars,
        "events": len(collected),
        "permission_outcomes": sorted({
            e.get("outcome") for e in collected if e.get("kind") == "permission"
        }),
    }


# --- 报告 -------------------------------------------------------------------

def _ms(value: float) -> str:
    if abs(value) < 1000:
        return f"{value:.1f}ms"
    return f"{value / 1000:.2f}s"


def render_report(results: list[dict], profile_text: str = "") -> str:
    out: list[str] = []
    out.append("# runtime 自身开销基准（零延迟假模型，autopilot）")
    out.append("")
    out.append("模型换成零延迟假模型，所以**墙上时间 ≈ 这份程序自己花的时间**。")
    out.append("工具执行是「真实工作」，单列；其余都算这份程序的开销。")
    out.append("")

    outcomes = sorted({o for r in results for o in r["permission_outcomes"]})
    out.append(f"权限裁决实际出现的来路：`{outcomes}` —— 只有 `autopilot`，"
               "也就是**一次都没问人**。")
    out.append("")

    out.append("## 主表（顶层三段互不重叠，`其余` = 墙上 − 三段）")
    out.append("")
    out.append("| 场景 | 轮×步 | 回灌/步 | 墙上时间 | **每步自身开销** | "
               "模型阶段 | 工具阶段 | 落盘阶段 | 其余 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in results:
        total_steps = r["rounds"] * r["steps"]
        p = r["probe_ms"]
        out.append(
            f"| {r['label']} | {r['rounds']}×{r['steps']} | {r['payload_bytes'] // 1024}KB "
            f"| {_ms(r['wall_ms'])} | **{_ms(r['self_ms'] / total_steps)}** "
            f"| {_ms(p.get(PHASE_REQUEST, 0.0))} | {_ms(p.get(PHASE_TOOLS, 0.0))} "
            f"| {_ms(p.get(PHASE_PERSIST, 0.0))} | {_ms(r['residual_ms'])} |"
        )
    out.append("")
    out.append("> `其余` 含轮首轮尾的固定开销（工具 schema 生成、run_started 事件、"
               "消息追加）。它在短回合里占比可观，长回合里被摊薄。")
    out.append("")

    out.append("## 嵌套细项（**不能**和上面三段相加）")
    out.append("")
    out.append("| 场景 | 工具执行 | 会话落盘次数 | 审计写入 | 载荷尾部拼装 | schema 生成 |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for r in results:
        p = r["probe_ms"]
        out.append(
            f"| {r['label']} | {_ms(r['handler_ms'])} | {r['checkpoints']} "
            f"| {_ms(p.get('审计写入', 0.0))} | {_ms(p.get('载荷尾部拼装', 0.0))} "
            f"| {_ms(p.get('工具 schema 生成', 0.0))} |"
        )
    out.append("")

    out.append("## 写入放大")
    out.append("")
    out.append("会话文件现在是**只追加**的（`state/store.py`），所以「累计落盘字节 ÷ "
               "最终会话大小」应当逼近 **1×**：每一段历史只被写过一次。")
    out.append("")
    out.append("**这个数掉回几十倍就说明落盘退回了整份重写** —— 它是这次改动唯一的"
               "观测点，也是唯一会在功能测试全绿的情况下悄悄退化的东西。")
    out.append("")
    out.append("| 场景 | checkpoint 次数 | 累计落盘 | 最终大小 | 放大倍数 |")
    out.append("|---|---:|---:|---:|---:|")
    for r in results:
        final = max(1, r["final_bytes"])
        out.append(
            f"| {r['label']} | {r['checkpoints']} | {r['bytes_written'] / 1e6:.1f}MB "
            f"| {final / 1e6:.2f}MB | {r['bytes_written'] / final:.1f}× |"
        )
    out.append("")

    out.append("## 逐轮分解")
    out.append("")
    out.append("同一份会话，后面几轮的起点已经是前面几轮**全部的历史** ——"
               "所以轮与轮之间的耗时不是常数。")
    out.append("")
    for r in results:
        if r["rounds"] < 2:
            continue
        out.append(f"- **{r['label']}**：" + " → ".join(_ms(ms) for ms in r["round_ms"]))
    out.append("")

    if profile_text:
        out.append("## cProfile 热点（按 tottime 排序）")
        out.append("")
        out.append("它和主表的数字**不可比**（cProfile 自己会给每个调用记账），"
                   "只用来回答「热点在哪个函数上」。")
        out.append("")
        out.append("```")
        out.append(profile_text.rstrip())
        out.append("```")
        out.append("")

    return "\n".join(out)


# --- 入口 -------------------------------------------------------------------

DEFAULT_PAYLOADS = [1024, 64 * 1024, 256 * 1024]
DEFAULT_STEPS = [12, 36]


def profile_case(rounds: int, steps: int, payload: int) -> str:
    profiler = cProfile.Profile()
    profiler.enable()
    run_scenario(rounds=rounds, steps=steps, payload_bytes=payload,
                 label="profile", session_id="perf-profile")
    profiler.disable()

    buffer = io.StringIO()
    pstats.Stats(profiler, stream=buffer).sort_stats("tottime").print_stats(25)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="runtime 自身开销基准")
    parser.add_argument("--payload", default=",".join(str(p) for p in DEFAULT_PAYLOADS),
                        help="每步回灌的字节数，逗号分隔")
    parser.add_argument("--steps", default=",".join(str(s) for s in DEFAULT_STEPS),
                        help="每轮的步数，逗号分隔")
    parser.add_argument("--rounds", type=int, default=1, help="基线场景的轮数")
    parser.add_argument("--repeat", type=int, default=3, help="基线场景跑几遍，取中位数")
    parser.add_argument("--no-ablation", action="store_true", help="只跑基线矩阵")
    parser.add_argument("--profile", action="store_true", help="额外跑一次 cProfile")
    parser.add_argument("--out", default=str(TMP_DIR / "selfcost-report.md"))
    parser.add_argument("--keep", action="store_true", help="保留 .perf-tmp 下的临时数据")
    args = parser.parse_args()

    payloads = [int(x) for x in args.payload.split(",") if x.strip()]
    steps_list = [int(x) for x in args.steps.split(",") if x.strip()]

    if not args.keep:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    biggest_payload = max(payloads)
    biggest_steps = max(steps_list)

    matrix: list[dict[str, Any]] = []
    # 主矩阵：横轴是上下文大小，纵轴是步数 —— 两条一起让"每步越来越贵"显形。
    for payload in payloads:
        for steps in steps_list:
            matrix.append(dict(rounds=args.rounds, steps=steps, payload_bytes=payload,
                               label=f"基线 {payload // 1024}KB"))
    if not args.no_ablation:
        # 消融：把两个"每步都要做的事"各自关掉，看曲线是不是就不陡了。
        matrix.append(dict(rounds=args.rounds, steps=biggest_steps,
                           payload_bytes=biggest_payload,
                           checkpoint=False, label="消融 无落盘"))
        matrix.append(dict(rounds=args.rounds, steps=biggest_steps,
                           payload_bytes=biggest_payload,
                           audit=False, label="消融 无审计"))
        matrix.append(dict(rounds=args.rounds, steps=biggest_steps,
                           payload_bytes=biggest_payload,
                           checkpoint=False, audit=False, label="消融 无落盘无审计"))
        matrix.append(dict(rounds=args.rounds, steps=biggest_steps,
                           payload_bytes=biggest_payload,
                           stream=True, chunks=40, label="变体 开流式"))
        # 多轮：起点是上一轮的全部历史，所以轮耗时应当逐轮上升。
        matrix.append(dict(rounds=5, steps=6, payload_bytes=biggest_payload, label="多轮 5×6"))

    results: list[dict] = []
    for index, case in enumerate(matrix):
        case = dict(case, session_id=f"perf-{index}")
        repeats = max(1, args.repeat) if case["label"].startswith("基线") else 1
        runs = [run_scenario(**case) for _ in range(repeats)]
        # 取**中位数**那一遍的完整剖面，而不是逐字段取中位数 —— 后者会拼出
        # 一个从未真实发生过的组合。
        runs.sort(key=lambda r: r["wall_ms"])
        results.append(runs[len(runs) // 2])

    profile_text = profile_case(args.rounds, biggest_steps, biggest_payload) if args.profile else ""

    text = render_report(results, profile_text)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    out_path.with_suffix(".json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
