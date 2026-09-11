"""内置工具的装配。

工具的「参数格式」在这里定义一次、两用：

  1. 生成发给模型的 schema（**预防**：模型提前看到约束）
  2. 校验模型实际给出的参数（**兜底**：错了就明确告诉它哪个字段不对）

两者同源，所以不会漂移 —— 这条是第 4 阶段的核心结论。

为什么放在 tools/ 而不是入口：它描述的是「这个项目自带哪些工具」，属于工具层
的知识。放在入口里会有一个具体代价 —— 测试为了拿到一个工具注册表，不得不
import 整个应用入口（连带把 CLI、httpx、配置全都拖进来）。
"""

from pydantic import Field

from .clock import get_current_time
from .filesystem import FileSystem
from .shell import (
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    Shell,
    shell_name,
)
from .tool import RiskLevel, Tool, ToolArgs, ToolRegistry


class ReadFileArgs(ToolArgs):
    """read_file 的参数。"""

    path: str = Field(min_length=1, description="文件路径")


class WriteFileArgs(ToolArgs):
    """write_file 的参数。"""

    path: str = Field(min_length=1, description="文件路径")
    content: str = Field(description="文件内容")


class ListFilesArgs(ToolArgs):
    """list_files 的参数。

    path 带默认值，所以生成的 schema 里它不是必填 —— 模型可以省略它。
    """

    path: str = Field(
        default=".",
        min_length=1,
        description="目录路径（相对于工作区），默认为工作区根目录",
    )


class GetCurrentTimeArgs(ToolArgs):
    """get_current_time 的参数。

    一个字段都没有 —— 拿当前时间不需要任何输入，也就没有"模型填错参数"这条路。
    空模型仍然要存在，是因为 Tool 的契约要求 args_model 必填：schema 和校验都
    从它推导，绕开它就得手写一份 schema，那就回到"两份事实互相漂移"的老问题。
    """


class ShellArgs(ToolArgs):
    """shell 的参数。

    command 写成字符串而不是 argv 数组，是因为模型要用的能力（管道、重定向、多个
    命令串联）本来就得由 shell 解析 —— 拆成数组就等于把这些能力砍掉，而安全边界
    无论如何都落在审批那一道，不在这里（见 tools/shell.py 的模块注释）。

    timeout_seconds 的边界写成 ge/le，而不是让 shell.py 自己去夹：范围必须和 schema
    同源，模型才可能**提前**看到「最多 300 秒」，而不是事后收到一个被悄悄改过的值 ——
    而且工具调用每次都要过人工审批，撞一次参数错误就得再问一次人。
    """

    command: str = Field(
        min_length=1,
        description=f"要执行的命令，按 {shell_name()} 的语法写",
    )
    timeout_seconds: int = Field(
        default=TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="最多等这条命令多少秒。默认值只够 ls / git status 这种秒回的命令，"
                    "装依赖、跑测试这类慢命令要显式调大",
    )


def create_tool_registry(workspace: str) -> ToolRegistry:
    """把内置工具装成一个注册表。

    workspace 既是文件工具的沙箱根，也是唯一能拦住"往工作区外面写"的东西 ——
    所以传进来的应该是项目目录，而不是它的父目录。
    """
    fs = FileSystem(workspace)
    shell = Shell(workspace)

    registry = ToolRegistry()

    registry.register(Tool(
        name="read_file",
        description=(
            "读取指定文件的全部内容（按 UTF-8 解码，不分页）。"
            "文件不存在、路径指向目录、或超出工作区都会报错。"
        ),
        risk=RiskLevel.LOW,
        args_model=ReadFileArgs,
        handler=fs.read_file,
    ))

    registry.register(Tool(
        name="write_file",
        description=(
            "把内容写入指定文件。整个文件会被替换 —— 不是追加，也不是局部修改；"
            "缺失的父目录会自动创建。所以要改动一个已存在的文件，必须先 read_file "
            "读出原文，再基于真实内容写出完整的新文本：凭记忆或凭猜测写会丢数据。"
        ),
        risk=RiskLevel.MEDIUM,
        args_model=WriteFileArgs,
        handler=fs.write_file,
    ))

    registry.register(Tool(
        name="list_files",
        description=(
            "列出目录下的条目名字。只列一层、不递归，也不返回大小、类型或修改时间。"
            "要摸清目录结构就逐层调用；目录不存在或路径不是目录会报错。"
        ),
        risk=RiskLevel.LOW,
        args_model=ListFilesArgs,
        handler=fs.list_files,
    ))

    # 风险定 LOW：它只读时钟、没有副作用、不碰工作区，放行不需要问人 ——
    # 和 read_file / list_files 同档，也就落在 main.py 的 auto_approve 里。
    registry.register(Tool(
        name="get_current_time",
        description="获取当前时间（ISO 8601，含时区偏移）",
        risk=RiskLevel.LOW,
        args_model=GetCurrentTimeArgs,
        handler=get_current_time,
    ))

    # 风险定 HIGH，而且**刻意不做参数级判断** —— 理由见 tools/shell.py 的模块注释。
    # 后果是明确的：main.py 的 auto_approve 里只有 LOW，所以每一条命令都会被拦下来
    # 问人。这是唯一诚实的默认值 —— 想给"只读命令"开自动放行，得先有 OS 级沙箱。
    #
    # 描述里的输出上限是从 shell.py 的常量插值来的，不手抄第二份 —— 参数在一处改、
    # 说明书跟着变，和 tool.py 里「schema 由 args_model 推导」是同一条原则。
    # 超时那件事不在这里复述：它是 timeout_seconds 这个参数自己的约束，由 schema 的
    # default/minimum/maximum 表达，写在散文里就成了第二份。
    registry.register(Tool(
        name="shell",
        description=(
            f"在工作区目录下执行一条 shell 命令（{shell_name()} 语法），返回输出和退出码。"
            f"命令是非交互的：需要输入时会立刻读到 EOF。"
            f"输出超过 {MAX_OUTPUT_CHARS} 字符会掐掉中间，头和尾都留着。"
            f"它不受文件工具那条路径限制 —— 命令能碰到工作区之外的路径。"
        ),
        risk=RiskLevel.HIGH,
        args_model=ShellArgs,
        handler=shell.run,
    ))

    return registry
