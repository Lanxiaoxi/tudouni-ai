"""在工作区里按正则搜文本 —— 引擎是**随仓库带进来的 ripgrep**（tools/vendor/rg/）。

**它和 shell 工具的分工，正是这个文件存在的理由。** `shell` 已经能跑
`Select-String` / `rg` 了，但那条路有两个无法回避的代价：

  1. 它是 HIGH 风险。默认策略（`auto_approve={LOW}`）下**每一次调用都要人工审批** ——
     模型想"搜一下全项目"得先把整条命令念给用户听、等一次批准。而「按正则搜文本」
     是个极其常规的只读动作，为它排队等审批才是真正的错配。
  2. 它的输出是一次性的终端文本，没有结构、无法限制条数、也无法保证落在工作区内。

所以这里把"搜文本"从"执行任意命令"里单独拎出来：路径走 `FileSystem.safe_path`，
和 read_file / list_files 是同一条边界，于是它可以安心地定 LOW 风险。

## 为什么引擎从 Python 换成了 ripgrep

换之前那一版是逐文件 `rglob` + `read_text` + `re.search`。它的**设计**（不筛文件、
排序不是过滤、预算当 backstop、说清楚）都对，唯一的硬伤是慢：在本仓库上搜一个词实测
**3.6 秒**（暖缓存、96 MB 工作区），同一份交给这一版是 **0.45 秒** —— 而"一次搜索几秒"
的后果不是"慢一点"，是模型会**绕开搜索**：它宁可猜一个文件名直接 read_file，也不肯先
问"这个词在哪儿"，而那正好是提示词里"优先搜索定位"那条规矩的反面。

换的只是引擎，**上面那些设计一条没动，因为它们在 Python 这一侧**：

  - 排序、名额、行截断、给模型看的文本，全部留在这里（`_order_key` / `_format_line`
    / `_truncate`）。ripgrep 连输出顺序都不保证（默认是并行搜的），所以顺序必须由我们
    定，而不是交给 `--sort path` —— 那个开关能换来顺序，代价是把它打回单线程。
  - 每条命中的行号、行文本由 `--json` 交回来（`{"type":"match", …}`），我们据此自己
    排版，不解析它的终端输出。

**代价写在明处**：三条"说清楚"的注脚退化了 —— 符号链接数、二进制/超大文件数、
以及"字节上限之后还剩几个候选"。这不是疏忽，是这次换引擎付的账：ripgrep 的 `--json`
只在 summary 里报"搜了几个文件、几个有命中"，**不报它跳过了谁**。要报就得自己再走一遍
目录树，而那趟走路正是它帮我们省掉的东西。现在这一侧仍然保证"没匹配到 ≠ 不存在"的
核心那一半：**有命中的总数照报**（含没列出来的那些）、超时要说明、引擎报错要说明。

## 它不筛任何文件

这一条是试出来的，写在这里免得日后被"优化"回去：

  - 「跳过点目录」不行。`.venv` 是第三方源码 —— 问"pytest 这个 fixture 怎么实现的"
    时答案就在里面。
  - 「按 `.gitignore` 跳过」也不行。`.gitignore` 回答的是"要不要进版本库"，不是
    "值不值得读"。这个项目自己的 `.gitignore` 注释就把判据写明了：「两者都是本机
    产生的数据，**不该进版本库**」。而排障时 `.tudouni/logs/` 和 `.tudouni/sessions/`
    恰恰是要搜的地方。

归根到底：**「值不值得搜」不是路径的属性，是这次问题的属性。** 同一个文件，这个问题
里是噪声，下个问题里就是答案。任何静态过滤表都只能猜，而它猜错的方式是报出"没匹配
到"—— 一个模型**无法与"真的不存在"区分开**的答案。

ripgrep 的**默认行为恰好就是"筛"**（读 .gitignore、跳隐藏、跳二进制），所以下面 argv
里那串 `--no-ignore*` / `--hidden` 不是可选的美化，是这一条设计的实现。少一个
`--no-ignore-vcs`，被 `.gitignore` 掉的 `ignored.py` 就静默消失了。

那怎么办？**排序 + 预算 + 说清楚**：

  - **排序**：非隐藏路径优先（见 `_order_key`）。这不是过滤 —— 名额够的时候隐藏路径
    里的命中照样出现，只是名额不够时它们让位。顺带的好处是字节预算也先花在项目自己
    的代码上（实测这个仓库：项目源码 0.7 MB，算上 .venv/.git 是 42 MB）。
  - **预算**：列出的文件数、每文件命中行数、单行字符数、输出总量、总的墙钟时间，五道
    各自有理由（见各常量的注释）。
  - **说清楚**：凡是"没列出来"和"没搜完"的，都在结果末尾说明白，包括还有几个路径里
    也有命中。这是让"没匹配到 ≠ 不存在"成立的地方。

唯一刻意排除的还是**符号链接**：它可能指到工作区外面、也可能指成环 —— 那是"能不能安全
地走"，不是"值不值得搜"。ripgrep 默认也不跟（`-L` 才跟），这一点上两边一致。

也不返回行内容以外的定位信息，也不替模型读正文：grep 负责回答"哪儿有"，"那儿是
什么"是 read_file 的事 —— 一次 grep 不该把几百 K 正文顺手塞进上下文。

## 正则方言变了，而模型必须知道

ripgrep 默认是 Rust regex：**没有反向引用（`\\1`）和环视（`(?=...)`）**，但有
`\\p{Greek}` 这类 Unicode 类和 `(?i)` 内联旗标；Python `re` 有前两者、没有后者。所以
`GrepArgs.pattern` 的描述里写明了方言 —— 模型写了个 `(?=x)` 会拿到一句
"regex parse error"，而它得知道该改的是**方言**，不是 path。

**刻意不给 `--pcre2`。** 那会把"这段正则是哪种方言"变成第二个变量，而它是模型看不见
的状态：同一个 pattern 在两次调用里可能一个成一个败。要它就等于要一整类"说不清为什么
失败"的失败。

## 安全：三件必须钉死的事（都实测过）

  1. **pattern 只走 `-e`，绝不作为位置参数。** 模型能控制 pattern，而 ripgrep 有
     `--pre=COMMAND`（实测它会真的去 spawn 那个命令）和 `--hostname-bin` 这类旗标。
     把 pattern 拼进 argv 的位置参数位，模型写一个 `--pre=...` 就等于拿到一次**绕过
     shell 审批**的命令执行。走 `-e` 之后它永远只是一个正则字符串。
  2. **`--no-config`。** ripgrep 会读 `RIPGREP_CONFIG_PATH` 指向的配置文件（实测：往里
     写 `--files-with-matches`，输出形状立刻就变了），配置文件里同样能塞 `--pre`。
     `--no-config` 是关掉它的那个开关。
  3. **不给模型任何旗标。** `GrepArgs` 只有 pattern / path / include / ignore_case /
     max_files，argv 的其余部分由 `_argv` 写死。`path` 仍然先过 `safe_path`，并且作为
     位置参数跟在 `--` 后面 —— 工作区里一个叫 `-foo` 的目录不该被当成旗标。
"""

