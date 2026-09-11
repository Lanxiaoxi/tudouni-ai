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
    # 这一次失败之后退避了多久才重试；None 表示不会再重试。
    #
    # 它必须单独记：duration_ms 只覆盖这一次请求的往返，而退避的 0.5 / 1 / 2 秒哪个
    # 请求都不属于。少了这个字段，"失败两次 + 退避 3 秒"在日志里看起来就是两次很快的
    # 调用，多出来的那 3 秒只能靠相邻事件的 ts 去猜 —— 而那正是排查"这一轮为什么这么慢"
    # 时最容易漏掉的一段。
    backoff_ms: int | None = None


def call_with_retry(
    model: ChatModel,
    messages: list[dict[str, Any]],
    tools: list[dict],
    on_attempt: Callable[[Attempt], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> ModelResponse:
    """调模型；暂时性失败按指数退避重试，确定性失败直接抛。

    sleep 可注入，测试里换成 no-op —— 验重试逻辑不该真的等 1.5 秒。

    clock 也可以注入，而且**上层会把同一个时钟传下来**：一次回合里所有 duration_ms
    必须出自同一个时钟，否则"模型 1.2s + 工具 0.3s"这种加法就是在混用两把尺子。
    （顺带它也是 model_call 的耗时能被精确断言的前提。）
    """
    for number in range(1, MAX_ATTEMPTS + 1):
        started = clock()

        try:
            response = model.complete(messages=messages, tools=tools)
        except ModelFatalError as exc:
            on_attempt(Attempt(number, "fatal", _elapsed_ms(started, clock()), error=str(exc)))
            raise
        except ModelTransientError as exc:
            # 退避时长先算出来、随这次尝试一起报出去，然后才真的睡。顺序反过来的话，
            # 事件里就没法带上"接下来要等多久"——而等待期间是没有任何事件可看的。
            last = number >= MAX_ATTEMPTS
            backoff = 0.0 if last else min(BACKOFF_BASE * 2 ** (number - 1), BACKOFF_CAP)
            on_attempt(Attempt(
                number, "error", _elapsed_ms(started, clock()), error=str(exc),
                backoff_ms=None if last else int(backoff * 1000),
            ))
            if last:
                raise
            sleep(backoff)
            continue

        on_attempt(Attempt(number, "ok", _elapsed_ms(started, clock()), response=response))
        return response

    # 循环只可能由 return 或 raise 退出；这行是为了让"函数总有返回值"在类型上成立。
    raise ModelTransientError("重试逻辑异常：未预期的出路")


def _elapsed_ms(started: float, now: float) -> int:
    return int((now - started) * 1000)
