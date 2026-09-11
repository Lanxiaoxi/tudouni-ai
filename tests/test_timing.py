"""耗时：口径、来源，以及"这一轮的时间花在哪"。

时间是最难断言的东西 —— 用真实时钟只能断言"大于 0"，那等于没断言。所以这里用的是
**注入的假时钟**：Agent（以及它调下去的 retry）从这里读时间，而推进它的是假模型、
假 handler、假 asker。于是"模型想了 1.5 秒"、"人看了 30 秒才按 y"都成了能精确写出来
的事实 —— 而三个口径有没有重叠，也就变成了一个能钉死的断言。
"""

import pytest

from agent_runtime.agents import Agent
from agent_runtime.agents.retry import MAX_ATTEMPTS, call_with_retry
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse, ModelTransientError
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, ExplodingModel, tool_call, usage


class FakeClock:
    """只有被明确推进时才走动的时钟。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimedModel(ChatModel):
    """每次返回响应之前先"想"一会儿。"""

    def __init__(self, clock: FakeClock, script: list[ModelResponse], think: float = 1.0):
        self.clock, self.script, self.think = clock, list(script), think

    def complete(self, messages, tools=None):
        self.clock.advance(self.think)
        return self.script.pop(0) if self.script else ModelResponse(content="(剧本用尽)")


def build(clock, risk=RiskLevel.LOW, *, exec_seconds=0.25, tool_calls=1, asker=None):
    """一个只含一个工具的注册表 + 一段"调 tool_calls 次工具然后收尾"的剧本。"""
    def handler(**kwargs):
        clock.advance(exec_seconds)
        return "<已执行>"

    registry = ToolRegistry()
    registry.register(Tool(name="list_files", description="列目录", risk=risk,
                           args_model=ListFilesArgs, handler=handler))

    script = [
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {}, call_id=f"c{i}")],
                      usage=usage())
        for i in range(tool_calls)
    ]
    script.append(ModelResponse(content="完成", usage=usage()))
    return registry, TimedModel(clock, script)


def run(clock, registry, model, **kwargs):
    collector = Collector()
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}), on_event=collector,
                  clock=clock, **kwargs)
    agent.run(Session.new("s"), "列目录")
    return collector


# --- 口径：工具耗时不含等人审批 -------------------------------------------

def test_tool_duration_excludes_the_time_spent_waiting_for_a_human():
    """这条是这个功能里最容易做错的地方。

    计时起点原本在函数入口，于是"我看了 30 秒才按 y"会被算进工具耗时 —— 那个数字
    几乎等于人的思考时间，拿它去判断哪个工具慢会完全跑偏，而且它和 waited_ms 相加
    还会重复计算。
    """
    clock = FakeClock()
    registry, model = build(clock, risk=RiskLevel.MEDIUM)

    def human(tool, arguments):
        clock.advance(30.0)         # 人盯着命令看了 30 秒
        return True

    collector = run(clock, registry, model, asker=human)

    assert collector.of("permission")[0]["waited_ms"] == 30_000
    assert collector.of("tool_result")[0]["duration_ms"] == 250      # 只有执行那一段


def test_denied_tool_records_no_execution_time_at_all():
    """被拒的那一支什么都没执行 —— 记 0，而不是"从函数入口算起的耗时"。"""
    clock = FakeClock()
    registry, model = build(clock, risk=RiskLevel.MEDIUM)

    collector = run(clock, registry, model, asker=lambda tool, args: clock.advance(5.0) or False)

    assert collector.of("permission")[0]["waited_ms"] == 5_000
    assert collector.of("tool_result")[0]["status"] == "denied"
    assert collector.of("tool_result")[0]["duration_ms"] == 0


def test_model_duration_is_measured_per_attempt():
    clock = FakeClock()
    registry, model = build(clock)
    collector = run(clock, registry, model)

    assert [e["duration_ms"] for e in collector.of("model_call")] == [1000, 1000]


# --- 回合总时长 -----------------------------------------------------------

def test_run_finished_carries_the_whole_turn():
    """模型 1s + 工具 0.25s + 模型 1s = 2.25s，一秒不多一秒不少。"""
    clock = FakeClock()
    registry, model = build(clock)
    collector = run(clock, registry, model)

    finished = collector.of("run_finished")
    assert len(finished) == 1
    assert finished[0]["stop_reason"] == "answered"
    assert finished[0]["duration_ms"] == 2250


@pytest.mark.parametrize("stop_reason,expected", [
    ("model_fatal", 1000),      # 模型失败：run_finished 也得带上已经花掉的时间
])
def test_every_run_finished_site_carries_the_duration(stop_reason, expected):
    """三个收尾点（答完 / 步数用尽 / 模型失败）都要带时长 —— 走同一个 helper 就不会漏。"""
    from agent_runtime.models.types import ModelFatalError

    clock = FakeClock()
    registry, _ = build(clock)

    class FailingButSlow(ChatModel):
        """先"想"一秒再失败 —— 好让"失败那一轮的时长"是个非零的确定值。"""
        def complete(self, messages, tools=None):
            clock.advance(1.0)
            raise ModelFatalError("401 鉴权失败")

    collector = Collector()
    agent = Agent(FailingButSlow(), registry, PermissionPolicy({RiskLevel.LOW}),
                  on_event=collector, clock=clock)

    with pytest.raises(ModelFatalError):
        agent.run(Session.new("s"), "你好")

    finished = collector.of("run_finished")
    assert finished[0]["stop_reason"] == stop_reason
    assert finished[0]["duration_ms"] == expected


def test_step_limit_also_carries_the_duration():
    """步数用尽那条路（它撞上限时抛异常）同样要带上时长。"""
    from agent_runtime.agents import StepLimitExceeded

    clock = FakeClock()
    registry, model = build(clock, tool_calls=5)     # 剧本够长，一定撞上限
    collector = Collector()
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  on_event=collector, clock=clock)

    with pytest.raises(StepLimitExceeded):
        agent.run(Session.new("s"), "一直调工具", max_steps=2)

    finished = collector.of("run_finished")
    assert finished[0]["stop_reason"] == "max_steps"
    assert finished[0]["duration_ms"] == 2500      # 两步，每步 1.0s 模型 + 0.25s 工具


def test_retry_attempts_drive_the_run_duration_too():
    """重试的两次尝试各自算在回合总时长里 —— 它们出自被传下去的同一个时钟。"""
    import agent_runtime.agents.retry as retry_module

    clock = FakeClock()
    registry, _ = build(clock)

    class FlakyOnce(ChatModel):
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None):
            self.n += 1
            clock.advance(2.0)
            if self.n == 1:
                raise ModelTransientError("连接中断")
            return ModelResponse(content="好了", usage=usage())

    collector = run(clock, registry, FlakyOnce(), asker=None)
    assert [e["duration_ms"] for e in collector.of("model_call")] == [2000, 2000]
    # 退避本身是 0 秒（retry 的默认值是 0.5s，但那要真的睡 —— 这里验的是口径不重叠）
    assert collector.of("run_finished")[0]["duration_ms"] == 4000


# --- 退避：它不属于任何一次请求，所以必须单独记 ---------------------------

def test_backoff_is_recorded_on_the_attempt_that_will_retry():
    """直接调 retry：sleep 注入成录音器，所以不会真的等，而退避时长是精确值。"""
    slept: list[float] = []
    attempts = []
    clock = FakeClock()

    class AlwaysTransient(ChatModel):
        def complete(self, messages, tools=None):
            clock.advance(1.0)
            raise ModelTransientError("连接中断")

    with pytest.raises(ModelTransientError):
        call_with_retry(AlwaysTransient(), [], [], attempts.append,
                        sleep=slept.append, clock=clock)

    assert len(attempts) == MAX_ATTEMPTS
    # 前两次会重试 → 带上"接下来等多久"；最后一次不会了 → None
    assert [a.backoff_ms for a in attempts] == [500, 1000, None]
    assert slept == [0.5, 1.0]                       # 退避真的发生了，只是没真的睡
    assert [a.duration_ms for a in attempts] == [1000] * MAX_ATTEMPTS


def test_the_agent_puts_the_backoff_into_the_audit_event(monkeypatch):
    import agent_runtime.agents.retry as retry_module

    monkeypatch.setattr(retry_module, "BACKOFF_BASE", 0.0)   # 别真的睡
    clock = FakeClock()
    registry, _ = build(clock)
    collector = run(clock, registry, ExplodingModel(ModelTransientError("断"), fail_times=1))

    calls = collector.of("model_call")
    assert calls[0]["status"] == "error"
    assert calls[0]["backoff_ms"] == 0        # 记了，只是这次退避被压成 0
    assert calls[1]["status"] == "ok"
    assert "backoff_ms" not in calls[1]       # 成功的尝试没有"接下来等多久"
