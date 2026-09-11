"""运行配置。

优先级（高 → 低）：

    1. 真实环境变量
    2. 项目根目录的 .env
    3. 内置默认值

.env 只是本地图方便，**绝不能盖掉真实环境变量** —— 否则某天部署时会被一个遗留的
.env 悄悄改到别的网关上，而这种问题极难排查。这个顺序有测试盯着。

密钥一律不进源码：源码会被提交到公开仓库，.env 不会（它在 .gitignore 里）。
`.env.example` 是给人看的那份模板，里面没有真密钥，所以它是被提交的。

**用 `dotenv_values` 而不是 `load_dotenv`**：前者只返回一个 dict，不往 os.environ
里写。没有全局副作用，优先级规则就能在这一个函数里读完，而不是靠库的默认行为。

权限设置走**另一条路**：工作区根目录的 `.tudouni.json`（见 PermissionConfig）。
它和密钥不是一件事 —— 密钥要能从环境变量覆盖、且绝不能进版本库；而权限策略必须
看得见、能 review、能提交。"这次启动到底放行了什么"写在环境变量里是没法 review 的。
"""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from agent_runtime.security.commands import Rule, format_rule, parse_rule


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 各模型的上下文窗口（token）。它是**输入侧**的上限：真正发不出去的条件是"输入 + 输出"
# 超过它，所以占比接近满之前就该有新会话。
#
# 为什么是一张表、而不是问 provider：OpenAI 兼容的响应里根本没有这个字段。
# 为什么表里没有的名字**不给分母**（而不是猜一个）：这个项目可以指向任意网关，而
# **错的百分比比没有百分比更坏** —— 它会被当成真的。所以 cli 那边只报用量、不报占比，
# 启动时也会说一句该往哪加。
#
# 数据来源：DeepSeek 官方文档「模型 & 价格」的"上下文长度"（当前为 1M）。
# 旧模型名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 仍然可调用、由 V4.1-Flash
# 提供服务，窗口与 Flash 相同，所以一并列上。
CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}

_ENV_API_KEY = "DEEPSEEK_API_KEY"
_ENV_BASE_URL = "DEEPSEEK_BASE_URL"
_ENV_MODEL = "DEEPSEEK_MODEL"


class ConfigError(Exception):
    """配置缺失或非法 —— 属于"用户得先做点事"，不是 bug。"""


@dataclass(frozen=True)
class ModelConfig:
    """模型连接的配置。"""

    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "ModelConfig":
        """读取配置。

        env_file 可指定，默认用项目根的 .env（文件不存在就跳过，不算错误 ——
        环境变量和 .env 任选其一即可）。
        """
        path = ENV_FILE if env_file is None else Path(env_file)
        file_values = dotenv_values(path) if path.is_file() else {}

        def pick(name: str, default: str = "") -> str:
            # 空串一律当作"没设"，这样 .env 里留空的项也能落到下一层默认值上，
            # 而不是变成一个空字符串把后面的判断搞乱。
            from_env_var = os.environ.get(name, "").strip()
            if from_env_var:
                return from_env_var
            from_file = (file_values.get(name) or "").strip()
            return from_file or default

        api_key = pick(_ENV_API_KEY)
        if not api_key:
            raise ConfigError(
                f"没找到 {_ENV_API_KEY}。两种给法，任选其一：\n"
                f"\n"
                f"  1) 写进 {ENV_FILE}（推荐，已被 .gitignore 忽略）\n"
                f"       先从模板复制一份：  Copy-Item {ENV_EXAMPLE_FILE.name} .env\n"
                f"       然后填上：          {_ENV_API_KEY}=sk-...\n"
                f"\n"
                f"  2) 设成环境变量\n"
                f"       当前终端：  $env:{_ENV_API_KEY} = \"sk-...\"\n"
                f"       永久有效：  setx {_ENV_API_KEY} \"sk-...\"   （重开终端生效）\n"
                f"\n"
                f"可选：{_ENV_MODEL}、{_ENV_BASE_URL}\n"
                f"注：环境变量的优先级高于 .env。"
            )

        return cls(
            api_key=api_key,
            base_url=pick(_ENV_BASE_URL, DEFAULT_BASE_URL),
            model=pick(_ENV_MODEL, DEFAULT_MODEL),
        )

    @property
    def context_tokens(self) -> int | None:
        """这个模型的上下文窗口；表里没有就返回 None（不猜）。

        派生值，不存成字段 —— 它完全由 model 决定，存下来就有了两份事实
        （和 Session.step_count、`Session` 里那句"步数不存字段"是同一个理由）。
        """
        return CONTEXT_WINDOWS.get(self.model)


# --- 权限设置：工作区根目录的 .tudouni.json -------------------------------

PERMISSION_FILE = PROJECT_ROOT / ".tudouni.json"

PERMISSION_FILE_NAME = PERMISSION_FILE.name

