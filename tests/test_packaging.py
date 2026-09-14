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

## 第二种容器：冻结出来的可执行文件

`scripts/build_release.py` 把同一个包冻成一个**不含 .py 的**压缩包（给不想要源码的用户）。
那是同一批文件的第二种落点，而它多出两条**源码目录里永远看不出来**的约定，所以各有一条
测试钉在这里（都不需要真的摆弄 PyInstaller）：

  * **路径要跟着包走，不跟着 `__file__` 走** —— 冻结之后 `__file__` 指到别处，数据文件
    却在 `_MEIPASS` 下。`paths.package_dir()` 里那个分支加上 `messages.py` / `grep.py`
    两处收口，是这件事成立的全部；
  * **子进程不能再走 `-m`** —— 界面把同一个 exe 再起一遍当 runtime 子进程，而冻结产物
    不是通用解释器，`-m agent_runtime.main` 解析不了。

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
        if name.startswith(("tests/", "doc/", "scripts/", "packaging/", ".venv/"))
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


def test_the_installer_scripts_have_the_bytes_their_target_needs():
    """随包那两个脚本的**存储形态**：BOM 与换行符。

    三条都属于"在你机器上一切正常、在用户机器上炸"那一类：

      * `install.ps1` **必须带** UTF-8 BOM —— Windows PowerShell 5.1（双击、
        `powershell -File` 走的那个）没有 BOM 就按系统 ANSI 代码页解码，中文注释变乱码，
        而乱码里出现一个引号类的字节就足以让整个脚本解析失败；
      * `install.sh` **不能带** BOM —— `#!` 前面多三个字节，内核就找不到解释器；
      * `install.sh` **必须是 LF** —— CRLF 让它变成 `#!/bin/sh\\r`，Linux 上直接
        bad interpreter（`.gitattributes` 里钉了 `*.sh text eol=lf`，这里是兜底）。

    ## 为什么这条测试不是多余的

    它是**会被无意破坏的**：实测过一次 —— 任何重写 `install.ps1` 的编辑器/工具都会把
    BOM 抹掉（这个仓库里改一次文件就会），而抹掉之后它在任何 UTF-8 编辑器里看都完全
    正常。`build_release.py` 打包前也查这三条，但那时人已经走到打包那一步了；这里让它在
    `pytest` 就红。
    """
    ps1 = (REPO_ROOT / "packaging" / "install.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf"), (
        "install.ps1 掉了 UTF-8 BOM —— Windows PowerShell 5.1 会把中文注释读成乱码并"
        "解析失败（存成「UTF-8 带 BOM」）"
    )

    sh = (REPO_ROOT / "packaging" / "install.sh").read_bytes()
    assert not sh.startswith(b"\xef\xbb\xbf"), "install.sh 的 #! 前面不能有 BOM"
    assert b"\r\n" not in sh, "install.sh 变成 CRLF 了 —— Linux 上会 bad interpreter"


# --- 冻结产物：两条只在二进制里才成立的约定 ---------------------------------------
#
# 下面两条都不摆弄 PyInstaller（那要几十秒、还要装它），它们只把"冻结"这件事**装出来**
# ——一个 `_MEIPASS`、一个 `sys.frozen` —— 然后问结果。理由见模块 docstring 最后那一节：
# 这两条约定在源码目录里跑起来**永远是对的**，所以只能靠测试守。


def test_the_code_paths_follow_the_bundle_when_frozen(tmp_path):
    """冻结之后，随包的数据文件要从 `_MEIPASS` 下找，不能从 `__file__` 算。

    它同时是**"收口"的证据**：`messages.py` 和 `grep.py` 原来各自用 `__file__` 算一遍
    （在源码目录里和 `package_dir()` 恰好等价），现在必须走 `paths`。谁把它们改回去，
    这一条就红 —— 而那种回退在本地是看不出来的。
    """
    script = (
        "import sys, json;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        f"sys._MEIPASS = {str(tmp_path)!r};"
        "from agent_runtime import paths;"
        "from agent_runtime.protocol import messages;"
        "from agent_runtime.tools.builtin import grep;"
        "print(json.dumps({'package': str(paths.package_dir()),"
        " 'schema': str(messages.SCHEMA_DIR), 'vendor': str(grep.vendor_dir())}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(tmp_path),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr

    answer = __import__("json").loads(result.stdout)
    bundle = tmp_path / "agent_runtime"
    assert answer["package"] == str(bundle), "冻结时包目录应该落在 _MEIPASS 下"
    assert answer["schema"] == str(bundle / "protocol" / "schema")
    assert answer["vendor"] == str(bundle / "tools" / "vendor" / "rg")


def test_the_runtime_child_is_spawned_without_dash_m_when_frozen(monkeypatch):
    """冻结产物里，子进程必须是"同一个 exe 直接带 `--runtime-stdio`"。

    界面是父进程，runtime 是它拉起的子进程，而冻结之后 `sys.executable` 就是那个 exe
    —— 它不是一个通用解释器，`-m agent_runtime.main` 会直接失败。症状是"界面闪一下
    就退"。这条约定在源码目录里**永远是对的**（那里 `-m` 正是对的写法），所以只能靠
    测试守。
    """
    from agent_runtime.protocol import client

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = client.default_argv(None, stream=True)
    assert "-m" not in frozen, f"冻结之后不该再走 -m：{frozen}"
    assert "agent_runtime.main" not in frozen
    assert frozen[0] == sys.executable
    assert "--runtime-stdio" in frozen

    monkeypatch.delattr(sys, "frozen")
    normal = client.default_argv(None, stream=True)
    assert "-m" in normal and "agent_runtime.main" in normal, (
        f"源码 / 安装那一支仍然要走 -m（它比绝对路径稳，见 client.py）：{normal}"
    )
