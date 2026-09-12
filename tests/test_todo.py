"""todo_write：模型自己维护的任务列表。

这一组测试盯的是三件容易被无声破坏的事：

  1. 每次调用**替换**整份列表（幂等），而全完成时存下来的是空列表 —— 反过来（增量、
     或者留着全对勾的列表）都会让它慢慢变成一份谁也不信的记录；
  2. 当前列表每轮重新贴在请求末尾，但**从不进 `session.messages`** —— 和步数提示
     同一条约定，理由也一样（逐轮变化的东西不该被持久化，也不该去稀释
     「一条 assistant = 一步」那个派生规则）；
  3. 它是进度，不是任务本身：不触发审批、不写 memory、也不替模型补状态。
"""

import builtins

import pytest
from pydantic import ValidationError

from agent_runtime.agents import Agent, StepLimitExceeded
from agent_runtime.cli import run_repl
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.todo import (
    IN_PROGRESS,
    TODOS_KEY,
    TodoBoard,
    progress_line,
    todo_note,
)
from agent_runtime.tools.tool import RiskLevel

from fakes import Collector, ScriptedModel, tool_call, usage

TWO_STEPS = [
    {"content": "写 tools/todo.py", "status": "in_progress"},
    {"content": "写测试", "status": "pending"},
]


def execute(board: TodoBoard, todos) -> str:
    """按模型调用的那条路跑一次：注册表 → 参数校验 → handler。"""
    registry = create_tool_registry(".", todos=board)
    return registry.get("todo_write").execute({"todos": todos}).text


# --- 全量替换 -------------------------------------------------------------

def test_each_call_replaces_the_whole_list():
    """第二次调用整份盖掉第一次 —— 不是增量、也不是合并。

    增量要求模型对"上一版长什么样"记得准，而计划天生在变；对不上就会失败或者错配
    （这正是 edit_file 的 old_string 只适合稳定内容的原因）。全量覆写是幂等的。
    """
    metadata: dict = {}
    board = TodoBoard(metadata)

    execute(board, TWO_STEPS)
    execute(board, [{"content": "只剩这一步", "status": "in_progress"}])

    assert [item["content"] for item in metadata[TODOS_KEY]] == ["只剩这一步"]


def test_all_completed_clears_the_list():
    """全对勾的列表此后每轮都要重发一遍，而它已经不含任何信息。

    这不是丢记录：每一次调用的参数都在会话历史里，那一份才是证据。
    """
    metadata: dict = {}
    text = execute(TodoBoard(metadata), [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "completed"},
    ])

    assert metadata[TODOS_KEY] == []
    assert "清空" in text
    assert todo_note(metadata) is None


def test_an_empty_list_clears_the_list():
    """模型可以主动把列表扔掉（这个任务不需要它了）。"""
    metadata: dict = {}
    text = execute(TodoBoard(metadata), [])

    assert metadata[TODOS_KEY] == []
    assert text == "任务列表已清空。"


def test_a_missing_list_is_rejected_instead_of_clearing():
    """`todos` 少给，必须是参数校验失败，**不能**被解析成"清空列表"。

    字段带默认值时就是这么坏掉的：一次静默的数据丢失，而且看起来完全正常（模型以为
    自己只是少写了一个字段，列表却没了）。所以它是必填。
    """
    metadata = {TODOS_KEY: [{"content": "别丢了我", "status": "pending"}]}
    registry = create_tool_registry(".", todos=TodoBoard(metadata))

    with pytest.raises(ValidationError):
        registry.get("todo_write").execute({})

    assert metadata[TODOS_KEY] == [{"content": "别丢了我", "status": "pending"}]


def test_an_unknown_status_is_rejected():
    """状态是字面量，不是一个自由字符串：拼错会当场打回，而不是存成第三种状态。"""
    registry = create_tool_registry(".", todos=TodoBoard({}))

    with pytest.raises(ValidationError):
        registry.get("todo_write").execute(
            {"todos": [{"content": "x", "status": "inprogress"}]}
        )

    # 枚举跟着 schema 一起发给模型（预防），校验只是兜底 —— 两份出自同一个字面量
    assert registry.get("todo_write").parameters["$defs"]["TodoItem"][
        "properties"
    ]["status"]["enum"] == ["pending", "in_progress", "completed"]


# --- 回给模型的那句话 -----------------------------------------------------

def test_the_ack_does_not_echo_the_list_back():
    """不回显整份列表：模型刚在 assistant 消息里发过一遍，而工具结果此后每轮都要重发。"""
    text = execute(TodoBoard({}), TWO_STEPS)

    assert "2 项" in text
    assert "写 tools/todo.py" not in text


