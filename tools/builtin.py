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


def create_tool_registry(workspace: str) -> ToolRegistry:
    """把内置的文件工具装成一个注册表。

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

    return registry
