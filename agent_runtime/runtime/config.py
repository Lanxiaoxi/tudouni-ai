"""运行配置。

## 配置只有一个来源

    ~/.tudouni/config.json

**不看真实环境变量、也不看 `.env`。** 这里以前写着一张三级优先级（真实环境变量 > 文件的
`env` 段 > 内置默认值），它随"配置只有一个来源"一起退休了：一个值有两个地方能放、而只有
一个生效，是最难排查的形态。来龙去脉写在 `agent_runtime/userconfig.py`。

密钥一律不进源码：源码会被提交到公开仓库，`~/.tudouni/config.json` 不会（它根本不在
仓库里）。`config.example.json` 是给人看的那份模板，里面没有真密钥，所以它是被提交的。

**这个模块解释的是不属于模型目录的那一段**：`web`（联网工具）。模型那一层（`providers`：
端点、模型清单、密钥）由 `state/catalog.py` 解释 —— 分开是因为它有自己的形状和判据。

## 三档配置，三种落点

| 什么 | 在哪 | 为什么在那 |
|---|---|---|
| 密钥、路由 | `~/.tudouni/config.json` | **跟着人走** —— 换个项目干活不该换密钥 |
| 权限策略 | `<cwd>/.tudouni/permissions.json` | **跟着仓库走** —— 它要能 review、能提交 |
| MCP server | `~/.tudouni/mcp.json` | 用户级，理由更硬（见 `MCP_FILE` 那一段） |

密钥和权限分开不是风格问题：密钥绝不能进版本库，而"这次启动到底放行了什么"是团队要
review 的东西 —— 两类东西混在一份文件里，早晚会有人把密钥提交上去。
"""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import i18n
from agent_runtime import paths, userconfig
from agent_runtime.security.commands import Rule, format_rule, parse_rule
from agent_runtime.tools.mcp import McpConfigError, McpServer, parse_servers


# 路径**一律从 `agent_runtime/paths.py` 取**，这里不再自己算。
#
# 它以前是这么算的：
#
#     REPO_ROOT = Path(__file__).resolve().parent.parent.parent
#     PROJECT_ROOT = REPO_ROOT / "agent_runtime"      # ← 上跳三层，再拼一个写死的名字
#
# **那一行已经坏了很久，而且完全没有症状。** 仓库目录上传时从 `agent_runtime` 改名成
# 了 `tudouni-ai`，字面量没跟着改，于是 `PROJECT_ROOT` 指向一个不存在的树：`.env`
# 一次都没被读到（密钥只能靠真实环境变量），`permissions.json` 被 `save_approvals`
# 在那个凭空的目录里创建出来。之所以查不出来，是因为这条链上每一处"文件不存在"都是
# **合法状态** —— `.env` 缺了不算错误、`permissions.json` 缺了就用内置默认。
#
# 教训不是"别写错字面量"，是**同一件事不该有三份算法**（另两份在 `state/catalog.py`
# 和 `runtime/composition.py`，它们从 `__file__` 推，算的是对的）。收口之后这一处
# 不含任何写死的目录名。
PROJECT_ROOT = paths.package_dir()

# `.env` 的**旧位置**。它不再被读（密钥现在写在 `~/.tudouni/config.json` 的 `providers`
# 段里，见 `userconfig.py`），但这个常量留着 —— `Runtime.notices()` 要检查它是否还躺在那儿
# 并说一句"它已经不算数了"。
#
# 为什么必须说：这份文件里装着密钥，而"我明明填了 key 却说没找到"是它失效之后**唯一**
# 的症状。和 `runtime/config.py` 放弃 `.tudouni.json` 旧位置、以及工作区里那份被忽略的
# `mcp.json` 完全同一条规矩：不读可以，不出声不行。
LEGACY_ENV_FILE = PROJECT_ROOT / ".env"

# `web` 段认识的**全部**键。多一个不认识的键就报错 —— 和 `permissions.json` /
# `mcp.json` / `providers` 同一条规矩：写错一个键名而它静默不生效，是最坏的失败形态
# （`tavily_api_key` 写成 `tavily_key`，`web_search` 就永远不注册，而人以为自己配了）。
_WEB_KEYS = frozenset({"tavily_api_key", "tavily_base_url"})

DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"


class ConfigError(userconfig.UserConfigError):
    """配置缺失或非法 —— 属于"用户得先做点事"，不是 bug。

    **它和 `CatalogError` 共一个基类**（`userconfig.UserConfigError`），而入口层捕的是
    基类。两个名字都留着，因为它们说的是两件不同的事：这个是"这次运行缺东西"（一条能用
    的模型路由都没有、`permissions.json` / `mcp.json` 里某个键写错），那个是
    "`providers` 段读不懂"。但对入口来说处置完全一样 —— 打到 stderr、退出码 2。

    **"缺某一把密钥"不在这一档**：模型层是抽象的那一层，密钥来自 `providers` 里的路由，
    所以缺配置的判据是"一条能用的路由都没有"，那件事由 `composition.open_runtime` 判。
    """


# --- 权限设置：`<工作区>/.tudouni/permissions.json` -----------------------
#
# 它原来是工作区根的 `.tudouni.json`，现在住进运行期的私有目录。挪进来的收益是具体的：
# 控制面只需要守一个 `.tudouni/`（它下面的东西**天生**就是"agent 不许写"），工作区根上
# 也少一个点文件。
#
# 旧位置的数据**有意不再读**（`.tudouni.json` / `.sessions/` / `.logs/` 一起放弃）。
# 留一条"新文件没有就去看旧文件"的分支，等于让这份配置解析永远背着一次历史迁移，而
# 那是一次性的事。旧文件留在磁盘上不影响任何行为；想彻底清掉就删了它，程序不会碰它。

PERMISSION_FILE_NAME = "permissions.json"


def permission_file() -> Path:
    """这个工作区的权限策略文件：`<cwd>/.tudouni/permissions.json`。

    **它是函数而不是常量，这一点是必须的。** 工作区跟着 cwd 走（见
    `paths.workspace_dir()`），所以一个模块级常量会在 `import` 那一刻把 cwd 冻死 ——
    而"哪个模块先被 import"不是任何人打算维护的顺序。冻错了的症状很难看：按一次 `t`
    记住的规则写进了**上一个目录**的 `permissions.json`，而这一个目录下一次启动照旧
    问你。

    对照 `MCP_FILE`：它在用户级、不随 cwd 变，所以它可以是常量。看一眼是函数还是常量
    就知道它属于哪一层，这个区别值得留着。
    """
    return paths.workspace_runtime_dir() / PERMISSION_FILE_NAME

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
        path = permission_file() if path is None else Path(path)
        if not path.is_file():
            return cls()

        raw = _read_json_object(path)

        unknown = [key for key in raw if key not in _KNOWN_PERMISSION_KEYS]
        if unknown:
            raise ConfigError(i18n.t(
                "config.error.unknown_keys", where=PERMISSION_FILE_NAME,
                names=", ".join(sorted(unknown)),
                known=", ".join(_KNOWN_PERMISSION_KEYS)))

        # "文件里没写这个键"和"写了空数组"必须分开处理：前者是"用内置默认"，后者是
        # "什么都不自动放行"。合成一个的话，见 DEFAULT_AUTO_APPROVE 上面那段。
        levels = (
            tuple(_string_list(raw, "auto_approve", path))
            if "auto_approve" in raw
            else DEFAULT_AUTO_APPROVE
        )
        bad_levels = [lv for lv in levels if lv not in _LEVELS_ALLOWED_IN_FILE]
        if "high" in bad_levels:
            raise ConfigError(i18n.t("config.error.auto_approve_high",
                                     file=PERMISSION_FILE_NAME))
        if bad_levels:
            raise ConfigError(i18n.t(
                "config.error.auto_approve_unknown", file=PERMISSION_FILE_NAME,
                levels=", ".join(bad_levels),
                allowed=", ".join(_LEVELS_ALLOWED_IN_FILE)))

        auto_approve_tools = frozenset(_string_list(raw, "auto_approve_tools", path))
        deny_tools = frozenset(_string_list(raw, "deny_tools", path))

        contradictory = auto_approve_tools & deny_tools
        if contradictory:
            raise ConfigError(i18n.t(
                "config.error.contradictory", file=PERMISSION_FILE_NAME,
                names=", ".join(sorted(contradictory))))

        shell_allow: list[Rule] = []
        for text in _string_list(raw, "shell_allow", path):
            try:
                shell_allow.append(parse_rule(text))
            except ValueError as exc:
                raise ConfigError(i18n.t("config.error.bad_shell_rule",
                                         file=PERMISSION_FILE_NAME,
                                         problem=exc)) from None

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


