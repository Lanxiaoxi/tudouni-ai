"""思维链（思考模式）：取出来、**整段写进审计**，但**不回传**。

三件事分开钉：

  1. 适配层把 `reasoning_content` 取出来了 —— 它和 `content` 同级返回，是本项目
     以前完全没读过的字段（所以那部分输出 token 花了钱却哪儿都看不见）；
  2. 它**整段进审计**（决策 5）：`model_call` 事件上多一个 `reasoning` 键，不截断。
     它是审计里第一个"内容型"字段，体积和敏感性两笔代价写在 doc/TUI-design.md 的 D2；
  3. 它**不在 `--debug` 里打了**（第零期那版的输出）。同一段正文有两条出口正是
     README 第 3 条设计原则反对的事 —— 想读思维链就去读审计。

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


# --- Agent：整段进审计，不截断 --------------------------------------------

def build(*, with_reasoning: bool = True):
    """跑一个两回合的会话，返回 (答案, 收到的审计事件)。

    事件靠一个列表 sink 收 —— 这条测试要问的是"事件里有没有它、是不是整段",
    而落盘、格式那些是 audit/ 的事。
    """
    events: list[dict] = []
    registry = ToolRegistry()
    registry.register(Tool(name="list_files", description="列目录", risk=RiskLevel.LOW,
                           args_model=ListFilesArgs, handler=lambda **kwargs: "<已执行>"))
    model = ScriptedModel([
        ModelResponse(
            content=None,
            reasoning=("我" * 500) if with_reasoning else None,
            tool_calls=[{"id": "c1", "name": "list_files", "arguments": "{}"}],
            usage=usage(),
        ),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(
        model, registry, PermissionPolicy({RiskLevel.LOW}),
        on_event=events.append,
    )
    return agent.run(Session.new("s"), "列目录"), events


def _model_calls(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "model_call"]


def test_reasoning_goes_into_the_audit_whole_and_untruncated():
    """整段进审计而不是预览：别处看不到它，截断等于不给看。

    "不截断"这条要专门断言 —— 它和 `arguments` / 工具结果那两处的取舍**相反**
    （那两处只记预览），所以很容易被后来的人按"审计只记预览"的惯例顺手改掉。
    """
    _, events = build()

    calls = _model_calls(events)
    assert calls, "至少要有一条 model_call"
    assert calls[0]["reasoning"] == "我" * 500      # 一字不差
    assert len(calls[0]["reasoning"]) == 500


def test_reasoning_is_absent_from_the_debug_output(capsys):
    """`--debug` 不再打它 —— 那是同一份事实的第二个出口。

    这一条同时是"改道完成"的证据：如果哪天有人把 debug 那一段加回来（它以前确实
    在那儿），这条测试会红，而上面那条仍然绿 —— 两个出口并存正是要避免的状态。
    """
    build()

    captured = capsys.readouterr()
    assert "thinking" not in captured.err
    assert "我" * 500 not in captured.err


def test_a_response_without_reasoning_adds_no_key():
    """网关不给这个字段时，事件里**不该多出一个恒为 null 的键**。

    恒为 null 的键会让后面做统计的人处处判空，而这一条本来就是可选的。
    """
    _, events = build(with_reasoning=False)

    for call in _model_calls(events):
        assert "reasoning" not in call
