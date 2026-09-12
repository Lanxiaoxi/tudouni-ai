"""思维链（思考模式）：取出来显示，但**不回传**。

两件事分开钉：

  1. 适配层把 `reasoning_content` 取出来了 —— 它和 `content` 同级返回，是本项目
     以前完全没读过的字段（所以那部分输出 token 花了钱却哪儿都看不见）；
  2. 它只在 `--debug` 下显示，而且**整段不截断** —— 别的地方根本看不到它。

**没有**回传是当前的选择，不是遗漏：官方文档说携带 tools 的请求必须完整回传
`reasoning_content`，否则 400；本项目每轮都带 tools。这一条记在
`models/openai_compatible.py` 的 `_extract_reasoning` 里（连同它的两个代价）。
"""

from types import SimpleNamespace

from agent_runtime.agents import Agent
from agent_runtime.models.openai_compatible import _extract_reasoning
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import ScriptedModel, usage


def message_with(**fields) -> SimpleNamespace:
    """假 provider 的 message 对象：只带我们要的那几个属性。"""
    return SimpleNamespace(**fields)


# --- 适配层：取出来 -------------------------------------------------------

def test_reasoning_is_read_from_the_provider_message():
    assert _extract_reasoning(message_with(reasoning_content="先看文件再改")) == "先看文件再改"


def test_a_gateway_without_the_field_is_not_an_error():
    """不同网关对 message 的填充差别很大 —— 少了这个字段就当没有，不是故障。"""
    assert _extract_reasoning(message_with(content="答案")) is None
    assert _extract_reasoning(message_with(reasoning_content=None)) is None
    assert _extract_reasoning(message_with(reasoning_content="")) is None       # 空串占位
    assert _extract_reasoning(message_with(reasoning_content="   ")) is None
    assert _extract_reasoning(message_with(reasoning_content=123)) is None      # 不是字符串


# --- Agent：只在 debug 下显示，而且不截断 ---------------------------------

def build(agent_debug: bool):
    registry = ToolRegistry()
    registry.register(Tool(name="list_files", description="列目录", risk=RiskLevel.LOW,
                           args_model=ListFilesArgs, handler=lambda **kwargs: "<已执行>"))
    model = ScriptedModel([
        ModelResponse(content=None, reasoning="我" * 500,
                      tool_calls=[{"id": "c1", "name": "list_files", "arguments": "{}"}],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, registry, PermissionPolicy({RiskLevel.LOW}), debug=agent_debug)
    return agent.run(Session.new("s"), "列目录")


def test_reasoning_is_shown_under_debug_and_not_truncated(capsys):
    """整段打而不是预览：答案在 stdout 上有全文，思维链别处根本看不到。"""
    build(agent_debug=True)

    err = capsys.readouterr().err
    assert "thinking（500 字符）" in err
    assert "我" * 500 in err                     # 一个字都没被截掉


def test_reasoning_is_silent_without_debug(capsys):
    answer = build(agent_debug=False)

    captured = capsys.readouterr()
    assert "thinking" not in captured.err
    assert "thinking" not in captured.out
    assert answer == "完成"                      # 答案照旧是 run() 的返回值（print 归 cli）


def test_a_response_without_reasoning_says_nothing(capsys):
    """网关不给这个字段时，debug 里不该多出一行空的"thinking（）"。"""
    registry = ToolRegistry()
    registry.register(Tool(name="list_files", description="列目录", risk=RiskLevel.LOW,
                           args_model=ListFilesArgs, handler=lambda **kwargs: None))
    model = ScriptedModel([ModelResponse(content="直接回答", usage=usage())])
    Agent(model, registry, PermissionPolicy({RiskLevel.LOW}), debug=True).run(
        Session.new("s"), "你好")

    assert "thinking" not in capsys.readouterr().err