import json
import platform
import subprocess
from pathlib import Path
from typing import Any

from pydantic import Field

from agent_runtime import paths

from ..text import truncate
from ..tool import ToolArgs
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

# 模型能给 max_files 设的范围。上限存在的意义和 shell 的 timeout 一样：把最坏情况
# 钉死 —— 没有它，"文件数交给模型"就等于"允许一次调用把整个仓库读进来"。
MAX_MAX_FILES = 200

# 一次调用的墙钟上限。**它是原来那个 MAX_TOTAL_BYTES（100 MB 字节预算）的替身。**
#
# 字节预算当初是按"成本随字节走"定的（旧实现要把每个文件读进 Python，实测 42 MB /
# 1.18 s）。现在读盘在子进程里、走 SIMD 和 mmap，本仓库（96 MB）是 0.45 s —— 字节不再
# 是诚实的量，**时间**才是。10 秒按这个基线约等于扫过 2 GB 文本，本机永远碰不到，
# 这正是 backstop 该有的样子。
#
# 它撞上时的行为必须是**响的**：超时就什么结果都不返回、并说明是超时（见 grep()）。
# 悄悄返回半份结果等于又造了一个"没匹配到 ≠ 不存在"的坑。和 shell.py 那个超时同一条
# 规矩：能自己决定要不要等更久的，是调用方，不是工具。
TIMEOUT_SECONDS = 10

