"""思考模式：开关、强度，以及它们怎么变成请求参数。

## 两个旋钮，不是一回事

| 旋钮 | 问的问题 | 取值 |
|---|---|---|
| `thinking` | 要不要让模型先想一段再答 | `on` / `off` |
| `effort` | 想的时候花多大力气 | `low` / `high` / `max` |

它们分开是因为**它们是两个问题**："关掉思考"和"想得少一点"在账单上和延迟上都不一样，
而合成一个（`effort=off`）之后，"关着的时候强度是什么"就变成一个必须回答、又没人
关心的问题。

## 这三个强度值是从端点问出来的，不是抄来的

DeepSeek 的 OpenAI 兼容端点接受 `none / minimal / low / medium / high / xhigh / max`
七个值（实测：非法值直接 400，而报错里列出了全部合法值），官方那张映射表把它们折成
`low / high / max` 三档：

    minimal, low        → low
    medium, high, xhigh → high
    max, ultra          → max

所以这里**只提供那三个规范值**，不做"把 minimal 当 low 存下来"这种事：多出来的四个
名字在映射之后没有任何区别，摆在清单里只会让人以为它们不一样。`none` 也不收 ——
它是"关掉思考"的另一种写法，而那个开关已经有名字了（`thinking=off`）。

**强度在关掉思考时仍然存着**（只是不发给端点）：用户的意图是"下次打开时还是这个强度"，
而不是"关掉思考顺便把强度清掉"。实测：`thinking=disabled` + `reasoning_effort=max`
不会报错，effort 被忽略 —— 但我们**照样不发**，因为"关着"这件事在请求里说一遍就够了，
多发一个没人读的字段只会让抓包的人以为它在生效。

## 一个未知强度不许静默降级

`resolve_effort` 认不出来时返回 None，由调用方决定怎么说 —— **不就近匹配**：把
`/effort hgih` 猜成 `high` 会让用户以为设置生效了，而它确实生效了（只是不是他想的
那个值）。这和 `/model` 打错不猜是同一条规矩。
"""

from __future__ import annotations

# 思考模式的开关。**默认开** —— 端点的默认行为就是开（实测：不带任何参数时
# `reasoning_content` 照样返回），而不改默认值的接口才不需要解释。
DEFAULT_THINKING = True

# 强度的规范三档。顺序是**从省到费**，清单和提示都按这个序打。
EFFORT_LEVELS: tuple[str, ...] = ("low", "high", "max")

# 端点默认的强度。官方文档写着"effort 默认为 high"，而我们的默认值必须和它一致 ——
# 否则"我们没设置"和"我们设成 high"在端点上就是两个不同的请求，而界面显示的那个
# 强度会是一个我们自己编出来的数。
DEFAULT_EFFORT = "high"

# 别名：端点接受、但折算之后没有区别的那些名字。
#
# **收下它们、但不列进 `EFFORT_LEVELS`**：`minimal` 和 `low` 在端点上完全等价，
# 摆出来只会让人以为它们不一样。而认它们的理由和模型别名一样 —— 配置文件里可能
# 已经写着它们（别的工具、别人的示例），而"能用的一档被判成不认识"是最没必要的意外。
ALIASES: dict[str, str] = {
    "minimal": "low",
    "medium": "high",
    "xhigh": "high",
    "ultra": "max",
}

# `none` 是"关掉思考"的另一种写法。它**不在别名表里**，因为它同时意味着开关那个维度
# 的变化 —— 折算成三档里的任何一个都是错的。所以它有自己的判别函数。
OFF_ALIASES = frozenset({"none", "off", "disabled", "false", "no"})


def resolve_effort(text: str) -> str | None:
    """把用户写的一段字折算成三档之一；认不出来返回 None（**不猜**）。"""
    name = (text or "").strip().lower()
    if name in EFFORT_LEVELS:
        return name
    return ALIASES.get(name)


def is_off(text: str) -> bool:
    """这段字是不是"关掉思考"的写法（`none` / `off` / `disabled` …）。"""
    return (text or "").strip().lower() in OFF_ALIASES


def resolve_thinking(text: str) -> bool | None:
    """把用户写的一段字折算成开关；认不出来返回 None（**不猜**）。

    认的那些词是给人写的，不是给机器解析的 —— `/thinking on` 和 `/thinking 开` 都该
    能懂，而一个只认 `true` 的命令在中文界面里是荒谬的。
    """
    name = (text or "").strip().lower()
    if name in ("on", "开", "true", "yes", "1", "enabled"):
        return True
    if name in ("off", "关", "false", "no", "0", "disabled", "none"):
        return False
    return None


def thinking_text(value: bool) -> str:
    """开关在界面上的说法。**两个状态都要能读出来**（不是"关着就不显示"）。"""
    return "开" if value else "关"


def request_fields(*, thinking: bool, effort: str) -> dict:
    """这一对设置 → **发请求时要带的那两个参数**。

    返回的是"参数名 → 值"，让适配层照着往请求里塞 —— 判定（要不要带、带哪个值）
    留在这里，因为它是这两个旋钮的知识；适配层只该认识"怎么把一个 dict 塞进请求"。

    三条实测得来的事实，每一条都对应这里的一个决定：

      1. **`reasoning_effort` 走顶层，不走 `extra_body`。** 官方那篇「思考模式」的
         样例把 `reasoning_effort` 和 `extra_body={"thinking": …}` 并列写，而 API 参考
         里又把它画在 `extra_body` 那一段里 —— 两种写法实测都能用。选顶层是因为它是
         OpenAI 的原生参数，SDK 认它（有类型、有补全），而 `extra_body` 是个逃生口。
         少用一个逃生口，就少一条"换个网关之后这个参数被吃掉"的路。
      2. **关掉思考时只发 `thinking`，不发 `reasoning_effort`。** 实测两者同时发不会
         报错（effort 被忽略），但那个字段没人读 —— 抓包的人会以为它生效了。
      3. **打开思考时两个都发。** `thinking: {type: "enabled"}` 端点上本来就是默认值，
         显式发一遍是为了**让请求自己说清它要什么**：默认值会随端点变，而这个配置是
         用户选的。多一个字段换"这条请求在任何时候都是同样的意思"，值得。
    """
    fields: dict = {"reasoning_effort": effort} if thinking else {}
    fields["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return fields


def summary(*, thinking: bool, effort: str) -> str:
    """`开 · high` 这样的一句，给 `/status` 和状态栏用。

    关掉思考时**不写强度**（写"关 · high"会让人以为 high 还在生效）—— 但强度并没有
    被丢掉，`/thinking on` 之后它还是原来那个。
    """
    return f"{thinking_text(thinking)} · {effort}" if thinking else thinking_text(thinking)


__all__ = [
    "ALIASES", "DEFAULT_EFFORT", "DEFAULT_THINKING", "EFFORT_LEVELS", "OFF_ALIASES",
    "is_off", "request_fields", "resolve_effort", "resolve_thinking", "summary",
    "thinking_text",
]
