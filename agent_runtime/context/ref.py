"""会话历史里那句"引用"。

工具结果的正文不在 `session.messages` 里了 —— 里面只有一句指向 Artifact 的引用。
这个模块是**那个格式唯一的定义处**：谁要拼、谁要认，都从这里走。

## 为什么历史里不放正文

`session.messages` 是**历史**（"发生过什么"），不是 Context（"现在要让模型看见
什么"）。这两件事过去是同一个东西，而后果在 token 账单上：一次 `read_file` 的
12 万字符此后每一轮都要重发一遍，哪怕模型早就把它读完了。

分开之后：

    History   一次 read_file 就是一句引用        永不增长（相对正文而言）
    Context   模型这一轮**此刻**要看见的那一段   由 representation 决定

## 引用长什么样

    [artifact art_9f2c1a4b7e30 · 12480 字符 · read_file]

它是**自解释的**：人用 `--history` 看会话时读得懂，模型如果某一轮恰好只看到这一句
（比如降级到 metadata 档）也知道"这里本来有一份东西、它有多大"。后面那半句关于
读取方式的说明（"完整内容由 Context 按 representation 渲染"）**不写给模型**：
模型看到的永远是渲染好的正文，这句内部机制的说明只会白占 token。

## 为什么另存一个 `artifact_id` 字段

格式是给人看的，而程序不该靠解析一段人话来找 Artifact —— 改一个标点就会让所有
历史里的引用取不到正文，而且失败方式是"模型看不到工具结果"，不像一个格式错误。
所以 tool 消息上另有一个显式的 `artifact_id` 键，解析只作为**兜底**（老会话文件
里没有那个键，见 `state/store.py` 的兼容路径）。
"""

from __future__ import annotations

import re
from typing import Any

# 引用里的标记。**只认这一种** —— 宽松匹配（比如"内容里出现 art_ 就算"）会让
# 一份恰好提到 Artifact 的文件正文被当成引用，而它取不到正文。
MARK = "[artifact"

# id 在**给人看的文本里**最多显示多少个字符。内容寻址的 id 是
# `art_` + 12 位哈希（17 个字符），但 `hydrate` 给老会话造的 id 来自正文哈希，
# 而任何长度都可能。表头里塞一个 200 字符的 id 不只是难看 —— 它是**每一轮都要
# 重发**的 token。超出的部分截掉并标一个省略号：读的人知道这是个 id、也知道它
# 被截过，而正文里那个显式的 `artifact_id` 字段仍然带着完整值。
MAX_SHOWN_ID = 24


def shown_id(artifact_id: str) -> str:
    """id 在文本里的显示形式。**只影响显示**，不改变身份。"""
    if len(artifact_id) <= MAX_SHOWN_ID:
        return artifact_id
    return f"{artifact_id[:MAX_SHOWN_ID]}…"

# `[artifact art_xxx · 12480 字符 · read_file]`
# 三段都可以缺：手写的历史、老版本的引用、将来多了几段 —— 只要 id 认得出来。
_PATTERN = re.compile(
    r"\[artifact\s+(?P<id>art_[A-Za-z0-9_-]+)"
    r"(?:\s*·\s*(?P<rest>[^\]]*))?\]"
)


def build(artifact_id: str, chars: int, tool: str = "") -> str:
    """拼出那句引用。"""
    parts = [f"{chars} 字符"]
    if tool:
        parts.append(tool)
    return f"{MARK} {shown_id(artifact_id)} · {' · '.join(parts)}]"


def parse(text: Any) -> str | None:
    """从一段文本里把 artifact_id 认出来。认不出返回 None。

    **不做任何猜测**：没有标记、或者标记里没有合法 id，就是 None。调用方拿到
    None 时应该把这段正文原样当内容用（那是老会话，或者一份不经过 Artifact 的
    消息）—— 而不是报错。
    """
    if not isinstance(text, str) or MARK not in text:
        return None
    match = _PATTERN.search(text)
    return match.group("id") if match else None


def artifact_id_of(message: Any) -> str | None:
    """一条消息指向哪份 Artifact。**先看显式字段，再退回解析正文。**

    两条路都要有：新写的消息走第一条（可靠），老会话文件里的消息只能走第二条
    （那些消息的正文就是全文，本来也不需要 Artifact —— 见 `manager.py` 的兼容
    路径）。返回 None 表示"这条消息不引用任何 Artifact"。
    """
    if not isinstance(message, dict):
        return None
    explicit = message.get("artifact_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return parse(message.get("content"))


def is_reference(message: Any) -> bool:
    """这条 tool 消息是不是一句引用（而不是正文）。"""
    return artifact_id_of(message) is not None


__all__ = ["MARK", "MAX_SHOWN_ID", "artifact_id_of", "build", "is_reference", "parse",
           "shown_id"]
