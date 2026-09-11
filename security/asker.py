"""审批询问者（asker）。

PermissionPolicy 只裁定「要不要问」，真正去问的是这里的东西。

把它做成一个可注入的可调用对象，是为了让同一份 Agent 代码同时服务几种场合：

    CLI        → cli_asker（问终端；配了 memory 时多给一个 t）
    测试        → 一个脚本化的假 asker（零 stdin、零 mock）
    无人值守     → lambda tool, args: True（全部放行）
    将来的 Web  → 挂起并等待审批结果的 asker

Agent 只知道「我有个东西能问」，不知道它背后是终端、是脚本还是网络。
"""

import sys
from collections.abc import Callable, Mapping
from typing import Any

from agent_runtime.security.commands import (
    Rule,
    command_of,
    command_parameter,
    suggest_prefix,
)
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.tools.tool import RiskLevel, Tool


# 一个 asker：给它工具和参数，回答「是否批准执行」。
#
# 「t（以后别再问）」不由这个返回类型表达：它由 asker 写进 memory，而 gate 用问前
# 问后的快照差把这件事记进审计（见 security/gate.py）。所以既有那些只返回布尔的
# asker（脚本化的假 asker、无人值守的 lambda）一行都不用改。
ApprovalAsker = Callable[[Tool, Mapping[str, Any]], bool]

# 审批提示里每个参数值的最大预览长度，**按风险分级**。
#
# 中低风险工具要被判断的是「动作类型」：write_file 的关键信息是 path，而它的 content
# 实测有 3401 字符，全打出来会把 path 挤没 —— 所以给预览。
#
# 但高风险工具的判断依据**就是参数本身**，那里截断等于让用户在看不全的情况下签字。
# shell 命令正是这一档：`git status && rm -rf /` 的重点全在后半句，而 120 字符的预览
# 正好会把它切掉。审批提示是这道关唯一的防线，它不能比被审的东西更短。
_PROMPT_PREVIEW = 120
_PREVIEW_LIMIT_BY_RISK: dict[RiskLevel, int | None] = {
    RiskLevel.LOW: _PROMPT_PREVIEW,
    RiskLevel.MEDIUM: _PROMPT_PREVIEW,
    RiskLevel.HIGH: None,           # None = 原样打全，不截断
}


def _preview(value: Any, limit: int | None) -> str:
    """把参数值压成单行；limit 为 None 时只压平、不截断。

    压平（换行渲染成字面的 \\n）一律做：审批提示是一行一行读的，多行内容会把后面的
    参数顶出屏幕。它是**无损**的，藏不掉任何东西 —— 这和截断是两回事。
    """
    text = value if isinstance(value, str) else repr(value)
    flat = text.replace("\n", "\\n")
    if limit is None or len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…(共 {len(flat)} 字符)"


# 按 t 之后会发生什么，**按风险分开说**。中低风险的后果只是"以后少一次确认"；
# 高风险不是 —— 对 shell 按 t 意味着你**再也看不见它要执行什么**，而命令原文正是
# 那道关唯一的判断依据（见上面 _PREVIEW_LIMIT_BY_RISK 那一段）。用同一句"以后不再
# 询问"盖住这两种情况，等于把最要紧的那半句省掉了。
_REMEMBER_CONSEQUENCE: dict[RiskLevel, str] = {
    RiskLevel.HIGH: "以后每次都直接执行，你不会再看到它要做什么",
}
_REMEMBER_DEFAULT_CONSEQUENCE = "以后不再询问这个工具"


def _remember_hint(tool: Tool, target: Rule | str, label: str) -> str:
    """t 那一行的说明 —— 它必须说清"按下去会记住什么"。

    命令类工具记的是一条**前缀**（`git add`），不是"整个 shell 免问"：两者的后果差着
    量级，而人唯一的判断依据就是这一行。缺省那句跟着风险等级走，新等级忘了配也落在
    安全的说法上。
    """
    if isinstance(target, tuple):
        return (
            f"以后 {' '.join(target)} 开头的命令都直接执行，不会再给你看"
            f"（写进 {label}，下次启动仍然有效）"
        )
    consequence = _REMEMBER_CONSEQUENCE.get(tool.risk, _REMEMBER_DEFAULT_CONSEQUENCE)
    return f"{consequence}（写进 {label}，下次启动仍然有效）"


