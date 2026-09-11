"""工具的参数格式：schema 由模型推导、校验真的拦住、路径真的越不出去。"""

import json

import pytest

from agent_runtime.tools.builtin import ListFilesArgs, ReadFileArgs, create_tool_registry
from agent_runtime.tools.filesystem import FileSystem
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry, ToolArgs


def test_schema_is_derived_from_args_model():
    """schema 从参数模型推导 —— 手写第二份就会漂移。

    这里验的就是"手写版永远写不出"的那些约束：minLength、additionalProperties。
    """
    registry = create_tool_registry(".")
    schema = {s["function"]["name"]: s["function"]["parameters"] for s in registry.schemas()}

    assert schema["read_file"]["properties"]["path"]["minLength"] == 1
    assert schema["read_file"]["required"] == ["path"]
    assert schema["read_file"]["additionalProperties"] is False
    # list_files 的 path 有默认值，所以不是必填 —— 模型可以省略
    assert "required" not in schema["list_files"]


def test_top_level_description_is_stripped():
    """Pydantic 会把模型类的 docstring 写进 schema 的 description。

    那些是给开发者看的内部注释，不该跟着每次请求发给模型 —— 所以基类里统一剥掉，
    但字段级 description（来自 Field(description=...)）要保留。
    """
    registry = create_tool_registry(".")
    read_file = next(s for s in registry.schemas() if s["function"]["name"] == "read_file")
    params = read_file["function"]["parameters"]

    assert "description" not in params
    assert params["properties"]["path"]["description"] == "文件路径"


def test_risk_is_declared_and_not_leaked_into_schema():
    registry = create_tool_registry(".")
    assert registry.get("read_file").risk is RiskLevel.LOW
    assert registry.get("write_file").risk is RiskLevel.MEDIUM
    assert "risk" not in json.dumps(registry.schemas(), ensure_ascii=False)


def test_tool_without_risk_cannot_be_constructed():
    """漏声明风险必须不可能 —— 默认成 LOW 就是静默放行，最坏的 fail-open。"""
    with pytest.raises(TypeError):
        Tool(name="x", description="d", args_model=ListFilesArgs, handler=lambda **k: None)


@pytest.mark.parametrize("arguments", [
    {"path": 123},              # 类型错
    {"path": ""},               # 空路径
    {"path": "   "},            # 只挡空串，挡不住空格 —— 记录现状
])
def test_validation_rejects_bad_arguments(arguments):
    registry = create_tool_registry(".")
    if arguments == {"path": "   "}:
        registry.get("list_files").execute(arguments)     # 能过，只是说明边界在哪
    else:
        with pytest.raises(Exception):
            registry.get("list_files").execute(arguments)


def test_extra_arguments_are_rejected():
    """Pydantic 默认是 extra="ignore" —— 会把多余参数静默丢掉。

    那样「模型读错了 schema」这件事就被藏起来了，所以基类统一改成 forbid。
    """
    registry = create_tool_registry(".")
    with pytest.raises(Exception) as exc:
        registry.get("list_files").execute({"path": ".", "bogus": 1})
    assert "bogus" in str(exc.value)


def test_empty_path_never_reaches_the_handler():
    """空路径必须在参数层就被拦住，而不是走到 OS 层报一个看不懂的错。"""
    registry = create_tool_registry(".")
    with pytest.raises(Exception) as exc:
        registry.get("read_file").execute({"path": ""})
    assert "at least 1 character" in str(exc.value)


def test_safe_path_blocks_escaping_the_workspace(workdir):
    """工作区边界 —— 这条挡住了"agent 去改同级其它项目"。"""
    workspace = workdir / "ws"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("ok", encoding="utf-8")
    fs = FileSystem(str(workspace))

    assert fs.read_file("inside.txt") == "ok"
    for escape in ["../outside.txt", "..\\outside.txt", "../../etc/passwd"]:
        with pytest.raises(PermissionError):
            fs.read_file(escape)


def test_workspace_root_itself_is_reachable_but_only_inside(workdir):
    """空路径会解析成工作区自己 —— 这是"参数校验管不了路径安全"的具体证据。

    min_length=1 挡住的是空串，`"."` 和 `"   "` 它都放行，真正兜底的是 safe_path。
    """
    workspace = workdir / "ws"
    workspace.mkdir()
    fs = FileSystem(str(workspace))
    assert fs.safe_path(".") == workspace.resolve()


def test_tool_descriptions_are_not_bare_labels():
    """描述是模型决定要不要调这个工具时唯一能看到的东西。

    「写入文件内容」这样的标签等于没写。Claude Code 的 Tools 段比它的系统提示词
    还长（44,145 字符 vs 12,399），说明说明书本来就该住在这里。这条挡的是"加新工具
    时顺手写一行标签"。
    """
    registry = create_tool_registry(".")
    for tool in registry.all():
        assert len(tool.description) >= 15, f"{tool.name} 的描述太短，等于没写"


def test_write_file_description_warns_that_it_overwrites():
    """没有 edit 工具，只有整文件覆盖的 write_file —— 这条警告是防丢数据的。

    真正的解法是加一个 edit 工具；在那之前，这句话必须留在模型看得见的地方。
    """
    registry = create_tool_registry(".")
    assert "整个文件会被替换" in registry.get("write_file").description


def test_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(Tool(name="t", description="d", risk=RiskLevel.LOW,
                           args_model=ListFilesArgs, handler=lambda **k: None))
    with pytest.raises(ValueError):
        registry.register(Tool(name="t", description="d", risk=RiskLevel.LOW,
                               args_model=ListFilesArgs, handler=lambda **k: None))
