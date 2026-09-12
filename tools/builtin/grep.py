"""在工作区里按正则搜文本。

**它和 shell 工具的分工，正是这个文件存在的理由。** `shell` 已经能跑
`Select-String` / `grep -r` 了，但那条路有两个无法回避的代价：

  1. 它是 HIGH 风险。默认策略（`auto_approve={LOW}`）下**每一次调用都要人工审批** ——
     模型想"搜一下全项目"得先把整条命令念给用户听、等一次批准。而「按正则搜文本」
     是个极其常规的只读动作，为它排队等审批才是真正的错配。
  2. 它的输出是一次性的终端文本，没有结构、无法限制条数、也无法保证落在工作区内。

所以这里把"搜文本"从"执行任意命令"里单独拎出来：路径走 `FileSystem.safe_path`，
和 read_file / list_files 是同一条边界，于是它可以安心地定 LOW 风险。

**它不筛任何文件。** 这一条是试出来的，写在这里免得日后被"优化"回去：

  - 「跳过点目录」不行。`.venv` 是第三方源码 —— 问"pytest 这个 fixture 怎么实现的"
    时答案就在里面。
  - 「按 `.gitignore` 跳过」也不行。`.gitignore` 回答的是"要不要进版本库"，不是
    "值不值得读"。这个项目自己的 `.gitignore` 注释就把判据写明了：「两者都是本机
    产生的数据，**不该进版本库**」。而排障时 `.tudouni/logs/` 和 `.tudouni/sessions/`
    恰恰是要搜的地方。

归根到底：**「值不值得搜」不是路径的属性，是这次问题的属性。** 同一个文件，这个问题
里是噪声，下个问题里就是答案。任何静态过滤表都只能猜，而它猜错的方式是报出"没匹配
到"—— 一个模型**无法与"真的不存在"区分开**的答案。

那怎么办？**排序 + 预算 + 说清楚**：

  - **排序**：非隐藏路径优先（见 `_order_key`）。这不是过滤 —— 名额够的时候隐藏路径
    里的命中照样出现，只是名额不够时它们让位。顺带的好处是字节预算也先花在项目自己
    的代码上（实测这个仓库：项目源码 0.7 MB，算上 .venv/.git 是 42 MB）。
  - **预算**：列出的文件数、每文件命中行数、输出总量、单文件字节、总字节，五道各自
    有理由（见各常量的注释）。
  - **说清楚**：凡是"没列出来"和"没检查"的，都在结果末尾说明白，包括还有几个路径里
    也有命中。这是让"没匹配到 ≠ 不存在"成立的地方。

唯一刻意排除的是**符号链接**：它可能指到工作区外面、也可能指成环 —— 那是"能不能安全
地走"，不是"值不值得搜"。

也不返回行内容以外的定位信息，也不替模型读正文：grep 负责回答"哪儿有"，"那儿是
什么"是 read_file 的事 —— 一次 grep 不该把几百 K 正文顺手塞进上下文。
"""

import re
from pathlib import Path

from ..text import truncate
from .filesystem import FileSystem


# 最多列出多少个有命中的文件。
#
# 50 是"能看见分布"的量级：一个词命中 30 个文件时，模型需要知道的是**这 30 个都是
# 谁**，而不是只看前 5 个然后以为只有 5 个。
MAX_FILES = 50

# 每个文件最多列多少条命中行。
#
# **刻意比 MAX_FILES 收得紧**，因为输出总量（MAX_OUTPUT_CHARS）是一份共享预算，两边
# 不可能同时宽，那就得选一边：**文件名比行内容值钱**。模型拿到路径之后，想看哪个文件
# 的内容直接 read_file；反过来只有行内容、不知道还有哪些文件命中，它就无从选择。
MAX_MATCHES_PER_FILE = 20

# 单行最多保留多少字符。压缩过的 JS/CSS 可以是几十万字符**一行**，整行塞回去等于把
# 一次 grep 变成一次 read_file。行号才是模型要的定位信息。
MAX_LINE_CHARS = 200

# 总输出上限。前面几个上限都是"每个文件"，文件一多总量仍然可能失控 —— 这里兜底。
# 超了取头尾（见 _truncate），而且结果里会标明省略了多少字符。
MAX_OUTPUT_CHARS = 16000

# 单个文件最多读多少字节。一个几百 MB 的日志文件会被 read_text 整个读进内存，
# 而它几乎必然是模型不想看的。
MAX_FILE_BYTES = 1_000_000

