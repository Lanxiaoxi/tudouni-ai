"""把 ripgrep 的官方构建拉进 `tools/vendor/rg/` —— grep 工具的引擎。

    uv run python scripts/fetch_rg.py            # 只拉本机平台
    uv run python scripts/fetch_rg.py --all      # 支持的平台全拉（现在是 win-x64 + linux-x64）
    uv run python scripts/fetch_rg.py --list     # 看看支持哪些、现在各自在不在
    uv run python scripts/fetch_rg.py --triple aarch64-apple-darwin   # 拉一个还没支持的

**"支持哪些平台"不在这里定义** —— 它是 `tools/builtin/grep.py` 里 `_TRIPLES` 那张表，
`--all` 拉的就是它的取值。所以仓库里该有几份、脚本会拉哪几份、代码认哪几个平台，永远
是同一份事实。加一个平台是两步：`--triple` 把二进制放进仓库，再往那张表加一行。

**它是"引擎从哪来"的唯一入口**：`tools/vendor/rg/` 里那些二进制不该手工下载、手工
解压、手工改名，因为那样没人说得清"仓库里这份到底是哪个构建"。这里把版本、资产名和
**sha256 都钉死在代码里**，每次拉取都校验 —— 校验不过就报错退出，不落地半个字节。

为什么是 sha256 而不只是"下载成功"：二进制是唯一一种**review 看不见内容**的依赖，
diff 里只有一行 "Bin 5429760 bytes"。所以它的可信度只能来自"这份字节等于官方发布的
那份"，而那是哈希能说清楚、别的东西说不清楚的。

哈希从哪来：GitHub Release API 里每个资产的 `digest` 字段（`sha256:...`），官方同时
也为每个资产发了一个 `.sha256` 文件，两者应当一致。校验失败时脚本会把官方 `.sha256`
的地址打出来，让你去核对，而不是让你把期望值改成实际值。

网络这一层只用标准库（`urllib`），所以它不依赖 `uv sync` 过 —— 新检出的仓库可以先跑
它。

**它为什么 import 项目自己的代码**：`_VENDOR_DIR` / `_TRIPLES` 这些"引擎放哪、哪个
平台对应哪个构建"的事实住在 `tools/builtin/grep.py` 里（工具自己要用同一份）。这里
再拼一遍路径就是第二份事实，而它漂移的样子最难查 —— 脚本往 A 处拉、工具去 B 处找，
表现是"明明拉成功了却还说找不到引擎"。
"""

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# `scripts/` 在包**外面**，所以要上两级才是包所在目录（仓库根）—— 和 verify_tui.py
# 里那句同一个道理：它们算的是同一个目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent_runtime.tools.builtin.grep import (  # noqa: E402
    host_triple,
    supported_triples,
    vendor_dir,
)

# 钉死的 ripgrep 版本。**升级就是改这一行 + 下面六个哈希，然后跑一次 --all。**
VERSION = "15.2.0"

# 官方为这个版本发布的构建 → 资产 sha256。
#
# **这张表比"本项目支持的平台"大**，而那个差别是刻意的：
#
#   * 表里有、`_TRIPLES` 里也有 = 支持的平台，`--all` 会拉，仓库里就该有；
#   * 表里有、`_TRIPLES` 里没有 = **官方有这个构建，但本项目不支持那个平台**。哈希留在
#     这里是为了"哪天要支持它"时一条 `--triple` 就能补上，而不是让人去 GitHub 页面上
#     手抄一个哈希（手抄的那份没人能证明它是对的）。
#
# 改版本时六个都要跟着换 —— 少换一个，那条会在校验那一步炸掉（好事：它是 fail-closed
# 的）。
SHA256 = {
    "x86_64-pc-windows-msvc":
        "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5",
    "x86_64-unknown-linux-musl":
        "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
    "aarch64-pc-windows-msvc":
        "e4abca10c3a64ebea742667dd7009449d49403db5460dd6873e389fa2945360f",
    "aarch64-unknown-linux-musl":
        "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915",
    "x86_64-apple-darwin":
        "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
    "aarch64-apple-darwin":
        "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4",
}

_BASE_URL = f"https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}"


def asset_name(triple: str) -> str:
    """官方资产的文件名。Windows 是 zip，其余是 tar.gz —— 这是官方的打包习惯，不是我们的选择。"""
    suffix = ".zip" if "windows" in triple else ".tar.gz"
    return f"ripgrep-{VERSION}-{triple}{suffix}"


def binary_name(triple: str) -> str:
    return "rg.exe" if "windows" in triple else "rg"


def member_name(triple: str) -> str:
    """压缩包里那个可执行文件的路径。

    ripgrep 的 release 打包形状是 `ripgrep-<版本>-<triple>/rg[.exe]`，外面还裹着一层
    同名目录 —— 所以这里要写出完整成员路径，而不是"取第一个可执行文件"。
    """
    return f"ripgrep-{VERSION}-{triple}/{binary_name(triple)}"


