"""审计事件。

这里只负责「把发生的事变成一条可序列化的记录」，不负责写到哪 —— 那是 sink 的事。

和 asker / on_checkpoint 是同一条原则：Agent 知道发生了什么，注入的实现决定
记到哪、什么格式。

每条事件都是纯 JSON 可序列化的。这是它能被逐行 append 到 jsonl 的前提 ——
一旦某个字段混进 datetime / Path / 异常对象，写入就会在运行时炸掉。
"""

from datetime import datetime
from typing import Any


def event(
    kind: str,
    *,
    session_id: str,
    run_id: str,
    step: int,
    **data: Any,
) -> dict[str, Any]:
    """构造一条事件记录。

    公共字段（时间、种类、会话、回合、步数）在这里统一加，事件特有的内容用
    关键字传进来。

    session_id 每行都带、而不是靠文件名推断：审计日志经常被复制、拼接、汇总，
    让每一行自带身份比省那几十个字节重要。
    """
    return {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "kind": kind,
        "session_id": session_id,
        "run_id": run_id,
        "step": step,
        **data,
    }