# 随仓库带的 ripgrep 住在哪。`tools/vendor/rg/<triple>/rg[.exe]`，一个平台一份
# （`tools/vendor/rg/README.md` 记着来源、许可和升级办法）。
#
# **走 `paths`，不自己算 `__file__`。** 原来那句 `parents[1] / "vendor" / "rg"` 在源码
# 目录里是对的，而冻结成可执行文件之后 `__file__` 指向别处、数据文件在 `_MEIPASS` 下 ——
# 症状特别隐蔽：`rg_binary()` 返回 None，于是 `grep` **不注册**，模型看不到它，改去猜
# 文件名然后 `read_file`（正好是提示词里"优先搜索定位"的反面）。而这一切只表现为启动时
# 一行提示。
_VENDOR_DIR = paths.package_dir() / "tools" / "vendor" / "rg"

# 平台 → ripgrep 官方 release 的 target triple。
#
# **只支持 x86_64 的 Windows 和 Linux**，这是刻意的：这两个是本项目真正在跑的机器，
# 而每一份二进制都是 4~5 MB 进 git 的账（diff 里只有一行 "Bin …"，review 看不见内容）。
# 多支持一个平台要付的是这份账，所以"先带上"不是个好理由 —— 真需要时是一条命令
# （见下面 supported_triples 那段）。
#
# **Linux 用 musl 那一份**：它是静态链接的，在任何 glibc 发行版上照样跑，所以一个
# triple 就覆盖整片发行版 —— 官方对 x86_64 也只发 musl 这一种 Linux 构建。
#
# 这张表就是"支持哪些平台"的唯一事实，两件事都从它推导：
#   * 没列进来的平台**不注册这个工具**（见 create_tool_registry），而不是给它一个
#     跑到一半才失败的 rg；
#   * `scripts/fetch_rg.py --all` 拉的就是它的取值 —— 所以仓库里该有哪几份、脚本会拉
#     哪几份、代码认哪几个平台，永远是同一份事实。
_TRIPLES = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
}


def supported_triples() -> tuple[str, ...]:
    """本项目支持的平台（= `_TRIPLES` 的取值），排序后返回。

    公开出来是因为**用它定义 `--all` 的是另一个文件**（`scripts/fetch_rg.py`）。让脚本
    自己列一遍平台就等于"支持哪些平台"有了第二份事实，而它漂移的症状很难查：脚本拉了
    一份代码不认的二进制，看起来一切正常，表现却还是"grep 没注册"。

    加一个平台是**两步**，两步都必要：跑 `fetch_rg.py --triple <triple>` 把二进制放进
    仓库，再往上面的 `_TRIPLES` 加一行。只做第一步的话，那台机器上依然不注册 ——
    "支持"是代码的决定，不是"文件恰好在那儿"的决定。
    """
    return tuple(sorted(_TRIPLES.values()))


class GrepArgs(ToolArgs):
    """grep 的参数。

    字段少是刻意的：**每一个字段都是一条模型能控制的 argv**，而 argv 里能塞的东西远
    不止搜索（`--pre` 那种）。所以这里只留"搜什么、在哪儿搜、怎么筛"。

    pattern 的描述里点了方言：模型写 `(?=x)` 或 `\\1` 会拿到 regex parse error，而它
    得知道该改的是方言而不是别的（见模块 docstring 那段）。
    """

    pattern: str = Field(
        min_length=1,
        description="正则表达式（Rust regex 方言：不支持反向引用 \\1 和环视 (?=...)，"
                    "支持 \\p{...} 和内联 (?i)）",
    )
    path: str = Field(
        default=".",
        min_length=1,
        description="从哪个目录开始搜（相对于工作区），默认为工作区根目录",
    )
    include: str = Field(
        default="",
        description="只搜文件名匹配这个 glob 的文件，例如 *.py；按文件名匹配、任意深度"
                    "都算。留空表示不限",
    )
    ignore_case: bool = Field(default=False, description="是否忽略大小写")
    max_files: int = Field(
        default=MAX_FILES,
        ge=1,
        le=MAX_MAX_FILES,
        description=f"最多列出多少个有命中的文件（默认 {MAX_FILES}）。"
                    f"命中文件更多时只列最前面的，但总数会告诉你有几个",
    )


def host_triple() -> str | None:
    """这台机器对应哪个官方构建；没有对应构建就返回 None。

    `platform.machine()` 在四个平台上分别报 AMD64 / ARM64（Windows）、x86_64 /
    aarch64（Linux）、x86_64 / arm64（macOS）—— 大小写和拼法都不统一，所以这里做一次
    显式翻译，而不是猜。（列出来是为了说明为什么必须翻译，不是支持的清单：`_TRIPLES`
    现在只用了 Windows / Linux 的 x86_64 那两格。）
    """
    return _TRIPLES.get((platform.system(), platform.machine()))


