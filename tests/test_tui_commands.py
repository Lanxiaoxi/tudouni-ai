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

from agent_runtime.frontends.tui import view_state, widgets
from test_tui import _build_app, _log_text, _settle

# 一份"两个模型可选"的 `init`。选择面板那几条测试共用它 —— 两处各抄一份 payload
# 的话，改了一处另一处就悄悄还在测旧形状。
CATALOG_INIT = {
    "v": 1, "t": "init", "session_id": "s", "resumed": True,
    "model": "deepseek-flash", "provider": "deepseek",
    "workspace": "C:/w", "max_steps": 80, "context_tokens": 1_000_000,
    "tools": [], "permissions": {},
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
}


def _picker_text(app) -> str:
    """选择面板上**画出来的字**（候选项那一份）。

    断言面板内容必须从控件里读，不能读会话流：面板的意义正是"那些字不随对话滚走"，
    所以它压根不在 `_log_text(app)` 里 —— 拿会话流去断言只会得到"什么都没发生"。
    """
    return "\n".join(str(child.render()) for child in app.screen.query(".option"))


def _marked_option(app) -> str:
    """面板里带 `●` 的那一行（当前那一项）。**用来钉"圆点跟没跟着走"。**"""
    for option in app.screen.options:
        if option.line.role == view_state.ROLE_WAITING:
            return str(option.line)
    return ""

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
async def test_model_without_arguments_opens_a_picker_and_with_an_argument_asks_the_runtime(monkeypatch):
    """不带参数**弹选择面板**，带参数把名字发给 runtime。

    面板那一条替代的是"先看一眼清单、再把名字一个字符不差地打一遍"—— 而那个名字
    可以又长又带 `provider/` 前缀（`deepseek/deepseek-v4-pro`）。所以这条测试盯两件事：
    面板里的候选**和清单是同一份**（名字写全），以及选中之后回给 runtime 的**就是那个
    全名**（界面不自己拼、也不猜）。

    带参数时名字**由 runtime 校验**：界面不知道自己有哪些模型（那是目录的知识），所以
    它连"这个名字对不对"都不判 —— 发出去，等回包（state 或 notice）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", dict(CATALOG_INIT)))
        await _settle(app, pilot)
        assert [item["id"] for item in app.state.model_catalog] == \
            ["deepseek-flash", "deepseek-v4-pro"]

        app.submit("/model")
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker), "不带参数弹面板"
        # 候选在**面板**里（不是会话流）：名字那一列写全成 provider/model。
        assert "deepseek/deepseek-v4-pro" in _picker_text(app)
        assert app._client.sent == [], "还没选，什么都不发"

        # `Enter` = 换到光标那一个。默认光标落在**当前的下一个**（打开面板的人几乎
        # 总是想换一个），也就是清单里第二个。
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_model",
                                        "model": "deepseek/deepseek-v4-pro"}

        app.submit("/model deepseek-v4-pro")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_model", "model": "deepseek-v4-pro"}


@pytest.mark.anyio
async def test_escape_in_the_model_picker_changes_nothing(monkeypatch):
    """`Esc` = **什么都不做**，而且一个字节都不发给 runtime。

    模型换错是要花真钱的（Pro 的未命中输入是 Flash 的四倍多），所以"没选"必须是一个
    零后果的动作 —— 和 `SessionPicker` 那条同源（换会话误触的代价同样大）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", dict(CATALOG_INIT)))
        await _settle(app, pilot)

        app.submit("/model")
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker)

        await pilot.press("escape")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker), "面板关掉了"
        assert app._client.sent == [], "什么都没发"
        assert app.state.model == "deepseek-flash"


