# tools/vendor/rg —— 随仓库带的 ripgrep

`grep` 工具的引擎住在这里。**这是"不对外有依赖"那句话的兑现方式**：可执行文件跟着
仓库走，用户不需要自己装 ripgrep，运行期也不联网。

## 目录约定

```
tools/vendor/rg/
  LICENSE-MIT                       ripgrep 的 MIT 许可原文（Unlicense OR MIT 双许可）
  x86_64-pc-windows-msvc/rg.exe     一个官方 target triple 一份，目录名就是 triple
  x86_64-unknown-linux-musl/rg
```

目录名和文件名都照 **ripgrep 官方 release 资产**的拼法（`ripgrep-<版本>-<triple>.zip`
里的那一层），这样"这份是哪个构建"不需要额外的对照表。

**Linux 用 musl 那一份**：它是静态链接的，在任何 glibc 发行版上照样跑，所以一个 triple
就覆盖整片发行版 —— 官方对 x86_64 也只发 musl 这一种 Linux 构建。

## 支持哪些平台 = `_TRIPLES` 那张表

**只有 x86_64 的 Windows 和 Linux。** 这个集合只有一份事实：`tools/builtin/grep.py` 里的
`_TRIPLES`，而 `scripts/fetch_rg.py --all` 拉的就是它的取值 —— 所以"仓库里该有几份、
脚本会拉哪几份、代码认哪几个平台"永远是同一个答案。

不支持一个平台**不是"文件恰好不在"**，是代码的决定。arm64 和 macOS 现在都在这条线上：
官方有构建、哈希也钉在 `fetch_rg.py` 里，但 `_TRIPLES` 没有它们，所以那些机器上：

- `create_tool_registry` **不注册** `grep`（模型看不到它，也就不会白调一次 ——
  和缺 `TAVILY_API_KEY` 不注册 `web_search` 同一条路）；
- `runtime/composition.py` 的 `notices()` 会往 stderr 打一句，说清是"没支持"还是"缺件"；
- **提示词里那句「搜文本用专用文件工具」会变成假话**，于是 `tests/test_prompt.py` 会红。
  这是刻意的：那句话要么成立，要么得改提示词，不能让它静默变成一句错的引导。

**要支持一个新平台是两步，两步都必要**：

```sh
uv run python scripts/fetch_rg.py --triple aarch64-apple-darwin   # 1. 二进制进仓库
# 2. 往 grep.py 的 _TRIPLES 加一行 —— 不加这行，上面那些表现一条都不会变
```

只做第一步的话，那台机器上依然不注册。这正是 `supported_triples()` 那段说明的意思。

## 仓库里现在有哪几份

```
x86_64-pc-windows-msvc/rg.exe    4,218,880 字节
x86_64-unknown-linux-musl/rg     5,408,904
                                 ── 合计约 9.2 MB
```

两份都是 **ripgrep 15.2.0 的官方构建**，由 `scripts/fetch_rg.py --all` 拉下来、逐个过
sha256 校验（哈希钉在脚本里）。想核对某一份是不是官方的：

```sh
uv run python scripts/fetch_rg.py --list         # 支持哪些、在不在、另外还有哪些官方构建
tools/vendor/rg/x86_64-pc-windows-msvc/rg.exe --version
# ripgrep 15.2.0 (rev e89fff89ac)   features:+pcre2
```

**官方 release 是带 PCRE2 的**（`--pcre2` 随时能开），但工具刻意不用它 —— 理由在
`tools/builtin/grep.py` 的模块 docstring 里：那会把"这段正则是哪种方言"变成模型看不见
的第二个变量，于是同一个 pattern 在两次调用里可能一个成一个败。

**这 9.2 MB 是"运行期零依赖"的价钱**，也是它唯一的代价：二进制在 git 里 diff 不出来
（只有一行 `Bin 4218880 bytes`），所以它的可信度只能来自"这份字节等于官方发布的那份"
—— 那是哈希能说清楚、别的东西说不清楚的事。所以每多带一个平台都是一笔要单独算的账，
"先带上说不定以后用得到"不是个好理由：真用得到时，上面那两步。

## 为什么不走 PyPI

两条都查过，都放弃了：

- **`ripgrep-bin`**（把官方二进制打成 `py3-none-<平台>` wheel，装完就有）：依赖的是
  **别人替你编的产物**，而"不对外有依赖"的诉求里包含这一层。它还只覆盖一部分平台
  （没有 win32、没有 freebsd）。
- **`rg-search`**（把 ripgrep 的 crate 用 PyO3 封成进程内库，最漂亮的那条）：只有
  `cp311` 的 wheel，而本项目的解释器钉在 3.12 —— 会退化成源码构建，需要 Rust 工具链。
  那比"带一个二进制"重得多，也直接违背"不对外有依赖"。

自己用 maturin 封一套同理：能得到"进程内、无子进程"，代价是每个平台每次升级都要自己
出 wheel。

## 许可

ripgrep 是 **Unlicense OR MIT** 双许可，随包附 `LICENSE-MIT` 即可（本目录下那份是从
`BurntSushi/ripgrep` 的 `LICENSE-MIT` 原样取来的，1081 字节）。
