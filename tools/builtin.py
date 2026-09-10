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

    path: str = Field(default=".", min_length=1, description="目录路径，默认为当前目录")


class GetCurrentTimeArgs(ToolArgs):
    """get_current_time 的参数。

    一个字段都没有 —— 拿当前时间不需要任何输入，也就没有"模型填错参数"这条路。
    空模型仍然要存在，是因为 Tool 的契约要求 args_model 必填：schema 和校验都
    从它推导，绕开它就得手写一份 schema，那就回到"两份事实互相漂移"的老问题。
    """


def create_tool_registry(workspace: str) -> ToolRegistry:
    """把内置工具装成一个注册表。

    workspace 既是文件工具的沙箱根，也是唯一能拦住"往工作区外面写"的东西 ——
    所以传进来的应该是项目目录，而不是它的父目录。
    """
    fs = FileSystem(workspace)

    registry = ToolRegistry()

    registry.register(Tool(
        name="read_file",
        description="读取文件内容",
        risk=RiskLevel.LOW,
        args_model=ReadFileArgs,
        handler=fs.read_file,
    ))

    registry.register(Tool(
        name="write_file",
        description="写入文件内容",
        risk=RiskLevel.MEDIUM,
        args_model=WriteFileArgs,
        handler=fs.write_file,
    ))

    registry.register(Tool(
        name="list_files",
        description="列出目录下的文件",
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

    return registry
