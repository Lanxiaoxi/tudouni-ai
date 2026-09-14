import sys
from collections.abc import Callable
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
    DeltaSink,
    ModelError,
    ModelFatalError,
    ModelResponse,
    ModelTransientError,
    TokenUsage,
)
# **`models/` → `state/` 是这一版新加的依赖**，只有这一个模块用一个纯数据的子模块
# （`state/reasoning.py`：思考开关与强度 → 请求参数）。它值得这个依赖，因为
# "改完之后下一次请求就用新值"这条契约要求值**在每次调用时现读**，而把
# `reasoning_effort=` 这种参数拼装写在两处（一次非流式、一次流式）就会漂。
#
# `state/reasoning.py` 自己不 import 任何东西（连 `dataclass` 都不用）—— 所以这条边
# 是单向的、没有环，也不会把 store / session 那一套拖进模型的加载路径。
from agent_runtime.state.reasoning import DEFAULT_EFFORT, DEFAULT_THINKING, request_fields


# 值得重试的 HTTP 状态码。SDK 自己的重试策略也认这几个。
_TRANSIENT_STATUS = frozenset({408, 409, 429})


class StreamAborted(BaseException):
    """**别用这个类。** 它只是一个类型上的占位，用来把"调用方在 `on_delta` 里
    抛了东西"这件事和"provider 失败了"分开。

    真正在用的是 `agents.RunCancelled`（它同样继承 `BaseException`，理由见那里的
    docstring）。这里不 import 它，因为依赖方向是 `agents → models`，反过来会成环
    —— 所以适配层认不出那个类，只能按"它是 `BaseException` 而不是 `Exception`"
    来放行。本类不参与任何逻辑，留着只是为了让这个模块的异常族是完整的。
    """


# 流式请求要不要顺便要一份 usage。见 `_complete_streaming` 那段。
STREAM_USAGE_OPTION = {"include_usage": True}

# 哪些 (base_url, model) 已经证明"不接受 stream_options"。
#
# 为什么是模块级的：一次 400 换来的知识必须留住，否则**每次模型调用**都要
# 先撞一次 400 再重发 —— 那是每一轮都白花一个往返。它的键是 (base_url, model)
# 而不是实例，因为 `session_switch` 会重新装配一个 model 实例（换会话 = 重来一遍
# 装配），而"这个网关收不收这个参数"跟会话没有关系。
#
# 为什么不是配置项：这是**探测出来的事实**，不是人的偏好。让人去写
# "我的网关支不支持 include_usage"，写错的那一侧症状是每一轮都失败一次。
_NO_STREAM_OPTIONS: set[tuple[str, str]] = set()


def _stream_options_supported(base_url: str, model: str) -> bool:
    return (base_url, model) not in _NO_STREAM_OPTIONS


def _is_stream_options_rejection(exc: Exception) -> bool:
    """这个 400 是不是"我不认 stream_options"。

    **只认这一个原因。** 把别的 400（模型名错、消息格式错）也让这里认下来，
    就会把一次"重发也没用"的失败变成一次多余的重发 —— 而且失败原因会被那句
    "这个网关不接受 stream_options" 盖住，正好是排查时最需要看到的东西。
    """
    if not isinstance(exc, APIStatusError):
        return False
    if getattr(exc, "status_code", None) != 400:
        return False
    body = getattr(exc, "body", None)
    text = f"{exc} {body if body is not None else ''}".lower()
    return "stream_options" in text or "include_usage" in text


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

    **`BaseException` 不是 `Exception` 的东西到不了这里**（`RunCancelled` 就是
    那样设计的）：调用点用的是 `except Exception`，所以它天然穿透 —— 这正是
    "在 on_delta 里抛异常 = 打断这一轮"能成立的原因。
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

    **流式的最后一个 chunk 也走这里**：那个 chunk 的 `choices` 是空数组、只有
    `usage`，所以判据必须是"有没有 usage"而不是"是不是一个完整的响应"。没有它
    （网关不认 `stream_options`、或者干脆不填），agent 那边就少几行 token 数字
    —— 比报一个 0 好：0 会被读成"这次没花钱"。
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