def test_work_remaining_without_an_in_progress_item_gets_a_nudge():
    """还有活却一条进行中都没有时，只提醒 —— **不替它改**。

    代填等于伪造模型的主张：它下一轮读到自己"说过"的状态会当真，而那份状态是我们编的。
    """
    metadata: dict = {}
    text = execute(TodoBoard(metadata), [
        {"content": "a", "status": "pending"},
        {"content": "b", "status": "pending"},
    ])

    assert "没有任何一项标为进行中" in text
    assert [item["status"] for item in metadata[TODOS_KEY]] == ["pending", "pending"]


def test_no_nudge_when_something_is_in_progress():
    text = execute(TodoBoard({}), TWO_STEPS)

    assert "提醒" not in text


# --- 每轮重新注入 ---------------------------------------------------------

def run_agent(session, tools=None, **kwargs):
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("todo_write", {"todos": TWO_STEPS})],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(
        model,
        tools if tools is not None else create_tool_registry(
            ".", todos=TodoBoard(session.metadata)
        ),
        PermissionPolicy({RiskLevel.LOW}),
        **kwargs,
    )
    agent.run(session, "把活干完")
    return model


def injected(payload) -> list[str]:
    """载荷里那些由运行时注入的临时消息（不是会话历史里的那种）。"""
    return [
        str(message.get("content"))
        for message in payload
        if message["role"] == "user" and "当前任务" in str(message.get("content"))
    ]


def test_the_model_sees_the_current_list_in_every_request():
    """列表是**贴上去的**，不是让模型去翻历史找最近那一版。

    第一次请求时列表还不存在（模型还没建），所以只有第二次请求带它 —— 这个"从无到有"
    正是注入点每轮重新求值的证据（换成"写进 messages 一次"就做不到）。
    """
    session = Session.new("s")
    model = run_agent(session, session_notes=todo_note)

    seen = [injected(payload) for payload in model.seen_messages]
    assert seen[0] == []
    assert seen[1] != []
    assert "[进行中] 写 tools/todo.py" in seen[1][0]
    assert "[待办] 写测试" in seen[1][0]
    # 和步数提示合成同一条临时消息：载荷尾部始终只有一条
    assert "剩余步数" in seen[1][0]


def test_the_list_never_enters_the_session_messages():
    """注入的那一份只活在请求里 —— 落盘的是工具调用本身，不是这段提示。

    和步数提示同一条约定（见 test_prompt.py 里那条）。两条理由：会话文件不该平白多出
    几十条 user 消息；而且列表逐轮变化，本来就不该被持久化成历史的一部分。
    """
    session = Session.new("s")
    run_agent(session, session_notes=todo_note)

    assert not any("当前任务" in str(m.get("content")) for m in session.messages)
    # 但它确实以"工具调用"的形式留下了记录（那是可追的证据）
    assert any(
        call["function"]["name"] == "todo_write"
        for m in session.messages if m["role"] == "assistant"
        for call in (m.get("tool_calls") or [])
    )


def test_without_the_injection_the_list_stays_hidden():
    """没注入就没有提示 —— 说明"列表回到模型眼前"这件事确实靠的是这个注入点。"""
    session = Session.new("s")
    model = run_agent(session)

    assert [injected(payload) for payload in model.seen_messages] == [[], []]
    # 但状态照样写下来了（写和读是两条独立装配的路，只有 main.py 两条都接上）
    assert progress_line(session.metadata) == "0/2 完成，当前：写 tools/todo.py"


# --- 跨进程 ---------------------------------------------------------------

def test_the_list_survives_a_restart(workdir):
    """列表存在会话文件里，所以恢复会话之后它还在 —— 这也是"要重新说一遍"的理由。"""
    store = JsonSessionStore(workdir)
    session = Session.new("s")
    TodoBoard(session.metadata)(TWO_STEPS)
    store.save(session)

    again = store.load("s")

    assert progress_line(again.metadata) == "0/2 完成，当前：写 tools/todo.py"
    assert "写测试" in todo_note(again.metadata)


@pytest.mark.parametrize("metadata", [
    {TODOS_KEY: "不是列表"},
    {TODOS_KEY: [{"content": "少了状态"}]},
    {TODOS_KEY: [{"content": "状态不认识", "status": "doing"}]},
    {TODOS_KEY: [{"content": 42, "status": "pending"}]},
    {TODOS_KEY: [{"content": "好的", "status": "pending"}, "不是对象"]},
])
def test_a_malformed_list_counts_as_no_list(metadata):
    """会话文件来自磁盘（会被复制、拼接、手工编辑），坏数据一律当"没有列表"。

    **一条不对就整份丢掉**，不是跳过那一条：跳过会报出一份"看起来完整、其实缺了几条"
    的列表，而模型会拿它当全部。宁可少说，不要错说 —— 而且绝不能在这里抛，一个坏字段
    不该让整个会话再也发不出请求。
    """
    assert todo_note(metadata) is None
    assert progress_line(metadata) is None


