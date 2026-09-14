"""一行 JSON ↔ 一个 dict。**唯一的编解码器。**

两端（子进程的读写、前端的读与写）都走这里，所以"一条消息怎么变成一行字节"
只写了一遍。任何一端自己 `json.dumps` 一遍，就是第二份事实 —— 而它漂掉的症状是
"偶发解析失败"，最难查。

## 四个不变式

1. **一行一条，`\\n` 结尾。** 用 `ensure_ascii=False`：中文按原样写，不要 `\\uXXXX`
   膨胀（一条 notice 能胖三倍，而且人肉读日志时完全没法看）。
2. **UTF-8 显式指定。** 子进程的 stdout 是**管道**不是终端，编码由环境决定 ——
   不显式管就会在某些机器上炸在第一次出中文的那一刻（Windows + cp936 实测过）。
3. **每条 flush。** 不 flush 的话事件会攒在 4~8KB 的缓冲区里，前端的"实时"变成
   "每 8KB 一跳"。
4. **坏行跳过并计数，不抛。** 一行坏掉可能是进程被杀留下的半截记录（和
   `JsonlSink.read` 同一种情形），不该让整条通道死掉 —— 但**必须能看见**
   （`skipped` 计数），否则就是静默失败。
"""

import json
from collections.abc import Iterator
from typing import Any

from agent_runtime.protocol import messages


class ProtocolError(RuntimeError):
    """协议层自己的错误（版本对不上、必填字段缺失）。落到前端就是一句人话。"""


def encode(message: dict[str, Any]) -> str:
    """一条消息 → 一行（**含结尾的 `\\n`**）。"""
    return json.dumps(message, ensure_ascii=False) + "\n"


def decode(line: str) -> dict[str, Any]:
    """一行 → 一条消息。空行返回 `{}`（调用方按"不认识"处理）。

    坏 JSON 抛 `json.JSONDecodeError`（不吞）—— 调用方决定是跳过还是停下，
    而"这一层不替上层做那个决定"是它保持纯函数的方式（`read_lines` 就是那个调用方，
    它选择跳过并计数）。
    """
    text = line.strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ProtocolError(f"一条协议消息必须是 JSON 对象，收到 {type(parsed).__name__}")
    return parsed


def check_version(message: dict[str, Any], *, direction: str) -> None:
    """核对信封版本。对不上就抛 —— 这是**唯一**该硬失败的地方。

    `direction` 只用来写清楚是"对方发的"还是"我们发的"：两端的排查路径不同。
    """
    got = message.get("v")
    if got == messages.VERSION:
        return
    raise ProtocolError(
        f"{direction}的消息版本对不上：收到 v={got!r}，本程序认的是 v={messages.VERSION}。"
        f"两端升级不同步 —— 升级之后重开。"
    )


def write_message(stream: Any, message: dict[str, Any]) -> None:
    """写一条并 flush。`stream` 是已经配好 UTF-8 的文本流（见 `transport_stdio`）。"""
    stream.write(encode(message))
    stream.flush()


class LineReader:
    """逐行读，跳过坏行，**并记着跳过了几条**。

    ## 为什么是生成器 + 一个能问的计数，而不是"读一个列表回来"

    这条通道的用途就是"一直开着等消息"，它不该攒着。

    ## 为什么坏行要计数

    跳过是对的（半截行可能是进程被杀留下的，和 `JsonlSink.read` 同一种情形），
    但**静默**跳过就不对了 —— 那正是这个项目反复反对的那种失败形态。计数攒着，
    由调用方（`transport_stdio` 的关闭路径）在 stderr 上说一句：

        [协议] 读入时跳过了 3 行（不是合法 JSON —— 手改过、或者被别的程序插了东西）

    真实情形里它更可能是"前端写坏了"，而那是需要看见的。
    """

    def __init__(self, stream: Any):
        self._stream = stream
        self.skipped = 0

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for line in self._stream:
            if not line.strip():
                continue
            try:
                yield decode(line)
            except (json.JSONDecodeError, ProtocolError, UnicodeDecodeError):
                self.skipped += 1
                continue

