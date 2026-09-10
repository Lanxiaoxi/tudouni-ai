"""权限关卡：拦住的时候，handler 必须**根本没被执行**。

只看返回值是不够的 —— 一个"先执行再报错"的实现也能让返回值看起来正确。
所以这里的断言都盯在"handler 被调用了几次"上。
"""

import json

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import Decision, PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import ScriptedModel, tool_call, usage


def make_registry(risk: RiskLevel):
    """建一个只含一个工具的注册表，并返回 handler 的调用记录。"""
    calls: list[dict] = []

    def handler(**kwargs):
        calls.append(kwargs)
        return ["<已执行>"]

    registry = ToolRegistry()
    registry.register(Tool(name="list_files", description="列目录", risk=risk,
                           args_model=ListFilesArgs, handler=handler))
    return registry, calls


def run_one_tool(registry, policy, asker, session=None):
    """一轮对话：调一次工具，然后给最终答案。"""
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, policy, asker=asker)
    session = session if session is not None else Session.new("s")
    agent.run(session, "列目录")
    return session


def never_asks(tool, arguments):
    raise AssertionError("这条路径不该询问用户")


# --- 放行 ---------------------------------------------------------------

def test_low_risk_is_auto_allowed_and_never_asks():
    registry, calls = make_registry(RiskLevel.LOW)
    session = run_one_tool(registry, PermissionPolicy({RiskLevel.LOW}), never_asks)
    assert calls == [{"path": "."}]       # handler 跑了，而且是带默认参数跑的
    assert "权限拒绝" not in json.dumps(session.messages, ensure_ascii=False)


def test_user_approval_allows_execution():
    registry, calls = make_registry(RiskLevel.MEDIUM)
    run_one_tool(registry, PermissionPolicy({RiskLevel.LOW}), lambda t, a: True)
    assert calls == [{"path": "."}]


# --- 拦住 ---------------------------------------------------------------

def test_user_denial_prevents_execution():
    registry, calls = make_registry(RiskLevel.MEDIUM)
    session = run_one_tool(registry, PermissionPolicy({RiskLevel.LOW}), lambda t, a: False)

    assert calls == []                    # ← 关键证据：handler 一次都没跑
    denial = [m for m in session.messages if m["role"] == "tool"][-1]["content"]
    assert "权限拒绝" in denial
    assert "不要重复" in denial            # 拒绝文案必须让模型知道下一步怎么办


def test_medium_risk_reaches_the_asker():
    """证明审批没有被绕过：中风险工具真的落到了 ASK 分支。"""
    registry, _ = make_registry(RiskLevel.MEDIUM)
    asked: list[str] = []
    run_one_tool(registry, PermissionPolicy({RiskLevel.LOW}),
                 lambda t, a: asked.append(t.name) or True)
    assert asked == ["list_files"]


def test_missing_asker_fails_closed():
    """要审批却没配询问方式 → 拒绝，绝不默认放行。"""
    registry, calls = make_registry(RiskLevel.MEDIUM)
    run_one_tool(registry, PermissionPolicy({RiskLevel.LOW}), asker=None)
    assert calls == []


def test_deny_decision_blocks_without_asking():
    """DENY 直接拒绝，也不必去问人 —— 答案已经知道了。"""
    registry, calls = make_registry(RiskLevel.LOW)

    class DenyAll:
        def decide(self, tool, arguments):
            return Decision.DENY

    run_one_tool(registry, DenyAll(), never_asks)
    assert calls == []


# --- 策略本身 -----------------------------------------------------------

def test_policy_is_pure_and_repeatable():
    """纯函数：同样输入永远同样输出，不读 stdin、没有副作用。"""
    tool = Tool(name="t", description="d", risk=RiskLevel.MEDIUM,
                args_model=ListFilesArgs, handler=lambda **k: None)
    policy = PermissionPolicy({RiskLevel.LOW})

    assert [policy.decide(tool, {}) for _ in range(50)] == [Decision.ASK] * 50
    assert PermissionPolicy([]).decide(tool, {}) is Decision.ASK
    assert PermissionPolicy({RiskLevel.MEDIUM}).decide(tool, {}) is Decision.ALLOW


def test_policy_accepts_plain_strings_from_config():
    """等级常常来自配置文件，JSON 读出来是普通字符串 —— 不该要求调用方转换。"""
    tool = Tool(name="t", description="d", risk=RiskLevel.MEDIUM,
                args_model=ListFilesArgs, handler=lambda **k: None)
    cfg = json.loads('{"auto_approve": ["low", "medium"]}')
    policy = PermissionPolicy(cfg["auto_approve"])

    assert policy.decide(tool, {}) is Decision.ALLOW


def test_policy_copies_the_set_it_is_given():
    """构造后改动外部集合，不该悄悄改掉策略的行为。"""
    source = {RiskLevel.LOW}
    policy = PermissionPolicy(source)
    tool = Tool(name="t", description="d", risk=RiskLevel.MEDIUM,
                args_model=ListFilesArgs, handler=lambda **k: None)
    source.add(RiskLevel.MEDIUM)
    assert policy.decide(tool, {}) is Decision.ASK