@pytest.mark.anyio
async def test_the_model_picker_waits_for_the_runtime_and_says_what_happened(monkeypatch):
    """选完**面板不马上关**：`/model` 的成败只有 runtime 知道。

    先关掉的话，"这条路由上没有密钥、没换成"就只表现为一张关掉的浮层 —— 和"换成了
    一下子没看出来"分不开。所以拿到那条 notice 之前，面板照旧开着；notice 到了，
    它的话**原样**写在面板上（关掉的时机交给用户按 `Esc`）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", dict(CATALOG_INIT)))
        await _settle(app, pilot)
        app.submit("/model")
        await _settle(app, pilot)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_model",
                                        "model": "deepseek/deepseek-v4-pro"}
        assert isinstance(app.screen, widgets.OptionPicker), "回话之前面板还在"

        app._inbox.put(("message", {"v": 1, "t": "notice", "level": "warn",
                                    "code": "model",
                                    "text": "[模型] 没换：acme 这条路由上没有密钥"}))
        await _settle(app, pilot)
        # 那句话在**会话流**里也留了一份（notice 那条路没动），往回翻还看得见。
        assert "没换" in _log_text(app)
        # 也在面板上（原样）—— 面板这时还开着，人看清了再按 `Esc`。
        assert isinstance(app.screen, widgets.OptionPicker)
        result = app.screen.query_one("#option-result").render()
        assert "这条路由上没有密钥" in str(result)

        await pilot.press("escape")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker)


@pytest.mark.anyio
async def test_an_unrelated_notice_does_not_take_the_picker_away(monkeypatch):
    """别的 notice 不许把选择面板挤走。

    面板开着时启动说明那类 notice 照样会来，而"选到一半被一条无关的话关掉面板"
    比不弹面板更坏 —— 用户会以为自己按错了什么。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", dict(CATALOG_INIT)))
        await _settle(app, pilot)
        app.submit("/model")
        await _settle(app, pilot)

        app._inbox.put(("message", {"v": 1, "t": "notice", "level": "info",
                                    "code": "startup", "text": "[启动] 技能目录已扫描"}))
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker), "面板还在"


