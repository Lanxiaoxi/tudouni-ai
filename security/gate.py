"""权限关卡：纯决策。

**只决定，不沟通。** 返回（拒绝文案 or None，审计字段），不发事件、不打印。
判定留在这一层，怎么把结果说出来交给调用方 —— 这样它可以被直接单测，而 Agent
那边的 _check_permission 从"决策 + 拼审计参数"的七十多行缩成一次调用加一次上报。

四种放行/拒绝的来路刻意分开记（outcome），因为它们事后要回答的问题不同：
"谁批准的"和"策略直接禁止的"在追责时是两件事。
"""

import time
from collections.abc import Mapping
from typing import Any, NamedTuple

from agent_runtime.security.asker import ApprovalAsker
from agent_runtime.security.policy import Decision, PermissionPolicy
from agent_runtime.tools.tool import Tool


class GateResult(NamedTuple):
    """一次权限裁决的结果。"""

    denial: str | None      # None 表示放行；有值就是回灌给模型的拒绝文案
    outcome: str            # auto_allowed / policy_denied / no_asker / approved / user_denied
    decision: Decision
    waited_ms: int | None   # 只在真的问过人才有值


def check_permission(
    tool: Tool,
    arguments: Mapping[str, Any],
    policy: PermissionPolicy,
    asker: ApprovalAsker | None = None,
) -> GateResult:
    """裁定一次工具调用能不能执行。

    拒绝走的是第 4 阶段那条通道（回灌一段文字给模型），而不是直接终止任务 ——
    用户拒绝的通常只是"这一次的做法"，模型有机会换一种方式。
    """
    decision = policy.decide(tool, arguments)

    if decision is Decision.ALLOW:
        return GateResult(None, "auto_allowed", decision, None)

    if decision is Decision.DENY:
        return GateResult(
            f"权限拒绝：{tool.name} 被权限策略禁止执行。"
            "不要重试同样的调用，请改用其它方式完成任务。",
            "policy_denied", decision, None,
        )

    if asker is None:
        # fail-closed：要审批却没配询问方式，一律拒绝，绝不默认放行。
        return GateResult(
            f"权限拒绝：{tool.name} 需要人工审批，但当前未配置审批方式，已拒绝。"
            "不要重试同样的调用。",
            "no_asker", decision, None,
        )

    started = time.perf_counter()
    approved = asker(tool, arguments)
    waited_ms = int((time.perf_counter() - started) * 1000)

    if approved:
        return GateResult(None, "approved", decision, waited_ms)

    return GateResult(
        f"权限拒绝：用户拒绝执行 {tool.name}。"
        "不要重复同样的调用；请换一种方式，或先说明你为什么需要执行它。",
        "user_denied", decision, waited_ms,
    )
