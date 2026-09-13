"""grep 工具的实现（引擎是随仓库带的 ripgrep）。

**它已经注册了**（见 `create_tool_registry`），所以这个文件守的是两件事：

  1. **实现层的契约**（换引擎不该动它）：路径走 `FileSystem.safe_path`（只在工作区内）；
     结果里的路径要能直接喂给 read_file；行号要对；没匹配到要说清楚读了多少个文件；
     正则写错要指出是正则的错；超长输出要截断但保留头尾。
  2. **这次换引擎新引入的那几条**：不筛任何文件（ripgrep 的默认行为恰好就是筛）、
     pattern 永远不会被当成旗标、引擎的配置文件读不进来、超时是响的、
     以及"这台机器上引擎真的在仓库里"。

第 2 组里前三条都是**实测出来的**，不是照着文档写的：`--needle--` 会被当成旗标
（`unrecognized flag`）、`RIPGREP_CONFIG_PATH` 会被读取（往里写 `--files-with-matches`
输出形状立刻就变）、`--pre=COMMAND` 会真的去 spawn 那个命令。它们决定了 argv 长什么样。
"""

import pytest

from agent_runtime.tools.builtin import grep as grep_module
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.filesystem import FileSystem
from agent_runtime.tools.builtin.grep import (
    MAX_LINE_CHARS,
    MAX_MATCHES_PER_FILE,
    _truncate,
    grep,
    host_triple,
    rg_binary,
)
from agent_runtime.tools.tool import RiskLevel


@pytest.fixture
def tree(workdir):
    """一个带子目录的小目录树，用来验证递归搜索。"""
    (workdir / "a.py").write_text("import os\nTODO: fix\nprint(1)\n", encoding="utf-8")
    (workdir / "notes.txt").write_text("todo: later\n", encoding="utf-8")
    sub = workdir / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("x = 1\n# TODO: also\n", encoding="utf-8")
    return workdir


# --- 引擎本身：它在不在、注册成了什么 --------------------------------

def test_the_engine_is_vendored_for_this_platform():
    """受支持的平台上，引擎必须真的在仓库里 —— 它是"不对外有依赖"那句话的兑现。

    这条红了不代表代码坏了，代表**这份检出是缺件的**：`tools/vendor/rg/` 里少了本机
    平台那一份。补它的命令在断言里。
    """
    if host_triple() is None:
        pytest.skip("这个平台不在 tools/builtin/grep.py 的 _TRIPLES 里")
    assert rg_binary() is not None, (
        "tools/vendor/rg/ 里没有本机平台的 ripgrep：跑 "
        "`uv run python scripts/fetch_rg.py` 补上（见 tools/vendor/rg/README.md）"
    )


def test_missing_engine_returns_text_not_an_exception(workdir, monkeypatch):
    """引擎不在了也要**返回文本**，而且要说清去哪儿补。

    走到这个分支说明注册表是在引擎还在的时候造的、之后文件没了（正常装配下
    create_tool_registry 压根不会注册它）。抛异常会被记成"工具故障"，而模型对这个
    分支能做的只有一件事：把"环境不完整"这句话转述给用户。
    """
    monkeypatch.setattr(grep_module, "rg_binary", lambda: None)

    result = grep(str(workdir), "whatever")

    assert "fetch_rg.py" in result
    assert "找不到" in result or "不在" in result


def test_registered_as_low_risk_and_parallel_safe(workdir):
    """LOW 是它敢存在的理由，parallel_safe 是它比 shell 值钱的地方。

    LOW：搜文本是极其常规的只读动作，每次都要人工审批才是错配（见模块 docstring）。
    parallel_safe：它不写工作区、不碰共享状态 —— 一批 read_file + grep 能真并发，
    而搜索花的是等磁盘的时间。
    """
    tool = create_tool_registry(str(workdir)).get("grep")

    assert tool.risk is RiskLevel.LOW
    assert tool.parallel_safe is True


