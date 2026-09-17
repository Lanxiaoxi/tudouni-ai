"""历史压缩（Context Compaction）：**较早的详细历史 → 一份工作记忆。**

这个功能的核心不是"删掉旧消息"，而是**换一种表示方式**，所以这里钉的是四条不变量：

  1. **原文一条都不动。** `session.messages` 的长度和内容在压缩前后逐字节相同 ——
     压缩只改"这一次请求往模型那边发什么"。磁盘上那份历史是档案库。
  2. **边界永远切在一条 `user` 消息之前。** 从中间切开会留下一条带 `tool_calls`
     却没有配对结果的 assistant 消息，那种历史此后每一轮都发不出去（400，而那个
     错误看起来像"上下文太长"）。
  3. **最近的消息不压缩。** 摘要替换掉的只有最早的那一段，尾部原文照旧。
  4. **失败什么都不改。** 摘要那次模型调用失败 / 没有可折的区间 / 正在重入时，
     状态、边界、摘要全都保持原样 —— 降级那一档继续兜着。

另有一条**兼容路径**：没压过的会话走的必须是**和这个功能之前完全相同**的那条路
（`attention` 直接返回原列表），否则这个功能会给所有老会话加一层隐形行为。
"""

import json

from agent_runtime import i18n
from agent_runtime.agents import Agent
from agent_runtime.context import (
    ArtifactStore,
    ContextBudget,
    ContextManager,
    compaction,
    ref,
)
from agent_runtime.context.models import ArtifactSource
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelResponse, ModelTransientError
from agent_runtime.security import PermissionPolicy
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.tools.builtin.filesystem import ListFilesArgs
from agent_runtime.tools.tool import RiskLevel, Tool, ToolRegistry

from fakes import usage


# --- 公共脚手架 ---------------------------------------------------------------

