"""命令行的拆解与规则匹配：一条命令能不能被"人预先写下的前缀"覆盖。

这是审批粒度的第二层。第一层在 security/policy.py（按风险等级），第二层在
security/memory.py（按工具名 / 命令前缀）。这一层只回答一个问题：

    这条命令行的**每一段**，是不是都在人写过的规则里？

**它不判断安全性。** 没有"只读命令"名单，没有启发式，没有分类器 —— 规则是人写的，
这里只做保守的匹配。所谓保守，指的是**每个拿不准的地方都往"没覆盖"落**：解析器看
不懂就返回 None，调用方拿不到"覆盖"这个结论就只能去问人。

三条不能妥协的（少一条就等于把审批拆了）：

  1. **整条命令行要逐段覆盖。** `git status && rm -rf build` 在规则 `git` 下也必须
     问 —— 只匹配第一段是最典型的错法，也是最危险的错法。段按 `;`、`&&`、`||`、
     `|`、`&` 和换行拆开。
  2. **看不懂就问。** 命令替换 `$(`、反引号、重定向 `>` `<`、`${`、引号不成对，
     一律落到 None。重定向尤其要挡：它能把一条只读命令变成写文件，而写文件该走
     write_file 那条审批。
  3. **按 token 比，不按字符串比。** 规则 `git commit` **不能**匹配
     `git commit-graph write` —— `line.startswith("git commit")` 会，那是一条静默放行。

匹配的语义是「规则的 token 序列，是命令开头 token 序列的前缀」：

    规则 `git add`  →  `git add -p x.py` ✓    `git commit -m x` ✗    `git -C /tmp add` ✗
    规则 `git`      →  以上全部 ✓（包括 git 的全局选项）

`git -C /tmp add` 落到"问"是刻意的方向：多问一次，不是多放行一次。
"""

import platform
from collections.abc import Iterable, Mapping
from typing import Any

# 一条规则：命令开头的若干 token。
Rule = tuple[str, ...]

# 段的边界。`&` 在 PowerShell 里其实是调用运算符、不是分隔符，这里**照样当分隔符**：
# 拆错的后果是段匹配不上、于是去问人，方向安全；漏拆的后果是放行一条拼接命令。
_SEPARATORS = ("&&", "||", ";", "|", "&", "\n", "\r")

# 出现这些就放弃判断。`$(` 和反引号是命令替换 —— 等于在一条命令里插进另一条；
# `${` 是变量/子表达式；`>` `<` 是重定向（写文件）。它们都能让"开头几个词"不再代表
# 这条命令真正会做什么。
_UNSAFE_SUBSTRINGS = ("$(", "`", "${", ">", "<")

# 哪些工具的参数里有一条"命令行"。
#
# 写成一张显式的表，而不是"参数名叫 command 就算"：判定要按工具来，而且加一个带命令
# 的工具时必须在表里出现才会走命令规则 —— 凭空生效的规则比没有规则更难查。
COMMAND_ARGUMENTS = {"shell": "command"}


def _is_windows(windows: bool | None) -> bool:
    return platform.system() == "Windows" if windows is None else windows


def command_parameter(tool_name: str) -> str | None:
    """这个工具的参数里哪一项是命令行；不是命令类工具就返回 None。

    调用方（审批提示）靠它区分「命令类工具但这次没给命令」和「根本不是命令类工具」——
    这两种情况下"按 t 记住什么"的答案完全不同。
    """
    return COMMAND_ARGUMENTS.get(tool_name)


def command_of(tool_name: str, arguments: Mapping[str, Any]) -> str | None:
    """这次调用里的命令行；不是命令类工具、或那一项缺失/为空，都返回 None。"""
    parameter = command_parameter(tool_name)
    if parameter is None:
        return None
    value = arguments.get(parameter)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def parse(command: str, *, windows: bool | None = None) -> list[list[str]] | None:
    """把一条命令行拆成「每段的 token 列表」；任何拿不准的地方返回 None。

    返回的段已经去掉了引号 —— 那是 shell 自己的语义（`git "add"` 跑的就是 `git add`），
    照着它做才是对的。空段（`;;`、行尾分号）直接丢掉：它什么都不执行。
    """
    if any(bad in command for bad in _UNSAFE_SUBSTRINGS):
        return None

    parsed: list[list[str]] = []
    for segment in _split(command):
        tokens = _tokenize(segment, windows=_is_windows(windows))
        if tokens is None:
            return None                      # 引号不成对之类 —— 不猜
        if tokens:
            parsed.append(tokens)

    # 一条什么都不做的命令不值得放行，也不值得推理：交给调用方去问。
    return parsed or None


def covered(
    command: str,
    rules: Iterable[Rule],
    *,
    windows: bool | None = None,
) -> Rule | None:
    """这条命令**每一段**是不是都被规则覆盖了？是就返回命中的规则，否则 None。

    返回的是**第一段**命中的规则：链式命令里它最能说明"这次为什么没问"。链上其余段
    可能命中别的规则（甚至更长的），但审计要的是"为什么"，不是"全部理由"。
    """
    parsed = parse(command, windows=windows)
    if not parsed:
        return None

    usable = [tuple(rule) for rule in rules]
    if not usable:
        return None

    first: Rule | None = None
    for index, tokens in enumerate(parsed):
        matched = _match_segment(tokens, usable, windows=_is_windows(windows))
        if matched is None:
            return None                      # 有一段没人认领 → 整条都得问
        if index == 0:
            first = matched
    return first


