"""Token 预算，以及超预算时的**降级**。

## 降级的顺序（设计原则第 7 条）

    full → range → preview → metadata → removed

**先降级、最后才删**。"删掉旧消息"是把一份信息变成零，而降级是把它变成"模型仍然
知道它存在、需要时能自己再读一次"—— 后者在长任务里几乎是白送的一份保险。

## 什么时候降、什么时候升

**只降不升。** 一旦某一档降下去了，此后就不会因为"这一轮 token 又够了"而升回来。
这一条是缓存稳定性（设计原则第 8 条）的直接后果：`full → range → full` 这种来回
摆动会让渲染出来的 prompt 每一轮都不同，而 provider 的前缀缓存是按**最长公共前缀**
算的 —— 前缀里第一个变化的字节之后，所有 token 都按未命中计费（官方价里贵约 50 倍）。

代价说清楚：一个长回合跑到后面，早期那些被降过档的 Artifact 不会自动恢复详略。
想拿回全文，模型可以自己再 `read_file` 一次 —— 那会产生一份**新的** Artifact，
而它是 full 档的。

## 从最旧的那一条开始降（而不是从最新的）

看起来反直觉：最新读进来的东西不是最该压的吗？

不是。降级的代价由**前缀缓存**决定：降一条排在前面（旧）的 Artifact，被打断的
前缀更短，垮掉的缓存也更少；降最后一条等于把整个前缀作废。而"谁不重要"这件事
已经由 `priority` / `pinned` 表达过了（系统提示词和用户任务是 pinned，动不了）。

（还有一条同方向的理由：最旧的工具结果往往已经被后续几步消化掉了，而最新的那条
正是模型刚刚要用的。）

## 估算为什么是估算

项目里没有 tokenizer 依赖，而 provider 实测的 `prompt_tokens` 只在**请求发出之后**
才知道（见 `composition.Runtime.context_tokens`）。所以这里的数字是估算，而它用来
做的决定是"要不要降级" —— 一个容错的方向明确的决定：

  * 估**高**了 ⇒ 提前降级，模型看到的东西比它本可以看的少一点（可用，只是保守）；
  * 估**低**了 ⇒ 请求超窗，provider 报 400，这一轮整个失败（不可用）。

所以估算的默认值刻意偏保守，而且 `calibrate()` 允许用实测值把偏差修掉 —— 修过
之后它就不再是"猜"，而是"上一次实测的比例"。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent_runtime.context.models import ContextItem, ContextState, Representation

# 一份正文按字符折算成 token 的比例。**按 ASCII 和"其它"分开算。**
#
# 0.25 ≈ 每 token 4 个 ASCII 字符（英文代码的常见比例），而 CJK 一个字符通常就
# 是 1~2 个 token。项目整个是中文的，混着来的时候按这两档加权远好过一刀切。
ASCII_TOKENS_PER_CHAR = 0.25
WIDE_TOKENS_PER_CHAR = 1.0

# 一条消息本身的固定开销（role、分隔符、特殊 token）。实测 OpenAI 兼容端点上
# 每条消息 3~4 个 token，取 4 是保守的那一侧。
MESSAGE_OVERHEAD = 4

# 一份 Artifact 渲染出来时那几行表头（路径、行号、"以下是…"）的开销。
SNIPPET_OVERHEAD = 12

# 给模型的回答留多少。**它必须留**：不留的话预算会刚好卡在窗口上，而模型的
# 回答没地方放 —— provider 会报 400，而那个错误看起来像"上下文太长了"。
DEFAULT_RESERVE = 4096

# 留出的余量比例。估算器一定有偏差，而"刚好塞满"是最坏的用法：下一次请求只要
# 多一点（比如工具结果的表头）就越界。10% 是一份便宜的保险。
DEFAULT_HEADROOM = 0.1

# `estimate_tokens` 的签名。换一个真的 tokenizer 只要换这一处。
Estimator = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。**保守**（见模块 docstring）。"""
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    wide_count = len(text) - ascii_count
    return int(ascii_count * ASCII_TOKENS_PER_CHAR + wide_count * WIDE_TOKENS_PER_CHAR)