def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def tool_call(text: str = "读一下") -> dict:
    """一条**带 tool_calls** 的 assistant 消息（边界那条不变量要它才测得出来）。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": f"c-{text}", "type": "function",
            "function": {"name": "read_file",
                         "arguments": json.dumps({"path": "a.py"})},
        }],
    }


def tool_result(artifact_id: str, chars: int = 120) -> dict:
    return {
        "role": "tool",
        "tool_call_id": "c-读一下",
        "content": ref.build(artifact_id, chars, "read_file"),
        "artifact_id": artifact_id,
    }


def long_history(session: Session, store: ArtifactStore, turns: int = 12) -> list[dict]:
    """往会话里灌 `turns` 个完整回合，每个回合带一次真实的工具结果。

    工具结果走的是**真的 ArtifactStore**（不是手写的假引用）：这样"折叠之后那些
    引用不再进载荷"才是可验证的 —— 手写引用的话，渲染那一侧根本查不到 Artifact，
    测出来的是一段和真实形状无关的行为。

    每个回合的正文刻意写长（4000 字符那一档）：压缩的触发线是**按 token 算**的，
    而"灌够十几个回合就过线"这件事得让每个回合真的有分量。
    """
    for index in range(turns):
        session.messages.append(user(f"第 {index} 轮：读一下文件"))
        session.messages.append(tool_call(str(index)))
        artifact = store.create(
            f"第 {index} 轮的文件内容\n" * 200,
            type="text", source=ArtifactSource(tool="read_file", path="a.py"),
            metadata={"status": "ok", "lines": 200},
        )
        session.messages.append(tool_result(artifact.artifact_id, artifact.chars))
        session.messages.append(assistant(f"第 {index} 轮读完了，结论是 {index}"))
    return session.messages


class SummaryModel(ChatModel):
    """摘要调用回一句固定的摘要，普通调用回最终答案。

    **判据是"请求里有没有工具定义"**（摘要那次传 `[]`，普通那次传完整的 schema）：
    按调用次数判会在测试里埋一个"第几次是谁"的隐式假设，而那个假设一改就悄悄测错
    东西 —— 用签名里真实存在的差别来分流，读的人一眼看得出哪次是哪种调用。
    """

    def __init__(self, content: str = "final", summary: str = "【历史摘要】\n任务：读文件"):
        self.content = content
        self.summary = summary
        self.summary_calls: list[list[dict]] = []
        self.seen: list[tuple[list[dict], list | None]] = []

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        self.seen.append(([dict(m) for m in messages], tools))
        if tools is not None and len(tools) == 0:
            self.summary_calls.append([dict(m) for m in messages])
            return ModelResponse(content=self.summary, usage=usage())
        return ModelResponse(content=self.content, usage=usage())


class FailingModel(SummaryModel):
    """摘要调用一律失败，普通调用照常。"""

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        if tools is not None and len(tools) == 0:
            raise ModelTransientError("网关抽风")
        return super().complete(messages, tools=tools, on_delta=on_delta)


def build(base, *, window: int | None = None, content: str = "final",
          model: ChatModel | None = None, event_sink=None):
    """一个**带着 Context** 的 Agent，和 `composition` 里的装配同构。

    `window=None` = **这个模型不知道自己的窗口** —— 和 `composition` 里"配置里没写
    `context_window`"那条路一样，于是预算整体关着（压缩也不会自动触发）。要测
    "到线就压"的用 `build_tight`。
    """
    store = ArtifactStore(base / "artifacts")
    manager = ContextManager(
        store, budget=ContextBudget(max_tokens=window, reserve=0, headroom=0.0),
    )
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="读文件", risk=RiskLevel.LOW,
        args_model=ListFilesArgs, handler=lambda **kw: "", parallel_safe=True,
    ))
    session = Session.new("s")
    agent = Agent(
        model or SummaryModel(content), registry, PermissionPolicy({RiskLevel.LOW}),
        context=manager, on_event=event_sink,
    )
    return agent, session, agent.model, store, manager


def build_tight(base, *, turns: int = 12, **kwargs):
    """`build` 的**自动触发版**：窗口刚好卡在这段历史下面一点。

    ## 为什么窗口是算出来的，不是写死的

    一段历史的估算取决于好几个会变的东西：系统提示词有多长、测试怎么起的、项目
    说明有没有被注入。写一个固定的小窗口（试过 8000 / 2500 / 2400）会让这条测试
    变成"断言提示词和这段历史一样长"—— 提示词长一点它绿、短一点它红，而红的时候
    看起来像压缩坏了。实测踩过：同一条历史在 pytest 里是 2009 token、在裸脚本里是
    2208，而固定阈值正好卡在中间。

    所以窗口**由实测的估算反推**：先建会话、灌历史、量一次，再把窗口设成"阈值比它
    低 10%"的那个值。这一段历史因此必然越线，而且越线的量正好是那一成。

    ## 两个代价，都是刻意的

      * 这段历史会被折叠（约 2/3 的回合进摘要），而**尾部 `KEEP_RECENT_MESSAGES`
        条与第一条 user 留在原文里** —— 边界规则本身由第 1 节那几条测试钉着；
      * 窗口很小 ⇒ **降级阶梯同时也在工作**（那几份工具正文塞不下）。压缩和降级
        本来就是同一条链上的两档，而这里要测的是"压了没有"。
    """
    agent, session, model, store, manager = build(base, window=None, **kwargs)
    long_history(session, store, turns=turns)
    window = int(agent._measure(session) / 0.9)
    manager.budget.max_tokens = window
    return agent, session, model, store, manager


# --- 1. 边界（`fold_point`）----------------------------------------------------

def test_the_fold_always_ends_on_a_user_message():
    """**边界切在一条 user 消息之前** —— 这是 tool_calls 配对那条硬约束。

    切在任意位置会留下一段"assistant 说要调工具、结果被切掉了"的历史，而 provider
    对那种载荷直接 400（而且那个错误看起来像"上下文太长"）。
    """
    messages = []
    for index in range(12):
        messages += [user(f"u{index}"), tool_call(str(index)), tool_result("art_x"),
                     assistant(f"a{index}")]

    point = compaction.fold_point(messages)
    assert point > 0
    assert messages[point]["role"] == "user"


def test_the_first_user_message_is_never_folded():
    """第一条 user 是**这个会话的任务锚点**，而且 `message_marks` 把它当 stable/pinned。

    折掉它等于把"用户最初要什么"从上下文里拿掉，而那是模型继续干活的唯一依据。
    """
    messages = [user("任务：修 bug")]
    for index in range(12):
        messages += [tool_call(str(index)), tool_result("art_x"), assistant(f"a{index}"),
                     user(f"u{index}")]

    point = compaction.fold_point(messages)
    assert point > 1, "应该折掉一段"
    assert point != 1, "不能正好折掉第一条 user"


def test_a_short_history_is_left_alone():
    """历史太短时**什么都不折**（折 2 条换一次模型往返是负收益）。"""
    messages = [user("u"), assistant("a"), user("u2"), assistant("a2")]
    assert compaction.fold_point(messages) == 0


def test_the_tail_is_never_folded():
    """最近 `KEEP_RECENT_MESSAGES` 条**永远留在原文里**（原则 2）。"""
    messages = []
    for index in range(20):
        messages += [user(f"u{index}"), assistant(f"a{index}")]

    point = compaction.fold_point(messages)
    assert len(messages) - point >= compaction.KEEP_RECENT_MESSAGES


def test_a_second_fold_must_move_the_boundary_forward():
    """第二次压缩必须**比上一次更靠后** —— 否则就是白跑一次模型。"""
    messages = []
    for index in range(10):
        messages += [user(f"u{index}"), assistant(f"a{index}")]

    first = compaction.fold_point(messages)
    assert first > 0
    # 把前沿当成已经折到的位置时，这一段已经折完了 ⇒ 返回 0（没得折）。
    assert compaction.fold_point(messages, first) == 0


# --- 2. 骨架（`digest_messages`）------------------------------------------------

def test_the_skeleton_carries_references_not_tool_bodies():
    """喂给摘要模型的是**骨架**：工具结果只留那句引用，不留正文。

    给了正文这件事就没意义了 —— 折叠一段 20 万 token 的历史需要读 20 万 token 的
    正文，而压缩的目的正是把这段历史变便宜。
    """
    body = "一整份文件的正文\n" * 200
    messages = [
        user("读一下 a.py"),
        tool_call("1"),
        {"role": "tool", "tool_call_id": "c-读一下", "content": "art_x",
         "artifact_id": "art_9f2c"},
        assistant("读完了"),
    ]
    # 真实的 tool 消息里存的是引用；这里再给它一份"引用 + 正文"的形状，
    # 确保摘要那一侧只取第一行。
    messages[2]["content"] = ref.build("art_9f2c", len(body), "read_file") + "\n" + body

    skeleton = compaction.digest_messages(messages)
    assert "art_9f2c" in skeleton
    assert body.strip() not in skeleton
    assert "读一下 a.py" in skeleton          # user 的话逐字保留
    assert "read_file" in skeleton            # 工具名与参数要留下


def test_the_skeleton_drops_the_earliest_messages_and_says_so():
    """超长骨架**从最早那头丢**，并如实标出丢了多少条。

    一句不说地丢，"我漏了一段"和"那一段没发生什么"在摘要模型眼里一模一样。
    """
    messages = [user(f"第 {index} 轮说了一句挺长的话 " + "x" * 200) for index in range(40)]

    skeleton = compaction.digest_messages(messages, limit=2_000)
    assert "更早的" in skeleton and "没有列在这里" in skeleton
    assert "第 39 轮" in skeleton, "最近的那几条必须在"
    assert "第 0 轮" not in skeleton


# --- 3. 状态（块 ↔ `session.metadata`）-----------------------------------------

def test_the_compaction_block_round_trips():
    state = compaction.Compaction(folded_messages=24, summary_id="art_1",
                                  generation=2, updated_at=123.0)
    assert compaction.from_block(compaction.to_block(state)) == state


def test_a_missing_or_broken_block_reads_as_none():
    """读不出来就是 None，**绝不抛** —— 一个坏键不该让整个会话打不开。"""
    assert compaction.load({}) is None
    assert compaction.from_block(None) is None
    assert compaction.from_block({"folded_messages": 0}) is None
    assert compaction.from_block({"folded_messages": "很多"}) is None
    assert compaction.load({"context_compaction": {"folded_messages": 5,
                                                   "summary_id": "art_x"}}) is not None


def test_attention_is_the_identity_before_any_compaction():
    """没压过的会话走的必须是**和这个功能之前完全相同**的那条路。"""
    messages = [user("u"), assistant("a")]
    assert compaction.attention(messages, 0) is messages


# --- 4. 载荷（一次真实的 Agent 回合）-------------------------------------------

def test_a_long_turn_folds_the_history_out_of_the_payload(workdir):
    """**折叠区不再进载荷，那个位置站着一条摘要。**

    这是整个功能的落地处：磁盘上那几十条一条不少，而模型看到的是"摘要 + 尾部原文"。
    """
    agent, session, model, store, manager = build_tight(workdir)
    before = list(session.messages)

    agent.run(session, "接着干")

    payload, tools = model.seen[-1]
    assert tools, "最后一次是普通调用（带工具定义）"
    assert payload[0]["role"] == "system"
    assert compaction.SUMMARY_HEADER in payload[1]["content"]
    assert len(payload) < len(before), "载荷应该明显变短"
    # **原文一条都没少**（原则 3）：这一轮只追加了用户那句话和换模型的说明，
    # 折叠区里的每一条都原样躺在那里。
    assert session.messages[:len(before)] == before
    state = compaction.load(session.metadata)
    assert state is not None and state.active
    assert state.folded_messages > 0


def test_the_folded_tool_results_have_no_seat_in_the_payload(workdir):
    """被折掉的那些轮次**连引用都不再出现**（它们只是被摘要概括了）。"""
    agent, session, model, store, manager = build_tight(workdir)

    agent.run(session, "接着干")
    state = compaction.load(session.metadata)
    payload, _ = model.seen[-1]

    folded_refs = [m["content"] for m in session.messages[:state.folded_messages]
                   if m.get("role") == "tool"]
    assert folded_refs, "测试前提：被折的那一段里真的有工具结果"
    sent = "\n".join(str(m.get("content")) for m in payload)
    for reference in folded_refs:
        assert reference not in sent


def test_the_folded_region_starts_at_a_user_message(workdir):
    """折叠之后载荷里留下的第一段对话**从一条 user 开始**（不是半个回合）。"""
    agent, session, model, store, manager = build_tight(workdir)

    agent.run(session, "接着干")
    state = compaction.load(session.metadata)
    assert session.messages[state.folded_messages]["role"] == "user"


def test_the_summary_is_stored_as_an_artifact(workdir):
    """摘要是**一份 Artifact**（`type=summary`）—— 于是它可追溯、可渲染，
    也解释了"为什么原始历史不用进上下文"。"""
    agent, session, model, store, manager = build_tight(workdir)

    agent.run(session, "接着干")
    state = compaction.load(session.metadata)
    artifact = store.get(state.summary_id)
    assert artifact is not None
    assert artifact.type == "summary"
    assert artifact.source.tool == "compact"
    assert artifact.metadata["messages"] == state.folded_messages
    assert store.content(state.summary_id) == model.summary


def test_the_summary_prompt_carries_the_skeleton_and_the_rule(workdir):
    """摘要那次调用是**一条独立的消息**：系统提示词 + 骨架。

    它不该带工具定义（那是整理，不是干活），也不该混进会话历史。
    """
    agent, session, model, store, manager = build_tight(workdir)

    agent.run(session, "接着干")
    assert model.summary_calls, "应该发生过一次摘要调用"
    sent = model.summary_calls[0]
    assert sent[0]["role"] == "system"
    assert "记忆整理器" in sent[0]["content"]
    assert sent[1]["role"] == "user"
    assert "历史骨架" in sent[1]["content"]
    assert f"第 1 到第 {compaction.load(session.metadata).folded_messages} 条" \
        in sent[1]["content"]


def test_a_second_compaction_folds_the_previous_summary_in(workdir):
    """第二次压缩要在**上一版摘要的基础上**接着写（不能只概括新增的那一段）。

    不带旧摘要的话，第二次压缩会把第一次概括过的历史整个丢掉 —— 而症状是摘要
    看着挺像，只是它是增量的。
    """
    agent, session, model, store, manager = build_tight(workdir)

    agent.run(session, "接着干")
    first = compaction.load(session.metadata)

    # 再灌一段历史，然后手动再压一次。
    long_history(session, store, turns=10)
    result = agent.compact_now(session)

    assert result["status"] == "compacted"
    assert result["total_folded"] > first.folded_messages
    second_prompt = model.summary_calls[-1][1]["content"]
    assert "上一版的摘要" in second_prompt
    assert model.summary in second_prompt


# --- 5. 触发（阈值与节流）------------------------------------------------------

def test_the_threshold_is_a_fraction_of_the_usable_budget():
    """触发线是**预算上限的 90%**，不是裸窗口的 90%（见 `DEFAULT_COMPACT_RATIO`）。

    两层缩进都要看得见：`effective_limit` 是"窗口 - 回答预留 - 余量"，而阈值又是
    它的九成。所以 100k 窗口下的触发线是 86k 而不是 90k —— 那 4k 的差别正是
    "压缩发生在降级快降不动的时候"这句话的量化形式。
    """
    budget = ContextBudget(max_tokens=100_000)
    assert budget.effective_limit == int((100_000 - 4_096) * 0.9)   # 86_313
    assert budget.compact_threshold == int(budget.effective_limit * 0.9)
    assert budget.compact_threshold < budget.effective_limit < 100_000


def test_an_unknown_window_turns_compaction_off(workdir):
    """窗口未知时**一律不压**：连"上下文有多大"都答不出来，谈触发线没有意义。"""
    manager = ContextManager(ArtifactStore(workdir / "artifacts"),
                             budget=ContextBudget(None))
    manager.last_estimate = 999_999
    assert manager.should_compact() is False


def test_nothing_is_compacted_while_the_estimate_is_under_the_line(workdir):
    """**没到线就不压**（阈值是自动触发唯一的判据）。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    long_history(session, store)
    manager.last_estimate = 0
    assert manager.should_compact() is False

    agent.run(session, "接着干")
    assert compaction.load(session.metadata) is None


