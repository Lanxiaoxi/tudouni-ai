"""控制面：agent 能写工作区里的任何东西，**除了 `.tudouni/`**。

这条边界和 safe_path 那条是方向相反的两个问题：safe_path 防它出去，这里防它进来。
之所以值得单独钉住，是因为 write_file 的边界正好是整个工作区，而运行期的私有目录
就住在工作区根部 —— 能写它，就等于能给自己发权限、伪造批准记录、抹掉审计证据，
或者给自己换一套行为指令（技能）。

所以这里的断言都盯在**文件有没有真的被建出来**，而不是只看抛没抛异常：一个"先写入
再报错"的实现也能让异常看起来正确。

edit_file 走的是同一条 writable_path，所以这里也把它钉住一遍 —— 加一个能写文件的
工具，最容易漏的就是控制面那条检查（write_file 挡住 ≠ edit_file 也挡住了）。
"""

import pytest

from agent_runtime.tools.builtin.filesystem import CONTROL_PLANE, FileSystem


@pytest.fixture
def fs(workdir):
    return FileSystem(str(workdir))


# --- 挡住 ---------------------------------------------------------------

def test_write_file_refuses_the_permission_file(fs, workdir):
    """能写它就能把 shell 加进免审批名单 —— 那就不叫权限策略了。"""
    with pytest.raises(PermissionError, match="control plane"):
        fs.write_file(".tudouni/permissions.json", '{"auto_approve_tools": ["shell"]}')

    assert not (workdir / ".tudouni" / "permissions.json").exists()


@pytest.mark.parametrize("path", [
    ".tudouni/sessions/20250101-000000.json",   # 能写它就能伪造"用户批准过"
    ".tudouni/logs/20250101-000000.jsonl",      # 能写它就能抹掉"谁批准了什么"
    ".tudouni/skills/evil/SKILL.md",            # 能写它就能给自己换一套行为指令
    ".tudouni/permissions.json",
    ".tudouni",                                 # 目录本身也不行
])
def test_write_file_refuses_the_runtime_dir(fs, workdir, path):
    with pytest.raises(PermissionError, match="control plane"):
        fs.write_file(path, "x")


def test_every_entry_in_the_table_is_actually_enforced(fs):
    """表里的每一项都要真的被挡住。

    加一项到 CONTROL_PLANE 却忘了接上检查，是这张表唯一会静默失效的方式 ——
    所以这里遍历表本身，而不是抄一份名字。
    """
    for name in CONTROL_PLANE:
        with pytest.raises(PermissionError):
            fs.write_file(name, "x")
        with pytest.raises(PermissionError):
            fs.write_file(f"{name}/inside.txt", "x")


def test_the_check_does_not_depend_on_case(fs, workdir):
    """Windows 的文件系统不区分大小写，`.TUDOUNI/` 写下去就是同一个目录。"""
    with pytest.raises(PermissionError):
        fs.write_file(".TUDOUNI/permissions.json", "x")

    assert not (workdir / ".tudouni").exists()


def test_escaping_the_workspace_is_still_refused(fs):
    """原有的那条边界不能被这次改动弄松。"""
    with pytest.raises(PermissionError, match="escapes workspace"):
        fs.write_file("../evil.txt", "x")


# --- edit_file 走的是同一条检查 ------------------------------------------

@pytest.mark.parametrize("path", [
    ".tudouni/permissions.json",
    ".tudouni/sessions/20250101-000000.json",
    ".tudouni/logs/20250101-000000.jsonl",
])
def test_edit_file_also_refuses_the_control_plane(fs, workdir, path):
    """edit_file 是另一个能改文件的工具，控制面必须同样挡住它。

    这里让文件先**真的存在**（否则 edit 会先在"文件不存在"那一支返回 —— 那也能
    "没改成"，但测的就不是控制面那条检查了）。
    """
    target = workdir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("原始内容", encoding="utf-8")

    with pytest.raises(PermissionError, match="control plane"):
        fs.edit_file(path, "原始内容", "被改掉了")

    assert target.read_text(encoding="utf-8") == "原始内容"


def test_edit_file_still_refuses_escaping_the_workspace(fs):
    with pytest.raises(PermissionError, match="escapes workspace"):
        fs.edit_file("../evil.txt", "a", "b")


# --- 放行 ---------------------------------------------------------------

def test_reading_the_control_plane_is_still_allowed(fs, workdir):
    """只挡写。读没有破坏性，而 agent 看不见自己项目的策略只会反复重试。

    技能正文更是必须读得到（它连同目录的附件一起被 read_file 读）。
    """
    (workdir / ".tudouni").mkdir(parents=True, exist_ok=True)
    (workdir / ".tudouni" / "permissions.json").write_text(
        '{"auto_approve": ["low"]}', encoding="utf-8"
    )

    assert "auto_approve" in fs.read_file(".tudouni/permissions.json")
    assert ".tudouni" in fs.list_files(".")


def test_normal_writes_still_work(fs, workdir):
    assert fs.write_file("notes/a.md", "内容") == "Written: notes/a.md"
    assert (workdir / "notes" / "a.md").read_text(encoding="utf-8") == "内容"


def test_a_nested_copy_of_the_name_is_not_the_control_plane(fs, workdir):
    """控制面是"根目录那个目录"，不是"叫这个名字的任何东西"。

    挡住 sub/.tudouni/ 只会让模型读不懂为什么被拒 —— 而那不是运行期目录。
    """
    fs.write_file("sub/.tudouni/permissions.json", "{}")
    fs.write_file("sub/sessions/notes.md", "x")   # 少一层也不是
    fs.write_file("tudouni.json", "{}")           # 少一个点也不是

    assert (workdir / "sub" / ".tudouni" / "permissions.json").exists()
