"""流式输出的验收。

分三层，因为它们的失败形态完全不同：

  1. **适配层拼装**（`_StreamAccumulator`）—— 纯函数，不碰网络。这一层最容易写错
     而且错了最难查：按错误的方式拼 `tool_calls` 的症状是"模型给的工具参数不合法"，
     看起来像模型的问题；
  2. **适配层的选择**（`complete(on_delta=…)` 走不走 `stream=True`）—— 打桩在
     `client.chat.completions.create` 上，断言发出去的请求长什么样；
  3. **端到端**（真子进程 + 假网关，`t:"delta"` 真的从 stdout 出来）——
     在 `test_protocol.py` 里，那边已经有起子进程和假网关了。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from agent_runtime.models.openai_compatible import (
    _NO_STREAM_OPTIONS,
    _StreamAccumulator,
    _is_stream_options_rejection,
)
from agent_runtime.models.types import ModelResponse

from fakes import Collector, recording_registry, tool_call, usage


# --- 造块 ---------------------------------------------------------------------

def _usage(prompt: int = 0, completion: int = 0, cached: int | None = None):
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_tokens_details=details,
    )


def _delta_chunk(*, content=None, reasoning=None, tool_calls=None, finish=None,
                 usage=None) -> Any:
    """一块 SSE。**usage 那一块的 choices 是空数组** —— 真实网关就是这样，
    所以这里也照着造，喂给同一个 feed。"""
    if usage is not None:
        return SimpleNamespace(usage=usage, choices=[])
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        delta.reasoning_content = reasoning
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(usage=None, choices=[choice])


def _tool(index: int, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


@pytest.fixture(autouse=True)
def _clean_downgrade_memory():
    """`_NO_STREAM_OPTIONS` 是模块级的（一次 400 换来的知识要留一个进程），
    测试之间必须清干净 —— 不清的话，一条测试的降级会让后面的测试少一个参数，
    而失败的那条看起来完全无关。"""
    saved = set(_NO_STREAM_OPTIONS)
    _NO_STREAM_OPTIONS.clear()
    try:
        yield
    finally:
        _NO_STREAM_OPTIONS.clear()
        _NO_STREAM_OPTIONS.update(saved)


# --- 1. 拼装 ------------------------------------------------------------------

def test_content_chunks_are_joined_in_order():
    acc = _StreamAccumulator()
    for piece in ("你", "好", "，世界"):
        acc.feed(_delta_chunk(content=piece))

    response = acc.as_response()
    assert response.content == "你好，世界"
    assert response.reasoning is None
    assert response.tool_calls == []


def test_reasoning_and_content_are_kept_apart():
    """思考链**不是**正文的一部分。混在一起的话，界面上会把一段自言自语当成回答，
    而审计里的 `reasoning` 也会被算进完成 token。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(reasoning="先看目录"))
    acc.feed(_delta_chunk(reasoning="，再读文件"))
    acc.feed(_delta_chunk(content="读完了。"))

    response = acc.as_response()
    assert response.reasoning == "先看目录，再读文件"
    assert response.content == "读完了。"


def test_tool_calls_are_joined_by_index_not_by_arrival_order():
    """**按 index 拼，不按到达顺序。** 同一批里两个调用在流里是交错的：
    先 index=0 的参数段、再 index=1 的整条、最后 index=0 的尾巴。
    按到达顺序拼会把两个调用的参数粘成一段 —— 那段 JSON 解不出来，
    而症状是"模型这一步给的工具参数不合法"。
    """
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(tool_calls=[_tool(0, call_id="call_a", name="read_file",
                                            arguments='{"pa')]))
    acc.feed(_delta_chunk(tool_calls=[_tool(1, call_id="call_b", name="list_files",
                                            arguments="{}")]))
    acc.feed(_delta_chunk(tool_calls=[_tool(0, arguments='th": "a.py"}')],
                          finish="tool_calls"))

    calls = acc.as_response().tool_calls
    assert [c["name"] for c in calls] == ["read_file", "list_files"]
    assert calls[0]["arguments"] == '{"path": "a.py"}'
    assert calls[0]["id"] == "call_a"
    assert calls[1]["id"] == "call_b"