# **一次调用最多读多少字节** —— 五道上限里唯一按"成本"而不是按"条数"设的。
#
# 成本随字节走，不随文件数走：这个项目实测过，一次 read_file 返回 12524 字符占了整轮
# 成本的 86%。所以字节是这里最诚实的量。
#
# 100 MB 是这么定的：这个仓库整个工作区（含 .venv、.git）才 42 MB，项目源码 0.7 MB，
# 所以它在本机**永远不会触发** —— 这正是 backstop 该有的样子。按实测的
# 42 MB / 1.18 s 折算，100 MB 大约对应 2.8 秒读盘，所以它同时也是一个粗粒度的时间
# 上限（读盘速度是常量级），不必再加一个 timeout 旋钮。
MAX_TOTAL_BYTES = 100_000_000

# 模型能给 max_files 设的范围。上限存在的意义和 shell 的 timeout 一样：把最坏情况
# 钉死 —— 没有它，"文件数交给模型"就等于"允许一次调用把整个仓库读进来"。
MAX_MAX_FILES = 200


def _relative(target: Path, workspace: Path) -> str:
    """返回相对工作区的路径，**统一用 / 分隔**。

    不给模型看绝对路径：一是它长、二是它把本机目录结构带进了上下文（和 session_id
    不编码路径是同一条规矩）。正斜杠是为了让 Windows 和 POSIX 上的输出形状一致 ——
    模型随后要把这个路径喂给 read_file，而 safe_path 两种写法都认。
    """
    return target.relative_to(workspace).as_posix()


def _order_key(target: Path, root: Path) -> tuple[int, str]:
    """候选文件的排序键：**非隐藏路径优先**，其次按相对路径字典序。

    这是排序，不是过滤 —— 见模块 docstring。「隐藏」按**相对 root** 判断，而不是相对
    工作区：这样 `path=".venv"` 的搜索会把里面的文件当普通文件对待（你都指到它里面
    了，就是明说要搜它），而工作区本身恰好位于一个隐藏目录下时也不会全家被降级。

    字典序那一半是为了可复现：顺序不稳定的话，名额用完时截到哪几个文件就成了随机的，
    模型无从复现同一次搜索。
    """
    relative = target.relative_to(root)
    hidden = any(part.startswith(".") for part in relative.parts)
    return (1 if hidden else 0, relative.as_posix())


def _iter_matches(text: str, regex: re.Pattern[str]) -> list[tuple[int, str]]:
    """逐行找出命中，返回 (行号, 该行文本)。

    逐行而不是 `finditer` 整个文本：`finditer` 对多行模式（`a.*\\nb`）能跨行匹配，
    但那会让"这是第几行"变成一个说不清的问题，而行号正是这个工具的核心产出。
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(hits) >= MAX_MATCHES_PER_FILE:
            break
        if regex.search(line):
            hits.append((lineno, line))
    return hits


def _format_line(lineno: int, line: str) -> str:
    clipped = line[:MAX_LINE_CHARS]
    if len(line) > MAX_LINE_CHARS:
        clipped += "…"
    return f"{lineno}:{clipped}"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超长取头尾两段。

    和 shell.py 里那个同名函数同一个理由：关键信息常常压在最后。这里大部分命中是
    "稀疏"的，但一个 minified 文件、或者一份恰好匹配到很多行的日志，完全可能把配额
    用光 —— 只留开头会让模型看不到后面那些文件。

    实现搬去了 tools/text.py（fetch_web 是第三处要用它的地方），这里留门面：默认上限
    仍然是本工具自己的 MAX_OUTPUT_CHARS。
    """
    return truncate(text, limit)


def _human_bytes(n: int) -> str:
    """把字节数说成人话。

    默认值是 100 MB，但测试会传很小的值 —— 那时 `n // 1_000_000` 会说出"0 MB 上限"
    这么一句错话。
    """
    return f"{n // 1_000_000} MB" if n >= 1_000_000 else f"{n} 字节"


def _read_text(target: Path) -> tuple[str, int] | None:
    """读一个文件，返回 (正文, 字节数)；读不了就返回 None。

    - 二进制文件用 `errors="replace"` 读进来的是一堆 U+FFFD，正则会对着它白白跑一遍。
      这里直接按"能不能 utf-8 解码"判断，解不了就当二进制跳过。这不是完美的二进制
      探测（有些二进制恰好是合法 utf-8），但零依赖、行为可预测，挡住的是绝大多数。
    - `stat()` 也放在 try 里面：文件可能在上一步列目录之后、这一步读之前被删掉，那时
      OSError 会从它冒出来。这个函数的契约是"读不了就返回 None"，不是"偶尔会抛"。
    """
    try:
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        return target.read_text(encoding="utf-8"), size
    except (UnicodeDecodeError, OSError):
        return None