def download(url: str) -> bytes:
    # GitHub 的发布下载会 302 到 release-assets.githubusercontent.com，urllib 默认
    # 跟随重定向，所以这里不需要额外处理。
    request = urllib.request.Request(url, headers={"User-Agent": "agent-runtime/fetch_rg"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def extract(blob: bytes, filename: str, member: str) -> bytes:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return archive.read(member)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        handle = archive.extractfile(member)
        if handle is None:
            raise KeyError(member)
        return handle.read()


def fetch(triple: str, *, quiet: bool = False) -> bool:
    """拉一个平台。成功返回 True；校验不过或下载失败返回 False（并已说明原因）。

    **支持与否不在这里判断**：`--triple` 允许拉一个 `_TRIPLES` 里没有的官方构建（那是
    "我要开始支持它了"的第一步）。所以这里只在**流水线的最后**提醒一句：光有文件，
    那个平台上 grep 还是不会注册。
    """
    expected = SHA256.get(triple)
    if expected is None:
        print(f"[{triple}] 不在 SHA256 表里 —— 这个平台没有对应的官方构建", file=sys.stderr)
        return False

    filename = asset_name(triple)
    url = f"{_BASE_URL}/{filename}"
    destination = vendor_dir() / triple / binary_name(triple)

    if not quiet:
        print(f"[{triple}] 下载 {url}")
    try:
        blob = download(url)
    except OSError as exc:
        print(f"[{triple}] 下载失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        # **绝不"改期望值让它过"。** 二进制是 review 看不见的依赖，这里放过去就等于
        # 把"这份字节等于官方发布的那份"这句话作废。
        print(
            f"[{triple}] sha256 不匹配，没有落地任何文件。\n"
            f"  期望：{expected}\n"
            f"  实际：{actual}\n"
            f"  官方也发了一份 .sha256，去核对它：{url}.sha256\n"
            f"  两者一致而这里仍不匹配 = 脚本里的期望值该更新（并当成一次依赖变更来 review）。",
            file=sys.stderr,
        )
        return False

    try:
        payload = extract(blob, filename, member_name(triple))
    except (KeyError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f"[{triple}] 压缩包里没有 {member_name(triple)}：{exc}", file=sys.stderr)
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    if platform.system() != "Windows":
        # 从压缩包里解出来的字节不带可执行位（zip 根本不存它）。
        destination.chmod(0o755)

    print(f"[{triple}] 已落地 {destination}（{len(payload)} 字节，sha256 校验通过）")
    if triple not in supported_triples():
        print(
            f"[{triple}] 注意：它**不在** grep.py 的 _TRIPLES 里，所以那个平台上 grep 仍然"
            f"不会注册。要真的支持它，得再往那张表加一行（见 supported_triples 的说明）。",
            file=sys.stderr,
        )
    return True


def _print_listing() -> None:
    """列出两边：支持的平台（要就位）和官方有构建但本项目不支持的（按需拉）。"""
    here = host_triple()
    supported = supported_triples()
    print(f"本机平台：{here or '不在支持列表里'}")

    print(f"\n支持的平台（{'、'.join(supported)}）：")
    for triple in supported:
        present = vendor_dir() / triple / binary_name(triple)
        mark = "已就位" if present.is_file() else "缺 —— 跑 --all 或 --triple"
        print(f"  {triple:<28} {mark}")

    others = sorted(set(SHA256) - set(supported))
    if others:
        print("\n官方也有构建、但本项目不支持（要支持就是两步：--triple 拉下来 + 往 "
              "_TRIPLES 加一行）：")
        for triple in others:
            print(f"  {triple:<28} sha256 {SHA256[triple][:12]}…")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"把 ripgrep {VERSION} 的官方构建拉进 tools/vendor/rg/",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true",
                       help="拉取全部**支持的**平台（_TRIPLES 的那几份）")
    group.add_argument("--triple", help="只拉这一个 triple（默认：本机平台）")
    group.add_argument("--list", action="store_true", help="列出支持的平台和当前状态")
    args = parser.parse_args(argv)

    if args.list:
        _print_listing()
        return 0

    if args.all:
        # **"全部"= 支持的那些**，不是"表里有的那些"：后者会把 macOS / arm64 也拉进
        # 这个仓库，而那是 20 MB 的账，不该由一条 --all 顺手决定。
        triples = list(supported_triples())
    elif args.triple:
        triples = [args.triple]
    else:
        triple = host_triple()
        if triple is None:
            print(
                f"这个平台（{platform.system()} / {platform.machine()}）不在支持列表里，"
                f"所以 grep 工具在这台机器上不会注册 —— 这**不是**缺件，是没支持它。\n"
                f"用 --list 看支持哪些平台；真要在它上面跑，见 fetch_rg.py 顶部那段。",
                file=sys.stderr,
            )
            return 1
        triples = [triple]

    failed = [triple for triple in triples if not fetch(triple)]
    if failed:
        print(f"这些平台没拉成：{'、'.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
