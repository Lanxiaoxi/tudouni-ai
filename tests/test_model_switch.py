"""`/model`（含跨路由）与思考设置：**会话级选择怎么存、什么时候生效**。

这一组测试盯的是三件事，它们的共同点是"错了也不会报错，只会静默地按另一个模型跑"：

  1. 会话级选择要跟着 `session.metadata` 走 —— 恢复会话之后还是你选的那个；
  2. 换模型**不在本轮生效**，而是在下一轮开头留一句话；
  3. 换**路由**（不只换模型名）时请求真的发到另一台上 —— 这是多 provider 的全部意义。

目录本身（`state/catalog.py`）和思考那个 domain（`state/reasoning.py`）在
`tests/test_catalog.py` 里测。
"""

import json
from contextlib import contextmanager

import pytest

from agent_runtime.runtime.config import (
    CONTEXT_WINDOWS,
    McpConfig,
    ModelConfig,
    PermissionConfig,
    WebConfig,
)
from agent_runtime.state import catalog
from agent_runtime.state import model as model_state
from agent_runtime.state import reasoning
from agent_runtime.state.session import Session


# --- 会话级选择 -----------------------------------------------------------------

def test_a_session_without_a_selection_falls_back_to_the_configured_model():
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    assert holder.selected == "deepseek-flash"
    assert holder.selected_provider == "deepseek"
    assert holder.last_used == ""
    assert holder.selection is None
    # 没有会话级选择时，思考那两个旋钮用 domain 的默认值（**开 + high**）。
    assert holder.thinking is True
    assert holder.effort == reasoning.DEFAULT_EFFORT


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
    holder.select_route(provider="deepseek", model="deepseek-v4-pro", now=1234.0)

    store = JsonSessionStore(workdir)
    store.save(session)
    back = store.load("s1")

    restored = model_state.SessionModel.restore(back.metadata, fallback="deepseek-flash")
    assert restored.selected == "deepseek-v4-pro"
    assert restored.selected_provider == "deepseek"
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


def test_an_old_selection_block_gets_the_default_thinking_settings():
    """**老会话文件里没有 `thinking` / `effort`** —— 缺字段是"用默认"，不是"关掉"。

    读成 `thinking=False` 会让每一个旧会话在恢复之后突然不再思考，而那种变化在界面上
    完全看不出来（只是答案变差了、变便宜了）。
    """
    holder = model_state.SessionModel.restore(
        {"model_selection": {"model": "deepseek-v4-pro"}}, fallback="deepseek-flash")
    assert holder.thinking is True
    assert holder.effort == reasoning.DEFAULT_EFFORT


def test_changing_back_before_any_answer_needs_no_notice():
    """换了又换回来：**不留那句话** —— 中间那次没产生任何回答，说"换过"是假的。

    判据是"选中的 ≠ 上一轮用过的"（而不是"刚刚调过 /model"），所以这件事自然成立。
    """
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    assert holder.notice_needed() is False       # 全新会话：没有"换"这回事
    holder.record_use()                          # 第一轮跑过（用的就是它）
    holder.select_route(provider="deepseek", model="deepseek-v4-pro")
    assert holder.notice_needed() is True
    holder.select_route(provider="deepseek", model="deepseek-flash")
    assert holder.notice_needed() is False


def test_a_brand_new_session_never_announces_a_model_change():
    """**新会话的第一次请求不该带那句"模型换了"。**

    没有这条判据的话，每一份新会话的第一轮历史里都会多一句 `[model changed: …]`
    （"从 deepseek-flash 换成 deepseek-flash"），而它读起来像系统提示词的一部分 ——
    一个每个会话都出现、又从不携带信息的东西，只会在真正需要它的那一次被忽略掉。
    """
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    assert holder.notice_needed() is False
    holder.record_use()
    assert holder.notice_needed() is False


def test_thinking_settings_do_not_count_as_a_model_change():
    """**改思考设置不算"换了模型"** —— 不改 route 就不该往历史里插那句话。

    插了的话，那句话说的是"上面那些轮次由 A 生成" —— 而 A 还是 A，那是假话。
    """
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    holder.record_use()
    holder.select_thinking(False)
    holder.select_effort("max")
    assert holder.notice_needed() is False
    assert holder.thinking is False and holder.effort == "max"


def test_record_use_clears_the_pending_notice():
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    holder.select_route(provider="deepseek", model="deepseek-v4-pro")
    holder.record_use()
    assert holder.last_used == "deepseek/deepseek-v4-pro"
    assert holder.notice_needed() is False


