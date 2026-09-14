"""打包：**装出来的那个包里，随代码走的文件一个都不能少。**

这一类失败有一个共同点：**源码目录里跑测试永远看不到它**。文件就在磁盘上，
`paths.package_dir()` 也算得对 —— 只有装成 wheel 之后才会发现某一类没被收进去，而症状是
"命令能起来，但某个功能悄悄没了"：

  * `prompts/system.zh.md` 缺了 ⇒ 第一次新建会话就抛 FileNotFoundError；
  * `config.example.json` 缺了 ⇒ 首次运行不再生成配置，那句"我已经给你建好了"变成
    "你得自己建一份"（`scaffold()` 会静默返回 None）；
  * `protocol/schema/` 缺了 ⇒ 协议的 schema 校验路径没得可校；
  * `tools/vendor/rg/` 缺了 ⇒ `grep` 工具**不注册**，模型看不到它，于是它改去猜文件名
    然后 `read_file` —— 正好是提示词里"优先搜索定位"的反面。

所以这里真的**构建一次 wheel** 再打开来看。慢（几秒）但没有替代品：断言"文件在源码目录里"
证明不了任何事。

## 为什么不是断言一张清单

清单会漂。这里只钉**四类各挑一个代表**，外加一条"tests/ 不许进 wheel"—— 那是另一个方向的
错误（`packages` 写成 `["."]` 就会把测试、doc、甚至 `.venv` 一起打进去）。
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 四类各一个代表。**路径写全**（含 `agent_runtime/` 前缀）：wheel 里的路径是从包名开始的，
# 少写前缀会让断言永远命中不了 —— 也就是测试静默失效。
MUST_BE_IN_THE_WHEEL = (
    "agent_runtime/prompts/system.zh.md",
    "agent_runtime/config.example.json",
    "agent_runtime/protocol/schema/outbound.schema.json",
    "agent_runtime/tools/vendor/rg/x86_64-unknown-linux-musl/rg",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    """现造一个 wheel。**module 级**：构建要几秒，而这个文件里每条测试看的是同一个产物。

    用 `python -m build` 之外的路径（直接调 hatchling）会绕开 `pyproject.toml` 里的配置，
    那就不是在测真正的打包了。这里走 `uv build`，因为它是这个项目实际用的那个工具；
    找不到 uv 就跳过 —— 一条环境依赖的测试红在别人机器上，比没有这条更坏。
    """
    uv = _find_uv()
    if uv is None:
        pytest.skip("这台机器上没有 uv，跳过打包检查")

    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [str(uv), "build", "--wheel", "--out-dir", str(out)],
        cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"构建失败：\n{result.stderr[-3000:]}"

    built = sorted(out.glob("*.whl"))
    assert len(built) == 1, f"应该只产出一个 wheel，实际 {built}"
    with zipfile.ZipFile(built[0]) as archive:
        yield archive


def _find_uv() -> Path | None:
    """找 uv。`shutil.which` 之外还看一下 `~/.local/bin` —— 装 uv 的官方脚本放那儿，
    而它不一定在非交互 shell 的 PATH 上（实测过）。"""
    import shutil

    found = shutil.which("uv")
    if found:
        return Path(found)
    candidate = Path.home() / ".local" / "bin" / "uv"
    return candidate if candidate.is_file() else None


@pytest.mark.parametrize("name", MUST_BE_IN_THE_WHEEL)
def test_the_wheel_carries_the_files_that_ride_along_with_the_code(wheel, name):
    """四类随码文件都得在 wheel 里。见模块 docstring 里各自缺了会怎样。"""
    assert name in wheel.namelist(), (
        f"{name} 没进 wheel —— 装出来的命令会在用到它的时候才崩，"
        f"而源码目录里跑测试看不到这个问题"
    )


def test_the_wheel_does_not_carry_the_repository(wheel):
    """反方向：**仓库里那些不该发布的东西不许进 wheel。**

    `packages` 写成 `["."]`（扁平布局最容易想到的写法）就会把 tests/、doc/、甚至 `.venv`
    一起打进去。那不会报错，只会让每个装它的人多下载几十兆、并在 site-packages 里多出一个
    顶层 `tests` 包 —— 而那个包会和别人的 `tests` 撞名。
    """
    leaked = [
        name for name in wheel.namelist()
        if name.startswith(("tests/", "doc/", "scripts/", ".venv/"))
    ]

    assert leaked == [], f"这些不该进 wheel：{leaked[:10]}"


def test_the_console_script_is_declared(wheel):
    """`tudouni` 这个命令必须真的被声明出来。

    它是"装成命令"这件事的**唯一**用户可见产物。少了它，`pip install` 照样成功，而人敲
    `tudouni` 得到的是 command not found —— 而那看起来像装失败了。
    """
    entry_points = [n for n in wheel.namelist() if n.endswith("entry_points.txt")]
    assert entry_points, "wheel 里没有 entry_points.txt"

    text = wheel.read(entry_points[0]).decode("utf-8")
    assert "[console_scripts]" in text
    assert "tudouni" in text
    # 指向的模块必须是真的能 import 的那个（拼错一个字母，`pip install` 不会报）。
    assert "agent_runtime.main:main" in text


def test_the_installed_layout_is_importable_from_anywhere(tmp_path):
    """**装成命令之后，包在哪和工作区在哪是两件事。**

    这条不构建 wheel，它验的是同一件事的另一半：从一个**和仓库无关的目录**里
    `import agent_runtime` 并问它两个根，答案必须一个跟着代码、一个跟着 cwd。

    以前这两个是同一个值（工作区就是包目录），所以任何混用都看不出来 —— 而装成命令之后
    立刻分家：包在 site-packages 里，那不是用户的项目。
    """
    script = (
        "import sys, json, pathlib;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from agent_runtime import paths;"
        "print(json.dumps({'package': str(paths.package_dir()),"
        " 'workspace': str(paths.workspace_dir())}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(tmp_path),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr

    answer = __import__("json").loads(result.stdout)
    assert answer["package"] == str(REPO_ROOT / "agent_runtime")
    assert answer["workspace"] == str(tmp_path)
