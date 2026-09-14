"""模型调用的重试策略。

**只重试暂时性失败。** 确定性失败（401、模型名错）立即停 —— 401 重试三次只是把
同一个失败重复三遍，白花时间和钱。「一律重试」是最常见的偷懒写法，也是最贵的。

为什么抽成独立函数而不是留在 Agent 里：它完全不碰会话、工具、权限。输入是
"怎么调模型"，输出是"响应或异常"，中间只多一个 on_attempt 回调用来留痕。

为什么不用 SDK 自带的重试：它的重试对审计日志**完全不可见** —— 一次"成功"的调用
背后可能已经失败过两轮，而日志里只有一条记录、耗时还把重试时间算了进去，看起来
像"模型很慢"。自己管，每一次尝试就是一条可查的事件。
"""

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import (
    DeltaSink,
    ModelFatalError,
    ModelResponse,
    ModelTransientError,
)


MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.5      # 秒
BACKOFF_CAP = 8.0


def _accepts_streaming_kwargs(model: ChatModel) -> bool:
    """这个模型的 `complete()` 认不认流式那两个参数？

    **这不是为了兼容老代码，而是因为"能不出流"是一个真实且合法的能力。**
    测试里那些手写的假模型（`tests/fakes.py` 的 ScriptedModel 等）签名就是
    `complete(messages, tools=None)` —— 它们按同一个契约实现了这个端口，
    只是没有流。硬把 `on_delta=None` 传进去只会换来一个 TypeError，而那看起来
    像"重试策略坏了"，不像"这个模型不会流"。

    判据是**签名里有没有那个参数**（而不是"跑一次看看会不会炸"）：后者会把一个
    真实的 TypeError（比如我们自己传错了参数名）吞成"这个模型不支持流式"。

    结果是按类型缓存的：一次回合要问它好几次，而 `inspect.signature` 不便宜
    —— 关键路径上每一次模型调用都多花几十微秒，不值得（这条路径的对照物是
    一次网络往返，但这个缓存是白捡的）。
    """
    cached = _STREAMING_KWARGS_CACHE.get(type(model))
    if cached is not None:
        return cached
    try:
        parameters = inspect.signature(model.complete).parameters
        # `**kwargs` 也算认（有的实现就是那么写的）。
        accepts = (
            "on_delta" in parameters
            or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        )
    except (TypeError, ValueError):  # pragma: no cover - 内建/被装饰到取不出签名
        accepts = False
    _STREAMING_KWARGS_CACHE[type(model)] = accepts
    return accepts


_STREAMING_KWARGS_CACHE: dict[type, bool] = {}


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
    on_delta: DeltaSink | None = None,
    on_retry: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> ModelResponse:
    """调模型；暂时性失败按指数退避重试，确定性失败直接抛。

    sleep 可注入，测试里换成 no-op —— 验重试逻辑不该真的等 1.5 秒。

    clock 也可以注入，而且**上层会把同一个时钟传下来**：一次回合里所有 duration_ms
    必须出自同一个时钟，否则"模型 1.2s + 工具 0.3s"这种加法就是在混用两把尺子。
    （顺带它也是 model_call 的耗时能被精确断言的前提。）

    ## 流式那两笔代价都落在这个文件的签名上

      * `on_delta` 原样透传 —— 这一层**不认识它是什么**，也不需要认识：它只是
        "把模型吐出来的东西转给谁"；
      * `on_retry` 在**每一次重试之前**调一次（第一次尝试不调）。它存在的唯一
        理由是流式：第 1 次尝试可能已经往界面上吐了半截正文，第 2 次会把整段重说
        一遍 —— 界面必须在第 2 次开始之前把旧的丢掉，否则看到的是两段回答首尾
        相接。这个决定属于调用方（它才知道"丢掉"意味着往哪儿发什么），所以这里
        只报一句"我要重试了"。
    """
    # 流式那两个参数只在对方认的时候才传（见 `_accepts_streaming_kwargs`）。
    # 循环外算一次：它只和模型的类型有关。
    streams = (on_delta is not None or on_retry is not None) and _accepts_streaming_kwargs(model)

    for number in range(1, MAX_ATTEMPTS + 1):
        # **重试之前先报一声。** 它必须在这一次尝试**开始之前** —— 调用方靠它把
        # 上一次吐到界面上的半截正文丢掉，晚了就会和新的一遍混在一起。
        if number > 1 and on_retry is not None:
            on_retry()
        started = clock()

        stream_kwargs: dict[str, Any] = {}
        if streams:
            stream_kwargs = {
                "on_delta": on_delta,
                # 适配层自己重发（今天只有"provider 拒绝 stream_options"那一例）
                # 时也要走同一条路：那也是"上一次尝试的半截正文作废了"。
                # **第一次尝试不传** —— 那时候界面上什么都没有，让前端白清一次
                # 没有意义（注意和 `models/` 那边"每次都调"的口径不同，见
                # `models/base.py` 里那个参数的说明）。
                "on_attempt_started": on_retry if number > 1 else None,
            }

        try:
            response = model.complete(messages=messages, tools=tools, **stream_kwargs)
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