def vendor_dir() -> Path:
    """随仓库带的 ripgrep 住在哪个目录（`tools/vendor/rg`）。

    公开出来是因为**写它的不只有这个模块**：`scripts/fetch_rg.py` 也要往这个目录里放
    文件。它自己拼一遍路径就成了第二份事实 —— 而且那种漂移的症状最坏：脚本往 A 处拉，
    工具去 B 处找，表现是"明明拉成功了却还是说找不到引擎"。
    """
    return _VENDOR_DIR


def rg_binary() -> Path | None:
    """内置 ripgrep 的可执行文件路径；这台机器上没有就返回 None。

    None 在这里是**"这套环境不完整"**，不是"这次搜索没找到东西"。两件事的处置完全
    不同：前者由 create_tool_registry 决定不注册这个工具（模型看不到就不会白调一次，
    和缺 TAVILY_API_KEY 不注册 web_search 同一条路），而后者是搜索的正常结果。
    """
    triple = host_triple()
    if triple is None:
        return None
    name = "rg.exe" if platform.system() == "Windows" else "rg"
    candidate = _VENDOR_DIR / triple / name
    return candidate if candidate.is_file() else None


def _argv(
    binary: Path,
    root: Path,
    pattern: str,
    include: str,
    ignore_case: bool,
) -> list[str]:
    """拼出这一次搜索的完整 argv。

    **这里是唯一允许出现旗标的地方**（模块 docstring 第三条）。模型给的东西只落在
    四个位置上：`-e` 的值、`-g` 的值、`-i` 的有无、以及 `--` 之后那个路径。
    """
    argv = [
        str(binary),
        # 不读 RIPGREP_CONFIG_PATH 指向的配置文件。少了它，一个外部配置文件就能改掉
        # 这次调用的形状（甚至塞进 --pre）。
        "--no-config",
        # 找回"不筛任何文件"的语义（模块 docstring 那段）。默认的 ripgrep 会读
        # .gitignore、跳隐藏目录 —— 对这个工具来说那全是静默的漏搜。
        "--hidden",
        "--no-ignore",
        "--no-ignore-vcs",
        "--no-ignore-parent",
        "--no-ignore-global",
        # 结构化输出：每条命中带路径、行号、整行文本，末尾一条 summary 带总计。
        # 我们不解析它的终端排版，那一层它随时可以改。
        "--json",
        # 每个文件最多要 21 条命中：多要一条是为了分清"正好 20 条"和"还有更多" ——
        # 只要 20 条的话，两种情况的输出一模一样。它同时让引擎在这个文件上早点收工。
        "--max-count",
        str(MAX_MATCHES_PER_FILE + 1),
        # pattern 走 -e：见模块 docstring 第一条，这是那段存在的全部理由。
        "-e",
        pattern,
    ]
    if include:
        argv += ["-g", include]
    if ignore_case:
        argv.append("-i")
    # `--` 之后一律是路径。工作区里真有一个叫 `-foo` 的目录时，模型指到它也能搜。
    argv += ["--", str(root)]
    return argv


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
    模型无从复现同一次搜索。**这一条在换引擎之后更要紧了** —— ripgrep 默认是并行搜的，
    它交回来的顺序本来就不保证，所以"顺序"这件事只剩这一个来源。
    """
    relative = target.relative_to(root)
    hidden = any(part.startswith(".") for part in relative.parts)
    return (1 if hidden else 0, relative.as_posix())


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


def _remember(
    buffered: dict[str, tuple[tuple[int, str], list[tuple[int, str]]]],
    rel: str,
    key: tuple[int, str],
    max_files: int,
) -> None:
    """把一个有命中的文件放进"要列出来"的那份名单，超额时**挤掉排最后的那个**。

    为什么是这套结构而不是"先全收下、最后排序取前 N"：命中一万个文件时，全收下就是
    几十 MB 的内存和一次全排序，而这个工具的预算本来就是按"只给模型看 N 个"定的。
    边收边按 `_order_key` 留最好的 N 个，内存与命中数无关，结果与**到达顺序无关** ——
    后半句是重点：ripgrep 的到达顺序不保证，这里必须自己保证可复现。

    被挤掉的文件不影响计数：总数来自引擎的 summary（searches_with_match），不是数
    这份名单。
    """
    buffered[rel] = (key, [])
    if len(buffered) > max_files:
        # 键是 (是否隐藏, 相对路径)，唯一且可比 —— 所以"最差的"永远只有一个。
        del buffered[max(buffered, key=lambda name: buffered[name][0])]


def _collect(
    stdout: str,
    workspace: Path,
    root: Path,
    max_files: int,
) -> tuple[list[tuple[str, list[tuple[int, str]]]], int, int]:
    """把引擎的 JSON 流解析成 (排好序的命中名单, 搜了几个文件, 几个文件有命中)。

    一行一条 JSON（JSON Lines），三种消息里只用得上两种：

      * `match` —— 一条命中：路径、行号、整行文本；
      * `summary` —— 最后一条，带 `stats`：`searches` 是**被搜过的文件数**，
        `searches_with_match` 是其中有命中的。这两个数正是结果末尾那几句"说清楚"
        要用的，而且比旧实现自己数更准（二进制文件也算被搜过，旧实现数不到它）。

    `begin` / `end` 刻意不解析：它们是每个文件一份的重复信息，而 summary 已经把总计
    给全了 —— 多一套"自己数"的账就是第二份事实。

    单条消息解析失败就跳过：引擎的 stdout 是我们的管道，混进一行非 JSON 不该让整个
    工具调用炸掉（那是"预期内的失败返回文本"那条规矩在这一层的形态）。
    """
    buffered: dict[str, tuple[tuple[int, str], list[tuple[int, str]]]] = {}
    searched = 0
    with_match = 0

    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue

        kind = message.get("type")
        if kind == "match":
            data = message.get("data") or {}
            path = (data.get("path") or {}).get("text")
            # `lines.text` 在二进制命中时不存在（那边是 lines.bytes）—— 那是一种
            # 拿不到行文本的命中，跳过而不是编一行出来。
            line = (data.get("lines") or {}).get("text")
            lineno = data.get("line_number")
            if path is None or line is None or lineno is None:
                continue

            target = Path(path)
            rel = _relative(target, workspace)
            if rel not in buffered:
                _remember(buffered, rel, _order_key(target, root), max_files)
            entry = buffered.get(rel)
            if entry is None:
                # 名额之外的文件：它后面的命中直接丢，但总数由 summary 负责，不靠这里。
                continue
            # 行尾的换行由引擎原样带回来（CRLF 文件里是 \r\n）—— 排版这一层去掉它，
            # 但保留行内原有的空白，那是原文的一部分。
            entry[1].append((lineno, line.rstrip("\r\n")))
        elif kind == "summary":
            stats = (message.get("data") or {}).get("stats") or {}
            searched = stats.get("searches") or 0
            with_match = stats.get("searches_with_match") or 0

    listed = [
        (rel, hits)
        for rel, (_, hits) in sorted(buffered.items(), key=lambda item: item[1][0])
    ]
    return listed, searched, with_match


def _engine_error_note(stderr: str) -> str | None:
    """引擎自己报的错（权限、路径消失之类）压成一句注脚。

    它和"没匹配到"是两件事：没匹配到可以放心，搜到一半出错不行。所以有就一定要说 ——
    否则模型会把"有文件没读上"当成"这里确实没有"。
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        return f"引擎报了 1 条错误：{lines[0]}"
    return f"引擎报了 {len(lines)} 条错误，第一条：{lines[0]}"


