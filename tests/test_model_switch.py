"""`/model` 与模型目录：**一个名字只有一处事实**。

这一组测试盯的是三件事，它们的共同点是"错了也不会报错，只会静默地按另一个模型跑"：

  1. 目录（`state/model.py`）和 `CONTEXT_WINDOWS`（`runtime/config.py`）不能各写一份；
  2. 会话级选择要跟着 `session.metadata` 走 —— 恢复会话之后还是你选的那个；
  3. 换模型**不在本轮生效**，而是在下一轮开头留一句话。
"""

import json

import pytest

from agent_runtime.runtime.config import CONTEXT_WINDOWS, DEFAULT_MODEL
from agent_runtime.state import model as model_state
from agent_runtime.state.session import Session


# --- 目录与窗口表 ---------------------------------------------------------------

def test_the_window_table_is_derived_from_the_catalog():
    """`CONTEXT_WINDOWS` 和目录**必须同源**。

    它们各写一份的后果是静默的：往目录里加一个模型（`/model` 立刻列出它），而窗口表
    没跟上，于是"选了它之后状态栏不报占比" —— 两处都不会报错，只是那个百分比消失了。
    """
    for item in model_state.MODEL_CATALOG:
        assert CONTEXT_WINDOWS[item.id] == item.window
    assert CONTEXT_WINDOWS[DEFAULT_MODEL] == model_state.get(DEFAULT_MODEL).window


def test_legacy_names_are_recognised_but_not_offered():
    """旧模型名**认，但不列进 `/model` 的清单**。

    官方明确说过那两个旧名字对应的模型已下线、请求由 V4.1-Flash 提供服务。所以：
      * 认它们 —— `DEEPSEEK_MODEL` 里可能就写着它们，而"昨天配的名字今天不能用"
        是我们不该制造的意外；
      * 不列它们 —— 摆出两个效果一样、价钱也一样的选项，是在骗选的人。
    """
    ids = {item.id for item in model_state.MODEL_CATALOG}
    for alias in model_state.ALIASES:
        assert alias not in ids
        # 认得出来，而且折算到一个真正的目录项上。
        assert model_state.get(alias) is not None
        assert model_state.get(alias).id in ids
        # 窗口照旧报得出来（否则旧名字的会话会突然没有分母）。
        assert CONTEXT_WINDOWS[alias] == model_state.get(alias).window


def test_an_unknown_model_name_has_no_window():
    """认不出来的名字**不给窗口**（返回 None，而不是猜一个）。

    这个项目可以指向自建网关（`DEEPSEEK_BASE_URL`），所以"不认识"是正常状态。
    错的百分比比没有百分比更坏 —— 它会被当成真的。
    """
    assert model_state.get("gpt-9") is None
    assert model_state.context_window("gpt-9") is None


def test_the_catalog_rows_are_plain_data():
    """`catalog_rows()` 的结果要能直接进 JSON（协议里发它）。"""
    rows = model_state.catalog_rows()
    assert rows and all(set(row) == {"id", "label", "window", "summary", "note"}
                        for row in rows)
    json.dumps(rows, ensure_ascii=False)


# --- 会话级选择 -----------------------------------------------------------------

def test_a_session_without_a_selection_falls_back_to_the_configured_model():
    holder = model_state.SessionModel.restore({}, fallback="deepseek-flash")
    assert holder.selected == "deepseek-flash"
    assert holder.last_used == ""
    assert holder.selection is None


def test_selecting_writes_into_metadata_and_survives_a_roundtrip(workdir):
    """选完要能跟着会话存下来 —— 恢复之后还是它。

    这里走的是**真的落盘往返**（`JsonSessionStore`），不是"字典里有个键"：会话说到底
    是磁盘上那个 JSON，而 `metadata` 里塞一个不可序列化的东西会在保存时才炸。

    **目录用 `workdir`，不用 `tempfile`**：系统临时目录在受限环境里连**清理**都会被拒
    （实测：`TemporaryDirectory.__exit__` 里的 `chmod` 抛 WinError 5），而那个失败
    发生在测试体之后 —— 断言全绿、用例却红，读起来像"落盘坏了"。`workdir` 是 conftest
    里为同一件事准备的（`tests/_tmp` 下自建自删）。
    """
    from agent_runtime.state.store import JsonSessionStore

    session = Session.new("s1", workspace=None)
    holder = model_state.SessionModel.restore(session.metadata, fallback="deepseek-flash")
    holder.select("deepseek-v4-pro", now=1234.0)

    store = JsonSessionStore(workdir)
    store.save(session)
    back = store.load("s1")

    restored = model_state.SessionModel.restore(back.metadata, fallback="deepseek-flash")
    assert restored.selected == "deepseek-v4-pro"
    assert restored.selected_since == 1234.0
    # 只是"想用"，还没用过 —— `/status` 要分得开这两件事。
    assert restored.last_used == ""