def test_the_route_name_carries_the_provider():
    """这句话里的名字是 `provider/model` —— 两条路由有同名模型时，光写模型名分不出
    "上面那些轮次是在哪跑的"。"""
    holder = model_state.SessionModel.restore(
        {}, fallback="deepseek-flash", fallback_provider="deepseek")
    assert holder.route_name() == "deepseek/deepseek-flash"


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
    """最小适配器：认识 `model` / `switch_model` / `install`，并记下每次请求的路线。

    **它同时记 provider**（`install` 收到什么就记什么）：跨路由换模型是这个版本的核心
    行为，而"换了但还发到老地址"只有把两样都记下来才验得出来。
    """

    def __init__(self, model: str = "deepseek-flash", provider: str = "deepseek") -> None:
        self.model = model
        self.provider = provider
        self.base_url = "http://x"
        self._api_key = "sk-x"
        self.thinking = reasoning.DEFAULT_THINKING
        self.effort = reasoning.DEFAULT_EFFORT
        self.seen: list[str] = []

    def switch_model(self, name: str) -> None:
        self.model = name

    def install(self, *, api_key: str, base_url: str, model: str,
                provider: str = "", thinking=None, effort=None) -> None:
        self._api_key, self.base_url = api_key, base_url
        self.model, self.provider = model, provider

    def set_reasoning(self, *, thinking: bool, effort: str) -> None:
        self.thinking, self.effort = thinking, effort

    def complete(self, messages, tools=None, on_delta=None, on_attempt_started=None):
        from agent_runtime.models.types import ModelResponse

        self.seen.append(f"{self.provider}/{self.model}")
        return ModelResponse(content="好")


class _StubbornAdapter(_FakeAdapter):
    """不支持中途换模型的适配器（默认实现会抛 NotImplementedError）。"""

    def switch_model(self, name: str) -> None:
        from agent_runtime.models.base import ChatModel

        ChatModel.switch_model(self, name)

    def install(self, **kwargs) -> None:
        from agent_runtime.models.base import ChatModel

        ChatModel.install(self, **kwargs)


def _agent(model, session, **kwargs):
    from agent_runtime.agents.agent import Agent
    from agent_runtime.security.policy import PermissionPolicy
    from agent_runtime.tools.tool import ToolRegistry

    return Agent(
        model, ToolRegistry(), PermissionPolicy(),
        session_model=model_state.SessionModel.restore(
            session.metadata, fallback="deepseek-flash",
            fallback_provider="deepseek"),
        **kwargs,
    )


def test_a_model_change_lands_in_the_history_before_the_next_turn():
    """换完模型跑下一轮：**那句话在历史里，而且排在用户那句话前面**。

    顺序是刻意的："从这个点开始用谁"的那个点，就是这一轮。
    """
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    agent.switch_model("deepseek-v4-pro", provider="deepseek")

    agent.run(session, "第二个问题", max_steps=1)

    roles = [m["role"] for m in session.messages]
    contents = [str(m.get("content") or "") for m in session.messages]
    assert roles[1:4] == ["user", "user", "assistant"]
    assert "model changed" in contents[1]
    assert "deepseek-flash" in contents[1] and "deepseek-v4-pro" in contents[1]
    assert contents[2] == "第二个问题"
    # 请求里带的是**新**模型名。
    assert adapter.seen == ["deepseek/deepseek-v4-pro"]


def test_switching_to_another_provider_reinstalls_the_route():
    """换到**另一条路由**要重造客户端（密钥、端点都变了），而且是立刻生效的。

    这条是多 provider 的核心：`switch_model` 收到一个不同的 provider 时必须走
    `install`（它会重造 SDK 客户端），而不是只改一个字段 —— 只改字段的症状是
    "界面写着换了、密钥还是旧那把、地址还是旧那个"。
    """
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash", "deepseek")
    agent = _agent(adapter, session)

    assert agent.switch_model("m1", provider="acme", api_key="sk-acme",
                              base_url="https://acme.example/v1") is True
    assert (agent.model_provider, agent.model_name) == ("acme", "m1")
    assert adapter.base_url == "https://acme.example/v1"
    assert adapter._api_key == "sk-acme"
    # 会话里记的也是那条路由 —— 恢复会话时它得答得出来。
    assert agent.session_model.route_name() == "acme/m1"


