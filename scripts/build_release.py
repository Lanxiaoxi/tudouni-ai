"""出一个发布包：**一个不含 .py 的压缩包**，用户解压、跑安装脚本、就能用。

    python scripts/build_release.py

在这台机器上构建**本平台**的那一份。产物落在 `dist/`：

    dist/tudouni-<版本>-<triple>.zip

包里有：冻结好的 `tudouni`（+ `_internal/`）、`install.ps1` / `install.sh`、`README.txt`。
**一个 `.py` 都没有** —— 这条由 `_check_no_source()` 在打包前真的走一遍目录来保证，
不是靠"我们相信 PyInstaller 不会放源码"。

## 为什么要跨平台各构建一次

PyInstaller **不是交叉编译器**：产物里带着 CPython 运行时和平台相关的扩展模块，所以
Windows 那份只能在 Windows 上出、Linux 那份只能在 Linux 上出。这个脚本本身是平台无关的，
在哪个平台跑就出哪个平台的包：

    Windows   python scripts/build_release.py
    Linux     python scripts/build_release.py

支持哪些平台由 `tools/builtin/grep.py` 的 `_TRIPLES` 说了算（现在只有
x86_64 的 Windows / Linux）—— 别的平台上这里会直接停下并说清楚，而不是出一个
少了 ripgrep 的包（那会让 `grep` 工具静默消失）。

## 验证是这一步的重点，不是附属品

打出来的包最容易的坏法不是"起不来"，而是**能起来、少一个功能**：提示词、协议 schema、
ripgrep 各自躺在包装脚本忘了带的地方，而源码目录里跑测试永远看不到（这正是
`tests/test_packaging.py` 存在的理由，这里把同一套判据搬到冻结产物上）。
所以 `_verify()` 会打开产物真的看一遍，并且真的把那个二进制跑起来。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = REPO_ROOT / "packaging"
SPEC_FILE = PACKAGING_DIR / "tudouni.spec"
DIST_DIR = REPO_ROOT / "dist"
WORK_DIR = REPO_ROOT / "build"

# 随代码走、**必须**出现在产物里的四类，路径是相对产物根（`<产物>/_internal/`）的。
# 和 `tests/test_packaging.py` 盯 wheel 的那四类一一对应 —— 两边都别单方面改。
REQUIRED = (
    "agent_runtime/prompts/system.zh.md",
    "agent_runtime/config.example.json",
    "agent_runtime/protocol/schema/outbound.schema.json",
)

# 版本戳：`--version` 和欢迎屏靠它。产物里没有 `pyproject.toml`（构建用的文件不发给
# 用户），所以它是**唯一**能让冻出来的程序说出自己版本的东西 —— 少了它，那个包就是
# "永远说不出版本号"的包，而这件事在源码目录里测不出来。
VERSION_STAMP = "agent_runtime/_version.txt"

# 随压缩包一起给用户的说明与安装脚本（它们本身不是程序的一部分）。
SHIPPED_ALONGSIDE = ("install.ps1", "install.sh", "README.txt")


def _triple() -> str:
    """本机对应哪个 ripgrep/发布产物的 triple。**问代码那张表**，不在这里抄第二份。"""
    sys.path.insert(0, str(REPO_ROOT))
    from agent_runtime.tools.builtin.grep import host_triple

    triple = host_triple()
    if triple is None:
        raise SystemExit(
            f"这台机器（{sys.platform}）不在支持列表里（见 grep.py 的 _TRIPLES）。\n"
            f"  发布包只出 x86_64 的 Windows / Linux；要加平台先跑 scripts/fetch_rg.py。"
        )
    return triple


def _version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _binary_name() -> str:
    return "tudouni.exe" if sys.platform == "win32" else "tudouni"


def _run_pyinstaller() -> Path:
    """跑 PyInstaller，返回产物目录（`dist/tudouni/`）。"""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "没有 PyInstaller。先装它：\n"
            "    uv pip install pyinstaller\n"
            "（它只在这一步用得上，所以不在项目的 run 依赖里。）"
        ) from None

    for stale in (DIST_DIR, WORK_DIR):
        shutil.rmtree(stale, ignore_errors=True)

    print("[1/4] PyInstaller 打包中……")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--noconfirm", "--clean",
         "--distpath", str(DIST_DIR),
         "--workpath", str(WORK_DIR),
         str(SPEC_FILE)],
        cwd=str(REPO_ROOT),
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败（退出码 {result.returncode}）")

    bundle = DIST_DIR / "tudouni"
    if not bundle.is_dir():
        raise SystemExit(f"PyInstaller 没吐出预期的目录：{bundle}")
    return bundle


def _internal(bundle: Path) -> Path:
    """PyInstaller 6 起数据文件住在 `<产物>/_internal/`（也就是运行期的 `_MEIPASS`）。"""
    return bundle / "_internal"


def _check_no_source(bundle: Path) -> None:
    """产物里**一个 `.py` 都不能有** —— 这是这一整件事的目的。

    值得单独一条检查（而不是"相信打包工具"）：`datas` 里多写一行 `(str(PKG), ".")`
    之类的顺手写法，就会把整个源码树原样搬进去，而**打包照样成功**、程序照样能跑、
    测试照样全绿。那种坏法没有任何别的症状。
    """
    leaked = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*.py"))
    if leaked:
        raise SystemExit(
            f"产物里有 {len(leaked)} 个 .py 文件，这正好是这次要避免的事：\n  "
            + "\n  ".join(leaked[:20])
        )


def _check_data_files(bundle: Path, version: str) -> None:
    """四类随代码走的文件 + 版本戳必须在。缺了的症状全是静默的。"""
    root = _internal(bundle)
    missing = [name for name in REQUIRED if not (root / name).is_file()]

    # 版本戳不但要在，**内容还得对**：一份写着旧版本号的戳比没有更坏（`--version` 会
    # 一脸确信地报一个错的数字，而那正是用户拿来核对"我装上新版没有"的东西）。
    stamp = root / VERSION_STAMP
    if not stamp.is_file():
        missing.append(VERSION_STAMP)
    elif stamp.read_text(encoding="utf-8").strip() != version:
        raise SystemExit(
            f"{VERSION_STAMP} 里是 {stamp.read_text(encoding='utf-8').strip()!r}，"
            f"而这次构建的版本是 {version!r}。"
        )

    # ripgrep 单拎出来：它是一份**可执行文件**，路径里还带 triple，所以没法写进
    # REQUIRED 那张常量表里（那会让这张表在别的平台上不成立）。
    sys.path.insert(0, str(REPO_ROOT))
    from agent_runtime.tools.builtin.grep import host_triple

    triple = host_triple()
    name = "rg.exe" if sys.platform == "win32" else "rg"
    rg = root / "agent_runtime" / "tools" / "vendor" / "rg" / str(triple) / name
    if not rg.is_file():
        missing.append(f"agent_runtime/tools/vendor/rg/{triple}/{name}")

    if missing:
        raise SystemExit(
            "产物里少了这些东西 —— 程序能启动，但对应的功能会静默消失：\n  "
            + "\n  ".join(missing)
        )


def _check_it_runs(bundle: Path, version: str) -> None:
    """真的把产物跑起来。三条，各自钉一件不重叠的事。

    ## 1. `--help`：这个二进制能起来

    它是**唯一一个不碰工作区**的调用（在 argparse 里就结束），所以它是"这个二进制
    本身能不能起来"的最便宜判据，不会顺带建出一个 `.tudouni/`。

    ## 2. `--version`：**版本戳真的被打进产物里了**

    源码目录里这条永远绿（那边从 `pyproject.toml` 读），而产物里没有那个文件 —— 少了
    戳，`--version` 会说"版本号读不出来"，而用户正是拿它核对"我装上新版没有"的。

    ## 3. `--runtime-stdio`：**TUI 要起的那条子进程真的能起**

    这一条是这次打包最该盯的地方：界面是个父进程，它把**同一个可执行文件**再起一遍
    当 runtime 子进程（`protocol/client.py`）。冻结之后 `sys.executable` 不再是一个
    通用解释器，`-m agent_runtime.main` 那条老命令会直接失败 —— 而症状是"界面闪一下
    就退"。所以这里喂一个空 stdin 给它：能正常收场就说明那一支通。

    顺带钉住父进程**会发出的那条命令**长什么样：它必须是"这个 exe 直接带
    `--runtime-stdio`"，而不是带 `-m`。这条断言很便宜，而它守的是一个只在冻结产物里
    才成立的约定。
    """
    exe = bundle / _binary_name()

    # **子进程的环境要隔离。** 这一问验的是"产物里的 runtime 起不起得来"，不是"构建这台
    # 机器上有没有配好模型"。不隔离的话，一个还没填密钥的人（或者刚清过配置、正拿自己
    # 当新用户试的人）会在打包这一步被拦住 —— 而那是**和产物毫无关系**的一次失败。
    # 实测撞过：开发机上 `~/.tudouni/config.json` 是空的模板，构建就红在"一条能用的路由
    # 都没有"上。
    #
    # 顺带挡掉一个副作用：`AGENT_CONFIG_FILE` 一旦指了，`scaffold()` 就不会往**构建者
    # 自己的 home** 里写模板（那是"我自己管路径"的表示）。一次打包不该动别人的 home。
    verify_config = WORK_DIR / "verify-config.json"
    verify_config.write_text("{}", encoding="utf-8")
    child_env = {
        **os.environ,
        "AGENT_CONFIG_FILE": str(verify_config),
        "DEEPSEEK_API_KEY": "sk-build-verify",   # 让内置那条兜底路由可用
    }

    # cwd 用**仓库里的一个临时目录**，不用 `tempfile`：`--runtime-stdio` 会真的开一个
    # 工作区、在 cwd 下建 `.tudouni/`，而系统 temp 在受限环境里未必写得动（实测撞过：
    # 沙箱把 `%TEMP%` 挡掉了，于是这条验证红在一个和产物毫无关系的原因上，还顺带
    # 让 tempfile 的清理再炸一次）。放进 `build/`（.gitignore 里有）一定写得动，
    # 而且**不碰产物目录** —— 否则那些运行期文件会被下一步原样打进 zip。
    workspace = WORK_DIR / "verify-workspace"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        for label, argv in (
            ("--help", [str(exe), "--help"]),
            ("--version", [str(exe), "--version"]),
            ("--runtime-stdio", [str(exe), "--runtime-stdio"]),
        ):
            with subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace",
                cwd=str(workspace), env=child_env,
            ) as child:
                stdout, stderr = child.communicate(timeout=120)
                code = child.returncode

            if code != 0:
                hint = "（这正是 TUI 起的那个子进程）" if label == "--runtime-stdio" else ""
                raise SystemExit(
                    f"`{exe.name} {label}` 退出码 {code}{hint}\n  stderr:\n{stderr[-2000:]}"
                )

            if label == "--version" and version not in stdout:
                raise SystemExit(
                    f"`{exe.name} --version` 没说出 {version!r}，说的是 {stdout.strip()!r}。\n"
                    f"  产物里少了版本戳 {VERSION_STAMP} —— 那样用户没法核对装的是哪一版。"
                )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # 父进程将来会拼出来的 argv。**必须**是冻结那一支。
    sys.path.insert(0, str(REPO_ROOT))
    from agent_runtime.protocol.client import default_argv

    saved_frozen = getattr(sys, "frozen", None)
    sys.frozen = True                      # type: ignore[attr-defined]
    try:
        argv = default_argv(None, stream=True)
    finally:
        if saved_frozen is None:
            del sys.frozen                  # type: ignore[attr-defined]
        else:
            sys.frozen = saved_frozen       # type: ignore[attr-defined]

    if "-m" in argv or "agent_runtime.main" in argv:
        raise SystemExit(
            f"冻结之后父进程还在用 `-m` 起子进程：{argv}\n"
            f"  那条命令在产物里解析不了模块 —— 界面会闪一下就退。"
        )


def _check_installer_scripts() -> None:
    """随包那两个脚本的**存储形态**：BOM 和换行符。

    三条都属于同一类失败：**在你机器上一切正常，在用户机器上炸**，而且症状离原因很远。

      * `install.ps1` **必须**带 UTF-8 BOM。Windows PowerShell 5.1（双击、
        `powershell -File` 走的那个）读 `.ps1` 时没有 BOM 就按系统 ANSI 代码页解码 ——
        中文注释变乱码，而乱码里出现一个引号类的字节就足以让整个脚本解析失败。而它在
        任何 UTF-8 编辑器里看都好好的。
      * `install.sh` **必须不带** BOM。`#!` 前面多三个字节，内核就找不到解释器。
      * `install.sh` **必须是 LF**。CRLF 让它变成 `#!/bin/sh\\r`，Linux 上直接
        "bad interpreter"，而 Windows 上打开看不出区别。这一条由 `.gitattributes` 里的
        `*.sh text eol=lf` 保证（仓库开着 `core.autocrlf=true`），这里只做兜底检查。
    """
    for name, want_bom, want_lf in (
        ("install.ps1", True, False),
        ("install.sh", False, True),
    ):
        raw = (PACKAGING_DIR / name).read_bytes()

        if raw.startswith(b"\xef\xbb\xbf") != want_bom:
            if want_bom:
                raise SystemExit(
                    f"{name} 没有 UTF-8 BOM —— Windows PowerShell 5.1 会把中文注释读成"
                    f"乱码并解析失败，而它在 UTF-8 编辑器里看完全正常。\n"
                    f"  用编辑器存成「UTF-8 带 BOM」。"
                )
            raise SystemExit(
                f"{name} 带了 UTF-8 BOM —— `#!` 前面多三个字节，内核找不到解释器。\n"
                f"  存成「UTF-8 不带 BOM」。"
            )

        if want_lf and b"\r\n" in raw:
            raise SystemExit(
                f"{name} 是 CRLF —— Linux 上会变成 `#!/bin/sh\\r`，直接报 "
                f"bad interpreter。\n"
                f"  存成 LF（`.gitattributes` 里的 `*.sh text eol=lf` 本该保证这一点）。"
            )


def _stage(bundle: Path, version: str, triple: str) -> Path:
    """把说明和安装脚本放进产物目录，返回待打包的那个目录。"""
    for name in SHIPPED_ALONGSIDE:
        source = PACKAGING_DIR / name
        if not source.is_file():
            raise SystemExit(f"少了 {source}")
        shutil.copy2(source, bundle / name)

    print(f"[2/4] 产物就绪：{bundle}（tudouni {version} / {triple}）")
    return bundle


def _archive(bundle: Path, version: str, triple: str) -> Path:
    """打成 zip。

    **用 zip 而不是 tar.gz**：用户要的就是"一个压缩包"，而两种平台给不同的格式只会
    多一种要解释的东西。代价是 zip **不带 POSIX 权限位**，所以 Linux 那份解出来
    `install.sh` 和 `tudouni` 都是 644 —— `README.txt` 里写了要先 `chmod +x install.sh`，
    而脚本自己会把 `tudouni` 和随包的 ripgrep 补上执行位。三处合起来才闭合。
    """
    target = DIST_DIR / f"tudouni-{version}-{triple}.zip"
    target.unlink(missing_ok=True)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())

    print(f"[4/4] 压缩包：{target}  ({target.stat().st_size / 1024 / 1024:.1f} MB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="打一个不含源码的 tudouni 发布包")
    parser.add_argument("--skip-build", action="store_true",
                        help="复用 dist/tudouni，只重跑验证和打包")
    args = parser.parse_args()

    triple = _triple()
    version = _version()

    # 先查随包的脚本本身 —— 它们的毛病（比如 install.ps1 掉了 BOM、install.sh 变成
    # CRLF）**只会在用户机器上现形**，而且是在"解压完、双击、什么都没发生"那一刻。
    # 放在打包前查最省事。
    _check_installer_scripts()

    bundle = DIST_DIR / "tudouni"
    if args.skip_build:
        if not bundle.is_dir():
            raise SystemExit(f"--skip-build 但 {bundle} 不存在，先完整跑一次")
    else:
        bundle = _run_pyinstaller()

    print("[3/4] 验证产物……")
    _check_no_source(bundle)
    _check_data_files(bundle, version)
    _check_it_runs(bundle, version)
    print("      · install.ps1 带 BOM、install.sh 是 LF（不然在用户机器上直接跑不起来）")
    print("      · 没有 .py 泄漏")
    print("      · 四类随代码走的文件都在，版本戳内容对得上")
    print("      · 二进制能起，`--version` 说得出话，runtime 子进程也起得来")

    bundle = _stage(bundle, version, triple)
    target = _archive(bundle, version, triple)

    print()
    print(f"给用户的就是这一个文件：{target.name}")
    print("  用户：解压 → 跑 install.ps1 / install.sh → cd 到自己项目 → tudouni --tui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
