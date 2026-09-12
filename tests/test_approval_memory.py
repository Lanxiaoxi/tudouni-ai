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
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
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
    memory = ApprovalMemory(on_change=lambda tools, prefixes: saved.append(tools))

    memory.grant("shell")
    memory.grant("shell")           # 没新东西，不该再写一遍文件
    memory.grant("write_file")

    assert saved == [frozenset({"shell"}), frozenset({"shell", "write_file"})]


def test_persist_receives_both_kinds_of_grant():
    """两类记忆共用一次落盘 —— 回调必须同时拿到它们，否则写回文件时会把另一半丢掉。"""
    saved = []
    memory = ApprovalMemory(on_change=lambda tools, prefixes: saved.append((tools, prefixes)))

    memory.grant_prefix(("git", "add"))

    assert saved == [(frozenset(), frozenset({("git", "add")}))]


def test_grant_prefix_is_idempotent():
    memory = ApprovalMemory()
    assert memory.grant_prefix(("git", "add")) is True
    assert memory.grant_prefix(("git", "add")) is False
    assert memory.prefixes() == frozenset({("git", "add")})


def test_persist_failure_keeps_the_memory_and_says_so(capsys):
    """落盘失败不能往外抛：agent 的 except Exception 就在外面，抛出去会让这一轮
    本来合法的工具调用变成"工具执行失败"。但也不能是静默失败。"""
    def broken(_tools, _prefixes):
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


def test_medium_risk_uses_the_ordinary_wording(typed, capsys):
    typed("n")
    cli_asker(make_tool(), {}, memory=ApprovalMemory())

    hint = [line for line in capsys.readouterr().err.splitlines() if "t = " in line]
    assert hint and "以后不再询问这个工具" in hint[0]


# --- 命令类工具：t 记的是前缀，不是整个 shell -----------------------------

def hint_of(capsys) -> str:
    lines = [line for line in capsys.readouterr().err.splitlines() if "t = " in line]
    return lines[0] if lines else ""


def test_t_on_a_command_tool_offers_a_prefix(typed, capsys):
    """对 shell 按 t 不是"整个 shell 免问"，而是"这条命令前缀免问"。

    提示必须把前缀原样写出来、并说清它落在哪个文件 —— 人唯一的判断依据就是这一行。
    """
    typed("n")
    cli_asker(
        make_tool(RiskLevel.HIGH, "shell"),
        {"command": "git add -p x.py"},
        memory=ApprovalMemory(label=PERMISSION_FILE_NAME),
    )

    hint = hint_of(capsys)
    assert "git add" in hint
    assert "不会再给你看" in hint               # 之后它要执行什么，你不会再看到
    assert "shell" not in hint                  # 不是整个工具
    assert PERMISSION_FILE_NAME in hint


def test_t_on_a_command_tool_records_the_prefix(typed):
    typed("t")
    memory = ApprovalMemory()
    tool = make_tool(RiskLevel.HIGH, "shell")

    assert cli_asker(tool, {"command": "git add -p x.py"}, memory=memory) is True

    assert memory.prefixes() == frozenset({("git", "add")})
    assert memory.tools() == frozenset()        # 没有顺手把整个 shell 记下来


def test_t_is_not_offered_when_the_command_cannot_be_parsed(typed, capsys):
    """解析不了就不提供 t：一个按键记下"整个 shell 免问"与这个功能的初衷正好相反。"""
    typed("t")
    memory = ApprovalMemory()

    assert cli_asker(make_tool(RiskLevel.HIGH, "shell"),
                     {"command": "git log > out.txt"}, memory=memory) is False

    assert memory.prefixes() == frozenset()
    assert memory.tools() == frozenset()        # 也没有退回"记住整个工具"
    stderr = capsys.readouterr().err
    assert "t = " not in stderr                 # 提示里根本没给这个选项
    assert "[y/N]" in stderr


def test_t_is_not_offered_for_a_chained_command(typed, capsys):
    """链式命令也不提供 t —— 推出来的前缀盖不住整条链，按了也白按。"""
    typed("t")
    memory = ApprovalMemory()

    assert cli_asker(make_tool(RiskLevel.HIGH, "shell"),
                     {"command": "git status && rm -rf build"}, memory=memory) is False

    assert memory.prefixes() == frozenset()
    stderr = capsys.readouterr().err
    assert "t = " not in stderr
    assert "[y/N]" in stderr


def test_a_bare_program_name_is_what_the_hint_shows_too(typed, capsys):
    """`ls -la` 推出的前缀是 `ls`（第二个 token 是选项）—— 提示照实说，人自己判断。"""
    typed("n")
    cli_asker(make_tool(RiskLevel.HIGH, "shell"), {"command": "ls -la"}, memory=ApprovalMemory())

    assert "ls 开头的命令" in hint_of(capsys)