# --- 6. 失败与边界情形 ---------------------------------------------------------

def test_a_failed_summary_changes_nothing(workdir):
    """摘要那次调用失败 ⇒ **状态、边界、摘要全都不动**，这一轮照旧跑完。

    压缩是可用性优化，不是这一轮的目的 —— 失败时正确的动作是"当作没发生"。
    """
    agent, session, model, store, manager = build_tight(workdir, model=FailingModel())
    before = list(session.messages)

    answer = agent.run(session, "接着干")

    assert answer == "final", "这一轮必须照常出结果"
    assert compaction.load(session.metadata) is None
    assert session.messages[:len(before)] == before
    assert all(artifact.type != "summary" for artifact in store.all())


def test_a_manual_compaction_on_a_short_session_says_nothing_to_do(workdir):
    """没有可折的区间时**如实返回 `nothing`**，不是报错也不是静默。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    result = agent.compact_now(session)

    assert result["status"] == "nothing"
    assert result["folded"] == 0
    assert compaction.load(session.metadata) is None


def test_a_manual_compaction_works_without_running_a_turn(workdir):
    """`/compact` 可以**在一轮之外**压（TUI 那条路就是这样）。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    long_history(session, store)
    before = len(session.messages)

    result = agent.compact_now(session)

    assert result["status"] == "compacted"
    assert result["folded"] > 0
    assert result["summary_id"]
    # `before` / `after` 报的是**这一刻的上下文估算**（同一次测量口径），
    # 而"省了多少"由内容决定 —— 见 `test_folding_is_the_lighter_representation`。
    assert result["before"] > 0 and result["after"] > 0
    assert len(session.messages) == before, "原文一条都不动"