# 认识的**全部**键。多一个不认识的键就报错 —— 理由和 ToolArgs 的 extra="forbid"
# 是同一个：写错一个键名而它静默不生效，是最坏的失败形态。你以为自己放行了或者
# 拒绝了什么，其实什么都没发生，而且没有任何地方会告诉你。
_KNOWN_PERMISSION_KEYS = ("auto_approve", "auto_approve_tools", "deny_tools", "shell_allow")

# 按等级放行只收这两个，high 必须走 auto_approve_tools 点名 —— 见 PermissionConfig。
_LEVELS_ALLOWED_IN_FILE = ("low", "medium")

# 文件里**没写** auto_approve 时的取值，和内置默认策略一致（只有 low 自动放行）。
#
# 它必须是 ("low",) 而不是空，而且必须在这里被显式用上：`t` 会在文件不存在时创建
# 它（只写 auto_approve_tools 一个键），如果"文件里没写这个键"等于空，那么按一次 t
# 就会顺带把"连读文件都要审批"变成现状 —— 一次按键改掉了没人打算改的东西。
DEFAULT_AUTO_APPROVE = ("low",)


@dataclass(frozen=True)
class PermissionConfig:
    """`.tudouni.json` 里的权限设置。

    四个键，都只有"收窄"和"点名"两种写法，没有"按等级放开一切"：

        {
          "auto_approve": ["low"],             // 按风险等级直接放行
          "auto_approve_tools": ["shell"],     // 按工具名直接放行
          "deny_tools": ["git_commit"],        // 按工具名直接拒绝，问都不问
          "shell_allow": ["git add", "ls"]     // 按命令前缀直接放行（只对 shell 有意义）
        }

    **为什么 high 不能写进 auto_approve。** 等级是工具自己声明的，所以"放行所有
    high"这条规则会随着将来新加的工具**自动变宽**：新加一个 high 的 delete_file，
    它一注册就已经免审批了，而写规则的人从没听说过这个工具。这正是 Tool.risk 不给
    默认值要防的那件事（"漏声明的新工具会静默落到那一档上"）。按名字点名不会 ——
    名单里写的是哪个工具，放行的就是哪个工具。

    **auto_approve 的缺省值是 ("low",)，和内置默认策略一致。** 这一点必须是这个
    值而不是空：`t` 会在文件不存在时创建它（只写一个键），如果缺省是空，那么按一次
    `t` 就会顺带把"读文件也要审批"变成现状 —— 一次按键改掉了没人打算改的东西。
    想表达"什么都不自动放行"，就在文件里显式写 `"auto_approve": []`。

    文件不存在不是错误（和 .env 一样），全部取默认值。

    **shell_allow 是命令前缀，不是命令。** 规则 `git add` 覆盖 `git add -p x.py`，
    但不覆盖 `git commit`；规则 `git` 覆盖所有以 git 开头的段（包括它的全局选项）。
    语义和边界写在 security/commands.py 里，那里还写着三条不能妥协的：整条命令行要
    逐段覆盖、看不懂就去问人、按 token 比而不是按字符串比。

    一个必须先说清的事实：**写裸程序名（`git`、`python`、`make`）约等于放开整个 shell
    工具** —— `git -c alias.x='!rm -rf x' x`、`git bisect run <任意命令>`、`git commit`
    会跑的 .git/hooks 都能从"允许 git"里长出来。想让规则真的收窄什么，就写到子命令
    （`git add`）这一层。
    """

    auto_approve: tuple[str, ...] = DEFAULT_AUTO_APPROVE
    auto_approve_tools: frozenset[str] = frozenset()
    deny_tools: frozenset[str] = frozenset()
    shell_allow: tuple[Rule, ...] = ()

    @classmethod
    def from_file(cls, path: Path | None = None) -> "PermissionConfig":
        path = PERMISSION_FILE if path is None else Path(path)
        if not path.is_file():
            return cls()

        raw = _read_json_object(path)

        unknown = [key for key in raw if key not in _KNOWN_PERMISSION_KEYS]
        if unknown:
            raise ConfigError(
                f'{PERMISSION_FILE_NAME} 里有不认识的键：{", ".join(sorted(unknown))}\n'
                f'  认识的只有：{", ".join(_KNOWN_PERMISSION_KEYS)}\n'
                f"  （写错一个键名而它静默不生效是最坏的失败形态，所以这里直接停下）"
            )

        # "文件里没写这个键"和"写了空数组"必须分开处理：前者是"用内置默认"，后者是
        # "什么都不自动放行"。合成一个的话，见 DEFAULT_AUTO_APPROVE 上面那段。
        levels = (
            tuple(_string_list(raw, "auto_approve", path))
            if "auto_approve" in raw
            else DEFAULT_AUTO_APPROVE
        )
        bad_levels = [lv for lv in levels if lv not in _LEVELS_ALLOWED_IN_FILE]
        if "high" in bad_levels:
            raise ConfigError(
                f'{PERMISSION_FILE_NAME} 的 auto_approve 不接受 "high"：等级是工具自己\n'
                f'  声明的，"放行所有 high" 会随着将来新加的工具自动变宽。要放行 shell\n'
                f'  就点名它：{{"auto_approve_tools": ["shell"]}}'
            )
        if bad_levels:
            raise ConfigError(
                f'{PERMISSION_FILE_NAME} 的 auto_approve 里有未知等级 '
                f'{", ".join(bad_levels)}；能按等级放行的只有 '
                f'{", ".join(_LEVELS_ALLOWED_IN_FILE)}'
            )

        auto_approve_tools = frozenset(_string_list(raw, "auto_approve_tools", path))
        deny_tools = frozenset(_string_list(raw, "deny_tools", path))

        contradictory = auto_approve_tools & deny_tools
        if contradictory:
            raise ConfigError(
                f'{PERMISSION_FILE_NAME} 里 {", ".join(sorted(contradictory))} 同时出现在 '
                f"auto_approve_tools 和 deny_tools —— 这两句互相矛盾，在这里改掉，"
                f"别让策略去猜哪个算数"
            )

        shell_allow: list[Rule] = []
        for text in _string_list(raw, "shell_allow", path):
            try:
                shell_allow.append(parse_rule(text))
            except ValueError as exc:
                raise ConfigError(f"{PERMISSION_FILE_NAME} 的 shell_allow 里有一条写错的规则：{exc}") from None

        return cls(
            auto_approve=tuple(levels),
            auto_approve_tools=auto_approve_tools,
            deny_tools=deny_tools,
            shell_allow=tuple(shell_allow),
        )

    def unknown_tools(self, known: Iterable[str]) -> frozenset[str]:
        """名单里那些**没有被注册**的工具名。

        它不该让启动失败 —— 策略可能是在工具上线之前就写好的。但也不能不说：
        把 shell 写成 shall 的人以为自己放行了，实际什么都没发生。
        """
        return frozenset((self.auto_approve_tools | self.deny_tools) - set(known))


