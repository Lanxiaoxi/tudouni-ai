"""ToolResult → Artifact。

它是设计原则第 1 条（"ToolResult 不直接进入 Context"）的**唯一**执行者：工具
负责产生信息，这里负责把那份信息变成一份可以被以后引用的 Artifact。

## 为什么需要一个"处理器"、而不是让工具自己造 Artifact

因为工具会越来越多，而 Context 层不该跟着改。"一次搜索产生了哪些 Artifact"
是搜索工具的知识，不是 Context 的知识 —— 所以那一层做成**按工具名注册的策略**：
默认策略处理一切，`read_file` 这种有额外形状（路径、行号）的可以自己挂一条。

    read_file  → [File Artifact]                 （带 path / 行数）
    grep       → [SearchResult Artifact]         （带命中数、模式）
    shell      → [CommandOutput Artifact]        （带命令、退出码）
    fetch_web  → [WebPage Artifact]              （带 URL、状态码）

## 默认策略：一次工具调用 = 一份 Artifact

这条默认**不是偷懒**，而是"完整重放"的要求：会话历史里 tool 消息存的是引用
（见 `context/ref.py`），所以每一份内容都必须真的有一份 Artifact 兜着。少了
哪一份，那一轮之后模型就再也看不到那次工具结果了 —— 而且它看起来像"工具没
返回东西"，不像一个 bug。

（反过来说：短结果确实不**需要**拆分或压缩。它照样进 Artifact，只是它的
representation 永远是 `full` —— 预算那一层不会去动一个只有 20 个字符的东西。）
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runtime.context.artifact_store import ArtifactStore
from agent_runtime.context.models import Artifact, ArtifactSource
from agent_runtime.tools.tool import ToolResult


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """一次工具调用的全部事实。**处理器需要的都在这里。**

    为什么不直接传 `ToolResult` + 一堆参数：处理器要判断"这份结果是什么形状"，
    而它需要的四样东西（工具名、参数、结果、结局）恰好就是"一次调用"这个概念。
    分散成四个参数之后，加一个字段就要改所有策略的签名。
    """

    tool: str
    arguments: Mapping[str, Any]
    result: ToolResult
    # ok / denied / invalid_args / error（和审计里那个 status 同一个词表）。
    status: str = "ok"


# 策略的签名。返回的 Artifact 已经收进 store 了 —— 处理器是唯一写 store 的人，
# 这样"哪些东西成了 Artifact"只有一处能回答。
Strategy = Callable[[ArtifactStore, ToolExecution], Artifact]


class ToolResultProcessor:
    """把一次工具执行的结局变成 Artifact。**按工具名可以定制。**

    定制的力度刻意做小：策略只决定"要不要在默认那份 Artifact 之外**多**记几份"，
    或者"metadata 里该带什么"。它**不能**决定"这次不记 Artifact" —— 那会让历史
    里的引用悬空（见模块 docstring）。
    """

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, tool: str, strategy: Strategy) -> None:
        """给某个工具挂一条策略。**同名覆盖**（后注册的说了算）。

        覆盖而不是报错：装配期按工具名注册，而 MCP 的工具是**运行中挂上来的**
        （`/mcp load`），名字由别人决定。报错的话，一个恰好叫 `read_file` 的外部
        工具就会让装配炸掉 —— 而那不是用户的错。
        """
        self._strategies[tool] = strategy

    def process(self, store: ArtifactStore, execution: ToolExecution) -> list[Artifact]:
        """这次执行产生了哪些 Artifact。**至少一份。**

        第一份永远是默认那份（正文原样），后面才是策略额外加的。顺序是有意的：
        调用方拿 `artifacts[0]` 当"这次工具结果"往 Context 里放（见 agent.py），
        而"另有一份摘要 Artifact"这种事不该改变那一条。
        """
        primary = self._primary(store, execution)
        extra = self._extra(store, execution)
        return [primary, *extra]

    def _primary(self, store: ArtifactStore, execution: ToolExecution) -> Artifact:
        strategy = self._strategies.get(execution.tool)
        if strategy is not None:
            return strategy(store, execution)
        return default_strategy(store, execution)

    def _extra(self, store: ArtifactStore, execution: ToolExecution) -> list[Artifact]:
        # V1 **不做**"额外 Artifact"（摘要、匹配文件清单之类）：设计稿第 23 条
        # 明确把自动 Summary 排除在外，而一条不产生额外东西的扩展点最难写错。
        # 留着这个方法是让"插进哪里"在代码里有个位置 —— 将来那条 `search` 策略
        # 要返回三份 Artifact 时，改的是这里，而不是 `process` 的结构。
        return []


def default_strategy(store: ArtifactStore, execution: ToolExecution) -> Artifact:
    """默认：整份正文一份 Artifact，类型按工具名粗分。

    类型只影响渲染时 `range` 怎么切（文本按行、别的按字符），所以它不需要精确 ——
    需要精确的是 metadata，而那是各工具自己的策略该补的。
    """
    return store.create(
        execution.result.text,
        type=_type_of(execution.tool),
        source=ArtifactSource(tool=execution.tool),
        metadata=_base_metadata(execution),
    )


def filesystem_strategy(store: ArtifactStore, execution: ToolExecution) -> Artifact:
    """读 / 写 / 改文件的那几个工具：把路径和行数记进 metadata。

    这三个字段是 `range` 那一档的全部依据 —— 模型说"我要 1800-1900 行"时，
    渲染器要知道这份 Artifact 真的是那个文件，以及它一共有多少行。
    """
    text = execution.result.text
    path = str(execution.arguments.get("path") or "")
    metadata = _base_metadata(execution)
    metadata.update({
        "path": path,
        "lines": _count_lines(text),
    })
    # `read_file` 才是"这是一份文件正文"；write_file / edit_file 返回的是一句话，
    # 记成 file 会让 `range` 那一档去切一句没有任何行的结果。
    kind = "file" if execution.tool == "read_file" else "text"
    return store.create(
        text,
        type=kind,
        source=ArtifactSource(tool=execution.tool, path=path),
        metadata=metadata,
    )


def search_strategy(store: ArtifactStore, execution: ToolExecution) -> Artifact:
    """grep / web_search：把"搜的是什么"记下来。"""
    metadata = _base_metadata(execution)
    for key in ("pattern", "query", "path", "include"):
        value = execution.arguments.get(key)
        if value:
            metadata[key] = value if isinstance(value, (int, float, bool)) else str(value)
    return store.create(
        execution.result.text,
        type="search",
        source=ArtifactSource(tool=execution.tool),
        metadata=metadata,
    )


def command_strategy(store: ArtifactStore, execution: ToolExecution) -> Artifact:
    """shell 和后台任务：命令原文是事后最要紧的一件事。"""
    metadata = _base_metadata(execution)
    command = execution.arguments.get("command")
    if command:
        metadata["command"] = str(command)
    return store.create(
        execution.result.text,
        type="command",
        source=ArtifactSource(tool=execution.tool),
        metadata=metadata,
    )


def web_strategy(store: ArtifactStore, execution: ToolExecution) -> Artifact:
    """fetch_web：URL 是它的身份。"""
    metadata = _base_metadata(execution)
    url = execution.arguments.get("url")
    if url:
        metadata["url"] = str(url)
    return store.create(
        execution.result.text,
        type="web",
        source=ArtifactSource(tool=execution.tool, url=str(url or "")),
        metadata=metadata,
    )


def default_processor() -> ToolResultProcessor:
    """装配里用的那一份：默认策略 + 那几个有额外形状的工具。"""
    processor = ToolResultProcessor()
    for name in ("read_file", "write_file", "edit_file", "list_files"):
        processor.register(name, filesystem_strategy)
    for name in ("grep", "web_search"):
        processor.register(name, search_strategy)
    for name in ("shell", "run_command", "job_output", "job_wait", "job_list"):
        processor.register(name, command_strategy)
    processor.register("fetch_web", web_strategy)
    return processor


def _base_metadata(execution: ToolExecution) -> dict[str, Any]:
    """每一种 Artifact 都该带上的那几格。"""
    data: dict[str, Any] = {"status": execution.status}
    if execution.result.audit:
        # 工具自己带回来的审计字段（ask_user 的 question_status、shell 的退出码……）。
        # **照抄进 metadata 而不是丢掉**：它是工具唯一知道的事实，而 Artifact 正是
        # "这份信息附带的事实"该待的地方。审计那一份仍然独立存在（两条出口服务
        # 两个读者：审计给人查账，metadata 给渲染器决定怎么展示）。
        data.update({str(k): v for k, v in execution.result.audit.items()})
    return data


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return len(text.split("\n")) - (1 if text.endswith("\n") else 0)


def _type_of(tool: str) -> str:
    """工具名 → 粗粒度类型。**认不出就当纯文本**（那是唯一不会出错的兜底）。"""
    if tool in ("read_file", "write_file", "edit_file", "list_files"):
        return "file"
    if tool in ("shell", "run_command", "job_output", "job_wait", "job_list"):
        return "command"
    if tool in ("grep", "web_search"):
        return "search"
    if tool == "fetch_web":
        return "web"
    return "text"


__all__ = [
    "Strategy",
    "ToolExecution",
    "ToolResultProcessor",
    "command_strategy",
    "default_processor",
    "default_strategy",
    "filesystem_strategy",
    "search_strategy",
    "web_strategy",
]
