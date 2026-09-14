# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 的配方：把 tudouni 冻成一个**不含 .py 的**目录。

    pyinstaller --noconfirm --clean packaging/tudouni.spec

一般不直接跑它 —— `scripts/build_release.py` 会带上正确的路径、验证产物、再连同安装脚本
打成一个 zip。这个文件只管"什么东西进产物"。

## 四类随代码走的文件，缺一个就是"能启动、少一个功能"

和 `tests/test_packaging.py` 盯 wheel 的那四类是**同一批**（prompts/、config.example.json、
protocol/schema/、tools/vendor/rg/），缺了各自的症状写在那边的模块 docstring 里。它们
**在源码目录里永远测不出问题** —— 只有产物里才可能缺，而表现全是静默的。

## 落点必须和 `paths.package_dir()` 对上

数据一律放进 `agent_runtime/` 下，也就是**源码树的布局原样搬过去**。冻结时
`paths.package_dir()` 返回 `<_MEIPASS>/agent_runtime`（见那个函数的 docstring），于是
`_VENDOR_DIR` / `SCHEMA_DIR` / `PROMPTS_DIR` 三处派生全部自动成立。

**改了这里的 dest，就要同时想清楚那边的返回值** —— 这两半是同一件事，拆在两个文件里
本来就有风险，所以它们各自都写明了对方。

## 为什么是 onedir 而不是 onefile

TUI 会把**同一个 exe** 再起一遍当 runtime 子进程（`protocol/client.py`）。onefile 每次
启动都要把整包解到临时目录，于是"开一个界面"要解两遍、多等几百毫秒、还多占一份磁盘。
onedir 解一次、两个进程共用。
"""

import sys
from pathlib import Path

# spec 住在 `packaging/` 下，所以仓库根是它的上一层。
ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "agent_runtime"

# 这台机器该带哪一份 ripgrep：**从代码那张表里问**，不在这里抄第二份。
# 抄一份的话，往 `_TRIPLES` 加平台而忘了改这里，症状是产物里少一个二进制 ——
# 而那是静默的（`grep` 不注册，见 `grep.py` 的 `_VENDOR_DIR` 那段）。
sys.path.insert(0, str(ROOT))
from agent_runtime.tools.builtin.grep import host_triple  # noqa: E402
from agent_runtime.version import current as package_version  # noqa: E402

TRIPLE = host_triple()
if TRIPLE is None:
    raise SystemExit(
        f"这台机器（{sys.platform}）没有随仓库带的 ripgrep，打不出完整的包。\n"
        f"  先跑 scripts/fetch_rg.py，或者往 grep.py 的 _TRIPLES 里加这个平台。"
    )

RG_NAME = "rg.exe" if sys.platform == "win32" else "rg"
RG_SOURCE = PKG / "tools" / "vendor" / "rg" / TRIPLE / RG_NAME
if not RG_SOURCE.is_file():
    raise SystemExit(f"少了 {RG_SOURCE} —— 跑 scripts/fetch_rg.py 补上再打包")

DATAS = [
    (str(PKG / "prompts"), "agent_runtime/prompts"),
    (str(PKG / "config.example.json"), "agent_runtime"),
    (str(PKG / "protocol" / "schema"), "agent_runtime/protocol/schema"),
]

# 版本戳 —— **产物里没有 `pyproject.toml`**（那是构建用的文件，不该跟着可执行文件发给
# 用户），而 `--version` 和欢迎屏都要那个数字。所以在这里把它单独写成一份数据随包带上，
# `agent_runtime/version.py` 优先读它（文件名就是那边认的 `STAMP_FILE_NAME`）。
#
# 写在 `build/` 下（PyInstaller 自己的中间目录，`.gitignore` 里有）：**不碰源码树**，
# 也**不用 `tempfile`** —— 受限环境里系统临时目录未必写得动（实测撞过一次，那次构建
# 红在一个和产物毫无关系的原因上）。
_version = package_version()
if not _version:
    raise SystemExit("读不出 pyproject.toml 里的版本号，打出来的包会是个没版本的东西")
STAMP = ROOT / "build" / "_version.txt"
STAMP.parent.mkdir(parents=True, exist_ok=True)
STAMP.write_text(_version, encoding="utf-8")
DATAS.append((str(STAMP), "agent_runtime"))

BINARIES = [
    (str(RG_SOURCE), f"agent_runtime/tools/vendor/rg/{TRIPLE}"),
]

a = Analysis(
    [str(PKG / "main.py")],
    pathex=[str(ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # 产物里不要源码。`main.py` 是被当作入口脚本喂进来的，PyInstaller 会把它编成
    # 字节码打进 PYZ，从来不会以 `.py` 的形态出现在产物里 —— 这一条只是把
    # "万一有谁把源码目录整体 add-data 进来"这种情况挡掉。
    excludes=["pytest", "_pytest", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tudouni",
    debug=False,
    strip=False,
    # UPX 会让杀毒软件更爱报警，而这里省下的那点体积不值得。
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tudouni",
)
