"""命令规则接上权限层之后：关卡什么时候放行、审计里记什么、别的工具会不会被牵连。

纯匹配的语义（什么算被覆盖、什么落到"问"）在 test_command_rules.py 里。这里只盯
接线，而那几件事各有各的失败方式：

  * 命中规则却不放行 —— 功能不生效；
  * 没命中却放行 —— 等于把审批拆了（所以有几条用 `never_asks` 证明"它本来会问"）；
  * 放行了但审计看不出凭哪条 —— 事后无法回答"撤掉哪条规则能把它变回要问"。
"""

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import ApprovalMemory, PermissionPolicy
from agent_runtime.security.gate import check_permission
from agent_runtime.state import Session
from agent_runtime.tools.builtin.shell import ShellArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, ScriptedModel, tool_call, usage


def shell_tool() -> Tool:
    return Tool(name="shell", description="跑命令", risk=RiskLevel.HIGH,
                args_model=ShellArgs, handler=lambda **kwargs: None)


def never_asks(tool, arguments):
    raise AssertionError("这条路径不该询问用户")


# --- 关卡：这一层和 tool 名那一层的关系 -----------------------------------

def test_a_covered_command_is_allowed_without_asking():
    result = check_permission(
        shell_tool(), {"command": "git add -p x.py"}, PermissionPolicy(),
        never_asks, ApprovalMemory(prefixes={("git", "add")}),
    )

    assert result.outcome == "command_allowed"
    assert result.rule == ("git", "add")        # 审计要能回答"凭哪一条"
    assert result.denial is None
    assert result.waited_ms is None             # 没等人


def test_an_uncovered_command_still_asks():
    asked = []

    def asker(tool, arguments):
        asked.append(arguments["command"])
        return True

    result = check_permission(
        shell_tool(), {"command": "rm -rf build"}, PermissionPolicy(),
        asker, ApprovalMemory(prefixes={("git",)}),
    )

    assert asked == ["rm -rf build"]
    assert result.outcome == "approved"


def test_a_fully_covered_chain_is_allowed_but_a_partly_covered_one_is_not():
    memory = ApprovalMemory(prefixes={("git",), ("ls",)})

    assert check_permission(shell_tool(), {"command": "git status && ls -la"},
                            PermissionPolicy(), never_asks, memory).outcome == "command_allowed"

    # 第二段没人认领 → 必须去问人。never_asks 会抛，那正是"它确实来问了"的证据。
    with pytest.raises(AssertionError, match="不该询问"):
        check_permission(shell_tool(), {"command": "git status && rm -rf build"},
                         PermissionPolicy(), never_asks, memory)


def test_the_tool_level_grant_is_reported_first():
    """整个工具已经免问时，报的是那条更宽的原因 —— 它单独就足够解释"为什么没问"。"""
    memory = ApprovalMemory({"shell"}, prefixes={("git", "add")})
    result = check_permission(shell_tool(), {"command": "git add x"},
                              PermissionPolicy(), never_asks, memory)

    assert result.outcome == "rule_allowed"


def test_deny_still_beats_a_command_rule():
    result = check_permission(
        shell_tool(), {"command": "git add x"},
        PermissionPolicy(deny_tools={"shell"}), never_asks,
        ApprovalMemory(prefixes={("git", "add")}),
    )

    assert result.outcome == "policy_denied"


def test_rules_do_not_leak_to_other_tools():
    """规则只对**带命令行的工具**生效 —— 别的工具的参数不该被拿去做前缀匹配。"""
    asked = []
    listed = Tool(name="list_files", description="列目录", risk=RiskLevel.MEDIUM,
                  args_model=ShellArgs, handler=lambda **kwargs: None)

    result = check_permission(listed, {"command": "git add x"}, PermissionPolicy(),
                              lambda tool, arguments: asked.append(tool.name) or True,
                              ApprovalMemory(prefixes={("git", "add")}))

    assert asked == ["list_files"]              # 走到了审批，没被命令规则放行
    assert result.outcome == "approved"


# --- 通过 Agent 跑一遍：审计里要看得见"凭哪条规则" -------------------------

def test_the_agent_audits_the_matched_rule():
    calls = []
    registry = ToolRegistry()
    registry.register(Tool(name="shell", description="跑命令", risk=RiskLevel.HIGH,
                           args_model=ShellArgs, handler=lambda **kwargs: calls.append(kwargs)))
    events = Collector()
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("shell", {"command": "git add x"})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])

    Agent(model, registry, PermissionPolicy(), asker=never_asks,
          memory=ApprovalMemory(prefixes={("git", "add")}),
          on_event=events).run(Session.new("s"), "提交一下")

    permission = events.of("permission")[0]
    assert permission["outcome"] == "command_allowed"
    assert permission["rule"] == "git add"
    assert calls == [{"command": "git add x", "timeout_seconds": 30}]