# --- 外部 MCP server：`~/.tudouni/mcp.json` ------------------------------
#
# **只从用户级目录读，不从工作区读。** 这一条是刻意的，理由比"权限策略要不要提交"
# 那一条更硬：mcp.json 里的 `command` 是"**启动时就要执行的代码**"，而不是"某个动作
# 要不要问人"。它比放行一个工具强得多，而且发生在任何审批之前 —— 审批机制根本没有
# 机会参与这个决定。
#
# 工作区级的位置（`<工作区>/.tudouni/mcp.json`）今天恰好是 gitignore 的（见
# .gitignore 里 `.tudouni/` 那一段），但那段注释明确写着"想把它变成随仓库走的团队
# 策略，删掉这一行即可"。那一天之后，工作区里的 mcp.json 就等于"clone 一个仓库就
# 自动执行任意命令"——比 `.git/hooks` 那条更宽，因为那条至少还要有人去跑一条 git 命令。
#
# 所以这里写死用户级；工作区里那份**不读**，但要报出来（见 main.py 的 [MCP] 那行）
# ——"文件明明在那儿却完全不起作用"和坏技能是同一类症状，绝不能静默。
USER_RUNTIME_DIR = paths.user_config_dir()

MCP_FILE = USER_RUNTIME_DIR / "mcp.json"

MCP_FILE_NAME = MCP_FILE.name


@dataclass(frozen=True)
class McpConfig:
    """外部 MCP server 的清单。

    形状（每一项的语义与校验在 tools/mcp.py 的 parse_servers 里，那里也写着每一处
    为什么这么严）：

        {
          "servers": {
            "github": {
              "command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-github"],
              "env": {"GITHUB_TOKEN": "..."},
              "timeout_seconds": 60
            }
          }
        }

    **密钥走 env，不进 .env。** 这看起来和"密钥不进配置文件"相反，其实是同一条：
    这份文件在**用户级目录**（`~/.tudouni/`），不进版本库，也不在工作区里 —— 它和
    `.env` 一样是"本机的、不被 review 的"那一类。反过来，进版本库的东西里不该有密钥。

    文件不存在不是错误（和 .env / permissions.json 一样）：没有 server 就没有 MCP
    工具，运行时其余部分一字不变。
    """

    servers: tuple[McpServer, ...] = ()

    @classmethod
    def from_file(cls, path: Path | None = None) -> "McpConfig":
        path = MCP_FILE if path is None else Path(path)
        if not path.is_file():
            return cls()

        raw = _read_json_object(path)
        try:
            return cls(parse_servers(raw))
        except McpConfigError as exc:
            # 形状问题归到 ConfigError 上：它和"缺密钥"是同一档 —— **用户得先做点事**，
            # 而且必须在开出会话之前停下（main.py 里那个 except ConfigError 就是这里）。
            raise ConfigError(i18n.t("config.error.mcp_problem",
                                     file=path.name, problem=exc)) from None


# --- 联网工具：Tavily 的密钥 ---------------------------------------------


