"""选哪个模型：目录、会话里的选择、以及"换过模型"那句话。

## 三件事，为什么住在一起

  1. **目录**（`MODEL_CATALOG`）—— 哪些模型是"这个运行时认识的名字"。它同时是
     `/model` 那个清单的数据源、`CONTEXT_WINDOWS` 的唯一来源、以及"这个名字不认识"
     那条错误的判据；
  2. **会话里的选择**（`SessionModel`）—— 选了哪个、这个会话实际用过的是哪个。
     它跟着 `session.metadata` 落盘；
  3. **换模型时写进对话的那句话**（`change_notice`）。

第 2、3 件事必须住在一起：那句说明说的是"上一条 route"和"下一条 route"的差，
而"上一条"只有 2 知道。拆开的话就得让调用方自己去凑那两个名字，而它凑错的方式
（比如拿"当前模型"当"上一个"）在界面上看起来完全正常。

## 它为什么在 `state/` 而不是 `models/`

`models/` 是**适配层**：把 provider 的协议翻译成 `ModelResponse`（见 `models/types.py`）。
"我们认识哪几个模型名"是**数据**，不是翻译 —— 而且它的三个消费者（`runtime/config.py`
的上下文窗口、`agents/agent.py` 的会话选择、`runtime/composition.py` 的装配）里有两个
住在 `state/` 与 `agents/`，那是内核（见 tests/test_imports.py 的分层测试）。放进
`models/` 的话，一个纯数据表就得从适配层里被内核读出来，方向不利于"换 provider 只动
`models/`"。

## 目录是**数据**，不是分支

`/model 不带参数`列它、`/model <名字>`按它校验、`CONTEXT_WINDOWS` 由它派生 ——
三处读同一份表。任何一处自己写死一个模型名，都会在下一次官方改名字时漂掉，
而漂掉的症状是"这个模型明明能用，界面说它不认识"。

## 已知边界（说在明处）

目录里的模型**共享同一个 base_url 与同一把密钥**：这个项目只有一个 provider
（`DEEPSEEK_BASE_URL`）。所以"换模型"换的是模型名，不是换网关 —— 谁想接两个网关，
那是另一件事（`Runtime.select_model` 因此会在 base_url 对不上时拒绝切换，而不是
假装切成功了）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 会话里那两个键。**不用 `asdict` 存**：这一块要能被子类化/被后来的版本加字段而
# 不让旧会话打不开（见 `from_block`），所以形状是显式写下来的。
SELECTION_KEY = "model_selection"

# "上一个回合实际用的是谁"。它**不属于选择**（选择是意图，它是事实），所以单独一个
# 键：混进 `SELECTION_KEY` 那块里之后，"换了又换回来"和"从来没换过"就分不出来了。
LAST_USED_KEY = "model_last_used"

# 官网那张表上现在有这两个名字（`deepseek-flash` 是 V4.1-Flash 的**官方名字**）。
#
# 旧名字（`deepseek-v4-flash` / `deepseek-v4-flash-vision-exp`）**不列进目录**：
# 官方明确说它们对应的模型已下线、请求由 V4.1-Flash 提供服务 ——
# 它们是能调用的**别名**，不是能选的东西。列进去会让 `/model` 摆出两个效果完全
# 一样、价钱也一样的选项，而那是在骗人。别名仍然认（见 `ALIASES`），因为
# `DEEPSEEK_MODEL` 和环境里可能就写着它们，而"你昨天配的名字今天不能用了"
# 不是我们该制造的意外。
CATALOG_VERSION = 1

DEFAULT_MODEL = "deepseek-flash"


@dataclass(frozen=True, slots=True)
class ModelRef:
    """目录里的一条：一个模型名，以及"选它意味着什么"。"""

    id: str
    label: str
    # 上下文窗口（输入侧上限）。`None` = 不知道 —— 界面按"只报用量、不报占比"处理，
    # 因为**错的百分比比没有百分比更坏**（它会被人当成真的）。
    window: int | None
    # 一句话说明"什么时候该选它"。`/model` 那张清单里跟着名字显示。
    summary: str
    # 细节（价格、并发、能力差异）。`/model --all` 才显示 —— 清单里那一行是给
    # "我现在要选一个"用的，塞满价格只会让人选不出来。
    note: str = ""


# 官方文档「模型 & 价格」那张表（api-docs.deepseek.com/zh-cn/quick_start/pricing）：
# 两个名字、窗口都是 1M、都支持 Tool Calls。
MODEL_CATALOG: tuple[ModelRef, ...] = (
    ModelRef(
        id="deepseek-flash",
        label="Flash",
        window=1_000_000,
        summary="快、便宜，日常干活用它",
        note="DeepSeek-V4.1-Flash；支持图像理解；并发上限 2500。"
             "缓存命中输入比 Pro 便宜约 7 倍。",
    ),
    ModelRef(
        id="deepseek-v4-pro",
        label="Pro",
        window=1_000_000,
        summary="贵得多，难题上更强",
        note="DeepSeek-V4-Pro-0813；不支持图像理解；并发上限 500。"
             "缓存未命中输入约为 Flash 的 4.5 倍。",
    ),
)

# 认下但**不列进目录**的旧名字 → 现在真正在服务的那个模型。
#
# 为什么认它们：`DEEPSEEK_MODEL` 里可能就写着它们（它们仍然可调用，官方说请求由
# V4.1-Flash 提供服务）。而"能用的名字被这个运行时判成不认识"是最没必要的那种意外。
# 为什么折算到主名字上：折算之后 `/status` 报的是**真正在服务的那个模型**，
# 而不是一个已经下线的名字 —— 后者会让"上下文窗口是多少""价钱怎么算"都无从谈起。
ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


def canonical(name: str) -> str:
    """把一个模型名折算成目录里的那个名字（认不出来就原样返回）。

    **不认识的名字不报错**：`DEEPSEEK_MODEL` 可以是任意一个网关上的任意一个名字
    （这个项目明确留了"指向自建网关"的余地，见 `runtime/config.py`）。报错会让那种
    用法根本起不来。所以这里只做"能折算就折算"，剩下的交给 `get()` 的 `None`。
    """
    text = (name or "").strip()
    return ALIASES.get(text, text)


def get(name: str) -> ModelRef | None:
    """按名字取一条目录项（先折算别名）。**取不到就是 None，不猜。**"""
    wanted = canonical(name)
    for item in MODEL_CATALOG:
        if item.id == wanted:
            return item
    return None


def context_window(name: str) -> int | None:
    """这个模型名的上下文窗口；不认识的名字返回 None。"""
    item = get(name)
    return item.window if item is not None else None


def context_windows() -> dict[str, int]:
    """`{模型名: 窗口}` —— 目录 + 别名，**只含知道窗口的那些**。

    它是 `runtime/config.py` 那张 `CONTEXT_WINDOWS` 的唯一来源。别名也进表：别人
    的 `DEEPSEEK_MODEL` 里写着旧名字时，那张表得照样答得出分母（旧名字现在由
    V4.1-Flash 提供服务，窗口同 Flash）。
    """
    table = {item.id: item.window for item in MODEL_CATALOG if item.window is not None}
    for alias in ALIASES:
        window = context_window(alias)
        if window is not None:
            table[alias] = window
    return table


def default_id(configured: str = "") -> str:
    """没有会话级选择时用哪个模型名。

    配置里写了的优先（**原样**用，不折算：那是用户自己选的，我们不该悄悄改成另一个
    名字）；没写就是目录里的默认值。
    """
    return (configured or "").strip() or DEFAULT_MODEL


def catalog_rows() -> list[dict]:
    """目录 → 协议/界面要的那种平常数据（`init.model_catalog`）。"""
    return [
        {
            "id": item.id,
            "label": item.label,
            "window": item.window,
            "summary": item.summary,
            "note": item.note,
        }
        for item in MODEL_CATALOG
    ]


# --- 会话里的选择 ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Selection:
    """这个会话现在的**意图**：想用哪个模型，以及什么时候改的主意。

    `since` 是"这一轮是在什么时候选的"（epoch 秒），给 `--audit` / `/status` 用：
    同一个会话里换过模型之后，"这条回答是哪一段的"光看历史读不出来。
    """

    model: str
    since: float = 0.0


def load(metadata: dict) -> Selection | None:
    """`session.metadata` → 这个会话的选择。**读不出来就是 None，绝不抛。**

    旧会话文件里没有这个键（这个功能之前建的），而一个坏掉的键不该让整个会话打不开
    —— 和 `agents_md.from_block` 立的是同一条规矩。
    """
    block = metadata.get(SELECTION_KEY) if isinstance(metadata, dict) else None
    return from_block(block)


def store(metadata: dict, model: str, *, now: float | None = None) -> Selection:
    """把选择写进 `session.metadata`（**不落盘** —— 落盘是 checkpoint 的事）。"""
    selection = Selection(model=model, since=now if now is not None else time.time())
    metadata[SELECTION_KEY] = {
        "version": CATALOG_VERSION,
        "model": selection.model,
        "since": selection.since,
    }
    return selection


def to_block(selection: Selection) -> dict:
    return {"version": CATALOG_VERSION, "model": selection.model, "since": selection.since}


def from_block(block: object) -> Selection | None:
    """`session.metadata` 里那一块 → `Selection`。**读不出来就是 None，绝不抛。**"""
    if not isinstance(block, dict):
        return None
    name = str(block.get("model") or "").strip()
    if not name:
        return None
    try:
        since = float(block.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    return Selection(model=name, since=since)


# --- "模型换了"那句话 ------------------------------------------------------------

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
_NOTICE_ZH = (
    "[model changed: 上面那些轮次由 {old} 生成；从这个点开始，这个会话用 {new}。]"
)
_NOTICE_FIRST = "[model changed: 这个会话从这里开始用 {new}（此前还没有模型回答过）。]"
_NOTICE_SAME = "[model changed: 这个会话继续用 {new}。]"


def change_notice(previous: str, selected: str) -> dict[str, str]:
    """换模型时插进 `session.messages` 的那一条。**一个 dict，不是字符串。**

    返回整条消息（而不是 content）是有意的：`session.messages` 里放的是消息。
    让调用方自己包一层 `{"role": "user", ...}` 就等于让"这是什么角色"多一个
    决定点，而它只有一个正确答案。
    """
    if previous and previous != selected:
        text = _NOTICE_ZH.format(old=previous, new=selected)
    elif previous:
        # 名字一样还走到了这里（`/model` 重报了同一个）：**不说"换过"** ——
        # 那会是假的。但也得留一句，否则"没有变化"和"什么都没发生"分不出来。
        text = _NOTICE_SAME.format(new=selected)
    else:
        text = _NOTICE_FIRST.format(new=selected)
    return {"role": "user", "content": text}


class SessionModel:
    """一个会话的模型选择：**意图 + 事实**，以及"什么时候该留下那句话"。

    它是 `Agent` 与 `Runtime` 共用的那一份状态，和 `TodoBoard` / `SkillBoard` 一样
    绑在 `session.metadata` 上（换会话时旧的必须失效，否则新会话会用旧会话的选择）。

    两个名字分工明确，**这是这个类存在的全部理由**：

      * `selected` —— 用户/操作者**想要**的那个（`/model` 写它）；
      * `last_used` —— 上一个回合**实际**用的那个（每轮开头记一次）。

    "要留下换模型那句话"的判据是 `selected != last_used`，而不是"刚刚调过
    `/model`"。这个区别在两条路上都是对的：

      * 一轮正跑着的时候按 `/model`：本轮已经用旧模型发出去了，`last_used` 还是旧的
        —— 所以那句话留到**下一轮**，本轮不受影响（这是刻意的，见
        `protocol/channels.py` 的 `_set_model`）；
      * 换了又换回来：`selected == last_used`，于是不留那句话 —— 中间那一次
        没有产生任何回答，说"模型换过"就是假的。
    """

    def __init__(self, metadata: dict, *, fallback: str):
        self.metadata = metadata
        self.fallback = fallback
        self.selection = load(metadata)

    @classmethod
    def restore(cls, metadata: dict, *, fallback: str) -> "SessionModel":
        return cls(metadata, fallback=fallback)

    @property
    def selected(self) -> str:
        """这个会话该用哪个模型（没有会话级选择就是配置里那个）。

        **不折算别名**：`selected` 说的是"名字"，而折算过之后 `/status` 会报一个
        配置文件里并不存在的名字，那时"我配的到底是什么"就没有地方能回答了。
        窗口和目录查询各自走 `model.get()`（那里折算）。
        """
        if self.selection is not None:
            return self.selection.model
        return self.fallback

    @property
    def selected_since(self) -> float:
        return self.selection.since if self.selection is not None else 0.0

    @property
    def last_used(self) -> str:
        return str(self.metadata.get(LAST_USED_KEY) or "")

    def select(self, model: str, *, now: float | None = None) -> Selection:
        """记下"想用这个"（**不碰 `last_used`** —— 那是回合的事）。"""
        self.selection = store(self.metadata, model, now=now)
        return self.selection

    def notice_needed(self) -> bool:
        """这一轮开头要不要留一句"模型换了"。

        **两个条件都要**：选中的 ≠ 上一轮实际用过的，**而且**它确实用过一回。

        第二条（`last_used` 非空）不是多余的：新会话、以及从没跑过一轮的会话，那个键
        是空的。只看第一条的话，**每次新会话的第一个回合都会插一句"模型换了"** ——
        而那时候没有任何"上一个模型"，说换过是假的（它会在每一份新会话的历史里出现，
        读起来像系统提示词的一部分）。
        """
        return bool(self.last_used) and self.selected != self.last_used

    def record_use(self) -> None:
        """这一轮真的用上了 `selected` —— 在**请求发出去之前**记。

        放在发请求之前（而不是拿到回答之后）：会话里那句话说的是"接下来由谁生成"，
        而模型失败时这句话仍然是**真的**（下一次请求确实会用它）。放到回答之后的话，
        一次失败的回合会让这句话在下一轮重复出现。
        """
        self.metadata[LAST_USED_KEY] = self.selected

    def notice(self, previous: str | None = None) -> dict[str, str]:
        """这一轮开头那条消息（`previous` 缺省用 `last_used`）。

        调用方（`Agent.run`）通常在 `record_use()` **之前**调它，所以默认值取的是
        上一个回合用过的那个名字 —— 那正是"上面那些轮次由谁生成"的答案。
        """
        old = self.last_used if previous is None else previous
        return change_notice(old, self.selected)

    def as_state(self) -> dict:
        """给 `ui(state)` 快照的那几个字段。"""
        return {
            "model": self.selected,
            "model_since": self.selected_since,
            "model_last_used": self.last_used,
        }


# `last_used` 的键名见文件开头（`LAST_USED_KEY`）：它和选择分开存，见那里。


__all__ = [
    "ALIASES", "CATALOG_VERSION", "DEFAULT_MODEL", "LAST_USED_KEY", "MODEL_CATALOG",
    "SELECTION_KEY", "ModelRef", "Selection", "SessionModel", "canonical",
    "catalog_rows", "change_notice", "context_window", "context_windows", "default_id",
    "from_block", "get", "load", "store", "to_block",
]