def test_grep_args_field_names_reach_the_handler(workdir):
    """注册表到 handler 之间那条缝：字段名没人钉住。

    这里有**两份**独立的事实 —— GrepArgs 的字段名，和 grep() 的参数名 —— 而它们只在
    `Grep.__call__` 的 `**arguments` 展开时相遇。名字一漂移（include 改名、max_files
    写成 maxFiles），真实会话里会抛 TypeError 变成"工具执行失败"，而 test_prompt.py
    那几条（测注册表元数据）和上面这条（测 risk）都还是绿的。
    """
    (workdir / "a.py").write_text("TODO: fix\n", encoding="utf-8")
    (workdir / "b.txt").write_text("TODO: fix\n", encoding="utf-8")

    result = create_tool_registry(str(workdir)).get("grep").execute(
        {"pattern": "TODO", "path": ".", "include": "*.py", "ignore_case": False,
         "max_files": 5}
    )

    assert "a.py" in result
    assert "b.txt" not in result      # include 真的到了 handler


# --- 不筛任何文件：换引擎之后最容易悄悄坏掉的一条 ----------------------

def test_nothing_is_filtered(workdir):
    """**ripgrep 的默认行为恰好就是"筛"**，所以这一条是 `--no-ignore*` / `--hidden`
    那串旗标的看门测试。少一个，被 `.gitignore` 掉的、藏在点目录里的就静默消失了 ——
    而"静默漏搜"正是这个工具最不能有的失败形态（模型无法与"真的不存在"区分开）。

    三处都要在：`.gitignore` 点名的文件、隐藏目录里的文件、以及 `.git` 自己（第三方
    源码和运行期数据都在这一类里，见模块 docstring 里那段"值不值得搜"）。
    """
    (workdir / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (workdir / "ignored.py").write_text("needle\n", encoding="utf-8")
    hidden = workdir / ".venv"
    hidden.mkdir()
    (hidden / "v.py").write_text("needle\n", encoding="utf-8")
    git = workdir / ".git"
    git.mkdir()
    (git / "config").write_text("needle\n", encoding="utf-8")

    result = grep(str(workdir), "needle")

    assert "ignored.py" in result        # .gitignore 说的话不算数
    assert ".venv/v.py" in result        # 点目录不是过滤条件
    assert ".git/config" in result       # .git 也不是


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
    """没匹配到是正常结果，不是故障 —— 而且要说清**到底搜了多少个文件**。

    这个数字不是装饰：模型要靠它区分"真的不存在"和"我没搜到那儿"。它现在来自引擎
    末尾那条 summary（`searches`），而不是我们数出来的 —— 引擎把二进制文件也算作
    搜过，自己数会漏掉那些。
    """
    result = grep(str(tree), "zzz-nope")

    assert "没有匹配" in result
    assert "搜了 3 个文件" in result      # a.py / notes.txt / pkg/b.py


def test_invalid_regex_blames_the_regex(tree):
    """正则语法错是**调用方自己**写错了，得让它知道改的是 pattern，不是 path。

    引擎的原话（带 caret 示意图）一并交回去：模型要改的就是那个 pattern，而
    "unclosed group" 这种话比我们转述一句"正则无效"有用得多。
    """
    result = grep(str(tree), "a(")

    assert "正则表达式无效" in result
    assert "a(" in result
    assert "regex parse error" in result


# --- 注入面：这三条是 argv 长成那样的全部理由 --------------------------

def test_pattern_is_never_taken_as_a_flag(workdir):
    """pattern 只走 `-e`。

    不这么做的话，模型写一个 `--pre=...` 就能让引擎去执行一条命令 —— 一次**绕过
    shell 审批**的命令执行（shell 是 HIGH，每次都要人看一眼）。这条测试钉的是
    "pattern 永远只是一个正则"：那个文本被当成正则去搜，而不是被当成旗标。
    """
    (workdir / "dash.txt").write_text("--pre=calc.exe\n", encoding="utf-8")

    result = grep(str(workdir), "--pre=calc.exe")

    assert "dash.txt" in result
    assert "1:--pre=calc.exe" in result      # 当作正则命中，而不是 unrecognized flag


def test_engine_config_file_is_not_read(workdir, monkeypatch):
    """引擎会读 `RIPGREP_CONFIG_PATH` 指向的文件（实测过），`--no-config` 是那道闸。

    少了它，一个外部配置文件就能改掉这次调用的形状 —— 实测往里写一句
    `--files-with-matches`，输出就从"文件:行号:内容"变成只剩文件名。配置文件里同样
    能塞 `--pre`，所以这不是"输出好看不好看"的问题。
    """
    config = workdir / "rgrc"
    config.write_text("--files-with-matches\n", encoding="utf-8")
    (workdir / "a.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))

    result = grep(str(workdir), "needle")

    assert "1:needle" in result          # 行号和行内容都还在 → 配置没生效


def test_timeout_is_loud_and_returns_nothing(workdir):
    """超时是唯一"什么都不返回"的分支，而且是故意的。

    返回半份命中而不说清，等于让模型把"被我掐断了"读成"就这么多" —— 那是这个工具
    最不能有的失败形态。说清超时，它就知道该缩小 path 或者加 include。
    """
    (workdir / "a.txt").write_text("needle\n", encoding="utf-8")

    result = grep(str(workdir), "needle", timeout_seconds=0.000001)

    assert "超过了" in result
    assert "没有任何结果" in result
    assert "include" in result           # 说清下一步该怎么办


# --- 结果上限 -----------------------------------------------------------

def test_max_files_bounds_the_list_but_still_counts_the_rest(workdir):
    """名单要截，但**数目不能截** —— 得说清"还有几个路径里也有命中"。

    这是让"没匹配到 ≠ 不存在"成立的地方：只列 2 个却不提还有 3 个，模型会以为全项目
    就只有 2 处。数目来自引擎的 summary，所以名单截在哪儿都不影响它。
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

    换引擎之后它更要紧了：ripgrep 是并行搜的，交回来的顺序本来就不保证 —— 名额截到
    哪几个如果跟着到达顺序走，同一次搜索会有不同结果。排序必须由我们这一侧定。
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


def test_per_file_match_cap_says_so(workdir):
    """每个文件的命中条数有上限，而且**到顶了要说**：不然模型会把"列了 20 条"
    读成"这个文件就 20 处"。

    上限多要一条（`--max-count = 上限 + 1`）正是为了分清"正好 20 条"和"还有更多"
    —— 只要 20 条的话，两种情况的输出一模一样。
    """
    (workdir / "many.txt").write_text(
        "".join(f"needle {i}\n" for i in range(MAX_MATCHES_PER_FILE + 5)), encoding="utf-8"
    )

    result = grep(str(workdir), "needle")

    assert f"({MAX_MATCHES_PER_FILE} 处命中)" in result
    assert f"命中已到 {MAX_MATCHES_PER_FILE} 条上限" in result


def test_symlinks_are_not_followed(workdir):
    """符号链接不跟着走 —— 它可能指到工作区外面，也可能指成环。

    这是**唯一**一类被排除的东西，而且理由不是"值不值得搜"，是"能不能安全地走"。
    引擎默认也不跟（`-L` 才跟），所以这条在两个引擎下都成立。

    注意这里**不再断言"跳过 N 个符号链接"那句注脚**：引擎不报它跳过了谁（summary
    里只有"搜了几个文件、几个有命中"）。这是换引擎付的代价，写在模块 docstring 里 ——
    要把它数出来就得自己再走一遍目录树，而那趟走路正是引擎帮我们省掉的东西。
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
    finally:
        outside.unlink(missing_ok=True)


def test_binary_files_are_not_searched_as_text(workdir):
    """二进制文件不该把里面的字节当文本报出来。"""
    (workdir / "one.py").write_text("needle\n", encoding="utf-8")
    (workdir / "blob.bin").write_bytes(b"\xff\xfe\x00\x01needle")

    result = grep(str(workdir), "needle")

    assert "one.py" in result
    assert "blob.bin" not in result


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