def test_a_broken_selection_block_is_ignored_not_fatal():
    """坏数据当"没选过"（回到配置里那个），**绝不抛**。

    旧会话文件里没有这个键，而手改坏的键不该让整个会话打不开 —— 和
    `agents_md.from_block` 立的是同一条规矩。
    """
    assert model_state.load({"model_selection": "deepseek-pro"}) is None
    assert model_state.load({"model_selection": {}}) is None
    assert model_state.load({"model_selection": {"model": ""}}) is None
    holder = model_state.SessionModel.restore(
        {"model_selection": {"model": "deepseek-v4-pro", "since": "昨天"}},
        fallback="deepseek-flash",
    )
    assert holder.selected == "deepseek-v4-pro"
    assert holder.selected_since == 0.0


def test_changing_back_before_any_answer_needs_no_notice():
    """换了又换回来：**不留那句话** —— 中间那次没产生任何回答，说"换过"是假的。

    判据是 `selected != last_used`（而不是"刚刚调过 /model"），所以这件事自然成立。
    """
    holder = model_state.SessionModel.restore({}, fallback="deepseek-flash")
    assert holder.notice_needed() is False       # 全新会话：没有"换"这回事
    holder.record_use()                          # 第一轮跑过（用的就是它）
    holder.select("deepseek-v4-pro")
    assert holder.notice_needed() is True
    holder.select("deepseek-flash")
    assert holder.notice_needed() is False


def test_a_brand_new_session_never_announces_a_model_change():
    """**新会话的第一次请求不该带那句"模型换了"。**

    没有这条判据的话，每一份新会话的第一轮历史里都会多一句 `[model changed: …]`
    （"从 deepseek-flash 换成 deepseek-flash"），而它读起来像系统提示词的一部分 ——
    一个每个会话都出现、又从不携带信息的东西，只会在真正需要它的那一次被忽略掉。
    """
    holder = model_state.SessionModel.restore({}, fallback="deepseek-flash")
    assert holder.notice_needed() is False
    holder.record_use()
    assert holder.notice_needed() is False


def test_record_use_clears_the_pending_notice():
    holder = model_state.SessionModel.restore({}, fallback="deepseek-flash")
    holder.select("deepseek-v4-pro")
    holder.record_use()
    assert holder.last_used == "deepseek-v4-pro"
    assert holder.notice_needed() is False


def test_the_notice_says_both_names():
    """那句话要同时说清"上面是谁写的"和"从这里开始是谁"。"""
    notice = model_state.change_notice("deepseek-flash", "deepseek-v4-pro")
    assert notice["role"] == "user"
    assert "deepseek-flash" in notice["content"]
    assert "deepseek-v4-pro" in notice["content"]
    # 全新会话（此前没有任何回答）是另一种措辞 —— 说"上面那些轮次由 X 生成"会是假的。
    first = model_state.change_notice("", "deepseek-v4-pro")
    assert "deepseek-v4-pro" in first["content"]
    assert "上面" not in first["content"]


# --- Agent 那一侧：换模型与"换过"那句话 -----------------------------------------

def _session_with_model(model: str = "deepseek-flash") -> Session:
    """一个**已经跑过至少一轮**的会话（`last_used` 有值）。

    这一格是"换模型要留一句话"那条判据的一半（另一半是"选中的 ≠ 用过的"）。新会话
    的 `last_used` 是空的，而那时候**不该**有那句话 —— 见
    `test_a_brand_new_session_never_announces_a_model_change`。
    """
    session = Session.new("s-agent", workspace=None)
    session.metadata[model_state.SELECTION_KEY] = {
        "version": 1, "model": model, "since": 0.0,
    }
    session.metadata[model_state.LAST_USED_KEY] = model
    return session


class _FakeAdapter:
    """最小适配器：认识 `model` / `switch_model`，并且记下每一次请求的模型名。"""

    def __init__(self, model: str = "deepseek-flash") -> None:
        self.model = model
        self.base_url = "http://x"
        self.seen: list[str] = []

    def switch_model(self, name: str) -> None:
        self.model = name

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        from agent_runtime.models.types import ModelResponse

        self.seen.append(self.model)
        return ModelResponse(content="好")


class _StubbornAdapter(_FakeAdapter):
    """不支持中途换模型的适配器（默认实现会抛 NotImplementedError）。"""

    def switch_model(self, name: str) -> None:
        from agent_runtime.models.base import ChatModel

        ChatModel.switch_model(self, name)


