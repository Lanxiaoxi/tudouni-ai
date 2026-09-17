"""历史压缩（Context Compaction）：**把较早的详细历史换成一份工作记忆。**

## 它和降级不是同一件事

`budget.py` 那一档降的是 **Artifact**（工具结果的正文有多少进得了这一次请求），
而 Compaction 动的是**历史本身**（哪些消息还要以原文出现在请求里）。两者回答的
是两个不同的问题：

    ContextBudget   「同一份信息，这次给多少？」
    Compaction      「这一段历史，还有必要带着原文吗？」

所以它在阶梯上的位置是**降级的下一位**：Artifact 全降到 metadata 还是塞不下时，
真正超窗的其实是那堆历史消息 —— 那时候唯一还有的余地就是把旧历史压掉。

## 三条原则（这个模块的每一处判断都是它们的直接后果）

  1. **压缩的是历史，不是当前工作状态。** 摘要必须回答"我在做什么、已经做了什么、
     发现了什么、做过哪些决定、还剩什么"—— 那是模型继续干活需要的东西。
  2. **最近的信息不压缩。** 最近几步是模型当下推理最相关的部分，一律原文保留。
  3. **原始历史仍然存在。** 这个模块**一个字节都不动 `session.messages`**：它只回答
     "折叠到第几条、摘要是什么"，而渲染那一侧据此替换。于是：

         完整历史 = 档案库（磁盘上的会话文件）
         Summary  = 工作记忆（一份 Artifact）
         Recent   = 当前注意力（原文保留的那几条）

     代价写在明处：会话文件和 `--history` 不会因为压缩而变小。**省 token 和
     省磁盘在 V1 是两件事** —— 想拿回被折叠的细节，读会话文件，或者让模型重新
     执行一次那个工具。

## 边界为什么必须落在 `user` 消息上

provider 要求每个 `tool_calls` 都有配对的 tool 结果，从中间切断历史会得到一个
**发不出去**的载荷（400，而且那个错误看起来像"上下文太长"）。所以折叠点只在
`role == "user"` 的消息之前 —— 一条 user 消息永远是"上一个回合已经完整结束"的标志。

两条例外：

  * **第一条 user 永不折叠**（它是这个会话的任务锚点，也是 `message_marks` 把
    stable/pinned 挂上去的那一条）；
  * **最后 `KEEP_RECENT_MESSAGES` 条永不折叠**（原则 2）。

## 摘要为什么由模型写，而不是数出来

纯机械摘要（"读过 12 个文件、跑了 3 条命令"）零成本、可测，但它抓不到这一层最值钱
的两样东西：**关键发现**和**已经做出的决定**。而那两样恰恰是"让 Agent 不必每次都把
过去的全部细节带在脑子里"的前提 —— 没有它们，压缩之后模型只能重新摸索一遍。

代价也写在明处：压缩时要多一次模型往返，那一步会明显变慢，而且它可能失败。失败
时的处置是**什么都不改**（见 `Agent._compact`）：宁可继续用降级那一档兜着，也不要
让一个半截的摘要进上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent_runtime import paths

# `session.metadata` 里那一块的键。**和 `model_selection` 同一层**（会话自己的事实），
# 而不是塞进 `ContextState`：那块要每次落盘都写（见 `state/store.py` 的 meta 记录），
# 而 ContextState 只在 version 变了之后才写 —— 摘要的边界变化不该被那个水位漏掉。
COMPACTION_KEY = "context_compaction"

# 这一块的格式版本。**它独立于会话文件的 STATE_VERSION**：那一版说的是"记录类型"，
# 这一版说的是"这一个键里的字段含义"。将来字段含义变了，旧会话读出来应该退化成
# "没有压缩过"，而不是让整个会话打不开。
BLOCK_VERSION = 1

# 折叠点在历史里怎么被说出来（给模型的正文表头）。**它必须自解释**：模型看到的是
# "之前发生过什么已经换成摘要了"，而不是"会话从这里开始"。
SUMMARY_HEADER = "【历史摘要】（以下是较早对话的压缩结果，更早的原文不在本次上下文里）"

# 保留多少条**最近**的消息不压缩。按条数而不是按 token：token 是估算的，而"最近
# 几轮"在语义上本来就是一个条数。12 条约等于 6 个来回。
KEEP_RECENT_MESSAGES = 12

# 至少要折掉多少条才值得压一次。**它挡的是"压了个寂寞"**：折 2 条消息换一次模型
# 往返，还顺手把前缀缓存作废一次 —— 那是负收益。
MIN_FOLD_MESSAGES = 8

# 喂给摘要模型的骨架最多多少字符。超了就从**最早**那头丢（保留最近的那一段），
# 并如实标出丢了多少条 —— 最近发生的事对"当前状态"这一格更重要。
DIGEST_MAX_CHARS = 24_000

# 一次工具调用的参数最多写多少字符。参数可能是一整份文件正文（write_file），
# 而摘要要的是"它改了什么"，不是全文。
DIGEST_ARGS_CHARS = 160

# 摘要模型看到的系统提示词（跟着代码走，和 `prompts/system.zh.md` 同一条规矩）。
PROMPT_FILE = "compact.zh.md"


@dataclass(frozen=True, slots=True)
class Compaction:
    """这个会话压缩到哪了。**它就是"摘要 + 边界"这两样事实的唯一载体。**

    `folded_messages` 是**消息条数**（不是下标）：它和 `state/store.py` 的水位是同一
    种度量 —— "前 N 条已经折进摘要了"。下一次压缩从这里接着折，所以它是增量折叠的
    全部依据。

    `summary_id` 指向一份 Artifact。**旧摘要不删**：每一次压缩产生一份新的（内容寻址
    的 id 因此也不同），而旧的那些留在 ArtifactStore 里 —— 那是"档案库"这个词的具体
    含义，也是事后能看出"摘要怎么一步步变成现在这样"的唯一途径。
    """

    folded_messages: int = 0
    summary_id: str = ""
    generation: int = 0
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        """折过东西、并且摘要还在 —— 两个条件缺一不可。

        只有边界没有摘要（正文被人删了）时它**不算激活**：那时候渲染会退化成"原样
        发历史"，而那比发一句"摘要没了"更安全（后者会让模型以为历史被清空了）。
        """
        return self.folded_messages > 0 and bool(self.summary_id)


def load(metadata: Mapping[str, Any]) -> Compaction | None:
    """`session.metadata` → 压缩状态。**读不出来就是 None，绝不抛。**

    老会话文件里没有这个键，而一个坏掉的键不该让整个会话打不开 —— 和 `model.py`
    的 `load` / `agents_md.from_block` 立的是同一条规矩。
    """
    block = metadata.get(COMPACTION_KEY) if isinstance(metadata, Mapping) else None
    return from_block(block)


def store(metadata: dict[str, Any], state: Compaction) -> Compaction:
    """把压缩状态写进 `session.metadata`（**不落盘** —— 落盘是 checkpoint 的事）。"""
    metadata[COMPACTION_KEY] = to_block(state)
    return state


def to_block(state: Compaction) -> dict[str, Any]:
    return {
        "version": BLOCK_VERSION,
        "folded_messages": int(state.folded_messages),
        "summary_id": str(state.summary_id),
        "generation": int(state.generation),
        "updated_at": float(state.updated_at),
    }


def from_block(block: object) -> Compaction | None:
    """`session.metadata` 里那一块 → `Compaction`。**读不出来就是 None。**"""
    if not isinstance(block, Mapping):
        return None
    try:
        folded = int(block.get("folded_messages") or 0)
    except (TypeError, ValueError):
        return None
    if folded <= 0:
        # 折了 0 条 = 没压过。**不返回一个"空的 Compaction"**：那会让"这个会话有压缩
        # 状态"在 `is not None` 这个判据上变成真的，而调用方正是这么判断的。
        return None
    try:
        updated = float(block.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated = 0.0
    return Compaction(
        folded_messages=folded,
        summary_id=str(block.get("summary_id") or ""),
        generation=int(block.get("generation") or 0),
        updated_at=updated,
    )


# --- 边界 -----------------------------------------------------------------------


def first_user_index(messages: list[dict[str, Any]]) -> int:
    """第一条 user 消息在哪。**没有就返回 `-1`。**"""
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            return index
    return -1


def fold_point(messages: list[dict[str, Any]], folded: int = 0) -> int:
    """该折叠到第几条（**左闭右开**：折叠 `[0, 返回值)`）。

    返回 `0` = 这次什么都不折（历史太短、或者找不到合法边界）。判据按优先级：

      1. 折叠区至少要有 `MIN_FOLD_MESSAGES` 条，而且后面要**留下**至少
         `KEEP_RECENT_MESSAGES` 条 —— 两个都满足不了就不折；
      2. 边界必须落在一条 **user 消息** 上（见模块 docstring 那一段）；
      3. 边界不能越过**第一条 user**（那个位置保留原文）；
      4. 边界必须比上次的 `folded` **更靠后** —— 否则这次压缩什么都没新增，
         而"重新生成一份一模一样的摘要"只是一次白花的模型调用。

    ## 边界怎么选（两段，顺序不能反）

    **第一段：位置。** 先取 `upper = 总数 - KEEP_RECENT_MESSAGES` —— 也就是"只保留
    最近 `KEEP_RECENT_MESSAGES` 条"的那条线。这一步决定**折掉多少**：折得越多，模型
    要带的原文越少，而摘要那一次模型往返的价钱与折多少无关。反过来（"尽量少折、
    只折到够"）会让每次压缩只换来几个百分点的余量，而摘要的成本一次都不少 ——
    实测踩过：折 5 条 / 49 条，摘要本身比折掉的那几条还贵。

    **第二段：对齐。** `upper` 那个位置**不一定落在一条 user 消息上**，而边界必须
    落在 user 上（第 2 条）。所以从 `upper` **往前**退，停在最靠后的一条 user 消息
    上 —— 也就是"最近的那个完整回合的开头"。

        折到这里 ↓
        [ 被折掉的历史 ][user 最近保留的第一个完整回合][assistant][tool]…[最新]

    为什么必须是完整回合（而不是"留够条数"就切）：`assistant` 的 `tool_calls` 与
    紧随其后的 `tool` 结果是**一对**，从中间切开会留下一条带 `tool_calls` 却没有
    配对结果的消息，provider 直接 400（而那个错误看起来像"上下文太长"）。往前退
    几步是多保留几条原文，代价小得多。

    取**最靠后**的那条 user（而不是最靠前的）：最近的信息尽量原文保留（原则 2），
    而"能折多少就折多少"是它的直接后果。
    """
    total = len(messages)
    upper = total - KEEP_RECENT_MESSAGES
    if upper - max(0, folded) < MIN_FOLD_MESSAGES:
        return 0

    floor = max(folded, first_user_index(messages) + 1, 1)
    for index in range(upper - 1, floor - 1, -1):
        if messages[index].get("role") == "user":
            return index
    return 0


# --- 骨架（喂给摘要模型的输入） ---------------------------------------------------


def digest_messages(
    messages: list[dict[str, Any]],
    *,
    start: int = 0,
    stop: int | None = None,
    limit: int = DIGEST_MAX_CHARS,
) -> str:
    """把要被折叠的那一段历史压成"骨架"：**给摘要模型看的，不是给人看的。**

    `[start, stop)` 是那一段。`stop=None` = 到末尾。

    ## 为什么不给工具结果的正文

    两个理由，方向一致：

      * **它不需要。** 摘要要写的是"发生了什么、发现了什么、决定了什么"，而
        `read_file` 返回的 12 万字符里没有这三样东西 —— 它们藏在模型那句"我确认了
        X"里，而那句话本来就在骨架里；
      * **给了就没意义了。** 折叠一段 20 万 token 的历史需要读 20 万 token 的正文，
        而压缩这件事的目的正是把这段历史变便宜。摘要模型读全文再写摘要，除了多花
        一次钱，什么也没换来。

    所以工具结果只留那行**引用**（`[artifact art_x · 12480 字符 · read_file]`）——
    它天然带着"这里本来有一份多大的东西"，而模型据此知道"需要的话可以重新读一次"。

    ## 丢的是最早的那一段，不是最新的

    骨架超过 `limit` 时从**开头**截掉，因为"当前状态"和"下一步"这两格靠的是最近
    发生的事。截掉多少条如实写在开头 —— 一条不说就丢的训练数据里，"我漏了一段"
    和"那一段没发生什么"长得一模一样。
    """
    blocks: list[str] = []
    used = 0
    end = len(messages) if stop is None else max(start, min(stop, len(messages)))
    kept_from = end

    for index in range(end - 1, start - 1, -1):
        block = describe_message(messages[index])
        if not block:
            kept_from = index
            continue
        if used + len(block) > limit and blocks:
            # 已经攒了内容、这一条放不下了 ⇒ 从这里往前的都不要了。
            break
        blocks.append(block)
        used += len(block)
        kept_from = index

    blocks.reverse()
    dropped = kept_from - start
    head = ""
    if dropped > 0:
        head = (
            f"（更早的 {dropped} 条消息没有列在这里 —— 它们发生得更早，"
            f"而这一段骨架只保留最近的部分）\n"
        )
    return head + "\n".join(blocks)


def describe_message(message: Mapping[str, Any]) -> str:
    """一条历史消息在骨架里长什么样。**空串 = 这条不必出现。**

    四类，各有各的"该留下什么"：

      * **user** 原文（逐字）—— 用户说了什么是要命的事实，压缩它是摘要自己该干的事；
      * **assistant** 正文原文，外加它调了哪些工具（名字 + 参数预览）；
      * **tool** 只留那句引用 —— 正文见 `digest_messages` 的 docstring；
      * 别的（比如 `[model changed: …]` 那种系统插话）按正文原样。
    """
    role = message.get("role")
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls = message.get("tool_calls")

    if role == "tool":
        # 它本来就是一句引用；万一老会话里存的是全文，也只留第一行 —— 骨架里
        # 不该出现正文（见 digest_messages）。
        return f"[工具结果] {_first_line(text)}" if text else ""

    label = {"user": "[用户]", "assistant": "[助手]", "system": "[系统]"}.get(
        str(role), f"[{role}]"
    )
    parts = [f"{label} {text}".rstrip()] if text else []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            name = ""
            arguments = ""
            if isinstance(function, Mapping):
                name = str(function.get("name") or "")
                arguments = str(function.get("arguments") or "")
            elif isinstance(call.get("name"), str):
                name = str(call["name"])
                arguments = str(call.get("arguments") or "")
            parts.append(f"  → 调用 {name}({_clip(arguments, DIGEST_ARGS_CHARS)})")
    return "\n".join(part for part in parts if part)


def _first_line(text: str) -> str:
    line = text.split("\n", 1)[0]
    return _clip(line, DIGEST_ARGS_CHARS)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…"


# --- 渲染 -----------------------------------------------------------------------


def summary_message(text: str) -> dict[str, Any]:
    """摘要那一条消息长什么样。**它是一条普通的 `user` 消息。**

    为什么不是 `system`：折叠区原本的内容里有用户和助手的话，而把它们换成一条
    system 会让"谁在说话"这件事在这一段上变成假的 —— 模型读到"系统要求我分析这个
    项目"和"用户要求我分析这个项目"是两件事。而一条 user 消息在形状上和它替换掉
    的那条 user 完全等价（原第一条 user 本来就占这个位置）。
    """
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n\n{text.strip()}"}


def attention(messages: list[dict[str, Any]], folded: int) -> list[dict[str, Any]]:
    """**这一次请求真正要看的历史**：系统提示词 + 摘要 + 未被折叠的原文。

    ## 系统提示词为什么被单独拎出来

    它是 `messages[0]`（`Session.new` 只在那里写一次），而折叠区是 `[0, folded)` ——
    两者在数字上重叠。**它必须留在载荷里**，理由三条，一条比一条硬：

      * 它是整个会话的行为准则（"只使用提供的工具""写后要验证"），少了它模型就换了
        一套行为；
      * 它 pinned 的判据正是"它是第一条 system"（见 `agents/agent.py` 的
        `message_marks`），而那个判据的前提是它真的在历史里；
      * `renderer` 原样透传它，`tool_calls` 的配对也和它无关。

    所以折叠区里**永远不含它**，而这一条由这里（而不是调用方）保证：调用方只该知道
    "折叠到第几条"，"系统提示词不能被折掉"是这个模块的边界规则。

    ## 返回什么

        [system, 摘要(如果折过), 未折叠的原文…]

    `folded <= 0`（没压过）时**逐字节返回原列表**：没压缩的会话因此走的是一条与
    压缩功能之前完全相同的路径。
    """
    if folded <= 0:
        return messages

    head = messages[:1] if messages and messages[0].get("role") == "system" else []
    tail_start = min(folded, len(messages))
    return [*head, *messages[tail_start:]]


def folded_view(
    messages: list[dict[str, Any]],
    folded: int,
    summary: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """**折叠之后真正要发出去的那一份历史**（`agents/agent.py` 的载荷就是它）。

        [system, 摘要(user), 未折叠的原文…]

    摘要**排在系统提示词之后**：系统提示词是整段请求的第一条（也是唯一能稳定命中
    前缀缓存的那一节，见 `renderer.py`），而摘要是"过去"的概括 —— 它没有理由插在
    行为准则前面。载荷的形状因此是"准则 → 记忆 → 近况"，和模型读一份对话的顺序一致。

    `summary=None`（没压过）时就是 `attention` 的结果 —— 那条路逐字节等于压缩功能
    之前的行为。
    """
    view = attention(messages, folded)
    if summary is None:
        return view
    head = view[:1] if view and view[0].get("role") == "system" else []
    return [*head, summary, *view[len(head):]]


def summary_text(store: Any, state: Compaction | None) -> str | None:
    """摘要的正文。**取不到返回 None**（调用方据此如实说一句，见 `missing_summary`）。

    `store` 是 `ArtifactStore`（这里不 import 它的类型，是为了让这个模块能只被
    "有哪些事实"测试，不必起一个真的目录）。
    """
    if state is None or not state.summary_id:
        return None
    text = store.content(state.summary_id)
    return text if text else None


def missing_summary(state: Compaction) -> str:
    """摘要的正文丢了（Artifact 目录被人删了）时，载荷里那一句话。

    **不能是空串。** 空串会被读成"这一整段历史什么都没发生"，而那是错的 ——
    它会让模型心安理得地重做一遍已经做过的事。说清"折过 N 条、内容取不到了、
    需要的话读会话文件"它才有正确的处置。
    """
    return (
        f"{SUMMARY_HEADER}\n\n"
        f"（摘要正文已经取不到了，它原本概括了最早的 {state.folded_messages} 条消息。"
        f"如果需要那段细节，请让用户重新读一次相关文件，或者查看会话文件。）"
    )


# --- 提示词 ---------------------------------------------------------------------


def prompt(path: Path | None = None) -> str:
    """读摘要提示词。

    **和 `state/session.py` 的 `load_system_prompt` 同一条规矩**，两条都是刻意的：

      * **不在 import 时读** —— 那会让 `--history` / `--list` / `--audit` 这些根本
        不发请求的路径也依赖这个文件存在，而"少一个提示词文件不该让查历史也失效"
        正是那一处立下的规矩；
      * **不缓存** —— 改完提示词不用重启，而一个进程只压几次，重新读盘的代价可以
        忽略。

    **它跟着代码走，不跟着工作区走**（`paths.package_dir()`，不是 `workspace_dir()`）：
    这份提示词描述的是"怎么给这个 agent 的记忆做摘要"，和 `prompts/system.zh.md`
    是同一个主人。工作区那边那份 `AGENT.md` 是另一个主人的文本。
    """
    file = path or (paths.package_dir() / "prompts" / PROMPT_FILE)
    if not file.exists():
        raise FileNotFoundError(
            f"摘要提示词不存在：{file}（压缩要用它，见 context/compaction.py）"
        )
    return file.read_text(encoding="utf-8").strip()


__all__ = [
    "BLOCK_VERSION",
    "COMPACTION_KEY",
    "DIGEST_ARGS_CHARS",
    "DIGEST_MAX_CHARS",
    "KEEP_RECENT_MESSAGES",
    "MIN_FOLD_MESSAGES",
    "PROMPT_FILE",
    "SUMMARY_HEADER",
    "Compaction",
    "attention",
    "describe_message",
    "digest_messages",
    "first_user_index",
    "fold_point",
    "folded_view",
    "from_block",
    "load",
    "missing_summary",
    "store",
    "summary_message",
    "summary_text",
    "to_block",
]