def suggest_prefix(command: str, *, windows: bool | None = None) -> Rule | None:
    """从这条命令里推出「按 t 该记住哪条前缀」；推不出来就返回 None。

    取第一段的"命令词"：第一个 token，再加上第二个 token（只要它不是选项）。
    `git add -p x.py` → `git add`；`ls -la` → `ls`；`python -m pytest` → `python`。

    最后那个偏粗（`-m` 是选项，推不出 `pytest`）—— 所以**审批提示会把推出来的前缀原样
    写出来**，看着不对就别按 t，想更细就在文件里手写一条（手写的规则可以是任意长度的
    token 前缀，比如 `python -m pytest`）。

    **链式命令一律返回 None。** `git status && rm -rf build` 推出来的"第一段前缀"是
    `git status`，而它盖不住这条链（链要求逐段覆盖），于是按了 t 下次照样问 —— 一个
    按下去什么都没记住、却看起来像"以后这条不用问了"的按键，比没有这个选项更坏。
    """
    parsed = parse(command, windows=windows)
    if not parsed or len(parsed) > 1:
        return None

    tokens = parsed[0]
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        return (tokens[0], tokens[1])
    return (tokens[0],)


def parse_rule(text: str, *, windows: bool | None = None) -> Rule:
    """把配置里的一条规则字符串变成 token 序列；写错了抛 ValueError。

    规则是**一个命令前缀**，不是一条命令，所以这里不分段：出现分隔符、重定向或者引号
    不成对就是写错了。挡在加载处而不是留给匹配器 —— "一条规则被当成两条"这种事必须
    在启动时说出来，而不是等到某次审批悄悄放行。
    """
    if any(separator in text for separator in _SEPARATORS):
        raise ValueError(f"规则里不能出现分隔符（; | && 换行等）：{text!r} —— 规则是一个命令前缀，不是一条命令")
    if any(bad in text for bad in _UNSAFE_SUBSTRINGS):
        raise ValueError(f"规则里不能出现重定向或命令替换：{text!r}")

    tokens = _tokenize(text, windows=_is_windows(windows))
    if not tokens:
        raise ValueError(f"规则是空的：{text!r}")
    return tuple(tokens)


def format_rule(rule: Rule) -> str:
    """把规则写回配置文件的一行。

    含空格或引号的 token 要重新加引号，否则 `git commit -m "wip wip"` 这样的规则写回去
    再读出来就变成两个 token —— 配置被自己的写回步骤改坏，是最难查的一类 bug。
    """
    return " ".join(_quote(token) for token in rule)


def _quote(token: str) -> str:
    if not any(char in token for char in " \t\"'"):
        return token
    if "'" not in token:
        return f"'{token}'"
    if '"' not in token:
        return f'"{token}"'
    raise ValueError(f"token 里同时有单双引号，没法写回配置文件：{token!r}")


def _split(command: str) -> list[str]:
    """按分隔符拆段，引号内的分隔符不算。引号不成对时，最后那段照样交给分词器去拒绝。"""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0

    while index < len(command):
        char = command[index]
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue

        if char in "\"'":
            quote = char
            current.append(char)
            index += 1
            continue

        matched = next((sep for sep in _SEPARATORS if command.startswith(sep, index)), None)
        if matched is None:
            current.append(char)
            index += 1
            continue

        segments.append("".join(current))
        current = []
        index += len(matched)

    segments.append("".join(current))
    return segments


def _tokenize(segment: str, *, windows: bool) -> list[str] | None:
    """按 shell 的规矩切 token 并去掉引号；引号不成对返回 None。

    转义字符跟着平台走：sh 是反斜杠，PowerShell 是反引号。单引号在两边都是完全字面。
    """
    escape = "`" if windows else "\\"
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0

    while index < len(segment):
        char = segment[index]

        if quote == "'":
            if char == "'":
                quote = None
            else:
                current.append(char)
            index += 1
            continue

        if quote == '"':
            if char == quote:
                quote = None
            elif char == escape and not windows and index + 1 < len(segment):
                current.append(segment[index + 1])
                index += 1
            elif char == escape and windows and index + 1 < len(segment):
                # PowerShell 的反引号在双引号里也是转义符
                current.append(segment[index + 1])
                index += 1
            else:
                current.append(char)
            index += 1
            continue

        if char in "\"'":
            quote = char
        elif char == escape and index + 1 < len(segment):
            current.append(segment[index + 1])
            index += 1
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1

    if quote is not None:
        return None                          # 引号没闭合 —— 不猜它想说什么

    if current:
        tokens.append("".join(current))
    return tokens


def _match_segment(tokens: list[str], rules: list[Rule], *, windows: bool) -> Rule | None:
    for rule in rules:
        if len(rule) <= len(tokens) and all(
            _same(token, expected, index, windows)
            for index, (token, expected) in enumerate(zip(tokens, rule))
        ):
            return rule
    return None


def _same(token: str, expected: str, index: int, windows: bool) -> bool:
    """命令名在 Windows 上不分大小写（可执行文件本来就不分），其余 token 逐字符比。

    只对第一个 token 折叠：参数里的大小写可能是路径，git 的子命令本身也是区分大小写的。
    """
    if index == 0 and windows:
        return token.casefold() == expected.casefold()
    return token == expected
