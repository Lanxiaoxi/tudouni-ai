"""测试用的替身。

刻意不用 mock 库：这里需要的是「可预测的假实现」，不是「调用断言」。手写的假模型
在重构时不会假通过 —— MagicMock 会默默接受任何调用方式，等你发现时已经是线上。
"""

import json
from typing import Any

from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse, TokenUsage
from agent_runtime.tools.builtin import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> dict[str, str]:
    """造一条模型发起的工具调用（形状与适配器归一化后的完全一致）。"""
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}


def usage(prompt: int = 10, cached: int = 8, completion: int = 2) -> TokenUsage:
    return TokenUsage(prompt_tokens=prompt, cached_tokens=cached, completion_tokens=completion)


class ScriptedModel(ChatModel):
    """按剧本依次返回响应。

    剧本用完时返回一句占位文本而不是报错 —— 这样测试失败时看到的是断言失败，
    而不是"剧本用尽"这种误导性错误。
    """

    def __init__(self, script: list[ModelResponse]):
        self.script = list(script)
        self.seen_message_counts: list[int] = []

    def complete(self, messages, tools=None):
        self.seen_message_counts.append(len(messages))
        return self.script.pop(0) if self.script else ModelResponse(content="(剧本用尽)")


class AlwaysCallsModel(ChatModel):
    """每一步都要调工具 —— 用来撞 max_steps。"""

    def __init__(self, name: str = "list_files", arguments: dict[str, Any] | None = None):
        self.name = name
        self.arguments = arguments or {}

    def complete(self, messages, tools=None):
        return ModelResponse(
            content=None,
            tool_calls=[tool_call(self.name, self.arguments)],
            usage=usage(),
        )


class ExplodingModel(ChatModel):
    """前 fail_times 次抛指定异常，之后恢复正常 —— 用来验重试。"""

    def __init__(self, exc: Exception, fail_times: int = 999):
        self.exc = exc
        self.fail_times = fail_times
        self.n = 0

    def complete(self, messages, tools=None):
        self.n += 1
        if self.n <= self.fail_times:
            raise self.exc
        return ModelResponse(content="恢复了", usage=usage())


class Collector:
    """收集审计事件。本身可调用，正好直接当 on_event。"""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def __call__(self, record: dict[str, Any]) -> None:
        self.events.append(record)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == kind]


def recording_registry(
    *,
    risk: RiskLevel = RiskLevel.LOW,
    name: str = "list_files",
    result: Any = None,
) -> tuple[ToolRegistry, list[dict[str, Any]]]:
    """只有一个工具的注册表，并记录 handler 到底被执行了几次。

    返回 (registry, calls)。calls 就是"handler 真的跑了吗"的证据 —— 权限和参数
    校验的测试靠它证明"被拦住时 handler 根本没被调用"，而不是只看了返回值。
    """
    calls: list[dict[str, Any]] = []

    def handler(**kwargs):
        calls.append(kwargs)
        return ["<已执行>"] if result is None else result

    registry = ToolRegistry()
    registry.register(Tool(
        name=name,
        description="列出目录",
        risk=risk,
        args_model=ListFilesArgs,
        handler=handler,
    ))
    return registry, calls
