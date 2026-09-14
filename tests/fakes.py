"""测试用的替身。

刻意不用 mock 库：这里需要的是「可预测的假实现」，不是「调用断言」。手写的假模型
在重构时不会假通过 —— MagicMock 会默默接受任何调用方式，等你发现时已经是线上。
"""

import json
from typing import Any

from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse, TokenUsage
from agent_runtime.state import catalog
from agent_runtime.tools.builtin.ask import ANSWERED, Answer, AskUserArgs
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
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
        # 存整份载荷，而不是只存条数：步数提示是每轮临时拼进请求、不进
        # session.messages 的 —— 只有在这里才看得到它到底发出去了没有。
        self.seen_messages: list[list[dict[str, Any]]] = []

    @property
    def seen_message_counts(self) -> list[int]:
        """每次请求的消息条数。派生值 —— 不再另存一份。"""
        return [len(messages) for messages in self.seen_messages]

    def complete(self, messages, tools=None):
        self.seen_messages.append(list(messages))
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


class ScriptedQuestioner:
    """脚本化的提问者：按顺序吐出预置答案，并记下被问了什么。

    和 ScriptedModel 同一模式（手写的假实现，不用 mock）—— 而且回答里的 waited_ms 是
    预置的，所以"等人回答"那一项在测试里能被钉成精确值，而真实时钟只能断言"大于 0"。

    剧本用完时返回一个**显眼的占位文本**而不是报错：测试失败时看到的应该是断言失败，
    不是"剧本用尽"这种误导性错误（和 ScriptedModel 同一条）。传入 Answer 可以精确控制
    status / waited_ms，传字符串就是一次普通的回答。
    """

    def __init__(self, *replies: str | Answer):
        self.replies = list(replies)
        # 被问了什么 —— 提问这一侧的"handler 真的跑了吗"的证据（见 _prepare/_run）。
        self.asked: list[AskUserArgs] = []

    def __call__(self, question: AskUserArgs) -> Answer:
        self.asked.append(question)
        if not self.replies:
            return Answer("(剧本用尽)", ANSWERED, 0)
        reply = self.replies.pop(0)
        return reply if isinstance(reply, Answer) else Answer(reply, ANSWERED, 0)


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


def context_windows() -> dict[str, int]:
    """当前配置里的 `{模型名: 窗口}`。

    它以前是 `runtime.config.context_windows()`。那个函数在 `ModelConfig` 退休之后**没有
    生产消费者**了（活路径走 `Runtime.model_ref().window`，根本不经过它），所以它也跟着
    走了 —— 测试要那张表就直接问目录。
    """
    return catalog.load().windows()


def model_registry(
    *,
    provider: str = "fake",
    model: str = "fake",
    base_url: str = "http://127.0.0.1:1",
    api_key: str = "sk-x",
    window: int | None = None,
) -> "catalog.Registry":
    """一份最小的模型目录 —— 取代以前那个 `ModelConfig(...)` 注入点。

    装配层现在只认 `catalog.Registry`（`open_runtime(catalog_config=...)`）：用哪条路由、
    哪把密钥、哪个模型本来就是**目录这一层**的事，而 `ModelConfig` 那个类已经随着"配置
    只有一个来源"退休了。

    给测试一个现成的目录，比让每个测试自己拼 `Provider` / `ModelRef` 短一截，也少一处
    会漂的写法。
    """
    ref = catalog.ModelRef(provider=provider, id=model, window=window)
    return catalog.Registry(
        providers=(
            catalog.Provider(name=provider, base_url=base_url, api_key=api_key,
                             models=(ref,)),
        ),
        source=f"测试造的（{provider}）",
    )


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
