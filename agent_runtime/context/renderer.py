"""Context → LLM Messages。

它是整条链的最后一站，也是**唯一**知道"一条 Artifact 该长成什么样"的地方：

    ContextItem(artifact_id, representation, options)
        │
        ├── ArtifactStore.get / read / content
        │
        ▼
    一段文本
        │
        ▼
    一条 message

## 为什么渲染不归 ContextManager 管

设计原则第 4 条：「ContextManager 管状态，不管渲染」。分界很实用 —— 状态要落盘、
要被界面读、要在进程之间恢复，所以它必须是纯数据（JSON 可序列化）；而"怎么把
它变成文本"是纯粹的展示逻辑，改它不该动到状态、也不该让会话文件格式变一下。

## 渲染出来的载荷形状：**和重构之前逐字节一致**

系统提示词仍然是独立的一条 `role="system"`，工具结果仍然是 `role="tool"` 且
带着原来的 `tool_call_id`。理由不是保守：

  * provider 要求 `tool_calls` 和 tool 结果**配对**（少一条就 400）；
  * 把系统提示词挪进 `role="user"` 会打断前缀缓存的语义（那是整段请求里唯一
    能稳定命中的部分）。

所以这次重构换掉的是**内容的来源**（ArtifactStore 而不是历史里的正文），而不是
载荷的形状。`full` 档渲染出来的正文与原来完全一致（见 `render_content`：那一档
不加任何表头）。

## 降级是**可以说出来的**

`range` / `preview` / `metadata` 三档渲染出来会带一句说明。它不是装饰：模型看到
半份内容却以为看到了全部，是这一层唯一会静默出错的地方 —— 那句说明（"只给了
1800-1900 行，全文 5000 行"）是它据此决定"要不要再读一次"的唯一依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent_runtime.context import ref
from agent_runtime.context.artifact_store import ArtifactStore, Snippet
from agent_runtime.context.manager import ContextManager
from agent_runtime.context.models import Artifact, ContextItem, Representation

# `preview` 档默认给多少行（条目里没写 `preview_lines` 时用它）。
DEFAULT_PREVIEW_LINES = 40

# `metadata` 档渲染出来的样子：它**只有一行**，所以那些字段必须省着写。
_METADATA_LABELS = {
    "tool": "工具",
    "path": "路径",
    "url": "网址",
    "command": "命令",
    "pattern": "模式",
    "query": "查询",
    "lines": "行数",
    "chars": "字符",
    "status": "状态",
}


@dataclass(frozen=True, slots=True)
class Rendered:
    """一份 Artifact 按某个档位展开的结果。"""

    text: str
    snippet: Snippet | None = None
    # 取不到正文（文件被人删了、或者会话是从一个只带了索引的备份恢复的）。
    # **它必须被说出来** —— 一条空的 tool 结果会让模型以为那次工具没输出。
    missing: bool = False


class ContextRenderer:
    """把 Context 渲染成一次请求的 messages。"""

    def __init__(
        self,
        store: ArtifactStore,
        manager: ContextManager,
        *,
        preview_lines: int = DEFAULT_PREVIEW_LINES,
    ) -> None:
        self.store = store
        self.manager = manager
        self.preview_lines = preview_lines

    # -- 一份 Artifact ----------------------------------------------------------

    def render_item(self, item: ContextItem) -> str | None:
        """按条目当前的档位渲染。**预算估算走这一条**（见 `manager.fit`）。

        返回 `None` = 这个条目不该出现在载荷里（被挤出去了，或者正文取不到）。
        和下面那条的区别是：这里容忍取不到（估算不需要为它编一段文本出来），
        而载荷那一条必须给出**一句话**，见 `render_tool_content`。
        """
        if item.removed:
            return None
        artifact = self.store.get(item.artifact_id)
        if artifact is None:
            return None
        rendered = self.render_artifact(artifact, item.representation, item.options)
        return None if rendered.missing else rendered.text

    def render_artifact(
        self,
        artifact: Artifact,
        representation: Representation,
        options: Mapping[str, Any] | None = None,
    ) -> Rendered:
        """一份 Artifact + 一个档位 → 一段文本。"""
        options = options or {}

        if representation is Representation.METADATA:
            return Rendered(self.metadata_line(artifact))

        if representation is Representation.FULL:
            text = self.store.content(artifact.artifact_id)
            if text is None:
                return Rendered(self.missing_line(artifact), missing=True)
            return Rendered(text)

        if representation is Representation.RANGE:
            start = _int(options.get("start_line"), 1)
            end = _int(options.get("end_line"), 0) or None
            snippet = self.store.read(artifact.artifact_id, start=start, end=end,
                                      max_chars=_optional_int(options.get("max_chars")))
            if snippet is None:
                return Rendered(self.missing_line(artifact), missing=True)
            return Rendered(_range_text(artifact, snippet, options), snippet=snippet)

        # PREVIEW
        lines = _int(options.get("preview_lines"), self.preview_lines)
        snippet = self.store.preview(
            artifact.artifact_id, lines=max(1, lines),
            max_chars=_optional_int(options.get("max_chars")),
        )
        if snippet is None:
            return Rendered(self.missing_line(artifact), missing=True)
        return Rendered(_preview_text(artifact, snippet, options), snippet=snippet)

    def metadata_line(self, artifact: Artifact) -> str:
        """`metadata` 档：**一行**关于正文的事实，一个字的正文都没有。

        它是降级的最后一档，所以它仍然必须是有用的：模型据此知道"有这么一份东西、
        它是什么、有多大、要不要再读一次"。
        """
        parts: list[str] = []
        for key, label in _METADATA_LABELS.items():
            value = _metadata_value(artifact, key)
            if value not in (None, ""):
                parts.append(f"{label}={value}")
        return f"[artifact {ref.shown_id(artifact.artifact_id)}] " + " ".join(parts)

    def missing_line(self, artifact: Artifact) -> str:
        """正文取不到时给模型的那句话。

        **不能是空串。** 空串会被读成"这个工具什么都没返回"，而那就是一个错误
        的结论 —— 它会让模型换一条完全没必要的路重做一遍。说清"内容已经不在
        磁盘上了"它才知道该重新读一次。
        """
        return (
            f"[artifact {ref.shown_id(artifact.artifact_id)} 的内容已经取不到了"
            f"（原始大小 {artifact.chars} 字符）——需要的话请重新执行一次那个工具]"
        )

    # -- 一整批 ----------------------------------------------------------------

    def render_tool_content(self, message: Mapping[str, Any]) -> str:
        """一条 tool 消息该发给模型的正文。

        五种情况，都要有确定的答案：

          1. 它引用一份在 Context 里的 Artifact ⇒ 按档位渲染；
          2. 引用的 Artifact 被预算挤出去了 ⇒ 一句"这一轮看不到它了"（**不是空串**，
             也不是原样照发 —— 后者等于降级没生效）；
          3. 引用的 Artifact 正文丢了 ⇒ `missing_line`；
          4. 它根本不引用 Artifact（老会话、或者一条普通的 tool 消息）⇒ 原样；
          5. 引用存在但我们这边没有它的条目（不该发生）⇒ 原样照发引用本身。
             这是唯一一个"宁可多发一点也不要丢信息"的方向。
        """
        artifact = ref.artifact_id_of(message)
        if artifact is None:
            content = message.get("content")
            return content if isinstance(content, str) else ""

        item = self.manager.item(artifact)
        if item is None:
            content = message.get("content")
            return content if isinstance(content, str) else ""

        if item.removed:
            known = self.store.get(artifact)
            size = known.chars if known is not None else 0
            return (
                f"[artifact {ref.shown_id(artifact)} 因为上下文预算被移出了本次上下文"
                f"（原始大小 {size} 字符）——需要的话请重新执行一次那个工具]"
            )

        stored = self.store.get(artifact)
        if stored is None:
            content = message.get("content")
            return content if isinstance(content, str) else ""

        rendered = self.render_artifact(stored, item.representation, item.options)
        return rendered.text

    def build(
        self,
        messages: list[dict[str, Any]],
        *,
        tail: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """把历史和 Context 合成一次请求的载荷。

        **顺序一个字都不动**：和 `session.messages` 完全同序，只是把 tool 消息的
        内容换成了按档位渲染的结果（设计原则第 8 条：不要为了优化而重排已有
        Context —— 那会让前缀缓存整个作废）。

        `tail` 是载荷末尾那条临时消息（会话状态 + 步数预算，见 `agents/agent.py`
        的 `_status_note`）。它**不进历史**，所以由调用方拼好传进来。
        """
        payload: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "tool":
                rendered = dict(message)
                rendered["content"] = self.render_tool_content(message)
                payload.append(rendered)
            else:
                payload.append(message)
        if tail is not None:
            payload.append(tail)
        return payload


def _range_text(
    artifact: Artifact, snippet: Snippet, options: Mapping[str, Any]
) -> str:
    """`range` 档的正文：表头 + 那几行。"""
    total = snippet.total_lines or int(artifact.metadata.get("lines") or 0)
    where = _where(artifact)
    head = (
        f"[artifact {ref.shown_id(artifact.artifact_id)}：{where}"
        f"第 {snippet.start_line}-{snippet.end_line} 行"
    )
    if total:
        head += f"（共 {total} 行）"
    head += "]"
    if not snippet.text:
        return f"{head}\n（这个区间没有内容）"
    return f"{head}\n{snippet.text}"


def _preview_text(
    artifact: Artifact, snippet: Snippet, options: Mapping[str, Any]
) -> str:
    """`preview` 档的正文：开头几行 + 一句"后面还有"。"""
    total = snippet.total_lines or int(artifact.metadata.get("lines") or 0)
    where = _where(artifact)
    head = f"[artifact {ref.shown_id(artifact.artifact_id)}：{where}开头 {snippet.end_line} 行"
    if total:
        head += f"（共 {total} 行）"
    head += "]"
    if not snippet.text:
        return f"{head}\n（没有内容）"
    return f"{head}\n{snippet.text}"


def _where(artifact: Artifact) -> str:
    """表头里那句"这是什么东西"。

    **路径排第一**（它是最能让人认出来的那一格），而且只在真的有路径时才写 ——
    一个恒为空的"路径="会让读的人以为那份 Artifact 真的没有路径。
    """
    for key, label in (("path", ""), ("url", ""), ("command", "命令 "),
                       ("pattern", "模式 "), ("query", "查询 ")):
        value = artifact.metadata.get(key)
        if value:
            return f"{label}{value}，"
    if artifact.source.path:
        return f"{artifact.source.path}，"
    return ""


def _metadata_value(artifact: Artifact, key: str) -> Any:
    if key == "id":
        return artifact.artifact_id
    if key == "chars":
        return artifact.chars
    if key == "type":
        return artifact.type
    value = artifact.metadata.get(key)
    if value in (None, ""):
        value = getattr(artifact.source, key, "")
    return value


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    """有值才转，`None` / 坏值一律当"没设上限"。

    坏值不当 0：`max_chars=0` 会让一份 Artifact 渲染成空文本，而它在载荷里和
    "这个工具什么都没返回"长得一模一样 —— 那正是这一层最该避免的失败形态。
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = ["DEFAULT_PREVIEW_LINES", "ContextRenderer", "Rendered"]
