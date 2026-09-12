"""一批工具调用里的并发。

这里要钉死的不是"它变快了"，而是三条**不变量**：

  1. 串行是默认。没声明 parallel_safe 的工具、混合批次、只有一条的批次，全都走
     老路 —— 事件、messages、耗时口径和并发之前一模一样。
  2. 顺序只由模型决定。谁先跑完不影响 messages 里 tool 结果的顺序，也不影响事件的
     顺序：同一个会话跑两次，历史必须一样（它会被落盘，不可复现就等于没法排查）。
  3. 权限裁决和事件都不进线程池。asker 走 stdin，两条审批同时问会互相抢输入；
     on_event 是注入进来的实现，"它必须自己加锁"会是一条没人想得到的隐式契约。

耗时口径另有一组断言（并行的批次读的是墙上时间，不是逐条之和）—— 那是"未归因"
不被算成 0 的前提。
"""

import threading

import pytest

from agent_runtime.agents import Agent
from agent_runtime.frontends.cli import Timing, _print_timing, summarize_time
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, tool_call, usage


class LockedClock:
    """线程安全的假时钟。

    不能直接用 tests/test_timing.py 那个：它是裸的 `self.now += seconds`，而并发路径
    里推进时钟的可能是任意一个工作线程 —— 丢一次增量，断言就会随机红（而且红得毫无
    规律）。时间本来就已经很难断言了，别在这里再加一份不确定性。
    """

    def __init__(self):
        self.now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class ThreadRecordingCollector(Collector):
    """连"事件是在哪个线程发的"一起记下来。

    on_event 的线程归属是一条契约（见 audit/jsonl.py：它每次 open/write/close 追加
    一行，多线程下没有任何互斥），所以它得能被断言，而不是靠读代码相信。
    """

    def __init__(self):
        super().__init__()
        self.threads: list[str] = []

    def __call__(self, record):
        self.threads.append(threading.current_thread().name)
        super().__call__(record)


class BatchModel(ChatModel):
    """第一步给出一整批调用，第二步收尾。"""

    def __init__(self, calls: list[dict]):
        self.calls = list(calls)
        self.sent = False

    def complete(self, messages, tools=None):
        if self.sent:
            return ModelResponse(content="完成", usage=usage())
        self.sent = True
        return ModelResponse(content=None, tool_calls=list(self.calls), usage=usage())


def batch_of(*name_and_path: tuple[str, str]) -> BatchModel:
    return BatchModel([
        tool_call(name, {"path": path}, f"c{i}")
        for i, (name, path) in enumerate(name_and_path)
    ])


def registry_with(
    handler, *, parallel_safe: bool = False, risk: RiskLevel = RiskLevel.LOW,
    name: str = "list_files",
) -> tuple[ToolRegistry, list[dict]]:
    """一个只有一个工具的注册表，并记下 handler 收到的每一次参数。"""
    calls: list[dict] = []

    def wrapper(**kwargs):
        calls.append(kwargs)
        return handler(**kwargs)

    registry = ToolRegistry()
    registry.register(Tool(
        name=name,
        description="列出目录",
        risk=risk,
        args_model=ListFilesArgs,
        handler=wrapper,
        parallel_safe=parallel_safe,
    ))
    return registry, calls


def run_agent(registry, model, clock=None, **kwargs):
    collector = ThreadRecordingCollector()
    session = Session.new("s")
    agent = Agent(
        model, registry, PermissionPolicy({RiskLevel.LOW}),
        on_event=collector, clock=clock or LockedClock(), **kwargs,
    )
    agent.run(session, "看看这些")
    return session, collector


def tool_texts(session) -> list[str]:
    return [m["content"] for m in session.messages if m["role"] == "tool"]


# --- 注册期的那条约束 -----------------------------------------------------

