"""`/status` `/tools` `/model` 三条命令：**渲染、发什么、以及回包之后界面变成什么。**

三条命令的形态是**一样的**（"发一条请求，runtime 回来一份数据，渲染成几行进会话流"），
而它们各自最容易错的地方不同，所以分开测：

  * `/status` —— **三个数各自的口径**不能混（上下文是上一次请求、累计是整会话、
    轮次数的是 run_started）。混了不会报错，只会显示一个看起来合理的错数；
  * `/tools` —— 权限那一列**必须来自 runtime**，而且"会问你"和"不问"要一眼分得开；
  * `/model` —— 目录外的名字**不能被猜**（打错一个字母换到另一个模型上，只有账单
    看得出来）。

渲染函数是纯函数，所以第一层直接调它们；第二层用 `app.submit(...)` + 喂回包，
测的是"命令发对了没有、回包画出来了没有"。
"""

import pytest

from agent_runtime.frontends.tui import view_state
from test_tui import _build_app, _log_text, _settle

# --- 第一层：渲染（纯函数）------------------------------------------------------

STATUS = {
    "status": {
        "session": {"id": "20260911-165811", "resumed": True,
                    "workspace": r"C:\Users\me\repo\agent_runtime",
                    "messages": 12, "steps": 7},
        "model": {"current": "deepseek-flash", "selected": "deepseek-flash",
                  "last_used": "deepseek-flash", "window": 1_000_000,
                  "base_url": "https://api.deepseek.com"},
        "counters": {"runs": 3, "model_calls": 9, "model_ok": 9, "tool_calls": 14,
                     "permission_waits": 2, "asks": 1},
        "usage": {"prompt": 203_500, "cached": 179_200, "miss": 24_300,
                  "completion": 8_100},
        "meta": {"max_steps": 80, "stream": True, "autopilot": False,
                 "tool_count": 14, "audit_path": r"C:\w\.tudouni\logs\x.jsonl"},
    },
    "last_prompt_tokens": 203_500,
    "context_tokens": 1_000_000,
}


def test_the_status_screen_reports_every_number_with_its_own_unit():
    """`/status` 里那三个 token 数字**口径不同，措辞就必须不同**：

      * `上下文` 是**上一次请求**发出去多少（下界）；
      * `累计输入/输出` 是**整个会话**的（那是钱）；
      * `轮次` 数的是 `run_started`。

    它们混起来的症状是"显示了一个看起来完全合理的错数"，所以每一行都得能被单独认出来。
    """
    text = "\n".join(str(line) for line in
                     view_state.render_status(view_state.ViewState(), STATUS))
    assert "20260911-165811" in text and "这次启动：继续" in text
    assert "12 条消息 · 7 步" in text
    assert "deepseek-flash" in text
    # 上下文：分子分母都在，而且带一位小数的占比。
    assert "203.5k / 1M（20.3%）" in text
    # 累计：**另一个口径**，而且带命中率（看钱要看未命中）。
    assert "203.5k token" in text and "命中率 88%" in text
    assert "8.1k token" in text
    # 轮次与调用。
    assert "3 轮 · 9 次模型调用 · 14 次工具调用" in text
    assert "审批 2 次" in text and "提问 1 次" in text
    # 这次运行的环境 + 去处。
    assert "最多 80 步" in text and "流式" in text and "逐条审批" in text
    assert "14 个" in text and "x.jsonl" in text


def test_the_status_screen_shows_a_pending_model_change():
    """刚 `/model` 完、下一次请求还没发出去时，那一行要说清"还没生效"。

    不说的话，"换了但没生效"看起来像坏了 —— 而它其实是设计好的时序（同一个回合不能
    由两个模型拼出来，见 `state/model.py` 的 `SessionModel`）。
    """
    message = {
        "status": {**STATUS["status"],
                   "model": {**STATUS["status"]["model"],
                             "selected": "deepseek-v4-pro"}},
        "last_prompt_tokens": 100,
        "context_tokens": 1_000_000,
    }
    text = "\n".join(str(line) for line in
                     view_state.render_status(view_state.ViewState(), message))
    assert "下一次请求生效" in text and "deepseek-v4-pro" in text


def test_the_status_screen_does_not_guess_a_denominator():
    """模型不在目录里时**只报用量、不报占比** —— 错的百分比比没有百分比更坏。"""
    message = {"status": {**STATUS["status"],
                          "model": {**STATUS["status"]["model"], "window": None}},
               "last_prompt_tokens": 2_000,
               "context_tokens": None}
    lines = [str(line) for line in
             view_state.render_status(view_state.ViewState(), message)]
    context = next(line for line in lines if "上下文" in line)
    assert "2k" in context
    # **只看上下文那一行**：命中率那个 `%` 是另一件事（它算得出来，而且必须报）。
    assert "%" not in context
    assert "不报占比" in context


