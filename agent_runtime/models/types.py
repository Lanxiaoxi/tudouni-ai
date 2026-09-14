from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelError(Exception):
    """模型调用的领域错误基类。

    Agent 只认识这一层，不认识任何 SDK 异常 —— 和 tool_calls、usage 在适配层
    归一化是同一个原则：provider 的细节不该漏到上层。将来换 provider，Agent
    一行都不用改。
    """


class ModelTransientError(ModelError):
    """暂时性失败：网络中断、超时、限流、5xx。

    重试有意义 —— 这类失败下一秒钟可能就好了。
    """


class ModelFatalError(ModelError):
    """确定性失败：401 鉴权失败、400 请求格式错、模型名不存在。

    重试只是把同一个失败重复三遍，白花时间和钱，所以立即停。
    """


@dataclass
class TokenUsage:
    """一次模型调用的 token 用量。

    在适配层归一化，Agent 就不必去碰 provider 专有的响应结构 —— 和 tool_calls
    是同一个做法。

    cached_tokens 是命中前缀缓存的那部分输入。它比未命中便宜大约 50 倍，所以
    成本和未命中要分开记，否则账算不对。
    """

    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0

    @property
    def miss_tokens(self) -> int:
        """未命中缓存的输入 —— 按全价计费的那部分。"""
        return self.prompt_tokens - self.cached_tokens


@dataclass
class ModelResponse:
    """统一模型响应结构"""

    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: TokenUsage | None = None
    # 思维链（思考模式）。它**不是**上一步的回复，而是同一次调用里、模型在给出
    # content / tool_calls 之前先吐的一段草稿；provider 把它和 content 同级返回。
    #
    # 目前只用来显示（--debug 打在 stderr），**不回传**给 API。这条是有代价的：
    # 官方文档说携带 tools 的请求必须完整回传 reasoning_content，否则 400 ——
    # 而本项目每轮都带 tools。当前端点没有严格执行，但这是个已知的偏离，
    # 详见 models/openai_compatible.py 里 _extract_reasoning 的说明。
    reasoning: str | None = None
    raw: Any = None
    # 这一次响应是不是**边收边报**出来的（`on_delta` 非空）。
    #
    # 它只记事实，不做判定：审计里 `model_call.streamed` 读的就是它，而读日志的人
    # 要回答的是"这条回答当时是逐字出现的、还是整段蹦出来的"。少了这个字段，
    # 两种情况在 jsonl 里长得一模一样（同样的 token 数、同样的 duration_ms）。
    streamed: bool = False
    # 逐字报出去的块数。**它不是给成本算账用的**（那是 completion_tokens），
    # 而是用来回答"这个网关到底是不是真的在流" —— 有些兼容网关会先缓冲整段、
    # 再一口气吐出来，那时候它看起来"流式开着"，实际一次往返就结束了。
    stream_chunks: int = 0


# 流式增量的回调。**按块调用**，而且只传"新增的那一段"，不传累计正文 ——
# 累计是每个消费者的自由（协议那一侧让前端自己拼，界面那一侧本来就是拼着画的）。
#
# 为什么是"一个可调用的 sink"而不是 `complete()` 的返回值变成迭代器：
#
#   * `ChatModel.complete()` 的契约是"一次调用换一个完整响应"，重试、审计、
#     会话一致性全都建立在它上面（见 agents/retry.py）。改成生成器会把
#     "一次尝试"变成一个跨越调用方代码的开放状态，失败时的处置就没有地方写了；
#   * 流式是**可选的加速**，不是另一种协议：`on_delta=None` 时适配层必须走
#     今天那条一次返回的老路，行为逐字节不变。
#
# `reasoning` 是思考链那一段（provider 把它和正文放在同一条 delta 流里）。两者
# 可以混着来，调用方按参数名分流，不许自己猜。
#
# 做成 `Protocol` 而不是 `Callable[[str, str], None]`：两个参数都是字符串，
# 按位置传一次就会把思考链和正文对调，而那个错误在界面上的症状是"答案里混进了
# 一段自言自语"—— 看起来像模型的问题，不像调用点写错了。
class DeltaSink(Protocol):
    def __call__(self, text: str = "", reasoning: str = "") -> None: ...