def _agent(model, session, **kwargs):
    from agent_runtime.agents.agent import Agent
    from agent_runtime.security.policy import PermissionPolicy
    from agent_runtime.tools.tool import ToolRegistry

    return Agent(
        model, ToolRegistry(), PermissionPolicy(),
        session_model=model_state.SessionModel.restore(
            session.metadata, fallback="deepseek-flash"),
        **kwargs,
    )


def test_a_model_change_lands_in_the_history_before_the_next_turn():
    """换完模型跑下一轮：**那句话在历史里，而且排在用户那句话前面**。

    顺序是刻意的："从这个点开始用谁"的那个点，就是这一轮。
    """
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    agent.switch_model("deepseek-v4-pro")

    agent.run(session, "第二个问题", max_steps=1)

    roles = [m["role"] for m in session.messages]
    contents = [str(m.get("content") or "") for m in session.messages]
    assert roles[1:4] == ["user", "user", "assistant"]
    assert "model changed" in contents[1]
    assert "deepseek-flash" in contents[1] and "deepseek-v4-pro" in contents[1]
    assert contents[2] == "第二个问题"
    # 请求里带的是**新**模型名。
    assert adapter.seen == ["deepseek-v4-pro"]


def test_the_notice_is_written_exactly_once():
    """第二轮回话不该再留一句 —— 判据是 `last_used`，它已经被记上了。"""
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    agent.switch_model("deepseek-v4-pro")
    agent.run(session, "一", max_steps=1)
    agent.run(session, "二", max_steps=1)
    notices = [m for m in session.messages
               if "model changed" in str(m.get("content") or "")]
    assert len(notices) == 1


def test_switching_mid_turn_does_not_split_a_turn_across_two_models():
    """**一轮跑到一半换模型：本轮不受影响，那句话留到下一轮。**

    这是"看不到就出错"的那一类：同一个回合由两个模型拼出来的话，审计里两条
    model_call 长得一模一样（同一个 run_id），而历史里那段话是谁写的就没有答案了。

    做法：让适配器在第一次请求返回**之前**去调 `agent.switch_model(...)`（那正是
    "用户在一轮跑着的时候按了 `/model`"的时刻）。
    """
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)

    original = adapter.complete

    def complete_then_switch(messages, tools=None, on_delta=None,
                             on_attempt_started=None):
        response = original(messages, tools, on_delta, on_attempt_started)
        model_state.SessionModel.restore(
            session.metadata, fallback="deepseek-flash").select("deepseek-v4-pro")
        agent.switch_model("deepseek-v4-pro")
        return response

    adapter.complete = complete_then_switch

    agent.run(session, "第一轮", max_steps=1)

    # 本轮用的是**旧**模型，而且历史里**没有**那句话（它对本轮是假的）。
    assert adapter.seen == ["deepseek-flash"]
    assert not [m for m in session.messages
                if "model changed" in str(m.get("content") or "")]

    # 下一轮：先留那句话，再请求 —— 请求用的是新模型。
    adapter.complete = original
    agent.run(session, "第二轮", max_steps=1)
    assert adapter.seen == ["deepseek-flash", "deepseek-v4-pro"]
    notices = [m for m in session.messages
               if "model changed" in str(m.get("content") or "")]
    assert len(notices) == 1


def test_an_adapter_that_cannot_switch_says_so():
    """适配器不支持中途换模型时，`switch_model` 返回 False（而不是抛或者假装成功）。

    这把"没换成"变成一个调用方必须处理的返回值 —— 而调用方（`Runtime.select_model`）
    把它变成一句"没换成"的提示。假装成功的话，界面会显示新模型而请求还发给旧的。
    """
    session = _session_with_model("deepseek-flash")
    agent = _agent(_StubbornAdapter("deepseek-flash"), session)
    assert agent.switch_model("deepseek-v4-pro") is False
    assert agent.model_name == "deepseek-flash"


def test_model_name_reads_the_adapter_not_a_copy():
    """`Agent.model_name` **从适配器上读** —— 存一份副本就会在某条路上分家。"""
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    assert agent.model_name == "deepseek-flash"
    adapter.model = "谁改的"
    assert agent.model_name == "谁改的"


# --- Runtime.select_model：三道检查 ---------------------------------------------

def test_select_model_rejects_a_name_outside_the_catalog():
    """目录外的名字**拒绝**，并给出处。

    接受它等于让 `/model` 那份清单变成一句谎话，而"选了一个它没列出来的模型"这件事
    没有任何地方会报（`init.model` 会显示它，而清单里没有它）。
    """
    with _runtime() as runtime:
        ok, message = runtime.select_model("gpt-9")
        assert ok is False
        assert "目录里没有这个模型" in message
        assert runtime.current_model == "deepseek-flash"