def test_a_manual_compaction_without_context_says_so(workdir):
    """没装 Context 时**如实说"没有上下文管理"** —— 而不是假装压了。"""
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="读文件", risk=RiskLevel.LOW,
        args_model=ListFilesArgs, handler=lambda **kw: "", parallel_safe=True,
    ))
    agent = Agent(SummaryModel(), registry, PermissionPolicy({RiskLevel.LOW}))
    session = Session.new("s")
    long_history(session, ArtifactStore(workdir / "unused"))
    result = agent.compact_now(session)

    assert result["status"] == "nothing"
    assert result["total_folded"] == 0


def test_folding_is_the_lighter_representation(workdir):
    """**历史稠密时，折叠之后载荷真的更小。**

    这条不是废话 —— 它是"压缩"这个词唯一能被证伪的地方。用两个极端的摘要来量同
    一段历史：一段只概括成一句话、一段复述得几乎一样长。历史稠密（每个回合几千
    字符的正文）时前者必然更省；而后者会露出这个功能的**真实代价** —— 摘要本身
    也要按 token 付钱，所以"压了反而更贵"在历史很短、摘要很长时是可能的（那正是
    `/context` 要报 `before` / `after` 两个数的理由）。
    """
    agent, session, model, store, manager = build_tight(
        workdir, model=SummaryModel(summary="一句话：读了 12 个文件，没有发现问题。"),
    )
    before = agent._measure(session)
    result = agent.compact_now(session)

    assert result["status"] == "compacted"
    assert result["before"] == before
    assert result["after"] < before, "历史稠密时压缩必须真的把载荷变小"


