"""会话的持久化。

两条安全底线都是被实测咬过才写上的：

  1. **session_id 不能直接拼文件名。** 实测 session_id="../../evil" 会把状态文件
     写到目录外面去（`C:\\Users\\XPS\\repo\\evil.json`）。这个洞在 tools/builtin/filesystem.py
     里已经用 safe_path 堵过一次，不能在基础设施层又开回来。
  2. **写文件不是原子的。** 实测把 JSON 截断一半再 load 就是 JSONDecodeError。
     而状态持久化的意义恰恰是扛崩溃 —— 它不能在最需要它的那一刻毁掉自己的数据。
     所以先写临时文件，再用 os.replace 一把替换。
"""

import json
import os
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from .session import Session, is_valid_session_id


STATE_VERSION = 1


class JsonSessionStore:
    """把一个 Session 存成一个 JSON 文件。

    先不上 SQLite：单进程、按 id 存取、文件不大，JSON 完全够用。等到出现并发写、
    需要按内容查询、或者单文件大到几百 KB，再换不迟。

    **只有一个目录，没有旧位置的兼容读取。** 运行期数据从一个工作区根上的三个地方
    （`.sessions/`、`.logs/`、`.tudouni.json`）合并进 `.tudouni/` 时，那些旧数据是
    有意放弃读的 —— 留着一条"读不到就去看旧目录"的分支，等于让这份代码永远背着一次
    历史迁移，而它服务的是一批一次性数据。
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError(f"非法 session_id: {session_id!r}")
        return self.directory / f"{session_id}.json"

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def list_ids(self) -> list[str]:
        """列出已保存的会话 id。

        按 id 排序 —— 自动分配的 id 是时间戳，所以这个顺序正好也是时间顺序。
        """
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def new_session_id(self) -> str:
        """分配一个还没被占用的会话 id。

        用本地时间戳：既保证唯一，又让 --list 的排序就是时间顺序。
        同一秒内被调用两次时补 -2、-3，避免把已有会话覆盖掉 —— 分配 id 这件事
        唯一不能接受的结果就是撞名。
        """
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate, n = base, 1
        while self.exists(candidate):
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def save(self, session: Session) -> None:
        """落盘。签名正好匹配 Agent 需要的 on_checkpoint 回调。"""
        path = self._path(session.session_id)
        payload = {"version": STATE_VERSION, **asdict(session)}

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 原子替换：磁盘上任何时刻要么是旧的完整版，要么是新的完整版，
        # 不存在"写了一半"的形态。
        os.replace(tmp, path)

    def load(self, session_id: str) -> Session:
        raw = json.loads(self._path(session_id).read_text(encoding="utf-8"))

        # 先看版本，再看字段。载荷是 {"version": N, **asdict(session)}，而 version 是
        # **读取端唯一能据以决定"要不要信这份文件"的东西** —— 字段过滤能容忍"多了几个
        # 键"，但容忍不了"同一个键的含义变了"（比如将来 messages 里出现一种新的内部
        # 消息）。那种变化要在写的时候就 bump STATE_VERSION，读的时候在这里拦下，
        # 而不是让它静默地当成新格式读进来。
        #
        # 缺 version 的文件（本字段落地之前写的）当作 1 —— 那时就是这个格式。
        version = raw.get("version", 1)
        if version > STATE_VERSION:
            raise ValueError(
                f"会话 {session_id!r} 是更新版本写的（文件 version={version}，"
                f"本程序认识的最高版本是 {STATE_VERSION}）；升级程序再打开它，"
                f"否则可能读错格式。"
            )

        # 只挑自己认识的字段。会话文件躺在硬盘上，比代码活得久 —— 直接
        # Session(**raw) 的话，将来多一个字段就会让所有旧会话都打不开。
        known = {f.name for f in fields(Session)}
        return Session(**{k: v for k, v in raw.items() if k in known})
