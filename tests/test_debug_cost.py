"""debug 关着的时候，不该替 debug 付钱。

`self._debug(f"… {self._preview(结果)}")` 看着人畜无害，但 f-string 的实参**在进
`_debug` 之前就求值了**，而 `_preview` 不是 O(1)：它先把换行压平再截断，也就是把整段
文本扫一遍。工具结果动辄几十万字符（`read_file` 不分页），于是**默认不开 debug 的每次
工具调用都在白扫** —— 实测一个 8MB 的结果约 10ms，一批五个就是 50ms。

这里数的是"扫了几次、扫了多长"，不是耗时 —— 时间断言在别的机器上只会随机红。
"""

from agent_runtime.agents import Agent
from agent_runtime.agents.agent import DEBUG_PREVIEW_LIMIT
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import ScriptedModel, tool_call, usage

# 比任何预览上限都大得多：只要它经过 _preview，就一定是一次全文扫描。
BIG = "x" * 200_000


class SpyAgent(Agent):
    """记下每一次 _preview 收到的文本有多长。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.previewed: list[int] = []

    def _preview(self, text, limit=DEBUG_PREVIEW_LIMIT):
        self.previewed.append(len(text))
        return Agent._preview(text, limit)


def build(agent_debug: bool) -> SpyAgent:
    registry = ToolRegistry()
    registry.register(Tool(
        name="list_files", description="列目录", risk=RiskLevel.LOW,
        args_model=ListFilesArgs, handler=lambda **kwargs: BIG,
    ))
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {}, "c1")], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = SpyAgent(model, registry, PermissionPolicy({RiskLevel.LOW}), debug=agent_debug)
    agent.run(Session.new("s"), "列目录")
    return agent


def test_a_huge_tool_result_is_never_scanned_when_debug_is_off():
    """这一条就是那笔白工：整个工具结果被扫一遍，只为了拼一句永远不会被打印的话。"""
    agent = build(agent_debug=False)

    assert len(BIG) not in agent.previewed


def test_the_same_result_is_scanned_exactly_once_when_debug_is_on():
    """开着 debug 就该打 —— 而且只打一次（多扫几遍同样是白工）。"""
    agent = build(agent_debug=True)

    assert agent.previewed.count(len(BIG)) == 1


def test_lazy_debug_still_prints_what_it_always_printed(capsys):
    """惰性化不能顺手把输出弄丢 —— 这条是上面那条的反面保险。"""
    build(agent_debug=True)

    err = capsys.readouterr().err
    assert "→ 工具调用 list_files" in err
    assert "← 工具结果 (200000 字符)" in err
    assert "content: 完成" in err


def test_nothing_is_printed_without_debug(capsys):
    build(agent_debug=False)

    assert "[debug]" not in capsys.readouterr().err