@dataclass(slots=True)
class Degraded:
    """一次降级里发生了什么。**给审计和界面用。**

    它只记"从哪一档变到哪一档"，不记正文 —— 一份被降级的 Artifact 的正文还在
    盘上，审计没有理由再抄一遍（那是 session 文件该干的事）。
    """

    artifact_id: str
    before: Representation
    after: Representation | None    # None = 从 Context 里摘掉了


class ContextBudget:
    """把 Context 塞进一个 token 上限里。

    它是**纯的**：给定 items 和取正文的方式，算出该降谁、降多少，然后改写 items。
    不读盘、不认识 Session、也不发事件 —— 那些由 `ContextManager` 接起来。
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        *,
        reserve: int = DEFAULT_RESERVE,
        headroom: float = DEFAULT_HEADROOM,
        estimator: Estimator = estimate_tokens,
        preview_lines: int = 40,
        range_lines: int = 400,
    ) -> None:
        # `max_tokens=None` = **这个模型不知道自己的窗口**（配置里没写 context_window）。
        # 那时预算整体关掉：一个假的上限比没有上限更坏 —— 它会去降级一个本来
        # 塞得下的 Context，而"为什么模型看不到全文"就变成一个查不出来的现象。
        self.max_tokens = max_tokens
        self.reserve = reserve
        self.headroom = headroom
        self.estimator = estimator
        self.preview_lines = preview_lines
        self.range_lines = range_lines
        # 实测/估算的比例。1.0 = 相信估算器。`calibrate` 会改它。
        self._factor = 1.0
        self._sample: tuple[int, int] | None = None

    @property
    def enabled(self) -> bool:
        """预算关着的时候，`fit` 什么都不做（只报数）。"""
        return self.max_tokens is not None and self.max_tokens > 0

    @property
    def effective_limit(self) -> int:
        """真正能用来放 Context 的 token 数：窗口 - 回答预留 - 余量。"""
        if not self.enabled:
            return 0
        assert self.max_tokens is not None
        usable = self.max_tokens - self.reserve
        return max(0, int(usable * (1.0 - self.headroom)))

    # -- 估算 ------------------------------------------------------------------

    def tokens(self, text: str) -> int:
        return int(self.estimator(text) * self._factor)

    def estimate_items(
        self,
        items: Iterable[ContextItem],
        render: Callable[[ContextItem], str | None],
    ) -> int:
        """这一批 items 大概占多少 token。

        `render` 是"把一条 item 按它当前档位渲染成文本"的注入点 —— 预算这一层
        不该知道怎么渲染（那是 renderer 的知识），它只需要一个**能拿到当下文本**
        的口子。返回 None 表示这条取不到（正文丢了），按 0 算。
        """
        total = 0
        for item in items:
            if item.removed:
                continue
            text = render(item)
            total += MESSAGE_OVERHEAD + SNIPPET_OVERHEAD
            if text:
                total += self.tokens(text)
        return total

    def calibrate(self, estimated: int, measured: int) -> float:
        """用 provider 实测的 `prompt_tokens` 修正估算比例。

        **只在两者都不小的时候修**：一次 200 token 的请求里，误差的绝对值很小而
        比例可以很离谱（估 180 实测 220 ⇒ 比例 1.22），拿它去修正会让后面几百 K
        的请求被这条噪音带偏。所以样本太小就不动。

        比例做上下限夹平：估算是为了做决定，而一个 5 倍的比例会把预算变成一个
        恒真/恒假的开关。
        """
        if estimated < 1000 or measured < 1000:
            return self._factor
        self._sample = (estimated, measured)
        ratio = measured / estimated
        self._factor = min(3.0, max(0.33, ratio))
        return self._factor

    @property
    def sample(self) -> tuple[int, int] | None:
        """最后一次校准用的 (估算, 实测)。给 `/status` 和排障用。"""
        return self._sample

    # -- 降级 ------------------------------------------------------------------

    def fit(
        self,
        state: ContextState,
        render: Callable[[ContextItem], str | None],
        *,
        degrade: Callable[[ContextItem], None] | None = None,
        remove: Callable[[ContextItem], None] | None = None,
        extra: int = 0,
        single_step: bool = False,
    ) -> list[Degraded]:
        """把 state 里的 items 降到塞得下为止。返回**这一步做了什么**。

        参数的分工：

          * `render` —— 拿当下档位的文本（估算要它）；
          * `degrade(item)` —— 把一条 item 降一档（**由调用方改写模型层**，
            因为"降级之后 options 里的行号要怎么变"是渲染的知识）；
          * `remove(item)` —— 从 Context 里摘掉（同样交给调用方，它要把
            `removed` 立起来 —— 见 `ContextItem`）；
          * `extra` —— **动不了的那部分**的 token 数（载荷末尾的临时内容）。
            它不参与降级，但必须从预算里先扣掉 —— 不扣的话"塞得下"这个判断是假的；
          * `single_step` —— 一次只降**一档**就返回（默认关，`ContextManager`
            开着它，见下面那段）。

        ## 为什么默认要一次降到底，而调用方反而要一次一档

        这个函数本身是"把这件事做完"的语义：算出一个塞得下的配置，或者如实说
        做不到。所以默认它一轮一轮降到底。

        `ContextManager` 传 `single_step=True`，理由是**载荷形状的稳定性**（设计
        原则第 8 条）：一步降三档和分三步降三档，最终档位一样，但中间那两次的
        载荷完全不同 —— 而那两次的 token 已经花出去了。一次只降一档，下一步就
        能带着**新的实测估算**再判断一次"还要不要再降"，于是"刚好够"变成可达的
        状态，而不是"一次降到底、此后都看不见全文"。

        没有 `degrade` 时只做"删"这一步；没有 `remove` 时降不动了就停手并
        如实返回（**不静默假装成功**）。
        """
        if not self.enabled:
            return []

        limit = self.effective_limit
        done: list[Degraded] = []

        # 循环有上界：每一步要么降一档、要么摘掉一条，而档位只有四档、条目有限。
        # 留一个宽裕的界是为了让"预算算错了"变成一次如实的返回，而不是死循环。
        for _ in range(4 * len(state.items) + 8):
            if extra + self.estimate_items(state.items, render) <= limit:
                return done

            item = self._next(state.items)
            if item is None:
                # 全都动不了了（pinned，或者都已经到底了）。
                return done

            before = item.representation
            if before is not Representation.METADATA and degrade is not None:
                degrade(item)
                done.append(Degraded(item.artifact_id, before, item.representation))
                if single_step:
                    return done
                continue

            if remove is not None:
                remove(item)
                done.append(Degraded(item.artifact_id, before, None))
                return done

            # 到这儿说明既降不动、也不许删（或者调用方没给 remove）：**停手**。
            # 继续转下去只会重复选中同一条 —— 而死循环在预算这一层是最坏的形态
            # （它发生在每一轮请求的路径上）。如实返回，让调用方去报告超预算。
            return done

        return done

    def _next(self, items: list[ContextItem]) -> ContextItem | None:
        """下一条该被动刀的是谁。**排序规则就是优先级本身。**

        `pinned` 一律跳过（那是这条字段的全部含义）。剩下的按：

          1. `priority` 小的先降（数值大 = 更重要）；
          2. 同 priority 时 `dynamic` 先于 `stable`（动态区本来就是"允许频繁变化"
             的那一区）；
          3. 还相同就 `sequence` 小的先降（旧的先降，理由见模块 docstring）。

        最后比 `artifact_id` 只是为了让顺序**完全确定**：同样输入两次跑出来必须
        动同一条，否则同一个会话两次恢复会话的 Context 会长得不一样。
        """
        candidates = [i for i in items if not i.pinned and not i.removed]
        if not candidates:
            return None
        return min(candidates, key=_degrade_key)


def _degrade_key(item: ContextItem) -> tuple[int, int, int, str]:
    zone_rank = 0 if item.zone.value == "dynamic" else 1
    return (item.priority, zone_rank, item.sequence, item.artifact_id)


__all__ = [
    "ASCII_TOKENS_PER_CHAR",
    "DEFAULT_HEADROOM",
    "DEFAULT_RESERVE",
    "MESSAGE_OVERHEAD",
    "SNIPPET_OVERHEAD",
    "WIDE_TOKENS_PER_CHAR",
    "ContextBudget",
    "Degraded",
    "Estimator",
    "estimate_tokens",
]
