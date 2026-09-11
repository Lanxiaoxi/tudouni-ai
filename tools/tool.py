from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


JsonSchema = Mapping[str, Any]
ToolHandler = Callable[..., Any]


class RiskLevel(str, Enum):
    """工具的风险等级。

    混入 str 是为了让配置文件（JSON/YAML）里读进来的普通字符串 "low" 也能直接
    和枚举互相比对、放进同一个 set：str 在 MRO 里排在 Enum 之前，取到的是
    str.__hash__，所以两个方向的成员判断都成立。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolArgs(BaseModel):
    """所有工具参数模型的基类。

    extra="forbid" 是这里的关键：Pydantic 默认是 "ignore"，会把模型多传的参数
    静默丢弃 —— 那样「模型读错了 schema」这件事就被藏起来了。统一改成拒绝，
    并且它会在生成的 schema 里写出 additionalProperties: false，让模型提前看到。
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        """剥掉 schema 顶层的 description。

        Pydantic 会把模型类的 docstring 自动写成 schema 的 description，于是这些
        面向开发者的内部注释会被原样发给模型（而且每次请求都发）。工具对模型的
        说明由 Tool.description 负责，所以这里统一剥掉，避免噪声和内部注释外泄。
        只删顶层；字段级的 description（来自 Field(description=...)）保留。
        """
        json_schema = handler(core_schema)
        json_schema.pop("description", None)
        return json_schema


