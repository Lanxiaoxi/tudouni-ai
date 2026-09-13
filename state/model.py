"""这个会话用哪个模型、想得多用力：**会话级的那一份选择**。

## 三样东西，为什么住在一起

  1. **选了什么**（哪条路由 + 哪个模型 + 思考开关 + 强度）—— 它跟着
     `session.metadata` 落盘，所以恢复会话时还是它；
  2. **实际用过的是什么** —— 回答"上面那些轮次是谁生成的"，以及"要不要留一句
     '模型换了'"；
  3. **换的时候写进对话的那句话** —— 它说的是"上一条 route"和"下一条 route"的差，
     而那两样只有这里知道。

拆开的话，调用方就得自己去凑那两个名字，而它凑错的方式（比如拿"当前模型"当"上一个"）
在界面上看起来完全正常。

## 目录不在这里

"这台机器上有哪些模型、各自在哪条路由上"是 `state/catalog.py` 的事 —— 它来自配置文件
（`models.local.json`），而这里是**会话里的选择**。两者分开的收益是具体的：换一份配置
不需要动会话文件，而恢复一个旧会话时那个选择仍然按它自己的意思生效（哪怕配置里那条
路由已经改了地址）。

## 已知边界（说在明处）

选择里存的是**名字**，不是配置对象。所以"配置里那条路由改了 base_url、而我恢复的是
一个选过它下面模型的会话"会得到一个"名字还在、路由是新的"的状态 —— 这是刻意的：
路由地址是本机事实，它变了就该用新的（改地址是运维动作，不是换模型）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent_runtime.state import reasoning

# 会话里那几个键。**不用 `asdict` 存**：这一块要能被子类化/被后来的版本加字段而
# 不让旧会话打不开（见 `from_block`），所以形状是显式写下来的。
SELECTION_KEY = "model_selection"

# "上一个回合实际用的是谁"。它**不属于选择**（选择是意图，它是事实），所以单独一个
# 键：混进 `SELECTION_KEY` 那块里之后，"换了又换回来"和"从来没换过"就分不出来了。
LAST_USED_KEY = "model_last_used"

CATALOG_VERSION = 2


@dataclass(frozen=True, slots=True)
class Selection:
    """这个会话现在的**意图**：想用哪条路由上的哪个模型、想得多用力。

    `since` 是"这一轮是在什么时候选的"（epoch 秒），给 `--audit` / `/status` 用：
    同一个会话里换过模型之后，"这条回答是哪一段的"光看历史读不出来。

    `provider` 为空串表示"按目录里的默认路由"（老会话文件里没有这个字段）。
    """

    provider: str = ""
    model: str = ""
    thinking: bool = reasoning.DEFAULT_THINKING
    effort: str = reasoning.DEFAULT_EFFORT
    since: float = 0.0


def load(metadata: dict) -> Selection | None:
    """`session.metadata` → 这个会话的选择。**读不出来就是 None，绝不抛。**

    旧会话文件里没有这个键（这个功能之前建的），而一个坏掉的键不该让整个会话打不开
    —— 和 `agents_md.from_block` 立的是同一条规矩。
    """
    block = metadata.get(SELECTION_KEY) if isinstance(metadata, dict) else None
    return from_block(block)


def store(metadata: dict, selection: Selection, *, now: float | None = None) -> Selection:
    """把选择写进 `session.metadata`（**不落盘** —— 落盘是 checkpoint 的事）。"""
    stamped = Selection(
        provider=selection.provider,
        model=selection.model,
        thinking=selection.thinking,
        effort=selection.effort,
        since=now if now is not None else time.time(),
    )
    metadata[SELECTION_KEY] = to_block(stamped)
    return stamped


def to_block(selection: Selection) -> dict:
    """选择 → 能进 `session.metadata` 的那种平常数据。"""
    return {
        "version": CATALOG_VERSION,
        "provider": selection.provider,
        "model": selection.model,
        "thinking": selection.thinking,
        "effort": selection.effort,
        "since": selection.since,
    }


def from_block(block: object) -> Selection | None:
    """`session.metadata` 里那一块 → `Selection`。**读不出来就是 None，绝不抛。**

    两个维度的默认值都从这里补（老块里没有 `thinking` / `effort`）：**缺字段是
    "用默认"，不是"关掉"** —— 把一个缺字段读成 `thinking=False` 会让每一个旧会话在
    恢复之后突然不再思考，而那种变化在界面上完全看不出来（只是答案变差了、变便宜了）。
    """
    if not isinstance(block, dict):
        return None
    model = str(block.get("model") or "").strip()
    if not model:
        return None
    try:
        since = float(block.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    thinking = block.get("thinking")
    if not isinstance(thinking, bool):
        thinking = reasoning.DEFAULT_THINKING
    effort = reasoning.resolve_effort(str(block.get("effort") or "")) or \
        reasoning.DEFAULT_EFFORT
    return Selection(
        provider=str(block.get("provider") or "").strip(),
        model=model,
        thinking=thinking,
        effort=effort,
        since=since,
    )


# --- "换了什么"那句话 -----------------------------------------------------------

# 它在会话历史里的样子：一条 user 消息，前缀是 `[model changed: …]`。
#
# 为什么必须留下这句话：一次会话的后半段由另一个模型生成，而**历史本身看不出来**
# —— 换过之后模型读到的是"自己"前面那些话，它会以为那些是自己说的，并照着那个
# 风格/质量继续。留下一条明确的记录之后，"这段是三块钱一次的模型写的"变成会话
# 里可查的事实，而不是只有审计日志里那一串 model_call 才知道的事。
#
# 为什么是 user 角色：换模型是**用户/操作者**做的决定，不是模型的产出。放进
# assistant 角色等于伪造模型的主张，而"模型说过什么"是这个项目里最不该伪造的东西。
#
# 措辞和 DSH 的 `modelSwitchNotice` 同一路（`[model changed: …]`），因为这句话
# 服务的读者是模型自己。
_NOTICE = "[model changed: 上面那些轮次由 {old} 生成；从这个点开始，这个会话用 {new}。]"
_NOTICE_FIRST = "[model changed: 这个会话从这里开始用 {new}（此前还没有模型回答过）。]"
_NOTICE_SAME = "[model changed: 这个会话继续用 {new}。]"


def route(selection: Selection | None, *, fallback: str) -> str:
    """一个选择在"哪条路由上的哪个模型"这个意义上的名字（给那句说明和界面对账用）。

    有 provider 时写成 `provider/model` —— 两条路由有同名模型时光看模型名分不出
    请求发到哪儿，而那个差别在账单上。
    """
    if selection is None:
        return fallback
    if selection.provider:
        return f"{selection.provider}/{selection.model}"
    return selection.model


def change_notice(previous: str, selected: str) -> dict[str, str]:
    """换模型时插进 `session.messages` 的那一条。**一个 dict，不是字符串。**

    返回整条消息（而不是 content）是有意的：`session.messages` 里放的是消息。
    让调用方自己包一层 `{"role": "user", ...}` 就等于让"这是什么角色"多一个
    决定点，而它只有一个正确答案。
    """
    if previous and previous != selected:
        text = _NOTICE.format(old=previous, new=selected)
    elif previous:
        # 名字一样还走到了这里：**不说"换过"** —— 那会是假的。但也得留一句，
        # 否则"没有变化"和"什么都没发生"分不出来。
        text = _NOTICE_SAME.format(new=selected)
    else:
        text = _NOTICE_FIRST.format(new=selected)
    return {"role": "user", "content": text}


class SessionModel:
    """一个会话的模型选择与思考设置：**意图 + 事实**，以及"什么时候该留下那句话"。

    它是 `Agent` 与 `Runtime` 共用的那一份状态，和 `TodoBoard` / `SkillBoard` 一样
    绑在 `session.metadata` 上（换会话时旧的必须失效，否则新会话会用旧会话的选择）。

    两个名字分工明确，**这是这个类存在的全部理由**：

      * `selected` / `selected_provider` —— 用户**想要**的那个（`/model` 写它）；
      * `last_used` —— 上一个回合**实际**用的那个（每轮开头记一次）。

    "要留下换模型那句话"的判据是两者不等，而不是"刚刚调过 `/model`"。这个区别在两条路
    上都是对的：

      * 一轮正跑着的时候按 `/model`：本轮已经用旧模型发出去了，`last_used` 还是旧的
        —— 所以那句话留到**下一轮**，本轮不受影响；
      * 换了又换回来：两者相等，于是不留那句话 —— 中间那一次没有产生任何回答。

    **思考开关和强度不算"换了模型"**：它们改的是同一条 route 上这一次请求怎么想，
    所以 `notice_needed()` 不看它们（否则每按一次 `/effort` 都会往历史里插一句话，
    而那句话说的是"上面那些轮次由 A 生成"—— 那是假的，A 还是 A）。
    """

    def __init__(self, metadata: dict, *, fallback: str = "",
                 fallback_provider: str = "") -> None:
        self.metadata = metadata
        self.fallback = fallback
        self.fallback_provider = fallback_provider
        self.selection = load(metadata)

    @classmethod
    def restore(cls, metadata: dict, *, fallback: str = "",
                fallback_provider: str = "") -> "SessionModel":
        return cls(metadata, fallback=fallback, fallback_provider=fallback_provider)

    # -- 读 --------------------------------------------------------------------

    @property
    def selected(self) -> str:
        """这个会话该用哪个模型（没有会话级选择就是目录里那个默认）。

        **不折算别名**：`selected` 说的是"名字"，而折算过之后 `/status` 会报一个配置
        文件里并不存在的名字，那时"我配的到底是什么"就没有地方能回答了。窗口和目录
        查询各自走 `catalog.Registry.find`（那里折算）。
        """
        if self.selection is not None and self.selection.model:
            return self.selection.model
        return self.fallback

    @property
    def selected_provider(self) -> str:
        """哪条路由（空串 = 还没定，由装配层按目录的默认值处理）。"""
        if self.selection is not None and self.selection.provider:
            return self.selection.provider
        return self.fallback_provider

    @property
    def thinking(self) -> bool:
        if self.selection is not None:
            return self.selection.thinking
        return reasoning.DEFAULT_THINKING

    @property
    def effort(self) -> str:
        if self.selection is not None and self.selection.effort:
            return self.selection.effort
        return reasoning.DEFAULT_EFFORT

    @property
    def selected_since(self) -> float:
        return self.selection.since if self.selection is not None else 0.0

    @property
    def last_used(self) -> str:
        return str(self.metadata.get(LAST_USED_KEY) or "")

    # -- 写 --------------------------------------------------------------------

    def _current(self) -> Selection:
        return Selection(
            provider=self.selected_provider,
            model=self.selected,
            thinking=self.thinking,
            effort=self.effort,
            since=self.selected_since,
        )

    def select_route(self, *, provider: str, model: str, now: float | None = None) -> Selection:
        """记下"想用这条路由上的这个模型"（**不碰 `last_used`** —— 那是回合的事）。"""
        self.selection = store(self.metadata, Selection(
            provider=provider, model=model,
            thinking=self.thinking, effort=self.effort,
        ), now=now)
        return self.selection

    def select_thinking(self, on: bool, *, now: float | None = None) -> Selection:
        """只改开关，模型与强度原样保留 —— 它们互不影响。"""
        current = self._current()
        self.selection = store(self.metadata, Selection(
            provider=current.provider, model=current.model,
            thinking=on, effort=current.effort,
        ), now=now or current.since)
        return self.selection

    def select_effort(self, effort: str, *, now: float | None = None) -> Selection:
        """只改强度。**关着思考时也照样记下来** —— 用户的意图是"下次打开时还是它"。"""
        current = self._current()
        self.selection = store(self.metadata, Selection(
            provider=current.provider, model=current.model,
            thinking=current.thinking, effort=effort,
        ), now=now or current.since)
        return self.selection

    # -- 那一句话 ---------------------------------------------------------------

    def notice_needed(self) -> bool:
        """这一轮开头要不要留一句"模型换了"。

        **两个条件都要**：选中的 ≠ 上一轮实际用过的，**而且**它确实用过一回。

        第二条（`last_used` 非空）不是多余的：新会话、以及从没跑过一轮的会话，那个键
        是空的。只看第一条的话，**每次新会话的第一个回合都会插一句"模型换了"** ——
        而那时候没有任何"上一个模型"，说换过是假的。

        判据里**不含思考开关和强度**（见类 docstring）：它们不改 route。
        """
        return bool(self.last_used) and self.route_name() != self.last_used

    def route_name(self) -> str:
        """当前选择在"哪条路由上的哪个模型"这个意义上的名字。"""
        return route(self.selection, fallback=(
            f"{self.fallback_provider}/{self.fallback}" if self.fallback_provider
            and self.fallback else self.fallback))

    def record_use(self) -> None:
        """这一轮真的用上了当前选择 —— 在**请求发出去之前**记。

        放在发请求之前（而不是拿到回答之后）：会话里那句话说的是"接下来由谁生成"，
        而模型失败时这句话仍然是**真的**（下一次请求确实会用它）。放到回答之后的话，
        一次失败的回合会让这句话在下一轮重复出现。
        """
        self.metadata[LAST_USED_KEY] = self.route_name()

    def notice(self, previous: str | None = None) -> dict[str, str]:
        """这一轮开头那条消息（`previous` 缺省用 `last_used`）。"""
        old = self.last_used if previous is None else previous
        return change_notice(old, self.route_name())

    def as_state(self) -> dict:
        """给 `ui(state)` 快照的那几个字段。"""
        return {
            "model": self.selected,
            "model_provider": self.selected_provider,
            "model_since": self.selected_since,
            "model_last_used": self.last_used,
            "thinking": self.thinking,
            "effort": self.effort,
        }


__all__ = [
    "CATALOG_VERSION", "LAST_USED_KEY", "SELECTION_KEY", "Selection", "SessionModel",
    "change_notice", "from_block", "load", "route", "store", "to_block",
]
