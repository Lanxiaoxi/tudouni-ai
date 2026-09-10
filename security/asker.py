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

from agent_runtime.tools.tool import Tool


# 一个 asker：给它工具和参数，回答「是否批准执行」。
ApprovalAsker = Callable[[Tool, Mapping[str, Any]], bool]

# 审批提示里每个参数值的最大预览长度。实测 write_file 的 content 有 3401 字符，
# 整个打出来会把审批提示淹掉，用户根本看不到「要写哪个文件」这个关键信息。
_PROMPT_PREVIEW = 120


def _preview(value: Any, limit: int = _PROMPT_PREVIEW) -> str:
    """把参数值压成单行短预览，过长则截断并标注真实长度。"""
    text = value if isinstance(value, str) else repr(value)
    flat = text.replace("\n", "\\n")
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…(共 {len(flat)} 字符)"


def cli_asker(tool: Tool, arguments: Mapping[str, Any]) -> bool:
    """在终端上征求用户批准。

    三个细节都是刻意的：

    1. **提示写 stderr。** input() 自己的提示语走 stdout，而 stdout 是 Agent 最终
       产出的通道 —— 混进去会污染结果（重定向到文件时最明显）。所以提示一律用
       print(..., file=sys.stderr)，input() 不传提示参数。

    2. **参数只打预览。** 审批提示要给的是「判断依据」，不是完整数据。write_file
       的 content 动辄几千字符，全打出来反而把 path 这种真正需要看的信息挤没了。

    3. **读不到输入时返回 False（拒绝）。** 非交互环境（管道、CI、将来的 Web）里
       input() 会抛 EOFError。默认放行等于「无人值守时静默执行中风险操作」，
       默认拒绝才是安全的失败方向。
    """
    args_preview = ", ".join(f"{name}={_preview(value)}" for name, value in arguments.items())

    print(f"[审批] 工具 {tool.name}  风险 {tool.risk.value}", file=sys.stderr)
    print(f"[审批] 参数 {args_preview}", file=sys.stderr)
    print("[审批] 是否执行？[y/N] ", end="", file=sys.stderr, flush=True)

    try:
        answer = input()
    except EOFError:
        print(file=sys.stderr)  # 补个换行，免得后续输出接在提示后面
        return False

    return answer.strip().lower() in {"y", "yes"}
