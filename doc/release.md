# 出发布包

给不装 Python、也不该看到源码的人一个压缩包：解压 → 跑安装脚本 → 能用。

## 出包

要 PyInstaller（不在 run 依赖里，单独装）：

```powershell
uv sync --group build          # 或者：uv pip install pyinstaller
uv run python scripts/build_release.py
```

Linux 上同一条命令。产物：

```
dist/tudouni-<版本>-<triple>.zip
```

**在哪个平台跑就出哪个平台的包。** PyInstaller 不是交叉编译器（产物里带着 CPython
运行时和平台相关的扩展），所以 Windows 和 Linux 要各跑一次。支持哪些平台由
`tools/builtin/grep.py` 的 `_TRIPLES` 决定（现在只有 x86_64 的 Windows / Linux）——
别的平台上脚本会直接停下，而不是出一个少了 ripgrep 的包。

## 包里是什么

```
tudouni.exe（Linux 上是 tudouni）  冻好的可执行文件，Python 运行时已打进去
_internal/                        依赖 + prompts/ + config.example.json
                                  + protocol/schema/ + tools/vendor/rg/
install.ps1 / install.sh          装到当前用户，不需要管理员
README.txt                        给用户的说明
```

**一个 `.py` 都没有。**

## 打包时会验证

`build_release.py` 在压缩之前逐条查，任一条不过就停下：

1. 产物里没有 `.py` —— `datas` 里多写一行就能把整棵源码树搬进去，而打包照样成功；
2. 四类随代码走的文件都在（缺了 = 能启动、少一个功能，且没有任何别的症状）；
3. `install.ps1` 带 UTF-8 BOM（少了它 Windows PowerShell 5.1 会把中文注释读成乱码
   并解析失败）；
4. 真的把 exe 跑起来：`--help`、`--version`（版本号必须对得上这次构建），以及
   `--runtime-stdio`（TUI 起的正是这个子进程）；
5. 冻结时 `protocol/client.py` 拼的 argv 不再带 `-m`。

顺带说清版本号是怎么进产物的：**产物里没有 `pyproject.toml`**（构建用的文件不该跟着
可执行文件发给用户），所以打包时把它写成一个版本戳（`agent_runtime/_version.txt`）随包带上，
`agent_runtime/version.py` 优先读它、读不到才回落去找 `pyproject.toml`。少了戳，用户就没法
核对"我这次装上新版没有" —— `tudouni --version` 会说"版本号读不出来"。

改完代码先跑全量测试：`uv run pytest`。

## 拿产物验端到端

`scripts/verify_tui.py` 默认起的是**源码**子进程。要让它驱动**产物**，在它之前把
这两行加上（我这么验过，整条 TUI 验收都会跑在冻结的 runtime 上）：

```python
sys.frozen = True
sys.executable = "<repo>/dist/tudouni/tudouni.exe"
```

## 用户那一边

```
Windows   解压 → 右键 install.ps1 →「使用 PowerShell 运行」→ 开新终端 → tudouni --tui
Linux     解压 → chmod +x install.sh && ./install.sh → 开新终端 → tudouni --tui
```

第一次运行会让用户把密钥填进 `~/.tudouni/config.json`（Windows 上是
`%USERPROFILE%\.tudouni\config.json`）。

zip **不带 POSIX 权限位**，所以 Linux 那份靠 `chmod +x install.sh` 起头，脚本自己再补上
`tudouni` 和 ripgrep 的执行位。

## 这不是保护

产物里是字节码，不是源码，但 `pyinstxtractor` 能把 `.pyc` 抠出来、反编译成接近原样的
东西。要抬高这一步的成本得换 Nuitka —— 那时 `packaging/tudouni.spec` 和
`paths.package_dir()` 的冻结分支都要跟着看一遍。

## 文件在哪

```
scripts/build_release.py    出包 + 验证
packaging/tudouni.spec      PyInstaller 配方：什么进产物、放在哪个落点
packaging/install.ps1       分发给用户
packaging/install.sh        分发给用户
packaging/README.txt        压缩包里的说明
```

`dist/` 和 `build/` 都是产物，都在 `.gitignore` 里。