def _tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归一化工具调用：这里产出的是**项目自己的形状**，上层只认这三个键。

    非流式那条路的 `arguments` 是 provider 给的一整串 JSON 字符串，流式那条路是
    一段一段拼起来的 —— 归一化之后没有区别，所以 `agents/agent.py` 的
    `json.loads(call["arguments"])` 两条路共用。
    """
    return [
        {
            "id": call["id"],
            "name": call["name"],
            "arguments": call["arguments"],
        }
        for call in tool_calls
    ]


def _extract_reasoning(message: Any) -> str | None:
    """取思维链（思考模式的 `reasoning_content`）；没有就返回 None。

    **取出来只为显示和审计，不回传。** 这里记一笔已知的偏离，免得下一个读代码的人以为
    "既然读了为什么不回传"：官方文档说携带 `tools` 的请求**必须完整回传**
    `reasoning_content`（即使那一轮没实际调用工具），否则 API 返回 400；而本项目
    每一轮都带 tools（7 个工具全量发出去）。当前端点没有严格执行这一点（实测跑得通），
    但换端点、或者网关收紧之后，症状会是"这一步直接发不出去"（ModelFatalError）。
    第二个代价小一些但真实：不回传等于模型每一步都重新想，多步任务的连贯性会打折。

    getattr 而不是属性访问：不同网关对 message 的填充程度差别很大 —— `_extract_usage`
    那段注释里已经为同一件事吃过一次亏。空串也当没有（有的网关用空串占位）。
    """
    value = getattr(message, "reasoning_content", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


class _StreamAccumulator:
    """把一条 SSE 增量流拼回一个完整的响应。

    **它是这个模块里唯一需要单独测的东西**，所以它不碰网络、不碰 SDK 的客户端
    —— 喂几个假 chunk 进去就能验"按 index 拼工具调用"这些规则（见
    `tests/test_streaming.py`）。混在 `complete()` 里的话，验一条拼装规则要先起一个
    假网关。

    三条实测得来的规则：

      1. **`tool_calls` 必须按 `index` 拼**，不能按到达顺序。同一批里两个
         `read_file` 的增量在流里是交错的（`index=0` 的参数段、`index=1` 的名字、
         再回到 `index=0`），按顺序拼会把两个调用的参数合成一段 —— 那段 JSON 解
         不出来，而症状是"模型这一步给的工具参数不合法"，看起来像模型的问题；
      2. **`name` 累积、`arguments` 累积、`id` 取第一个非空。** 官方形状是
         name/id 只在第一块出现，但有的网关会把 name 重复发；追加对"重复发"是错的，
         所以 name 只在还是空串时收；
      3. **`index` 可能整个缺席**（非官方网关）。那时候整段流只有一条工具调用，
         按 0 攒 —— 没有第二条可以跟它混。
    """

    __slots__ = ("_content", "_reasoning", "_tools", "_usage", "_finish")

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        # index -> {"id", "name", "arguments"}，用 dict 保住**下标顺序**
        # （Python 的 dict 按插入序，而 0/1/2 的插入序就是模型给的顺序）。
        self._tools: dict[int, dict[str, str]] = {}
        self._usage: TokenUsage | None = None
        self._finish: str | None = None

    # -- 收一块 ---------------------------------------------------------------

    def feed(self, chunk: Any) -> None:
        """吃一块。usage 那一块没有 choices，所以两条路要分开判。"""
        if (usage := _extract_usage(chunk)) is not None:
            # **覆盖而不是累加**：这份 usage 覆盖的是整次请求，不是这一块。
            # 累加会把一个 1500 token 的回答报成几百万（实测过类似的账）。
            self._usage = usage

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        if (reason := getattr(choice, "finish_reason", None)) is not None:
            self._finish = reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return

        if text := getattr(delta, "content", None):
            self._content.append(text)
        # 思考链和正文在**同一条 delta 流**里，按字段名分流。有的网关把它放在
        # `reasoning` 而不是 `reasoning_content`，两个都认。
        if part := (getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)):
            self._reasoning.append(part)

        for call in getattr(delta, "tool_calls", None) or []:
            self._feed_tool_call(call)

    def _feed_tool_call(self, call: Any) -> None:
        raw_index = getattr(call, "index", None)
        index = raw_index if isinstance(raw_index, int) else 0
        slot = self._tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id := getattr(call, "id", None):
            slot["id"] = slot["id"] or call_id
        function = getattr(call, "function", None)
        if function is None:
            return
        if name := getattr(function, "name", None):
            slot["name"] = slot["name"] or name
        if arguments := getattr(function, "arguments", None):
            slot["arguments"] += arguments

    # -- 交出去 ---------------------------------------------------------------

    @property
    def content(self) -> str | None:
        """拼好的正文。**一块都没有时是 None，不是空串** —— 和 provider 的
        非流式响应一致（`message.content` 在纯工具调用那一轮就是 null），
        而 `Session` 里存 null 与存 "" 会在历史里长得不一样。"""
        text = "".join(self._content)
        return text or None

    @property
    def reasoning(self) -> str | None:
        text = "".join(self._reasoning)
        return text or None

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for index in sorted(self._tools):
            slot = self._tools[index]
            if not slot["name"] and not slot["arguments"]:
                # 空槽（有的网关会先发一个只带 id 的占位块）：丢掉，别让上层看到
                # 一个没有名字的调用 —— 那会变成一条"工具名不认识"的错误结果。
                continue
            calls.append({
                "id": slot["id"] or f"call_{index}",
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            })
        return calls

    @property
    def usage(self) -> TokenUsage | None:
        return self._usage

    @property
    def finish_reason(self) -> str | None:
        return self._finish

    @property
    def chunks(self) -> int:
        """收到多少块正文/思考。审计里记它（见 `ModelResponse.stream_chunks`）。"""
        return len(self._content) + len(self._reasoning)

    # -- 降级 -----------------------------------------------------------------

    def as_response(self) -> ModelResponse:
        """把攒下的东西交出去。**降级重发前也要用**：第一段流里已经收到的正文
        是**保真的**（它只是没带 usage 而已），丢掉它等于让模型白说一遍。"""
        content, reasoning = self.content, self.reasoning
        return ModelResponse(
            content=content,
            tool_calls=_tool_calls(self.tool_calls),
            usage=self.usage,
            reasoning=reasoning,
            streamed=bool(content or reasoning or self._tools),
            stream_chunks=self.chunks,
        )


class OpenAICompatibleModel(ChatModel):
    """支持 OpenAI 兼容 API 的模型适配器。

    ## 它是一个"能换路线"的适配器，不是一条固定连接

    这一版里 provider 是可选的（`/model` 能在多条路由之间选），所以这个对象同时记着
    **当前这条路线**（`route` / `base_url` / `api_key`）和**怎么换过去**（`install`）。

    ## 客户端是**懒造**的

    换路由要换 `OpenAI(...)`（base_url 和密钥都是构造参数、且 SDK 的 client 是连接池），
    而"造一个客户端"会建连接池、读代理环境变量 —— 一个**只用来看一眼 `/model` 清单**的
    会话不该为它付钱，一个装配到一半就失败的会话更不该留下它。

    所以 `self.client` 可能一直是 None：`complete()` 之前会 `_ensure_client()`。
    调用方别去碰 `self.client`（它是实现细节，见 `_ensure_client` 那段）。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        http_client: httpx.Client | None = None,
        *,
        provider: str = "",
        thinking: bool = DEFAULT_THINKING,
        effort: str = DEFAULT_EFFORT,
    ):
        """
        初始化适配器

        Args:
            api_key: API 密钥
            base_url: API 基础地址（如 https://api.deepseek.com）
            model: 模型名称（如 deepseek-chat）
            http_client: 可选的自定义 HTTP 客户端（用于跳过证书验证等）
            provider: 这条路线叫什么（只用来显示和审计；请求本身不带它）
            thinking: 要不要让模型先想一段（`/thinking`）
            effort: 想的时候花多大力气（`/effort`）
        """
        self.model = model
        # 降级那条路要拿它们当键（见 `_NO_STREAM_OPTIONS`）。
        self.base_url = base_url
        self.provider = provider
        # 这两个是**每次请求现读**的（和 `model` 一样）：`/thinking` `/effort` 改完之后
        # 下一个请求就该用新值，不需要重建适配器、也不该在重试之间变化。
        self.thinking = thinking
        self.effort = effort
        self._api_key = api_key
        self._http = http_client
        self._client: OpenAI | None = None

    # -- 路线 ------------------------------------------------------------------

    def _ensure_client(self) -> OpenAI:
        """造（或复用）SDK 客户端。**只有真要发请求时才调。**

        关掉 SDK 自带的重试（默认 2 次）：它的重试对审计日志完全不可见 —— 一次"成功"
        的调用背后可能已经失败过两轮，而日志里只有一条记录、耗时还把重试时间算了进去，
        会被误读成"模型很慢"。重试改由 Agent 负责，每一次尝试都是一条可查的事件。
        """
        if self._client is None:
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self.base_url,
                http_client=self._http,
                max_retries=0,
            )
        return self._client

    @property
    def route(self) -> str:
        """`provider/model`（没有 provider 时就只是模型名）。"""
        return f"{self.provider}/{self.model}" if self.provider else self.model

    def install(self, *, api_key: str, base_url: str, model: str,
                provider: str = "", thinking: bool | None = None,
                effort: str | None = None) -> None:
        """换成另一条路线（连同模型）。**这是"换 provider"那一半。**

        和 `switch_model` 分开：那个只改一个字段（同一条路由上换模型），而这里要换
        密钥和端点 —— 也就是**必须重造 SDK 客户端**（它们是构造参数）。旧客户端先关掉：
        它持着连接池，而一个换过路由的会话不会再用它。

        参数里 `thinking` / `effort` 允许为 None 表示"不动它们" —— 换路由通常不改这两样
        （用户想要的是同一个思考设置，只是换个地方问）。
        """
        if base_url != self.base_url or api_key != self._api_key:
            self.close()
        self.base_url = base_url
        self._api_key = api_key
        self.provider = provider
        self.model = model
        if thinking is not None:
            self.thinking = thinking
        if effort is not None:
            self.effort = effort

    def switch_model(self, model: str) -> None:
        """换一个模型名。**同一条 base_url、同一把密钥、同一个 http client。**

        换的只是每次请求 `model=` 那个字段 —— 那正是"同一条路由上换模型"在这里的全部
        含义。**跨路由换要调 `install`**（它要重造客户端）。

        `_NO_STREAM_OPTIONS` 那个降级缓存**不用清**：它的键是 `(base_url, model)`，所以
        "这个网关的这个模型不吃 stream_options"这件事天然是按模型分开记的。清掉的话，
        换回一个已知会撞 400 的模型时会白撞一次，而那一次会往界面上吐一段要作废的正文。

        **中途换是安全的**：`complete()` 每次调用时现读 `self.model`，所以换完之后发出
        去的下一个请求就用新名字，而正在返回的那一个不受影响（它已经发出去了）。
        """
        self.model = model

    def set_reasoning(self, *, thinking: bool, effort: str) -> None:
        """改思考开关与强度（`/thinking` `/effort`）。**下一次请求生效。**

        和 `switch_model` 同一条时序：正在返回的那一个请求不受影响（参数在发出去那一刻
        就定了），而下一个请求用新值。所以"跑到一半改强度"不会把一个回合劈成两半。
        """
        self.thinking = thinking
        self.effort = effort

    def close(self) -> None:
        """收掉 SDK 客户端（换路由时、以及会话收摊时）。

        **失败不当失败**：收一个已经死掉的连接池不该盖住"换路由"这个动作本身的结果。
        """
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001
            _warn_close(exc)


    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaSink | None = None,
        on_attempt_started: Callable[[], None] | None = None,
    ) -> ModelResponse:
        """调用 OpenAI 兼容 API。

        provider 的异常在这里就被翻译成 ModelTransientError / ModelFatalError，
        不让 openai 的异常类型漏到上层。

        `on_delta` 给了就走流式（`stream=True`），不给就是今天那条一次返回的老路
        —— **两条路返回的是同一个 ModelResponse**，所以重试、审计、会话一致性
        都不用知道刚才走的是哪条。
        """
        if on_delta is None:
            return self._complete_once(messages, tools)
        return self._complete_streaming(messages, tools, on_delta, on_attempt_started)

    # -- 非流式：原来那条路，一个字没改 ----------------------------------------

    def _complete_once(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> ModelResponse:
        try:
            response = self._ensure_client().chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or [],
                **request_fields(thinking=self.thinking, effort=self.effort),
            )
        except Exception as exc:
            # from exc 保住原始 traceback，排查时还看得到 SDK 那层到底报了什么
            raise _to_domain_error(exc) from exc

        message = response.choices[0].message

        return ModelResponse(
            content=message.content,
            tool_calls=[
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                }
                for call in (message.tool_calls or [])
            ],
            usage=_extract_usage(response),
            reasoning=_extract_reasoning(message),
            raw=response,
        )

    # -- 流式 ------------------------------------------------------------------

    def _complete_streaming(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_delta: DeltaSink,
        on_attempt_started: Callable[[], None] | None = None,
    ) -> ModelResponse:
        """开一条流，边收边报，最后返回和 `_complete_once` 同形的响应。

        ## 为什么"先探一下能不能带 stream_options"，而不是直接带上

        流式响应默认**不带 usage**，而每轮末尾那句统计（累计 token、缓存命中率、
        上下文占比）全靠它。要拿到它只能显式传 `stream_options={"include_usage":
        true}` —— DeepSeek 官方端点支持，但别的兼容网关可能直接回 400。

        所以：带上试，被 400 明确拒绝（报文里提到 stream_options）就**丢掉这个
        参数重发一次**，并且记住这个 (base_url, model) 不再带（`_NO_STREAM_OPTIONS`）
        —— 不然每一轮都要白撞一次。

        **重发就是"再来一次尝试"，所以要走 `on_attempt_started`**：第一段流在撞上
        400 之前可能已经吐了几句，而重发那一遍会把整段重说一遍 —— 界面不丢掉
        前面那半截的话，看到的是两段回答首尾相接。丢掉的决定在调用方（它才知道
        "丢"意味着往哪儿发什么），这里只负责如实报一句"我又开始了一次"。

        ## 六条拼装规则在 `_StreamAccumulator` 里

        这一层只管三件事：带不带 `stream_options`、把块喂进去、把报出来的块和最终
        返回的正文保持成同一份。拼装规则（按 index 拼工具调用那些）有它们自己的
        测试，见 `tests/test_streaming.py`。
        """
        accumulator = _StreamAccumulator()
        include_usage = _stream_options_supported(self.base_url, self.model)

        while True:
            if on_attempt_started is not None:
                on_attempt_started()
            try:
                self._pump(messages, tools, on_delta, accumulator, include_usage)
                break
            except Exception as exc:
                if include_usage and _is_stream_options_rejection(exc):
                    _NO_STREAM_OPTIONS.add((self.base_url, self.model))
                    _warn_stream_options(self.base_url, self.model, exc)
                    include_usage = False
                    # 攒着的东西不丢：正文是真的，只是缺一份账。重发那一遍会把
                    # 它接着说下去（`on_attempt_started` 已经让界面把旧的擦掉了）。
                    continue
                raise _to_domain_error(exc) from exc

        return accumulator.as_response()

    def _pump(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_delta: DeltaSink,
        accumulator: _StreamAccumulator,
        include_usage: bool,
    ) -> None:
        """开流、逐块吃、逐块报。**异常一律留给调用方翻译。**

        正文和思考链各报各的：`on_delta(text=…)` / `on_delta(reasoning=…)`。
        provider 可能先吐几百块思考链再吐正文，两条都由回调原样送到界面 ——
        "要不要显示思考链"是界面的事，不是这一层的（TUI 默认折叠着）。
        """
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools or [],
            "stream": True,
            # 思考开关与强度：**和 `model` 一样是每次调用现读的**，所以 `/thinking`
            # `/effort` 改完之后下一个请求就用新值，而正在返回的那一个不受影响。
            **request_fields(thinking=self.thinking, effort=self.effort),
        }
        if include_usage:
            request["stream_options"] = dict(STREAM_USAGE_OPTION)

        stream = self._ensure_client().chat.completions.create(**request)
        for chunk in stream:
            accumulator.feed(chunk)
            choices = getattr(chunk, "choices", None) or []
            delta = getattr(choices[0], "delta", None) if choices else None
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            reasoning = (getattr(delta, "reasoning_content", None)
                         or getattr(delta, "reasoning", None))
            if not text and not reasoning:
                continue
            # **按关键字传**：两个参数都是字符串，按位置传一次就会把思考链和
            # 正文对调，而那个错误看起来像"答案里混进了一段自言自语"。
            on_delta(text=text or "", reasoning=reasoning or "")


def _warn_stream_options(base_url: str, model: str, exc: Exception) -> None:
    """降级时说一句。**必须大声说** —— 不说的话，症状是"这个会话的 token 统计
    突然全是空的"，而没有人会把它和一次 400 联系起来。
    """
    print(
        f"[warn] {base_url} 的 {model} 不接受 stream_options（{exc}）—— "
        f"流式继续，但这个会话不会再有 token 用量和缓存命中（本轮末尾那句统计"
        f"会少掉输入/命中/上下文占比）。",
        file=sys.stderr,
    )


def _warn_close(exc: Exception) -> None:
    """收掉客户端失败时说一句（`install` / `close` 那条路）。

    它发生在**换路由成功之后**或者收摊的时候，所以不能抛出去盖住那件事本身的结果 ——
    但也不能不说：一个没关掉的连接池会让 keep-alive 的 socket 活过这个进程，
    而那种症状（句柄泄漏）在 Windows 上表现为"某个文件/端口被占用"，与这里毫无关系。
    """
    print(f"[warn] 关闭模型客户端时出错（已忽略）：{type(exc).__name__}: {exc}",
          file=sys.stderr)
