"""Context 系统的四个核心对象。**这一层不认识 Agent、不认识工具、也不读盘。**

    ToolResult      这次工具执行发生了什么（运行时事件）
    Artifact        一份可以被以后引用的信息（数据本身）
    ContextItem     「我现在想让 LLM 怎么看到这份信息」（数据的使用方式）
    ContextState    当前这一轮 Context 的全貌

分得这么细，是因为它们各自回答一个不同的问题，而混在一起之后那些问题就再也
分不开了：

    ToolResult   →  「刚刚发生了什么？」
    Artifact     →  「我有什么信息？」
    ContextItem  →  「我现在要让模型看见哪一份、以什么形态看见？」
    ContextState →  「此刻 Context 长什么样？」

## 为什么 Artifact 里没有正文

`content_ref` 是一个指针，正文在 `ArtifactStore` 里（磁盘上）。理由不是洁癖：
ContextState 会被写进会话文件、会被发给模型看的那些逻辑反复遍历，而它一旦能装
得下 12 万字符的文件正文，"大数据永远不要进 ContextState"这条原则就只剩下一句
注释了。指针还让同一份信息可以有不同的引用方式（`ContextItem.representation`）。

## 为什么 ContextItem 要有 zone

`stable` / `dynamic` 是从 provider 的**前缀缓存**倒推出来的：整段请求里只有前缀
稳定命中的部分便宜（官方价里未命中的输入贵约 50 倍）。所以"这条信息会不会变"
必须写在数据上，而不是留给渲染那一层去猜 —— 猜错的代价是每一轮都在为同一段
文字付未命中的价。

`system` 是第三种 zone，代表"渲染成 role=system 那一条"（系统提示词）。它单独
一档而不是 stable 的一个特例，因为它的**渲染形态**不同：stable/dynamic 都渲染成
普通消息，而 system 必须是独立的一条 system 消息（见 renderer.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Representation(str, Enum):
    """一份 Artifact 在 Context 里的**详略档位**。

    四档是有序的，而且顺序就是**压缩的方向**（见 `ORDER`）：Token 不够时从 `full`
    往 `metadata` 退，而不是"删掉旧消息"。混入 `str` 是为了让它能直接进 JSON
    （会话文件里存的就是这些值）并且能被字符串比对。
    """

    METADATA = "metadata"
    PREVIEW = "preview"
    RANGE = "range"
    FULL = "full"

    @property
    def rank(self) -> int:
        """越详略档位越低（metadata 最省）。**降级 = rank 减一。**"""
        return _ORDER.index(self)

    def degraded(self) -> "Representation":
        """退一档。**已经是 metadata 时返回自己**（调用方据此判断"降不动了"）。"""
        rank = self.rank
        return self if rank == 0 else _ORDER[rank - 1]


# 详略档位从省到详。**它的顺序就是降级顺序**，所以只写一遍。
_ORDER: tuple[Representation, ...] = (
    Representation.METADATA,
    Representation.PREVIEW,
    Representation.RANGE,
    Representation.FULL,
)


def _as_representation(value: "Representation | str") -> Representation:
    """把字符串/枚举统一成枚举。认不出就抛（**不静默回落到 full**）。

    静默回落是最坏的失败形态：写错一个档位名而它照旧按 full 渲染，等于"我配的
    压缩没生效"变成一个查不出来的现象。和配置文件里写错键名同一条规矩。
    """
    if isinstance(value, Representation):
        return value
    try:
        return Representation(value)
    except ValueError:
        known = "、".join(r.value for r in _ORDER)
        raise ValueError(f"不认识的 representation：{value!r}（可选：{known}）") from None


class Zone(str, Enum):
    """一块 Context 属于哪一区。见模块 docstring 里"为什么要有 zone"。"""

    SYSTEM = "system"
    STABLE = "stable"
    DYNAMIC = "dynamic"


def _as_zone(value: "Zone | str") -> Zone:
    if isinstance(value, Zone):
        return value
    try:
        return Zone(value)
    except ValueError:
        known = "、".join(z.value for z in Zone)
        raise ValueError(f"不认识的 zone：{value!r}（可选：{known}）") from None


@dataclass(frozen=True, slots=True)
class ArtifactSource:
    """这份信息是**怎么来的**。

    它不参与渲染（模型看不到它），但事后追责要问的第一个问题就是这个 —— "这段
    内容是从哪儿冒出来的"。所以它记的是来源的形状（哪个工具、哪个路径、哪个 URL），
    而不是一句人写的描述。
    """

    tool: str = ""
    path: str = ""
    url: str = ""

    def to_json(self) -> dict[str, str]:
        """只写非空的格子。**恒为空串的键会让读日志的人以为那次真的没有路径。**"""
        data: dict[str, str] = {}
        for name in ("tool", "path", "url"):
            value = getattr(self, name)
            if value:
                data[name] = value
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "ArtifactSource":
        raw = data or {}
        return cls(
            tool=str(raw.get("tool") or ""),
            path=str(raw.get("path") or ""),
            url=str(raw.get("url") or ""),
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    """一份可以被以后引用的信息。**正文不在这里**（见模块 docstring）。

    字段的取舍：

      * `id` —— 稳定身份。规则是"同一份信息还是同一份信息，就别换 id"：随机 id
        会让渲染出来的 prompt 每轮都变，而那正好把前缀缓存打掉。
      * `type` —— 粗粒度分类（`file` / `command` / `search` / `web` / `text`）。
        渲染器按它决定怎么把 `range` 切片（文本按行、别的按字符）。
      * `metadata` —— 工具知道、而 Context 层推不出来的事实（路径、行号、状态码
        ……）。**它是 `range`/`metadata` 两档渲染的全部依据。**
      * `chars` —— 正文字符数。放在这里而不是每次去数：预算判断每一步都要用它，
        而去数一遍意味着把正文读进内存。
    """

    artifact_id: str
    type: str = "text"
    source: ArtifactSource = field(default_factory=ArtifactSource)
    content_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    chars: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.artifact_id,
            "type": self.type,
            "source": self.source.to_json(),
            "content_ref": self.content_ref,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "chars": self.chars,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Artifact":
        return cls(
            artifact_id=str(data.get("id") or ""),
            type=str(data.get("type") or "text"),
            source=ArtifactSource.from_json(data.get("source")),
            content_ref=str(data.get("content_ref") or ""),
            metadata=dict(data.get("metadata") or {}),
            created_at=float(data.get("created_at") or 0.0),
            chars=int(data.get("chars") or 0),
        )


@dataclass(slots=True)
class ContextItem:
    """「我现在想让 LLM 怎么看到这份信息」。

    它是 Artifact 和 Context 之间**唯一的桥梁**：Artifact 回答"我有什么信息"，
    而它回答"此刻这份信息以什么形态、排在哪一区、值不值得为它挤掉别人"。

    `representation="auto"` 在 `ContextState.add` 那一刻被定成 `full` —— **只定
    一次**，此后它就是一个具体档位。理由见设计原则"Representation 不要无意义
    变化"：每次调用都重算一遍的档位会让渲染出来的 prompt 每轮都不一样。
    """

    artifact_id: str
    representation: Representation = Representation.FULL
    zone: Zone = Zone.DYNAMIC
    # 值大的先降级（"更不重要"）。默认 0 = 所有东西一样重要。
    priority: int = 0
    # True = "Token 不够也不许动它"。系统提示词、用户任务、项目说明是这一档。
    pinned: bool = False
    # 进入 Context 的序号。**它只增不改**，用来做"同优先级下谁先降级"的稳定判据
    # （以及事后还原"当时是什么顺序"）。
    sequence: int = 0
    # 渲染所需的额外参数（`start_line` / `end_line` / `preview_lines`）。
    options: dict[str, Any] = field(default_factory=dict)
    # 已经被挤出 Context 了（预算降到底之后的最后一步，见 `budget.py`）。
    #
    # **条目本身留着、只是不渲染正文**，而不是从 `items` 里删掉。两个理由：
    #
    #   1. 一份已经降到底的 Artifact 在预算松下来之后**不该自己冒出来**（只降不升），
    #      所以"它曾经在这里、现在不在了"这件事必须被记住；
    #   2. 调试时"这一轮为什么没看到那份文件"要有一个能查的答案 —— 从列表里删掉
    #      之后，日志里只剩"它从来没进来过"这一种可能。
    removed: bool = False

    def to_json(self) -> dict[str, Any]:
        """写进会话文件的那一份。

        **空 options / 默认档位照样写**：读回来的时候它们有默认值，但"当时是哪一
        档"必须能被原样读出来 —— 少写等于让恢复会话的人去猜。
        """
        return {
            "artifact_id": self.artifact_id,
            "representation": self.representation.value,
            "zone": self.zone.value,
            "priority": self.priority,
            "pinned": self.pinned,
            "sequence": self.sequence,
            "options": dict(self.options),
            "removed": self.removed,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ContextItem":
        return cls(
            artifact_id=str(data.get("artifact_id") or ""),
            representation=_as_representation(data.get("representation") or "full"),
            zone=_as_zone(data.get("zone") or "dynamic"),
            priority=int(data.get("priority") or 0),
            pinned=bool(data.get("pinned")),
            sequence=int(data.get("sequence") or 0),
            options=dict(data.get("options") or {}),
            removed=bool(data.get("removed")),
        )


@dataclass(slots=True)
class ContextNote:
    """一条**每轮重新拼出来的**临时内容（不进历史、不落盘）。

    目前只有一样东西是这一档：载荷末尾那条会话状态 + 步数预算（见
    `agents/agent.py` 的 `_status_note`）。它必须进 Context 的账本，否则预算会
    把它当成"空气"—— 一条 200 token 的提醒在一次 100K 的请求里不重要，但当
    预算已经卡着上限时，它就是压垮的那一根。

    它和 `ContextItem` 分开而不是复用：那一个的 `artifact_id` 是必填的，而这一
    类内容**根本没有 Artifact**（它每一轮都不一样，存下来就是存垃圾）。硬塞一个
    假 id 会让"哪些东西在盘上有正文"这个问题的答案变成错的。
    """

    text: str = ""
    zone: Zone = Zone.DYNAMIC
    priority: int = 0
    pinned: bool = True
    sequence: int = 0

    def to_json(self) -> dict[str, Any]:
        """**只给审计和界面用，不进会话文件**（它是逐轮的，存下来没有意义）。"""
        return {
            "chars": len(self.text),
            "zone": self.zone.value,
            "priority": self.priority,
            "pinned": self.pinned,
        }


@dataclass(slots=True)
class ContextState:
    """当前这一轮 Context 的全貌。**只装指针，不装正文。**

    `version` 每变一次加一。它不是版本号洁癖 —— 它是**降级的记账**：预算那一层
    只在 version 变了之后才允许再次动档位，于是"同一批 Context 在一轮里被反复
    重算"这件事在结构上就不可能发生（那正是缓存抖动的来源）。
    """

    items: list[ContextItem] = field(default_factory=list)
    version: int = 0
    # 临时内容（见 `ContextNote`）。**刻意不进 `to_json`** —— 会话文件里存的是
    # "这一刻的 Context 状态"，而临时内容每轮都不一样，存下来只会让恢复会话的人
    # 以为那是当时真的发出去的东西。
    notes: list[ContextNote] = field(default_factory=list)

    def get(self, artifact_id: str) -> ContextItem | None:
        for item in self.items:
            if item.artifact_id == artifact_id:
                return item
        return None

    def live(self) -> list[ContextItem]:
        """真正会进渲染的那些条目（没被挤出去的）。"""
        return [i for i in self.items if not i.removed]

    def next_sequence(self) -> int:
        """下一个 sequence。**从已有的最大值推**，不另存一个计数器。

        另存计数器就有了两份事实，而恢复会话时它们会对不上（读回来的 items 有
        sequence，计数器却是新的）—— 症状是新条目插到旧条目"中间"。
        """
        return max((i.sequence for i in self.items), default=-1) + 1

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "items": [i.to_json() for i in self.items]}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "ContextState":
        raw = data or {}
        items = [
            ContextItem.from_json(entry)
            for entry in (raw.get("items") or [])
            if isinstance(entry, Mapping)
        ]
        return cls(items=items, version=int(raw.get("version") or 0))

    def next_note_sequence(self) -> int:
        """临时内容的下一个序号。和 `next_sequence` 分开数 —— 两类条目不是一套编号。"""
        return max((n.sequence for n in self.notes), default=-1) + 1


__all__ = [
    "Artifact",
    "ArtifactSource",
    "ContextItem",
    "ContextNote",
    "ContextState",
    "Representation",
    "Zone",
]
