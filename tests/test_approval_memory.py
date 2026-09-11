"""按 t 记住的免问规则：记住什么、谁记的、以及审计里能不能看出来。

这是整套权限里唯一会**改自己状态**的东西，所以三件事都要有证据：

  1. 记住的只有工具名，而且只增不减 —— 作用范围一眼看得懂；
  2. 规则生效的每一次放行，来路必须和"人这一次批准的"分得开（outcome=rule_allowed）；
  3. 落盘失败不能让这一轮工具调用变成"工具执行失败"，但也不能是静默失败。
"""

import builtins

import pytest

from agent_runtime.agents import Agent
from agent_runtime.config import PERMISSION_FILE_NAME
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import ApprovalMemory, PermissionPolicy
from agent_runtime.security.asker import cli_asker
from agent_runtime.security.gate import check_permission
from agent_runtime.security.memory import SAVE_FAILED_NOTE
from agent_runtime.state import Session
from agent_runtime.tools.builtin import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool

from fakes import Collector, ScriptedModel, recording_registry, tool_call, usage


def make_tool(risk: RiskLevel = RiskLevel.MEDIUM, name: str = "list_files") -> Tool:
    return Tool(name=name, description="列目录", risk=risk,
                args_model=ListFilesArgs, handler=lambda **kwargs: None)


def never_asks(tool, arguments):
    raise AssertionError("这条路径不该询问用户")


def answering(text: str, memory=None) -> bool:
    """把 cli_asker 当成"用户敲了 text"来用。"""
    return cli_asker(make_tool(), {}, memory=memory)


@pytest.fixture
def typed(monkeypatch):
    """让内置 input() 返回指定的一行。"""
    def _typed(text: str):
        monkeypatch.setattr(builtins, "input", lambda: text)
    return _typed


# --- 记忆本身 -----------------------------------------------------------

def test_grant_is_idempotent_and_deduplicates():
    memory = ApprovalMemory()
    assert memory.grant("shell") is True
    assert memory.grant("shell") is False       # 第二次什么都没变
    assert memory.tools() == frozenset({"shell"})


def test_tools_returns_a_snapshot_not_a_live_view():
    """gate 靠"问前问后各拍一张"来发现这次批准新增了什么 —— 拿到的必须是那一刻
    的样子，不能是个还会跟着变的视图。"""
    memory = ApprovalMemory({"shell"})
    before = memory.tools()

    memory.grant("write_file")

    assert before == frozenset({"shell"})
    assert memory.tools() == frozenset({"shell", "write_file"})


def test_persist_is_called_once_per_new_name():
    saved: list[frozenset[str]] = []
    memory = ApprovalMemory(on_change=saved.append)

    memory.grant("shell")
    memory.grant("shell")           # 没新东西，不该再写一遍文件
    memory.grant("write_file")

    assert saved == [frozenset({"shell"}), frozenset({"shell", "write_file"})]


def test_persist_failure_keeps_the_memory_and_says_so(capsys):
    """落盘失败不能往外抛：agent 的 except Exception 就在外面，抛出去会让这一轮
    本来合法的工具调用变成"工具执行失败"。但也不能是静默失败。"""
    def broken(_names):
        raise OSError("磁盘满了")

    memory = ApprovalMemory(on_change=broken)
    assert memory.grant("shell") is True

    assert "shell" in memory                       # 这次会话仍然免问
    assert SAVE_FAILED_NOTE in capsys.readouterr().err


# --- 关卡：规则的来路要和"这一次批准的"分开 ------------------------------

def test_memory_short_circuits_the_asker():
    result = check_permission(make_tool(), {}, PermissionPolicy(), never_asks,
                              ApprovalMemory({"list_files"}))

    assert result.outcome == "rule_allowed"        # 不是 approved
    assert result.denial is None
    assert result.waited_ms is None                # 没等人，就没有等待时长


def test_rule_allowed_does_not_need_an_asker():
    """规则已经给了答案，缺询问方式不影响这个答案 —— 否则会出现"配了免问规则却被拒"。"""
    result = check_permission(make_tool(), {}, PermissionPolicy(), None,
                              ApprovalMemory({"list_files"}))

    assert result.outcome == "rule_allowed"


def test_policy_denial_beats_a_remembered_rule():
    """拒绝永远优先：矛盾时 fail-closed，不因为人以前按过 t 就放行。"""
    policy = PermissionPolicy(deny_tools={"list_files"})
    result = check_permission(make_tool(), {}, policy, never_asks,
                              ApprovalMemory({"list_files"}))

    assert result.outcome == "policy_denied"