def test_compaction_is_idempotent_when_there_is_nothing_new(workdir):
    """连着压两次：第二次**什么都不折**（前沿没动就白跑一次模型）。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    long_history(session, store)
    first = agent.compact_now(session)
    assert first["status"] == "compacted"

    second = agent.compact_now(session)
    assert second["status"] == "nothing"
    assert second["folded"] == 0
    assert second["total_folded"] == first["total_folded"]


# --- 7. 恢复会话 ----------------------------------------------------------------

def test_compaction_survives_a_resume(workdir):
    """压缩状态住在 `session.metadata` 里，**跨进程活着**：恢复会话之后照旧折叠。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    long_history(session, store)
    agent.compact_now(session)
    state = compaction.load(session.metadata)

    sessions = JsonSessionStore(workdir / "sessions")
    sessions.save(session)
    resumed = sessions.load("s")

    assert compaction.load(resumed.metadata) == state
    # 恢复出来的会话接上同一个 ArtifactStore 时，摘要照旧取得到。
    assert compaction.summary_text(store, compaction.load(resumed.metadata)) \
        == model.summary


def test_the_session_file_keeps_every_message_after_compaction(workdir):
    """**磁盘上一条都不少。** 压缩改的是"发什么"，不是"存什么" —— 会话文件是档案库。"""
    agent, session, model, store, manager = build(workdir, window=1_000_000)
    long_history(session, store)
    before = len(session.messages)

    agent.compact_now(session)

    sessions = JsonSessionStore(workdir / "sessions")
    sessions.save(session)
    messages = [record for record in sessions.read("s") if record.get("t") == "msg"]
    assert len(messages) == before
    assert len(sessions.load("s").messages) == before


