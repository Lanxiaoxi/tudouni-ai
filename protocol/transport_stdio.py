"""stdio 传输：子进程的 stdin/stdout 就是协议通道。

**这个模块只做一件事：把一对文本流变成一对方向。** 协议怎么解释、状态怎么推导、
人机通道怎么等回应 —— 都不在这里，那些在 `channels` 和 `state` 里。分开的理由很
实际：将来加 Web 时，`transport_http.py` 要和这个文件长得一样（同样的
`Transport` 形状），而"谁在对面"是唯一变化的东西。

## 三件必须在**开工之前**做掉的事

它们都写在 `open_stdio` 里，因为漏掉任何一件的症状是同一种："跑起来了，但时序全乱"。
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from agent_runtime.protocol import codec


@dataclass
class StdioTransport:
    """一对文本流 + 两个方向。

    它**不拥有**进程的生命周期（那是启动方的事），只拥有"怎么读写"这件事。
    """

    reader: TextIO
    writer: TextIO
    # 读入时跳过的坏行数。由 `close()` 说出来 —— 静默跳过是不行的（见 codec.LineReader）。
    _lines: codec.LineReader = field(default=None, repr=False)  # type: ignore[assignment]

    def recv(self):
        """逐条读入的消息（生成器）。坏行跳过并计数。"""
        return self._lines

    def send(self, message: dict[str, Any]) -> None:
        codec.write_message(self.writer, message)

    @property
    def skipped_lines(self) -> int:
        return self._lines.skipped

    def close(self) -> None:
        """收尾。**只报账，不关流** —— 谁开的谁关。"""
        if self.skipped_lines:
            print(
                f"[协议] 读入时跳过 {self.skipped_lines} 行（不是合法 JSON —— "
                f"手改过、或者被别的程序往这个管道里插了东西）",
                file=sys.stderr,
            )


def open_stdio() -> StdioTransport:
    """把当前进程的 stdin/stdout 变成协议通道。

    **三个细节，每一个都是踩过才知道的：**

    1. **`reconfigure(encoding="utf-8", newline="\\n")`。** 现有 CLI 打的全是 ASCII
       （`cli.py` 那段解释了为什么），所以它从来没碰到过这个问题；协议里第一条就带
       中文（`notice` 的文字），而 Python 从管道写非 ASCII 时用的是**环境决定的
       编码** —— 不显式管就会在某些机器上炸在第一次出中文的那一刻。
       `newline="\\n"` 同理：Windows 上默认写成 `\\r\\n`，而按 `\\n` 切行的前端会得到
       一堆尾部带 `\\r` 的行。
    2. **每条 flush**（在 `codec.write_message` 里）。stdout 接管道时 Python 用块缓冲
       （4~8KB），不 flush 的话事件会攒着，"实时"变成"每 8KB 一跳"。
    3. **stdin 也用 UTF-8 解。** 它和写端是两件独立的事，而默认编码同样是环境决定的。

    启动方还会带 `-u`（无缓冲）作为双保险 —— 两条都做，是因为哪一条单独失守都表现成
    同一件难查的事："界面偶尔不刷新"。
    """
    # reconfigure 只在解释器自带的文本流上有；测试里传进来的是 StringIO，那就不管 ——
    # 它的编码本来就是确定的。
    for stream, name in ((sys.stdout, "stdout"), (sys.stdin, "stdin")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if name == "stdout":
            reconfigure(encoding="utf-8", newline="\n")
        else:
            reconfigure(encoding="utf-8")

    reader = codec.LineReader(sys.stdin)
    return StdioTransport(reader=sys.stdin, writer=sys.stdout, _lines=reader)


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """起子进程时要带的环境。

    `PYTHONIOENCODING` 是上面第 1 条的**双保险**：即使有人把 reconfigure 那行删了，
    解释器启动时也会按 UTF-8 开那三个流。父进程用得上它（见 `frontends/tui`）。
    """
    env = dict(os.environ if base is None else base)
    env["PYTHONIOENCODING"] = "utf-8"
    return env