def test_the_thinking_switch_and_effort_land_on_the_adapter_and_the_session():
    """两个旋钮同时**改适配器**（下一次请求用它）和**记进会话**（恢复时还是它）。

    分给两个方法、让调用方记得两步都走，是一条迟早会漏的约定 —— 漏掉"记进会话"的
    症状是"恢复会话之后它又开始思考了"，而那笔钱已经在花了。
    """
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)

    assert agent.set_reasoning(thinking=False, effort=None) is True
    assert adapter.thinking is False
    assert agent.thinking is False
    assert agent.session_model.thinking is False
    # 强度**没被动过** —— 关掉思考不该清掉它。
    assert agent.effort == reasoning.DEFAULT_EFFORT

    assert agent.set_reasoning(thinking=None, effort="max") is True
    assert adapter.effort == "max"
    assert agent.session_model.effort == "max"
    # 而且关着的时候强度照样记着：`/thinking on` 之后回来还是它。
    assert agent.session_model.thinking is False
    assert agent.session_model.effort == "max"


def test_the_notice_is_written_exactly_once():
    """第二轮回话不该再留一句 —— 判据是 `last_used`，它已经被记上了。"""
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    agent.switch_model("deepseek-v4-pro", provider="deepseek")
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
            session.metadata, fallback="deepseek-flash",
            fallback_provider="deepseek").select_route(
                provider="deepseek", model="deepseek-v4-pro")
        agent.switch_model("deepseek-v4-pro", provider="deepseek")
        return response

    adapter.complete = complete_then_switch

    agent.run(session, "第一轮", max_steps=1)

    # 本轮用的是**旧**模型，而且历史里**没有**那句话（它对本轮是假的）。
    assert adapter.seen == ["deepseek/deepseek-flash"]
    assert not [m for m in session.messages
                if "model changed" in str(m.get("content") or "")]

    # 下一轮：先留那句话，再请求 —— 请求用的是新模型。
    adapter.complete = original
    agent.run(session, "第二轮", max_steps=1)
    assert adapter.seen == ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]
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
    assert agent.switch_model("deepseek-v4-pro", provider="deepseek") is False
    assert agent.model_name == "deepseek-flash"
    # 没换成的时候**会话里也不许留下"选了它"** —— 两件事必须在同一处发生。
    assert agent.session_model.selected == "deepseek-flash"


def test_model_name_reads_the_adapter_not_a_copy():
    """`Agent.model_name` **从适配器上读** —— 存一份副本就会在某条路上分家。"""
    session = _session_with_model("deepseek-flash")
    adapter = _FakeAdapter("deepseek-flash")
    agent = _agent(adapter, session)
    assert agent.model_name == "deepseek-flash"
    adapter.model = "谁改的"
    assert agent.model_name == "谁改的"


# --- Runtime.select_model：名字的三种写法 + 跨路由 -------------------------------

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
        # 回报里的名字带路由（`provider/model`）—— 这样"上一个是谁"也答得清。
        assert "deepseek/deepseek-v4-pro" in message
        assert "deepseek/deepseek-flash" in message
        assert runtime.current_model == "deepseek-v4-pro"
        assert runtime.current_provider == "deepseek"
        # 分母跟着换（它是派生的，不是存下来的字段）。
        assert runtime.context_tokens == CONTEXT_WINDOWS["deepseek-v4-pro"]


def test_select_model_is_idempotent():
    with _runtime() as runtime:
        runtime.select_model("deepseek-v4-pro")
        ok, message = runtime.select_model("deepseek-v4-pro")
        assert ok is True
        assert "已经是" in message


def test_a_second_provider_gets_used_when_it_is_the_only_one_that_can_be(workdir):
    """**跨路由**：第一条没有密钥时，装配会退到后面那条能用的。

    这是多 provider 那个功能的底线行为 —— 一条配置里只要**有一条**能用，会话就该开得
    起来，而缺密钥的那几条只在 `/model` 里表现为"选不了它下面的模型"。
    """
    registry = _registry(workdir, {
        "nokey": {"base_url": "https://a.example/v1",
                  "models": [{"id": "m1", "context_window": 1000}]},
        "acme": {"base_url": "https://b.example/v1", "api_key": "sk-b",
                 "models": [{"id": "m2", "context_window": 2048}]},
    })
    with _runtime(registry=registry) as runtime:
        assert (runtime.current_provider, runtime.current_model) == ("acme", "m2")
        assert runtime.context_tokens == 2048


