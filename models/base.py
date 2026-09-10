from abc import ABC, abstractmethod
from typing import Any

from .types import ModelResponse


class ChatModel(ABC):
    """模型统一接口抽象基类"""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """
        调用模型生成响应

        Args:
            messages: 对话消息列表
            tools: 可用的工具定义

        Returns:
            ModelResponse: 统一格式的响应

        Raises:
            ModelTransientError: 暂时性失败（网络、超时、限流、5xx），调用方可以重试
            ModelFatalError: 确定性失败（鉴权、请求格式、模型名），重试没有意义

        实现类必须把 provider 自己的异常翻译成上面两种 —— 调用方不该认识任何
        SDK 的异常类型。
        """
        raise NotImplementedError