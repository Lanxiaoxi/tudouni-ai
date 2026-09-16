"""Artifact 的存放。

它只回答一个问题：**Artifact 放在哪、怎么取出来。** 它不知道 Context 的存在，
也不该知道 —— 所以这里不会出现 `store.add_to_context(...)` 这种 API。

    会话目录/
    ├── sessions/<id>.jsonl     历史（tool 消息里只有一句引用）
    └── artifacts/<id>/
        ├── manifest.json       这份会话的全部 Artifact 元数据
        └── refs/art_xxx.txt    正文（一份一个文件）

## 为什么正文在文件里、而不在会话文件里

会话文件是**只追加**的（见 `state/store.py`）：一条记录一行，落盘量是 O(新增)。
把动辄几万字符的正文塞进去，就等于每一轮都在往磁盘上抄同一份内容，而那份内容
本来只需要写一次。分开之后：

  * 会话文件里只有一句引用（几十字节），落盘量回到 O(1)；
  * 正文按内容寻址，**同一份内容写第二次是免费的**（见 `create`）。

## 为什么 manifest 整份重写

它是"这份会话有哪些 Artifact"的唯一说法，而 Artifact 的增删都不频繁（一次工具
调用一两个）。增量写要维护一份"写到哪儿了"的水位，而那份水位和文件分家时的症状
是"有些 Artifact 重开会话就没了"—— 没有异常、只是少了几条。几百 KB 的重写换掉
一整类静默错误，划算。

## 内容寻址，以及 id 为什么不是随机的

`artifact_id = art_<sha256(正文)[:12]>`。设计原则第 10 条要求 id 稳定，而随机的
`art_7f3a…` 每轮都不一样 —— 渲染出来的 prompt 于是一轮一个样，前缀缓存全废。
按内容定 id 让"同一份信息"天然拿到同一个名字，而且**跨进程、跨机器都对得上**。

代价说清楚：**id 相同 ⇒ 内容相同**，反过来不成立（不同来源的同一段内容会是同一个
id）。两份内容相同、来源不同的 Artifact 会各拿到一个 id（第二个带 `-2` 后缀，
见 `_disambiguate`）—— 它们**是两次不同的事件**，合并会让"这次读的是哪个文件"
这件事在历史里丢失。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from agent_runtime.context.models import Artifact, ArtifactSource

# 单份正文的文件名。**它由 id 推导，不存在 Artifact 里** —— 存了就有两份事实。
REFS_DIR = "refs"
MANIFEST_NAME = "manifest.json"

# id 里哈希取多少位。12 位十六进制 = 48 bit，一份会话里撞一次的概率可以忽略，
# 而它比全量 64 位短得多 —— id 每一轮都要出现在 prompt 里（tool 消息的引用），
# 而那是按 token 计费的位置。
HASH_CHARS = 12

# 内存里缓存几份正文。一次请求要按 representation 取正文（见 renderer），而
# 紧接着的下一轮往往要取同一批 —— 缓存让"读文件"这件事不变成每一步的固定开销。
# 只按**份数**限，不按字节：一份正文的大小由工具决定，用字节限会让大文件永远
# 命中不了缓存，而它恰恰是最贵的那一份。
CACHE_ENTRIES = 32


def new_artifact_id(content: str) -> str:
    """按正文算一个稳定的 id。见模块 docstring。"""
    return f"art_{_digest(content)[:HASH_CHARS]}"


def _digest(content: str) -> str:
    """正文的 sha256（十六进制）。

    `surrogatepass` 是必须的：工具结果里可能出现落单的代理字符（一段从别处抄来的
    文本、一个坏掉的编码），而默认的 `strict` 会在**算哈希**这一步抛
    UnicodeEncodeError —— 于是"保存一份工具结果"这件事在最不该失败的地方失败。
    """
    return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class Snippet:
    """一份 Artifact 按某个 representation 展开之后的那一段。

    `start_line` / `end_line` 是 1-based 的**行号区间**，而且是"这段文字在原始
    正文里占据的位置"——不是"我截了第几行到第几行"。两者在被截断的正文里不同，
    而 `read_file` 会返回整份文件，所以它这里总是一致的；留着这两个字段是为了
    在正文被别的工具截断过时仍然说得对。

    `truncated` 表示"这还不是全部"。**它必须一路传到渲染出来的文本里**：模型看到
    半份内容却以为看到了全部，是这一层唯一会静默出错的地方。
    """

    text: str
    start_line: int = 1
    end_line: int = 0
    truncated: bool = False
    total_lines: int = 0


class ArtifactStore:
    """一堆 Artifact 的落盘与读取。**一个会话一个目录。**

    它不做并发控制：Agent 那条路上只有主线程会写（工具执行虽然并发，但结果是
    回到主线程之后才交给 `ToolResultProcessor` 的，见 `agents/agent.py` 的
    `_run_batch`）。
    """

    def __init__(self, directory: str | Path, *, clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.clock = clock
        self._items: dict[str, Artifact] = {}
        # id -> 正文。见 CACHE_ENTRIES。
        self._cache: dict[str, str] = {}
        # **(正文哈希, 来源) -> 已经用过这个组合的那个 id**。
        #
        # 它是"同一个内容 + 同一个来源 ⇒ 同一个 Artifact"这条判据的**唯一**依据，
        # 而它必须能被 `load()` 重建出来（从 manifest 里）。第一版把这个集合只留给
        # 本进程新造的那些，于是"同一个文件读了两次"在**重开会话之后**会多出一份
        # 重复的 Artifact —— 而症状只是磁盘上多几个文件，看不出错。
        self._by_key: dict[tuple[str, str], str] = {}
        self._loaded = False

    # -- 路径 ------------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_NAME

    def _ref_path(self, artifact_id: str) -> Path:
        """一份正文落在哪。

        **这里不拼接 id 里的任何东西到路径上**（`_ref_name` 只留安全字符），
        理由和 `state/store.py` 里 `_path` 那段一模一样：id 会进文件名，实测
        `"../../evil"` 能把文件写到目录外面去。
        """
        return self.directory / REFS_DIR / f"{_ref_name(artifact_id)}.txt"

    # -- 读盘 / 落盘 ------------------------------------------------------------

    def load(self) -> list[str]:
        """从盘上恢复元数据。返回**被丢掉的那些 id**（正文不见了）。

        为什么"正文不见了"要返回而不是抛：会话文件比 Artifact 目录活得久是常态
        （用户可能只删了那个目录、或者某次落盘只写了一半）。这时候正确的行为是
        "把这个 Artifact 当成不存在"，让 History 里那句引用渲染成一句"已失效"
        —— 而不是让整个会话打不开。**但绝不能静默**：调用方要把这份名单报出来。
        """
        self._items = {}
        self._cache = {}
        self._by_key = {}
        self._loaded = True

        if not self.manifest_path.exists():
            return []

        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # manifest 坏了 = 这份会话的 Artifact 索引没了。**不当成致命错误**：
            # 历史还在（会话文件是另一份事实），丢的只是"正文还能不能取回来"。
            return []

        entries = raw.get("artifacts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return []

        missing: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            artifact = Artifact.from_json(entry)
            if not artifact.artifact_id:
                continue
            if not self._ref_path(artifact.artifact_id).exists():
                missing.append(artifact.artifact_id)
                continue
            self._items[artifact.artifact_id] = artifact
            # 去重表要能从盘上重建 —— 见 `_by_key` 那段。**可能撞车**（同一个
            # 内容+来源真的被存成了两份，比如手工拼出来的 manifest）：那就让
            # 先到的那一份赢，后面的仍然在 `_items` 里（它们各有自己的 id，
            # 谁也删不掉谁）。
            self._by_key.setdefault(
                (_content_digest(artifact.content_ref), _source_key(artifact.source)),
                artifact.artifact_id,
            )
        return missing

    def _save_manifest(self) -> None:
        """整份重写（原子替换）。见模块 docstring。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "artifacts": [a.to_json() for a in self._items.values()],
        }
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path)

    # -- 写 --------------------------------------------------------------------

    def create(
        self,
        content: str,
        *,
        type: str = "text",
        source: ArtifactSource | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        """把一份正文收进来，返回它的 Artifact。

        **先有正文、再有 Artifact**，所以 id 在写之前就定得下来（内容寻址）。
        落盘是"先写正文、再更新 manifest"：反过来会在崩溃时留下一条指向空气的
        索引，而那个状态在读取端要被当成"正文不见了"处理 —— 顺序对了就根本不会
        出现。
        """
        self._ensure_loaded()
        source = source or ArtifactSource()
        digest = _digest(content)
        # 去重表的键用**短哈希**（`HASH_CHARS` 位），它必须和 `_content_digest`
        # 从盘上读回来的那一截**一模一样长** —— 否则"本进程刚造的"和"上次会话
        # 留下的"会对不上键，而那个症状是"重启之后同一个文件再读一次会多出一份
        # 重复的 Artifact"（实测踩过）。48 bit 的碰撞概率在一份会话里可以忽略。
        key = (digest[:HASH_CHARS], _source_key(source))

        # 这个内容+来源**已经有过一份**了 ⇒ 把那一份还给调用方，什么都不写。
        #
        # 写在前面而不是"写完之后去重"是有意的：正文文件是**内容寻址**的
        # （文件名就是哈希），所以两份相同内容的文件本来就是同一个文件 ——
        # 重写一遍只会多一次 I/O，而它换来的"新的 created_at"没有人用得上。
        known = self._by_key.get(key)
        if known is not None and known in self._items:
            return self._items[known]

        artifact_id = f"art_{digest[:HASH_CHARS]}"
        if artifact_id in self._items:
            # 同一份正文、**不同的来源**（比如内容和另一个文件一模一样）。
            # 换一个 id 而不是复用：两条 metadata 说的不是同一件事（路径不同），
            # 而"这次读的是哪个文件"正是事后要查的。见模块 docstring 最后一段。
            artifact_id = self._disambiguate(artifact_id)

        path = self._ref_path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        artifact = Artifact(
            artifact_id=artifact_id,
            type=type,
            source=source,
            content_ref=str(path),
            metadata=dict(metadata or {}),
            created_at=self.clock(),
            chars=len(content),
        )
        self._items[artifact_id] = artifact
        self._cache[artifact_id] = content
        self._by_key[key] = artifact_id
        self._save_manifest()
        return artifact

    def _disambiguate(self, base: str) -> str:
        """同一个内容、不同来源：换一个不撞的 id。

        后缀从 `-2` 起（不是 `-1`）：`art_x` 和 `art_x-2` 摆在一起时，读的人一眼
        看得出后者是"第二次"，而 `-1` 会让人以为还有个 `art_x-0` 没显示出来。
        """
        n = 2
        while f"{base}-{n}" in self._items:
            n += 1
        return f"{base}-{n}"

    def delete(self, artifact_id: str) -> bool:
        """删掉一份 Artifact（正文文件 + 索引）。**不存在的 id 不算错。**

        预算那一层降到 `metadata` 还降不下去时走这里（见 `budget.py`）。所以
        它必须是幂等的：同一次降级可能连着调它两次，而第二次报错的后果是
        "一次超预算把整个回合炸掉"。
        """
        self._ensure_loaded()
        artifact = self._items.pop(artifact_id, None)
        self._cache.pop(artifact_id, None)
        if artifact is None:
            return False
        try:
            self._ref_path(artifact_id).unlink()
        except OSError:
            # 文件已经被人删了 / 权限不对 —— 索引已经摘掉，那就是"这份 Artifact
            # 不在 Context 里了"这个事实。**不抛**：这里的调用方在降级路径上，
            # 而它要做的事（把条目从 Context 里摘掉）已经做完了。
            pass
        self._save_manifest()
        return True

    # -- 读 --------------------------------------------------------------------

    def get(self, artifact_id: str) -> Artifact | None:
        """元数据。**不读正文。**"""
        self._ensure_loaded()
        return self._items.get(artifact_id)

    def all(self) -> list[Artifact]:
        """按创建顺序返回全部 Artifact。**不读正文。**"""
        self._ensure_loaded()
        return list(self._items.values())

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._items)

    def __contains__(self, artifact_id: object) -> bool:
        self._ensure_loaded()
        return artifact_id in self._items

    def content(self, artifact_id: str) -> str | None:
        """整份正文。取不到返回 None（**不抛**，理由见 `Snippet` 那段调用方）。"""
        self._ensure_loaded()
        if artifact_id in self._cache:
            return self._cache[artifact_id]
        artifact = self._items.get(artifact_id)
        if artifact is None:
            return None
        path = self._ref_path(artifact_id)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        self._remember(artifact_id, text)
        return text

    def lines(self, artifact_id: str) -> list[str] | None:
        """按行切开。`range` / `preview` 两档要用它。"""
        text = self.content(artifact_id)
        return None if text is None else _split_lines(text)

    def read(
        self,
        artifact_id: str,
        start: int | None = None,
        end: int | None = None,
        max_chars: int | None = None,
    ) -> Snippet | None:
        """按行区间取一段（1-based，闭区间）。取不到返回 None。

        `start` / `end` 缺省 = 全都要。区间越界不报错、按实际内容夹平：模型看的是
        渲染出来的文本，一个"行号超出"的错误对它毫无用处，而**说清楚实际给了哪些
        行**才有用（所以返回值里带着真实的 start_line/end_line）。

        ## `max_chars` 不是可有可无的

        行数是**降级的第一层**，但它对"一行特别长的正文"完全无效 —— 压缩过的
        JSON、一整份被 `''.join()` 拼出来日志、以及任何没有换行的输出，行数都是
        1。那种情况下"给 20 行"等于给全文，而降级循环于是一遍一遍选中同一条、
        一遍一遍降不动（实测：估算从 2016 **涨到** 2034，多出来的是表头）。

        所以字符预算是同一件事的第二道闸：**先按行夹，再按字符夹**，两者都生效
        才算真的降下来了。切断的位置标在 `Snippet.truncated` 上，渲染时那句话会
        一路传到模型面前。
        """
        lines = self.lines(artifact_id)
        if lines is None:
            return None
        total = len(lines)
        first = max(1, int(start)) if start else 1
        last = total if end is None else min(total, int(end))
        if last < first:
            # 空区间：给一段空文本，但行号仍然如实（调用方据此能看出"这一档没内容"）。
            return Snippet(text="", start_line=first, end_line=first - 1,
                           truncated=False, total_lines=total)
        return _clip(
            lines[first - 1:last], first, last, total,
            None if max_chars is None else max(1, int(max_chars)),
        )

    def preview(
        self, artifact_id: str, *, lines: int = 40, max_chars: int | None = None
    ) -> Snippet | None:
        """前 N 行（再按字符夹一次）。"""
        return self.read(artifact_id, start=1, end=max(1, int(lines)),
                         max_chars=max_chars)

    def metadata_of(self, artifact_id: str) -> dict[str, Any]:
        """一份 Artifact 的"元数据档"渲染结果：**没有正文，只有关于正文的事实。**

        它是降级的最后一档（再往下就是删掉），所以它必须仍然是有用的：路径、
        行数、大小、来源 —— 模型据此至少知道"有这么一份东西、它有多大"。
        """
        artifact = self.get(artifact_id)
        if artifact is None:
            return {}
        data: dict[str, Any] = {
            "id": artifact.artifact_id,
            "type": artifact.type,
            "chars": artifact.chars,
        }
        if artifact.source.tool:
            data["tool"] = artifact.source.tool
        if artifact.source.path:
            data["path"] = artifact.source.path
        if artifact.source.url:
            data["url"] = artifact.source.url
        data.update(artifact.metadata)
        return data

    def _remember(self, artifact_id: str, text: str) -> None:
        self._cache[artifact_id] = text
        while len(self._cache) > CACHE_ENTRIES:
            # 淘汰最早进来的那一份（FIFO 而不是 LRU）：这里只求"别每一步都读盘"，
            # 而 LRU 要求每次命中都挪一次顺序 —— 那点开销在每一步都要付。
            self._cache.pop(next(iter(self._cache)))

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()