@pytest.mark.anyio
async def test_the_option_picker_shows_the_note_of_the_selected_row(monkeypatch):
    """选中那条的 note（"这条路由没有密钥"之类）画在清单下面。

    它不能挤进候选那一行：那一行已经有"名字 / 摘要 / 窗口"三段，再挂一段话上去会
    折行，而折行会让**名字那一列**对不齐 —— 那一列正是这个面板唯一要一眼扫完的东西。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        payload = dict(CATALOG_INIT)
        payload["model_catalog"] = {
            "models": [
                {"provider": "deepseek", "id": "deepseek-flash", "label": "Flash",
                 "window": 1_000_000, "summary": "快、便宜", "current": True,
                 "note": "当前用的就是这个"},
                {"provider": "acme", "id": "m2", "label": "M2", "window": 200_000,
                 "summary": "自建网关", "current": False,
                 "note": "这条路由上没有密钥"},
            ],
            "aliases": [],
        }
        app._inbox.put(("message", payload))
        await _settle(app, pilot)

        app.submit("/model")
        await _settle(app, pilot)
        note = app.screen.query_one("#option-note").render()
        assert "这条路由上没有密钥" in str(note), "默认光标那一条的 note 画出来了"


@pytest.mark.anyio
async def test_the_theme_picker_applies_at_once_and_closes(monkeypatch):
    """`/theme` 那条面板和 `/model` 那条**有一处刻意的不同：选完立刻关**。

    配色是本地的，`_set_theme` 当场就重画了 —— 没有"等 runtime 回话"那一段，让一个
    已经完成的操作继续占着屏幕没有意义。所以它走 `runtime_backed=False`。
    """
    from agent_runtime.frontends.tui import theme as theme_mod

    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        assert app.theme == "A"

        app.submit("/theme")
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker)
        # 13 套都在，而且带展示序号（`/theme 6` 收的就是那个数）。
        text = _picker_text(app)
        assert " 1 P3 粉紫" in text and " 6 A 石墨琥珀" in text
        assert "11 P3-T 粉紫 · 透明" in text and "12 A-T 石墨琥珀 · 透明" in text
        # 深一档那套排在最后（13）：名字里"深"字是它和 12 的分界，面板上看得出来。
        assert "13 A-T2 石墨琥珀 · 深透明" in text
        assert app._client.sent == [], "本地操作，不进 runtime"

        # 把光标挪到 `C 墨绿仪器` 再确认：配色当场换、面板当场收、回声进会话流。
        app.screen._index = theme_mod.ORDER.index("C")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.theme == "C" and "墨绿" in app.palette.name
        assert not isinstance(app.screen, widgets.OptionPicker), "选完立刻关"
        assert "配色换成" in _log_text(app)


@pytest.mark.anyio
async def test_escape_in_the_theme_picker_changes_nothing(monkeypatch):
    """`Esc` = **什么都不做**：配色一个像素都不动。"""
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app.submit("/theme")
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker)

        await pilot.press("escape")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker)
        assert app.theme == "A"


@pytest.mark.anyio
async def test_the_effort_picker_moves_the_dot_when_the_runtime_confirms(monkeypatch):
    """换完那一档之后，**面板上的 `●` 和高亮要跟着挪过去**。

    这条是用户一眼看出来的：面板选完**不关**（等 runtime 那条 notice），而它画的是
    **打开那一刻**的候选快照 —— 不重画的话，选了 `low` 之后圆点照旧停在 `high` 上，
    看起来正是"我刚才那一按没生效"，而它其实生效了。

    数据本来就跟得上（`apply_state` 会更新 `state.effort`），跟不上的只有"面板没有
    按新数据重画"这一件事 —— 所以这条钉的是**重画**，不是数据。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s", "resumed": True,
            "model": "deepseek-flash", "provider": "deepseek",
            "workspace": "C:/w", "max_steps": 80, "tools": [], "permissions": {},
            "audit_path": "x", "notices": [],
            "thinking": True, "effort": "high",
            "effort_levels": ["low", "high", "max"],
        }))
        await _settle(app, pilot)

        app.submit("/effort")
        await _settle(app, pilot)
        assert "● high" in _marked_option(app)

        # 光标停在当前那一档，`↑` 一下就到 `low`，回车 → 请求发出去。
        await pilot.press("up")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_effort", "effort": "low"}
        assert "● high" in _marked_option(app), "回话之前圆点还在原处（不许乐观更新）"

        # runtime 的快照到了：圆点和高亮挪到 `low`。
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "effort": "low",
                                    "effort_levels": ["low", "high", "max"]}))
        await _settle(app, pilot)
        assert app.state.effort == "low"
        assert "● low" in _marked_option(app), "圆点跟着挪过去了"
        assert isinstance(app.screen, widgets.OptionPicker), "面板照旧开着"


@pytest.mark.anyio
async def test_the_model_picker_moves_the_dot_when_the_runtime_confirms(monkeypatch):
    """`/model` 同理：换完之后 `●` 挪到新模型那一行（也在**面板**上，不只看左栏）。"""
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", dict(CATALOG_INIT)))
        await _settle(app, pilot)
        app.submit("/model")
        await _settle(app, pilot)
        assert "● deepseek/deepseek-flash" in _marked_option(app)

        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_model",
                                        "model": "deepseek/deepseek-v4-pro"}

        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "model": "deepseek-v4-pro",
                                    "model_provider": "deepseek",
                                    "model_window": 1_000_000}))
        await _settle(app, pilot)
        assert "● deepseek/deepseek-v4-pro" in _marked_option(app)


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

    面板的候选**就是那一份清单**：界面只是把"runtime 说有哪几档"摆出来让人挑。
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
        assert isinstance(app.screen, widgets.OptionPicker), "不带参数弹面板"
        text = _picker_text(app)
        assert "low" in text and "high" in text and "max" in text
        assert "● low" in text, "当前那一档标出来"
        assert app._client.sent == [], "还没选，什么都不发"