# --- 8. 命令那一层的文案 -------------------------------------------------------

def test_the_compaction_note_covers_every_status():
    """五种结果**各有一句话**，而且都不是空的。

    合成一句"压缩完成"会把后四种（没得压 / 正在压 / 没装 Context / 出错）全掩盖掉
    —— 而那正是这个功能最不该有的失败形态。
    """
    from agent_runtime.frontends.tui import view_state

    for status in ("compacted", "nothing", "busy", "no_context"):
        note, _role = view_state.compaction_note({"status": status, "folded": 3,
                                                  "total_folded": 9, "messages": 20,
                                                  "summary_chars": 120,
                                                  "before": 1000, "after": 400,
                                                  "duration_ms": 2400})
        assert note.strip()
    compacted, _ = view_state.compaction_note({
        "status": "compacted", "folded": 3, "total_folded": 9, "messages": 20,
        "summary_chars": 120, "before": 1000, "after": 400, "duration_ms": 2400,
    })
    assert "3" in compacted and "9" in compacted


def test_the_context_screen_renders_with_and_without_compaction():
    """`/context` 那一屏**没压过时也要能画**（而且不能显示一排零）。"""
    from agent_runtime.frontends.tui import view_state

    idle = view_state.render_context({"context": {
        "active": False, "folded": 0, "messages": 3, "summary_id": "",
        "generation": 0, "summary_chars": 0, "window": 200_000,
        "context": {"artifacts": 2, "items": 2, "open": 2, "removed": 0,
                    "pinned": 1, "estimated_tokens": 1000,
                    "limit_tokens": 176_331, "compact_threshold": 158_697,
                    "degraded": 0, "version": 1},
    }})
    text = "\n".join(str(line) for line in idle)
    assert i18n.t("context.not_folded") in text
    assert "0 条" not in text

    active = view_state.render_context({"context": {
        "active": True, "folded": 24, "messages": 61, "summary_id": "art_9f2c",
        "generation": 2, "summary_chars": 1800, "window": 200_000,
        "context": {"artifacts": 31, "items": 18, "open": 18, "removed": 0,
                    "pinned": 3, "estimated_tokens": 151_000,
                    "limit_tokens": 176_331, "compact_threshold": 158_697,
                    "degraded": 0, "version": 12},
    }})
    text = "\n".join(str(line) for line in active)
    assert "24/61" in text
    assert "art_9f2c" in text


