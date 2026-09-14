"""用户级配置文件本身：**去哪找、怎么读、什么时候由我们把它建出来。**

这份文件（`~/.tudouni/config.json`）是新用户最先编辑的东西，也是唯一装着密钥的东西。
所以这里盯的三件事都是"第一次用它的人会撞上什么"：

  1. **形状读不懂时说人话**（写错一个键名而它静默不生效是最坏的失败形态）；
  2. **优先级不能反**（真实环境变量 > 文件 > 默认值）；
  3. **首次运行的脚手架**（`scaffold()`）—— 什么时候写、什么时候坚决不写。

`ModelConfig` / `WebConfig` / `catalog` 那几层怎么消费它，在 `test_config.py` 和
`test_catalog.py` 里。
"""

import json
import os
import stat

import pytest

from agent_runtime import paths, userconfig


def write(workdir, payload) -> "object":
    path = workdir / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- 读 -----------------------------------------------------------------------

def test_a_missing_file_is_not_an_error(workdir):
    """**没有配置文件不是错误。**

    这条是"这份文件是加法，不是又一道门"的落点：只给环境变量的部署（容器、CI）从来不会
    有这个文件，而它们必须能跑。
    """
    cfg = userconfig.read(workdir / "nope.json")

    assert cfg.exists is False
    assert cfg.providers == {} and cfg.env == {}
    # **路径照样带上** —— 调用方要拿它去说"该往哪写"，而那必须是刚才找过的那个文件。
    assert cfg.path == workdir / "nope.json"


def test_an_explicitly_pointed_file_must_exist(workdir, monkeypatch):
    """`AGENT_CONFIG_FILE` 指的文件**必须存在**，找不到就报错。

    这是唯一一处"文件不存在算错误"的地方，而它和上一条不矛盾：显式指定了一份却找不到，
    几乎总是路径写错 —— 那时候静默退到"没有配置"会让人对着一份没生效的文件查半天。
    """
    monkeypatch.setenv(userconfig.FILE_ENV, str(workdir / "nope.json"))

    with pytest.raises(userconfig.UserConfigError) as caught:
        userconfig.read()

    assert userconfig.FILE_ENV in str(caught.value)


def test_unknown_top_level_keys_are_refused(workdir):
    """顶层写错一个键名直接停下，并列出认识的那几个。

    `"provider"`（少个 s）、`"environment"`（不是 `env`）都是很自然的手滑，而静默忽略的
    表现是"我明明配了，它却完全没生效"。
    """
    with pytest.raises(userconfig.UserConfigError) as caught:
        userconfig.read(write(workdir, {"providers": {}, "environment": {}}))

    assert "environment" in str(caught.value)
    assert "providers" in str(caught.value) and "env" in str(caught.value)


def test_a_comment_key_is_allowed_at_the_top(workdir):
    """`$comment` 认，因为 JSON 没有注释而这份文件是给人手写的。"""
    cfg = userconfig.read(write(workdir, {"$comment": ["随便写点什么"],
                                          "env": {"A": "1"}}))

    assert cfg.env == {"A": "1"}


def test_the_env_section_must_be_strings(workdir):
    """`env` 里的值必须是字符串，**数字不自动转**。

    `{"DEEPSEEK_API_KEY": 12345}` 几乎总是漏了引号，而悄悄接受它会让一把"密钥"以
    `"12345"` 的形态发出去，然后收到一句鉴权失败 —— 症状离原因太远。
    """
    with pytest.raises(userconfig.UserConfigError) as caught:
        userconfig.read(write(workdir, {"env": {"DEEPSEEK_API_KEY": 12345}}))

    assert "DEEPSEEK_API_KEY" in str(caught.value)
    assert "字符串" in str(caught.value)


def test_a_comment_inside_env_is_skipped(workdir):
    """`env` 里 `$` 开头的键当注释跳过。

    模板里那段"可选的还有哪几个键"必须能待在它说明的东西旁边 —— 放到顶层 `$comment` 里，
    读的人就得在两处之间来回找。而 `$` 不可能和真的环境变量名撞上。
    """
    cfg = userconfig.read(write(workdir, {"env": {
        "A": "1", "$comment": ["这一段是说明", "可以是数组"],
    }}))

    assert cfg.env == {"A": "1"}


def test_a_bad_json_says_where(workdir):
    """坏 JSON 要报出**行列号** —— "不是合法 JSON"这五个字对手写的人没有用。"""
    path = workdir / "config.json"
    path.write_text('{"env": {"A": "1",}}', encoding="utf-8")   # 多一个逗号

    with pytest.raises(userconfig.UserConfigError) as caught:
        userconfig.read(path)

    assert "第 1 行" in str(caught.value)


def test_a_bom_does_not_break_the_parse(workdir):
    """带 BOM 的 UTF-8 要能读。

    Windows 上"另存为 UTF-8"常常带 BOM，而带 BOM 的 JSON 会让 `json.loads` 在第一行就报
    `Expecting value` —— 一个看不见的字符引起的失败，没人猜得到。
    """
    path = workdir / "config.json"
    path.write_text('{"env": {"A": "1"}}', encoding="utf-8-sig")

    assert userconfig.read(path).env == {"A": "1"}


