"""ask_user：模型向人提问这条通道。

这一组测试盯的是四件事，而它们分别对应四种**事后会出事**的错法：

  1. 答案真的回到了上下文里，而且带着"这是用户说的"这个标记 —— 没有标记，模型会把
     自己编的答案当成用户给的，然后一路往下做；
  2. **没有人可问**时给的是"没有人回答"，不是空串、不是"用户没有意见"；
  3. 提问**不产生任何权限效果** —— 拿到"同意了"不会让下一次 shell 免审；
  4. 它永远不在工作线程里问（会问人的工具不能并行），"谁回答了哪一条"只有一个答案。

第 3 条是安全边界，不是风格问题：能靠提问换放行的话，模型自己念一句"用户已经同意"
就成了绕过审批的路。所以它有一条专门的测试，而且断言落在"asker 仍然被叫了"和
"memory 里什么都没多"这两个**事实**上，而不是文案上。
"""

import builtins
import json
import threading

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import ApprovalMemory, PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin.ask import (
    ANSWERED,
    SKIPPED,
    UNAVAILABLE,
    MAX_ANSWER_CHARS,
    Answer,
    AskUserArgs,
    cli_questioner,
    unavailable_questioner,
)
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import Collector, ScriptedModel, ScriptedQuestioner, tool_call, usage


QUESTION = {"question": "用哪个数据库？", "options": ["PostgreSQL", "SQLite"]}


def run(questioner, arguments=None, session=None, policy=None, asker=None, on_event=None):
    """一轮对话：模型问一个问题，拿到答案，然后给出最终答复。

    走的是**真实的注册表**（create_tool_registry），所以这条测试同时钉住了"装配起来
    确实能用"—— 只测一个手搭的工具的话，tools/builtin/__init__.py 里忘了注册也发现不了。
    """
    registry = create_tool_registry(".", questioner=questioner)
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("ask_user", arguments or QUESTION)],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    session = session if session is not None else Session.new("s")
    Agent(model, registry, policy or PermissionPolicy({RiskLevel.LOW}),
          asker=asker, on_event=on_event).run(session, "开始")
    return session, model


def tool_texts(session) -> list[str]:
    return [m["content"] for m in session.messages if m["role"] == "tool"]


# --- 答案回到上下文里 -----------------------------------------------------

def test_the_answer_reaches_the_model_marked_as_the_users_words():
    """前缀「用户回答：」是这条测试的重点。

    模型看不到工具结果的来源：工具结果和用户的话在它眼里是两种东西。不标出来，一次
    提问之后它就可能把自己的推断当成人给的答案，而这正是死循环式提问的起点。
    """
    questioner = ScriptedQuestioner("PostgreSQL")
    session, model = run(questioner)

    assert [q.question for q in questioner.asked] == ["用哪个数据库？"]
    assert tool_texts(session) == ["用户回答：PostgreSQL"]
    # 而且**下一轮请求**真的带上了它 —— 这才是"答案回到了上下文里"的证据。
    assert any(
        message["role"] == "tool" and message["content"] == "用户回答：PostgreSQL"
        for message in model.seen_messages[-1]
    )


def test_a_huge_answer_is_truncated_and_says_so():
    """答案会永久留在会话历史里、此后每轮重发，所以有上限 —— 但截断必须说出来。

    不说的话，模型拿到半截文本却以为那是全部，接着按半句话干活。
    """
    session, _ = run(ScriptedQuestioner("x" * (MAX_ANSWER_CHARS + 100)))
    text = tool_texts(session)[-1]

    assert "回答被截断" in text
    assert len(text) < MAX_ANSWER_CHARS + 100     # 真的切了，不是只加了一句话


# --- 没有人可问 / 跳过了 --------------------------------------------------

def test_nobody_to_ask_is_never_an_empty_answer():
    """空串会被模型读成"用户没有意见" —— 而它根本不知道有没有人在看。

    所以这一支的文案必须说清"没有人回答"，并明确禁止把它当成默许。
    """
    session, _ = run(unavailable_questioner)
    text = tool_texts(session)[-1]

    assert "没有人可以回答" in text
    assert "默许" in text
    assert text.strip()
    assert "用户回答：" not in text