@dataclass(frozen=True, slots=True)
class ToolResult:
    """比"一段文本"多一点的返回值：回灌给模型的文本 + 只有工具自己知道的审计字段。

    绝大多数工具直接返回字符串（`_run` 把它当文本用），不需要这个类型。需要它的只有
    那种**耗时里含等人的时间**的工具（ask_user）：它的 duration_ms 会落进 tool_result
    事件，而等人那一段在汇总里必须能减出来（见 cli.summarize_time）—— 否则"我看了 30 秒
    才回答"会显示成"这个工具花了 30 秒"，和当年审批那条一模一样。

    所以 audit 里放的是**工具唯一知道、而 Agent 推不出来**的事实，不是"随便什么元数据"：
    它已经能看见 tool 名、status、chars、duration_ms，别把那些再抄一遍。
    """

    text: str
    audit: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    # 必填且没有默认值：加新工具时无法"忘记"声明风险。若给默认值，漏声明的新工具
    # 会静默落到那一档上 —— 默认成 LOW 等于静默放行，是最坏的 fail-open。
    risk: RiskLevel
    args_model: type[ToolArgs]
    handler: ToolHandler

    # 这个工具能不能和其他工具**同时**执行。声明在工具自己身上，而不是在
    # agent.py 里按名字写一张白名单 —— 理由和 risk 一样：知道"它有没有副作用"的
    # 人在注册处，不在循环里。
    #
    # 和 risk 不同，这里**带默认值**，而且那个不对称是有理由的：risk 缺省成 LOW
    # 是 fail-open（静默放行），而 parallel_safe 缺省成 False 是 fail-closed ——
    # 漏声明的后果只是"这条没并行"，慢一点，不会错。所以不需要强制每个工具都写它。
    #
    # 判定标准只有一条：**这个 handler 有没有副作用。** 有副作用（写文件、执行命令、
    # 改任何共享状态）就不能并行，因为同批调用之间没有任何隔离 —— 见 agent.py 里
    # 那段"为什么整批才能并行"。
    #
    # 另有一条注册期硬约束（见 ToolRegistry.register）：parallel_safe 的工具必须
    # 是 LOW。要并行就不能在批内弹审批 —— asker 走的是 stdin，两条审批同时问会
    # 互相抢输入，而"谁批准了哪一条"也没法回答了。
    parallel_safe: bool = False

    # 这个工具的 handler 会**阻塞在人的输入上**（目前的唯一一个：ask_user）。
    #
    # 为什么不能只靠"记得别给它标 parallel_safe"：ask_user 的风险是 LOW，而上面那条
    # 注册期校验只管"标了并行却不是 LOW" —— 于是"会问人"+"标了并行"这个组合**能**
    # 通过校验，跑到真实会话里才变成两条提问互相抢 stdin，而"谁回答了哪一条"也就没
    # 法回答了。这个字段把那句话变成启动时就炸的坏配置（和 risk 不给默认值是同一个
    # 手法：让"忘了"这件事不可能悄悄发生）。
    #
    # 默认 False 是 fail-closed：漏声明的后果只是"少一条启动期校验"，不是"多问了一次人"。
    interactive: bool = False

    @property
    def parameters(self) -> JsonSchema:
        """参数 schema 从 args_model 推导，不再手写第二份。

        手写的 schema 和校验规则是同一个事实的两个来源，早晚会漂移。这里让
        args_model 成为唯一来源，schema 只是它的一种渲染结果。
        """
        return self.args_model.model_json_schema()

    def to_openai_schema(self) -> JsonSchema:
        """转换成 OpenAI-compatible API 使用的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }

    def to_schema(self, format: str = "openai") -> JsonSchema:
        """根据格式转换为对应 schema。"""
        if format == "openai":
            return self.to_openai_schema()
        else:
            raise ValueError(f"Unknown format: {format}")

    def execute(self, args: Mapping[str, Any]) -> Any:
        """校验参数后再执行。

        校验失败会抛 pydantic.ValidationError，由调用方决定怎么反馈给模型。
        """
        validated = self.args_model.model_validate(args)
        return self.handler(**validated.model_dump())


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already exists: {tool.name}")

        # 并行的前提之一是"这一批不弹审批"（asker 走 stdin，两条审批同时问会互相抢
        # 输入，而"谁批准了哪一条"也就没法回答了）。LOW 是"默认不用问人"的那一档，
        # 所以把这条约束钉在注册处：坏组合在**启动时**就炸，而不是任务跑到一半才
        # 出怪事 —— 入口本来就会把注册表打印一遍，报错的位置正好是看得见的地方。
        #
        # 它是**必要条件，不是充分条件**：LOW 说的是"不用问人"，不是"没有副作用"。
        # 真正保证安全的是写这个工具的人读过 handler、确认它只读 —— 那件事没有
        # 任何类型能表达，只能靠声明。所以这条校验挡的是"标了并行却要弹审批"，
        # 挡不住"把一个会写的工具标成 LOW 又标成 parallel_safe"。
        #
        # 用 != 而不是 is not：RiskLevel 混了 str，所以配置里读进来的普通字符串
        # "low" 和 RiskLevel.LOW 相等（见上面那个枚举的注释）。
        if tool.parallel_safe and tool.risk != RiskLevel.LOW:
            raise ValueError(
                f"{tool.name} 声明了 parallel_safe，但风险等级是 {tool.risk.value}："
                f"能并行执行的工具必须是 low。\n"
                f"  并行的前提是批内不弹审批（审批走 stdin，两条同时问会互相抢输入）。"
            )

        # 和上面那条是同一个理由的另一半：会问人的工具**永远**不能并行，而且它跟风险
        # 等级无关 —— 上面那条拦不住它（LOW 是自动放行档，正好绕开）。两条合起来才
        # 说完"能并行的前提是批内没有任何人在说话"。
        if tool.interactive and tool.parallel_safe:
            raise ValueError(
                f"{tool.name} 会阻塞在人的输入上（interactive），不能声明 parallel_safe："
                f"两条提问同时问会互相抢输入，而「谁回答了哪一条」也就没法回答了。"
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")

        return self._tools[name]

    def all(self) -> list[Tool]:
        """按注册顺序返回所有工具。

        有这个方法，入口就不必去碰 _tools —— 私有属性被外部读一次，就等于把
        "内部结构"变成了事实上的公开接口。
        """
        return list(self._tools.values())

    def schemas(self, format: str = "openai") -> list[dict]:
        """返回所有工具的 schema 列表。"""
        return [tool.to_schema(format) for tool in self._tools.values()]