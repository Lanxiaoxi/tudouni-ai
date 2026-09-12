"""工具层的包出口 —— **只有契约**。

这里只导出 Tool / ToolRegistry / ToolArgs / ToolResult / RiskLevel / InvalidArgsError。

**为什么不把内置工具和 mcp 也 re-export 一遍**（这里原来有 35 个名字，实测**一个消费者
都没有**）：代价是实打实的 —— `security/policy.py` 只想要一个 `Tool`，而 Python 在
import `agent_runtime.tools.tool` 之前会先执行这个 `__init__`，于是 httpx、
agent_runtime.skills 全被拖进来（实测 16 个内部模块）。一个"公开 API"如果没人用、却要
每一个消费者替它付加载代价，那它不是 API，是负债。

那两样东西各有各的地方：

  * `tools/builtin/`  内置工具：一个工具一个模块（参数模型与 handler 同居），装配在
                      它自己的 `__init__.py` 里（`create_tool_registry`）
  * `tools/mcp.py`    外部 server：传输 + 协议 + 把外部工具适配成 Tool
"""

from .tool import InvalidArgsError, RiskLevel, Tool, ToolArgs, ToolRegistry, ToolResult

__all__ = [
    "InvalidArgsError",
    "RiskLevel",
    "Tool",
    "ToolArgs",
    "ToolRegistry",
    "ToolResult",
]