def iter_refs(directory: str | Path) -> Iterator[Path]:
    """这个目录里所有的正文文件。给排障和清理用（`--audit` 之外的一条路）。"""
    return Path(directory, REFS_DIR).glob("*.txt")


def _ref_name(artifact_id: str) -> str:
    """id → 安全的文件名。**非白名单字符一律换掉**（见 `_ref_path`）。"""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in artifact_id)


def _content_digest(content_ref: str) -> str:
    """从 `content_ref` 里把哈希那一截取回来。**长度和 `new_artifact_id` 一致。**

    **不再算一次哈希**：id 就是哈希派生出来的（见 `create`），而 `load` 时重算
    意味着把每一份正文都读一遍 —— 会话一恢复就要读几百 MB。

    它必须和 `new_artifact_id` 用同一个长度（`HASH_CHARS`），否则去重表在
    "本进程造的"和"从盘上读回来的"之间对不上键 —— 而那个症状是"重启之后
    同一个文件再读一次会多出一份重复的 Artifact"。

    后缀（`-2` 那种）要剥掉：同一个内容、不同来源时 id 会带后缀，而哈希那一截
    没变，所以"这份内容见过没有"的判据必须看哈希本身。
    """
    name = Path(content_ref).stem
    body = name[4:] if name.startswith("art_") else name
    return body.split("-", 1)[0][:HASH_CHARS]


