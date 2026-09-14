"""模型目录与提供方路由：**一个模型去哪、叫什么、有什么能力**。

## 这一份数据回答四个问题

  1. **请求发到哪**（provider → base_url + 密钥）；
  2. **有哪些模型能选**（`/model` 那份清单）；
  3. **每个模型有什么能力**（上下文窗口、能不能看图）；
  4. **默认用哪个**。

四件事必须同一份数据：各写一份的话，"清单里能选、发出去报模型不存在"这类漂移
全都是静默的。

## 配置文件：`~/.tudouni/config.json` 的 `providers` 段

它**跟着人走，不跟着工作区走**：换个项目干活不该换密钥。文件的定位、`env` 段、以及
"为什么不再读 `.env` 和 `<包目录>/models.local.json`"都写在 `agent_runtime/userconfig.py`
里 —— 这个模块只负责**解释 `providers` 段**，不负责找文件、也不负责读它。

```jsonc
{
  "providers": { "deepseek": { "base_url": "…", "api_key_env": "DEEPSEEK_API_KEY",
                               "models": [{"id": "deepseek-flash", …}] } },
  "env": { "DEEPSEEK_API_KEY": "sk-…" }
}
```

**密钥可以直接写进这个文件**：它在用户级目录、不进版本库、也不在工作区里被 agent 改
（控制面那条只保护 `.tudouni/`，而这一份恰好也在那底下）。同时**也认 `api_key_env`**
（引用一个环境变量名），因为"密钥不进任何文件、只从环境来"是更好的做法，而这两种都该
能选。优先级是 **`api_key` > 环境变量 > 同一份文件的 `env` 段**：写死了就是"我要用这个"。

**没有这个文件也能跑。** 那种情况下会自动造一条 `deepseek` 路由（密钥从
`DEEPSEEK_API_KEY` 读、端点从 `DEEPSEEK_BASE_URL` 读）—— 也就是说，这份配置文件是
**加法**，不是"不配就跑不起来"的又一道门。容器里只给环境变量的部署就靠这一条。

## 坏配置一律降级 + 出声，绝不拦启动

一条路由起不来（缺密钥、名字写错）只该少一个选择，不该让整个会话开不出来 —— 这和
`SkillCatalog` 的 `problems` / `shadowed` 是同一条处置。**但必须说出来**：`problems()`
返回的那些行由启动路径打到 stderr，因为"文件明明在那儿却完全不起作用"是最坏的失败
形态（人反复改一份不生效的配置，而真正生效的在别处）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_runtime import userconfig
from agent_runtime.state import reasoning

# 文件的定位与读取**全在 `userconfig`**（那是个只 import 标准库和 `paths` 的叶子）。
# 这里刻意不再自己算路径、也不再自己读 JSON：
#
#   * 同一份文件被两个模块读（这里要 `providers`，`runtime/config.py` 要 `env`），
#     各写一份"去哪找、怎么读"就是同一件事的第二份算法 —— 而那正是 `paths.py` 那段
#     docstring 记着的那次事故（三份算法里有一份写死了目录名，改名之后静默失效）；
#   * **仍然不 import `runtime.config`**：那个模块反过来要 import 本模块，环会让
#     "哪个先加载"变成一个必须小心维持的顺序。
MODELS_EXAMPLE_NAME = userconfig.EXAMPLE_FILE_NAME

# 内置那条路由的名字与端点。**它是"没有配置文件时也能跑"的依据**，也是
# `DEFAULT_MODEL` 的出处 —— `runtime/config.py` 从这里取，不另写一份字面量。
BUILTIN_PROVIDER = "deepseek"
BUILTIN_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
# 内置那条兜底路由的密钥环境变量。**公开**：`composition` 要在"一条路由都没有"那句话
# 里说清"只给一把密钥也能跑"是哪一把，而那句话不该自己抄一个字面量（抄了就会漂）。
BUILTIN_API_KEY_ENV = "DEEPSEEK_API_KEY"
_API_KEY_ENV = BUILTIN_API_KEY_ENV
_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
_MODEL_ENV = "DEEPSEEK_MODEL"

# 一条路由的合法键。**不认识的键直接报错，不忽略** —— 和 `permissions.json` /
# `mcp.json` 同一条规矩：写错一个键名而它静默不生效，是最坏的失败形态。
_PROVIDER_KEYS = frozenset({
    "display_name", "base_url", "api_key", "api_key_env", "models",
})
_MODEL_KEYS = frozenset({
    "id", "label", "context_window", "summary", "note", "vision",
    "reasoning_effort",
})

# 内置的 DeepSeek 目录。**它是"没有配置文件时也能跑"的依据**，不是第二份目录表 ——
# 一旦配置文件的 `providers` 里声明了 `deepseek` 这条路由，这里就不再参与。
#
# 名字与能力取自官方文档（api-docs.deepseek.com 的「模型 & 价格」）：
# 两个模型、窗口都是 1M。旧名字（`deepseek-v4-flash` / `…-vision-exp`）作为**别名**
# 认下来，但不列进清单 —— 官方说它们对应的模型已下线、由 V4.1-Flash 提供服务。
_BUILTIN_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek-flash",
        "label": "Flash",
        "context_window": 1_000_000,
        "summary": "快、便宜，日常干活用它",
        "note": "DeepSeek-V4.1-Flash；支持图像理解；并发上限 2500。"
                "缓存命中输入比 Pro 便宜约 7 倍。",
        "vision": True,
        "reasoning_effort": "high",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "Pro",
        "context_window": 1_000_000,
        "summary": "贵得多，难题上更强",
        "note": "DeepSeek-V4-Pro-0813；不支持图像理解；并发上限 500。"
                "缓存未命中输入约为 Flash 的 4.5 倍。",
        "vision": False,
        "reasoning_effort": "high",
    },
)

_BUILTIN_BASE_URL_ENV = _BASE_URL_ENV  # 兼容旧名字（同一个值）

# 别名 → 现在真正在服务的那个模型。收下它们是因为别人的配置/环境变量里可能就写着它们，
# 而"能用的名字被判成不认识"是最没必要的意外。
ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


class CatalogError(userconfig.UserConfigError):
    """`providers` 段的**形状**写坏了 —— 属于"用户得先做点事"，不是 bug。

    它和"某条路由起不来"是两档：这个是"这一段根本读不懂"（不认识的键、类型不对），
    那个是"读懂了，但这条路由用不了"（缺密钥）。前者当场停下，后者降级 + 出声 ——
    因为前者再猜也没意义，而后者少一条路由不该让会话开不出来。

    **它继承 `UserConfigError`**，而入口层捕的是基类。在这之前它谁都不继承、也**没有
    任何地方捕它**，所以一个写坏的配置文件会以一整段 Python traceback 收场 —— 而那
    恰好是新用户最先编辑的那份文件。
    """


@dataclass(frozen=True, slots=True)
class ModelRef:
    """目录里的一条：**一个模型 + 它在哪条路由上**。

    `id` 是发给端点的模型名（原样传），`label` 是给人看的短名。两者分开是因为
    端点要的常常是难看的长名字（`claude-sonnet-4-5-20250929`），而清单里那一列
    放不下也不该放。
    """

    provider: str
    id: str
    # 上下文窗口。None = 不知道 —— 界面据此**只报用量、不报占比**（错的百分比比没有
    # 百分比更坏，它会被人当成真的）。
    window: int | None = None
    label: str = ""
    summary: str = ""
    note: str = ""
    vision: bool = False
    # 这条路由上这个模型的**出厂强度**。用户没 `/effort` 过时用它。
    default_effort: str = reasoning.DEFAULT_EFFORT

    @property
    def title(self) -> str:
        return self.label or self.id

    @property
    def qualified(self) -> str:
        """`provider/model` —— 清单里那一眼能认出"去哪条路由"的写法。

        两条路由有同名模型时（比如自建网关上也叫 `deepseek-flash`），光看模型名
        分不出请求发到哪儿，而这两件事的账单和合规后果完全不同。
        """
        return f"{self.provider}/{self.id}"

    def as_row(self, *, current: bool) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "id": self.id,
            "label": self.title,
            "window": self.window,
            "summary": self.summary,
            "note": self.note,
            "vision": self.vision,
            "current": current,
        }


@dataclass(frozen=True, slots=True)
class Provider:
    """一条路由：请求发到哪、用哪把密钥、提供哪些模型。"""

    name: str
    base_url: str
    api_key: str
    models: tuple[ModelRef, ...] = ()
    display_name: str = ""

    @property
    def title(self) -> str:
        return self.display_name or self.name

    @property
    def usable(self) -> bool:
        """**有没有密钥。** 没密钥的路由照样留在目录里，但不能被选中。"""
        return bool(self.api_key)

    def find(self, model: str) -> ModelRef | None:
        """这条路由上的某个模型（先折算别名）。"""
        wanted = ALIASES.get((model or "").strip(), (model or "").strip())
        for item in self.models:
            if item.id == wanted:
                return item
        return None


@dataclass(frozen=True, slots=True)
class Registry:
    """这一台机器上**认识的全部模型**，加它们的路由。"""

    providers: tuple[Provider, ...] = ()
    # 启动时那些"该看一眼"的说明（缺密钥、声明了却没有模型、`DEEPSEEK_MODEL` 找不到）。
    # **分两档**，因为它们的处置不同：`problems` 是"这条路由用不了/你的配置没生效"，
    # `notes` 是"它好着呢，这是它的事实"（比如"这条路由的密钥是从文件里读的"）。
    # 混在一起的话，真正的问题会被淹没在正常信息里 —— 而"密钥到底是哪来的"又必须说，
    # 因为它是排查"为什么它用的是旧密钥"唯一的线索。
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # 这份目录是哪来的（给 `/status` 和启动那行说明用）。**它是事实，不是装饰**：
    # "为什么我改的配置没生效"这个问题的答案就是这一个字符串。
    source: str = "内置"

    def provider(self, name: str) -> Provider | None:
        for item in self.providers:
            if item.name == name:
                return item
        return None

    @property
    def usable(self) -> bool:
        """**有没有至少一条能用的路由。** 这就是"模型配好了没有"的判据。

        注意它问的**不是**"有没有某一把密钥"：密钥来自路由，而路由可能是用户自己的网关。
        入口在起界面之前问一次（`composition.check_config`）问的就是这一个问题，所以它
        值得有个名字 —— 而不是让每个调用点各写一遍 `any(p.usable for p in ...)`。

        （`default_provider()` 不是这个判据：一条都不用时它会退回 `providers[0]`，
        也就是**返回一条不能用的路由** —— 那是它该有的行为，"挑一条来当默认"，不是
        "有没有能用的"。）
        """
        return any(item.usable for item in self.providers)

    def models(self) -> tuple[ModelRef, ...]:
        """可选的模型清单，**按路由顺序**（顺序就是配置文件里的顺序）。"""
        return tuple(item for provider in self.providers for item in provider.models)

    def find(self, name: str, *, provider: str | None = None) -> ModelRef | None:
        """按名字找一个模型。

        `provider` 给了就只在它下面找；没给就**在所有路由里找** —— 但**同名多个时
        不猜**：那种情况下要求写 `provider/model`（见 `Unresolved.ambiguous`）。
        两条路由都有 `deepseek-flash` 时随便挑一条，是那种"看起来完全正常、账单
        却在另一个账号上"的错误。
        """
        wanted = (name or "").strip()
        if not wanted:
            return None
        if "/" in wanted:
            head, _, tail = wanted.partition("/")
            found = self.provider(head.strip())
            return found.find(tail) if found is not None else None
        if provider is not None:
            found = self.provider(provider)
            return found.find(wanted) if found is not None else None
        hits = [item for item in self.models() if item.id == ALIASES.get(wanted, wanted)]
        return hits[0] if len(hits) == 1 else None

    def ambiguous(self, name: str) -> tuple[ModelRef, ...]:
        """这个名字是不是落在多条路由上（那就要写 `provider/model`）。"""
        wanted = ALIASES.get((name or "").strip(), (name or "").strip())
        return tuple(item for item in self.models() if item.id == wanted)

    def default_provider(self) -> Provider | None:
        """没指定时用哪条路由：**第一条能用的**。

        顺序是配置文件里的顺序 —— 所以"哪个是默认"是人自己排出来的，不是我们按名字
        猜的（按名字猜的话，加一条路由可能悄悄改掉默认，而没有任何一行输出会变）。
        """
        for item in self.providers:
            if item.usable:
                return item
        return self.providers[0] if self.providers else None

    def default_model(self, provider: Provider | None = None) -> ModelRef | None:
        found = provider or self.default_provider()
        if found is None or not found.models:
            return None
        return found.models[0]

    def windows(self) -> dict[str, int]:
        """`{模型名: 窗口}` —— 老接口（`config.CONTEXT_WINDOWS`）要的那张表。

        只含**知道窗口**的那些，而且别名也进表：别人的 `DEEPSEEK_MODEL` 里写着旧名字
        时，那张表得照样答得出分母。
        """
        table = {item.id: item.window for item in self.models() if item.window is not None}
        for alias in ALIASES:
            hit = self.find(alias)
            if hit is not None and hit.window is not None:
                table[alias] = hit.window
        return table


# --- 解释 providers 段 ----------------------------------------------------------
#
# 找文件、读 JSON、校验顶层形状都在 `userconfig`。这里从"已经是一个 dict"开始 ——
# 那条分界线让这个模块只需要认识"一条路由长什么样"这一件事。

def _text(raw: dict, key: str, *, where: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CatalogError(f"{where} 的 \"{key}\" 必须是字符串，实际是 {type(value).__name__}")
    return value.strip()


def _flag(raw: dict, key: str, *, where: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise CatalogError(f"{where} 的 \"{key}\" 必须是 true/false")
    return value


def _window(raw: dict, key: str, *, where: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CatalogError(
            f"{where} 的 \"{key}\" 必须是一个正整数（token 数），实际是 {value!r}；"
            f"不知道就整个删掉这一行 —— 那种情况界面只报用量、不报占比"
        )
    return int(value)


def _models_from(raw: Any, *, provider: str, where: str) -> tuple[ModelRef, ...]:
    """一条路由的 `models` 数组。**空数组 = 没有可选项**（不是"什么都行"）。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CatalogError(f"{where} 的 \"models\" 必须是一个数组")
    out: list[ModelRef] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        spot = f"{where} 的 models[{index}]"
        if not isinstance(item, dict):
            raise CatalogError(f"{spot} 必须是一个对象")
        unknown = sorted(set(item) - _MODEL_KEYS)
        if unknown:
            raise CatalogError(
                f"{spot} 里有不认识的键：{', '.join(unknown)}\n"
                f"  认识的只有：{', '.join(sorted(_MODEL_KEYS))}"
            )
        model_id = _text(item, "id", where=spot)
        if not model_id:
            raise CatalogError(f"{spot} 少了 \"id\"（发给端点的模型名）")
        if model_id in seen:
            raise CatalogError(f"{spot} 的 id {model_id!r} 和前面那条重复了")
        seen.add(model_id)

        effort = _text(item, "reasoning_effort", where=spot) or reasoning.DEFAULT_EFFORT
        if reasoning.resolve_effort(effort) is None:
            raise CatalogError(
                f"{spot} 的 reasoning_effort {effort!r} 不认识；"
                f"能写的只有 {', '.join(reasoning.EFFORT_LEVELS)}"
                f"（＋ {', '.join(sorted(reasoning.ALIASES))} 这些等价写法）"
            )
        out.append(ModelRef(
            provider=provider,
            id=model_id,
            label=_text(item, "label", where=spot),
            window=_window(item, "context_window", where=spot),
            summary=_text(item, "summary", where=spot),
            note=_text(item, "note", where=spot),
            vision=_flag(item, "vision", where=spot),
            default_effort=reasoning.resolve_effort(effort) or reasoning.DEFAULT_EFFORT,
        ))
    return tuple(out)


