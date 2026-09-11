"""edit_file：只改一段，而不是把整篇正文背回来写回去。

这组测试盯的是**另一种失败形态**：write_file 是整文件覆盖，改一行也要模型输出整篇
正文，背错一个字就是静默丢数据。edit_file 的价值全在"文件里其余内容不经模型的手"，
所以每条断言都要落回"盘上那份文件到底变成什么样"，而不是只看返回了哪句话。
"""

from agent_runtime.tools.filesystem import FileSystem


def fs_in(workdir) -> FileSystem:
    return FileSystem(str(workdir))


def write(workdir, name: str, text: str):
    (workdir / name).write_text(text, encoding="utf-8")


def read(workdir, name: str) -> str:
    return (workdir / name).read_text(encoding="utf-8")


# --- 成功 -----------------------------------------------------------------

def test_replaces_a_single_occurrence(workdir):
    fs = fs_in(workdir)
    write(workdir, "a.py", "x = 1\ny = 2\nz = 3\n")

    fs.edit_file("a.py", "y = 2", "y = 20")

    assert read(workdir, "a.py") == "x = 1\ny = 20\nz = 3\n"


def test_replacement_preserves_everything_else(workdir):
    """其余内容**逐字节不变** —— 这正是它相对 write_file 的意义。

    模型只提供了 old/new 两段，文件里没被这两段覆盖的部分根本不该有机会被改坏。

    但这条夹具用 write_text 写字：在 Windows 上它自己就把换行翻成了 CRLF，所以这里
    实际只证明了 CRLF 文件的情形。LF 文件和混合换行见下面那一组 —— 它们必须用
    write_bytes 造夹具，否则断言永远绿（那正是这个 bug 藏得住的原因）。
    """
    fs = fs_in(workdir)
    body = "".join(f"line {i}\n" for i in range(200))
    write(workdir, "big.txt", body)

    fs.edit_file("big.txt", "line 100\n", "changed\n")

    assert read(workdir, "big.txt") == body.replace("line 100\n", "changed\n")


# --- 换行：没被替换的字节必须原样 -----------------------------------------
#
# read_text/write_text 会在两边各翻译一次换行（读时 CRLF→LF，写时 LF→os.linesep）。
# 于是编辑一个 LF 文件里的**一行**，整份文件的换行全变 —— 而 autocrlf=true 时 git 会把
# 两边都归一，`git diff` 只显示那一行，所以它极难被发现。这一组盯的就是它。

def test_lf_file_stays_lf(workdir):
    """只改一行，其余行的换行不能跟着变。"""
    fs = fs_in(workdir)
    (workdir / "a.txt").write_bytes(b"one\ntwo\nthree\n")

    fs.edit_file("a.txt", "two", "TWO")

    assert (workdir / "a.txt").read_bytes() == b"one\nTWO\nthree\n"


def test_crlf_file_can_be_edited_with_lf_snippets(workdir):
    """模型手里的片段来自 read_file，那里的换行已经被归一成 LF。

    所以对齐必须是双向的：CRLF 文件要能被 LF 的片段匹配上，而且**改完还是 CRLF**。
    只顾一头就会把另一半弄坏 —— 要么 CRLF 文件改不动，要么 LF 文件被整份翻掉。
    """
    fs = fs_in(workdir)
    (workdir / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    fs.edit_file("a.txt", "one\ntwo", "1\n2")      # 片段是 LF 的

    assert (workdir / "a.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


def test_mixed_endings_each_keep_their_own(workdir):
    """混合换行的文件里，只有被替换的那一段变 —— 其余每一行保留自己的换行。"""
    fs = fs_in(workdir)
    (workdir / "a.txt").write_bytes(b"crlf\r\nlf\ntarget\ncrlf2\r\n")

    fs.edit_file("a.txt", "target", "TARGET")

    assert (workdir / "a.txt").read_bytes() == b"crlf\r\nlf\nTARGET\ncrlf2\r\n"


def test_empty_new_string_deletes_the_snippet(workdir):
    fs = fs_in(workdir)
    write(workdir, "a.txt", "keep\nremove\nalso keep\n")

    fs.edit_file("a.txt", "remove\n", "")

    assert read(workdir, "a.txt") == "keep\nalso keep\n"


def test_replace_all_rewrites_every_occurrence(workdir):
    fs = fs_in(workdir)
    write(workdir, "a.txt", "old old old\n")

    fs.edit_file("a.txt", "old", "new", replace_all=True)

    assert read(workdir, "a.txt") == "new new new\n"


def test_multiline_snippets_work(workdir):
    """跨行替换是常见用法 —— 它要求 old_string 里的换行和文件里的一致。"""
    fs = fs_in(workdir)
    write(workdir, "a.py", "def f():\n    return 1\n\n\ndef g():\n    return 2\n")

    fs.edit_file("a.py", "def f():\n    return 1\n", "def f():\n    return 42\n")

    assert read(workdir, "a.py") == "def f():\n    return 42\n\n\ndef g():\n    return 2\n"


# --- 拒绝改（但都不算工具故障） -------------------------------------------

def test_ambiguous_match_changes_nothing_and_says_why(workdir):
    """多处命中时**什么都不做** —— 替模型猜"改哪一处"就是静默改错地方。"""
    fs = fs_in(workdir)
    write(workdir, "a.txt", "x\nx\n")
    before = read(workdir, "a.txt")

    message = fs.edit_file("a.txt", "x", "y")

    assert read(workdir, "a.txt") == before      # 一个字都没动
    assert "2" in message and "replace_all" in message


def test_missing_old_string_points_back_at_read_file(workdir):
    """对不上的典型原因是模型凭记忆写片段 —— 要把它引回 read_file。"""
    fs = fs_in(workdir)
    write(workdir, "a.txt", "actual content\n")
    before = read(workdir, "a.txt")

    message = fs.edit_file("a.txt", "imagined content", "new")

    assert read(workdir, "a.txt") == before
    assert "read_file" in message


def test_missing_file_tells_the_model_to_use_write_file(workdir):
    fs = fs_in(workdir)
    message = fs.edit_file("nope.txt", "a", "b")

    assert "write_file" in message
    assert not (workdir / "nope.txt").exists()


def test_same_old_and_new_is_refused_not_a_silent_noop(workdir):
    """不做任何事也要说出来 —— 否则模型会以为改成功了，然后接着往下走。"""
    fs = fs_in(workdir)
    write(workdir, "a.txt", "same\n")

    message = fs.edit_file("a.txt", "same", "same")

    assert "没有变化" in message


def test_binary_file_is_refused(workdir):
    """非 UTF-8 读不进来 —— 报清楚，别让 UnicodeDecodeError 冒到 agent 那层。"""
    fs = fs_in(workdir)
    (workdir / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")

    message = fs.edit_file("bin.dat", "x", "y")

    assert "UTF-8" in message


def test_editing_a_directory_is_refused(workdir):
    fs = fs_in(workdir)
    (workdir / "sub").mkdir()

    assert "list_files" in fs.edit_file("sub", "a", "b")


# --- 边界：edit 和 write 走同一条 ----------------------------------------

def test_edit_respects_the_workspace_boundary(workdir):
    """edit_file 不是绕过 safe_path 的后门。"""
    fs = fs_in(workdir)
    (workdir.parent / "outside.txt").write_text("secret", encoding="utf-8")

    try:
        fs.edit_file("../outside.txt", "secret", "leaked")
    except PermissionError:
        pass
    else:
        raise AssertionError("越界写必须被拒绝")

    assert (workdir.parent / "outside.txt").read_text(encoding="utf-8") == "secret"