def test_a_tool_call_without_index_is_accumulated_as_one():
    """`index` 可能整个缺席（非官方网关）。那时候整段流只有一条调用 —— 按 0 攒，
    而不是按"第几个到达的块"攒（后者会把一条调用拆成好几条）。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(tool_calls=[_tool(0, call_id="c1", name="read_file")]))
    acc.feed(_delta_chunk(tool_calls=[_tool(0, arguments='{"path"')]))
    acc.feed(_delta_chunk(tool_calls=[_tool(0, arguments=': "a.py"}')]))

    calls = acc.as_response().tool_calls
    assert len(calls) == 1
    assert calls[0]["arguments"] == '{"path": "a.py"}'


def test_a_repeated_name_is_not_appended_twice():
    """有的网关每块都重发 `name`。追加的话工具名会变成 `read_fileread_file`。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(tool_calls=[_tool(0, name="read_file", arguments='{"a"')]))
    acc.feed(_delta_chunk(tool_calls=[_tool(0, name="read_file", arguments=": 1}")]))

    assert acc.as_response().tool_calls[0]["name"] == "read_file"


def test_usage_is_replaced_not_accumulated():
    """usage 那一块说的是**整次请求**。累加会把一个 1500 token 的回答报成几百万。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(content="x"))
    acc.feed(_delta_chunk(usage=_usage(prompt=100, completion=7, cached=64)))

    usage = acc.as_response().usage
    assert (usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens) == (100, 64, 7)
    assert usage.miss_tokens == 36


def test_no_usage_block_means_no_usage():
    """网关不填 usage 时是 **None，不是 0** —— 0 会被读成"这次没花钱"。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(content="x"))
    assert acc.as_response().usage is None


def test_a_tool_only_turn_has_no_content():
    """纯工具调用那一轮的 content 是 **None，不是空串** —— 和 provider 的非流式
    响应一致，而且 `Session` 里存 null 与存 "" 会在历史里长得不一样。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(tool_calls=[_tool(0, call_id="c1", name="read_file",
                                            arguments="{}")], finish="tool_calls"))

    response = acc.as_response()
    assert response.content is None
    assert response.reasoning is None
    assert len(response.tool_calls) == 1


def test_an_empty_placeholder_slot_is_dropped():
    """有的网关先发一个只带 id 的占位块。留着它会变成一条"工具名不认识"的错误结果。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(tool_calls=[_tool(0, call_id="c1")]))
    acc.feed(_delta_chunk(tool_calls=[_tool(1, call_id="c2", name="read_file",
                                            arguments="{}")]))

    calls = acc.as_response().tool_calls
    assert [c["name"] for c in calls] == ["read_file"]


def test_stream_chunks_counts_both_kinds():
    """块数是"这个网关到底有没有在流"唯一的证据（有的会先缓冲整段再吐）。"""
    acc = _StreamAccumulator()
    acc.feed(_delta_chunk(reasoning="想"))
    acc.feed(_delta_chunk(content="答"))
    acc.feed(_delta_chunk(usage=_usage(prompt=1, completion=1)))

    response = acc.as_response()
    assert response.stream_chunks == 2
    assert response.streamed is True


# --- 2. 适配层的选择 ----------------------------------------------------------

class _FakeStream:
    """一个可迭代的假流，并记下它被读完了没有。"""

    def __init__(self, chunks: list[Any]):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            yield chunk
        self.closed = True