def save_approvals(
    path: Path,
    *,
    tools: Iterable[str],
    prefixes: Iterable[Rule],
) -> None:
    """把"人说过别再问"的东西写回 `.tudouni.json`。

    两类分别进 `auto_approve_tools`（工具名）和 `shell_allow`（命令前缀）—— 它们是
    同一件事的两种粒度，所以共用这一个落盘口，也就不会出现"只写了一半"的状态。

    这是**人在审批时按 t** 留下的改动，所以只动这两个键：其余键、以及键的顺序都原样
    保留，人在这份文件里手写的东西不会因为按了一次 t 就被重排或丢掉。

    先写临时文件再 os.replace —— 和会话状态文件同一个理由：磁盘上任何时刻要么是旧的
    完整版，要么是新的完整版，不存在"写了一半"的形态。这个文件最需要它的时候，正是
    它不能毁掉自己的时候。

    读不懂就抛 ConfigError，**绝不覆盖** —— 那会把文件里还没被人看见的设置一起删掉。
    """
    raw = _read_json_object(path) if path.is_file() else {}
    raw["auto_approve_tools"] = sorted(set(tools))
    raw["shell_allow"] = sorted({format_rule(rule) for rule in prefixes})

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json_object(path: Path) -> dict[str, Any]:
    """把文件读成一个 JSON 对象；读法上的每种毛病都变成一句能照着改的 ConfigError。"""
    try:
        # utf-8-sig 而不是 utf-8：Windows 上"另存为 UTF-8"常常带 BOM，而带 BOM 的
        # JSON 会让 json.loads 在第一行就报 Expecting value —— 一个看不见的字符引起
        # 的失败，没人猜得到。没有 BOM 时它和 utf-8 完全一样。
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path} 不是 UTF-8 编码，读出来是乱码。用记事本「另存为」时选 UTF-8。"
        ) from None
    except OSError as exc:
        raise ConfigError(f"读不了 {path}：{exc}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from None

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path} 的最外层必须是一个 JSON 对象（{{...}}），实际是 {type(data).__name__}"
        )
    return data


def _string_list(raw: dict[str, Any], key: str, path: Path) -> list[str]:
    """取一个字符串数组；键不存在就是空。

    单独拦一下"给的是字符串而不是数组"：那种手滑（"auto_approve_tools": "shell"）
    如果放过去，字符串会被逐字符遍历成五个工具名，错误报到很远的地方才现形。
    """
    value = raw.get(key, [])
    if isinstance(value, str) or not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ConfigError(
            f'{path.name} 的 "{key}" 必须是字符串数组，例如 ["shell"]'
        )

    cleaned = [item.strip() for item in value]
    if any(not item for item in cleaned):
        raise ConfigError(f'{path.name} 的 "{key}" 里有空字符串')
    return cleaned
