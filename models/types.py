from dataclasses import dataclass, field
from typing import Any


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