@dataclass(frozen=True)
class WebConfig:
    """联网工具的配置。它读 `~/.tudouni/config.json` 的 **`web` 段**。

    **它有自己的节**（而不是挤在某个通用的键值表里）：那不来自模型目录，也不该让人以为
    它和环境变量有什么关系 —— 它的键名就是 `tavily_api_key` / `tavily_base_url` 这两格。

    **缺密钥不是错误，不拦启动 —— 这和模型那一档是有意相反的。** 模型那边缺了
    "一条能用的路由"整个程序什么都干不了，所以那是"用户得先做点事"、必须拦住启动；
    搜索密钥缺了只是少一个工具。混成一样会让"只想用文件工具的人"被迫先去注册一个
    搜索服务。

    代理由 httpx 的 trust_env 决定（main.py 里关掉了），不在这里做一个键 —— 一个
    只有一半人看得懂的 HTTP_PROXY 变体，比让人显式写代码更坏。
    """

    tavily_api_key: str = ""
    tavily_base_url: str = DEFAULT_TAVILY_BASE_URL

    @classmethod
    def from_file(cls, config_file: Path | None = None) -> "WebConfig":
        """从 `web` 段读。**不认识的键就报错**（见 `_WEB_KEYS`）。

        值一律是字符串、空串算"没填"（`userconfig.text`）—— 模板里那两行留空的形态正是
        "还没填"，于是它落到默认值上，而不是变成一个空字符串把后面的判断搞乱。
        """
        cfg = userconfig.read(config_file)

        unknown = sorted(key for key in cfg.web if key not in _WEB_KEYS)
        if unknown:
            where = userconfig.config_file() if config_file is None else config_file
            raise ConfigError(i18n.t(
                "config.error.unknown_web_keys", path=where,
                names=", ".join(unknown), known=", ".join(sorted(_WEB_KEYS))))
        return cls(
            tavily_api_key=userconfig.text(cfg.web, "tavily_api_key"),
            tavily_base_url=userconfig.text(
                cfg.web, "tavily_base_url", DEFAULT_TAVILY_BASE_URL),
        )

    @property
    def enabled(self) -> bool:
        """有没有配密钥。派生值，不存字段 —— 它完全由 tavily_api_key 决定。"""
        return bool(self.tavily_api_key)


def save_approvals(
    path: Path,
    *,
    tools: Iterable[str],
    prefixes: Iterable[Rule],
) -> None:
    """把"人说过别再问"的东西写回权限文件（`permissions.json`）。

    两类分别进 `auto_approve_tools`（工具名）和 `shell_allow`（命令前缀）—— 它们是
    同一件事的两种粒度，所以共用这一个落盘口，也就不会出现"只写了一半"的状态。

    这是**人在审批时按 t** 留下的改动，所以只动这两个键：其余键、以及键的顺序都原样
    保留，人在这份文件里手写的东西不会因为按了一次 t 就被重排或丢掉。

    先写临时文件再 os.replace —— 和会话状态文件同一个理由：磁盘上任何时刻要么是旧的
    完整版，要么是新的完整版，不存在"写了一半"的形态。这个文件最需要它的时候，正是
    它不能毁掉自己的时候。

    **父目录要自己建。** 这个文件住在 `.tudouni/` 里面，而那个目录可能还不存在
    （全新工作区、或者人手动删过它）—— 按一次 `t` 就 FileNotFoundError 是这里最荒唐的
    失败形态：用户做了一个正确的操作，程序崩在"我自己的目录还没建"上。

    读不懂就抛 ConfigError，**绝不覆盖** —— 那会把文件里还没被人看见的设置一起删掉。
    """
    raw = _read_json_object(path) if path.is_file() else {}
    raw["auto_approve_tools"] = sorted(set(tools))
    raw["shell_allow"] = sorted({format_rule(rule) for rule in prefixes})

    path.parent.mkdir(parents=True, exist_ok=True)
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
        raise ConfigError(i18n.t("config.error.not_utf8", path=path)) from None
    except OSError as exc:
        raise ConfigError(i18n.t("config.error.unreadable", path=path,
                                 error=exc)) from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(i18n.t("config.error.bad_json", path=path, line=exc.lineno,
                                 column=exc.colno, message=exc.msg)) from None

    if not isinstance(data, dict):
        raise ConfigError(i18n.t("config.error.not_object", path=path,
                                 kind=type(data).__name__))
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
        raise ConfigError(i18n.t("config.error.not_string_list",
                                 file=path.name, key=key))

    cleaned = [item.strip() for item in value]
    if any(not item for item in cleaned):
        raise ConfigError(i18n.t("config.error.empty_string_in_list",
                                 file=path.name, key=key))
    return cleaned