class _FakeCompletions:
    """替掉 `client.chat.completions`。`calls` 就是"发出去的请求长什么样"的证据。

    `rejects` 是一个可选的报文片段：请求里出现 `stream_options` 就回一个带这句话的
    400（用来验降级那条路），否则把一个假流给出去。
    """

    def __init__(self, chunks: list[Any] | None = None,
                 rejects: str | None = None):
        self.chunks = chunks or []
        self.rejects = rejects
        self.calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []

    def create(self, **request: Any):
        self.calls.append(request)
        if self.rejects is not None and "stream_options" in request:
            import httpx
            from openai import APIStatusError

            http_request = httpx.Request("POST", "http://fake.local/v1/chat/completions")
            raise APIStatusError(
                self.rejects,
                response=httpx.Response(400, request=http_request),
                body={"error": {"message": self.rejects}},
            )
        stream = _FakeStream(self.chunks)
        self.streams.append(stream)
        return stream


def _model_with(completions: _FakeCompletions):
    """一个真的 `OpenAICompatibleModel`，只把 completions 换成假的。

    不 monkeypatch 整个 client：`__init__` 里那句 `max_retries=0` 和 base_url
    都是被测行为的一部分（降级记忆的键就是 base_url）。
    """
    from agent_runtime.models.openai_compatible import OpenAICompatibleModel

    model = OpenAICompatibleModel(
        api_key="sk-test", base_url="http://fake.local/v1", model="fake-model",
    )
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        close=lambda: None,
    )
    return model


def test_without_a_sink_the_request_is_not_streamed():
    """不给 sink 就走原来那条路：**请求里不许出现 stream** ——
    老路径的行为要逐字节不变。"""
    completions = _FakeCompletions()
    completions.create = lambda **request: SimpleNamespace(  # type: ignore[method-assign]
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="答", tool_calls=None, reasoning_content=None))],
        usage=_usage(prompt=1, completion=1),
    )
    model = _model_with(completions)

    response = model.complete([{"role": "user", "content": "hi"}], [])

    assert response.content == "答"
    assert response.streamed is False
    assert completions.calls == []          # 走的是被换掉的那个 create


def test_with_a_sink_the_request_asks_for_a_stream_and_usage():
    completions = _FakeCompletions([
        _delta_chunk(content="答"),
        _delta_chunk(usage=_usage(prompt=10, completion=1, cached=4)),
    ])
    model = _model_with(completions)
    seen: list[tuple[str, str]] = []

    response = model.complete(
        [{"role": "user", "content": "hi"}], [],
        on_delta=lambda text="", reasoning="": seen.append((text, reasoning)),
    )

    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}
    assert seen == [("答", "")]
    assert response.content == "答"
    assert response.streamed is True
    assert response.usage.prompt_tokens == 10


def test_reasoning_deltas_go_to_the_reasoning_argument():
    """两个参数都是字符串，串了位置看起来像"答案里混进一段自言自语"。"""
    completions = _FakeCompletions([
        _delta_chunk(reasoning="想"),
        _delta_chunk(content="答"),
    ])
    model = _model_with(completions)
    seen: list[tuple[str, str]] = []

    model.complete([], [], on_delta=lambda text="", reasoning="": seen.append((text, reasoning)))

    assert seen == [("", "想"), ("答", "")]


def test_a_rejected_stream_options_is_dropped_and_remembered():
    """网关回 400 说它不认 stream_options：**丢掉参数重发一次**，
    并且记住这个 (base_url, model) 不再带 —— 不然每一轮都要白撞一次 400。"""
    completions = _FakeCompletions(
        [_delta_chunk(content="答")],
        rejects="unknown parameter: stream_options",
    )
    model = _model_with(completions)

    response = model.complete([], [], on_delta=lambda text="", reasoning="": None)

    assert response.content == "答"
    assert "stream_options" in completions.calls[0]
    assert "stream_options" not in completions.calls[1]
    assert ("http://fake.local/v1", "fake-model") in _NO_STREAM_OPTIONS
    # 之后再流式就不再带它了（一次 400 换来的知识留住了）
    completions.calls.clear()
    model.complete([], [], on_delta=lambda text="", reasoning="": None)
    assert "stream_options" not in completions.calls[0]


