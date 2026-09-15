"""界面文案：**一个键，两套目录**，外加"这次用哪套"的判定。

## 它管什么，不管什么

| 管 | 不管 |
|---|---|
| 界面上**给人看**的字（栏、面板、提示、通知、状态那一行） | 模型看到的字（系统提示词、工具描述、`todo_note`/`job_note`/`skill_note`） |
| 从哪个配置项读语言（`ui.language`） | 模型说什么语言（那是提示词的事，和这里无关） |

**这条界线是这个功能存在的全部理由**：把界面换成英文，模型的输出语言一个字都不该变。
项目里本来就有现成的接缝 —— `todo.progress_line`（给人看）和 `todo.todo_note`（给模型）
是分开的两个函数，`jobs`/`skills` 里各有一对。翻译只许发生在"给人看"的那一半。

## 为什么是"键 + 目录表"，而不是在代码里写两种语言

三种做法里只有这一种不会漂：

  * 两个模块（`view_state_zh.py` / `view_state_en.py`）—— 两份实现，改一处忘一处，
    而症状是"英文界面里某几行动作不对"，没人查得出来；
  * 就地判断（`"离开" if lang == "zh" else "Exit"`）—— 语言知识散进 600 个调用点，
    而"漏了一处"在中文下永远看不出来（中文是默认值）；
  * **键 + 目录表**（这里）—— 代码里只有一个键，两种语言各有一份表，而"两张表的键
    必须完全一致"是一条能自动跑的判据（`tests/test_i18n.py`）。

## 语言是**进程级**的，只在启动时定一次

它不是一个到处传的参数。理由很实际：文案出现在 600 来个调用点（`widgets.py` 的
`render_parts`、`view_state.py` 的十几个纯渲染函数、`app.py` 的每一句 `_say`），
给每个都加一格 `lang` 会把那些函数的签名和可测性一起改掉 —— 而它们的价值恰恰在于
"给一段数据，还几行字"。

全局可变状态在这里是安全的，因为它有**一个写入点**：进程启动时 `activate()` 一次，
之后没人再改（运行中换语言是刻意不做的：runtime 子进程那半边跟不上，见
`doc/TUI-design.md`）。测试要换语言就用 `with_language()`，它会复原。

## 缺一条文案时怎么办

两种失败分开对待，因为它们的严重程度差着量级：

  * **键在中文表里也没有** ⇒ `KeyError`。这是编程错误（写错了键名），必须当场响 ——
    和这个项目对待"多写一个配置键"是同一条规矩；
  * **键在中文表里有、当前语言没有** ⇒ 回落中文，并记进 `missing()`。症状是"英文界面
    里冒出一行中文"（看得见、不崩），而 `tests/test_i18n.py` 有一条测试盯着这个集合
    必须永远是空的 —— 于是它不会真的发生。

## 这个模块不 import textual，也不 import runtime

`tests/test_imports.py` 里那条"textual 只准出现在 app.py / widgets.py"的白名单不用为
它开口子；而 `protocol/state.py`（它要翻 `activity` 那一行）也能引它而不破坏"协议层
不依赖界面框架"。它对 `userconfig` 有一条依赖边（为了 `LangError` 这个基类，以及
"语言从配置的哪一格来"），而 `userconfig` 是叶子 —— 不成环。
"""

import contextlib
from collections.abc import Iterator

from agent_runtime import userconfig
from agent_runtime.i18n import en as _en
from agent_runtime.i18n import zh as _zh

# 认识的语言。**只有这两个**，而且只有主语言子标签 —— `zh-CN` / `en-US` 这种写法
# 现在没有意义（界面没有地区差异），而"接受它但不做任何区别"会让用户以为它有效。
ZH = "zh"
EN = "en"
LANGS: tuple[str, ...] = (ZH, EN)

# 默认语言。**它是"没有说"的意思，不是"中文更好"** —— 老配置里没有 `ui` 段，
# 于是行为逐字节不变（这是加这个功能时唯一不能破的东西）。
DEFAULT = ZH

# 配置里那一格的键名和段名。**它们在这里而不是散在调用点**：改名字只改这一处。
SECTION = "ui"
LANGUAGE_KEY = "language"

_CATALOGS: dict[str, dict[str, str]] = {ZH: _zh.CATALOG, EN: _en.CATALOG}

# 当前语言。见模块 docstring：写入点只有 `set_language` / `activate`。
_current = DEFAULT

# 当前语言缺、但中文表里有的那些键。`t()` 记，测试断言它为空。
_missing: set[str] = set()


class LangError(userconfig.UserConfigError):
    """界面语言认不出来 —— 属于"用户得先做点事"，不是 bug。

    它是 `UserConfigError` 的子类，所以入口那几处 `except UserConfigError`（退出码 2）
    不用为它加分支：写错一个语言值和写坏一个配置键，处置完全一样。

    **不许静默回默认。** 一个认不出的 `"en_US"` 静默变成中文，用户看到的是"我配的
    英文没生效" —— 而他会去查一个不存在的 bug。
    """


def current() -> str:
    """这次进程用的是哪一套。"""
    return _current


def catalog(lang: str | None = None) -> dict[str, str]:
    """一套目录表（测试和"键是否齐全"那条判据要用）。"""
    return _CATALOGS[lang or _current]


def missing() -> frozenset[str]:
    """当前语言缺、回落成中文的那些键。**正常运行时它永远是空的**（测试盯着）。"""
    return frozenset(_missing)


def clear_missing() -> None:
    """把上面那个集合清空。给测试用（每个用例开始时清一次）。"""
    _missing.clear()