def test_the_status_screen_does_not_invent_a_state():
    """没有状态时**不编一份空状态**（"0 轮 0 调用"会被读成事实，而事实是"还没问过"）。"""
    lines = view_state.render_status(view_state.ViewState(), {})
    assert len(lines) == 1
    assert "还没有状态" in str(lines[0])


def test_the_tools_screen_answers_both_questions_at_once():
    """`/tools` 同时回答"有哪些工具"和"它会不会问我" —— 所以**每一条都要列**。

    只列自动放行的（"✓"那种写法）会把拒绝名单藏起来，而"我明明配了 deny_tools"正是
    最该在这里看见答案的问题。
    """
    message = {
        "tools": [
            {"name": "read_file", "risk": "low", "disposition": "auto",
             "parallel_safe": True, "interactive": False, "external": False,
             "granted": False, "command": None},
            {"name": "shell", "risk": "high", "disposition": "ask",
             "parallel_safe": False, "interactive": False, "external": False,
             "granted": True, "command": "command"},
            {"name": "mcp__kb__search", "risk": "high", "disposition": "deny",
             "parallel_safe": False, "interactive": False, "external": True,
             "granted": False, "command": None},
        ],
        "granted_prefixes": ["git add"],
    }
    lines = view_state.render_tools(view_state.ViewState(), message)
    text = "\n".join(str(line) for line in lines)
    assert "read_file" in text and "自动放行" in text and "可并发" in text
    assert "shell" in text and "需要审批" in text and "按过 t" in text
    assert "mcp__kb__search" in text and "直接拒绝" in text and "外部" in text
    assert "git add" in text
    # 三种处置**颜色上也要分得开**（这是"会不会弹审批"那一列的全部价值）。
    # `segments` 为 None = "整行一个 role"，所以两种都要摊平了取。
    roles = {role for line in lines
             for _text, role in (line.segments or [(str(line), line.role)])}
    assert view_state.ROLE_WAITING in roles and view_state.ROLE_DENIED in roles


def test_the_tools_screen_says_so_when_nothing_is_registered():
    """一个工具都没有时**说清楚**，别让屏幕空着（缺密钥/引擎时会这样）。"""
    lines = view_state.render_tools(view_state.ViewState(), {})
    assert len(lines) == 1 and "一个工具都没注册" in str(lines[0])


def test_the_model_list_marks_the_current_one_and_lists_aliases_separately():
    """目录里标出当前那个；**旧名字不算可选项**，单独列。"""
    state = view_state.ViewState(
        model="deepseek-flash",
        model_catalog=[
            {"id": "deepseek-flash", "label": "Flash", "window": 1_000_000,
             "summary": "快、便宜", "note": "细节 A", "current": True},
            {"id": "deepseek-v4-pro", "label": "Pro", "window": 1_000_000,
             "summary": "贵得多", "note": "细节 B", "current": False},
        ],
        model_aliases=[{"id": "deepseek-v4-flash", "of": "deepseek-flash"}],
    )
    text = "\n".join(str(line) for line in view_state.render_models(state))
    assert "当前模型：deepseek-flash" in text
    assert "● deepseek-flash" in text
    assert "  deepseek-v4-pro" in text          # 前面没有 ●
    assert "细节 A" in text and "细节 B" in text
    assert "认下的旧名字：deepseek-v4-flash → deepseek-flash" in text


# --- 第二层：命令与回包（真的 App）----------------------------------------------