def test_text_already_streamed_survives_the_downgrade():
    """降级重发时**已经吐出去的正文不能丢** —— 它是保真的（provider 只是不接受
    那个参数），丢掉等于让模型白说一遍、用户白等一次。

    顺带钉住一件事：那一段正文**只报一次**（重发的那条流里没有它）。
    """
    import httpx
    from openai import APIStatusError

    completions = _FakeCompletions([_delta_chunk(content="后半段")])
    state = {"n": 0}

    def create(**request):
        state["n"] += 1
        completions.calls.append(request)
        if state["n"] == 1:
            def broken():
                yield _delta_chunk(content="前半段")
                http_request = httpx.Request("POST", "http://x/")
                raise APIStatusError(
                    "unknown parameter: stream_options",
                    response=httpx.Response(400, request=http_request),
                    body={"error": {"message": "unknown parameter: stream_options"}},
                )

            return broken()
        return _FakeStream(completions.chunks)

    completions.create = create  # type: ignore[method-assign]
    model = _model_with(completions)
    seen: list[str] = []

    response = model.complete(
        [], [], on_delta=lambda text="", reasoning="": seen.append(text))

    # 两段流各自的块各报一次：第一段报"前半段"，重发那段报"后半段"。
    # **界面上看到的最终正文只有第二段** —— `on_attempt_started` 就是让界面
    # 在重发之前把第一段丢掉的那个信号。
    assert seen == ["前半段", "后半段"]
    assert response.content == "前半段后半段"


def test_a_second_attempt_is_announced_before_it_starts():
    """`on_attempt_started` 在**每次尝试开始之前**各调一次（含第一次）。
    界面靠它把上一次吐出去的半截正文丢掉 —— 少了这个信号，两段回答会首尾相接。"""
    completions = _FakeCompletions(
        [_delta_chunk(content="答")],
        rejects="unknown parameter: stream_options",
    )
    model = _model_with(completions)
    attempts: list[int] = []

    model.complete(
        [], [],
        on_delta=lambda text="", reasoning="": None,
        on_attempt_started=lambda: attempts.append(len(attempts) + 1),
    )

    assert attempts == [1, 2]


def test_only_a_stream_options_400_is_treated_as_a_rejection():
    """别的 400（模型名错、消息格式错）不许被认下来 —— 认了就会把一次
    "重发也没用"的失败变成一次多余的重发，而且真正的失败原因会被那句
    "网关不接受 stream_options" 盖住。"""
    from openai import APIStatusError

    import httpx

    request = httpx.Request("POST", "http://fake.local/v1/chat/completions")
    bad_model = APIStatusError(
        "model not found", response=httpx.Response(400, request=request),
        body={"error": {"message": "model not found"}},
    )
    assert _is_stream_options_rejection(bad_model) is False

    not_found = APIStatusError(
        "not found", response=httpx.Response(404, request=request), body=None)
    assert _is_stream_options_rejection(not_found) is False


def test_a_transient_failure_mid_stream_is_translated():
    """流中途断线要变成可重试的领域异常 —— 那是"重试前先发 delta_reset"的前提。"""
    import httpx
    from openai import APIConnectionError

    from agent_runtime.models.types import ModelTransientError

    completions = _FakeCompletions()
    completions.create = lambda **request: (_ for _ in ()).throw(  # type: ignore[method-assign]
        APIConnectionError(request=httpx.Request("POST", "http://x/")))
    model = _model_with(completions)

    with pytest.raises(ModelTransientError):
        model.complete([], [], on_delta=lambda text="", reasoning="": None)