def _source_key(source: ArtifactSource) -> str:
    """"同一份来源"的判据。**只认工具和路径/URL**，不认 metadata ——
    metadata 里有时间戳、行号之类每次都不同的东西，把它们算进来等于取消去重。
    """
    return f"{source.tool}\x00{source.path}\x00{source.url}"


def _clip(
    lines: list[str], first: int, last: int, total: int, max_chars: int | None
) -> Snippet:
    """把一段行拼成文本，并在需要时按字符预算截断。**两件事一起算，因为它们互相影响。**

    切断之后 `end_line` 必须跟着改：模型是照着这个区间判断"我看到的是哪一部分"的，
    报一个比实际内容长的区间等于让它以为自己读到了更多。

    `max_chars = None` 表示不设字符上限（`full` 档和"用户明确要这一段"时就是它）。
    """
    kept: list[str] = []
    used = 0
    end = first - 1
    cut = False
    for offset, line in enumerate(lines):
        if max_chars is None:
            kept.append(line)
            used += len(line) + (1 if kept[1:] else 0)
            end = first + offset
            continue
        head = 1 if not kept else 0                     # 行之间那个换行
        if used + head + len(line) <= max_chars:
            kept.append(line)
            used += head + len(line)
            end = first + offset
            continue
        # 这一行放不下了。**分两种，必须分开**：
        #
        #   * 已经有内容了 ⇒ 到此为止（切断在行边界上，模型看到的是完整的行）；
        #   * 一行都还没放下（**第一行本身就超预算** —— 压缩过的 JSON、一整份
        #     拼出来的日志、任何没有换行的输出）⇒ 给这一行的头一段。
        #
        # 只处理前一种是最容易犯的错：那种正文的行数永远是 1，于是"降级"一遍
        # 一遍地返回全文，而档位确实变了 —— 从日志和档位上都看不出问题。
        if not kept:
            kept.append(line[: max(1, max_chars - head)])
            end = first + offset
        cut = True
        break

    text = "\n".join(kept)
    if cut and max_chars is not None and len(text) > max_chars:
        # 头一段本身也可能超出（多行的小碎片累加），再按总预算夹一次 —— 只在
        # 真的超了的时候动，否则会把正常的分行原样截断。
        text = _head_slice(text, max_chars)
    return Snippet(
        text=text,
        start_line=first,
        end_line=end,
        truncated=cut or first > 1 or last < total,
        total_lines=total,
    )


def _head_slice(text: str, limit: int) -> str:
    """取前 `limit` 个字符并留一个省略号。**只在这一层用**（`tools/text.py` 那个
    `truncate` 是"取头尾两段"，服务于另一个目的：那里模型的下一步是"用更精确的
    查询再来一次"，而这里是"省 token"）。"""
    return text[:limit] + "…"


def _split_lines(text: str) -> list[str]:
    """按 `\\n` 切，并**吃掉末尾那个空元素**。

    `"a\\nb\\n".split("\\n")` 得到 `["a","b",""]` —— 那个空串不是一行，而是"文件
    以换行结尾"这件事。把它当一行的话，`total_lines` 会永远比实际多一，而模型
    照着自己看到的行数去引用行号就会差一行。
    """
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


__all__ = ["CACHE_ENTRIES", "HASH_CHARS", "ArtifactStore", "Snippet", "new_artifact_id"]