def test_switching_across_providers_moves_the_request(workdir):
    """`/model acme/m2`：**请求真的改发到另一台上**，而且会话里记着那条路由。

    这条是整个多 provider 功能的验收：配置里两条路由、两条都有密钥，换过去之后
    `current_provider` / `base_url` / 窗口三样一起跟着变。
    """
    registry = _registry(workdir, {
        "deepseek": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                     "models": [{"id": "deepseek-flash", "context_window": 1000}]},
        "acme": {"base_url": "https://b.example/v1", "api_key": "sk-b",
                 "models": [{"id": "m2", "context_window": 2048}]},
    })
    with _runtime(registry=registry) as runtime:
        assert runtime.current_provider == "deepseek"

        ok, message = runtime.select_model("acme/m2")
        assert ok is True and "acme/m2" in message
        assert runtime.current_provider == "acme"
        assert runtime.current_base_url == "https://b.example/v1"
        assert runtime.context_tokens == 2048
        assert runtime.agent.session_model.route_name() == "acme/m2"
        # 适配器上那把密钥也换了 —— 换路由不换密钥就等于用旧账号敲新地址。
        assert runtime.agent.model._api_key == "sk-b"


def test_a_bare_provider_name_picks_that_routes_first_model(workdir):
    """`/model acme` 是自然用法（"换到 acme 去"），而 acme 上有什么它自己知道。"""
    registry = _registry(workdir, {
        "deepseek": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                     "models": [{"id": "deepseek-flash", "context_window": 1000}]},
        "acme": {"base_url": "https://b.example/v1", "api_key": "sk-b",
                 "models": [{"id": "m2", "context_window": 2048},
                            {"id": "m3", "context_window": 4096}]},
    })
    with _runtime(registry=registry) as runtime:
        assert runtime.select_model("acme")[0] is True
        assert runtime.current_model == "m2", "用那条路由上的第一个模型"


def test_a_name_on_two_routes_is_refused_until_you_say_which(workdir):
    """同名模型落在两条路由上时**拒绝并报出候选** —— 随便挑一条是那种"看起来完全
    正常、账单却在另一个账号上"的错误。"""
    registry = _registry(workdir, {
        "a": {"base_url": "https://a.example/v1", "api_key": "sk-a",
              "models": [{"id": "same", "context_window": 1000}]},
        "b": {"base_url": "https://b.example/v1", "api_key": "sk-b",
              "models": [{"id": "same", "context_window": 1000}]},
    })
    with _runtime(registry=registry) as runtime:
        ok, message = runtime.select_model("same")
        assert ok is False
        assert "a/same" in message and "b/same" in message
        # 写全了就认。
        assert runtime.select_model("b/same")[0] is True
        assert runtime.current_provider == "b"


def test_a_route_without_a_key_cannot_be_selected(workdir):
    """没密钥的路由**在清单里但选不了**，而且那句话要说清怎么补。"""
    registry = _registry(workdir, {
        "deepseek": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                     "models": [{"id": "deepseek-flash", "context_window": 1000}]},
        "nokey": {"base_url": "https://b.example/v1",
                  "models": [{"id": "m2", "context_window": 2048}]},
    })
    with _runtime(registry=registry) as runtime:
        ok, message = runtime.select_model("nokey/m2")
        assert ok is False
        assert "没有密钥" in message and "api_key" in message
        assert runtime.current_provider == "deepseek"


def test_thinking_and_effort_refuse_with_a_usable_message():
    """两个旋钮的拒绝路径：**认不出的值不改任何东西，而且说清能写什么。**"""
    with _runtime() as runtime:
        ok, message = runtime.select_effort("hgih")
        assert ok is False and "low" in message and "max" in message
        assert runtime.agent.effort == reasoning.DEFAULT_EFFORT

        # `none` 是"关掉思考"，属于另一个旋钮 —— 指路，不照做。
        ok, message = runtime.select_effort("none")
        assert ok is False and "/thinking off" in message

        # 正常改：开关与强度互不影响。
        assert runtime.select_thinking(False)[0] is True
        assert runtime.agent.thinking is False
        assert runtime.select_effort("max")[0] is True
        assert runtime.agent.effort == "max"
        assert runtime.agent.thinking is False, "改强度不该顺手把思考打开"


def test_the_thinking_settings_are_written_to_disk_immediately(workdir, monkeypatch):
    """和 `/model` 同一条：**一句话不说就退出，恢复会话时那两个设置还在。**

    它们决定花多少钱、想多久 —— 丢掉之后用户看到的是一份"我明明关了"的设置。
    """
    registry = _registry(workdir, {
        "deepseek": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                     "models": [{"id": "deepseek-flash", "context_window": 1000}]},
    })
    with _open_isolated(workdir, monkeypatch, registry) as (booted, runtime, session_id):
        assert runtime.select_thinking(False)[0] is True
        assert runtime.select_effort("low")[0] is True
        reloaded = booted.store.load(session_id)

    restored = model_state.SessionModel.restore(
        reloaded.metadata, fallback="deepseek-flash")
    assert restored.thinking is False
    assert restored.effort == "low"


