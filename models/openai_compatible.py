from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .base import ChatModel
from .types import (
    ModelError,
    ModelFatalError,
    ModelResponse,
    ModelTransientError,
    TokenUsage,
)


# 值得重试的 HTTP 状态码。SDK 自己的重试策略也认这几个。
_TRANSIENT_STATUS = frozenset({408, 409, 429})


def _to_domain_error(exc: Exception) -> ModelError:
    """把 SDK 异常翻译成领域异常。

    分成两类，因为处置方式完全不同：

      - 暂时性（网络、超时、限流、5xx）→ 退避重试有意义
      - 确定性（401、400、模型名错）→ 重试只是把同一个失败重复三遍

    实测过的对应关系：
        错误的 api_key      -> AuthenticationError (401)  -> Fatal
        不存在的模型名       -> BadRequestError    (400)  -> Fatal
        连不上的地址         -> APIConnectionError        -> Transient

    注意 APITimeoutError 是 APIConnectionError 的子类，所以第一个分支就覆盖了它。
    """
    if isinstance(exc, (APIConnectionError, RateLimitError, InternalServerError)):
        return ModelTransientError(str(exc))

    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in _TRANSIENT_STATUS or (status is not None and status >= 500):
            return ModelTransientError(str(exc))
        return ModelFatalError(str(exc))

    # 认不出来的异常归为 Fatal：未知问题的重试是赌博，失败要快。
    return ModelFatalError(f"{type(exc).__name__}: {exc}")


def _extract_usage(response: Any) -> TokenUsage | None:
    """把 provider 的 usage 归一化成 TokenUsage。

    缓存命中数取 **OpenAI 标准的 `prompt_tokens_details.cached_tokens`**，而不是
    DeepSeek 专有的 `prompt_cache_hit_tokens` —— 这个类叫「OpenAI 兼容」，那就
    别把某个 provider 的扩展字段写成必需项。

    全程 getattr + 默认值：不同网关对 usage 的填充程度差别很大，缺字段不该让
    整次调用失败（token 统计不值得让任务挂掉）。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    details = getattr(usage, "prompt_tokens_details", None)
    return TokenUsage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        cached_tokens=getattr(details, "cached_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


class OpenAICompatibleModel(ChatModel):
    """支持 OpenAI 兼容 API 的模型适配器"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        http_client: httpx.Client | None = None,
    ):
        """
        初始化适配器

        Args:
            api_key: API 密钥
            base_url: API 基础地址（如 https://api.deepseek.com）
            model: 模型名称（如 deepseek-chat）
            http_client: 可选的自定义 HTTP 客户端（用于跳过证书验证等）
        """
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            # 关掉 SDK 自带的重试（默认是 2 次）。它的重试对审计日志完全不可见：
            # 一次"成功"的调用背后可能已经失败过两轮，而日志里只有一条记录、耗时
            # 还把重试时间算了进去 —— 会被误读成"模型很慢"。重试改由 Agent 负责，
            # 这样每一次尝试都是一条可查的事件。
            max_retries=0,
        )
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """调用 OpenAI 兼容 API。

        provider 的异常在这里就被翻译成 ModelTransientError / ModelFatalError，
        不让 openai 的异常类型漏到上层。
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or [],
            )
        except Exception as exc:
            # from exc 保住原始 traceback，排查时还看得到 SDK 那层到底报了什么
            raise _to_domain_error(exc) from exc

        message = response.choices[0].message

        # 解析工具调用
        tool_calls = []
        if message.tool_calls:
            for call in message.tool_calls:
                tool_calls.append({
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                })

        return ModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=_extract_usage(response),
            raw=response,
        )