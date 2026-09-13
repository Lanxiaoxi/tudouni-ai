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

**外观（每行长什么样、颜色对不对）不在这份测试的范围里**：项目规范说 UI 改动只测
逻辑正确性，观感交给用户。所以这里断言的都是"哪个值出现在了哪一行""有没有按策略
分色"这类**能被判定对错**的东西，不做逐字排版断言。
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
        "model": {"provider": "deepseek", "current": "deepseek-flash",
                  "selected": "deepseek-flash", "last_used": "deepseek-flash",
                  "window": 1_000_000, "base_url": "https://api.deepseek.com",
                  "reasoning": {"thinking": True, "effort": "high"}},
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


def test_the_status_screen_says_which_provider_and_thinking_settings():
    """`/status` 要回答"它在用什么、想得多用力"。

    provider 单独写出来是因为这一版能接多个网关（`/model` 会在它们之间选），而
    "deepseek-flash" 这个名字在两条路由上都可能出现 —— 只看模型名说不出请求发到哪儿。
    """
    text = "\n".join(str(line) for line in
                     view_state.render_status(view_state.ViewState(), STATUS))
    assert "@deepseek" in text, "路由名要跟着模型名一起显示"
    assert "思考" in text and "开" in text and "high" in text
    # **官方端点不单独占一行**（它是绝大多数会话的样子），自建网关才写出来。
    assert "api.deepseek.com" not in text

    other = {"status": {**STATUS["status"],
                        "model": {**STATUS["status"]["model"],
                                  "provider": "acme",
                                  "base_url": "https://gateway.example.com/v1"}},
             "last_prompt_tokens": 10, "context_tokens": 1_000_000}
    text = "\n".join(str(line) for line in
                     view_state.render_status(view_state.ViewState(), other))
    assert "@acme" in text and "https://gateway.example.com/v1" in text


def test_a_disabled_thinking_setting_does_not_print_an_effort():
    """关掉思考时**不写强度**：`关 · high` 会让人以为 high 还在生效。

    强度并没有被丢掉（`/thinking on` 之后还是原来那个），只是这一行不该撒谎 ——
    但它得说清"记着"，否则用户会以为刚才那个 max 没了。
    """
    message = {"status": {**STATUS["status"],
                          "model": {**STATUS["status"]["model"],
                                    "reasoning": {"thinking": False, "effort": "max"}}},
               "last_prompt_tokens": 100,
               "context_tokens": 1_000_000}
    line = next(str(item) for item in
                view_state.render_status(view_state.ViewState(), message)
                if "思考" in str(item))
    assert "关" in line
    assert "max" not in line
    assert "记着" in line


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
    """清单里标出当前那个；**旧名字不算可选项**，单独列。

    名字写成 `provider/model`：同名模型可以在多条路由上，而只写模型名的话那两行长得
    一模一样 —— 可"选了哪一个"决定了请求发到哪个账号上。
    """
    state = view_state.ViewState(
        model="deepseek-flash", provider="deepseek",
        model_catalog=[
            {"provider": "deepseek", "id": "deepseek-flash", "label": "Flash",
             "window": 1_000_000, "summary": "快、便宜", "note": "细节 A",
             "current": True},
            {"provider": "deepseek", "id": "deepseek-v4-pro", "label": "Pro",
             "window": 1_000_000, "summary": "贵得多", "note": "细节 B",
             "current": False},
        ],
        model_aliases=[{"id": "deepseek-v4-flash", "of": "deepseek-flash"}],
    )
    text = "\n".join(str(line) for line in view_state.render_models(state))
    assert "当前模型：deepseek/deepseek-flash" in text
    assert "● deepseek/deepseek-flash" in text
    assert "  deepseek/deepseek-v4-pro" in text          # 前面没有 ●
    assert "细节 A" in text and "细节 B" in text
    assert "认下的旧名字：deepseek-v4-flash → deepseek-flash" in text