@pytest.mark.anyio
async def test_the_effort_picker_starts_on_the_current_level_and_sends_what_was_picked(monkeypatch):
    """`/effort` 的光标**停在当前那一档**（不像 `/model` 停在下一个）。

    档位只有三四个，"换一档"和"看现在是哪一档"按一次键的成本一样 —— 那就停在真值上。
    按下 `↓` 之后 `Enter` 回给 runtime 的必须是**那一档的名字**。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s", "resumed": True,
            "model": "deepseek-flash", "provider": "deepseek",
            "workspace": "C:/w", "max_steps": 80, "tools": [], "permissions": {},
            "audit_path": "x", "notices": [],
            "thinking": True, "effort": "low",
            "effort_levels": ["low", "high", "max"],
        }))
        await _settle(app, pilot)

        app.submit("/effort")
        await _settle(app, pilot)
        await pilot.press("down")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_effort", "effort": "high"}


@pytest.mark.anyio
async def test_an_old_runtime_without_a_catalog_falls_back_to_the_text_list(monkeypatch):
    """runtime 没给清单时**退回纯文本**，不弹一张空面板。

    一个选项都没有的浮层看起来像界面坏了，而那其实是 runtime 那一版协议里没有这一格。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "session_id": "s", "resumed": True,
            "model": "deepseek-flash", "workspace": "C:/w", "max_steps": 80,
            "tools": [], "permissions": {}, "audit_path": "x", "notices": [],
        }))
        await _settle(app, pilot)

        app.submit("/model")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker), "不弹空面板"
        assert "没给模型清单" in _log_text(app)

        app.submit("/effort")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker)
        assert "没给档位清单" in _log_text(app)


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


# --- `/mcp` ------------------------------------------------------------------

MCP_SNAPSHOT = {
    "v": 1, "t": "ui", "kind": "mcp",
    "mcp_servers": [
        {"name": "github", "state": "loaded", "tools": 12, "error": "",
         "where": "npx -y @modelcontextprotocol/server-github"},
        {"name": "kb", "state": "unload", "tools": 0, "error": "",
         "where": "https://kb.example.com"},
        {"name": "broken", "state": "failed", "tools": 0,
         "error": "McpError: 起不来 npx", "where": "npx -y broken"},
    ],
    "mcp_notes": ["[MCP] 当前挂载情况（配置里改了要重启才生效）"],
}


def test_the_mcp_rows_spell_out_the_three_states():
    """三种状态必须是**三行不同的话**，而且 `failed` 带着原因。

    合成两种的话，"我没开它"和"我开了、它坏了"长得一样 —— 而这两种情况下一步完全
    不同（一个是按一下，一个是去看原因）。这条也钉住"原因不许被吞掉"。
    """
    rows = {row["name"]: row for row in MCP_SNAPSHOT["mcp_servers"]}
    lines = {name: str(view_state.mcp_line(row, 8)) for name, row in rows.items()}

    assert "12 个工具" in lines["github"]
    assert "未加载" in lines["kb"]
    assert "没连上" in lines["broken"] and "起不来 npx" in lines["broken"]
    assert "未加载" not in lines["broken"], "失败不能说成未加载"


def test_the_mcp_stream_lines_keep_the_runtimes_own_words():
    """会话流里那几行：一句概况 + **runtime 拼的后果原样贴**。

    清单本身在面板和左栏，这里不重画（那份清单会随对话滚走，而"我挂了哪几个"是
    随时想再看一眼的东西）。所以这条钉的是"概况的口径"和"那句话没被改写"。
    """
    lines = [str(line) for line in view_state.render_mcp(MCP_SNAPSHOT)]
    text = "\n".join(lines)

    assert "1 个在跑 / 共 3 个" in text
    assert "[MCP] 当前挂载情况（配置里改了要重启才生效）" in text
    # 一条都没有时返回空：系统自己发的快照不该在流里留噪。
    assert view_state.render_mcp({"mcp_servers": [], "mcp_notes": []}) == []


