"""命令前缀规则：一条命令行到底能不能被"人预先写下的前缀"覆盖。

这个模块不判断安全性 —— 它只做保守的匹配。所以这里的断言盯的是**三个方向的失败**：

  1. 该放行的别问（`git add -p x.py` 在规则 `git add` 下）；
  2. 该问的**绝不能**放行（`git status && rm -rf build` 在规则 `git` 下也必须问，
     只匹配第一段是这个功能最典型、也最危险的错法）；
  3. 拿不准的一律落到"问"（重定向、命令替换、引号不成对、推不出前缀）。

接了线的行为（关卡怎么用它、审计里记什么）在 test_command_permissions.py。
"""

import pytest

from agent_runtime.security.commands import (
    command_of,
    covered,
    format_rule,
    parse,
    parse_rule,
    suggest_prefix,
)


# --- 匹配：token 前缀，不是字符串前缀 -------------------------------------

def test_a_rule_covers_the_command_it_names_and_its_arguments():
    assert covered("git add -p x.py", [("git", "add")]) == ("git", "add")


def test_a_subcommand_rule_does_not_cover_a_sibling_subcommand():
    assert covered("git commit -m x", [("git", "add")]) is None


def test_matching_is_by_token_not_by_string():
    """`git commit` 不能匹配 `git commit-graph` —— startswith("git commit") 会。"""
    assert covered("git commit-graph write", [("git", "commit")]) is None


def test_a_bare_program_rule_covers_everything_that_starts_with_it():
    rule = ("git",)
    assert covered("git commit -m x", [rule]) == rule
    assert covered("git -C /tmp add", [rule]) == rule


def test_a_global_option_takes_it_out_of_a_subcommand_rule():
    """`git -C /tmp add` 落到"问"是刻意的失败方向：多问一次，不是多放行一次。"""
    assert covered("git -C /tmp add", [("git", "add")]) is None
    assert covered("git -C /tmp add", [("git",)]) == ("git",)


def test_options_after_the_subcommand_are_fine():
    assert covered("python -m pytest -q tests/", [("python", "-m", "pytest")]) is not None
    assert covered("python -m pytest_bad", [("python", "-m", "pytest")]) is None


def test_no_rules_means_nothing_is_covered():
    assert covered("git status", []) is None


# --- 链式、管道、重定向：整条命令行要逐段覆盖 ------------------------------

@pytest.mark.parametrize("command", [
    "git status && rm -rf build",
    "git status; rm -rf build",
    "git status || rm -rf build",
    "git log | rm -rf build",
    "git status & rm -rf build",
    "git status\nrm -rf build",
])
def test_a_chain_is_only_covered_when_every_segment_is(command):
    """只匹配第一段是这个功能最危险的错法 —— 它会把 `&& rm -rf build` 一并放行。"""
    assert covered(command, [("git",)]) is None


def test_a_chain_whose_segments_are_all_covered_passes():
    assert covered("git status && git diff", [("git",)]) == ("git",)
    assert covered("git status && ls -la", [("git",), ("ls",)]) == ("git",)


def test_the_first_segment_names_the_matched_rule():
    """审计里记的是第一段命中的规则：链式命令里它最能说明"为什么没问"。"""
    assert covered("ls -la && git status", [("git",), ("ls",)]) == ("ls",)


@pytest.mark.parametrize("command", [
    "git log > out.txt",          # 重定向：能把只读命令变成写文件
    "git log >> out.txt",
    "git log < in.txt",
    "git status $(rm -rf x)",     # 命令替换：等于在一条命令里插进另一条
    "git status `rm -rf x`",
])
def test_constructs_we_refuse_to_reason_about_are_never_covered(command):
    assert covered(command, [("git",)]) is None


@pytest.mark.parametrize("command", [
    'git commit -m "unbalanced',
    "git commit -m 'unbalanced",
    "   ",
])
def test_anything_unparseable_falls_to_ask(command):
    assert covered(command, [("git",)]) is None


# --- 引号与空段 -----------------------------------------------------------

def test_a_separator_inside_quotes_is_not_a_separator():
    assert parse('git commit -m "a; b"') == [["git", "commit", "-m", "a; b"]]
    assert covered('git commit -m "a; b"', [("git", "commit")]) == ("git", "commit")