def grep(
    workspace: str,
    pattern: str,
    path: str = ".",
    include: str = "",
    ignore_case: bool = False,
    max_files: int = MAX_FILES,
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> str:
    """在工作区的 path 下递归搜索 pattern，返回给模型的文本。

    **永远返回字符串，不抛异常** —— 和 shell.py 是同一条规矩。「没匹配到」是搜索的
    正常结果，不是工具故障：抛出去的话 agent.py 会把它记成工具故障，而模型真正需要
    的恰好是"这里确实没有"这个信息。

    路径安全由 `safe_path` 兜底：`path` 指到工作区外面会在这里被拦下来。正则的语法
    错误是**模型自己写错了**，也该由它读着错误信息自己改，所以连它一起返回而不是抛。

    **超时是唯一"什么都不返回"的分支**，而且是故意的：返回半份命中而不说清，就等于
    让模型把"超时截断"读成"就这么多"。说清超时，它就知道该缩小 path 或者加 include。

    `timeout_seconds` 只有测试会改（照 retry.py 里 `sleep` 那条先例）。它是 backstop，
    不是给模型调的旋钮，所以刻意不进 GrepArgs 的 schema。
    """
    fs = FileSystem(workspace)
    # 报给模型的路径必须相对**工作区** —— 模型会把它原样交给 read_file，read_file 是按
    # 工作区解析的。（这里曾经传错过基准，结果 path="tools" 时报出 "builtin.py"，
    # read_file 直接 FileNotFoundError。）
    root = fs.safe_path(path)

    if not root.exists():
        return f"路径不存在：{path}"
    if not root.is_dir():
        return f"不是目录：{path}（这个工具只搜目录；要读单个文件用 read_file）"

    binary = rg_binary()
    if binary is None:
        # 走到这里说明注册表是在 rg 还在的时候造的、之后文件没了（正常装配下
        # create_tool_registry 压根不会注册它）。不抛异常，因为调用方只能读文本。
        triple = host_triple() or "这个平台"
        return (
            f"内置的 ripgrep 不在（找的是 {_VENDOR_DIR / triple}）。"
            f"这不是搜索没结果，是这套运行环境不完整：跑 `python scripts/fetch_rg.py` "
            f"会把对应平台的 rg 放进 tools/vendor/rg/。"
        )

    argv = _argv(binary, root, pattern, include, ignore_case)

    try:
        completed = subprocess.run(
            argv,
            # 工作目录设成工作区：报出来的路径就落在工作区里，_relative 之后形状稳定。
            cwd=fs.workspace,
            # 引擎不该读 stdin；给了路径之后它也不会。钉死是防它哪天改成先看一眼 stdin。
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            # JSON 里混进非 utf-8 字节时替换掉，而不是让整个工具调用炸掉。
            errors="replace",
            timeout=timeout_seconds,
            # 没有它，在 TUI 后面每搜一次都会闪一个黑框。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return (
            f"搜索超过了 {timeout_seconds} 秒，已终止，这次没有任何结果。\n"
            f"缩小范围再试：把 path 指到子目录，或者用 include 限定文件名（例如 *.py）。"
        )
    except OSError as exc:
        # 可执行文件在、但起不来（权限、格式不对、被杀软拦住）。
        return f"内置的 ripgrep 起不来（{binary}）：{type(exc).__name__}: {exc}"

    # 正则写错要**单独说**：否则模型会以为"没搜到"，然后去改一个本来没问题的 path 或
    # include，白绕一圈。引擎把它打在 stderr 上（带 caret 示意图），原样交回给模型 ——
    # 它要改的就是那个 pattern。
    if "regex parse error" in completed.stderr:
        return f"正则表达式无效：{pattern}\n{completed.stderr.strip()}"

    listed, searched, with_match = _collect(
        completed.stdout, fs.workspace, root, max_files
    )

    if listed:
        blocks: list[str] = []
        for rel, hits in listed:
            shown = hits[:MAX_MATCHES_PER_FILE]
            header = f"{rel} ({len(shown)} 处命中)"
            lines = [_format_line(lineno, line) for lineno, line in shown]
            if len(hits) > MAX_MATCHES_PER_FILE:
                lines.append(f"…（此文件命中已到 {MAX_MATCHES_PER_FILE} 条上限）")
            blocks.append("\n".join([header, *lines]))
        body = "\n".join(blocks)
    else:
        body = (
            f"没有匹配：在 {path} 下找不到符合 {pattern} 的内容"
            f"（搜了 {searched} 个文件）"
        )

    notes: list[str] = []
    if with_match > len(listed):
        # **先说总数**，而不是只说"列了前 N 个"：输出本身还可能被 _truncate 再切一刀，
        # 所以"看得见几个"是不确定的，而"一共几个有命中"是确定的、也是模型真正需要的
        # 那个数 —— 它是"这次搜索问得太宽"的信号。
        notes.append(f"共 {with_match} 个路径有命中，只挑列了最前面的 {len(listed)} 个")
        notes.append("非隐藏路径排在前面，没列出来的更可能在隐藏目录或第三方目录里")
    if error_note := _engine_error_note(completed.stderr):
        notes.append(error_note)
    if notes:
        body += "\n\n（" + "；".join(notes) + "）"

    return _truncate(body)


class Grep:
    """把工作区绑在身上的 grep。

    注册表的 handler 契约是 `handler(**kwargs)`，而 `workspace` **不该经过模型的手**
    —— 它是这次运行的沙箱根，模型能改它就等于能改搜索边界。所以在装配时绑一次
    （和 FileSystem / Shell 持有 workspace 是同一个形状）。
    """

    def __init__(self, workspace: str):
        self.workspace = workspace

    def __call__(self, **arguments: Any) -> str:
        return grep(self.workspace, **arguments)