@pytest.mark.anyio
async def test_status_and_tools_ask_the_runtime_instead_of_reading_files(monkeypatch):
    """两条命令**只发一条请求**，不自己去读 `.tudouni/` 或算权限。

    这是 `list_sessions` 立过的规矩：目录布局、策略判定都是 runtime 的知识，前端
    自己算就是第二份事实 —— 而它漂掉的症状是"界面说会问我，实际没问"。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app.submit("/status")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "status"}

        app.submit("/tools")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "tools"}

        # 回包到了才画 —— 界面不自己编一屏状态出来。
        assert "状态" not in _log_text(app)


@pytest.mark.anyio
async def test_the_status_reply_lands_in_the_conversation_log(monkeypatch):
    """回包 `ui(status)` 渲染进会话流（**不是面板**）：那一屏是"看一眼就走"的东西。"""
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app.submit("/status")
        await _settle(app, pilot)
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "status", **STATUS}))
        await _settle(app, pilot)

        text = _log_text(app)
        assert "20260911-165811" in text
        assert "203.5k / 1M（20.3%）" in text
        # 同一份数据也留在 state 里（后续要重画时用得上），但它**不是**面板数据。
        assert app.state.status["session"]["id"] == "20260911-165811"


@pytest.mark.anyio
async def test_the_tools_reply_lands_in_the_conversation_log(monkeypatch):
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app.submit("/tools")
        await _settle(app, pilot)
        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "tools",
            "tools": [{"name": "read_file", "risk": "low", "disposition": "auto",
                       "parallel_safe": True, "interactive": False,
                       "external": False, "granted": False, "command": None}],
            "granted_prefixes": [],
        }))
        await _settle(app, pilot)

        text = _log_text(app)
        assert "read_file" in text and "自动放行" in text
        assert [row["name"] for row in app.state.tools] == ["read_file"]


@pytest.mark.anyio
async def test_model_without_arguments_lists_and_with_an_argument_asks_the_runtime(monkeypatch):
    """不带参数**只列清单**（不做"轮换到下一个"），带参数把名字发给 runtime。

    名字**由 runtime 校验**：界面不知道自己有哪些模型（那是目录的知识），所以它
    连"这个名字对不对"都不判 —— 发出去，等回包（state 或 notice）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s", "resumed": True,
            "model": "deepseek-flash", "workspace": "C:/w", "max_steps": 80,
            "context_tokens": 1_000_000, "tools": [], "permissions": {},
            "audit_path": "C:/w/.tudouni/logs/s.jsonl", "notices": [],
            "model_catalog": {
                "models": [{"id": "deepseek-flash", "label": "Flash",
                            "window": 1_000_000, "summary": "快、便宜",
                            "note": "", "current": True},
                           {"id": "deepseek-v4-pro", "label": "Pro",
                            "window": 1_000_000, "summary": "贵得多",
                            "note": "", "current": False}],
                "aliases": [],
            },
        }))
        await _settle(app, pilot)
        assert [item["id"] for item in app.state.model_catalog] == \
            ["deepseek-flash", "deepseek-v4-pro"]

        app.submit("/model")
        await _settle(app, pilot)
        assert _log_text(app).count("deepseek-v4-pro") >= 1
        assert app._client.sent == [], "不带参数只列清单，不进 runtime"

        app.submit("/model deepseek-v4-pro")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_model", "model": "deepseek-v4-pro"}


@pytest.mark.anyio
async def test_a_state_snapshot_moves_the_model_and_the_window_together(monkeypatch):
    """`ui(state)` 快照里的**模型和窗口必须一起更新**。

    只更新名字的话，状态栏会拿新模型的用量去比旧窗口 —— 看起来完全正常，只是数错了。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "state",
            "model": "deepseek-v4-pro", "model_window": 500_000,
        }))
        await _settle(app, pilot)
        assert app.state.model == "deepseek-v4-pro"
        assert app.state.context_tokens == 500_000

        # 一份老 runtime 发来的快照没有这两个键：**保住已经拿到的那份**。
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "messages": 3, "steps": 1}))
        await _settle(app, pilot)
        assert app.state.model == "deepseek-v4-pro"
        assert app.state.context_tokens == 500_000
        assert app.state.messages == 3


@pytest.mark.anyio
async def test_the_model_catalog_survives_a_session_switch(monkeypatch):
    """换会话**不清**目录（它是进程级的常量数据，不是会话状态）。

    清掉的话，`/new` 之后 `/model` 会摆出一张空清单 —— 而"清单空了"看起来像 runtime
    没给，不像界面自己弄丢了。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s1", "resumed": False,
            "model": "deepseek-flash", "workspace": "C:/w", "max_steps": 80,
            "tools": [], "permissions": {}, "notices": [],
            "model_catalog": {"models": [{"id": "deepseek-flash", "label": "F",
                                          "window": 1, "summary": "", "note": "",
                                          "current": True}],
                              "aliases": []},
        }))
        await _settle(app, pilot)
        assert app.state.model_catalog

        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s2", "resumed": False,
            "model": "deepseek-flash", "workspace": "C:/w", "max_steps": 80,
            "tools": [], "permissions": {}, "notices": [],
            "model_catalog": {"models": [{"id": "deepseek-flash", "label": "F",
                                          "window": 1, "summary": "", "note": "",
                                          "current": True}],
                              "aliases": []},
        }))
        await _settle(app, pilot)
        assert app.state.model_catalog, "换会话之后目录还在"
