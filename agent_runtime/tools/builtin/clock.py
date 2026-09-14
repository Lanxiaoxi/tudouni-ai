"""读取当前时间的工具实现。

为什么单独一个模块，而不是塞进 filesystem.py：两者的依赖完全不同。文件工具需要
workspace 这个沙箱边界，必须被注入一个 FileSystem 实例；时间工具**无状态、无边界、
不需要注入任何东西**。硬塞在一起，会让「文件沙箱」这个概念被一个与文件无关的函数
稀释，读代码的人还得先确认时间函数到底用没用 workspace。

和 filesystem.py 一样，这里只有**执行**；风险等级在装配处（tools/builtin/__init__.py）
给，而参数格式就在下面 —— 它和 handler 在同一个文件里。
"""

from datetime import datetime

from ..tool import ToolArgs


class GetCurrentTimeArgs(ToolArgs):
    """get_current_time 的参数。

    一个字段都没有 —— 拿当前时间不需要任何输入，也就没有"模型填错参数"这条路。
    空模型仍然要存在，而不是让 args_model 空着去手写一份 external_schema：内置工具的
    schema 和校验都从 args_model 推导（见 tool.py），绕开它就得手写第二份 schema，
    那就回到"两份事实互相漂移"的老问题。external_schema 是留给**别人的** schema 的
    （MCP，见 tools/mcp.py），不是省一个空类的捷径。
    """


def get_current_time() -> str:
    """返回本机当前时间，ISO 8601 格式（秒级、带 UTC 偏移）。

    用 `.astimezone()` 而不是裸的 `datetime.now()`：后者返回的是 **naive** 时间，
    isoformat() 不会带上偏移，于是"12:34:56"这个字符串**无法还原成一个时刻** ——
    换台机器、换个时区解读，就是另一个时间点。带了偏移（如 +08:00）才是唯一的。
    """

    # 刻意不接受任何参数（比如"给我某个时区的时间"）：一来无参就不可能被模型填错，
    # 二来命名时区依赖 IANA 时区库（Windows 上还需要额外的 tzdata 包），
    # 那会让这个本该零依赖的工具多出一个可失败点。真需要别的时区，模型完全可以
    # 拿到本机时间 + 偏移之后自己换算。
    return datetime.now().astimezone().isoformat(timespec="seconds")
