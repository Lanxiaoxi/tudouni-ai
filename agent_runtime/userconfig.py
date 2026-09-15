"""用户级配置：`~/.tudouni/config.json`，**这台机器上独一份**。

## 它取代了什么，以及为什么

在这之前，"这台机器怎么连模型"分散在两个文件里，而它们的位置都锚在**源码目录**上：

    <包目录>/.env                  ← 密钥（DEEPSEEK_API_KEY / TAVILY_API_KEY）
    <包目录>/models.local.json     ← 路由与模型清单
    ~/.tudouni/models.json         ← 上面那份不存在时的退路

装成命令之后这套就散了：包在 site-packages 里，那不是用户会去编辑的地方（也不该是 ——
`pip install --upgrade` 会把它覆盖掉）。而"我在哪个目录干活"和"我用哪把密钥"本来就是
两件毫不相干的事：换个项目不该换密钥。

所以这些合成一份，放在**跟着人走**的位置：

    ~/.tudouni/config.json
      ├─ "providers"   路由 + 模型清单 + 密钥（原来的 models.local.json，形状基本没变）
      ├─ "web"         联网工具那几个键（tavily 的密钥与端点）
      └─ "ui"          界面自己的偏好（现在是界面语言；见 `agent_runtime/i18n/`）

## 配置只有一个来源：这份文件

**不看真实环境变量、也不看 `.env`。** 以前这里有一条三级优先级

    真实环境变量  >  config.json 的 "env" 段  >  内置默认值

它退休了，理由是它自己的形状：一个值有两个地方能放、而只有一个生效，是最难排查的那种
形态（改了 A 没反应，因为 B 在盖着它）。而这份文件本来就不进版本库、就在用户自己的机器
上 —— "放进文件里"没有任何代价，那条优先级换来的只是这种困惑。

同一件事也发生在 `providers` 的密钥上：以前还有 `api_key_env`（填一个**环境变量的名
字**），而它和 `api_key` 长得几乎一样、一个装名字一个装值。实测有人把密钥填进了装名字的
那个，然后只拿到一句"没有密钥" —— 而"两个地方都能放"正是让他那么填的原因。现在密钥只有
`api_key` 一种写法，写了 `api_key_env` 会被当成**不认识的键**当场指出来。

## 为什么 `web` 是一个节，而不是一张扁平的键值表

它以前叫 `env`，是一张扁平映射，理由是"要能一对一地接住 `.env`"。`.env` 没了之后那个
理由也没了，而"一个叫 env 的段里放着不来自环境变量的东西"是个会被反复追问的命名。

现在是**一节一个主人**：`web` 归联网工具（`runtime/config.py` 的 `WebConfig` 读它），
`providers` 归模型目录（`state/catalog.py` 读它），`ui` 归界面文案
（`agent_runtime/i18n/` 读它的 `language` 那一格）。这个模块只保证它们都是
"字符串 → 字符串"的形状，**不解释里面的键** —— 那是各自主人的知识。

## `.env` 不再被读，而且这件事要出声

**刻意不留"新文件没有就去看 .env"那条分支。** 那等于让这份解析永远背着一次历史迁移，
而迁移是一次性的事（和 `runtime/config.py` 里放弃 `.tudouni.json` 旧位置同一条规矩）。
但**不读不等于不说**：旧位置那份 `.env` 如果还在，`Runtime.notices()` 会说一句它已经
不算数了 —— "文件明明在那儿却完全不起作用"是这个项目认定的最坏失败形态。

## 这个模块只做"读这份文件"，不解释里面的内容

它是个**叶子**：只 import 标准库和 `paths`。所以 `state/catalog.py`（解释 providers）
和 `runtime/config.py`（解释 env 里那几个键）都能引它而不成环 —— 而它们俩之间本来就
有一条边（`config → catalog`），再让其中一个去替另一个读文件会很别扭：向"模型目录"
索取搜索服务的密钥，读起来就是错的。

`providers` 的**形状校验在 catalog**，不在这里：那是它的知识。这里只管到顶层那一层
（认识哪些键、`env` 是不是字符串映射）。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime import paths

# 文件名。用户级目录见 `paths.user_config_dir()`。
CONFIG_FILE_NAME = "config.json"

# 模板（随代码走，会被提交，所以里面绝不能有真密钥）。它同时取代了原来的
# `.env.example` 和 `models.example.json`。
EXAMPLE_FILE_NAME = "config.example.json"

# 环境变量：**这次读哪份配置**。测试和"临时换一份配置"都要它。
#
# **这是全项目仅剩的一个环境变量，而它不是"配置"。** 它回答的是"去哪找那份文件"，
# 不是"配置里某个键是什么" —— 配置本身只有一个来源（这份文件），见模块 docstring。
# 保留它是因为测试必须能把子进程指到一份隔离的配置上（`--runtime-stdio`、ansi 客户端
# 那几条会起真进程的测试没有地方传参数），而换成命令行开关就得给每个子进程调用点加一层
# 透传。
#
# 它比命令行开关合适：这一层不认识 argparse，而"用哪份配置"是**环境**的事实。
# 名字从 `AGENT_MODELS_FILE` 改过来了 —— 那份文件已经不只装模型了，而一个说谎的
# 变量名会让人以为它只影响 `/model`。
FILE_ENV = "AGENT_CONFIG_FILE"

# 顶层认识的**全部**键。多一个不认识的就报错，不忽略 —— 和 `permissions.json` /
# `mcp.json` / frontmatter 同一条规矩：写错一个键名而它静默不生效，是最坏的失败形态。
_KNOWN_TOP_KEYS = frozenset({"providers", "web", "ui", "$comment"})


class UserConfigError(Exception):
    """配置有问题 —— 属于"用户得先做点事"，不是 bug。

    **它是 `runtime.config.ConfigError` 和 `state.catalog.CatalogError` 的基类**，
    而入口层（`main.py` / `protocol/serve.py` / `protocol/channels.py`）捕的就是它。

    为什么要有这个基类：`CatalogError` 以前**没有任何地方捕**，所以
    `~/.tudouni/config.json` 里一个多写的逗号会以一整段 Python traceback 收场 ——
    而那恰好是新用户最先编辑的一份文件。两个名字都留着（它们说的是两件不同的事：
    "这份文件读不懂"和"这次运行缺东西"），但入口只需要认一个。

    基类住在这个叶子模块，是因为另两个模块之间已经有一条依赖边（`config → catalog`），
    让其中任何一个持有基类都会把方向拧成环。
    """


@dataclass(frozen=True, slots=True)
class UserConfig:
    """读出来的那份配置。**没有文件时是一个空的它**，不是 None。

    空对象而不是 None：调用方要写的是"从 env 里取这个键"，而不是"先判断有没有文件、
    再取"。后者会在每一个消费点重复一次同样的判断，而漏掉一处的症状是 AttributeError
    —— 发生在启动路径上。
    """

    # **我们看的是哪个文件** —— 不管它在不在。
    #
    # 这一格和 `found` 是两件事，刻意分开：`path` 回答"去哪找了"，`found` 回答"找到了
    # 吗"。合成一个（用 `None` 表示"没有文件"）会让报错文案说错话 —— 显式传了一个不存在
    # 的路径时，那句"建一份 …"只能指向**默认位置**，于是用户被引导去建一个他根本没在用
    # 的文件。
    #
    # 它是事实、不是装饰：`/status` 和启动那行说明要靠它回答"为什么我改的配置没生效"。
    path: Path | None = None
    # 那个文件在不在。见 `path` 上面那段。
    found: bool = False
    # 原样的 providers 段。**这里不解释它**（那是 catalog 的知识），只保证它是个对象。
    providers: dict[str, Any] = field(default_factory=dict)
    # 原样的 `web` 段。由 `WebConfig` 解释里面的键，这里只保证是"字符串 → 字符串"。
    web: dict[str, str] = field(default_factory=dict)
    # 原样的 `ui` 段。由 `i18n.language_from()` 解释里面的键（现在只有 `language`）。
    ui: dict[str, str] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.found


def example_file() -> Path:
    """随代码走的那份模板。**它会被提交，所以里面不能有真密钥**（有测试盯着）。"""
    return paths.package_dir() / EXAMPLE_FILE_NAME


def config_file() -> Path:
    """这次读哪份配置。`AGENT_CONFIG_FILE` 指了就用它，否则 `~/.tudouni/config.json`。

    **不检查存在性**（那是 `read()` 的事）—— 报错信息、以及"首次运行往哪写模板"都要
    先拿到这个路径，哪怕它还不存在。
    """
    forced = (os.environ.get(FILE_ENV) or "").strip()
    return Path(forced) if forced else paths.user_config_dir() / CONFIG_FILE_NAME


def scaffold() -> Path | None:
    """把模板抄到**默认位置**，返回写出来的路径；什么都没做就返回 `None`。

    ## 什么时候调它：缺密钥要报错的那一刻，不是每次启动

    这个取舍是刻意的。"每次启动都确保它存在"读起来更整齐，但它会让**只给环境变量的
    部署**（容器、CI）每跑一次就在 `$HOME` 里凭空写一个文件 —— 那种地方的 home 常常是
    临时的、甚至是只读的，而我们凭什么在那儿留东西？

    所以三种情形分别是：

      * 没有配置、也没有密钥 ⇒ **写模板，然后报错并指着它**（新用户的第一次运行）；
      * 没有配置、密钥在环境变量里 ⇒ 什么都不写，正常跑（容器里不会被惊到）；
      * 配置已经在了 ⇒ 一个字节都不碰。

    ## 三条边界

      1. **只碰默认位置。** 设了 `AGENT_CONFIG_FILE` 就什么都不做 —— 那是"我自己管路径"
         的表示，而 `read()` 对一个指定了却不存在的文件本来就是报错（不是静默新建）；
      2. **排他创建（`x` 模式）。** 不是"先判断再写"：那中间有一个窗口，而这个文件里将来
         装着密钥，任何一次覆盖都是数据丢失。撞上已存在就当没做过；
      3. **POSIX 上权限收到 0600 / 目录 0700。** 我们正在造一个**用户马上会往里填密钥**
         的文件，默认 umask 常常给出组可读。Windows 上 `chmod` 基本没有语义，静默跳过。

    模板缺失（装坏了、被删了）时返回 `None` 而不是抛异常，也不自己再写一份内联的默认内容
    —— 那就成了同一份模板的第二个来源，而它们漂掉的那天没人看得出来。调用方拿到 `None`
    就退回"你自己建一份"那句话。
    """
    if (os.environ.get(FILE_ENV) or "").strip():
        return None

    target = paths.user_config_dir() / CONFIG_FILE_NAME
    source = example_file()
    if not source.is_file():
        return None

    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError:
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _tighten(target.parent, 0o700)
        # `x` = 排他创建。见 docstring 第 2 条：这个文件将来装密钥，不能有覆盖的窗口。
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(text)
        _tighten(target, 0o600)
    except FileExistsError:
        return None
    except OSError:
        # home 只读、磁盘满、权限不够 —— 都不该让"报一句缺密钥"变成一次崩溃。
        # 调用方拿到 None，那句话就退回"你自己建一份"。
        return None
    return target


def _tighten(path: Path, mode: int) -> None:
    """收紧权限，**失败就算了**。

    Windows 上 `chmod` 只能改只读位，没有"组/其他人"的概念，所以那边这一步没有意义 ——
    但它也不该报错。同理，某些网络文件系统上 chmod 直接失败。
    """
    if os.name == "nt":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def read(path: Path | None = None) -> UserConfig:
    """读那份配置。**文件不存在不是错误** —— 返回一个空的 `UserConfig`。

    "没有配置文件也能跑"是刻意保留的：那种情况下 `catalog` 会造一条内置的 `deepseek`
    路由、密钥从 `DEEPSEEK_API_KEY` 这个真实环境变量读。也就是说这份文件是**加法**，
    不是又一道"不配就跑不起来"的门（容器里只给环境变量的部署就靠这一条）。

    **例外：`AGENT_CONFIG_FILE` 指的文件必须存在。** 显式指定了一份却找不到，几乎总是
    路径写错 —— 那时候静默退到"没有配置"会让人对着一份没生效的文件查半天。

    形状问题一律抛 `UserConfigError`，**绝不猜**。到 `env` / `providers` 是不是对的类型
    为止；`providers` 里面长什么样由 `state/catalog.py` 说。
    """
    target = config_file() if path is None else Path(path)
    if not target.is_file():
        if path is None and (os.environ.get(FILE_ENV) or "").strip():
            raise UserConfigError(
                f"{FILE_ENV}={target} 指的文件不存在 —— "
                f"要么把它建出来，要么删掉这个环境变量（那样会读默认位置）"
            )
        # **路径照样带上**（`found=False`）：调用方要拿它去说"该往哪写"，而那必须是
        # 它刚才找过的那个文件，不是默认位置。
        return UserConfig(path=target)

    raw = _read_json_object(target)

    unknown = sorted(set(raw) - _KNOWN_TOP_KEYS)
    if unknown:
        # **"认识哪些"从常量算，不写死。** 它以前是字面量 `providers、env`，而这个常量
        # 改成 `{providers, web, $comment}` 之后那句话就开始**说谎**了 —— 用户照着它改，
        # 改成一个同样不被认识的键名。报错文案和判据各写一份，漂掉是迟早的事。
        raise UserConfigError(
            f"{target} 里有不认识的顶层键：{', '.join(unknown)}\n"
            f"  认识的只有：{'、'.join(sorted(_KNOWN_TOP_KEYS - {'$comment'}))}\n"
            f"  （写错一个键名而它静默不生效是最坏的失败形态，所以这里直接停下）"
        )

    providers = raw.get("providers", {})
    if providers is None:
        providers = {}
    if not isinstance(providers, dict):
        raise UserConfigError(
            f'{target} 的 "providers" 必须是一个对象（路由名 → 路由配置），'
            f"实际是 {type(providers).__name__}"
        )

    return UserConfig(path=target, found=True, providers=providers,
                      web=_string_map(raw.get("web"), target, section="web"),
                      ui=_string_map(raw.get("ui"), target, section="ui"))


def text(mapping: dict[str, str], name: str, default: str = "") -> str:
    """从一份"字符串 → 字符串"的段里取一个值。**空串一律当"没填"。**

    `{"tavily_api_key": ""}` 是"还没填"，不是"填了一个空值" —— 于是它落到 `default` 上，
    而不是变成一个空字符串把后面的判断搞乱（模板里留空的那一行正是这个形态）。

    它是**模块级函数**而不是 `UserConfig` 的方法：消费一段配置的人（`WebConfig`）手上拿到的
    是那一**段**，不是整份文件。
    """
    return (mapping.get(name) or "").strip() or default


def _string_map(value: Any, where: Path, *, section: str) -> dict[str, str]:
    """`"web"` 那一段：一个扁平的**字符串 → 字符串**映射。

    数字和布尔**不自动转成字符串**：`{"tavily_api_key": 12345}` 几乎总是写错了引号，
    而悄悄接受它会让一把"密钥"以 `"12345"` 的形态发出去，然后收到一句鉴权失败 ——
    症状离原因太远。嵌套对象同理（那是把 `{"web": {"web": {...}}}` 这种写法写进来了）。

    **`$` 开头的键当注释跳过。** JSON 没有注释，而这份文件是给人手写的 —— 模板里那段
    "可选的几个键有哪些"必须能待在它说明的东西旁边（放到顶层 `$comment` 里，读的人就
    得在两处之间来回找）。`$` 前缀是这个项目已有的约定（顶层就有 `$comment`），而它
    不可能和真的配置键撞上。

    `section` 只用来把那句话说得准（"web 的 tavily_api_key"），不改变行为。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UserConfigError(
            f'{where} 的 "{section}" 必须是一个对象（键 → 值），'
            f"实际是 {type(value).__name__}"
        )
    out: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and key.startswith("$"):
            continue
        if not isinstance(item, str):
            raise UserConfigError(
                f'{where} 的 {section}["{key}"] 必须是字符串，'
                f"实际是 {type(item).__name__}"
                f"（数字和 true/false 也要写成字符串，否则很可能是漏了引号）"
            )
        out[str(key)] = item
    return out


def _read_json_object(path: Path) -> dict[str, Any]:
    """把文件读成一个 JSON 对象；每种毛病都变成一句能照着改的 `UserConfigError`。

    `utf-8-sig` 而不是 `utf-8`：Windows 上"另存为 UTF-8"常常带 BOM，而带 BOM 的 JSON
    会让 `json.loads` 在第一行就报 `Expecting value` —— 一个看不见的字符引起的失败，
    没人猜得到。没有 BOM 时它和 `utf-8` 完全一样。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise UserConfigError(
            f"{path} 不是 UTF-8 编码，读出来是乱码。用记事本「另存为」时选 UTF-8。"
        ) from None
    except OSError as exc:
        raise UserConfigError(f"读不了 {path}：{exc}") from None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UserConfigError(
            f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from None

    if not isinstance(data, dict):
        raise UserConfigError(
            f"{path} 的最外层必须是一个 JSON 对象（{{...}}），实际是 {type(data).__name__}"
        )
    return data


__all__ = [
    "CONFIG_FILE_NAME",
    "EXAMPLE_FILE_NAME",
    "FILE_ENV",
    "UserConfig",
    "UserConfigError",
    "config_file",
    "example_file",
    "read",
    "scaffold",
    "text",
]