def test_select_model_accepts_a_catalog_name_and_reports_the_old_one():
    with _runtime() as runtime:
        ok, message = runtime.select_model("deepseek-v4-pro")
        assert ok is True
        assert "deepseek-v4-pro" in message and "deepseek-flash" in message
        assert runtime.current_model == "deepseek-v4-pro"
        # 分母跟着换（它是派生的，不是存下来的字段）。
        assert runtime.context_tokens == CONTEXT_WINDOWS["deepseek-v4-pro"]


def test_select_model_is_idempotent():
    with _runtime() as runtime:
        runtime.select_model("deepseek-v4-pro")
        ok, message = runtime.select_model("deepseek-v4-pro")
        assert ok is True
        assert "已经是" in message


def test_select_model_refuses_when_the_endpoint_does_not_match():
    """适配器上的 base_url 和配置对不上时**拒绝换**，而不是把请求发到没配密钥的地址上。

    这一档里没有 "provider" 这个概念（一个 base_url、一把密钥），所以"换到另一个网关
    的模型"没法表达 —— 能做的是别让它悄悄发生。
    """
    with _runtime() as runtime:
        runtime.agent.model.base_url = "https://example.invalid"
        ok, message = runtime.select_model("deepseek-v4-pro")
        assert ok is False
        assert "对不上" in message
        assert runtime.current_model == "deepseek-flash"


def test_the_selection_is_written_to_disk_immediately(workdir, monkeypatch):
    """`/model` 之后**一句话都不说就退出**，恢复会话时那个选择还在。

    这条钉的是"立刻落盘"那一步：等下一个检查点的话，最自然的用法之一（进去、换模型、
    退出）会丢掉这次选择 —— 而恢复时我们会说"你选的是 flash"，那和刚给出过的承诺相反。
    """
    from agent_runtime.runtime.channels import cli_channels
    from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
    from agent_runtime.runtime.config import (
        McpConfig, ModelConfig, PermissionConfig, WebConfig,
    )

    monkeypatch.setattr("agent_runtime.runtime.composition.project_dir",
                        lambda: workdir)
    booted = boot()
    session_id, session, resumed = resolve_session(booted.store, None)
    runtime = open_runtime(
        booted=booted, session_id=session_id, session=session,
        channels=cli_channels(), resumed=resumed,
        model_config=ModelConfig(api_key="sk-x", base_url="http://127.0.0.1:1",
                                 model="deepseek-flash"),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(),
    )
    try:
        assert runtime.select_model("deepseek-v4-pro")[0] is True
        # 磁盘上那一份已经是新的了（**没有任何回合跑过**）。
        reloaded = booted.store.load(session_id)
    finally:
        runtime.close()

    restored = model_state.SessionModel.restore(
        reloaded.metadata, fallback="deepseek-flash")
    assert restored.selected == "deepseek-v4-pro"


def test_the_selection_is_per_session_and_survives_a_resume():
    """`/model` 换的是**这个会话**，恢复它时还是那个（和任务列表、技能同一条路）。

    这条同时钉住了"新会话用回配置里那个"：会话级选择住在 `session.metadata` 里，
    所以另开一个会话拿到的就是干净的一份。
    """
    with _runtime() as runtime:
        runtime.select_model("deepseek-v4-pro")
        assert runtime.agent.session_model.selected == "deepseek-v4-pro"

    with _runtime() as other:
        assert other.current_model == "deepseek-flash"
        assert other.agent.session_model.selection is None


def _runtime():
    """一个真的 Runtime（假密钥、假网关地址，不发任何请求）。

    **调用方必须 `runtime.close()`**（`httpx.Client` 是进程级资源）—— 所以这里不返回
    "已经替你收好"的东西，而是让测试用 `with` 收。给 Runtime 打补丁换掉 `close` 是
    不行的：它是 frozen + slots 的 dataclass，`monkeypatch.setattr` 会撞上
    `super(type, obj)` 那个 TypeError。
    """
    from agent_runtime.runtime.channels import cli_channels
    from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
    from agent_runtime.runtime.config import (
        McpConfig, ModelConfig, PermissionConfig, WebConfig,
    )

    booted = boot()
    session_id, session, resumed = resolve_session(booted.store, None)
    return open_runtime(
        booted=booted, session_id=session_id, session=session,
        channels=cli_channels(), resumed=resumed,
        model_config=ModelConfig(api_key="sk-x", base_url="http://127.0.0.1:1",
                                 model="deepseek-flash"),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(),
    )
