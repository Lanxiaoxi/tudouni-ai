from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .types import DeltaSink, ModelResponse


class ChatModel(ABC):
    """模型统一接口抽象基类"""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaSink | None = None,
        on_attempt_started: Callable[[], None] | None = None,
    ) -> ModelResponse:
        """
        调用模型生成响应

        Args:
            messages: 对话消息列表
            tools: 可用的工具定义
            on_delta: 可选的流式回调。给了就**边收边报**（每收到一块调一次，
                只传新增的那一段）；不给就是一次返回完整响应。
                **它是可选的加速，不是另一种契约**：两种情况下本方法都返回
                同一个完整的 ModelResponse，所以重试、审计、会话一致性
                全都不用知道它存在。
            on_attempt_started: 可选的"这一次尝试开始了"通知，**每次尝试各调一次**。
                实现里唯一需要它的场景是"适配层自己重发"（今天只有一例：
                provider 拒绝 `stream_options` 时丢掉它重发）。界面上必须把上一次
                尝试吐出去的半截正文丢掉，否则会看到两段回答首尾相接 —— 而那个
                决定属于调用方，不属于适配层，所以这里只报事实。

        Returns:
            ModelResponse: 统一格式的响应

        Raises:
            ModelTransientError: 暂时性失败（网络、超时、限流、5xx），调用方可以重试
            ModelFatalError: 确定性失败（鉴权、请求格式、模型名），重试没有意义

        实现类必须把 provider 自己的异常翻译成上面两种 —— 调用方不该认识任何
        SDK 的异常类型。

        **`on_delta` 里抛出的异常必须原样穿透**（不翻译、不吞）：它是调用方打断
        一次正在生成的回答的唯一入口（`RunCancelled` 就是这么走的，见
        `agents/agent.py`）。而且它继承 `BaseException`，所以"别用裸
        `except Exception` 把它变成 ModelFatalError"这件事是类型上保证的。
        """
        raise NotImplementedError

    def switch_model(self, model: str) -> None:
        """**可选能力**：换一个模型名（`/model`）。

        它不是抽象方法 —— 因为"能不能中途换模型"是**适配器自己的性质**：一个把模型名
        烘进请求路径、或者一次构造就绑死了模型名的实现做不到，而那种实现照样是一个
        合法的 `ChatModel`。所以默认实现是抛，调用方（`Agent.switch_model`）先
        `getattr` 探一下，探不到就如实报"这个适配器不支持换模型"，而不是把沉默的失败
        记成成功。

        契约只有一条：**换完之后发出的下一个请求用新名字，已经发出去的那一个不受影响。**
        （`complete()` 每次调用时现读，所以这一点对同步实现是自然成立的。）
        """
        raise NotImplementedError(
            f"{type(self).__name__} 不支持运行中换模型"
        )