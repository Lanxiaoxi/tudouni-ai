"""会话的持久化。

两条安全底线都是被实测咬过才写上的：

  1. **session_id 不能直接拼文件名。** 实测 session_id="../../evil" 会把状态文件
     写到目录外面去（`C:\\Users\\XPS\\repo\\evil.json`）。这个洞在 tools/builtin/filesystem.py
     里已经用 safe_path 堵过一次，不能在基础设施层又开回来。
  2. **写到一半被杀不能毁掉已经写好的东西。** 见下面「为什么是追加而不是整份重写」。

## 为什么是追加而不是整份重写

这一版之前，`save()` 是"`json.dumps` 整份会话 → 写临时文件 → `os.replace`"。
它在**单个回合**里是对的，但和 Agent 那个检查点频率放在一起就成了一个二次项：

    Agent 每走一步都落一次盘（agents/agent.py 里那个 ★），而每一步都会让历史变长
    → 第 i 步写出去的是"前 i 步的全部"，合计 **O(步数²)**

实测（scripts/perf_selfcost.py，80 步、每步回读 256KB 的文件）：
**863MB 的落盘量**，而最终会话只有 21MB —— 放大 **41 倍**，
占那一轮"程序自身耗时"的 **92%**（关掉落盘，自身开销降 18 倍）。
而 `cProfile` 显示 `save()` 一个函数吃掉了全场 90.7% 的 CPU。

所以改成**只追加**：一份会话一个 `.jsonl`，每条记录一行，
`save()` 只写"上次之后新增的那一段"。第 i 步写出去的是 O(1)，二次项消失。

## 三条记录，以及为什么是三条

    {"t":"head","version":1,"session_id":"s"}          整个文件只有第一行
    {"t":"meta","m":{...}}                              metadata 的**全量**快照
    {"t":"msg", "m":{...}}                             一条消息

**为什么 messages 可以只记增量**：全项目改 `messages` 只有 `agents/agent.py` 里
四处，全是 `.append()` —— 没有回改、没有删除、没有重排。所以"上次写了多少条"
是一个单调前进的水位，`messages[水位:]` 就是新增的那一段。

**为什么 metadata 要记全量、而且是每次落盘都记一条**：它不是只追加的 ——
`state/model.py`（换模型 / 改思考）、`tools/builtin/skills.py`、`tools/builtin/todo.py`
一共五处会原地改写它的顶层键。全量快照让"最后一次 meta 记录说了算"成为**唯一**
的重放规则，于是这里不需要"跟上次比一比变没变"的逻辑。

那个比较看着更省字节，但它是一个**会静默出错**的优化：比较基准必须是一份缓存，
而缓存一旦和实际写下去的东西分家（比如哪天有人改成原地改 `metadata` 里的嵌套值），
症状是"改完之后重开会话又变回去了"——没有任何异常，只是丢了一次改动。
一条 meta 记录只有几百字节到几 KB，而一次落盘的消息增量动辄几百 KB，
省它换一个静默错，不划算。

**重放规则因此只有三条**：head 取第一条、meta 取最后一条、msg 全部按顺序累加。

## 崩溃安全：比整份重写更强，而不是更弱

原子替换保证的是"目标文件要么是旧的完整版、要么是新的完整版"。追加式下这条
换成了另一条：**已经落盘的行照样读得出来，最坏只丢掉最后一条没写完的记录**
（`_replay` 跳过解析失败的行，和 `audit/jsonl.py` 的 `read()` 是同一条规矩）。

两边的差别只在"丢多少"：整份重写丢的是**整个检查点**（回到上一步的开头），
追加丢的是**最后一条消息**。而且追加不需要临时文件、不需要 `os.replace`
—— 少了两个可能失败的步骤。

## 为什么不再读旧的 `.json`

上一版把会话存成 `sessions/<id>.json`（整份快照）。这一版**有意不再读它**，
理由和当初放弃 `.sessions/` / `.logs/` 那两个旧位置**不同**，但结论一样：
那份格式是"同一个文件里既有元数据又有全部消息"，而新格式是流式的，
要兼容就得把两种形状都塞进 `_replay`，让这份代码永远背着一个只有历史数据
才走得到的第二分支 —— 而它服务的是一批只读一次、之后就该被删掉的数据。

**代价说清楚**：`--session <老 id>` 不会再接上那次对话，而是**新建一个同名会话**
（`exists()` 看的是 `.jsonl`，而它不存在）。老 `.json` 文件不会被删、也不会被读，
就那样留在磁盘上；想留着就手动改个后缀，想清掉就删掉整个目录。
"""

import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from .session import Session, is_valid_session_id


STATE_VERSION = 1

# 会话文件的扩展名。**它必须是 `.jsonl` 而不是 `.json`**：文件内容是一行一条记录，
# 而 `.json` 会让任何一个编辑器/工具以为"这是一份 JSON 文档"，然后读失败。
# 同时它也是"哪些文件是会话"的判据（`list_ids` 按它 glob）。
SUFFIX = ".jsonl"