def _api_key(raw: dict, *, where: str, env: dict[str, str]) -> tuple[str, str]:
    """一条路由的密钥：`(值, 从哪来)`。

    优先级 **文件里的 `api_key` > `api_key_env` 指的那个真实环境变量 > 同一份文件
    `env` 段里的同名键**。返回"从哪来"是为了让启动那行说明能说清"这条路由的密钥是从
    哪儿读的"—— 排查"为什么它用的是旧密钥"时，这句话是唯一的线索。
    """
    key = _text(raw, "api_key", where=where)
    if key:
        return key, "文件"
    name = _text(raw, "api_key_env", where=where)
    if not name:
        return "", ""
    from_env = (os.environ.get(name) or "").strip()
    if from_env:
        return from_env, f"环境变量 {name}"
    from_file = (env.get(name) or "").strip()
    if from_file:
        return from_file, f'配置里 env 的 {name}'
    return "", ""


def _builtin_registry(env: dict[str, str], *, source: str) -> Registry:
    """没有配置文件时的那条 `deepseek` 路由（读 `DEEPSEEK_*`）。

    它让"不写配置文件也能跑"成立 —— 也就是这个项目在这之前的全部用法。
    """
    key = (os.environ.get(_API_KEY_ENV) or "").strip() or (env.get(_API_KEY_ENV) or "").strip()
    base = ((os.environ.get(_BASE_URL_ENV) or "").strip()
            or (env.get(_BASE_URL_ENV) or "").strip() or BUILTIN_BASE_URL)
    models = tuple(
        ModelRef(
            provider=BUILTIN_PROVIDER,
            id=item["id"],
            label=item.get("label", ""),
            window=item.get("context_window"),
            summary=item.get("summary", ""),
            note=item.get("note", ""),
            vision=bool(item.get("vision")),
            default_effort=reasoning.resolve_effort(item.get("reasoning_effort", ""))
            or reasoning.DEFAULT_EFFORT,
        )
        for item in _BUILTIN_MODELS
    )
    return Registry(
        providers=(Provider(name=BUILTIN_PROVIDER, base_url=base, api_key=key,
                            models=models),),
        source=source,
    )