def cli_asker(
    tool: Tool,
    arguments: Mapping[str, Any],
    memory: ApprovalMemory | None = None,
) -> bool:
    """在终端上征求用户批准。

    四个细节都是刻意的：

    1. **提示写 stderr。** input() 自己的提示语走 stdout，而 stdout 是 Agent 最终
       产出的通道 —— 混进去会污染结果（重定向到文件时最明显）。所以提示一律用
       print(..., file=sys.stderr)，input() 不传提示参数。

    2. **参数按风险决定打多全。** 审批提示要给的是「判断依据」。中低风险工具的判断
       依据是「动作类型」—— write_file 的 content 动辄几千字符，全打出来反而把 path
       这种真正需要看的信息挤没了。高风险工具的判断依据就是参数本身，所以原样打全：
       shell 命令的重点常常在后半句，截断等于让用户在看不全的情况下签字。

    3. **读不到输入时返回 False（拒绝）。** 非交互环境（管道、CI、将来的 Web）里
       input() 会抛 EOFError。默认放行等于「无人值守时静默执行中风险操作」，
       默认拒绝才是安全的失败方向。回车也走这一支 —— 提示是 [y/N]，不是 [Y/n]。

       **OSError 也算"读不到"。** stdin 被接走或者已关闭时（pytest 的捕获、
       把 stdin 关掉的守护进程）input() 抛的是 OSError 而不是 EOFError，而它
       冒出去会穿过 gate 落到 Agent 的 except Exception 上，把一次审批变成
       "工具执行失败" —— 一个读不到输入的环境，答案和 EOF 完全一样：拒绝。
       这里只有 input() 一条语句，所以 OSError 只可能来自 stdin。

    4. **t 只在真的能记住时才给，而且记住什么要说清楚。** memory 为 None 时提示里
       没有 t，打进来的 t 会落到"不是 y"那一支（拒绝）—— 答应了却记不住比拒绝更坏：
       用户以为已经永久放行了，下一次却还被问。命令类工具（shell）记的是**命令前缀**
       （`git add` 开头），不是整个 shell；前缀推不出来时（命令行里有重定向、命令替换
       之类，解析器不肯猜）干脆不提供 t —— 一个按键记下"整个 shell 免问"和这个功能的
       初衷正好相反。
    """
    # 用 .get 而不是 []：将来加了新的风险等级而这张表忘了配，缺省值是「原样打全」。
    # 多显示一点是安全的失败方向，少显示才是危险的。
    limit = _PREVIEW_LIMIT_BY_RISK.get(tool.risk)
    args_preview = ", ".join(
        f"{name}={_preview(value, limit)}" for name, value in arguments.items()
    )

    # 按 t 该记住什么：命令类工具记前缀，其余工具记工具名。推不出来就是 None ——
    # None 表示"这次不提供 t"，而不是"记住整个工具"。
    target: Rule | str | None = None
    if memory is not None:
        if command_parameter(tool.name) is None:
            target = tool.name
        else:
            command = command_of(tool.name, arguments)
            target = suggest_prefix(command) if command is not None else None

    print(f"[审批] 工具 {tool.name}  风险 {tool.risk.value}", file=sys.stderr)
    print(f"[审批] 参数 {args_preview}", file=sys.stderr)

    if target is not None:
        print(f"[审批] t = {_remember_hint(tool, target, memory.label)}", file=sys.stderr)
        print("[审批] 是否执行？[y/N/t] ", end="", file=sys.stderr, flush=True)
    else:
        print("[审批] 是否执行？[y/N] ", end="", file=sys.stderr, flush=True)

    try:
        answer = input()
    except (EOFError, OSError):
        print(file=sys.stderr)  # 补个换行，免得后续输出接在提示后面
        return False

    answer = answer.strip().lower()

    if answer == "t" and target is not None:
        # 记在这里而不是返回给 gate：asker 的返回类型保持布尔，而 gate 用问前问后的
        # 快照差看见这次新增（见 security/gate.py）。
        if isinstance(target, str):
            memory.grant(target)
        else:
            memory.grant_prefix(target)
        return True

    return answer in {"y", "yes"}