def test_the_audit_records_the_compaction(workdir):
    """每次压缩记一条 `context_compacted` —— 和 `context_degraded` 同一档。

    没有它的话，"这一轮的 token 为什么突然降了"在审计里查不出原因。
    """
    events: list[dict] = []
    agent, session, model, store, manager = build(
        workdir, window=1_000_000, event_sink=events.append,
    )
    long_history(session, store)

    agent.compact_now(session)

    kinds = [item["kind"] for item in events]
    assert "context_compacted" in kinds
    record = next(item for item in events if item["kind"] == "context_compacted")
    assert record["folded"] > 0
    assert record["folded_total"] == compaction.load(session.metadata).folded_messages
    assert record["summary_chars"] > 0
    assert record["before"] > 0 and record["after"] > 0


# --- 9. 协议那一侧（TUI 按 `/compact` 走的那条路）-------------------------------

class _RecordingTransport:
    """一个把出站消息收下来的假传输（不碰真的 stdin/stdout）。

    **必须继承 `StdioTransport`**：`ProtocolServer` 建它的时候要 `Transport` 那个
    协议的形状，而这里只需要 `send` 有地方可写。
    """

    def __init__(self) -> None:
        from agent_runtime.protocol.transport_stdio import StdioTransport

        self.sent: list[dict] = []
        self._base = StdioTransport

    def send(self, message: dict) -> None:
        self.sent.append(message)

    def recv(self):
        return iter(())

    def close(self) -> None:
        pass

    def kinds(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("kind") == kind]


