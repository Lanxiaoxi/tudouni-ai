"""权限文件（`.tudouni/permissions.json`）：读法、报错，以及按 t 之后写回去的那一半。

这个文件是**唯一由人写给权限系统看的东西**，所以两种失败都不能容忍：

  1. 写错一个键名而它静默不生效 —— 你以为放行了，其实什么都没发生；
  2. 写回去时把还没被人看见的设置一起删掉 —— 按一次 t 丢掉别的配置。

前者靠"不认识就报错"，后者靠"只动那一个键 + 读不懂就不覆盖"。

文件住在 `.tudouni/` 里面（运行期的私有目录），而那个目录**可能还不存在** —— 全新
工作区就是这样。所以写之前建目录是这条路径自己的责任，不是测试的：按一次 `t` 而
程序崩在"我自己的目录还没建"上，是这里最荒唐的失败形态。
"""

import json
from pathlib import Path

import pytest

from agent_runtime.config import (
    ConfigError,
    PERMISSION_FILE,
    PermissionConfig,
    save_approvals,
)


def permission_path(workdir) -> Path:
    return workdir / ".tudouni" / "permissions.json"


def write(workdir, text: str):
    """把内容写进权限文件；**父目录按生产的写法建出来**（save_approvals 也会建）。"""
    path = permission_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(workdir, payload) -> object:
    return write(workdir, json.dumps(payload, ensure_ascii=False))


# --- 缺省 ---------------------------------------------------------------

def test_missing_file_is_not_an_error(workdir):
    """和 .env 一样：没有就是没有，不算配置错误。"""
    cfg = PermissionConfig.from_file(permission_path(workdir))

    assert cfg.auto_approve_tools == frozenset()
    assert cfg.deny_tools == frozenset()


def test_the_permission_file_lives_in_the_runtime_dir():
    """路径本身也钉一下：它是"运行期私有数据都在一个目录里"这条约定的入口。

    配套的是控制面（`tools/filesystem.py` 的 CONTROL_PLANE 守着 `.tudouni/`），
    所以这个文件天生就是"agent 不许写"的 —— 不需要再单独点名它。
    """
    from agent_runtime.skills import RUNTIME_DIR_NAME

    assert PERMISSION_FILE.parent.name == RUNTIME_DIR_NAME
    assert PERMISSION_FILE.name == "permissions.json"


def test_save_creates_the_runtime_dir_when_missing(workdir):
    """全新工作区里按一次 `t`：目录不存在也要能写成。"""
    path = permission_path(workdir)

    save_approvals(path, tools={"shell"}, prefixes=())

    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["auto_approve_tools"] == ["shell"]


def test_default_auto_approve_matches_the_builtin_policy(workdir):
    """缺省必须是 ("low",)，不能是空。

    按 t 会在文件不存在时把它建出来（只写一个键），如果缺省是空，那一次按键就会
    顺带把"读文件也要审批"变成现状 —— 改掉了没人打算改的东西。
    """
    assert PermissionConfig.from_file(workdir / "nope.json").auto_approve == ("low",)


def test_explicit_empty_levels_mean_ask_for_everything(workdir):
    """"没写"和"写了空数组"是两件事，必须能分辨。"""
    cfg = PermissionConfig.from_file(write_json(workdir, {"auto_approve": []}))
    assert cfg.auto_approve == ()


# --- 正常读 -------------------------------------------------------------

def test_reads_all_three_keys(workdir):
    cfg = PermissionConfig.from_file(write_json(workdir, {
        "auto_approve": ["low", "medium"],
        "auto_approve_tools": ["shell"],
        "deny_tools": ["git_commit"],
    }))

    assert cfg.auto_approve == ("low", "medium")
    assert cfg.auto_approve_tools == frozenset({"shell"})
    assert cfg.deny_tools == frozenset({"git_commit"})


def test_accepts_a_utf8_bom(workdir):
    """Windows 上"另存为 UTF-8"常常带 BOM，而带 BOM 的 JSON 会让 json.loads 在第一行
    就报 Expecting value —— 一个看不见的字符引起的失败，没人猜得到。"""
    path = permission_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deny_tools": ["shell"]}), encoding="utf-8-sig")

    assert PermissionConfig.from_file(path).deny_tools == frozenset({"shell"})


# --- 报错：都要说清楚怎么改 ---------------------------------------------

def test_unknown_key_is_an_error(workdir):
    """写错一个键名而它静默不生效，是最坏的失败形态。"""
    with pytest.raises(ConfigError) as exc:
        PermissionConfig.from_file(write_json(workdir, {"auto_aproved": ["low"]}))

    message = str(exc.value)
    assert "auto_aproved" in message          # 说清是哪个键
    assert "auto_approve" in message          # 并且给出认识的那些


def test_high_cannot_be_auto_approved_by_level(workdir):
    """等级是工具自己声明的，"放行所有 high" 会随新工具自动变宽。"""
    with pytest.raises(ConfigError) as exc:
        PermissionConfig.from_file(write_json(workdir, {"auto_approve": ["high"]}))

    assert "auto_approve_tools" in str(exc.value)   # 报错要给出正确的写法


def test_unknown_level_is_an_error(workdir):
    with pytest.raises(ConfigError, match="banana"):
        PermissionConfig.from_file(write_json(workdir, {"auto_approve": ["banana"]}))


def test_a_bare_string_is_not_a_list(workdir):
    """手滑写成 "shell" 而不是 ["shell"]：逐字符遍历会变出五个工具名，错报到很远。"""
    with pytest.raises(ConfigError, match="必须是字符串数组"):
        PermissionConfig.from_file(write_json(workdir, {"auto_approve_tools": "shell"}))