def test_a_base_exception_from_the_sink_passes_through():
    """**这是"在 on_delta 里打断这一轮"能成立的全部依据。**
    `RunCancelled` 继承 BaseException，适配层的 `except Exception` 抓不到它；
    要是有人把它改成裸 `except BaseException`，取消就会变成 model_fatal。"""
    class Cancelled(BaseException):
        pass

    completions = _FakeCompletions([_delta_chunk(content="答")])
    model = _model_with(completions)

    def sink(text: str = "", reasoning: str = "") -> None:
        raise Cancelled

    with pytest.raises(Cancelled):
        model.complete([], [], on_delta=sink)


# --- 3. Agent 那一层 ----------------------------------------------------------
#
# 这一组测的是"流式接上之后 Agent 的契约有没有变"：正文照旧进历史、审计里只有
# 汇总、取消能在流中间发生、重试之前那一声 reset 真的发出去了。

class StreamingModel:
    """一个会出流的假模型：把 `content` 拆成几块喂给 `on_delta`。

    **它按真实契约实现**（`complete(messages, tools, on_delta, on_attempt_started)`），
    所以 `agents/retry.py` 会把流式那两个参数传进来 —— 而"传进来了没有"本身就该被测。

    `streamed=True` 也一起标上：那是真实适配层会做的事（见
    `_StreamAccumulator.as_response`），而审计里那三个汇总字段读的正是它 ——
    假模型不标的话，测出来的是"假模型没说它流过"，不是"Agent 没记"。
    """

    def __init__(self, script: list[ModelResponse], pieces: int = 3):
        self.script = list(script)
        self.pieces = pieces
        self.seen_on_delta: list[bool] = []
        self.attempts_started: list[int] = []

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        self.seen_on_delta.append(on_delta is not None)
        if on_attempt_started is not None:
            on_attempt_started()
        response = self.script.pop(0)
        text = response.content or ""
        chunks = 0
        if on_delta is not None and text:
            size = max(1, len(text) // self.pieces)
            for index in range(0, len(text), size):
                on_delta(text=text[index:index + size])
                chunks += 1
        if chunks:
            response.streamed = True
            response.stream_chunks = chunks
        return response


def _agent(model, *, on_delta, on_event=None, should_stop=None):
    from agent_runtime.agents.agent import Agent
    from agent_runtime.security.policy import PermissionPolicy
    from agent_runtime.tools.tool import RiskLevel

    registry, _calls = recording_registry()
    return Agent(
        model, registry, PermissionPolicy({RiskLevel.LOW}),
        on_event=on_event, on_delta=on_delta, should_stop=should_stop,
    )


def test_the_streamed_text_is_exactly_what_lands_in_history():
    """逐字吐出去的东西和存进会话的正文**必须是同一份**。

    对不上的症状非常难查：屏幕上是 A、`--history` 里是 B，而"哪个对"没有第三方
    可以裁决（审计里没有正文）。所以这条钉的是"两份事实其实是同一份"。
    """
    from agent_runtime.state import Session

    session = Session.new("s")
    model = StreamingModel([ModelResponse(content="我先看一眼，然后改掉那一行。")])
    seen: list[str] = []
    agent = _agent(model, on_delta=lambda **kw: seen.append(kw.get("text") or ""))

    answer = agent.run(session, "改一下 a.py")

    assert "".join(seen) == answer == "我先看一眼，然后改掉那一行。"
    assert model.seen_on_delta == [True], "on_delta 没被传下去 —— 流式根本没开"
    # 历史里是一条完整的 assistant 消息（不是几块）。
    assistant = [m for m in session.messages if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == answer


def test_without_a_sink_the_agent_does_not_stream():
    """不注入 `on_delta` = 不流式：模型层收到的那个参数必须是 **None**。

    这条是 `--no-stream` 的底线。踩过一次：`Agent._complete_with_retry` 永远传
    一个 `_DeltaRelay` 过去（哪怕它是空的），于是模型层以为"有人要流式"、
    照样发 `stream: true` —— 而 `init.stream` 说的是 false。两个事实对不上，
    用户看到的是"关掉了但请求里还在流"。
    """
    from agent_runtime.state import Session

    session = Session.new("s")
    model = StreamingModel([ModelResponse(content="整段。")])
    agent = _agent(model, on_delta=None)

    answer = agent.run(session, "你好")

    assert answer == "整段。"
    assert model.seen_on_delta == [False], "没有 sink 时不该把 on_delta 传下去"


def test_the_audit_gets_a_summary_not_the_whole_stream():
    """审计里是**汇总**（`streamed` / `stream_chunks` / `streamed_chars`），
    而不是每一块一条记录。一次回答上千块，`JsonlSink` 每条一次 open/write/close。
    """
    from agent_runtime.state import Session

    session = Session.new("s")
    model = StreamingModel([ModelResponse(content="一二三四五六七八九十")], pieces=5)
    collector = Collector()
    agent = _agent(model, on_delta=lambda **kw: None, on_event=collector)

    agent.run(session, "写十个字")

    calls = collector.of("model_call")
    assert [c["kind"] for c in collector.events].count("delta") == 0
    assert calls[0]["streamed"] is True
    assert calls[0]["stream_chunks"] == 5
    assert calls[0]["streamed_chars"] == 10


def test_a_streamed_turn_with_no_text_has_no_stream_fields():
    """纯工具调用那一步一个字都没吐：审计里**不该**出现 `streamed`。

    一个恒为 true/false 的键会让后面做统计的人处处判空（和 `reasoning` 同一条规矩）。
    """
    from agent_runtime.state import Session

    session = Session.new("s")
    model = StreamingModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content="好了", usage=usage()),
    ])
    collector = Collector()
    agent = _agent(model, on_delta=lambda **kw: None, on_event=collector)

    agent.run(session, "看看目录")

    calls = collector.of("model_call")
    assert "streamed" not in calls[0], "这一步一个字都没吐，不该有 streamed"
    assert calls[1]["streamed"] is True