def test_the_protocol_path_answers_compact_and_context(fake_openai, workdir):
    """`/compact` 与 `/context` 走协议那一条路，回包的形状就是它该有的样子。

    ## 为什么这条值得单独测

    TUI 和 runtime 是两个进程，中间只有协议 —— 而 `_handle_compact` 是**唯一**
    会为了一个 `/` 命令去调模型的地方（那条线程、那份异步答复都是它的事）。
    单元测试测不到它：它们直接调 `Agent.compact_now`。

    这里刻意**用真的 runtime**（假网关在 `fake_openai` 里）：那条路上有装配、
    有 Context、有审计 sink，而"命令发了但什么都没回"这类问题只有在这条路上才现形。
    """
    import threading
    import time

    from agent_runtime.protocol import messages
    from agent_runtime.protocol.channels import ProtocolServer
    from agent_runtime.runtime.channels import cli_channels
    from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
    from agent_runtime.runtime.config import McpConfig, PermissionConfig, WebConfig
    from agent_runtime.state import Session
    from fakes import model_registry

    base, _scripts, _calls = fake_openai
    booted = boot()
    session = Session.new("compact-e2e")
    # 灌一段够折的历史。**工具结果走真的 ArtifactStore**（和 runtime 装配出来的
    # 是同一个目录，见 `paths.session_artifacts_dir`）—— 手写假引用的话，渲染
    # 那一侧查不到 artifact，这条测试测的就不是那条真路。
    from agent_runtime import paths

    store = ArtifactStore(paths.session_artifacts_dir(session.session_id))
    long_history(session, store)

    transport = _RecordingTransport()
    server = ProtocolServer(transport)
    runtime = open_runtime(
        booted=booted, session_id="compact-e2e", session=session,
        channels=cli_channels(), resumed=False,
        # **窗口写小**：`/context` 那两格（额度与阈值）只有在模型知道自己的窗口时
        # 才有值（窗口未知时预算整体关着，见 `ContextBudget.enabled`）。
        catalog_config=model_registry(base_url=base, model="fake", window=8_000),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(),
    )
    try:
        server.attach(runtime)

        # `/context`：只读、同步，回一条 `ui(kind=context)`。
        #
        # `v` 那一格不能少 —— `_dispatch` 第一件事就是校信封版本，对不上直接
        # 返回 False（"收摊"）。真客户端（`ProtocolClient.send`）每一条都带它。
        transport.sent.clear()
        assert server._dispatch({"v": messages.VERSION,
                                 "t": messages.IN_CONTEXT}) is True
        context = transport.kinds(messages.UI_CONTEXT)[-1]["context"]
        assert context["active"] is False
        assert context["context"]["limit_tokens"] > 0

        # `/compact`：**异步**（摘要要跑一次模型往返），所以这里等那条线程。
        transport.sent.clear()
        assert server._dispatch({"v": messages.VERSION,
                                 "t": messages.IN_COMPACT}) is True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not transport.kinds(
            messages.UI_COMPACTED
        ):
            threading.Event().wait(0.05)

        replies = transport.kinds(messages.UI_COMPACTED)
        assert replies, "`/compact` 必须回一条 ui(kind=compacted)"
        result = replies[-1]["compaction"]
        assert result["status"] == "compacted"
        assert result["folded"] > 0
        assert result["summary_id"]
        # 同一条回包里**附一份账**（否则用户还得再打一次 `/context`）。
        assert replies[-1]["context"]["active"] is True
        # 而且补了一份面板快照（消息数那几格跟着变了）。
        assert transport.kinds(messages.UI_STATE)
    finally:
        runtime.close()
