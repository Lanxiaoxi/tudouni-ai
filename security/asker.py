"""审批询问者（asker）。

PermissionPolicy 只裁定「要不要问」，真正去问的是这里的东西。

把它做成一个可注入的可调用对象，是为了让同一份 Agent 代码同时服务几种场合：

    CLI        → cli_asker（问终端）
    测试        → 一个脚本化的假 asker（零 stdin、零 mock）
    无人值守     → lambda tool, args: True（全部放行）
    将来的 Web  → 挂起并等待审批结果的 asker

Agent 只知道「我有个东西能问」，不知道它背后是终端、是脚本还是网络。
"""

import sys
from collections.abc import Callable, Mapping
from typing import Any

from agent_runtime.tools.tool import RiskLevel, Tool


# 一个 asker：给它工具和参数，回答「是否批准执行」。
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


def cli_asker(tool: Tool, arguments: Mapping[str, Any]) -> bool:
    """在终端上征求用户批准。

    三个细节都是刻意的：

    1. **提示写 stderr。** input() 自己的提示语走 stdout，而 stdout 是 Agent 最终
       产出的通道 —— 混进去会污染结果（重定向到文件时最明显）。所以提示一律用
       print(..., file=sys.stderr)，input() 不传提示参数。

    2. **参数按风险决定打多全。** 审批提示要给的是「判断依据」。中低风险工具的判断
       依据是「动作类型」—— write_file 的 content 动辄几千字符，全打出来反而把 path
       这种真正需要看的信息挤没了。高风险工具的判断依据就是参数本身，所以原样打全：
       shell 命令的重点常常在后半句，截断等于让用户在看不全的情况下签字。

    3. **读不到输入时返回 False（拒绝）。** 非交互环境（管道、CI、将来的 Web）里
       input() 会抛 EOFError。默认放行等于「无人值守时静默执行中风险操作」，
       默认拒绝才是安全的失败方向。
    """
    # 用 .get 而不是 []：将来加了新的风险等级而这张表忘了配，缺省值是「原样打全」。
    # 多显示一点是安全的失败方向，少显示才是危险的。
    limit = _PREVIEW_LIMIT_BY_RISK.get(tool.risk)
    args_preview = ", ".join(
        f"{name}={_preview(value, limit)}" for name, value in arguments.items()
    )

    print(f"[审批] 工具 {tool.name}  风险 {tool.risk.value}", file=sys.stderr)
    print(f"[审批] 参数 {args_preview}", file=sys.stderr)
    print("[审批] 是否执行？[y/N] ", end="", file=sys.stderr, flush=True)

    try:
        answer = input()
    except EOFError:
        print(file=sys.stderr)  # 补个换行，免得后续输出接在提示后面
        return False

    return answer.strip().lower() in {"y", "yes"}