def test_contradiction_between_the_two_tool_lists_is_an_error(workdir):
    """同一个工具既放行又拒绝 —— 别让策略去猜哪个算数。"""
    with pytest.raises(ConfigError) as exc:
        PermissionConfig.from_file(write_json(workdir, {
            "auto_approve_tools": ["shell"],
            "deny_tools": ["shell"],
        }))

    assert "shell" in str(exc.value)


def test_broken_json_reports_where(workdir):
    with pytest.raises(ConfigError) as exc:
        PermissionConfig.from_file(write(workdir, '{"auto_approve": ["low",}'))

    message = str(exc.value)
    assert "第 1 行" in message
    assert "permissions.json" in message


def test_non_object_root_is_an_error(workdir):
    with pytest.raises(ConfigError, match="JSON 对象"):
        PermissionConfig.from_file(write(workdir, '["low"]'))


# --- 没注册的工具名 -----------------------------------------------------

def test_unknown_tools_flags_typos_but_does_not_fail(workdir):
    """把 shell 写成 shall 的人以为自己在放行 —— 必须说出来，但不该拦启动。"""
    cfg = PermissionConfig.from_file(write_json(workdir, {
        "auto_approve_tools": ["shall"],
        "deny_tools": ["git_commit"],
    }))

    assert cfg.unknown_tools({"shell", "git_commit"}) == frozenset({"shall"})


# --- 按 t 之后写回去 ----------------------------------------------------

def test_save_creates_the_file_and_survives_a_round_trip(workdir):
    path = permission_path(workdir)
    save_approvals(path, tools={"shell"}, prefixes=())

    cfg = PermissionConfig.from_file(path)          # 写出来的必须自己能读回去
    assert cfg.auto_approve_tools == frozenset({"shell"})
    assert cfg.auto_approve == ("low",)             # 缺省不被这次写入改掉


def test_save_keeps_the_other_keys_and_their_order(workdir):
    """只动它自己那两个键 —— 手写的部分不该因为按了一次 t 被重排。"""
    path = write_json(workdir, {
        "deny_tools": ["git_commit"],
        "auto_approve": ["low", "medium"],
    })

    save_approvals(path, tools={"shell"}, prefixes=())

    assert list(json.loads(path.read_text(encoding="utf-8"))) == [
        "deny_tools", "auto_approve", "auto_approve_tools", "shell_allow",
    ]


def test_save_is_sorted_and_idempotent(workdir):
    path = permission_path(workdir)
    save_approvals(path, tools={"shell", "write_file"}, prefixes=())
    first = path.read_text(encoding="utf-8")
    save_approvals(path, tools={"write_file", "shell"}, prefixes=())

    assert path.read_text(encoding="utf-8") == first      # 同样的集合，同样的文件
    assert json.loads(first)["auto_approve_tools"] == ["shell", "write_file"]


def test_save_leaves_no_temp_file_behind(workdir):
    """先写临时文件再 os.replace —— 但临时文件不能留在工作区里。"""
    path = permission_path(workdir)
    save_approvals(path, tools={"shell"}, prefixes=())

    assert sorted(p.name for p in (workdir / ".tudouni").iterdir()) == ["permissions.json"]


def test_save_refuses_to_overwrite_a_file_it_cannot_read(workdir):
    """读不懂就绝不覆盖：那会把文件里还没被人看见的设置一起删掉。"""
    path = write(workdir, '{"deny_tools": ["git_commit",}')

    with pytest.raises(ConfigError):
        save_approvals(path, tools={"shell"}, prefixes=())

    assert path.read_text(encoding="utf-8") == '{"deny_tools": ["git_commit",}'


# --- 命令规则（shell_allow）---------------------------------------------

def test_shell_allow_becomes_token_rules(workdir):
    """规则是**命令前缀**，存进内存时已经切成 token —— 匹配阶段不再解析字符串。"""
    cfg = PermissionConfig.from_file(write_json(workdir, {
        "shell_allow": ["git add", "python -m pytest", "ls"],
    }))

    assert cfg.shell_allow == (("git", "add"), ("python", "-m", "pytest"), ("ls",))


def test_a_rule_with_a_separator_is_an_error(workdir):
    """规则是前缀，不是命令 —— 写成分号拼接就是在把两条命令伪装成一条规则。"""
    with pytest.raises(ConfigError) as exc:
        PermissionConfig.from_file(write_json(workdir, {"shell_allow": ["git add; rm -rf /"]}))

    assert "shell_allow" in str(exc.value)


def test_a_rule_with_a_redirect_is_an_error(workdir):
    with pytest.raises(ConfigError):
        PermissionConfig.from_file(write_json(workdir, {"shell_allow": ["git log > out.txt"]}))


def test_save_writes_both_kinds_of_grant_and_round_trips(workdir):
    """两类记忆共用一次落盘：工具名和命令前缀，写出来的必须自己能读回去。"""
    path = permission_path(workdir)
    save_approvals(path, tools={"write_file"}, prefixes={("git", "add"), ("ls",)})

    cfg = PermissionConfig.from_file(path)
    assert cfg.auto_approve_tools == frozenset({"write_file"})
    assert set(cfg.shell_allow) == {("git", "add"), ("ls",)}
    assert cfg.auto_approve == ("low",)          # 缺省不被这次写入改掉


def test_save_re_quotes_tokens_that_contain_spaces(workdir):
    """含空格的 token 要重新加引号，否则写回去再读出来就变成两个 token ——
    配置被自己的写回步骤改坏是最难查的一类 bug。"""
    path = permission_path(workdir)
    rule = ("git", "commit", "-m", "wip wip")
    save_approvals(path, tools=(), prefixes={rule})

    assert PermissionConfig.from_file(path).shell_allow == (rule,)