def test_a_t_answer_is_recorded_as_remembered_and_applies_to_the_next_call():
    """端到端的那一步：按一次 t 之后，第二次连问都不问。"""
    memory = ApprovalMemory()
    tool = make_tool()

    def pressing_t(t, arguments):
        memory.grant(t.name)
        return True

    first = check_permission(tool, {}, PermissionPolicy(), pressing_t, memory)
    second = check_permission(tool, {}, PermissionPolicy(), never_asks, memory)

    assert first.outcome == "approved"
    assert first.remembered == frozenset({"list_files"})   # 审计看得见"这次顺带记住了"
    assert second.outcome == "rule_allowed"


def test_a_plain_yes_remembers_nothing():
    result = check_permission(make_tool(), {}, PermissionPolicy(), lambda t, a: True,
                              ApprovalMemory())

    assert result.outcome == "approved"
    assert result.remembered == frozenset()


def test_the_agent_audits_which_call_created_the_rule():
    """审计里要能回答"这条 shell 为什么没问就跑了"。"""
    registry, calls = recording_registry(risk=RiskLevel.MEDIUM)
    memory = ApprovalMemory()
    events = Collector()

    def pressing_t(t, arguments):
        memory.grant(t.name)
        return True

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {})], usage=usage()),
        ModelResponse(content=None, tool_calls=[tool_call("list_files", {}, call_id="c2")], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    Agent(model, registry, PermissionPolicy(), asker=pressing_t, memory=memory,
          on_event=events).run(Session.new("s"), "列目录")

    permission_events = events.of("permission")
    assert [e["outcome"] for e in permission_events] == ["approved", "rule_allowed"]
    # 第一次批准必须带上"它同时留下了什么规则"，否则那几十次自动放行只能靠猜
    assert permission_events[0]["remembered"] == ["list_files"]
    assert "remembered" not in permission_events[1]
    assert len(calls) == 2          # 两次都真的执行了


# --- 提示本身 -----------------------------------------------------------

def test_typing_t_remembers_the_tool(typed):
    typed("t")
    memory = ApprovalMemory()

    assert cli_asker(make_tool(), {}, memory=memory) is True
    assert memory.tools() == frozenset({"list_files"})


@pytest.mark.parametrize("answer", ["", "n", "no", "随便什么"])
def test_everything_that_is_not_yes_is_a_no(typed, answer):
    """回车也走这一支：提示是 [y/N]，不是 [Y/n]。"""
    typed(answer)
    assert cli_asker(make_tool(), {}, memory=ApprovalMemory()) is False


def test_t_is_refused_when_there_is_nowhere_to_remember_it(typed):
    """答应了却记不住比拒绝更坏：用户以为已经永久放行，下一次却还被问。"""
    typed("t")
    assert cli_asker(make_tool(), {}) is False


def test_eof_never_grants_anything(monkeypatch):
    def eof():
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    memory = ApprovalMemory()

    assert cli_asker(make_tool(), {}, memory=memory) is False
    assert memory.tools() == frozenset()


def test_stdin_that_cannot_be_read_is_also_a_no(monkeypatch):
    """stdin 被接走或已关闭时 input() 抛的是 OSError，不是 EOFError。

    它冒出去会穿过 gate 落到 Agent 的 except Exception 上，把一次审批变成
    "工具执行失败" —— 而读不到输入的答案和 EOF 完全一样：拒绝。
    """
    def unreadable():
        raise OSError("reading from stdin while output is captured")

    monkeypatch.setattr(builtins, "input", unreadable)
    memory = ApprovalMemory()

    assert cli_asker(make_tool(), {}, memory=memory) is False
    assert memory.tools() == frozenset()


def test_the_t_option_is_only_shown_when_it_can_be_honored(typed, capsys):
    typed("")
    cli_asker(make_tool(), {}, memory=ApprovalMemory())
    assert "/t]" in capsys.readouterr().err

    cli_asker(make_tool(), {})
    without = capsys.readouterr().err
    assert "t = " not in without
    assert "[y/N]" in without


def test_high_risk_says_what_t_really_gives_away(typed, capsys):
    """对 shell 按 t 不是"少一次确认"，而是**再也看不见它要执行什么** ——
    而命令原文正是那道关唯一的判断依据。"""
    typed("n")
    cli_asker(make_tool(RiskLevel.HIGH, "shell"), {"command": "ls"}, memory=ApprovalMemory())

    hint = [line for line in capsys.readouterr().err.splitlines() if "t = " in line]
    assert hint and "不会再看到" in hint[0]
    assert PERMISSION_FILE_NAME in hint[0]      # 说清它被写进了哪个文件


def test_medium_risk_uses_the_ordinary_wording(typed, capsys):
    typed("n")
    cli_asker(make_tool(), {}, memory=ApprovalMemory())

    hint = [line for line in capsys.readouterr().err.splitlines() if "t = " in line]
    assert hint and "以后不再询问这个工具" in hint[0]