@pytest.mark.anyio
async def test_the_mcp_command_opens_a_panel_and_toggling_asks_the_runtime(monkeypatch):
    """`/mcp` 弹面板；在面板里按一下 = **发一条请求**（面板不关、也不乐观更新）。

    三条一起看才算数：命令打开了面板、面板里那一下发的是 `t:"mcp"`（带着选中的
    那个名字和"该挂还是该卸"）、以及回包之后面板就地重画（不重开、不关掉）。

    方向由**runtime 给的状态**决定，不由用户按了什么键决定 —— 所以"在跑的那一行
    按一下是卸载、没在跑的那一行按一下是挂载"，而 `failed` 那一档落进"挂载"（也就是
    重试）。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", MCP_SNAPSHOT))
        await _settle(app, pilot)
        assert [row["name"] for row in app.state.mcp] == ["github", "kb", "broken"]

        app.submit("/mcp")
        await _settle(app, pilot)
        panel = app._mcp_panel()
        assert panel is not None, "`/mcp` 不带参数要弹面板"
        assert app._client.sent == [], "开面板本身不发请求"

        # 第一行是 github（在跑）→ 那一下是卸载。
        panel.action_toggle()
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "unload",
                                        "servers": ["github"]}

        # 回包到了：面板**还在**（不关），而且它读的是新状态。
        app._inbox.put(("message", {
            **MCP_SNAPSHOT,
            "mcp_servers": [
                {"name": "github", "state": "unload", "tools": 0, "error": "",
                 "where": "npx -y x"},
                {"name": "kb", "state": "loaded", "tools": 3, "error": "",
                 "where": "https://kb.example.com"},
                {"name": "broken", "state": "failed", "tools": 0,
                 "error": "McpError: 起不来 npx", "where": "npx -y broken"},
            ],
            "mcp_notes": ["[MCP] server github 卸下了（摘掉 12 个工具）"],
        }))
        await _settle(app, pilot)
        assert app._mcp_panel() is not None
        assert app.state.mcp[0]["state"] == "unload"
        assert "卸下了" in _log_text(app), "runtime 那句话要进会话流"

        # 现在第一行是"没在跑"→ 那一下是挂载。
        app._mcp_panel().action_toggle()
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "load",
                                        "servers": ["github"]}


@pytest.mark.anyio
async def test_the_mcp_command_with_an_argument_asks_directly(monkeypatch):
    """带参数那种写法不弹面板，直接发请求 —— 两条路走的是**同一个出口**。

    （CLI 那一侧只有这一种写法，所以它们必须不可能漂。）
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app.submit("/mcp load github")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "load",
                                        "servers": ["github"]}

        # 认不出来的写法：说一句，并且**把面板打开**（那是"下一步该干什么"最省事的
        # 回答），而**不发请求**。
        before = len(app._client.sent)
        app.submit("/mcp 全部打开")
        await _settle(app, pilot)
        assert "认不出这个写法" in _log_text(app)
        assert len(app._client.sent) == before


