"""模型目录与提供方路由：**一个模型去哪、叫什么、有什么能力**。

## 这一份数据回答四个问题

  1. **请求发到哪**（provider → base_url + 密钥）；
  2. **有哪些模型能选**（`/model` 那份清单）；
  3. **每个模型有什么能力**（上下文窗口、能不能看图）；
  4. **默认用哪个**。

四件事必须同一份数据：各写一份的话，"清单里能选、发出去报模型不存在"这类漂移
全都是静默的。

## 配置文件：`~/.tudouni/config.json` 的 `providers` 段

它**跟着人走，不跟着工作区走**：换个项目干活不该换密钥。文件的定位、`web` 段、以及
"为什么不再读 `.env`"都写在 `agent_runtime/userconfig.py` 里 —— 这个模块只负责
**解释 `providers` 段**，不负责找文件、也不负责读它。

```jsonc
{
  "providers": { "deepseek": { "base_url": "…", "api_key": "sk-…",
                               "models": [{"id": "deepseek-flash", …}] } }
}
```

**密钥就写在这条路由里**（`api_key`）：这份文件在用户级目录、不进版本库、也不在工作区里
被 agent 改（控制面那条只保护 `.tudouni/`，而这一份恰好也在那底下）。填上它这条路由就能
用，**就结束了** —— 配置只有这一个来源：不看真实环境变量、也不看 `.env`。要换密钥就改这
个文件，没有第二个地方可改（"两个地方都能放、只有一个生效"正是最难排查的那种形态）。

**没有 `providers` 就是一条路由也没有。** 那会以一句能照着改的话收场（见
`composition._no_model_message`），而不是悄悄退到某条内置路由上去。以前那条"不配置也能
跑"的兜底路由读的是环境变量（`DEEPSEEK_API_KEY`），它随环境变量一起退休了。

## 坏配置一律降级 + 出声，绝不拦启动

一条路由起不来（缺密钥、名字写错）只该少一个选择，不该让整个会话开不出来 —— 这和
`SkillCatalog` 的 `problems` / `shadowed` 是同一条处置。**但必须说出来**：`problems()`
返回的那些行由启动路径打到 stderr，因为"文件明明在那儿却完全不起作用"是最坏的失败
形态（人反复改一份不生效的配置，而真正生效的在别处）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime import userconfig
from agent_runtime.state import reasoning

# 文件的定位与读取**全在 `userconfig`**（那是个只 import 标准库和 `paths` 的叶子）。
# 这里刻意不再自己算路径、也不再自己读 JSON：
#
#   * 同一份文件被两个模块读（这里要 `providers`，`runtime/config.py` 要 `web`），
#     各写一份"去哪找、怎么读"就是同一件事的第二份算法 —— 而那正是 `paths.py` 那段
#     docstring 记着的那次事故（三份算法里有一份写死了目录名，改名之后静默失效）；
#   * **仍然不 import `runtime.config`**：那个模块反过来要 import 本模块，环会让
#     "哪个先加载"变成一个必须小心维持的顺序。
MODELS_EXAMPLE_NAME = userconfig.EXAMPLE_FILE_NAME

# 一条路由的合法键。**不认识的键直接报错，不忽略** —— 和 `permissions.json` /
# `mcp.json` 同一条规矩：写错一个键名而它静默不生效，是最坏的失败形态。
#
# 这里**没有 `api_key_env`**：它要的是"环境变量的名字"，也就是把密钥放在**另一个地方**
# 再指过来，而配置的唯一来源是这份文件。它现在落进"不认识的键"，所以谁写了它当场就会被
# 指出来 —— 这正是一次实测换来的：有人把**密钥本身**填进了那个字段（它和 `api_key` 长得
# 太像，一个装名字、一个装值），而旧代码只会说"没有密钥"，用户手里明明有一把填进去的
# 密钥，于是只能来问"为什么"。
_PROVIDER_KEYS = frozenset({
    "display_name", "base_url", "api_key", "models", "verify",
})
# 模型那几个**可选**的说明性字段（`label` / `summary` / `note`）与 `vision` 仍然收下 ——
# 它们是 `/model` 那份清单要显示的东西。模板里不出现它们（那才是给人抄的那份，越短越好），
# 但 schema 认，因为"这条路上这个模型叫什么、有什么能力"是配置的事实。
_MODEL_KEYS = frozenset({
    "id", "label", "context_window", "summary", "note", "vision",
    "reasoning_effort",
})


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
    verify: bool = True  # 是否验证 SSL 证书，默认为 True

    @property
    def title(self) -> str:
        return self.display_name or self.name

    @property
    def usable(self) -> bool:
        """**有没有密钥。** 没密钥的路由照样留在目录里，但不能被选中。"""
        return bool(self.api_key)

    def find(self, model: str) -> ModelRef | None:
        """这条路由上的某个模型。名字**原样比**，不做任何折算。"""
        wanted = (model or "").strip()
        for item in self.models:
            if item.id == wanted:
                return item
        return None


@dataclass(frozen=True, slots=True)
class Registry:
    """这一台机器上**认识的全部模型**，加它们的路由。"""

    providers: tuple[Provider, ...] = ()
    # 启动时那些"该看一眼"的说明。**分两档**：`problems` 是"这条路由用不了 / 你的配置
    # 没生效"，`notes` 是"它好着呢，这是它的事实"。混在一起的话，真正的问题会被淹没在
    # 正常信息里。
    #
    # `notes` 现在**没人往里放东西**了：它以前装的是"这条路由的密钥来自哪个环境变量"，
    # 而那个问题在"配置只有一个来源"之后没有答案可给（密钥就在这份文件里，看一眼就知道）。
    # 字段留着是因为它在协议里是一条已经存在的出站信息，删它要动协议版本 —— 那和这次
    # 收口无关。
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # 这份目录是哪来的（给 `/status` 和启动那行说明用）。**它是事实，不是装饰**：
    # "为什么我改的配置没生效"这个问题的答案就是这一个字符串。
    source: str = ""

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
        hits = [item for item in self.models() if item.id == wanted]
        return hits[0] if len(hits) == 1 else None

    def ambiguous(self, name: str) -> tuple[ModelRef, ...]:
        """这个名字是不是落在多条路由上（那就要写 `provider/model`）。"""
        wanted = (name or "").strip()
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
        """`{模型名: 窗口}` —— 只含**知道窗口**的那些。

        界面拿它当占比的分母；表里没有这个名字时只报用量、不报占比（错的百分比比没有
        百分比更坏，它会被当成真的）。
        """
        return {item.id: item.window for item in self.models() if item.window is not None}


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


def _api_key(raw: dict, *, where: str) -> str:
    """这条路由的密钥：**就写在它自己里面**（`api_key`）。

    这里以前还有一条 `api_key_env` 分支（先查真实环境变量、再查同一份文件的 `env` 段）。
    它退休了，理由写在模块 docstring 里：配置只有一个来源，就是这份文件。而那个字段名
    和 `api_key` 长得几乎一样、一个装名字一个装值 —— 实测有人把密钥填进了装名字的那个，
    然后只拿到一句"没有密钥"。
    """
    return _text(raw, "api_key", where=where)


def load(path: Path | None = None) -> Registry:
    """读那份配置的 `providers` 段。**永远返回一个 Registry，坏消息放在 `problems`。**

    `path=None` 时读 `userconfig.config_file()`（默认 `~/.tudouni/config.json`，可以用
    `AGENT_CONFIG_FILE` 顶掉）。**没有那个文件、或者它里面没写 `providers`**，就是一条
    路由都没有 —— 那由调用方报一句能照着改的话（`composition._no_model_message`），不在
    这里造一条兜底路由。

    **形状错误抛 `CatalogError`（`UserConfigError` 的子类），语义问题进 `problems`。**
    这条分界线是有意的：读不懂再猜也没意义（停下让人改），而"某条路由缺密钥"只是少一个
    选择，不该让会话开不出来。
    """
    cfg = userconfig.read(path)
    path = cfg.path
    providers_raw = cfg.providers

    problems: list[str] = []
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
        key = _api_key(item, where=where)
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
            verify=_flag(item, "verify", where=where, default=True),
        ))
        if not key:
            problems.append(
                f"[模型] 路由 {name} 没有密钥（{where}），选不了它下面的模型 —— "
                f'在**这条路由里**写上 "api_key"'
            )

    # **没有第二处可看**：密钥、端点、模型清单都只在这份文件里，所以这里不再有"密钥来自
    # 哪个环境变量"那种说明（那是以前留给"两个地方都能放"的线索）。
    if providers and not any(item.usable for item in providers):
        problems.append(
            "[模型] 一条可用的路由都没有（每条都缺密钥）—— /model 会摆出一张选不了的清单"
        )
    return Registry(providers=tuple(providers), problems=tuple(problems),
                    source=str(path))


__all__ = [
    "CatalogError", "MODELS_EXAMPLE_NAME", "ModelRef", "Provider", "Registry", "load",
]