# 记录的种类。见模块 docstring 里那三条重放规则。
HEAD = "head"
META = "meta"
MSG = "msg"


class _Replayed(NamedTuple):
    """一份 `.jsonl` 读出来的全部事实。"""

    head: dict[str, Any]        # 第一行那条 head（原样，用来读 version）
    messages: list[dict[str, Any]]
    metadata: dict[str, Any]    # 最后一条 meta 记录说的（没有就是空 dict）


class JsonSessionStore:
    """把一个 Session 存成一个**只追加**的 JSONL 文件。

    先不上 SQLite：单进程、按 id 存取、文件不大，JSON 行完全够用。等到出现并发写、
    需要按内容查询、或者单文件大到几百 MB，再换不迟。
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        # session_id -> 已经写下去多少条消息。**它是一份缓存，不是第二份事实。**
        #
        # 唯一的权威始终是文件自己：`_watermark` 在缓存里没有这个会话时会去读一遍
        # 文件、把水位重新数出来（换进程、换 store 实例之后就是这样）。所以
        # "缓存和文件分家"这件事不会静默发生 —— 它只可能表现为多读一次盘。
        #
        # 为什么不把水位存进 session.metadata：那是**会话语义**的一部分，会被落盘、
        # 会被发给模型（`session_notes` 读的就是 metadata），而"我已经写到哪儿了"
        # 是存储层自己的记账，两者混在一起就是 `test_step_count_is_derived` 那条
        # 反对的东西（存成字段就有了两份，早晚不一致）。
        self._watermark: dict[str, int] = {}

    # -- 路径 ------------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        """一个 id 对应哪个文件。**这是那件事唯一的说法** —— 别处要拼路径都走这里。

        它同时兜住 id 的合法性校验：`session_id` 会被拼进文件名，实测
        "../../evil" 能把文件写到目录外面去。
        """
        if not is_valid_session_id(session_id):
            raise ValueError(f"非法 session_id: {session_id!r}")
        return self.directory / f"{session_id}{SUFFIX}"

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def list_ids(self) -> list[str]:
        """列出已保存的会话 id。

        按 id 排序 —— 自动分配的 id 是时间戳，所以这个顺序正好也是时间顺序。
        （真正的排序判据是 `metadata["created_at"]`，见 `composition.session_summaries`；
        这里只负责给出一份确定的、稳定的清单。）
        """
        return sorted(p.stem for p in self.directory.glob(f"*{SUFFIX}"))

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

    # -- 写 --------------------------------------------------------------------

    def save(self, session: Session) -> None:
        """把"上次之后新增的那一段"追加到文件末尾。签名正好匹配 `on_checkpoint`。

        **只追加，永不重写**：已经写在文件里的字节一个都不动。这是二次项消失的原因，
        也是崩溃安全的来源（见模块 docstring）。
        """
        path = self._path(session.session_id)          # 先过 id 校验，早失败
        watermark = self._watermark_of(session.session_id)

        lines: list[str] = []
        if watermark < 0:
            # 文件还不存在 —— 这一次要把它建起来。head 必须是第一行，所以它和
            # "新建"是同一件事，不可能分开发生。
            lines.append(_dump({"t": HEAD, "version": STATE_VERSION,
                                "session_id": session.session_id}))
            watermark = 0

        # 水位比实际条数还大意味着 messages 被**缩短**了 —— 而它是只追加的
        # （见模块 docstring）。静默按空增量处理的话，会话文件会停在一个
        # "看起来正常、其实少了几条"的状态上，而那种错误没有任何症状。
        if len(session.messages) < watermark:
            raise ValueError(
                f"会话 {session.session_id!r} 的消息从 {watermark} 条变成了 "
                f"{len(session.messages)} 条；这份存储只支持追加，不支持删改。"
            )

        # metadata 每次落盘都记一条全量快照 —— 理由见模块 docstring
        # （"最后一次 meta 记录说了算"是唯一的规则，所以这里不需要比较逻辑）。
        lines.append(_dump({"t": META, "m": session.metadata}))
        lines.extend(_dump({"t": MSG, "m": message})
                     for message in session.messages[watermark:])

        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("".join(lines))
        except BaseException:
            # 写失败（磁盘满、写到一半被杀、Ctrl-C 落在这一刻……）→
            # **把缓存的水位丢掉**，下一次 save 去文件里重新数一遍。
            #
            # 留着它的后果很具体：下一次 save 会从**旧**水位重放，把这一次已经
            # 真正写下去的那几条**重复追加**一遍 —— 而重放对重复的消息没有任何
            # 去重，于是历史里真的会出现两条一样的消息，看起来像模型自己重复了。
            # 重新数一遍拿到的是"文件里真正有的那些"，重放才是对的。
            #
            # 捕 `BaseException` 而不是 `Exception`：`KeyboardInterrupt` 正好可能
            # 落在这一句上，而"被打断"和"写失败"对这份缓存是同一件事。
            self._watermark.pop(session.session_id, None)
            raise

        self._watermark[session.session_id] = len(session.messages)

    def _watermark_of(self, session_id: str) -> int:
        """这个会话已经写下去多少条消息。`-1` 表示文件还不存在。

        缓存里有就直接用；没有就去文件里数一遍。**数一遍只发生在"这个 store 实例
        第一次见到这个会话"的时候** —— 换进程、换 store 实例之后就那一次。
        """
        path = self._path(session_id)

        # 每次 save 都 stat 一下（微秒级）。**这个目录是用户可见的**，会话文件可能
        # 被人从中间删掉，而 `open("a")` 会把一个**没有 head 的文件**凭空建出来
        # —— 那种文件之后谁都读不了（`_replay` 会说"不是一份会话文件"），
        # 而症状是"这个会话突然打不开了"，与"什么时候删的"毫无关系。
        if not path.exists():
            self._watermark.pop(session_id, None)
            return -1

        if session_id in self._watermark:
            return self._watermark[session_id]

        count = len(self._replay(path).messages)
        self._watermark[session_id] = count
        return count

    # -- 读 --------------------------------------------------------------------

    def load(self, session_id: str) -> Session:
        replayed = self._replay(self._path(session_id))

        # 先看版本，再看字段。version 是**读取端唯一能据以决定"要不要信这份文件"
        # 的东西** —— 字段过滤能容忍"多了几个键"，但容忍不了"同一个键的含义变了"
        # （比如将来 messages 里出现一种新的内部消息）。那种变化要在写的时候就
        # bump STATE_VERSION，读的时候在这里拦下，而不是让它静默地当成新格式读进来。
        #
        # 缺 version 当作 1：head 是这个格式的第一行，没有它就不该有这份文件，
        # 但少一个字段不该让读取端崩，按最老的版本理解是安全的失败方向。
        version = replayed.head.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool):
            version = 1
        if version > STATE_VERSION:
            raise ValueError(
                f"会话 {session_id!r} 是更新版本写的（文件 version={version}，"
                f"本程序认识的最高版本是 {STATE_VERSION}）；升级程序再打开它，"
                f"否则可能读错格式。"
            )

        # 只挑自己认识的字段。会话文件躺在硬盘上，比代码活得久 —— 直接
        # Session(**raw) 的话，将来多一个字段就会让所有旧会话都打不开。
        known = {f.name for f in fields(Session)}
        raw = {
            "session_id": session_id,
            "messages": replayed.messages,
            "metadata": replayed.metadata,
        }
        return Session(**{k: v for k, v in raw.items() if k in known})

    def _replay(self, path: Path) -> _Replayed:
        """把一份 `.jsonl` 重放成事实。**三条规则，见模块 docstring。**

        跳过解析失败的行（进程在写入中途被杀留下的半截记录）—— 那是设计内的情形，
        不该让整份历史不可读。和 `audit/jsonl.py` 的 `read()` 是同一条规矩。
        """
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"会话文件不存在：{path}") from None

        head: dict[str, Any] | None = None
        messages: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}

        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                kind = record.get("t")
                if kind == HEAD:
                    # 第一条说了算：head 只在建文件时写一次，后面再出现 head
                    # 只可能是被人手工拼进去的，而"以第一条为准"和 file 的
                    # 生成顺序一致。
                    if head is None:
                        head = record
                elif kind == META:
                    # **最后一条说了算**：metadata 会被原地改写，所以重放必须
                    # 让最新的那条盖掉旧的。
                    value = record.get("m")
                    if isinstance(value, dict):
                        metadata = value
                elif kind == MSG:
                    value = record.get("m")
                    if isinstance(value, dict):
                        messages.append(value)
                # 不认识的 t 直接跳过：将来加的记录种类不该让旧程序读不了
                # —— 这和"只挑认识的 Session 字段"是同一条规矩。

        if head is None:
            raise ValueError(
                f"{path} 不是一份会话文件：里面没有 {HEAD} 记录"
                f"（那必须是第一行）。"
            )
        return _Replayed(head=head, messages=messages, metadata=metadata)

    def read(self, session_id: str) -> Iterator[dict[str, Any]]:
        """对偶的读取 API：逐条吐出记录本身，**不做重放**。

        它不是 `load()` 的另一个实现 —— `load()` 给的是"这份会话现在是什么"，
        而这个给的是"文件里都写了些什么"。验证、排障、将来的压缩工具要的是后者。
        """
        with self._path(session_id).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _dump(record: dict[str, Any]) -> str:
    """一条记录 → 一行。

    **紧凑分隔符 + `ensure_ascii=False`**，两个都不是随手写的：

      * 紧凑（`,` / `:` 不带空格）比默认省几个字节一行，而这里一行就是一条消息，
        量大了之后那几个字节是要乘的；
      * `ensure_ascii=False` 让中文按原样进文件。写成 `\\uXXXX` 的话一份中文会话
        会膨胀三倍 —— 而这个项目整个是中文的，那不是边角情况。
    """
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