@pytest.mark.anyio
async def test_the_mcp_panel_answers_to_real_keystrokes(monkeypatch):
    """面板的**键位**：`↑↓` 移动、`Enter` 开关选中的那个、`Esc` 只关不做别的。

    ## 为什么这条要按真键

    上一条走的是 `action_toggle()`（直调），它验的是"按下去该发什么"；而**键怎么
    路由到这个面板**是另一件事 —— `McpPanel` 用的是 `on_key` 而不是 `BINDINGS`
    （理由和 `QuestionPanel` / `SessionPicker` 一样：没有焦点在可编辑控件上，键直接
    落到 screen 上；走绑定反而要和 `App` 那一层的 `↑↓`（翻会话流）抢）。这条路
    只在真按键下才成立，而它坏掉的样子是"面板开着、按什么都没反应"。

    `Esc` 那一条尤其要钉：开关是**立刻生效**的，所以这个面板没有"取消"这个概念 ——
    关掉它不会把已经挂上的卸下来（那会是一个很难发现的假象：看着像撤销了）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", MCP_SNAPSHOT))
        await _settle(app, pilot)

        app.submit("/mcp")
        await _settle(app, pilot)
        panel = app._mcp_panel()
        assert panel is not None

        # 开局选中的是第一行（github，在跑）。
        rows = list(panel.query(".option"))
        assert str(rows[0].render()).startswith("▌")
        assert "● github" in str(rows[0].render())
        assert "12 个工具" in str(rows[0].render())

        # `↓` 走一行：选中 kb（没在跑），而**整块重画了**（不是只有底色在动）。
        await pilot.press("down")
        await _settle(app, pilot)
        rows = list(panel.query(".option"))
        assert str(rows[1].render()).startswith("▌")
        assert "○ kb" in str(rows[1].render())
        assert "12 个工具" in str(rows[0].render()), "非选中行也要有内容"

        # `Enter` = 开关选中的那个。kb 没在跑 → 挂载。
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "load",
                                        "servers": ["kb"]}
        # 等回包的这一段，面板上要有字（否则起子进程那几秒看起来像没按到）。
        assert "正在等 runtime" in str(panel.query("#mcp-foot").first().render())
        assert app._mcp_panel() is not None, "开关之后面板不关"

        # 空格和 Enter 是一回事（两个键都合手）。
        await pilot.press("space")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "load",
                                        "servers": ["kb"]}

        # `↑` 回到第一行：现在是 github（在跑）→ 那一下是卸载。
        await pilot.press("up")
        await _settle(app, pilot)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "mcp", "action": "unload",
                                        "servers": ["github"]}

        # `Esc` 只关面板：**不发任何请求**（"取消"这个概念在这里不存在）。
        before = len(app._client.sent)
        await pilot.press("escape")
        await _settle(app, pilot)
        assert app._mcp_panel() is None
        assert len(app._client.sent) == before


def test_the_rail_shows_only_the_servers_that_are_running():
    """左栏那块**只列在跑的**，右侧计数是 `在跑的 / 配置里的总数`。

    "配了哪几个但没开"是 `/mcp` 面板要回答的 —— 放进左栏只会让"这一栏里到底有几个
    东西是活的"变得要数一遍才知道。而分母留着，"配了三个只挂上一个"就不会看着像坏了。
    """
    state = view_state.ViewState()
    empty = view_state.rail_blocks(state)[-1]
    assert empty[0] == "后台 MCP" and empty[1] == ""
    assert "当前没有挂载" in str(empty[2][0])

    # **`ui(mcp)` 和 `ui(state)` 里的那一格名字不同，而且这是刻意的**：前者是
    # `mcp_servers`（"这一条消息的主题就是它"），后者是 `mcp`（"这一屏里的一格"）。
    # 界面按前者填 state，左栏读后者 —— 所以这条测试走的是界面那条路。
    state.mcp = [dict(row) for row in MCP_SNAPSHOT["mcp_servers"]]
    title, count, lines = view_state.rail_blocks(state)[-1]
    text = "\n".join(str(line) for line in lines)
    assert title == "后台 MCP" and count == "1 / 3"
    assert "github" in text and "12 个工具" in text
    # 没在跑 / 没连上的那两条**不在这块**（它们在面板里）。
    assert "kb" not in text and "broken" not in text


def test_the_collapsed_rail_summary_counts_mounted_servers():
    """收起左栏之后那一行摘要里也报在跑几个（和左栏同一口径）。"""
    state = view_state.ViewState()
    assert "MCP" not in view_state.rail_summary(state)
    state.mcp = [dict(row) for row in MCP_SNAPSHOT["mcp_servers"]]
    assert "1 个 MCP server" in view_state.rail_summary(state)
