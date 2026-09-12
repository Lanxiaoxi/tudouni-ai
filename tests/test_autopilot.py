"""`--autopilot`：这一轮没有人可问，所以一律不问。

它**只管审批那一关**。所以这里的断言分两类：

  * 审批：不问、不等人、审计里每一次放行都写着 `autopilot`；
  * 不归它管的：拒绝名单照样拒、工作区边界和控制面写入照样拦 —— 那些是"不许做"，
    不是"要不要问"。把它们和审批混为一谈，正是这个开关最容易被人误以为的意思。

审计那一条是设计上的重点：用 `asker=lambda t, a: True` 也能让调用通过，但每一次都会
记成 `approved`（有人批准）—— 那是假话，事后没法回答"这次会话到底有没有人看着"。
"""

import pytest

from agent_runtime.agents import Agent
from agent_runtime.frontends.cli import build_parser
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import ApprovalMemory, PermissionPolicy
from agent_runtime.security.gate import check_permission
from agent_runtime.state import Session
from agent_runtime.tools.builtin.shell import ShellArgs
from agent_runtime.tools.builtin.filesystem import FileSystem
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, ScriptedModel, tool_call, usage


def shell_tool(risk: RiskLevel = RiskLevel.HIGH) -> Tool:
    return Tool(name="shell", description="跑命令", risk=risk,
                args_model=ShellArgs, handler=lambda **kwargs: None)


def never_asks(tool, arguments):
    raise AssertionError("autopilot 下不该询问用户")


# --- 审批那一关 -----------------------------------------------------------

def test_autopilot_allows_without_asking():
    result = check_permission(shell_tool(), {"command": "rm -rf build"}, PermissionPolicy(),
                              never_asks, autopilot=True)

    assert result.outcome == "autopilot"
    assert result.denial is None
    assert result.waited_ms is None          # 没等人，就没有等待时长


def test_autopilot_labels_every_allow_as_autopilot_not_by_the_other_reasons():
    """**这一条是设计上的重点。**

    等级名单里的工具本来就会记成 auto_allowed、命中规则的记成 command_allowed ——
    那些来路和"正常开着审批的会话"长得一模一样。开着 autopilot 的会话里每条放行都
    必须写着 autopilot，否则"这次会话有没有人看着"就只能靠推理去猜。
    """
    policy = PermissionPolicy(auto_approve={RiskLevel.LOW})
    low = shell_tool(RiskLevel.LOW)
    memory = ApprovalMemory(prefixes={("rm",)})

    assert check_permission(low, {}, policy, never_asks, memory,
                            autopilot=True).outcome == "autopilot"
    assert check_permission(shell_tool(), {"command": "rm -rf build"}, policy, never_asks,
                            memory, autopilot=True).outcome == "autopilot"


def test_autopilot_does_not_bypass_the_deny_list():
    """`deny_tools` 是"不许做"，不是"要不要问" —— autopilot 无权推翻它。"""
    policy = PermissionPolicy(deny_tools={"shell"})
    result = check_permission(shell_tool(), {"command": "rm -rf build"}, policy,
                              never_asks, autopilot=True)

    assert result.outcome == "policy_denied"
    assert "权限拒绝" in result.denial


def test_autopilot_is_off_by_default():
    """没开的时候一切照旧 —— 需要审批的工具仍然去问。"""
    asked = []
    check_permission(shell_tool(), {"command": "ls"},
                     PermissionPolicy(), lambda t, a: asked.append(t.name) or True)

    assert asked == ["shell"]


# --- 不归它管的：边界 -----------------------------------------------------

def test_autopilot_does_not_touch_the_workspace_boundary(workdir):
    """边界在工具里，不在关卡里 —— autopilot 让工具被执行，但它照样越不出去。"""
    fs = FileSystem(str(workdir))

    with pytest.raises(PermissionError, match="escapes workspace"):
        fs.write_file("../outside.txt", "x")


def test_autopilot_does_not_touch_the_control_plane(workdir):
    """同一个道理：`.tudouni/` 仍然写不进去。

    "人批准了也不能写"这条如果被 autopilot 破掉，那这个开关就等于把整个权限体系
    的自举保护一起交出去了。
    """
    fs = FileSystem(str(workdir))

    with pytest.raises(PermissionError, match="control plane"):
        fs.write_file(".tudouni/permissions.json", '{"auto_approve_tools": ["shell"]}')


# --- 接线：CLI → Agent → 审计 --------------------------------------------

def test_the_flag_parses():
    assert build_parser().parse_args([]).autopilot is False
    assert build_parser().parse_args(["--autopilot"]).autopilot is True


def test_the_agent_audits_autopilot_and_runs_the_tool():
    calls = []
    registry = ToolRegistry()
    registry.register(Tool(name="shell", description="跑命令", risk=RiskLevel.HIGH,
                           args_model=ShellArgs, handler=lambda **kwargs: calls.append(kwargs)))
    events = Collector()
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("shell", {"command": "ls"})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])

    Agent(model, registry, PermissionPolicy(), asker=never_asks, on_event=events,
          autopilot=True).run(Session.new("s"), "看看目录")

    permission = events.of("permission")[0]
    assert permission["outcome"] == "autopilot"
    assert "waited_ms" not in permission         # 没等人
    assert calls == [{"command": "ls", "timeout_seconds": 30}]   # 工具真的执行了