def test_no_questioner_configured_behaves_the_same_way():
    """要问却没配提问通道，按"没有人可问"处理 —— 和 gate 里 no_asker 同一个失败方向。

    默认成"能问、问出来算同意"是最坏的 fail-open，所以这条钉的是 questioner=None
    这一支的**方向**，不是它的文案。
    """
    session, _ = run(None)
    assert "没有人可以回答" in tool_texts(session)[-1]


def test_skipped_is_not_consent():
    session, _ = run(ScriptedQuestioner(Answer("", SKIPPED, 0)))
    text = tool_texts(session)[-1]

    assert "跳过" in text
    assert "用户回答：" not in text


# --- 提问不产生任何权限效果（安全边界） ----------------------------------

def test_an_answer_never_grants_anything():
    """**这一条是安全边界。**

    模型先问一句"我可以执行这条命令吗"，再自己去执行 —— 如果提问的答案能影响权限，
    那它只要把问题写得像一句许可，就等于给自己发了一张放行条。所以：shell 仍然被
    拦下来问人，memory 里也什么都没有多。
    """
    memory = ApprovalMemory()
    asked: list[str] = []
    registry = create_tool_registry(".", questioner=ScriptedQuestioner("同意，你执行吧"))
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("ask_user", QUESTION, "c0")], usage=usage()),
        ModelResponse(content=None,
                      tool_calls=[tool_call("shell", {"command": "rm -rf build"}, "c1")],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])

    session = Session.new("s")
    Agent(model, registry, PermissionPolicy({RiskLevel.LOW}),
          asker=lambda tool, arguments: asked.append(tool.name) or False,
          memory=memory).run(session, "开始")

    assert asked == ["shell"]                 # ← 仍然拦下来问了人
    assert memory.tools() == frozenset()      # ← 提问没有往 memory 里写任何东西
    assert "权限拒绝" in tool_texts(session)[-1]


def test_asking_is_never_gated_by_an_approval():
    """提问自己不该触发审批。

    为了问一个问题先弹一次审批，那个审批才是真正的打断 —— 而 ask_user 是 LOW，
    默认策略下自动放行，正好。
    """
    asked: list[str] = []
    session, _ = run(ScriptedQuestioner("PostgreSQL"),
                     asker=lambda tool, arguments: asked.append(tool.name) or False)

    assert asked == []
    assert "权限拒绝" not in json.dumps(session.messages, ensure_ascii=False)


# --- 会问人的工具永远不在工作线程里 --------------------------------------

def test_registration_rejects_an_interactive_tool_that_claims_parallel_safe():
    """这条组合在**启动时**就该炸。

    ask_user 的风险是 LOW，而"能并行的必须是 LOW"那条校验正好拦不住它 —— 于是
    "会问人"+"标了并行"能通过校验、跑到真实会话里才变成两条提问互相抢 stdin，
    而"谁回答了哪一条"也就没法回答了。
    """
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="interactive"):
        registry.register(Tool(
            name="ask_user", description="问问题", risk=RiskLevel.LOW,
            args_model=ListFilesArgs, handler=lambda **kwargs: None,
            interactive=True, parallel_safe=True,
        ))


def test_the_real_registry_marks_ask_user_interactive():
    tool = create_tool_registry(".").get("ask_user")

    assert tool.interactive is True
    assert tool.parallel_safe is False


def test_a_batch_containing_a_question_falls_back_to_serial():
    """整批只要有 ask_user，就不会进线程池 —— 所以问题一定在主线程里、按模型给的顺序问。

    盯的是**后果**而不是标志位：并行的前提是整批 parallel_safe，谁要是给 ask_user
    加上了那个标志，这条会红（上面那条注册期校验会先一步炸）。
    """
    threads: list[str] = []

    def questioner(question: AskUserArgs) -> Answer:
        threads.append(threading.current_thread().name)
        return Answer("PostgreSQL", ANSWERED, 0)

    registry = create_tool_registry(".", questioner=questioner)
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[
            tool_call("list_files", {"path": "."}, "c0"),      # 只读、本来能并行
            tool_call("ask_user", QUESTION, "c1"),
        ], usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    Agent(model, registry, PermissionPolicy({RiskLevel.LOW})).run(Session.new("s"), "开始")

    assert threads == ["MainThread"]


# --- 审计 -----------------------------------------------------------------

