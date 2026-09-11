"""步数用尽：既不是答案，也不是失败。

这条边界很容易被无声破坏 —— 只要把"超过最大执行步数"当成 run() 的返回值，cli.py
就会把它 print 进 **stdout**，于是 `> 对话.txt` 里混进一句"已停止"，用户从输出里
分不出"答完了"和"被砍断了"。所以这里钉住的是三件事：

  1. 撞上限要**抛**，不能返回（返回等于伪装成答案）；
  2. 抛之前审计和落盘必须已经发生（顺序错了，日志里就只剩一条悬空的 run_started，
     异常消息里"会话是完好的"也就成了一句空话）；
  3. 撞上限之后会话仍然一致、可以直接接着跑 —— 这是它和 ModelFatalError 最大的
     区别，也是 run_repl 里那句"接着跑"的依据。
"""

import pytest

from agent_runtime.agents import Agent, StepLimitExceeded
from agent_runtime.models.types import ModelError
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.tool import RiskLevel

from fakes import AlwaysCallsModel, Collector
from test_message_invariants import assert_consistent


def build(registry, collector=None, checkpoint=None) -> Agent:
    return Agent(
        AlwaysCallsModel(), registry, PermissionPolicy({RiskLevel.LOW}),
        asker=lambda t, a: False, on_event=collector, on_checkpoint=checkpoint,
    )


def test_exhausted_budget_raises_instead_of_returning(registry):
    agent = build(registry)
    with pytest.raises(StepLimitExceeded) as exc:
        agent.run(Session.new("s"), "一直调工具", max_steps=3)

    assert exc.value.step == 3
    assert exc.value.tools == ["list_files"]      # 卡在什么上面 —— 唯一的线索
    assert "接着跑" in str(exc.value)              # 会话是好的，这句得说出来

    # 它不能是 ModelError 的一种：那两类失败的处置是"重试"和"改配置"，而步数用尽
    # 两者都不需要。混进同一棵树，run_repl 里那几个 except 的先后顺序就成了语义。
    assert not isinstance(exc.value, ModelError)


def test_the_limit_is_recorded_as_its_own_stop_reason(registry):
    collector = Collector()
    with pytest.raises(StepLimitExceeded):
        build(registry, collector).run(Session.new("s"), "一直调工具", max_steps=3)

    assert [e["stop_reason"] for e in collector.of("run_finished")] == ["max_steps"]


def test_audit_and_checkpoint_happen_before_the_raise(workdir, registry):
    """顺序反了就等于白抛：日志里少了这一轮的结局，会话也没真的存下去。"""
    store = JsonSessionStore(workdir)
    collector = Collector()

    with pytest.raises(StepLimitExceeded):
        build(registry, collector, store.save).run(Session.new("s"), "一直调工具", max_steps=3)

    assert collector.of("run_finished")            # 先记审计
    assert store.exists("s")                       # 再落盘
    assert_consistent(store.load("s").messages)    # 存下去的是完整状态，不是半截


def test_the_session_survives_and_can_be_resumed(registry):
    """撞上限的位置在循环顶部，那里 messages 一致（上一步的结果都 append 完了）。"""
    session = Session.new("s")
    with pytest.raises(StepLimitExceeded):
        build(registry).run(session, "一直调工具", max_steps=2)

    assert_consistent(session.messages)
    assert [m["role"] for m in session.messages][:2] == ["system", "user"]
