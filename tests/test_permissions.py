"""权限关卡：拦住的时候，handler 必须**根本没被执行**。

只看返回值是不够的 —— 一个"先执行再报错"的实现也能让返回值看起来正确。
所以这里的断言都盯在"handler 被调用了几次"上。
"""

import json

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import Decision, PermissionPolicy
from agent_runtime.security.asker import _PREVIEW_LIMIT_BY_RISK, _preview
from agent_runtime.state import Session
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
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


# --- 审批提示本身 -------------------------------------------------------
#
# 关卡决定「要不要问」，提示决定「问了有没有用」。一条被截断的提示会让审批退化成
# 走过场 —— 用户看不全被审的东西，却要为此签字。

def test_high_risk_arguments_are_not_truncated():
    """高风险工具的判断依据**就是参数本身**，那里不能截断。

    shell 命令是这一档的由来：`git status && rm -rf /` 的重点全在后半句，而 120 字符
    的预览正好会把它切掉 —— 审批提示是那道关唯一的防线，它不能比被审的东西更短。
    """
    long_command = "echo " + "x" * 500
    limit = _PREVIEW_LIMIT_BY_RISK[RiskLevel.HIGH]

    assert limit is None
    assert _preview(long_command, limit) == long_command


def test_medium_risk_arguments_are_still_previewed():
    """中低风险保持原样：write_file 的 content 有几千字符，全打出来会把 path 挤没。"""
    preview = _preview("x" * 500, _PREVIEW_LIMIT_BY_RISK[RiskLevel.MEDIUM])

    assert len(preview) < 500
    assert "共 500 字符" in preview       # 截断必须标出真实长度，否则看不出来被截了


def test_preview_flattens_newlines_without_losing_content():
    """压平是多行命令必须做的（否则后面的参数被顶出屏幕），但它是**无损**的。

    这和截断是两回事：压平只是换了表示法，藏不掉任何东西。
    """
    assert _preview("rm -rf /tmp\nrm -rf /var", None) == "rm -rf /tmp\\nrm -rf /var"


def test_unknown_risk_level_falls_back_to_showing_everything():
    """将来加了新等级而这张表忘了配，缺省必须是"全打出来"。

    多显示一点是安全的失败方向，少显示才是危险的。
    """
    assert _PREVIEW_LIMIT_BY_RISK.get("brand-new-level") is None