def test_quotes_are_removed_like_the_shell_would():
    """`git "add"` 跑的就是 `git add` —— 去引号是照着 shell 的语义做，不是漏洞。"""
    assert parse('git "add" x') == [["git", "add", "x"]]
    assert covered('git "add" x', [("git", "add")]) == ("git", "add")


def test_empty_segments_are_skipped_but_the_real_ones_still_count():
    assert covered("git status;", [("git",)]) == ("git",)
    assert covered("git status;;rm -rf x", [("git",)]) is None


# --- 命令名的大小写 -------------------------------------------------------

def test_the_program_name_is_case_insensitive_on_windows():
    """只折叠**第一个** token。

    Windows 的可执行文件本来就不分大小写，所以 `GIT status` 跑的就是 git。但子命令和
    参数不折叠：git 自己的子命令是区分大小写的（`git ADD` 会被 git 拒绝），而参数里的
    大小写可能是路径。折叠不到的地方就落到"问"，方向安全。
    """
    assert covered("GIT status", [("git",)], windows=True) == ("git",)
    assert covered("GIT status", [("git",)], windows=False) is None
    assert covered("GIT ADD x", [("git", "add")], windows=True) is None
    assert covered("git ADD x", [("git", "add")], windows=True) is None


# --- 从一条命令里推出"该记住哪条前缀" -------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("git add -p x.py", ("git", "add")),
    ("git status", ("git", "status")),
    ("ls -la", ("ls",)),                        # 第二个 token 是选项
    ("python -m pytest -q", ("python",)),       # 同上：偏粗，所以提示里照实说
    ("pytest", ("pytest",)),
])
def test_suggest_prefix_picks_the_command_words(command, expected):
    assert suggest_prefix(command) == expected


@pytest.mark.parametrize("command", ["git log > out.txt", 'git commit -m "x', ""])
def test_suggest_prefix_gives_up_on_unparseable_commands(command):
    """推不出前缀就不给"记住"这个选项 —— 那比记错了强，也比记下整个工具强。"""
    assert suggest_prefix(command) is None


@pytest.mark.parametrize("command", [
    "git status && rm -rf build",
    "git status; git diff",
    "git log | head",
])
def test_suggest_prefix_gives_up_on_chains(command):
    """链式命令不给前缀：一条前缀盖不住两条命令。

    推出来的会是第一段的 `git status`，而它盖不住整条链（链要逐段覆盖），于是按了 t
    下次照样问 —— 一个看起来像"以后这条不用问了"、实际什么都没记住的按键，比没有
    这个选项更坏。
    """
    assert suggest_prefix(command) is None


# --- 规则自身的解析与回写 -------------------------------------------------

def test_parse_rule_splits_into_tokens():
    assert parse_rule("python -m pytest") == ("python", "-m", "pytest")
    assert parse_rule('git commit -m "wip wip"') == ("git", "commit", "-m", "wip wip")


@pytest.mark.parametrize("text", [
    "git add; rm -rf /",        # 规则被当成两条命令
    "git log > out.txt",
    "git status && ls",
    "",
    "   ",
])
def test_a_malformed_rule_is_an_error_not_a_silent_rule(text):
    """写错的规则必须在加载时说出来 —— 否则它要么永远不生效，要么生效成别的东西。"""
    with pytest.raises(ValueError):
        parse_rule(text)


@pytest.mark.parametrize("rule", [
    ("git", "add"),
    ("git", "commit", "-m", "wip wip"),
    ("python", "-m", "pytest"),
])
def test_format_rule_round_trips(rule):
    """写回配置文件的那一行，读出来必须还是同一条规则。"""
    assert parse_rule(format_rule(rule)) == rule


# --- 哪些工具的参数里有一条命令行 -----------------------------------------

def test_command_of_knows_which_tools_carry_a_command():
    assert command_of("shell", {"command": "git status"}) == "git status"
    assert command_of("shell", {}) is None
    assert command_of("shell", {"command": "   "}) is None
    assert command_of("read_file", {"command": "git status"}) is None