def test_parallel_safe_requires_low_risk():
    """能并行的前提之一是批内不弹审批，所以这条组合在注册时就该炸。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="parallel_safe"):
        registry.register(Tool(
            name="write_file", description="写文件", risk=RiskLevel.MEDIUM,
            args_model=ListFilesArgs, handler=lambda **kwargs: "ok", parallel_safe=True,
        ))


def test_the_real_registry_only_marks_the_read_only_tools():
    """把"哪些工具敢放并行"钉在真正的注册表上。

    防的是将来有人顺手给 write_file / edit_file / shell 加上这个标志：那两个文件工具
    是"读进来、改一段、整份写回去"，并发调用会互相盖掉对方，而且两边都会报成功。
    """
    tools = create_tool_registry(".").all()
    assert {t.name for t in tools if t.parallel_safe} == {
        "read_file", "list_files", "get_current_time",
    }


# --- 顺序只由模型决定 -----------------------------------------------------

def test_results_keep_the_model_order_even_when_they_finish_out_of_order():
    """这一条同时证明了两件事：真的并发了，而且顺序没被调度改掉。

    第一个调用（慢）会一直等到第二个（快）跑完才放行 —— 串行执行的话这里会直接卡到
    超时，所以它不可能"假通过"。
    """
    second_ran = threading.Event()

    def handler(**kwargs):
        if kwargs["path"] == "慢":
            assert second_ran.wait(5), "第二个调用没有在第一个结束之前跑起来 —— 没有并发"
            return "慢的结果"
        second_ran.set()
        return "快的结果"

    registry, calls = registry_with(handler, parallel_safe=True)
    session, collector = run_agent(registry, batch_of(("list_files", "慢"), ("list_files", "快")))

    assert [c["path"] for c in calls] == ["慢", "快"]            # 都跑了，各一次
    assert tool_texts(session) == ["慢的结果", "快的结果"]        # messages 里是模型的顺序
    assert [m["tool_call_id"] for m in session.messages if m["role"] == "tool"] == ["c0", "c1"]
    # 事件也按模型的顺序报（两条同名工具，所以看参数）：按完成顺序发的话这里会是
    # ["快", "慢"] —— 而"这个会话里到底先跑了哪一条"从日志上就再也看不出来了。
    assert [e["arguments"] for e in collector.of("tool_call")] == [
        '{"path": "慢"}', '{"path": "快"}',
    ]


def test_a_parallel_batch_says_so_in_the_audit():
    """没有这个标记，事后从日志里分不出这一批是并发还是逐条 —— 而"为什么快"正是
    拿着日志要回答的问题。"""
    registry, _ = registry_with(lambda **kwargs: "ok", parallel_safe=True)
    _, collector = run_agent(registry, batch_of(("list_files", "a"), ("list_files", "b")))

    batch = collector.of("tool_batch")
    assert len(batch) == 1
    assert batch[0]["calls"] == 2 and "wall_ms" in batch[0]
    assert [e.get("parallel") for e in collector.of("tool_result")] == [True, True]
    # tool_call 事件的顺序就是模型给的顺序（两条同名，所以看 id 更清楚）
    assert [e["arguments"] for e in collector.of("tool_call")] == ['{"path": "a"}', '{"path": "b"}']


# --- 串行是默认 -----------------------------------------------------------

def test_tools_without_the_flag_stay_serial():
    """默认值就是"不并行"。没声明的东西一条都不许进池子。"""
    registry, _ = registry_with(lambda **kwargs: "ok")           # parallel_safe 缺省 False
    _, collector = run_agent(registry, batch_of(("list_files", "a"), ("list_files", "b")))

    assert collector.of("tool_batch") == []
    assert [e.get("parallel") for e in collector.of("tool_result")] == [None, None]


def test_one_bad_call_sends_the_whole_batch_back_to_serial():
    """混合批次整批退回串行。

    关键的形状是 [读、写、读回验证]（提示词里明确要求写完之后读回来确认）：只把读
    挑出来并发的话，读回验证可能发生在写之前 —— 模型读到旧内容却报告"已确认改好
    了"。那是静默错误，所以宁可不并行。
    """
    registry, _ = registry_with(lambda **kwargs: "读到的", parallel_safe=True)
    registry.register(Tool(
        name="write_file", description="写文件", risk=RiskLevel.MEDIUM,
        args_model=ListFilesArgs, handler=lambda **kwargs: "写好了",
    ))

    _, collector = run_agent(
        registry, batch_of(("list_files", "a"), ("write_file", "b")),
    )

    assert collector.of("tool_batch") == []
    assert [e["tool"] for e in collector.of("tool_result")] == ["list_files", "write_file"]


def test_a_single_call_is_never_worth_a_pool():
    """一条调用进池子只是白白多一个线程 —— 阈值是"至少两条"。"""
    registry, _ = registry_with(lambda **kwargs: "ok", parallel_safe=True)
    _, collector = run_agent(registry, batch_of(("list_files", "a")))

    assert collector.of("tool_batch") == []
    assert collector.of("tool_result")[0].get("parallel") is None


# --- 裁决和事件都不进线程池 -----------------------------------------------

def test_approval_still_happens_on_the_main_thread_in_order():
    """审批走 stdin，不能并发问。

    这里把 auto_approve 清空，逼两个**只读**工具也走审批 —— 于是并行路径下"裁决在
    哪、按什么顺序"才真的被考到。整批先问完再执行是这条路径上的刻意取舍：这一批都是
    只读的，人的判断不依赖前一条的结果。
    """
    asked: list[tuple[str, str]] = []

    def asker(tool, arguments):
        asked.append((threading.current_thread().name, arguments["path"]))
        return True

    registry, _ = registry_with(lambda **kwargs: "ok", parallel_safe=True)
    collector = ThreadRecordingCollector()
    agent = Agent(
        batch_of(("list_files", "a"), ("list_files", "b")), registry,
        PermissionPolicy(()),                       # 什么都不自动放行 → 一律问人
        asker=asker, on_event=collector, clock=LockedClock(),
    )
    agent.run(Session.new("s"), "看看这些")

    assert asked == [("MainThread", "a"), ("MainThread", "b")]
    assert [e["outcome"] for e in collector.of("permission")] == ["approved", "approved"]


def test_every_audit_event_is_emitted_from_the_main_thread():
    """on_event 不需要是线程安全的 —— 那是注入实现的契约，不该悄悄加一条。"""
    registry, _ = registry_with(lambda **kwargs: "ok", parallel_safe=True)
    _, collector = run_agent(registry, batch_of(("list_files", "a"), ("list_files", "b")))

    assert set(collector.threads) == {"MainThread"}


def test_a_buggy_tool_still_prints_its_traceback_from_the_main_thread(capsys):
    """非预期异常的完整栈必须留在 stderr 上，而且不能在工作线程里一行一行地打。

    traceback.print_exc() 是一行一次写，两个线程同时打会交错成读不懂的东西，所以栈
    是带回主线程打的。这条断言同时保证它确实打出来了 —— 吞掉它，我们自己 debug 的
    路就断了。
    """
    def handler(**kwargs):
        raise RuntimeError("工具自己写错了")

    registry, _ = registry_with(handler, parallel_safe=True)
    session, collector = run_agent(registry, batch_of(("list_files", "a"), ("list_files", "b")))

    err = capsys.readouterr().err
    assert "工具抛出未预期的异常" in err
    assert err.count("RuntimeError: 工具自己写错了") == 2      # 两条调用各一份
    # 给模型的文本仍然是简化过的：模型不该看见栈，否则它会去改一个不存在的问题
    assert all("RuntimeError" in text and "Traceback" not in text for text in tool_texts(session))
    assert [e["status"] for e in collector.of("tool_result")] == ["error", "error"]


# --- 耗时口径 -------------------------------------------------------------

def test_a_parallel_batch_counts_its_wall_time_not_the_sum():
    """逐条之和大于墙上时间，所以并行的批次必须按墙上时间算。

    不这么做的话，"未归因 = 回合总 - 各项之和"会减出负数、被 max(0, ...) 吞掉，
    报出一行恒为 0 的"未归因" —— 看起来完全正常。
    """
    timing = summarize_time([
        {"kind": "model_call", "duration_ms": 5000},
        {"kind": "tool_result", "duration_ms": 5000, "parallel": True},
        {"kind": "tool_result", "duration_ms": 5000, "parallel": True},
        {"kind": "tool_batch", "wall_ms": 5100},
        {"kind": "run_finished", "duration_ms": 10200},
    ])

    assert timing.tool_ms == 5100                 # 不是 10000：那 10000 里有一半是重叠的
    assert timing.saved_ms == 4900                # 省下来的那一份单独说
    assert timing.unattributed_ms == 100          # 10200 - 5000 - 5100


def test_logs_without_any_batch_event_keep_the_old_arithmetic():
    """旧日志里没有 tool_batch，也没有 parallel —— 数字一个都不许变。"""
    timing = summarize_time([
        {"kind": "tool_result", "duration_ms": 250},
        {"kind": "tool_result", "duration_ms": 750},
        {"kind": "run_finished", "duration_ms": 2000},
    ])

    assert timing.tool_ms == 1000
    assert timing.saved_ms == 0


def test_the_saving_is_shown_separately_from_the_tool_segment(capsys):
    _print_timing(Timing(run_ms=10200, model_ms=5000, tool_ms=5100,
                         waited_ms=0, backoff_ms=0, saved_ms=4900))
    line = capsys.readouterr().out

    assert "工具 5.1s" in line and "并行省 4.9s" in line and "未归因 100ms" in line