def test_the_selection_is_written_to_disk_immediately(workdir, monkeypatch):
    """`/model` 之后**一句话都不说就退出**，恢复会话时那个选择还在。

    这条钉的是"立刻落盘"那一步：等下一个检查点的话，最自然的用法之一（进去、换模型、
    退出）会丢掉这次选择 —— 而恢复时我们会说"你选的是 flash"，那和刚给出过的承诺相反。
    """
    registry = _registry(workdir, {
        "deepseek": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                     "models": [{"id": "deepseek-flash", "context_window": 1000},
                                {"id": "m2", "context_window": 2048}]},
    })
    with _open_isolated(workdir, monkeypatch, registry) as (booted, runtime, session_id):
        assert runtime.select_model("m2")[0] is True
        # 磁盘上那一份已经是新的了（**没有任何回合跑过**）。
        reloaded = booted.store.load(session_id)

    restored = model_state.SessionModel.restore(
        reloaded.metadata, fallback="deepseek-flash")
    assert restored.selected == "m2"


def test_the_selection_is_per_session_and_survives_a_resume():
    """`/model` 换的是**这个会话**，恢复它时还是那个（和任务列表、技能同一条路）。

    这条同时钉住了"新会话用回目录里那个默认值"：会话级选择住在 `session.metadata` 里，
    所以另开一个会话拿到的就是干净的一份。
    """
    with _runtime() as runtime:
        runtime.select_model("deepseek-v4-pro")
        assert runtime.agent.session_model.selected == "deepseek-v4-pro"

    with _runtime() as other:
        assert other.current_model == "deepseek-flash"
        assert other.agent.session_model.selection is None


# --- 造一个真的 Runtime（用假的目录）--------------------------------------------

def _registry(workdir, providers: dict):
    """把一段 `providers` 写成配置文件再读成 Registry。

    **走真的读盘那条路**（`catalog.load`）：这样目录的形状错误（少 base_url、
    不认识的键）在测试里也会现形，而不是被一个手搓的 Registry 绕过去。
    """
    path = workdir / "models.local.json"
    path.write_text(json.dumps({"providers": providers}, ensure_ascii=False),
                    encoding="utf-8")
    return catalog.load(path, env_file=workdir / "missing.env")


def _channels():
    """CLI 版的通道（不起子进程、不弹面板）—— 装配要它。"""
    from agent_runtime.runtime.channels import cli_channels

    return cli_channels()


@contextmanager
def _open_isolated(workdir, monkeypatch, registry):
    """一个把会话/日志都落在 `workdir` 里的 Runtime —— "立刻落盘"那两条测试要它。

    没有这个隔离，"立刻落盘"验的就是**开发机上那份真的 `.tudouni/`**（测试会往仓库里
    写会话文件，而它们本来只该验证"写没写"）。
    """
    from agent_runtime.runtime import composition

    monkeypatch.setattr(composition, "project_dir", lambda: workdir)
    booted = composition.boot()
    session_id, session, resumed = composition.resolve_session(booted.store, None)
    runtime = composition.open_runtime(
        booted=booted, session_id=session_id, session=session,
        channels=_channels(), resumed=resumed,
        model_config=_model_config(),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(), catalog_config=registry,
    )
    try:
        yield booted, runtime, session_id
    finally:
        runtime.close()


def _runtime(registry=None):
    """一个真的 Runtime（假密钥、假网关地址，**不发任何请求**）。

    **调用方必须 `runtime.close()`**（`httpx.Client` 是进程级资源）—— 所以这里不返回
    "已经替你收好"的东西，而是让测试用 `with` 收。给 Runtime 打补丁换掉 `close` 是
    不行的：它是 frozen + slots 的 dataclass，`monkeypatch.setattr` 会撞上
    `super(type, obj)` 那个 TypeError。
    """
    from agent_runtime.runtime import composition

    booted = composition.boot()
    session_id, session, resumed = composition.resolve_session(booted.store, None)
    return composition.open_runtime(
        booted=booted, session_id=session_id, session=session,
        channels=_channels(), resumed=resumed,
        model_config=_model_config(),
        permission_config=PermissionConfig(), web_config=WebConfig(),
        mcp_config=McpConfig(), catalog_config=registry,
    )


def _model_config():
    return ModelConfig(api_key="sk-x", base_url="http://127.0.0.1:1",
                       model="deepseek-flash")
