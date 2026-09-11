"""权限关卡：纯决策。

**只决定，不沟通。** 返回（拒绝文案 or None，审计字段），不发事件、不打印。
判定留在这一层，怎么把结果说出来交给调用方 —— 这样它可以被直接单测，而 Agent
那边的 _check_permission 从"决策 + 拼审计参数"的七十多行缩成一次调用加一次上报。

四种放行/拒绝的来路刻意分开记（outcome），因为它们事后要回答的问题不同：

    auto_allowed    风险等级在名单里 —— 没问过任何人，也不需要问
    rule_allowed    这个**工具**你以前按过 t —— 问过，但问的不是这一次
    command_allowed 这条**命令**命中了 .tudouni.json 里的前缀规则 —— 也没问这一次
    approved        这一次问了人，人批准了
    user_denied     这一次问了人，人拒绝了
    policy_denied   策略直接禁止，问都不用问
    no_asker        要问却没配询问方式，按拒绝处理

"谁批准的"和"策略直接禁止的"在追责时是两件事；"人批准的"、"人以前批准过、之后一直
自动放行"、"人预先写下的一条命令规则放过的"同样是三件事 —— 它们分别是**这一次**有人
看过、以及两种没有人在场的放行，而最后一种还要多回答一句"是哪条规则"。
"""

import time
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from agent_runtime.security.asker import ApprovalAsker
from agent_runtime.security.commands import Rule, command_of, covered
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.security.policy import Decision, PermissionPolicy
from agent_runtime.tools.tool import Tool


class GateResult(NamedTuple):
    """一次权限裁决的结果。"""

    denial: str | None      # None 表示放行；有值就是回灌给模型的拒绝文案
    outcome: str            # 见模块 docstring 里那张表
    decision: Decision
    waited_ms: int | None   # 只在真的问过人才有值
    # 这一次裁决**新增**的免问规则（人按了 t）。审计要记它：一次批准同时改变了将来
    # 的行为，那不是"谁批准了什么"里可以省掉的一半。空集合表示这次没有新增。
    remembered: frozenset[str] = frozenset()
    # 命中的命令前缀规则（outcome == command_allowed 时才有值）。它回答的是
    # "这条命令为什么没问" —— 没有它，日志里只能看出"没问过"，看不出凭哪一条。
    rule: Rule | None = None


def check_permission(
    tool: Tool,
    arguments: Mapping[str, Any],
    policy: PermissionPolicy,
    asker: ApprovalAsker | None = None,
    memory: ApprovalMemory | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> GateResult:
    """裁定一次工具调用能不能执行。

    拒绝走的是第 4 阶段那条通道（回灌一段文字给模型），而不是直接终止任务 ——
    用户拒绝的通常只是"这一次的做法"，模型有机会换一种方式。

    clock 是注入的，而且**上层传下来的是同一个时钟**：一次回合里 model_call /
    permission / tool_result 的耗时必须出自同一把尺子，否则把它们相加是在混用三种
    单位。顺带它也是 waited_ms 能被精确断言的前提（真实时钟只能断言"大于 0"）。
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

    # 人在之前某次审批里按过 t（整个工具）。这一支排在 no_asker **之前**：规则已经把
    # 答案给了，没有询问方式不影响这个答案 —— 反过来会让"配了免问规则却因为缺 asker
    # 被拒"这种荒唐结果出现。
    if memory is not None and tool.name in memory:
        return GateResult(None, "rule_allowed", decision, None)

    # 命令前缀规则：比"整个工具免问"更细的一层。
    #
    # 它排在整工具那条**后面**，是为了让审计里报出来的原因总是"单独就足够"的那一个：
    # 工具已经被整体免问时，报"命中了哪条命令规则"会让人以为撤掉那条规则它就会重新
    # 被问，而事实不是这样。
    if memory is not None:
        command = command_of(tool.name, arguments)
        if command is not None:
            matched = covered(command, memory.prefixes())
            if matched is not None:
                return GateResult(None, "command_allowed", decision, None, rule=matched)

    if asker is None:
        # fail-closed：要审批却没配询问方式，一律拒绝，绝不默认放行。
        return GateResult(
            f"权限拒绝：{tool.name} 需要人工审批，但当前未配置审批方式，已拒绝。"
            "不要重试同样的调用。",
            "no_asker", decision, None,
        )

    # 问之前先给记忆拍一张快照，问完再比一次：多出来的就是这次按键留下的规则。
    # 这样 asker 的契约（工具和参数进去、布尔出来）不用改，而"这次批准顺带改了
    # 将来的行为"这件事仍然能被审计看见。
    before = memory.tools() if memory is not None else frozenset()

    started = clock()
    approved = asker(tool, arguments)
    waited_ms = int((clock() - started) * 1000)

    if approved:
        remembered = (memory.tools() - before) if memory is not None else frozenset()
        return GateResult(None, "approved", decision, waited_ms, remembered)

    return GateResult(
        f"权限拒绝：用户拒绝执行 {tool.name}。"
        "不要重复同样的调用；请换一种方式，或先说明你为什么需要执行它。",
        "user_denied", decision, waited_ms,
    )
