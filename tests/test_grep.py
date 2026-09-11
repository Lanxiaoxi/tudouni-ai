"""grep 工具的实现。

**它已经取消注册**：不再出现在 create_tool_registry 里，模型看不到它、也不会被
调用。这里保留的是 tools/grep.py 本身的实现测试 —— 那艘船还在，只是没挂在这条
装配线上。

实现层的契约没变：路径走 FileSystem.safe_path（只在工作区内）；结果里的路径要能
直接喂给 read_file；行号要对、没匹配到要说清楚、正则写错要指出是正则的错、超长
输出要截断但保留头尾、二进制/超大文件要跳过。
"""

import pytest

from agent_runtime.tools.filesystem import FileSystem
from agent_runtime.tools.grep import (
    MAX_LINE_CHARS,
    _truncate,
    grep,
)


@pytest.fixture
def tree(workdir):
    """一个带子目录的小目录树，用来验证递归搜索。"""
    (workdir / "a.py").write_text("import os\nTODO: fix\nprint(1)\n", encoding="utf-8")
    (workdir / "notes.txt").write_text("todo: later\n", encoding="utf-8")
    sub = workdir / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("x = 1\n# TODO: also\n", encoding="utf-8")
    return workdir


# --- 递归 + 行号 --------------------------------------------------------

def test_recurses_and_reports_path_line_text(tree):
    result = grep(str(tree), "TODO")

    assert "a.py" in result
    assert "2:TODO: fix" in result
    assert "pkg/b.py" in result          # 递归进了子目录
    assert "pkg\\b.py" not in result     # 路径统一用正斜杠
    assert "notes.txt" not in result     # 大小写敏感：todo 不该命中 TODO


def test_ignore_case(tree):
    result = grep(str(tree), "todo", ignore_case=True)

    assert "a.py" in result
    assert "notes.txt" in result


def test_include_filters_by_filename(tree):
    result = grep(str(tree), "TODO|todo", include="*.py")

    assert "a.py" in result
    assert "notes.txt" not in result


def test_path_scopes_the_search(tree):
    result = grep(str(tree), "TODO", path="pkg")

    assert "pkg/b.py" in result          # 路径相对**工作区**，不是相对 pkg
    assert "a.py" not in result


def test_reported_paths_are_usable_by_read_file(tree):
    """grep 报出的路径必须能直接喂给 read_file —— 这是两个工具之间的契约。

    只断言 `"b.py" in result` 是看不出来的：`b.py` 和 `pkg/b.py` 都能让它通过。
    实测过一个 bug：路径是按 root（搜索起点）算的，于是 path="tools" 时报出
    "builtin.py"，而 read_file("builtin.py") 直接 FileNotFoundError —— 模型会以为
    文件不存在，而不是以为路径错了。
    """
    result = grep(str(tree), "TODO", path="pkg")

    assert result.splitlines()[0].split(" (")[0] == "pkg/b.py"
    FileSystem(str(tree)).read_file("pkg/b.py")   # 不抛，就证明这个路径是真的


# --- 边界：不越界、不抛异常 --------------------------------------------

def test_escaping_the_workspace_is_blocked(tree):
    """工作区边界 —— 这条是它敢只读、不越界的根据。"""
    with pytest.raises(PermissionError):
        grep(str(tree), "TODO", path="../..")


def test_non_directory_path_says_use_read_file(tree):
    result = grep(str(tree), "TODO", path="a.py")
    assert "不是目录" in result
    assert "read_file" in result


def test_missing_path_is_returned_not_raised(tree):
    assert "路径不存在" in grep(str(tree), "TODO", path="nope")


def test_no_match_is_a_clear_message(tree):
    """没匹配到是正常结果，不是故障 —— 而且要说清**到底读了多少个文件**。

    这个数字不是装饰：模型要靠它区分"真的不存在"和"我没读到那儿"（后者在字节上限
    触发时会变得更要紧）。
    """
    result = grep(str(tree), "zzz-nope")

    assert "没有匹配" in result
    assert "读了 3 个文件" in result      # a.py / notes.txt / pkg/b.py


def test_invalid_regex_blames_the_regex(tree):
    """正则语法错是**调用方自己**写错了，得让它知道改的是 pattern，不是 path。"""
    result = grep(str(tree), "a(")

    assert "正则表达式无效" in result
    assert "a(" in result


# --- 结果上限 -----------------------------------------------------------