def grep(
    workspace: str,
    pattern: str,
    path: str = ".",
    include: str | None = None,
    ignore_case: bool = False,
    max_files: int = MAX_FILES,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> str:
    """在工作区的 path 下递归搜索 pattern，返回给模型的文本。

    **永远返回字符串，不抛异常** —— 和 shell.py 是同一条规矩。「没匹配到」是搜索的
    正常结果，不是工具故障：抛出去的话 agent.py 会把它记成工具故障，而模型真正需要
    的恰好是"这里确实没有"这个信息。

    路径安全由 `safe_path` 兜底：`path` 指到工作区外面会在这里被拦下来。正则的语法
    错误是**模型自己写错了**，也该由它读着错误信息自己改，所以连它一起返回而不是抛。

    `max_total_bytes` 只有测试会改（照 retry.py 里 `sleep` 那条先例）。它是 backstop，
    不是给模型调的旋钮，所以刻意不进 GrepArgs 的 schema。
    """
    fs = FileSystem(workspace)
    # 两个不同的基准，别混：root 是"从哪开始搜"，而报给模型的路径必须相对**工作区** ——
    # 模型会把它原样交给 read_file，read_file 是按工作区解析的。（这里曾经传错过 root，
    # 结果 path="tools" 时报出 "builtin.py"，read_file 直接 FileNotFoundError。）
    root = fs.safe_path(path)

    if not root.exists():
        return f"路径不存在：{path}"
    if not root.is_dir():
        return f"不是目录：{path}（这个工具只搜目录；要读单个文件用 read_file）"

    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        # 说清楚是**正则**写错了。否则模型会以为"没搜到"，然后去改一个本来没问题的
        # path 或 include，白绕一圈。
        return f"正则表达式无效：{pattern}\n{exc}"

    candidates = sorted(
        (p for p in root.rglob(include or "*") if p.is_file()),
        key=lambda p: _order_key(p, root),
    )

    blocks: list[str] = []
    matched = 0          # 有命中的文件总数 —— 不只是列出来的那些
    read_files = 0       # 真正读了正文的
    unreadable = 0       # 二进制，或超过 MAX_FILE_BYTES
    symlinks = 0         # 刻意不跟
    considered = 0       # 走到过第几个候选
    read_bytes = 0

    for target in candidates:
        if read_bytes >= max_total_bytes:
            break

        considered += 1

        if target.is_symlink():
            symlinks += 1
            continue

        loaded = _read_text(target)
        if loaded is None:
            unreadable += 1
            continue

        text, size = loaded
        read_files += 1
        read_bytes += size

        hits = _iter_matches(text, regex)
        if not hits:
            continue

        matched += 1
        if len(blocks) >= max_files:
            # 名额用完了，但**继续往下数** —— 结果末尾要说清"还有几个路径里也有命中"，
            # 那是让"没匹配到 ≠ 不存在"成立的地方。不再往 blocks 里塞就是了。
            continue

        header = f"{_relative(target, fs.workspace)} ({len(hits)} 处命中)"
        lines = [_format_line(lineno, line) for lineno, line in hits]
        if len(hits) >= MAX_MATCHES_PER_FILE:
            lines.append(f"…（此文件命中已到 {MAX_MATCHES_PER_FILE} 条上限）")
        blocks.append("\n".join([header, *lines]))

    if blocks:
        body = "\n".join(blocks)
    else:
        body = f"没有匹配：在 {path} 下找不到符合 {pattern} 的内容（读了 {read_files} 个文件）"

    notes: list[str] = []
    if matched > len(blocks):
        # **先说总数**，而不是只说"列了前 N 个"：输出本身还可能被 _truncate 再切一刀，
        # 所以"看得见几个"是不确定的，而"一共几个有命中"是确定的、也是模型真正需要的
        # 那个数 —— 它是"这次搜索问得太宽"的信号。
        notes.append(f"共 {matched} 个路径有命中，只挑列了最前面的 {len(blocks)} 个")
        notes.append("非隐藏路径排在前面，没列出来的更可能在隐藏目录或第三方目录里")
    if considered < len(candidates):
        notes.append(
            f"另有 {len(candidates) - considered} 个候选文件根本没被检查，"
            f"总读取上限是 {_human_bytes(max_total_bytes)}"
        )
    if unreadable:
        notes.append(
            f"另有 {unreadable} 个文件没读，它们是二进制或超过 {_human_bytes(MAX_FILE_BYTES)}"
        )
    if symlinks:
        notes.append(f"跳过 {symlinks} 个符号链接，不跟着走以避免目录循环和越界")
    if notes:
        body += "\n\n（" + "；".join(notes) + "）"

    return _truncate(body)
