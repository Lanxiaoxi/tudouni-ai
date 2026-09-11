"""grep 工具。

它和 shell 是同一个能力的两种走法，所以这组测试盯的第一件事是**分工**：
grep 走的是 FileSystem.safe_path 那条边界（只在工作区内），因此它是 LOW 风险、
默认自动放行；而"搜文本"这件事交给 shell 做就要每次人工审批。这条分工一旦退化
（比如 grep 不小心能搜到工作区外面），自动放行就变成了一个洞。

其余几条盯的是"结果能不能被模型用起来"：行号要对、没匹配到要说清楚、正则写错要
指出是正则的错、超长输出要截断但保留头尾、二进制/超大文件要跳过。
"""

import pytest

from agent_runtime.tools.builtin import GrepArgs, create_tool_registry
from agent_runtime.tools.grep import (
    MAX_LINE_CHARS,
    MAX_MAX_FILES,
    _truncate,
    grep,
)
from agent_runtime.tools.tool import RiskLevel

from fakes import ScriptedModel, tool_call, usage
from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import Decision, PermissionPolicy
from agent_runtime.state import Session


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
    registry = create_tool_registry(str(tree))
    result = registry.get("grep").execute({"pattern": "TODO", "path": "pkg"})

    assert result.splitlines()[0].split(" (")[0] == "pkg/b.py"
    registry.get("read_file").execute({"path": "pkg/b.py"})   # 不抛，就证明这个路径是真的


# --- 边界：不越界、不抛异常 --------------------------------------------

def test_escaping_the_workspace_is_blocked(tree):
    """工作区边界 —— 这条是 grep 能定 LOW 风险、自动放行的前提。"""
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
    """正则语法错是**模型自己**写错了，得让它知道改的是 pattern，不是 path。"""
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


# --- 装配层 -------------------------------------------------------------

def test_grep_is_low_risk_and_auto_approved():
    """它走的是文件沙箱那条路，所以能进 auto_approve —— 这正是它存在的理由。"""
    tool = create_tool_registry(".").get("grep")

    assert tool.risk is RiskLevel.LOW
    assert PermissionPolicy({RiskLevel.LOW}).decide(tool, {"pattern": "x"}) is Decision.ALLOW


def test_schema_shows_pattern_required_and_bounds():
    params = create_tool_registry(".").get("grep").parameters

    assert params["required"] == ["pattern"]
    assert params["properties"]["max_files"]["minimum"] == 1
    assert params["properties"]["max_files"]["maximum"] == MAX_MAX_FILES
    assert params["properties"]["path"]["default"] == "."


def test_extra_arguments_are_rejected():
    with pytest.raises(Exception) as exc:
        create_tool_registry(".").get("grep").execute({"pattern": "x", "bogus": 1})
    assert "bogus" in str(exc.value)


def test_max_files_out_of_range_is_rejected():
    with pytest.raises(Exception):
        GrepArgs(pattern="x", max_files=0)
    with pytest.raises(Exception):
        GrepArgs(pattern="x", max_files=MAX_MAX_FILES + 1)


def test_agent_can_use_grep_without_approval(workdir):
    """端到端：默认策略下 grep 被自动放行，模型能拿到真实命中。"""
    (workdir / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("grep", {"pattern": "def main"})],
                      usage=usage()),
        ModelResponse(content="找到 main", usage=usage()),
    ])
    agent = Agent(model, create_tool_registry(str(workdir)),
                  PermissionPolicy({RiskLevel.LOW}), asker=lambda t, a: False)
    session = Session.new("s")
    agent.run(session, "找 main")

    tool_result = [m for m in session.messages if m["role"] == "tool"][-1]["content"]
    assert "app.py" in tool_result
    assert "def main():" in tool_result
