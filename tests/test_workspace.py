"""工作区是哪个目录，以及**哪些目录不许当工作区**。

工作区就是 cwd（`paths.workspace_dir()`）。这一条本身很短，但它带着一个必须一起成立的
后果：**agent 的文件围栏跟着工作区走**，所以工作区选得越大，那道围栏就越接近一句空话。

在 `~` 下敲一次命令，围栏圈住的是整个 home —— `.ssh/`、别的项目的 `.env`、浏览器的
cookie 库全在里面，而 `read_file` 是 LOW（**免审批**）。所以这里的判据不是"配置得好不好"，
是"这一道边界还在不在"。

两层分工，测试也分两层：

  * `paths.unsafe_workspace()` —— **判据**，返回一个原因码（机器认的）；
  * `composition.check_workspace()` —— **措辞**，把原因码翻成一句能照着做的话。

和 `Notice.code` / `Notice.text`、`is_valid_session_id` / `check_session_id` 是同一条分工。
"""

from pathlib import Path

import pytest

from agent_runtime import paths
from agent_runtime.runtime.composition import check_workspace


# --- 判据 ---------------------------------------------------------------------

def test_a_plain_directory_is_fine(workdir, monkeypatch):
    """**除了那三种，一律放行。**

    一个空目录、一个没有 `.git` 的目录、`/tmp` 下随手建的目录都是正常用法。刻意不加
    "这里看起来不像项目"那种启发式判断：拒绝的理由必须是说得出口的事实，而猜错的下场是
    逼人去绕过整个机制。
    """
    monkeypatch.chdir(workdir)

    assert paths.unsafe_workspace() == ""
    assert check_workspace() is None


def test_the_home_directory_is_refused(workdir, monkeypatch):
    """cwd 就是 home ⇒ 拒绝。

    这是**最容易犯**的那一种：新开一个终端默认就在 home。所以它必须是一道门，不是
    启动时的一条注脚 —— 注脚出现在人正准备打第一句话的时候，没有人会读。
    """
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))  # Path.home() 在 Windows 上看这个
    monkeypatch.chdir(workdir)

    assert paths.unsafe_workspace() == paths.UNSAFE_HOME


def test_the_filesystem_root_is_refused():
    """cwd 是根 ⇒ 拒绝。**判据是"父目录就是自己"，不是字符串比 `/`。**

    写死 `/` 会在 Windows 上漏掉 `C:\\`、在 UNC 路径上漏掉 `\\\\server\\share\\`，
    而漏掉的表现是"它居然让我在 C 盘根目录上跑起来了"。

    这条不 chdir 到真的根目录去（那会污染整个测试进程的 cwd，而 pytest 之后还要读
    相对路径）—— 判据接受一个显式路径，正是为了能这么测。
    """
    root = Path(Path.cwd().anchor or "/")

    assert paths.unsafe_workspace(root) == paths.UNSAFE_ROOT


def test_a_directory_above_home_is_refused(workdir, monkeypatch):
    """cwd 在 home 的上层（`/home`、`/Users`、`C:\\Users`）⇒ 拒绝。

    它比"就是 home"更坏一档：交出去的不是自己那一个 home，是**这台机器上每个人的**。
    """
    home = workdir / "users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert paths.unsafe_workspace(workdir / "users") == paths.UNSAFE_ABOVE_HOME
    # 再上一层也算（`home.parents` 是整条链）。
    assert paths.unsafe_workspace(workdir) == paths.UNSAFE_ABOVE_HOME


def test_a_sibling_of_home_is_fine(workdir, monkeypatch):
    """home 的**兄弟**目录没问题 —— 拒绝的是"包含 home"，不是"挨着 home"。

    这一条挡的是把判据写成"路径里有 home 的名字"那种实现：`/home/echuzhi-backup`
    不该被拒，而它和 `/home/echuzhi` 只差几个字符。
    """
    home = workdir / "home" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    assert paths.unsafe_workspace(workdir / "home" / "me-backup") == ""


# --- 措辞 ---------------------------------------------------------------------

def test_the_refusal_says_where_why_and_what_to_do(workdir, monkeypatch):
    """拒绝那句话必须说清**三件事**：这是哪儿、为什么不行、下一步敲什么。

    只说"拒绝"的报错会让人以为程序坏了，然后去翻源码 —— 而这是一个**用户做点事就能
    解决**的情况（`cd` 一下），和缺密钥同一档。
    """
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))
    monkeypatch.chdir(workdir)

    problem = check_workspace()

    assert problem is not None
    assert str(workdir) in problem          # 这是哪儿
    assert "home" in problem                # 为什么不行
    assert "read_file" in problem           # 代价说清了：免审批的那个工具
    assert "cd" in problem                  # 下一步敲什么


@pytest.mark.parametrize("reason", [
    paths.UNSAFE_HOME, paths.UNSAFE_ROOT, paths.UNSAFE_ABOVE_HOME,
])
def test_every_reason_code_has_a_sentence(reason, monkeypatch):
    """**每个原因码都要有一句对应的话。**

    `check_workspace` 用一个 dict 按原因码取措辞，所以新加一个码而忘了加措辞会是
    `KeyError` —— 那发生在启动路径上，症状是"程序在拒绝我的时候自己崩了"。
    这条测试让那种遗漏在测试里就现形。
    """
    monkeypatch.setattr(paths, "unsafe_workspace", lambda *a, **k: reason)

    problem = check_workspace()

    assert problem is not None and problem.strip()
