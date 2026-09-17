"""ContextManager：**决定当前这一轮 LLM 应该看到什么。**

它不存数据（那是 `ArtifactStore` 的事），也不渲染（那是 `ContextRenderer` 的事）。
它管的是状态：有哪些 Artifact 现在"在 Context 里"、各以什么档位、谁不许动，以及
超预算时该动谁。

## 它是 Artifact 和 Context 之间那道门

    ArtifactStore   我有什么信息？        （不知道 Context 存在）
    ContextManager  我现在让模型看见什么？ （不知道正文长什么样）

后者只装指针（`ContextItem.artifact_id`）。所以 `Artifact` 可以存在而**不**进
Context —— 那是常态：一次 `grep` 产生了命中文件清单，但只有其中一份进了 Context。

## 兼容：老会话里那些"正文还在历史里"的消息

重构之前的会话文件里，tool 消息的内容就是全文，没有 Artifact 也没有 ContextItem。
`hydrate()` 会给它们补一条 `full` 档的条目并把正文收进 ArtifactStore —— 于是
"老会话照常能接着聊"这件事不需要读取端写第二套渲染路径（见它的 docstring）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from agent_runtime.context import ref
from agent_runtime.context.artifact_store import ArtifactStore
from agent_runtime.context.budget import (
    MESSAGE_OVERHEAD,
    ContextBudget,
    Degraded,
)
from agent_runtime.context.models import (
    Artifact,
    ArtifactSource,
    ContextItem,
    ContextNote,
    ContextState,
    Representation,
    Zone,
)

# items 变了之后要发生什么。**只有一个参数：整份新的 ContextState。**
# 传整份而不是"变了哪一条"：调用方（落盘、界面）要的永远是"现在是什么样"，
# 而增量形式会让它自己再维护一份合并逻辑 —— 那就是同一份事实的第二个来源。
OnChange = Callable[[ContextState], None]

# 文件类的 Artifact 降到 `range` 时最多给多少行。**它是上限，不是目标** ——
# 目标由正文自己的大小反推（见 `ContextManager._degrade`）。
DEFAULT_RANGE_LINES = 400

# 降到 `preview` 时最多给多少行。同上。
DEFAULT_PREVIEW_LINES = 40

# 降级的目标比例：`range` 大约留两成 token，`preview` 大约留半成。
#
# 为什么是"比例"而不是"固定行数"：降级的目的是**把 token 降下来**，而同一个行数
# 在 100 行的文件和 2 万行的日志上完全不是一回事。第一版用固定行数，实测在 200
# 行的文件上那一步反而是负优化（表头比省下来的正文还多）。
DEFAULT_RANGE_RATIO = 0.2
DEFAULT_PREVIEW_RATIO = 0.05

# 降级窗口的下限（行）。**再小就没有意义了**：给 3 行等于把这份信息变成噪音，
# 而那时的正确动作是继续往下一档降（`metadata` / 挤出去），不是给一份读不出东西
# 的残片。它也让 `_degrade` 的窗口永远 ≥ 1 —— 空区间在渲染上和"这份文件没内容"
# 长得一样。
MIN_WINDOW_LINES = 20

# 同上，字符那一侧的下限。
MIN_WINDOW_CHARS = 400

# 字符预算的**上限**（一行特别长的正文才会碰到它）。它和 `DEFAULT_RANGE_LINES`
# 是一对：行数和字符数各有一道自己的闸，因为它们对不同的正文形状失效。
DEFAULT_RANGE_CHARS = 24_000
DEFAULT_PREVIEW_CHARS = 3_000

# 渲染时表头会占掉的那几十个字符（`[artifact art_x：path，第 1-20 行（共 900 行）]`）。
#
# **它必须从字符预算里先扣掉**，否则"降级到 16000 字符"实际发出去的是 16057 个 ——
# 预算算 4000 token、真实载荷 4000 多一点，而"估算偏小"是危险的那一侧（请求会
# 直接超窗）。表头的长度随路径和行号变，所以这里给的是一份**保守的上界**：
# 它只影响降级的力度，多扣 80 个字符的代价可以忽略。
RENDER_HEADER_RESERVE = 80


class ContextManager:
    """一个会话的 Context 状态。"""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        state: ContextState | None = None,
        budget: ContextBudget | None = None,
        on_change: OnChange | None = None,
        range_lines: int = DEFAULT_RANGE_LINES,
        preview_lines: int = DEFAULT_PREVIEW_LINES,
        range_ratio: float = DEFAULT_RANGE_RATIO,
        preview_ratio: float = DEFAULT_PREVIEW_RATIO,
        range_chars: int = DEFAULT_RANGE_CHARS,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> None:
        self.store = store
        self.state = state or ContextState()
        self.budget = budget or ContextBudget()
        self.on_change = on_change
        self.range_lines = range_lines
        self.preview_lines = preview_lines
        self.range_ratio = range_ratio
        self.preview_ratio = preview_ratio
        self.range_chars = range_chars
        self.preview_chars = preview_chars
        # 上一步估算出来的 token 数。**它是估算，不是实测** —— 后者只有 provider
        # 知道。两者的关系见 `budget.calibrate`。
        self.last_estimate = 0
        # 上一步降过级的那些。给审计和界面读（"这一轮为什么没看到全文"）。
        self.last_degraded: list[Degraded] = []

    # -- 写 --------------------------------------------------------------------

    def add(
        self,
        artifact_id: str,
        representation: Representation | str = Representation.FULL,
        *,
        zone: Zone | str = Zone.DYNAMIC,
        priority: int = 0,
        pinned: bool = False,
        options: Mapping[str, Any] | None = None,
        notify: bool = True,
    ) -> ContextItem:
        """让一份 Artifact 进 Context（已经在里面就更新它的档位/区/优先级）。

        **幂等**：同一个 id 调两次不会出现两条。这一点是必须的 —— 同一份 Artifact
        可能被两条路径同时加进来（工具结果那条、以及 `read_file` 之后 `hydrate`
        那条），而两条重复条目会让它渲染两遍（白花钱），或者更坏：两条档位不同，
        于是"到底按哪个渲染"没有答案。
        """
        existing = self.state.get(artifact_id)
        if existing is not None:
            existing.representation = _representation(representation)
            existing.zone = _zone(zone)
            existing.priority = int(priority)
            existing.pinned = bool(pinned)
            existing.options = dict(options or {})
            existing.removed = False
            self._touch(notify)
            return existing

        item = ContextItem(
            artifact_id=artifact_id,
            representation=_representation(representation),
            zone=_zone(zone),
            priority=int(priority),
            pinned=bool(pinned),
            sequence=self.state.next_sequence(),
            options=dict(options or {}),
        )
        self.state.items.append(item)
        self._touch(notify)
        return item

    def add_artifact(
        self,
        artifact: Artifact,
        *,
        zone: Zone | str = Zone.DYNAMIC,
        priority: int = 0,
        pinned: bool = False,
        representation: Representation | str = Representation.FULL,
        options: Mapping[str, Any] | None = None,
        notify: bool = True,
    ) -> ContextItem:
        """`add` 的糖：直接给 Artifact 对象。"""
        return self.add(
            artifact.artifact_id, representation, zone=zone, priority=priority,
            pinned=pinned, options=options, notify=notify,
        )

    def remove(self, artifact_id: str) -> bool:
        """把一份 Artifact 从 Context 里摘掉（**不删数据**）。返回"原本在不在"。

        「从 Context 里摘掉」和「删掉 Artifact」是两件事，这里做的是前者：
        `removed=True`，正文留在盘上。设计原则第 2 条说得很清楚 —— **Artifact 可以
        存在，但不一定进入 Context**。真要删数据是 `ArtifactStore.delete`（预算
        降到底时才会走到）。
        """
        item = self.state.get(artifact_id)
        if item is None or item.removed:
            return False
        item.removed = True
        self._touch(True)
        return True

    def restore(self, artifact_id: str) -> bool:
        """把一条被挤出去的条目放回来（**保持它原来的档位**）。"""
        item = self.state.get(artifact_id)
        if item is None or not item.removed:
            return False
        item.removed = False
        self._touch(True)
        return True

    def clear(self) -> None:
        """清空 Context（**不动 ArtifactStore**）。"""
        if not self.state.items:
            return
        self.state.items = []
        self._touch(True)

    def set_notes(self, texts: Iterable[str]) -> list[ContextNote]:
        """把这一轮的临时内容（载荷末尾那条会话状态）换掉。

        **它不是一条 ContextItem**：没有 Artifact、不落盘、每轮重算（见
        `ContextNote` 的 docstring）。但它必须进账本，否则预算算出来的数是"少了
        一条消息"的数 —— 而那条消息在预算卡着上限时正好是压垮它的那一根。

        调用点在同一轮里会走很多次（每一步一次），所以它**不碰版本号、也不通知**
        任何回调：版本号变了会触发落盘，而"步数提示从 3 变成 2"没有任何落盘的
        价值。
        """
        self.state.notes = [
            ContextNote(text=text, sequence=index)
            for index, text in enumerate(texts) if text
        ]
        return list(self.state.notes)

    def notes(self) -> list[ContextNote]:
        return list(self.state.notes)

    # -- 读 --------------------------------------------------------------------

    def items(self) -> list[ContextItem]:
        """全部条目（含被挤出去的），按进入顺序。"""
        return list(self.state.items)

    def item(self, artifact_id: str) -> ContextItem | None:
        return self.state.get(artifact_id)

    def representation_of(self, artifact_id: str) -> Representation | None:
        """这份 Artifact 此刻是什么档位。`None` = 它不在 Context 里。"""
        item = self.state.get(artifact_id)
        return None if item is None or item.removed else item.representation

    def zone_items(self, zone: Zone | str) -> list[ContextItem]:
        target = _zone(zone)
        return [i for i in self.state.items if i.zone is target and not i.removed]

    def stats(self) -> dict[str, Any]:
        """给界面用的一份摘要（TUI 左栏那块、以及 `/status`）。

        **它只报数，不报正文** —— 这个函数的调用点在每一次状态快照上。

        `open` / `compact` 这一对是**这个功能最要紧的两个数**：`open` 是"手里一共
        有多少份信息"（进过 Context 的全部，含被挤出去的），`compact` 是"此刻真的
        发得出去几份"。两个数差得越大，说明预算压得越紧 —— 而过去这两个数是一个
        （工具结果直接进历史，有多少就发多少）。
        """
        live = self.state.live()
        return {
            # 盘上有多少份 Artifact（进过 Context 的 + 从没进过的）
            "artifacts": len(self.store),
            # 此刻在 Context 里、真的会被渲染出去的
            "items": len(live),
            "compact": len(live),
            # 进过 Context 但被预算挤出去的
            "removed": len(self.state.items) - len(live),
            "open": len(self.state.items),
            "pinned": sum(1 for i in live if i.pinned),
            "stable": sum(1 for i in live if i.zone is Zone.STABLE),
            "dynamic": sum(1 for i in live if i.zone is Zone.DYNAMIC),
            "version": self.state.version,
            "estimated_tokens": self.last_estimate,
            "limit_tokens": self.budget.effective_limit,
            "degraded": len(self.last_degraded),
            # 历史压缩那条线（`compaction.py`）。**只报阈值，不报"压过没有"** ——
            # 那是会话的事（`session.metadata`），Context 这一层不知道历史长什么样，
            # 见模块 docstring 那条分界。
            "compact_threshold": self.budget.compact_threshold,
        }

    def should_compact(self) -> bool:
        """到历史压缩那条线了吗。**它只回答"该不该考虑"，不回答"能不能压"。**

        后者是 `compaction.fold_point` 的事（要拿到整份历史才算得出来），而这一层
        刻意不认识历史 —— 判据分两处是模块 docstring 那条"管状态、不管渲染"的同一条
        分界：Context 知道"现在多大"，Agent 知道"历史长什么样"。

        用的是上一次算出来的估算（`last_estimate`，已被 provider 的实测值校准过）。
        它和"此刻真实大小"的差别就是这一步里新进来的工具结果 —— 而那正是调用方在
        每一步之前都会重新估一次的原因。预算关着时一律 False：窗口未知的情况下，
        "到点了"这句话没有意义（见 `ContextBudget.enabled`）。
        """
        if not self.budget.enabled:
            return False
        return self.last_estimate >= self.budget.compact_threshold

    # -- 预算 ------------------------------------------------------------------

    def fit(
        self,
        render: Callable[[ContextItem], str | None],
        *,
        extra: int = 0,
    ) -> list[Degraded]:
        """把 Context 降到预算之内，返回这一步降了什么。

        `render` 是"按当前档位渲染成文本"的口子（由 renderer 给，见 `budget.py`）。
        降级的**改写**在这里做，因为"降一档之后 options 该怎么变"是 Context 状态
        的知识（`_degrade`）。

        `extra` 是**载荷里降不动的那些内容**的 token 数（调用方算好）：系统提示词、
        用户/助手消息、以及每条 tool 消息那行引用。它们不在 `state.items` 里（那些
        是 Artifact），却和 Artifact 挤同一个窗口 —— **不扣掉就等于在算一本缺了
        一半的账**：历史越长，算出来的"还塞得下"越假，最后把 Artifact 全降到 0 也
        还是超窗，而 provider 只会回一个看起来像"上下文太长"的 400。

        默认 0 = "这次没有固定开销"（测试、以及 `scripts/walkthrough_context.py`
        那种只演 Context 本身的场合）。**它不是"不必算"，只是调用方说没有。**
        本模块不自己去翻 messages：那形状是 renderer / agent 的知识，见模块
        docstring 那条"管状态、不管渲染"的分界。

        **每一步都调它，但只有超预算时才会真的变。** 已经降下去的不回升
        （见 `budget.py` 的模块 docstring）。
        """
        budget = self.budget
        if not budget.enabled:
            # 窗口未知：只报数。
            self.last_estimate = self.estimate(render, extra=extra)
            self.last_degraded = []
            return []

        before = _fingerprint(self.state)
        self.last_degraded = budget.fit(
            self.state,
            render,
            degrade=self._degrade,
            remove=self._evict,
            extra=extra + self._notes_tokens(),
            # **一次只降一档**：见 `budget.fit` 的 `single_step` 那段。降到刚好
            # 够就停 —— 多降的那几档是白丢的信息。
            single_step=True,
        )
        after = _fingerprint(self.state)
        self.last_estimate = self.estimate(render, extra=extra)
        if before != after:
            # 档位变了 ⇒ 渲染出来的 prompt 变了 ⇒ 这是一个新版本。**版本号变了的
            # 后果之一是它会被重新落盘**（见 state/store.py 的 context 记录）。
            self.state.version += 1
            self._notify()
        return self.last_degraded

    def estimate(self, render: Callable[[ContextItem], str | None],
                 *, extra: int = 0) -> int:
        """当前这一轮请求大概占多少 token（含临时内容和固定开销）。

        `extra` 的口径和 `fit` 的那个完全一样（调用方算好的"降不动的那部分"），
        传同一个值给两边才成立 —— `fit` 拿它做决定、这里拿它报数，**两处差一个数
        就会让"按估算报出来的余量"和"按估算做的降级"对不上**，而那种症状是
        "明明还有余量却在降级"（或者反过来）。

        它也喂 `calibrate`（`last_estimate` 是那里那个 estimated）。所以少了 `extra`
        不只是报数偏小：**provider 实测的 `prompt_tokens` 是含固定开销的**，比例
        会被算高，于是此后每一次降级都偏狠。
        """
        total = extra + self.budget.estimate_items(self.state.live(), render)
        for note in self.state.notes:
            total += MESSAGE_OVERHEAD + self.budget.tokens(note.text)
        return total

    def _notes_tokens(self) -> int:
        return sum(
            MESSAGE_OVERHEAD + self.budget.tokens(note.text) for note in self.state.notes
        )

    def calibrate(self, measured_prompt_tokens: int) -> float:
        """用 provider 实测的输入 token 数修正估算。返回新的比例。

        `measured_prompt_tokens` 是**上一次请求**的（provider 只有发完才知道），
        所以它修正的是"下一次估算"。它比估算准得多 —— 但用它直接当"当前 Context
        有多大"是错的：两次请求之间 Context 变了（新工具结果进来了）。这里只用
        它修比例，不动 estimate 本身。
        """
        return self.budget.calibrate(max(1, self.last_estimate), measured_prompt_tokens)

    def _degrade(self, item: ContextItem) -> None:
        """把一条条目降一档，并**把 options 一起调对**。

        options 不是可选的细节：一条 `range` 档的条目如果还带着"全文"的行号范围，
        渲染出来就是全文 —— 降级做了一个空动作，而"降了但没变"在 token 账单上
        看不出来（它只是没降下来），在日志里也看不出来（档位确实变了）。

        ## 窗口大小是**算出来的**，不是一个固定常数

        第一版把 `range` 定成"最多 400 行"、`preview` 定成 40 行。那在"200 行的
        文件"上等于什么都没做 —— 实测过：一次降级让估算从 2016 **涨到** 2034
        （多出来的表头），而降级循环于是一直选中同一条、一直降不动。

        真正想要的语义是**"要降到原来的几成"**，所以窗口由正文自己的大小反推
        （见 `_window_lines`）：降级对一份 20 万字符的日志要给出几百行，对一份
        100 行的文件只该给出几十行。`range_lines` / `preview_lines` 那两个配置
        此时退化成**上限**（一份巨大的正文也不该一次给出上千行）。
        """
        target = item.representation.degraded()
        if target is item.representation:
            return
        item.representation = target

        if target is Representation.PREVIEW:
            item.options = dict(_window(
                self.store, item.artifact_id, self.preview_ratio,
                self.preview_lines, self.preview_chars,
            ))
            return

        if target is Representation.METADATA:
            # metadata：没有可选的参数（它渲染的是 Artifact 自己的元数据）。
            item.options = {}
            return

        # RANGE：保留模型/用户原来要看的那个位置，只把窗口收窄 —— 降级不该把模型
        # 正在看的地方换掉（那会让它读到完全无关的几百行）。
        start = _int_option(item.options, "start_line", 1)
        total = _total_lines(self.store, item.artifact_id)
        window = _window(self.store, item.artifact_id, self.range_ratio,
                         self.range_lines, self.range_chars)
        lines = window["preview_lines"]
        end = start + max(1, lines) - 1
        if total:
            # 起点本身已经靠尾了（比如用户要的是最后一页）就往前挪，而不是给一个
            # 空区间 —— 空区间和"这份文件没内容"在渲染上长得一样。
            if start + lines - 1 > total:
                start = max(1, total - lines + 1)
                end = min(total, start + lines - 1)
            else:
                end = min(total, end)
        item.options = {"start_line": start, "end_line": end, **window}

    def _evict(self, item: ContextItem) -> None:
        """降到底之后从 Context 里摘掉。**不删 Artifact。**"""
        item.removed = True

    # -- 加载 / 兼容 ------------------------------------------------------------

    def hydrate(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        default_zone: Zone | str = Zone.DYNAMIC,
        default_priority: int = 0,
    ) -> list[str]:
        """给一批**还没有 Artifact 的历史消息**补上 Artifact 和 ContextItem。

        它是"从重构之前的会话文件恢复"那条路：那时候 tool 消息里存的是全文，
        Context 是隐式的（"全都发出去"）。做两件事：

          * 把正文收进 ArtifactStore（于是它此后可以按档位渲染、可以降级）；
          * 建一条 `full` 档的条目 —— **这就是当时的行为**（全文都发）。

        `messages` 里已经有 `artifact_id` 的那些直接跳过（它们本来就是新的）。
        返回新建的 artifact_id 列表，调用方据此决定要不要落盘。

        **每一条 tool 消息都必须有 Artifact 兜着**：历史里那句引用是渲染的唯一
        入口，缺一份就等于那一轮之后模型再也看不到那次工具结果。所以这里连
        "内容为空"的消息也收（一份空 Artifact 是诚实的：那次工具确实没输出）。
        """
        created: list[str] = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            if message.get("role") != "tool":
                continue
            if message.get("artifact_id"):
                continue          # 已经是新的形状了
            # 老会话里 tool 消息的 content 就是正文。`ref.parse` 认出来的引用
            # （比如手工改过的会话）也走"当成正文收进来"这条路：那一段文本就是
            # 我们手里唯一的事实。
            text = message.get("content")
            text = text if isinstance(text, str) else ""
            artifact = self.store.create(
                text,
                type="text",
                source=ArtifactSource(tool="legacy"),
                metadata={"status": "ok", "hydrated": True, "message_index": index},
            )
            self.add(
                artifact.artifact_id,
                zone=default_zone,
                priority=default_priority,
                notify=False,
            )
            created.append(artifact.artifact_id)
        if created:
            self._touch(True)
        return created

    # -- 记账 ------------------------------------------------------------------

    def _touch(self, notify: bool) -> None:
        self.state.version += 1
        if notify:
            self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self.state)


def _representation(value: Representation | str) -> Representation:
    from agent_runtime.context.models import _as_representation   # 单处定义在 models
    return _as_representation(value)


def _zone(value: Zone | str) -> Zone:
    from agent_runtime.context.models import _as_zone
    return _as_zone(value)


def _int_option(options: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(options.get(key, default))
    except (TypeError, ValueError):
        return default


def _window(
    store: ArtifactStore,
    artifact_id: str,
    ratio: float,
    line_cap: int,
    char_cap: int,
) -> dict[str, int]:
    """降级到某一档时给多少行、多少字符 —— **按正文自己的大小反推**。

    两个上限一起给，因为**单个都不够**：

      * 行数对"一行特别长的正文"完全无效（压缩过的 JSON、一整份拼出来的日志、
        任何没有换行的输出）—— 那种正文 `split("\\n")` 只有一行，"给 20 行"
        就是给全文；
      * 字符数对"很多很短的行的正文"偏紧（一份 5000 行的 CSV 每行 8 个字符，
        按字符算会把行数砍到只剩几行，而模型需要的是行的连续性）。

    比例是"要降到原来的几成"，而它们是**同一个语义的两种度量** —— 所以两个都算、
    两个都夹。代价说清楚：一份 5000 行 × 8 字符的 CSV 会被字符那一道再砍一次，
    即实际给到的比"两成行数"还少。那是保守的那一侧（降级本来就是要把 token 降
    下来），而真正需要精确时模型可以自己再 `read_file` 一次。

    正文大小认不出来时（`lines` / `chars` 都是 0）给上限：那时我们不知道它多大，
    宁可多给也不要让"降级"变成"清空"。
    """
    artifact = store.get(artifact_id)
    metadata = artifact.metadata if artifact is not None else {}
    chars = artifact.chars if artifact is not None else 0
    total = _as_int(metadata.get("lines"))

    lines = line_cap if not total else max(
        MIN_WINDOW_LINES, min(line_cap, int(total * ratio))
    )
    if not chars:
        max_chars = char_cap
    else:
        max_chars = max(MIN_WINDOW_CHARS, min(char_cap, int(chars * ratio)))
    return {
        "preview_lines": lines,
        "max_chars": max(1, max_chars - RENDER_HEADER_RESERVE),
    }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _total_lines(store: ArtifactStore, artifact_id: str) -> int:
    """这份 Artifact 一共多少行。**不知道就是 0**（那时窗口取配置的上限）。"""
    artifact = store.get(artifact_id)
    if artifact is None:
        return 0
    return _as_int(artifact.metadata.get("lines"))


def _fingerprint(state: ContextState) -> tuple:
    """状态里"会影响渲染"的那一部分。**只用来判断"变了没有"。**

    它不含 `version` 自己（那正是要被判断的东西），也不含"什么时候加的"这种
    不影响渲染的字段。
    """
    return tuple(
        (i.artifact_id, i.representation.value, i.removed, i.zone.value,
         tuple(sorted((k, str(v)) for k, v in i.options.items())))
        for i in state.items
    )


__all__ = [
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_PREVIEW_LINES",
    "DEFAULT_PREVIEW_RATIO",
    "DEFAULT_RANGE_CHARS",
    "DEFAULT_RANGE_LINES",
    "DEFAULT_RANGE_RATIO",
    "MIN_WINDOW_CHARS",
    "MIN_WINDOW_LINES",
    "ContextManager",
    "OnChange",
]