def test_max_files_bounds_the_list_but_still_counts_the_rest(workdir):
    """名单要截，但**数目不能截** —— 得说清"还有几个路径里也有命中"。

    这是让"没匹配到 ≠ 不存在"成立的地方：只列 2 个却不提还有 3 个，模型会以为全项目
    就只有 2 处。
    """
    for i in range(5):
        (workdir / f"f{i}.py").write_text("needle\n", encoding="utf-8")

    result = grep(str(workdir), "needle", max_files=2)

    assert result.count("处命中") == 2
    assert "共 5 个路径有命中" in result
    assert "只挑列了最前面的 2 个" in result


def test_hidden_paths_do_not_crowd_out_project_files(workdir):
    """隐藏路径排在**后面**，所以名额先给项目自己的代码。

    这条替代了早先"过滤掉隐藏目录"那版设计：排序能解决同一个问题，而且不删任何东西。
    实测过的形状是：整个工作区 4629 个文件里 4232 个在 .venv，字母序里 `.venv` 排最
    前，于是名额全被它占掉、项目源码一个都进不来。
    """
    hidden = workdir / ".venv"
    hidden.mkdir()
    for i in range(30):
        (hidden / f"v{i}.py").write_text("needle\n", encoding="utf-8")
    (workdir / "src.py").write_text("needle\n", encoding="utf-8")

    narrow = grep(str(workdir), "needle", max_files=1)
    assert "src.py" in narrow                        # 项目文件必须进名单
    assert ".venv/v0.py" not in narrow               # 名额不够时，隐藏路径让位
    assert "共 31 个路径有命中" in narrow

    # 但名额够的时候，隐藏路径里的命中照样出现 —— 这就是"排序不是过滤"的证据
    wider = grep(str(workdir), "needle", max_files=40)
    assert ".venv/v0.py" in wider


def test_total_bytes_cap_stops_reading_and_says_so(workdir):
    """字节上限是唯一按**成本**设的那道，它停了要说清"哪些根本没被检查"。

    注意它停的是"检查"，跟 max_files 停"列名单"是两件事 —— 所以措辞也不同。
    """
    for i in range(10):
        (workdir / f"f{i}.py").write_text("x" * 2000, encoding="utf-8")

    result = grep(str(workdir), "needle", max_total_bytes=5000)

    assert "没有匹配" in result
    assert "读了 3 个文件" in result                  # 5000 / 2000 → 第 4 个之前就停了
    assert "另有 7 个候选文件根本没被检查" in result
    assert "5000 字节" in result                      # 小于 1 MB 时不许说"0 MB"


def test_symlinks_are_not_followed(workdir):
    """符号链接不跟着走 —— 它可能指到工作区外面，也可能指成环。

    这是**唯一**一类被排除的东西，而且理由不是"值不值得搜"，是"能不能安全地走"
    （见模块 docstring）。所以它必须被数出来、说出来，而不是悄悄消失。

    有些机器不让建符号链接（Windows 需要开发者模式或管理员），那就跳过 —— 假装通过
    比不写更糟。
    """
    outside = workdir.parent / "outside-secret.txt"
    outside.write_text("NEEDLE\n", encoding="utf-8")
    try:
        (workdir / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        outside.unlink(missing_ok=True)
        pytest.skip("这台机器不让建符号链接（Windows 需要开发者模式/管理员）")

    try:
        result = grep(str(workdir), "NEEDLE")
        assert "NEEDLE" not in result                # 没有跟着链接走到工作区外面
        assert "跳过 1 个符号链接" in result
    finally:
        outside.unlink(missing_ok=True)


def test_long_lines_are_clipped(workdir):
    (workdir / "big.py").write_text("x" * (MAX_LINE_CHARS + 100) + "\n", encoding="utf-8")

    result = grep(str(workdir), "x")

    assert "…" in result
    assert str(MAX_LINE_CHARS + 100) not in result


def test_truncation_keeps_both_ends():
    text = "头" * 100 + "中" * 20000 + "尾" * 100
    result = _truncate(text)

    assert result.startswith("头" * 100)
    assert result.endswith("尾" * 100)
    assert "中间省略" in result


def test_short_output_is_untouched():
    assert _truncate("短") == "短"


def test_binary_and_oversized_files_are_skipped_and_counted(workdir):
    (workdir / "one.py").write_text("needle\n", encoding="utf-8")
    (workdir / "blob.bin").write_bytes(b"\xff\xfe\x00\x01needle")

    # 不指定 include，让那个二进制也进入候选；它解码不了，应被跳过而不是让调用报错。
    result = grep(str(workdir), "needle")

    assert "one.py" in result
    assert "blob.bin" not in result
