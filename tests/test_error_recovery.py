"""错误恢复：两类失败分开处理，而且每一次尝试都要留痕。

最关键的一条是**确定性失败不许重试** —— 401 重试三次只是把同一个失败重复三遍，
白花时间和钱。它也是"一律重试"那种偷懒写法最容易犯的错。
"""

import httpx
import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.openai_compatible import _to_domain_error
from agent_runtime.models.types import (
    ModelFatalError,
    ModelResponse,
    ModelTransientError,
)
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.tool import RiskLevel

import agent_runtime.agents.retry as retry_module
from fakes import Collector, ExplodingModel, ScriptedModel, usage


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """把退避等待压成 0 —— 测试不该为了验重试真的睡 1.5 秒。"""
    monkeypatch.setattr(retry_module, "BACKOFF_BASE", 0.0)


def run_with(model, registry, collector=None):
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
                  asker=lambda t, a: False, on_event=collector)
    return agent.run(Session.new("s"), "你好")


# --- 两类失败的处置 ------------------------------------------------------

def test_fatal_error_is_not_retried(registry):
    collector = Collector()
    model = ExplodingModel(ModelFatalError("401 鉴权失败"))

    with pytest.raises(ModelFatalError):
        run_with(model, registry, collector)

    assert model.n == 1, f"确定性失败只该试 1 次，实际 {model.n} 次"
    calls = collector.of("model_call")
    assert len(calls) == 1 and calls[0]["status"] == "fatal"
    assert collector.of("run_finished")[0]["stop_reason"] == "model_fatal"


def test_transient_error_is_retried_then_gives_up(registry):
    collector = Collector()
    model = ExplodingModel(ModelTransientError("连接中断"))

    with pytest.raises(ModelTransientError):
        run_with(model, registry, collector)

    assert model.n == retry_module.MAX_ATTEMPTS
    attempts = [e["attempt"] for e in collector.of("model_call")]
    assert attempts == [1, 2, 3]
    assert all(e["status"] == "error" for e in collector.of("model_call"))
    assert collector.of("run_finished")[0]["stop_reason"] == "model_error"


def test_transient_error_recovers(registry):
    """先抖动后恢复 —— 任务该照常完成，而且日志里能看出试了两次。"""
    collector = Collector()
    model = ExplodingModel(ModelTransientError("抖一下"), fail_times=1)

    assert run_with(model, registry, collector) == "恢复了"
    calls = collector.of("model_call")
    assert [e["attempt"] for e in calls] == [1, 2]
    assert calls[1]["status"] == "ok"
    assert collector.of("run_finished")[0]["stop_reason"] == "answered"


def test_model_failure_leaves_a_consistent_session(registry):
    """模型失败的时机总在循环顶部 —— 此时上一步的结果都已 append 完，状态是一致的。

    所以失败之后可以直接 --session 继续，不必重建会话。
    """
    model = ExplodingModel(ModelFatalError("401"))
    session = Session.new("s")
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}), asker=lambda t, a: False)
    with pytest.raises(ModelFatalError):
        agent.run(session, "你好")

    roles = [m["role"] for m in session.messages]
    assert roles == ["system", "user"]      # 一致：没有悬空的 tool_calls


# --- provider 异常 → 领域异常的映射 --------------------------------------

def _request():
    return httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def test_sdk_errors_are_translated_to_domain_errors():
    """适配层负责翻译，上层永远不该认识 SDK 的异常类型。"""
    import openai

    req = _request()
    assert isinstance(openai.APIConnectionError(request=req), Exception)

    assert isinstance(_to_domain_error(openai.APIConnectionError(request=req)),
                      ModelTransientError)
    assert isinstance(
        _to_domain_error(openai.APITimeoutError(request=req)), ModelTransientError)

    resp401 = httpx.Response(401, request=req)
    assert isinstance(
        _to_domain_error(openai.AuthenticationError("unauthorized", response=resp401, body=None)),
        ModelFatalError)

    resp400 = httpx.Response(400, request=req)
    assert isinstance(
        _to_domain_error(openai.BadRequestError("bad model", response=resp400, body=None)),
        ModelFatalError)

    resp429 = httpx.Response(429, request=req)
    assert isinstance(
        _to_domain_error(openai.RateLimitError("slow down", response=resp429, body=None)),
        ModelTransientError)

    resp503 = httpx.Response(503, request=req)
    assert isinstance(
        _to_domain_error(openai.InternalServerError("oops", response=resp503, body=None)),
        ModelTransientError)


def test_unknown_exception_is_fatal_not_retried():
    """认不出的异常归 Fatal —— 未知问题的重试是赌博，失败要快。"""
    assert isinstance(_to_domain_error(TypeError("我们自己的 bug")), ModelFatalError)


# --- 事件字段 -----------------------------------------------------------

def test_model_call_event_carries_status_and_attempt(registry):
    collector = Collector()
    run_with(ScriptedModel([ModelResponse(content="好", usage=usage(100, 80, 5))]),
             registry, collector)
    call = collector.of("model_call")[0]
    assert call["status"] == "ok"
    assert call["attempt"] == 1
    assert call["prompt_tokens"] == 100
    assert call["cached_tokens"] == 80
    assert call["miss_tokens"] == 20
    assert call["completion_tokens"] == 5
    assert "duration_ms" in call


def test_audit_arguments_are_truncated(registry):
    """审计日志只记参数预览 —— write_file 的 content 可能很长、也可能含敏感内容。"""
    big = "S" * 5000
    from fakes import tool_call
    collector = Collector()
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("list_files", {"path": big})], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    run_with(model, registry, collector)

    arguments = collector.of("permission")[0]["arguments"]
    assert len(arguments) < 300
    assert "共" in arguments and "字符" in arguments      # 标注了真实长度