def test_cancelling_mid_stream_stops_at_once_and_keeps_the_session_sane():
    """**流式让"随时取消"第一次成立**（决策 1 的第三笔代价还掉了）。

    三条一起钉，因为它们是同一件事的三个方面：
      * 取消**立刻**生效（不是等模型说完）—— 第二块一到就停；
      * 收尾照常（`run_finished(cancelled)` + 落盘），否则界面会一直等一条永远
        不来的事件（它按那条事件熄灭转圈）；
      * **半截正文不进历史**（认下的取舍）：屏幕上那半句在会话里查不到，
        而历史里不会留下一条被砍断的 assistant 消息。
    """
    from agent_runtime.agents import RunCancelled
    from agent_runtime.state import Session

    session = Session.new("s")
    model = StreamingModel([ModelResponse(content="一二三四五六七八九十")], pieces=10)
    collector = Collector()
    seen: list[str] = []
    state = {"stop": False}

    def sink(**kwargs):
        if kwargs.get("text"):
            seen.append(kwargs["text"])
            state["stop"] = True          # 第一块之后就要求停

    agent = _agent(model, on_delta=sink, on_event=collector,
                   should_stop=lambda: state["stop"])

    with pytest.raises(RunCancelled):
        agent.run(session, "写十个字")

    # 只吐了第一块就停了 —— 没有等模型把十字说完。
    assert len(seen) == 1
    assert [e["kind"] for e in collector.events].count("run_finished") == 1
    assert collector.of("run_finished")[0]["stop_reason"] == "cancelled"
    # **历史里没有那条半截的 assistant 消息。**
    assert [m for m in session.messages if m["role"] == "assistant"] == []


