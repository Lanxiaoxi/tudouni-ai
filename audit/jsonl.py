"""审计日志的落盘。

只追加、不改写，所以天然抗崩溃：最坏情况是最后一行只写了一半。这也是它跟
会话状态文件（每次重写整份、必须靠 os.replace 保原子）的根本区别 —— 两者的
写入模式不同，所以放在不同的目录、用不同的策略。

一个会话一个文件，名字就是会话 id。所以事件的 session_id 和文件名是冗余的，
但保留它让每一行自带身份，日志被复制拼接之后仍然能查。
"""

import json
from pathlib import Path
from typing import Any, Iterator

from agent_runtime.state.session import is_valid_session_id


class JsonlSink:
    """把事件逐行追加到 .jsonl 文件。

    它是可调用的，所以能直接当 Agent 的 on_event 传进去：

        agent = Agent(..., on_event=JsonlSink(PROJECT_DIR / ".tudouni" / "logs"))
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError(f"非法 session_id: {session_id!r}")
        return self.directory / f"{session_id}.jsonl"

    def __call__(self, record: dict[str, Any]) -> None:
        """追加一条事件。

        每条都独立开关文件，而不是长期握着一个句柄：这样任何一条写完就已经落盘，
        进程随时被杀都不会丢掉已经记录的事件（最坏只是最后一行不完整）。
        代价是每条多一次 open/close —— 对一个回合才几十条事件来说完全值得。
        """
        path = self._path(record["session_id"])
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read(self, session_id: str) -> Iterator[dict[str, Any]]:
        """读回事件。

        不是查看器，是对偶的读取 API（验证和将来做分析都要用）。它会跳过解析
        失败的行 —— 那些是进程在写入中途被杀留下的半截记录，属于设计内的情形，
        不该让整份日志不可读。

        和 JsonSessionStore 一样，**只有一个目录，不看旧位置**：那些日志是合并之前
        的产物，有意放弃读。
        """
        path = self._path(session_id)
        if not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
