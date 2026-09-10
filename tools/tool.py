from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
class Tool:
    name: str
    description: str
    # 必填且没有默认值：加新工具时无法"忘记"声明风险。若给默认值，漏声明的新工具
    # 会静默落到那一档上 —— 默认成 LOW 等于静默放行，是最坏的 fail-open。
    risk: RiskLevel
    args_model: type[ToolArgs]
    handler: ToolHandler

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