# --- 它只是进度 -----------------------------------------------------------

def test_it_never_triggers_an_approval():
    """任务管理不弹审批：它没有副作用，只是为了问一句"允许我记进度吗"而打断人，很荒唐。

    它的风险是 LOW，默认策略里自动放行 —— 断言落在"asker 一次都没被叫"这个事实上。
    """
    asked: list[str] = []
    session = Session.new("s")
    run_agent(session, asker=lambda tool, arguments: asked.append(tool.name) or False)

    assert asked == []


def test_it_is_not_parallel_safe():
    """它写会话 metadata 这块共享状态：一批里两条同时跑就是经典的 lost update，
    而且两边都会报成功（和 edit_file 不能并行是同一个理由）。"""
    tool = create_tool_registry(".").get("todo_write")

    assert tool.risk is RiskLevel.LOW
    assert tool.parallel_safe is False
    assert tool.interactive is False


def test_the_audit_records_what_is_still_tracked():
    """审计里记的是**存下来还留着的那份**：事后要回答的是"还剩几项、这功能有没有被用"。

    模型发了什么，`tool_call` 那条事件的参数里已经有一份完整的 —— 不记第二遍。
    """
    events = Collector()
    session = Session.new("s")
    run_agent(session, on_event=events)

    result = [e for e in events.of("tool_result") if e["tool"] == "todo_write"][0]
    assert result["todos_total"] == 2
    assert result["todos_in_progress"] == 1
    assert result["todos_completed"] == 0


# --- 给人的那一行 ---------------------------------------------------------

def test_progress_line_is_one_short_line():
    metadata = {TODOS_KEY: [
        {"content": "读现有实现", "status": "completed"},
        {"content": "写 tools/todo.py", "status": "in_progress"},
        {"content": "写测试", "status": "pending"},
    ]}

    assert progress_line(metadata) == "1/3 完成，当前：写 tools/todo.py"


def test_progress_line_stays_short_even_for_a_long_task():
    """行式终端里这一行不能因为一条任务的正文就折行 —— 折行的进度条只是噪声。"""
    line = progress_line({TODOS_KEY: [{"content": "x" * 200, "status": IN_PROGRESS}]})

    assert len(line) < 60


def test_hitting_the_step_limit_says_what_is_left(monkeypatch, capsys):
    """撞上限时"还剩什么"是最该说的一句 —— 列表本来就记着它，而这时用户手上唯一的
    问题正是"还差多少"。"""
    class LimitAgent:
        def run(self, session, line, **kwargs):
            raise StepLimitExceeded(40, ["read_file"])

    session = Session.new("s")
    TodoBoard(session.metadata)([
        {"content": "读现有实现", "status": "completed"},
        {"content": "写 tools/todo.py", "status": "in_progress"},
        {"content": "写测试", "status": "pending"},
    ])

    answers = {"typed": 0}

    def fake_input():
        # 第一行是用户那句话；回合被 StepLimitExceeded 打断之后循环会再读一次，
        # 那时给 EOF（= 用户按了 Ctrl+Z），循环正常退出。
        answers["typed"] += 1
        if answers["typed"] == 1:
            return "把活干完"
        raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)

    run_repl(LimitAgent(), session, "s", None, None)

    err = capsys.readouterr().err
    assert "步数用尽" in err
    assert "未做完的：1/3 完成，当前：写 tools/todo.py" in err


# --- 描述与 schema --------------------------------------------------------

def test_the_description_says_when_not_to_build_a_list():
    """这个工具最容易变成仪式：单步的琐碎活也建一张三级列表，除了烧 token 什么也没干。

    所以"什么时候别用我"必须写在描述里 —— 它是每一轮都发出去的那一份（提示词对恢复的
    旧会话已经过期，见 prompts/system.zh.md 那段说明）。
    """
    description = create_tool_registry(".").get("todo_write").description

    assert "完整的新列表" in description     # 全量替换，不是增量
    assert "不要建列表" in description       # 什么时候别用
    assert "in_progress" in description      # 活性约束


def test_nested_args_models_do_not_leak_internal_comments():
    """嵌套模型的 docstring 同样会被 Pydantic 写成 `$defs` 里的 description。

    ToolArgs 那个钩子原来只剥顶层 —— 于是"内部注释不外泄"这条在**只有嵌套模型才走得到**
    的那条路上是假的，而它照样每次请求都发。todo_write 是这个项目第一个嵌套 args 模型。
    """
    parameters = create_tool_registry(".").get("todo_write").parameters

    assert "$defs" in parameters            # 确认这条路上真的有嵌套定义
    assert all("description" not in d for d in parameters["$defs"].values())
    # 字段级的 description 要留着 —— 那才是给模型看的说明
    assert parameters["$defs"]["TodoItem"]["properties"]["content"]["description"]