# --- 优先级 -------------------------------------------------------------------

def test_the_priority_is_env_var_then_file_then_default(workdir, monkeypatch):
    """**真实环境变量 > 文件 > 默认值。** 这个方向不能反。

    反了会让某天部署时被一份遗留的配置悄悄改到别的网关，而那种问题从源码里完全看不出来。
    """
    cfg = userconfig.read(write(workdir, {"env": {"K": "from-file"}}))

    assert cfg.value("K") == "from-file"
    assert cfg.value("MISSING", "fallback") == "fallback"

    monkeypatch.setenv("K", "from-env")
    assert cfg.value("K") == "from-env"


def test_an_empty_value_counts_as_unset(workdir, monkeypatch):
    """留空的那一格是"还没填"，不是"填了一个空值"。

    **首次运行生成的模板里那一格就是空的**，所以这条路径是每个新用户都会走一遍的。
    """
    cfg = userconfig.read(write(workdir, {"env": {"K": "   "}}))

    assert cfg.value("K") == ""
    assert cfg.value("K", "fallback") == "fallback"

    monkeypatch.setenv("K", "from-env")
    assert cfg.value("K") == "from-env"


def test_value_in_file_ignores_the_environment(workdir, monkeypatch):
    """`value_in_file()` **只看文件**。

    它回答的是另一个问题（"这份文件里写了什么"），而那有两个用处：判断模板里有没有夹带
    真密钥（用 `value()` 的话，开发机上恰好设着那个变量就会假红），以及说清"这条路由的
    密钥是从哪儿来的"。
    """
    cfg = userconfig.read(write(workdir, {"env": {"K": "from-file"}}))
    monkeypatch.setenv("K", "from-env")

    assert cfg.value("K") == "from-env"
    assert cfg.value_in_file("K") == "from-file"


# --- 首次运行的脚手架 ---------------------------------------------------------
#
# 三种情形，三种行为。它们的分界写在 `scaffold()` 的 docstring 里，这里逐条钉住。

def test_scaffold_writes_the_template_into_a_fresh_home(workdir, monkeypatch):
    """没有配置时：**把模板抄到默认位置**，并返回那个路径。"""
    monkeypatch.delenv(userconfig.FILE_ENV, raising=False)
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))

    created = userconfig.scaffold()

    assert created == workdir / paths.RUNTIME_DIR_NAME / userconfig.CONFIG_FILE_NAME
    assert created.is_file()
    # 抄出来的那份必须**真的能被这个程序读懂**（模板本身是坏的是最浪费时间的一种失败）。
    assert "DEEPSEEK_API_KEY" in userconfig.read(created).env


def test_scaffold_does_not_touch_an_existing_file(workdir, monkeypatch):
    """已经有配置时：**一个字节都不碰**，返回 None。

    用的是排他创建（`x` 模式）而不是"先判断再写"：那中间有一个窗口，而这个文件里装着
    密钥 —— 任何一次覆盖都是数据丢失。
    """
    monkeypatch.delenv(userconfig.FILE_ENV, raising=False)
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))
    target = workdir / paths.RUNTIME_DIR_NAME / userconfig.CONFIG_FILE_NAME
    target.parent.mkdir(parents=True)
    target.write_text('{"env": {"DEEPSEEK_API_KEY": "sk-mine"}}', encoding="utf-8")

    assert userconfig.scaffold() is None
    assert "sk-mine" in target.read_text(encoding="utf-8")


def test_scaffold_keeps_its_hands_off_an_explicit_path(workdir, monkeypatch):
    """设了 `AGENT_CONFIG_FILE` 时：**什么都不做。**

    那是"我自己管路径"的表示。在别处凭空建一个默认位置的文件，只会让人以为自己指的那份
    没生效。
    """
    monkeypatch.setenv(userconfig.FILE_ENV, str(workdir / "mine.json"))
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))

    assert userconfig.scaffold() is None
    assert not (workdir / paths.RUNTIME_DIR_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 没有组/其他人的语义")
def test_the_scaffolded_file_is_not_group_readable(workdir, monkeypatch):
    """**权限收到 0600 / 目录 0700。**

    我们正在造一个用户马上会往里填密钥的文件，而默认 umask 常常给出组可读。这一条不是
    洁癖：多用户机器上"我的 key 谁都能看"和把 key 提交进仓库是同一档事故。
    """
    monkeypatch.delenv(userconfig.FILE_ENV, raising=False)
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))

    created = userconfig.scaffold()

    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    assert stat.S_IMODE(created.parent.stat().st_mode) == 0o700


def test_scaffold_stays_quiet_when_it_cannot_write(workdir, monkeypatch):
    """写不进去（只读 home、磁盘满）时返回 None，**不抛异常**。

    它被调用的时机是"正要报一句缺密钥"，而那一刻再崩一次只会把真正的原因盖掉。
    """
    monkeypatch.delenv(userconfig.FILE_ENV, raising=False)
    monkeypatch.setenv("HOME", str(workdir))
    monkeypatch.setenv("USERPROFILE", str(workdir))
    # 让模板读不到 —— 装坏了、被删了都走这一条。
    monkeypatch.setattr(userconfig, "example_file",
                        lambda: workdir / "not-there.json")

    assert userconfig.scaffold() is None