def test_the_model_list_groups_by_provider():
    """两条路由都有同一个模型名时，清单要**分得开**（名字一样不等于去处一样）。"""
    state = view_state.ViewState(
        model="deepseek-flash", provider="deepseek",
        model_catalog=[
            {"provider": "deepseek", "id": "deepseek-flash", "label": "Flash",
             "window": 1_000_000, "summary": "官方", "note": "", "current": True},
            {"provider": "acme", "id": "deepseek-flash", "label": "Flash",
             "window": 1_000_000, "summary": "网关", "note": "", "current": False},
        ],
        model_aliases=[],
    )
    lines = [str(line) for line in view_state.render_models(state)]
    text = "\n".join(lines)
    assert "deepseek/deepseek-flash" in text and "acme/deepseek-flash" in text
    assert "官方" in text and "网关" in text


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
                "models": [{"provider": "deepseek", "id": "deepseek-flash",
                            "label": "Flash", "window": 1_000_000,
                            "summary": "快、便宜", "note": "", "current": True},
                           {"provider": "deepseek", "id": "deepseek-v4-pro",
                            "label": "Pro", "window": 1_000_000,
                            "summary": "贵得多", "note": "", "current": False}],
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
            "model_catalog": {"models": [{"provider": "deepseek",
                                          "id": "deepseek-flash", "label": "F",
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
            "model_catalog": {"models": [{"provider": "deepseek",
                                          "id": "deepseek-flash", "label": "F",
                                          "window": 1, "summary": "", "note": "",
                                          "current": True}],
                              "aliases": []},
        }))
        await _settle(app, pilot)
        assert app.state.model_catalog, "换会话之后目录还在"


# --- `/thinking` `/effort`（第二版加的）----------------------------------------

@pytest.mark.anyio
async def test_thinking_and_effort_send_requests_and_render_replies(monkeypatch):
    """两条命令都**不进** `/model` 那一条：它们是自己的会话级设置。

    `/thinking` 不带参数只报当前值（不做"切一下"）：和 `/theme` `/model` 同一条交互
    规矩 —— 轮换把"现在是什么"变成一个必须靠记忆的状态。

    **发出去的是布尔，不是 "off" 两个字**：认哪些词算开是前端的事，而协议上那一格
    只有一个形状。写错了在替身那里就会现形（它照协议记账）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app.submit("/thinking")
        await _settle(app, pilot)
        assert app._client.sent == [], "不带参数只报当前值"

        app.submit("/thinking off")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_thinking", "on": False}

        app.submit("/effort max")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_effort", "effort": "max"}


@pytest.mark.anyio
async def test_a_bad_thinking_word_is_refused_without_guessing(monkeypatch):
    """认不出的写法**不发请求也不猜** —— 猜错的方向是"用户以为关掉了、其实还开着"。"""
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app.submit("/thinking maybe")
        await _settle(app, pilot)
        assert app._client.sent == []
        assert "认不出" in _log_text(app)


@pytest.mark.anyio
async def test_the_effort_menu_comes_from_the_runtime_not_the_frontend(monkeypatch):
    """档位清单**随协议来**（`init.effort_levels`），前端不写死也不 import 内核。

    这条钉的是决策 18：前端只讲协议。写死一份清单的代价是具体的 —— 端点加一档就得改
    两个地方，而漏改的那一处只表现为"这一档选不了"。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s", "resumed": True,
            "model": "deepseek-flash", "provider": "deepseek",
            "workspace": "C:/w", "max_steps": 80, "tools": [], "permissions": {},
            "audit_path": "x", "notices": [],
            "thinking": False, "effort": "low",
            "effort_levels": ["low", "high", "max"],
        }))
        await _settle(app, pilot)
        assert app.state.effort_levels == ("low", "high", "max")
        assert app.state.thinking_on is False and app.state.effort == "low"

        app.submit("/effort")
        await _settle(app, pilot)
        text = _log_text(app)
        assert "low" in text and "high" in text and "max" in text
        assert app._client.sent == [], "不带参数只列档位"


@pytest.mark.anyio
async def test_a_state_snapshot_carries_the_live_thinking_settings(monkeypatch):
    """开关和强度**只能由 runtime 的快照改**（界面不许乐观更新）。

    "灯亮着、其实没开"在这两格上的代价和 autopilot 一样：它们决定下一次请求花多少钱、
    想多久，而用户按下的那一刻看到的必须是真的。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "state",
            "thinking": False, "effort": "low",
        }))
        await _settle(app, pilot)
        assert app.state.thinking_on is False
        assert app.state.effort == "low"

        # 老 runtime 的快照没有这两个键 → 保住已有的那份（不是猜成"开"）。
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "messages": 1}))
        await _settle(app, pilot)
        assert app.state.thinking_on is False and app.state.effort == "low"