def load(path: Path | None = None) -> Registry:
    """读那份配置的 `providers` 段。**永远返回一个能用的 Registry，坏消息放在 `problems`。**

    `path=None` 时读 `userconfig.config_file()`（默认 `~/.tudouni/config.json`，可以用
    `AGENT_CONFIG_FILE` 顶掉）。**没有那个文件、或者它里面没写 `providers`**，就退到内置
    那条 `deepseek` 路由 —— 见 `_builtin_registry`。

    **形状错误抛 `CatalogError`（`UserConfigError` 的子类），语义问题进 `problems`。**
    这条分界线是有意的：读不懂再猜也没意义（停下让人改），而"缺密钥"少一条路由不该让
    会话开不出来。

    ## `env_file=` 那个参数没了

    它以前是"去哪找 `.env`"。现在密钥和 providers 在**同一份文件**里，所以一个路径就够 ——
    而留着两个参数会让"这两份必须是同一个文件"变成调用方要记住的事（测试里最容易忘，
    而忘了的症状是"密钥读不到"）。
    """
    cfg = userconfig.read(path)
    env = cfg.env

    # 没有文件、或者文件里没写 providers（只写了 env / 只放了密钥）—— 两种都退到内置那条
    # 路由。**第二种必须和第一种一样待**：只想换个密钥的人不该被迫把整份模型清单抄一遍。
    if not cfg.providers:
        source = str(cfg.path) if cfg.exists else f"内置（{_API_KEY_ENV}）"
        return _builtin_registry(env, source=source)

    path = cfg.path
    providers_raw = cfg.providers

    problems: list[str] = []
    notes: list[str] = []
    providers: list[Provider] = []
    for name, item in providers_raw.items():
        where = f"{path.name} 的 providers.{name}"
        if not isinstance(item, dict):
            raise CatalogError(f"{where} 必须是一个对象")
        unknown = sorted(set(item) - _PROVIDER_KEYS)
        if unknown:
            raise CatalogError(
                f"{where} 里有不认识的键：{', '.join(unknown)}\n"
                f"  认识的只有：{', '.join(sorted(_PROVIDER_KEYS))}"
            )
        base_url = _text(item, "base_url", where=where)
        if not base_url:
            raise CatalogError(f'{where} 少了 "base_url"（请求发到哪）')
        key, origin = _api_key(item, where=where, env=env)
        models = _models_from(item.get("models"), provider=name, where=where)
        if not models:
            problems.append(
                f"[模型] 路由 {name} 一个模型都没声明（\"models\" 是空的），"
                f"所以它不会出现在 /model 里"
            )
        providers.append(Provider(
            name=name,
            base_url=base_url,
            api_key=key,
            models=models,
            display_name=_text(item, "display_name", where=where),
        ))
        # **密钥来自哪里也要说**（`notes`，不是 `problems`）：它是排查"为什么它用的是
        # 旧密钥"唯一的线索，而那种 bug 的症状只是"鉴权失败"或"账单在另一个账号上"。
        notes.append(f"[模型] 路由 {name}：密钥来自{origin}" if key
                     else f"[模型] 路由 {name}：没有密钥")
        if not key:
            problems.append(
                f"[模型] 路由 {name} 没有密钥（{where}），选不了它下面的模型 —— "
                f'写上 "api_key"，或者用 "api_key_env" 指一个环境变量'
            )

    default = (os.environ.get(_MODEL_ENV) or env.get(_MODEL_ENV) or "").strip()
    registry = Registry(providers=tuple(providers), problems=tuple(problems),
                        notes=tuple(notes), source=str(path))
    if not any(item.usable for item in registry.providers):
        problems.append(
            "[模型] 一条可用的路由都没有（每条都缺密钥）—— /model 会摆出一张"
            f"选不了的清单，而 {BUILTIN_API_KEY_ENV} 兜底那条路也没配上"
        )
    if default and registry.find(default) is None:
        problems.append(
            f"[模型] {_MODEL_ENV}={default} 在所有路由里都找不到，已按默认模型继续"
        )
    return replace(registry, problems=tuple(problems))


__all__ = [
    "ALIASES", "BUILTIN_API_KEY_ENV", "BUILTIN_BASE_URL", "BUILTIN_PROVIDER",
    "CatalogError", "DEFAULT_MODEL",
    "MODELS_EXAMPLE_NAME", "ModelRef", "Provider", "Registry", "load",
]