def test_the_audit_records_who_answered_and_how_long_it_took():
    """审计要能回答"这一轮到底有没有人真的回答过"，以及人占了多久。

    同时钉住"同一份事实只写一遍"：答案正文已经在会话文件里了，审计**不抄全文**。
    """
    events = Collector()
    run(ScriptedQuestioner(Answer("PostgreSQL", ANSWERED, 4200)), on_event=events)

    result = [e for e in events.of("tool_result") if e["tool"] == "ask_user"][0]
    assert result["question_status"] == ANSWERED
    assert result["human_wait_ms"] == 4200
    assert result["status"] == "ok"
    assert "PostgreSQL" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("answer,expected", [
    (unavailable_questioner(AskUserArgs(question="q")), UNAVAILABLE),
    (Answer("", SKIPPED, 0), SKIPPED),
])
def test_the_audit_separates_the_three_endings(answer, expected):
    """三种结局在审计里必须分得开：谁回答了、谁跳过了、有没有人在场。"""
    events = Collector()
    run(ScriptedQuestioner(answer), on_event=events)

    result = [e for e in events.of("tool_result") if e["tool"] == "ask_user"][0]
    assert result["question_status"] == expected


# --- CLI 提问者 -----------------------------------------------------------

def typed(monkeypatch, text: str, **arguments) -> Answer:
    """把 cli_questioner 当成"用户在终端上敲了 text"来用。"""
    monkeypatch.setattr(builtins, "input", lambda: text)
    return cli_questioner(AskUserArgs(**{"question": "用哪个？", **arguments}))


def test_enter_means_skipped(monkeypatch):
    """回车是**跳过**，不是同意。连续交互里最容易做的动作就是一路回车。"""
    answer = typed(monkeypatch, "  ", options=["PostgreSQL"])

    assert answer.status == SKIPPED
    assert answer.text == ""


def test_eof_means_nobody_to_ask(monkeypatch):
    """管道 / CI 里 input() 抛 EOFError —— 答案和"没有人"完全一样，不是空答案。"""
    def eof():
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)

    assert cli_questioner(AskUserArgs(question="q")).status == UNAVAILABLE


def test_unreadable_stdin_is_also_nobody_to_ask(monkeypatch):
    """stdin 被接走时抛的是 OSError。

    它冒出去会穿过工具层变成"工具执行失败"，把一次提问伪装成一个坏掉的工具 ——
    而只测 EOFError 是发现不了的（两者是不同的异常类型）。
    """
    def unreadable():
        raise OSError("stdin 被接走了")

    monkeypatch.setattr(builtins, "input", unreadable)

    assert cli_questioner(AskUserArgs(question="q")).status == UNAVAILABLE


@pytest.mark.parametrize("text,options,multi,expected", [
    ("1", ["PostgreSQL", "SQLite"], False, "PostgreSQL"),
    ("2", ["PostgreSQL", "SQLite"], False, "SQLite"),
    ("1,2", ["PostgreSQL", "SQLite"], True, "PostgreSQL、SQLite"),
    ("1，2", ["PostgreSQL", "SQLite"], True, "PostgreSQL、SQLite"),   # 中文逗号
    ("换个别的", ["PostgreSQL", "SQLite"], False, "换个别的"),         # 不是编号 → 原文
    ("3", ["PostgreSQL", "SQLite"], False, "3"),                     # 越界 → 原文
    ("1, 也行", ["PostgreSQL", "SQLite"], True, "1, 也行"),           # 混着来 → 整行原文
])
def test_a_number_is_translated_but_anything_else_is_passed_through(
    monkeypatch, text, options, multi, expected
):
    """编号换成选项原文；换不了就原样当自由文本 —— **看不懂就不猜**。

    混着来的那种（`1, 也行`）整行都算自由文本：猜一半会把人多说的那半句丢掉，
    而丢掉的正好可能是真正有用的信息。
    """
    answer = typed(monkeypatch, text, options=options, multi_select=multi)

    assert answer.status == ANSWERED
    assert answer.text == expected


def test_the_question_and_options_go_to_stderr(monkeypatch, capsys):
    """提问走 stderr，stdout 只留 Agent 的产出。

    和 cli_asker 同一条约定：`> 对话.txt` 拿到的必须是干净的对话正文，提问是 runtime
    的交互，不是答案的一部分。
    """
    typed(monkeypatch, "1", options=["PostgreSQL", "SQLite"], header="数据库")
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "用哪个？" in captured.err
    assert "1) PostgreSQL" in captured.err
    assert "（数据库）" in captured.err
