"""模型调用的重试策略。

**只重试暂时性失败。** 确定性失败（401、模型名错）立即停 —— 401 重试三次只是把
同一个失败重复三遍，白花时间和钱。「一律重试」是最常见的偷懒写法，也是最贵的。

为什么抽成独立函数而不是留在 Agent 里：它完全不碰会话、工具、权限。输入是
"怎么调模型"，输出是"响应或异常"，中间只多一个 on_attempt 回调用来留痕。

为什么不用 SDK 自带的重试：它的重试对审计日志**完全不可见** —— 一次"成功"的调用
背后可能已经失败过两轮，而日志里只有一条记录、耗时还把重试时间算了进去，看起来
像"模型很慢"。自己管，每一次尝试就是一条可查的事件。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelFatalError, ModelResponse, ModelTransientError


MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5      # 秒
BACKOFF_CAP = 8.0


@dataclass(frozen=True)
class Attempt:
    """一次尝试的结果，交给调用方去留痕。

    成功时 response 有值；失败时 error 有值。status 有三种：
    ok / error（暂时性）/ fatal（确定性）。
    """

    number: int
    status: str
    duration_ms: int
    error: str | None = None
    response: ModelResponse | None = None


def call_with_retry(
    model: ChatModel,
    messages: list[dict[str, Any]],
    tools: list[dict],
    on_attempt: Callable[[Attempt], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> ModelResponse:
    """调模型；暂时性失败按指数退避重试，确定性失败直接抛。

    sleep 可注入，测试里换成 no-op —— 验重试逻辑不该真的等 1.5 秒。
    """
    for number in range(1, MAX_ATTEMPTS + 1):
        started = time.perf_counter()

        try:
            response = model.complete(messages=messages, tools=tools)
        except ModelFatalError as exc:
            on_attempt(Attempt(number, "fatal", _elapsed_ms(started), error=str(exc)))
            raise
        except ModelTransientError as exc:
            on_attempt(Attempt(number, "error", _elapsed_ms(started), error=str(exc)))
            if number >= MAX_ATTEMPTS:
                raise
            sleep(min(BACKOFF_BASE * 2 ** (number - 1), BACKOFF_CAP))
            continue

        on_attempt(Attempt(number, "ok", _elapsed_ms(started), response=response))
        return response

    # 循环只可能由 return 或 raise 退出；这行是为了让"函数总有返回值"在类型上成立。
    raise ModelTransientError("重试逻辑异常：未预期的出路")


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