def test_a_retry_announces_the_reset_before_it_starts_again():
    """重试之前那一句"刚才吐的作废了"要走两条出口：

      * 界面（`on_delta(reset=True)`）—— 不丢的话屏幕上两段回答首尾相接；
      * 审计（一条 `delta_reset` 事件）—— 它是"屏幕上出现过的东西后来被丢了"
        唯一的痕迹（审计里没有 delta 正文）。
    """
    from agent_runtime.models.types import ModelTransientError
    from agent_runtime.state import Session

    class Flaky(StreamingModel):
        """第一次吐一块然后断线，第二次正常说完。"""

        def __init__(self):
            super().__init__([ModelResponse(content="接上了。")])
            self.n = 0

        def complete(self, messages, tools=None, on_delta=None,
                     on_attempt_started=None):
            self.n += 1
            if self.n == 1:
                if on_delta is not None:
                    on_delta(text="半截…")
                raise ModelTransientError("断线了")
            return super().complete(messages, tools, on_delta, on_attempt_started)

    session = Session.new("s")
    model = Flaky()
    collector = Collector()
    got: list[tuple[str, str, bool]] = []
    agent = _agent(
        model,
        on_delta=lambda **kw: got.append(
            (kw.get("text") or "", kw.get("reasoning") or "", bool(kw.get("reset")))),
        on_event=collector,
    )

    answer = agent.run(session, "你好")

    assert answer == "接上了。"
    # 半截 -> 一条 reset -> 完整那段（逐块）。
    assert got[0] == ("半截…", "", False)
    resets = [i for i, item in enumerate(got) if item == ("", "", True)]
    assert resets, f"重试之前没有告诉界面把那半截丢掉：{got!r}"
    # reset 之后吐出来的那些块拼起来就是答案 —— 也就是"半截被换成了完整版"。
    assert "".join(text for text, _r, reset in got[resets[0] + 1:] if not reset) == answer
    # 审计里**只有一条** delta_reset（一次重试会经过两个层，别记两遍）。
    assert collector.of("delta_reset") != []
    assert collector.of("model_call")[0]["status"] == "error"
    assert collector.of("model_call")[1]["status"] == "ok"


def test_a_retry_that_streamed_nothing_leaves_no_reset_in_the_audit():
    """一次**什么都没吐**的重试不该在审计里留下"清空过正文"—— 那句话是假的。

    注意界面那一侧照发（`reset=True`）：它要清的东西本来就没有，无害；
    而漏发一条的后果是屏幕上留着一段错位的半截正文 —— 两个方向的代价不对称。
    """
    from agent_runtime.models.types import ModelTransientError
    from agent_runtime.state import Session

    class Flaky(StreamingModel):
        def __init__(self):
            super().__init__([ModelResponse(content="第二次成了。")])
            self.n = 0

        def complete(self, messages, tools=None, on_delta=None,
                     on_attempt_started=None):
            self.n += 1
            if self.n == 1:
                raise ModelTransientError("一上来就断")
            return super().complete(messages, tools, on_delta, on_attempt_started)

    session = Session.new("s")
    collector = Collector()
    agent = _agent(Flaky(), on_delta=lambda **kw: None, on_event=collector)

    assert agent.run(session, "你好") == "第二次成了。"
    assert collector.of("delta_reset") == []


def test_reasoning_deltas_are_relayed_on_their_own_channel():
    """思考链走 `reasoning=`，正文走 `text=` —— **两个参数不许串**。"""
    from agent_runtime.state import Session

    class Thinker(StreamingModel):
        def complete(self, messages, tools=None, on_delta=None,
                     on_attempt_started=None):
            if on_delta is not None:
                on_delta(reasoning="先想一下")
                on_delta(text="答案是 42")
            return ModelResponse(content="答案是 42", reasoning="先想一下",
                                 usage=usage())

    session = Session.new("s")
    got: list[tuple[str, str]] = []
    collector = Collector()
    agent = _agent(Thinker([]), on_delta=lambda **kw: got.append(
        (kw.get("text") or "", kw.get("reasoning") or "")), on_event=collector)

    answer = agent.run(session, "42?")

    assert got == [("", "先想一下"), ("答案是 42", "")]
    assert answer == "答案是 42"
    # 思考链进审计是**整段**（决策 5），不是逐块。
    assert collector.of("model_call")[0]["reasoning"] == "先想一下"