def validate(value: str) -> str:
    """把一段用户输入认成一种语言。**认不出就抛**（见 `LangError`）。

    大小写不敏感、两端空白不算：`"EN"` / `" en "` 都收。这是配置文件和命令行都会
    经过的唯一一道判定。
    """
    text = (value or "").strip().lower()
    if text in LANGS:
        return text
    raise LangError(
        f"不认识的界面语言：{value!r}\n"
        f"  现在只有这两种：{'、'.join(LANGS)}"
        f"（{ZH} = 中文，{EN} = English）\n"
        f"  它写在配置文件的 {SECTION} 段的 {LANGUAGE_KEY} 上，"
        f"或者用 --lang 临时给一个。"
    )


def set_language(lang: str) -> str:
    """定下这套语言。返回归一化之后的那个值。"""
    global _current
    _current = validate(lang)
    return _current


def t(text_key: str, **fields: object) -> str:
    """取一条文案。有 `fields` 时按 `str.format` 插值。

    第一格叫 `text_key` 而不是 `key`：**文案里完全可能有一个叫 `{key}` 的占位符**
    （校验配置的那几句就是），而参数同名会让 `t("...", key=...)` 直接 TypeError。
    名字只在这一层有意义，调用点全是位置传参。

    `fields` 为空**不做格式化** —— 文案里可能有花括号（配置片段、JSON 例子），
    而 `"{a}".format()` 会把它们当成占位符炸掉。
    """
    table = _CATALOGS[_current]
    template = table.get(text_key)
    if template is None:
        template = _CATALOGS[ZH].get(text_key)
        if template is None:
            raise KeyError(f"没有这条界面文案：{text_key!r}（中文表里也没有）")
        # 当前语言漏了这一条：回落中文，并记账（见模块 docstring）。
        _missing.add(text_key)
    if not fields:
        return template
    try:
        return template.format(**fields)
    except (KeyError, IndexError) as exc:
        raise KeyError(f"文案 {text_key!r} 的占位符对不上：{template!r}（{exc}）") from None


def has(text_key: str) -> bool:
    """有没有这一条。**只给"可选文案"用**（一条命令不一定有 `detail`）。

    判据看**中文表**：它是原文，也就是"这条文案存不存在"的定义处。英文表少一条
    由 `t()` 的回落兜住，而那件事有它自己的测试（`missing()` 必须永远是空的）。
    """
    return text_key in _CATALOGS[ZH]


def tn(text_key: str, n: int, **fields: object) -> str:
    """带数字的那一句。**英文有单复数，中文没有**，所以判据长在这里。

    表里可以给 `<key>.one` / `<key>.other` 两条（英文表就该给），也可以只给 `<key>`
    一条（中文表一律只给一条）。给了就按 `n == 1` 选，没给就退回 `t(key)` ——
    于是"中文不需要区分单复数"这件事不用在代码里写第二遍。

    `n` 自动进插值字段，所以表里可以直接写 `{n}`。
    """
    fields.setdefault("n", n)
    table = _CATALOGS[_current]
    suffixed = f"{text_key}.one" if n == 1 else f"{text_key}.other"
    if suffixed in table:
        return t(suffixed, **fields)
    return t(text_key, **fields)


@contextlib.contextmanager
def with_language(lang: str) -> Iterator[str]:
    """临时换一套语言，出来时复原。**给测试用**（也可以给"某一段固定用中文"用）。

    它是 `try/finally` 而不是"记得改回去"：一条断言失败的测试如果没复原语言，
    后面几百条测试会以"英文模式"跑，而红的是它们 —— 那种连锁失败比原本那条难查得多。
    """
    global _current
    before = _current
    try:
        yield set_language(lang)
    finally:
        _current = before


def language_from(cfg: userconfig.UserConfig) -> str:
    """从读出来的配置里取语言。**没写就是默认**（老配置照样跑）。

    这里只管"`ui` 段里那一格"，`ui` 段本身的形状由 `userconfig._string_map` 保证
    （它已经把非字符串的值挡在外面了）。
    """
    value = userconfig.text(cfg.ui, LANGUAGE_KEY, "")
    return validate(value) if value else DEFAULT


def activate(lang: str | None = None) -> str:
    """**进程启动时调一次**：命令行 > 配置文件 > 默认。

    三种情况分开，因为它们的失败方向不同：

      * 命令行给了值 ⇒ 当场校验。它是人刚敲进来的，认不出就该报错（退出码 2）；
      * 没给 ⇒ 读配置。**配置文件本身读不动**（没有、JSON 坏、`providers` 形状错）
        时退回默认语言而**不在这里报错** —— 那份错误有它自己的那一站
        （`composition.check_config` / `open_runtime`），在这里抢着报会让用户看到
        两句话，而其中一句还是关于一个次要问题的；
      * 配置读得动、但 `ui.language` 写了个认不出的值 ⇒ **抛**。这正是"写错一个键名
        而它静默不生效是最坏的失败形态"那条规矩要拦的东西。
    """
    if lang:
        return set_language(lang)
    try:
        cfg = userconfig.read()
    except userconfig.UserConfigError:
        # 刻意宽：这一层不解释那份文件出了什么事（见 docstring）。
        return set_language(DEFAULT)
    return set_language(language_from(cfg))


__all__ = [
    "DEFAULT",
    "EN",
    "LANGS",
    "LANGUAGE_KEY",
    "SECTION",
    "ZH",
    "LangError",
    "activate",
    "catalog",
    "clear_missing",
    "current",
    "has",
    "language_from",
    "missing",
    "set_language",
    "t",
    "tn",
    "validate",
    "with_language",
]
