"""TUI 前端。

两层分开测，因为它们能测到的程度差得很远：

  1. **`view_state.py` 的纯函数** —— 事件怎么变成给人看的行。这是这个界面里最值得
     测的部分（全是判断，没有布局），而且它**不需要 Textual**；
  2. **`TuiApp` 的骨架** —— 用 Textual 自己的测试台（`run_test`）：能挂载、`init`
     能画出来、斜杠命令有反应。**布局好不好看测不了**，所以不去测它。
"""

import asyncio
import time

import pytest

from agent_runtime.frontends.tui import theme as theme_mod
from agent_runtime.frontends.tui import view_state
from agent_runtime.protocol import state as agent_state
from agent_runtime.runtime.composition import DEFAULT_MAX_STEPS


def _think_lines(turn) -> list:
    """回合里那些**思考过程的行**（折叠的块头，或者铺开的块头）。

    **判据是那行文字里有"思考过程"，不能只看 `ROLE_THINK_HEAD`** —— 回合头底下那行
    `  > 用户说的话` 用的是同一个 role（它和思考块头一样，都是"不抢眼的引导行"）。
    只看 role 的话，每一条断言都会拿到用户在回合里说的第一句话，而失败信息看起来
    完全无关（实测踩过）。
    """
    out = []
    for chunk in turn.chunks:
        for line in getattr(chunk["block"], "lines", []):
            if "思考过程" in str(line):
                out.append(line)
    return out


def _think_blocks(turn) -> list:
    """回合里那些**思考块**（折着或铺着，判据同上）。"""
    return [chunk for chunk in turn.chunks
            if any("思考过程" in str(line)
                   for line in getattr(chunk["block"], "lines", []))]


# `run_test()` 是 async 的，而项目里**不装 pytest-asyncio** —— anyio 的 pytest 插件
# 已经够用（它是 textual 的传递依赖，不额外增加负担）。anyio 默认会跑所有后端
# （包括 trio，而 trio 没装），所以钉死成 asyncio。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- 第一层：纯函数 -----------------------------------------------------------

def test_clip_reports_whether_it_truncated():
    """它只回答"截没截"，不加省略号 —— 加什么是调用方的排版决定。"""
    assert view_state.clip("abc", 10) == ("abc", False)
    text, cut = view_state.clip("a" * 10, 4)
    assert text == "aaaa" and cut is True
    # limit <= 0 表示"不截"
    assert view_state.clip("a" * 100, 0) == ("a" * 100, False)


def test_indent_keeps_every_line_inside_the_box():
    assert view_state.indent("a\nb", "| ") == "| a\n| b"


def test_run_started_shows_what_the_user_said():
    state = view_state.ViewState()
    lines = view_state.render_event(state, {"kind": "run_started", "user_input": "你好"})
    assert any("你好" in line for line in lines)


def test_a_denied_tool_says_it_did_not_run():
    """**拒绝和出错必须分开说。**

    两者在 `chars` 上看不出来（都是 0 附近），而"没执行"和"执行了但坏了"是
    完全不同的事 —— 用户据此决定要不要换个做法。
    """
    state = view_state.ViewState()
    lines = view_state.render_event(state, {
        "kind": "tool_result", "tool": "shell", "status": "denied",
        "chars": 0, "duration_ms": 1,
    })
    assert any("没有执行" in line for line in lines)


def test_step_limit_looks_different_from_answered():
    """**这是 `StepLimitExceeded` 那个类存在的全部理由。**

    不许让人分不清"答完了"和"被砍断了" —— 所以两边的行数/措辞都必须不同。
    """
    state = view_state.ViewState()
    answered = view_state.render_event(state, {"kind": "run_finished",
                                               "stop_reason": "answered"})
    limited = view_state.render_event(state, {"kind": "run_finished",
                                              "stop_reason": "max_steps"})
    assert answered and limited
    assert len(limited) > len(answered)
    assert any("没有" in line and "收尾" in line for line in limited)
    assert not any("收尾" in line for line in answered)


def test_thinking_is_collapsed_by_default_and_the_count_is_ours():
    """决策 17：思维链默认折叠成一行。

    字符数**由前端 `len()` 出来** —— 不让子进程多发一个计数字段（那是同一份事实的
    第二个来源，两侧的口径未必一致）。
    """
    state = view_state.ViewState()
    message = {"kind": "model_call", "status": "ok", "run_id": "r1",
               "reasoning": "想" * 300, "duration_ms": 5}
    lines = view_state.render_event(state, message)

    assert any("思考过程（300 字符" in line for line in lines)
    assert not any("想" * 10 in line for line in lines), "默认不许铺开"
    assert state.thinking["r1"] == ("想" * 300, False)


def test_toggling_shows_the_whole_thinking_text():
    """展开之后**一字不差、不截断** —— 它别处看不到（审计里有，但界面要能读）。"""
    state = view_state.ViewState()
    message = {"kind": "model_call", "status": "ok", "run_id": "r1",
               "reasoning": "想" * 300, "duration_ms": 5}
    view_state.render_event(state, message)
    state.toggle_thinking("r1")

    lines = view_state.render_event(state, {**message, "reasoning": "想" * 300})
    assert any("想" * 300 in line for line in lines)


def test_the_answer_is_recorded_by_run_id_not_by_arrival_order():
    """`event(run_finished)` 和 `ui(run_finished)` 是**两条**消息，顺序不保证。

    所以配对只能靠 `run_id`。靠到达顺序的话，一旦两条消息的次序变了（这在
    工作线程 + 主循环之间是可能的），界面会把答案贴到错误的一轮上 ——
    而那看起来完全正常。

    **它交出去的是原文，不是行**（`Answer`）：正文要按 Markdown 渲染，拆成行就等于
    把语法丢掉。所以这里能钉的只有"给什么还什么"和下面那条"空答案不画"。
    """
    state = view_state.ViewState()
    answer = view_state.answer_body(state, {"run_id": "r9", "answer": "# 答案"})
    assert answer is not None and answer.text == "# 答案"
    assert state.answers["r9"] == "# 答案"

    # 第二次同 run_id（重放）覆盖，而不是追加出第二条。
    view_state.answer_body(state, {"run_id": "r9", "answer": "答案2"})
    assert state.answers["r9"] == "答案2"


def test_an_empty_answer_draws_nothing():
    """模型失败时 `answer` 是空串 —— 那时**不该**画一个空的 `[agent]` 气泡。"""
    state = view_state.ViewState()
    assert view_state.answer_body(state, {"run_id": "r", "answer": ""}) is None


def test_the_autopilot_badge_always_says_which_state_it_is_in():
    """两个状态都写在那一格里，**不做成"开着才显示"**。

    只显示"开"的话，"这一格空着"既可能是关掉了、也可能是没画出来 —— 而这一格的
    语义是"接下来还会不会问你"，它不允许有歧义。

    窄屏换短词：状态栏右边是 `width: auto`，多出来的每一列都是从**左段**身上扣的，
    而左段被裁成半句正是 F5 那次踩过的坑。
    """
    state = view_state.ViewState()
    assert str(state.autopilot_badge()) == "自动放行 关"
    state.autopilot = True
    assert str(state.autopilot_badge()) == "自动放行 开"
    assert str(state.autopilot_badge(compact=True)) == "放行 开"


def test_apply_state_only_believes_a_real_true_for_autopilot():
    """`ui state` 里那个 `autopilot` 也只认真正的 `true`（和 runtime 收请求时一致）。

    两边同一条规矩，因为猜错的方向是"不问就执行"。
    """
    state = view_state.ViewState()
    view_state.apply_state(state, {"kind": "state", "autopilot": "true"})
    assert state.autopilot is False

    view_state.apply_state(state, {"kind": "state", "autopilot": True})
    assert state.autopilot is True


def test_status_bar_left_is_a_projection_not_a_second_truth():
    """状态栏左边完全由 `agent.state` + 会话规模推出来。

    这里只钉一件事：**`activity` 说的是"最近发生了什么"**，不是界面自己编的词。
    没有流式，"模型在想"和"工具在跑"分不出更细的粒度 —— 硬分只能用时间间隔去猜。
    """
    state = view_state.ViewState(session_id="s1", model="m",
                                 max_steps=DEFAULT_MAX_STEPS)
    assert "空闲" in state.status_left()

    state.agent = agent_state.reduce(
        agent_state.initial(), {"t": "event", "kind": "run_started", "step": 0}
    )
    assert "准备中" in state.status_left()

    # **`step` 只在 `run_started` 上更新**，后面的事件带的是同一个 step。
    # 而 step 的语义是"第几次模型往返"，`run_started` 那条是 **0** ——
    # 所以真实的第 2 步长这样（实测踩过：我第一版在这里传了 step=1，
    # 于是断言写成 `第 2/N 步` 而实际是 `第 1/N 步`）。
    #
    # `N` 跟着 `DEFAULT_MAX_STEPS` 走，**不写死**：那个数从 80 抬到 120 那一次，
    # 就是这两条写死 80 的断言在挡路，而它们真正要钉的是"分子分母各自的来源"，
    # 不是某一次定下来的值。
    state.agent = agent_state.reduce(state.agent, {
        "t": "event", "kind": "run_started", "step": 0,
    })
    state.agent = agent_state.reduce(state.agent, {
        "t": "event", "kind": "tool_call", "step": 1, "tool": "read_file",
        "tool_index": 1,
    })
    line = state.status_left()
    assert "read_file" in line and "第 2 个" in line
    assert f"第 1 / {DEFAULT_MAX_STEPS} 步" in line


def test_status_bar_right_shows_context_cache_and_audit():
    """右边那四个数**此前只有"回合结束时那一条统计"这一条出口** —— 也就是只有
    跑完才看得见。放进常驻状态栏是设计稿的加法之一。

    占比的分母来自 `init.context_tokens`（响应里没有这个字段，见那里的说明）。
    """
    state = view_state.ViewState(
        context_tokens=1_000_000, audit_path=r"C:\w\.tudouni\logs\s.jsonl")
    assert "上下文  —" in state.status_right()

    state.prompt_tokens = 14_100
    state.cached_tokens = 12_400
    right = state.status_right()
    assert "14.1k / 1M" in right, right
    assert "1.4%" in right
    assert "命中 88%" in right, right
    # 左栏和状态栏报的是审计**目录**（F2 那一行写的就是 `.tudouni/logs`）：
    # 文件名就是会话 id，而它已经在同一块的上一行写着，32 列的栏里再抄一遍会折成三行。
    assert "审计 .tudouni/logs" in right, right

    # 窄屏（`compact`）只留用量和命中率 —— 否则左段会被裁成半句。
    narrow = state.status_right(compact=True)
    assert "14.1k / 1M" in narrow and "命中 88%" in narrow
    assert "审计" not in narrow and "本轮" not in narrow


def test_no_denominator_when_the_model_is_not_in_the_table():
    """**错的百分比比没有百分比更坏**（和 cli 那条口径一致）。

    `init.context_tokens` 为 None 时只报用量 —— 不猜一个分母。
    """
    state = view_state.ViewState(prompt_tokens=2_000, context_tokens=None)
    right = state.status_right()
    assert "上下文 2k" in right
    assert "%）" not in right
    assert "/" not in right.split("·")[0]


def test_the_jobs_block_says_what_is_still_hanging():
    """左栏那块后台任务：**四档各有各的说法**，而"结果还没收"必须自己说出来。

    它是这一栏里唯一对应着**活着的进程**的一块（任务列表过期只是信息旧，而后台任务
    过期意味着"我以为已经收掉的服务还在占着端口"）。所以：

      * 空态说清它是谁弄出来的（`shell_background`）；
      * 右侧计数数的是**还没收场的条数**，不是"一共起过几条" —— 那一块要回答的问题
        是"还有几件事悬着"；
      * `uncollected`（跑完了、结果还没收）用 `waiting` 那档色，因为它是**唯一需要
        动手的一档**。
    """
    state = view_state.ViewState(session_id="s")
    title, count, lines = view_state.rail_blocks(state)[4]
    assert (title, count) == ("后台任务", "")
    assert [str(line) for line in lines] == [
        "当前没有后台任务", "shell_background 起的会在这里"]

    state.jobs = [
        {"id": "1", "command": "npm run dev", "state": "running",
         "seconds": 45, "exit_code": None},
        {"id": "2", "command": "pytest -q", "state": "uncollected",
         "seconds": 12, "exit_code": 0},
        {"id": "3", "command": "echo hi", "state": "done",
         "seconds": 1, "exit_code": 0},
    ]
    title, count, lines = view_state.rail_blocks(state)[4]
    assert count == "2 / 3", "悬着的两条，而不是起了三条"
    text = "\n".join(str(line) for line in lines)
    assert "npm run dev" in text and "在跑 45s" in text
    assert "结果还没收" in text
    assert "已收" in text
    # 只有"结果还没收"那一档抢眼睛。
    roles = [role for line in lines for _text, role in line.segments]
    assert roles.count(view_state.ROLE_WAITING) == 1


def test_the_rail_summary_counts_outstanding_jobs(monkeypatch):
    """收起上下文栏时那一行摘要也得说 —— 它是**窄屏上唯一还提这件事的地方**。"""
    state = view_state.ViewState(session_id="s")
    assert "后台" not in view_state.rail_summary(state)

    state.jobs = [{"id": "1", "command": "npm run dev", "state": "running",
                   "seconds": 3, "exit_code": None}]
    assert "1 个后台任务" in view_state.rail_summary(state)

    # 全收干净了也要留一句：**"起过又收干净了"和"从来没起过"是两件事**。
    state.jobs = [{"id": "1", "command": "echo hi", "state": "done",
                   "seconds": 1, "exit_code": 0}]
    assert "都收过了" in view_state.rail_summary(state)


def test_the_jobs_badge_is_the_one_that_survives_a_collapsed_rail():
    """状态栏那枚徽标：**一件都不悬着时一格都不占**，悬着时它必须说清几件。

    它存在的理由就是"上下文栏默认收起"—— 那个徽标是收起时唯一常驻的出口（宽屏上
    连那一行摘要都不显示，见 `app._refresh_chrome`）。
    """
    state = view_state.ViewState(session_id="s")
    assert state.jobs_badge() is None, "没有后台任务时不占格子"

    state.jobs = [{"id": "1", "command": "npm run dev", "state": "running",
                   "seconds": 3, "exit_code": None}]
    badge = state.jobs_badge()
    assert str(badge) == "后台 1"
    assert badge.role == view_state.ROLE_PROCESS, "只在跑是正常状态，不该喊"

    state.jobs.append({"id": "2", "command": "pytest -q", "state": "uncollected",
                       "seconds": 9, "exit_code": 0})
    badge = state.jobs_badge()
    assert str(badge) == "后台 2 · 1 条待收"
    assert badge.role == view_state.ROLE_WARN, "待收那一档是有人在等一个动作"

    # 窄屏只留条数：右边是 `width: auto`，多出来的每一列都从左段身上扣。
    assert str(state.jobs_badge(compact=True)) == "后台 2"

    state.jobs = [{"id": "1", "command": "echo hi", "state": "done",
                   "seconds": 1, "exit_code": 0}]
    assert str(state.jobs_badge()) == "后台 1 已收"


def test_apply_state_takes_jobs_and_reset_clears_them():
    """后台任务那份面板数据**按会话走**，所以换会话时必须清掉。

    漏了它的症状是最坏的一种：面板上留着一个**进程已经死了**的服务在"跑"
    （上一个会话收尾时 `Runtime.close()` 把它杀了），而用户会照着它去查一个假问题。
    """
    state = view_state.ViewState(session_id="s")
    message = {
        "kind": "state",
        "jobs": [{"id": "1", "command": "npm run dev", "state": "running",
                  "seconds": 2, "exit_code": None}],
    }
    view_state.apply_state(state, message)
    assert state.jobs == message["jobs"]
    # **存的是副本**：那份消息是读线程放进来的，留着它的引用等于把界面状态和
    # 一条已经处理完的消息绑在一起（`apply_state` 对别的列表也是这么做的）。
    assert state.jobs[0] is not message["jobs"][0]

    state.reset_for_session()
    assert state.jobs == []
    assert state.jobs_badge() is None


@pytest.mark.anyio
async def test_the_background_badge_really_reaches_the_status_bar(monkeypatch):
    """徽标**真的画进状态栏了** —— `jobs_badge()` 返回一句话不等于它被渲染。

    这条盯的是那句承诺本身："上下文栏收起时也看得见后台任务"。宽屏 + 收起时，
    状态栏是**唯一**的出口（窄屏那一行摘要要 `narrow and not rail_open` 才显示）。
    少接一根线，症状是"有任务在跑而界面上一个字都没有"，不会报任何错。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)

        bar = app.query_one("#status", widgets_module.StatusBar)
        _left, right = bar.render_parts(app.state, app.palette, (time.time(), 140))
        assert "后台" not in str(right), "没有后台任务时一格都不占"

        # 面板默认是收起的 —— 这正是这一条要验的前提。
        assert app.state.rail_open is False

        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "state",
            "jobs": [{"id": "1", "command": "npm run dev", "state": "running",
                      "seconds": 12, "exit_code": None}],
        }))
        await _settle(app, pilot)

        _left, right = bar.render_parts(app.state, app.palette, (time.time(), 140))
        text = str(right)
        assert "后台 1" in text
        assert text.index("后台 1") < text.index("上下文"), \
            "挨着成本那一段的左边（和 autopilot 徽标同一档：状态在前、账在后）"


@pytest.mark.anyio
async def test_the_tui_asks_for_a_fresh_snapshot_while_a_job_is_outstanding(monkeypatch):
    """后台任务会在**没人在看的时候**结束 —— 界面得自己去问一次。

    `ui(state)` 只在几条由交互触发的时刻发，所以一段安静时间里一条命令跑完了，
    面板上还写着"在跑"：那句话是假的，方向和"把已启动当成已成功"相反，但同样是
    "界面上写着的事实不成立"。

    而它**只在真有东西悬着时才问**，还要节流 —— 没有理由的轮询会把"协议上每条消息
    都有原因"这件事稀释掉，也把这条消息变成心跳。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)

        def polls():
            return [m for m in app._client.sent if m["t"] == "refresh_state"]

        assert polls() == [], "没有后台任务时一次都不问"

        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "state",
            "jobs": [{"id": "1", "command": "npm run dev", "state": "running",
                      "seconds": 3, "exit_code": None}],
        }))
        await _settle(app, pilot)
        assert polls(), "有东西悬着就该问一次"

        # 节流：紧接着的几帧（50ms 一次 pump）不该再问 —— 间隔是 2 秒。
        before = len(polls())
        await _settle(app, pilot)
        assert len(polls()) == before

        # 全都收干净了就不再问。**这里把节流放开**，好证明"不问"是因为没东西悬着，
        # 而不是因为还没到下一个间隔。
        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "state",
            "jobs": [{"id": "1", "command": "npm run dev", "state": "done",
                      "seconds": 3, "exit_code": 0}],
        }))
        await _settle(app, pilot)
        app._last_state_refresh = 0.0
        await _settle(app, pilot)
        assert len(polls()) == before


def test_the_permission_block_says_only_what_the_runtime_sent():
    """决策 14：**runtime 发什么显示什么**，界面不硬编码一份默认值。

    "哪几档自动放行"是 `PermissionPolicy` 的判断，由 runtime 算好放进
    `risk_scope` —— 界面照着渲染，它不该知道"默认只有 low"这件事。
    """
    state = view_state.ViewState(session_id="s")
    blocks = dict((title, lines) for title, _count, lines in view_state.rail_blocks(state))
    assert any("没报范围" in str(line) for line in blocks["权限范围"])

    state.risk_scope = [
        {"risk": "low", "disposition": "auto"},
        {"risk": "medium", "disposition": "ask"},
        {"risk": "high", "disposition": "ask"},
    ]
    blocks = dict((title, lines) for title, _count, lines in view_state.rail_blocks(state))
    text = "\n".join(str(line) for line in blocks["权限范围"])
    assert "low" in text and "自动放行" in text
    assert "medium" in text and "high" in text and "询问" in text
    # 决策 3：**只给 MEDIUM / HIGH 上色**，LOW 不着色。
    roles = {role for line in blocks["权限范围"] for _text, role in line.segments}
    assert view_state.ROLE_RISK_MEDIUM in roles and view_state.ROLE_RISK_HIGH in roles
    assert len(blocks["权限范围"]) == 3


def test_the_skill_block_counts_loaded_skills_only():
    """左栏那块说的是**已加载**（这个会话读过哪几份），不是**可用**（工作区里有几个）。

    这一对区分被当成 bug 报过一次，原话是"左栏写着 0，而 `Ctrl+S` 里明明列着技能"。
    两处的数据根本不是同一个：

      * `state.skills` ← `session.metadata`（`load_skill` 写下的指针，按会话）；
      * `state.skill_catalog` ← 工作区扫出来的全部技能（每次启动扫一遍）。

    所以"已加载 0 / 可用 1"是**正常的一个状态**（有货但还没读）。这条测试钉两件事：
    计数只数已加载、而且**空态不去解释"可用有几个"** —— 那一块每一行都该说自己那一块
    的事，可用清单的出口是 `Ctrl+S`（曾经在这里加过一句"工作区里有 N 个可用"，去掉了）。
    """
    # 工作区里有货、但还没读过任何一份：计数仍然是 0，而空态不提"可用"。
    state = view_state.ViewState(session_id="s",
                                 skill_catalog=[{"name": "frontend-design"}])
    title, count, lines = view_state.rail_blocks(state)[1]
    assert (title, count) == ("已加载技能", "0"), "计数数的是已加载，不是可用"
    assert [str(line) for line in lines] == [
        "还没有加载技能", "load_skill 读过的会一直生效"]

    # 工作区里也一个技能都没有 —— 空态**一模一样**（这就是"不解释可用"的直接后果）。
    empty = view_state.ViewState(session_id="s")
    _title, count, lines = view_state.rail_blocks(empty)[1]
    assert count == "0"
    assert [str(line) for line in lines] == [
        "还没有加载技能", "load_skill 读过的会一直生效"]

    # 读过之后：列名字、计数跟着走，可用清单里有几个无关。
    loaded = view_state.ViewState(
        session_id="s", skills=[{"name": "frontend-design"}],
        skill_catalog=[{"name": "frontend-design"}, {"name": "pdf"}])
    title, count, lines = view_state.rail_blocks(loaded)[1]
    assert (title, count) == ("已加载技能", "1")
    assert [str(line) for line in lines] == ["frontend-design"]


def test_the_empty_rail_blocks_do_not_name_internal_tools():
    """空态的两句话**不点名内部工具**，也不替运行时解释自己为什么是空的。

    "agent 调用 todo_write 后出现在这里"曾经在左栏里挂了很久 —— 它把实现细节
    （任务是哪个工具写的）写进了界面，而用户要的只是"这里会出现什么"。同一条规矩
    也用在那句"load_skill 读过的会一直生效"上：它说的是**效果**（读过的会留着），
    不是"哪个工具会调用它"。名字会变、工具会合并，而效果那句话不会。
    """
    state = view_state.ViewState(session_id="s")
    blocks = {title: [str(line) for line in lines]
              for title, _count, lines in view_state.rail_blocks(state)}
    assert blocks["任务"] == ["当前还没有任务", "agent 创建的任务会在这里"]
    assert blocks["已加载技能"] == ["还没有加载技能",
                                    "load_skill 读过的会一直生效"]
    assert "todo_write" not in "\n".join(blocks["任务"])


def test_the_rail_summary_only_mentions_skills_once_they_are_loaded():
    """收起那一行只说**已加载**了几个技能；一个没加载就完全不提技能。

    摘要那一行的职责是"说清收起之后少了什么"。没加载时说"可用 N 个"是在说另一块
    的事（而且它和左栏那一块的口径就对不上了）—— 见 `_skill_block` 的 docstring。
    """
    state = view_state.ViewState(session_id="s",
                                 skill_catalog=[{"name": "a"}, {"name": "b"}])
    assert "技能" not in view_state.rail_summary(state)

    state.skills = [{"name": "a"}]
    assert "1 个技能" in view_state.rail_summary(state)

    # 一个技能都没有：这一行不提技能，但别的那几段照旧。
    bare = view_state.ViewState(session_id="s", todos=[{"content": "x",
                                                        "status": "pending"}])
    summary = view_state.rail_summary(bare)
    assert "技能" not in summary and "0/1 个任务" in summary


# --- 工具行的语法（设计稿的核心改动 2） ---------------------------------------

def test_a_tool_line_carries_its_own_syntax_and_risk_colour():
    """`→ [1] read_file(...)` + 风险**靠颜色不靠文字**。

    LOW 不着色也**不写"低风险"**：`risk=low` 的工具占多数，写出来只是噪声。
    """
    state = view_state.ViewState()
    state.tool_risks = {"read_file": "low", "edit_file": "medium", "shell": "high"}

    low = view_state.render_event(state, {
        "kind": "tool_call", "tool": "read_file", "tool_index": 0,
        "arguments": "frontends/tui/view_state.py", "call_id": "c1"})
    medium = view_state.render_event(state, {
        "kind": "tool_call", "tool": "edit_file", "tool_index": 1,
        "arguments": "a.py", "call_id": "c2"})
    high = view_state.render_event(state, {
        "kind": "tool_call", "tool": "shell", "tool_index": 2,
        "arguments": "python -m pytest -q", "call_id": "c3"})

    assert "[1] read_file(frontends/tui/view_state.py)" in str(low[0])
    assert "风险" not in str(low[0]), "LOW 不许有文字标签"
    assert "MEDIUM 风险" in str(medium[0])
    assert "HIGH 风险" in str(high[0])

    def roles(line):
        return {role for _text, role in line.segments}

    assert view_state.ROLE_RISK_MEDIUM in roles(medium[0])
    assert view_state.ROLE_RISK_HIGH in roles(high[0])
    assert view_state.ROLE_RISK_HIGH not in roles(low[0])


def test_the_result_line_is_paired_by_call_id():
    """决策 4：`←` 用 **`call_id`** 配对，不靠事件顺序。

    并行批次里"顺序一致"是个实现细节而不是契约，一旦不对应就是"结果贴错调用"
    —— 看起来完全正常、实际全错的展示。
    """
    state = view_state.ViewState()
    view_state.render_event(state, {
        "kind": "tool_call", "tool": "read_file", "tool_index": 0,
        "arguments": "a.py", "call_id": "c1"})
    view_state.render_event(state, {
        "kind": "tool_call", "tool": "read_file", "tool_index": 1,
        "arguments": "b.py", "call_id": "c2"})

    # 结果**故意乱序**回来，而且事件里的 tool_index 是错的 —— 配对必须靠 call_id。
    lines = view_state.render_event(state, {
        "kind": "tool_result", "tool": "read_file", "call_id": "c2",
        "tool_index": 0, "status": "ok", "chars": 312, "duration_ms": 12})
    assert "[2]" in str(lines[0]), str(lines[0])
    assert "312 字符" in str(lines[0])


def test_a_turn_has_a_header_that_gets_a_final_form():
    """回合分隔线：**开头说"进行中"，结束改成"3 步 · 4.2s · 已答"**。

    设计稿的对话流是按回合分块的，而块与块之间必须有一条能一眼扫到的界。
    """
    state = view_state.ViewState()
    start = view_state.render_event(state, {
        "kind": "run_started", "run_id": "r1", "user_input": "你好"})
    assert start[0].role == view_state.ROLE_TURN_START
    assert "回合 1" in str(start[0]) and "进行中" in str(start[0])
    assert any("你好" in str(line) for line in start)

    for _ in range(3):
        view_state.render_event(state, {
            "kind": "model_call", "run_id": "r1", "status": "ok",
            "duration_ms": 1200, "prompt_tokens": 12400})
    end = view_state.render_event(state, {
        "kind": "run_finished", "run_id": "r1", "stop_reason": "answered",
        "duration_ms": 4200})
    assert end[0].role == view_state.ROLE_TURN_END
    assert "3 步" in str(end[0]) and "4.2s" in str(end[0]) and "已答" in str(end[0])


def test_cancel_is_reported_as_its_own_outcome():
    """被中断**必须**和"答完了"长得不一样 —— 和步数用尽同一个理由。"""
    state = view_state.ViewState()
    view_state.render_event(state, {"kind": "run_started", "run_id": "r1"})
    lines = view_state.render_event(state, {
        "kind": "run_finished", "run_id": "r1", "stop_reason": "cancelled",
        "duration_ms": 900})
    assert "已中断" in str(lines[0])
    assert any("停下" in str(line) for line in lines[1:])


# --- 上下文栏的开合（决策 1） -------------------------------------------------

def test_the_rail_opens_once_when_the_todo_list_appears():
    """决策 26：任务列表**从无到有**时顶开一次，之后听用户的（`Ctrl+B` 收得掉）。

    这里钉的是一条**边沿**，因为电平式（"有任务就开着"）会让 `Ctrl+B` 彻底失效：
    用户收起之后 50ms，下一次刷新又把它顶回来 —— 界面上看起来就是"这个键坏了"。
    """
    state = view_state.ViewState()
    assert view_state.should_auto_open(state) is False, "默认收起"

    # 从无到有 → 顶开一次。**不管之前手动收起过没有**：那个决定的前提是"那时没任务"。
    state.rail_pinned = True
    state.todos = [{"content": "写测试", "status": "in_progress"}]
    assert view_state.should_auto_open(state) is True

    # 用户按 Ctrl+B 收起：同一批任务还在，但那次顶开已经用掉了 → 不再顶回来。
    state.rail_open = False
    assert view_state.should_auto_open(state) is False

    # 列表**内容更新**不算新事件（长任务里 todo_write 会被调很多次，每次都顶开会变成
    # 一个自己弹开的栏）。
    state.todos = [{"content": "写测试", "status": "completed"}]
    assert view_state.should_auto_open(state) is False

    # 技能也不算新事件：它不改变"有没有活要干"。
    state.todos = []
    state.skills = [{"name": "x", "digest": "d"}]
    assert view_state.should_auto_open(state) is False

    # 清空之后重新武装：下一次出现又是新事件。
    state.skills = []
    state.todos = [{"content": "写文档", "status": "pending"}]
    assert view_state.should_auto_open(state) is True


def test_the_rail_summary_says_what_collapsing_hides():
    """窄屏降级那一行（F5）：收起之后少了什么必须说出来，否则"收起"就是"看不见"。"""
    state = view_state.ViewState(risk_scope=[
        {"risk": "low", "disposition": "auto"},
        {"risk": "medium", "disposition": "ask"}])
    state.todos = [{"content": "a", "status": "completed"},
                   {"content": "b", "status": "pending"}]
    state.skills = [{"name": "tui-design", "digest": "x"}]
    summary = view_state.rail_summary(state)
    assert "Ctrl+B" in summary
    assert "1/2 个任务" in summary and "1 个技能" in summary and "medium 询问" in summary


def test_the_todo_block_counts_and_marks_each_item():
    state = view_state.ViewState(todos=[
        {"content": "确认现状", "status": "completed"},
        {"content": "写工具行语法", "status": "in_progress"},
        {"content": "补文档", "status": "pending"},
    ])
    title, count, lines = view_state.rail_blocks(state)[0]
    assert title == "任务" and count == "1 / 3"
    text = "\n".join(str(line) for line in lines)
    assert "✓ 确认现状" in text and "◐ 写工具行语法" in text and "○ 补文档" in text


# --- 配色与命令面板（纯数据那一半） -------------------------------------------

def test_there_are_thirteen_themes_and_the_default_one_is_graphite_amber():
    """留下来的五套色卡（P3/P5/P6/P7/P9）+ 设计稿 F7 的五套（A–E）+ 三套透明版。

    色卡里 `P1 暖橄榄 / P2 海蓝橙 / P4 暖光 / P8 藏青陶土` 四套是用户裁掉的：
    **key 不重排**（留下来的号一个没改），而展示序号是列表位置算出来的 ——
    所以 `/theme 1` 现在落到 `P3` 上、`/theme 6` 才是默认那套 `A`、`/theme 13` 是
    最后追加的那套深透明 `A-T2`。
    """
    assert len(theme_mod.ORDER) == 13
    assert theme_mod.ORDER == ("P3", "P5", "P6", "P7", "P9",
                               "A", "B", "C", "D", "E", "P3-T", "A-T", "A-T2")
    assert theme_mod.DEFAULT_THEME == "A"
    palette = theme_mod.get("A")
    assert palette.name == "石墨琥珀"
    assert palette.accent == "#E0A83E"
    # ⑦ 靛夜还在，只是不再是启动默认 —— 它的 accent 仍然是提亮过的那个值
    # （原色 #463DE8 在 #161616 上只有 2.3:1）。
    indigo = theme_mod.get("P7")
    assert indigo.name == "靛夜"
    assert indigo.accent == "#7670EF"
    assert indigo.bg == "#161616"
    # 被裁掉的那四套真的没了（不是"只是从列表里藏起来"）。
    for gone in ("P1", "P2", "P4", "P8"):
        assert gone not in theme_mod.THEMES
        assert theme_mod.resolve(gone) is None


def test_the_two_clear_variants_only_change_the_background():
    """`A-T` / `P3-T`：**除了 `bg` 之外**每一格都和原版一模一样。

    这条是这两套的全部承诺 —— 透明版不是第十一套配色，是同一套配色少涂一层。
    所以按住"九个 token 逐格相等"来断言，而不是只看一眼颜色差不多。

    `bg` 是 `ansi_default`（终端自己的底色），而派生角色**按原版的 `bg` 算**
    （`ansi_default` 不是一个能拿去插值的色值）：左栏、思考块、命令面板全都跟原版
    对齐 —— 全透的话这些块就没了。

    **深一档那套（`A-T2`）不在这条里**：它多交了一格（`chrome`），见下一条。
    """
    for base_key, clear_key in (("A", "A-T"), ("P3", "P3-T")):
        base, clear = theme_mod.get(base_key), theme_mod.get(clear_key)
        assert clear.palette.transparent is True
        assert base.palette.transparent is False
        assert clear.bg == theme_mod.ANSI_DEFAULT
        assert base.bg.startswith("#")
        # "只透最底下那一层"的数据形态：`clear_roles` 是空的。
        assert clear.palette.clear_roles == ()
        for attr in ("chrome", "surface", "line", "ink", "ink2", "ink3",
                     "accent", "warn", "danger", "ok", "dark"):
            assert getattr(clear, attr) == getattr(base, attr), (clear_key, attr)
        # 派生角色也逐格跟着原版（"只有最底下那一大片不同"）。
        for attr in ("rail", "elevated", "sunk", "hairline", "ink4",
                     "accent_soft", "danger_soft", "skill", "rail_bar"):
            assert getattr(clear, attr) == getattr(base, attr), (clear_key, attr)
        # CSS 里那一格确实是"终端自己的底色"，而不是一个近似色。
        assert clear.variables()["td-bg"] == theme_mod.ANSI_DEFAULT
        # 名字和出处看得出它是谁的透明版（`/theme` 列表里两套并排站着）。
        assert clear.name == f"{base.name} · 透明"
        assert "透明版" in clear.palette.source
        assert clear.palette.name_en.endswith("Clear")


def test_the_deep_clear_variant_also_hands_the_bars_to_the_terminal():
    """`A-T2 石墨琥珀 · 深透明`：**在 `A-T` 之上再交 `chrome` / `surface` 两格**。

    ## 这条盯的是"深一层"到底深在哪

    用户点名的那一套：`A-T` 只有对话区那一片底是终端的，而屏幕上最显眼的其实是
    两处实色 —— 顶栏 / 会话头 / 状态栏 / 输入框那四条色带（`chrome`），以及开场那
    三个方块「开始 / 最近 / 提示」（`surface`）。所以这里按**两件事**断言，两件都必要：

      1. 它和 `A-T` 的差别**只有那两格** —— 别的 token（`line` / `ink` / `sunk` / …）
         连同派生角色逐个相等。**框线照旧是主题色**（`line` / `hairline`，输入框上下
         那两条 `accent`、那三个框的圆角边也是 `accent`），文字照旧是琥珀那套；
      2. 那两格**真的是 `ansi_default`**（不是抄了一个近似的深色）—— 一路走到
         `$td-chrome` / `$td-surface`，横栏、三个框于是和对话区连成一整块。

    为什么盯 `A-T` 而不是盯 `A`：这一套是从浅的那套派生的（`_transparent(A-T, …)`），
    所以"差了哪几格"这句话只有在"以 `A-T` 为基准"时才说得准。
    """
    light = theme_mod.get("A-T")
    deep = theme_mod.get("A-T2")
    assert deep.palette.transparent is True
    assert deep.bg == theme_mod.ANSI_DEFAULT
    assert deep.chrome == theme_mod.ANSI_DEFAULT
    assert deep.surface == theme_mod.ANSI_DEFAULT
    # (1) 除了交出去的那两格，一格都不差 —— 浅的那套自己多透的格子也照旧继承下来。
    assert deep.palette.clear_roles == ("chrome", "surface")
    assert light.palette.clear_roles == ()
    for attr in ("line", "ink", "ink2", "ink3",
                 "accent", "warn", "danger", "ok", "dark"):
        assert getattr(deep, attr) == getattr(light, attr), attr
    for attr in ("rail", "elevated", "sunk", "hairline", "ink4",
                 "accent_soft", "danger_soft", "skill", "rail_bar"):
        assert getattr(deep, attr) == getattr(light, attr), attr
    # 它和原版 `A` 的色值血缘也还在（派生角色的基准色是原版的 `bg`，没被浅的那套
    # 的 `ansi_default` 覆盖掉）。
    base = theme_mod.get("A")
    for attr in ("rail", "elevated", "sunk", "hairline", "ink4"):
        assert getattr(deep, attr) == getattr(base, attr), attr
    assert deep.palette.base_bg == base.bg
    # (2) CSS 变量那两格是"终端自己的底色"，不是近似色。
    variables = deep.variables()
    assert variables["td-chrome"] == theme_mod.ANSI_DEFAULT
    assert variables["td-surface"] == theme_mod.ANSI_DEFAULT
    assert variables["td-bg"] == theme_mod.ANSI_DEFAULT
    # 横栏、开场那三个框的底和对话区于是**是同一个东西**（都是"别涂"）。
    assert variables["td-chrome"] == variables["td-bg"] == variables["td-surface"]
    # 框线那一格没有被顺手透掉（用户要的是"框线要主题色"）。
    assert variables["td-line"] == base.line
    assert variables["td-hairline"].startswith("#")
    # 名字 / 出处：列表里第三套透明版，看得出它是谁的、也看得出它更深一档。
    assert deep.name == "石墨琥珀 · 深透明"
    assert "透明版" in deep.palette.source
    assert deep.palette.name_en == "Graphite Amber · Deep Clear"


def test_every_theme_carries_all_roles():
    """九个 token 齐全，而派生角色确实**落在两个端点之间**（不是随手写的字面量）。"""
    for key in theme_mod.ORDER:
        palette = theme_mod.get(key)
        # 透明版允许把某几格**交给终端**（`ansi_default`）—— 但只允许它声明的那几格：
        # `bg` 是所有透明版都透的那一格，`clear_roles` 里是"还多透了哪几格"。
        # 声明之外的一格都不许"不是色值"，否则一次手滑就能把实色主题透掉一块。
        clear = set(palette.clear_roles)
        if palette.transparent:
            clear.add("bg")
        for attr in ("chrome", "surface", "line", "ink", "ink2", "ink3",
                     "accent", "warn", "danger", "ok", "bg"):
            value = getattr(palette, attr)
            if attr in clear:
                assert value == theme_mod.ANSI_DEFAULT, (key, attr, value)
            else:
                assert value.startswith("#") and len(value) == 7, (key, attr, value)
        # 只有透明版才准声明"交给终端"的格子（实色主题里那些格子必须是色值）。
        if not palette.transparent:
            assert palette.palette.clear_roles == (), key
        assert palette.rail != palette.bg
        assert palette.hairline != palette.bg
        # 变量表齐全（CSS 里用到的每一个 `$td-*` 都得在这儿）。
        variables = palette.variables()
        cleared = {f"td-{role}" for role in palette.clear_roles}
        for name in ("td-chrome", "td-surface", "td-line", "td-ink",
                     "td-ink2", "td-ink3", "td-accent", "td-warn", "td-danger",
                     "td-ok", "td-rail", "td-elevated", "td-sunk", "td-hairline",
                     "td-ink4", "td-accent-soft", "td-danger-soft", "td-skill",
                     "td-rail-bar"):
            if name in cleared:
                # 声明要透的那几格：变量表里也必须是"别涂"，而不是某个近似的色值。
                assert variables[name] == theme_mod.ANSI_DEFAULT, (key, name)
            else:
                assert variables[name].startswith("#"), (key, name)
        assert variables["td-bg"] == palette.bg, key


def test_theme_blend_is_linear_and_clamped():
    assert theme_mod.blend("#000000", "#FFFFFF", 0) == "#000000"
    assert theme_mod.blend("#000000", "#FFFFFF", 1) == "#FFFFFF"
    assert theme_mod.blend("#000000", "#FFFFFF", 0.5) == "#808080"


def test_theme_resolve_accepts_key_number_name_and_nothing_else():
    """`/theme` 的全部交互设计：key / 展示序号 / 名字里的一段。

    **序号是列表位置，不是 key 里那个数字**：裁掉四套色卡之后这两个数分了家 ——
    `P7 靛夜` 现在是第 4 套，所以 `/theme 4` 才是它，而 `/theme 7` 是 `B 极地冷`。
    这正是"`/theme` 的序号最容易记错"那句话的实例，也是选择面板存在的理由。
    """
    assert theme_mod.resolve("p7") == "P7"
    assert theme_mod.resolve("P7") == "P7"
    assert theme_mod.resolve("4") == "P7"
    assert theme_mod.resolve("7") == "B"
    assert theme_mod.resolve("6") == "A"
    assert theme_mod.resolve("9") == "D"
    assert theme_mod.resolve("12") == "A-T"
    # 深一档那套（13）：key / 序号 / 名字里那两个字都能选中它。
    assert theme_mod.resolve("13") == "A-T2"
    assert theme_mod.resolve("a-t2") == "A-T2"
    assert theme_mod.resolve("AT2") == "A-T2"
    assert theme_mod.resolve("深") == "A-T2"
    assert theme_mod.resolve("靛") == "P7"
    assert theme_mod.resolve("墨绿") == "C"
    assert theme_mod.resolve("a") == "A"
    # 透明版的 key：连字符可带可不带（`a-t` / `at` / `A_T` 都落到同一套）。
    assert theme_mod.resolve("a-t") == "A-T"
    assert theme_mod.resolve("AT") == "A-T"
    assert theme_mod.resolve("p3t") == "P3-T"
    # 被裁掉的那四套的 key 不再是任何东西。
    assert theme_mod.resolve("p1") is None
    assert theme_mod.resolve("P8") is None
    assert theme_mod.resolve("zz") is None
    assert theme_mod.resolve("") is None


def test_resolve_picks_the_most_exact_name_when_two_match():
    """名字命中多于一条时**不是"不认识"**：最精确的那条赢。

    有了透明版，`石墨琥珀 · 透明` 这个名字就是**含** `石墨琥珀` 的 —— 老规矩
    （命中多于一条返回 None）会让 `/theme 琥珀` 变成"没有这套配色"，而列表里明明
    有它。规则是：以它开头的赢，其次短的赢，最后按展示顺序定。
    """
    # 原版赢（`石墨琥珀` 以 `琥珀` 开头，透明版只是含它）。
    assert theme_mod.resolve("琥珀") == "A"
    assert theme_mod.resolve("violet") == "P3"      # Pink Violet < … · Clear
    # 只有透明版含这三个字的三套都命中，取最短的（`粉紫 · 透明`）。
    assert theme_mod.resolve("透明") == "P3-T"
    assert theme_mod.resolve("clear") == "P3-T"
    # 深一档那套：`深透明` / `Deep Clear` 都只有它含，所以它是唯一命中。
    assert theme_mod.resolve("深透明") == "A-T2"
    assert theme_mod.resolve("deep") == "A-T2"
    # 该不认识的一个都没多认：拼一半的名字不是名字（它照旧返回 None）。
    assert theme_mod.resolve("琥珀色") is None
    assert theme_mod.resolve("暖橄榄") is None


def test_the_palette_listing_covers_every_theme():
    listing = theme_mod.listing()
    for index, key in enumerate(theme_mod.ORDER, 1):
        assert f"{index} {key} {theme_mod.get(key).name}" in listing


@pytest.mark.anyio
async def test_the_clear_variant_leaves_the_screen_background_to_the_terminal(monkeypatch):
    """透明版的底**一路走到最后都没有被换成一个真彩色**。

    ## 这条盯的是一个真踩过的坑

    "交给终端"这件事在三个地方都可能被吃掉，而每一处都只是"看着还是实心的一块"：

      1. `theme.py` 的 `$td-bg` 得是 `ansi_default`（不是某个近似的黑）；
      2. `Screen.styles.background` 得保住 `ansi=-1`；
      3. **Textual 默认挂的那个 `ANSIToTruecolor` 过滤器不许把它换成真彩色** ——
         它会把每一个没有 triplet 的颜色按自己那套终端主题猜一个（MONOKAI 猜
         `#0C0C0C`），于是屏幕上出现 `48;5;232`：一块具体的黑，而不是"不涂"。
         `app._KeepDefaultBackground` 就是为这一条存在的。

    第 3 条是这个功能里最不直观的一处（前三处都对了，看起来还是不对），所以它
    值得一条测试而不是一句注释。**实色那几套照旧**：它们的底该被换成真彩色。
    """
    from rich.color import Color as RichColor
    from rich.color import ColorType
    from rich.style import Style as RichStyle

    from agent_runtime.frontends.tui import app as app_module

    app = _build_app(monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        app.theme = "A-T"
        await pilot.pause()
        # (1)(2)：屏幕底是"终端自己的底色"，不是一个色值。
        assert app.screen.styles.background.ansi == -1
        # (3)：过滤器原样放过它，而别的底色照旧被换成真彩色。
        filter = app_module._KeepDefaultBackground(app.ansi_theme)
        default_bg = RichStyle.from_color(bgcolor=RichColor.parse("default"))
        assert filter.truecolor_style(default_bg, RichColor.parse("#131210")) \
            .bgcolor.type == ColorType.DEFAULT
        opaque_bg = RichStyle.from_color(bgcolor=RichColor.from_ansi(4))
        assert filter.truecolor_style(opaque_bg, RichColor.parse("#131210")) \
            .bgcolor.triplet is not None

        # (4)：真正画出来的那一格 —— 对话区在**过滤器之后**仍然是 `default`。
        # 这条才是"屏幕上会不会涂"的直接证据：前三处都对、这一处不对时，看到的
        # 仍然是一块实心（只是颜色从套色变成了近黑）。
        log_line = app.query_one("#log").render_line(0)
        assert [segment.style.bgcolor for segment in log_line
                if segment.style is not None][0].type == ColorType.DEFAULT

        # 而它确实装在那个位置上 —— **而且换主题之后还在那儿**：Textual 的
        # `_refresh_truecolor_filter` 会按 `isinstance` 找到这一格再塞一个新的进去，
        # 不重写它就等于"每次换配色都把透明版降级回一块实心黑"。
        assert type(app._filters[0]) is app_module._KeepDefaultBackground
        app.theme = "C"
        await pilot.pause()
        assert type(app._filters[0]) is app_module._KeepDefaultBackground

        # 实色那一套没被这条改动碰到：屏幕底和对话区都是一个真彩色。
        app.theme = "A"
        await pilot.pause()
        assert app.screen.styles.background.ansi is None
        opaque_line = app.query_one("#log").render_line(0)
        assert [segment.style.bgcolor for segment in opaque_line
                if segment.style is not None][0].triplet is not None


@pytest.mark.anyio
async def test_the_deep_clear_variant_paints_neither_the_bars_nor_the_input_box(monkeypatch):
    """`A-T2`：**顶栏 / 会话头 / 状态栏 / 输入框一个都不涂**，而框线照旧是主题色。

    ## 这条就是"深一层"的验收

    用户点名的效果是屏幕上那四条横色带消失（它们本来和对话区不是一片），而**框线
    要留着**、还是主题色。所以这里按两半断言：

      * 浅的那套（`A-T`）：对话区透了，横栏和那三个框还是实色（`ansi is None`）；
      * 深的那套（`A-T2`）：它们全变成 `ansi == -1`（"别涂"），对话区照旧；
      * 而框线**两套都是主题色**（输入框上下两条 `accent`、那三个框的圆角边也是
        `accent`、左栏右边那条是 `hairline`）—— "框线要主题色"这条要求写在这儿，
        免得下一次谁顺手把它也透了。

    ## 为什么要量到"合成之后的整屏"

    控件自己的 `render_line` 只是**内容那一层**：它上面还要盖屏幕的底、再走一遍
    每个控件的过滤器（`StylesCache.render_widget`），而"这一块是不是一片实色"是在那
    之后才定下来的。所以这条测试按 `render_strips()` 断言 —— 那是驱动真正拿到的
    那一份。**实测它还逮到过一件事**：`NO_COLOR` 在环境里的话，Textual 会挂一个
    `Monochrome` 过滤器把"默认底色"也换成黑（那是"别用颜色"的正当行为），于是整条
    断言会在一个和配色无关的环境变量下变红 —— 这条测试量的是颜色，所以先把那个变量
    摘掉。
    """
    # `App.__init__` 就是在这个变量上决定挂不挂 `Monochrome` 的，所以要在造 App 之前。
    from rich.color import Color as RichColor
    from rich.color import ColorType

    monkeypatch.delenv("NO_COLOR", raising=False)
    app = _build_app(monkeypatch)
    # 横栏那几条 + 输入框（`chrome` 这个 token 收的全部地方）。
    bars = ("#top", "#session", "#status", "#input-box", "#input")
    # 开场那三个框（`surface` 收的地方）—— 它们的名字就是 welcome 那三个类。
    boxes = (".start-box", ".recent-box", ".hint-box")

    def composited_backgrounds(selector: str, row_offset: int = 0) -> set:
        """那一块**合成之后**某一行的底色（整屏那一份，不是控件内容那一份）。"""
        row = app.query_one(selector).region.y + row_offset
        strip = app.screen._compositor.render_strips()[row]
        return {segment.style.bgcolor for segment in strip
                if segment.style is not None and segment.style.bgcolor is not None}

    def all_clear(selectors, row_offset: int = 0) -> None:
        """这几块整行都是"别涂"（`default`，而且是**真的量过**的那一层）。"""
        for selector in selectors:
            assert composited_backgrounds(selector, row_offset) == \
                {RichColor.parse("default")}, selector

    async with app.run_test(size=(100, 30)) as pilot:
        # 欢迎屏要等 `init`（见 `test_tui_boot.py`），而"开始 / 最近 / 提示"就是它。
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)
        assert len(app.query(".welcome-box")) == 3, "开场那三个框要在这一屏上"

        app.theme = "A-T"
        await _settle(app, pilot)
        assert app.screen.styles.background.ansi == -1
        for selector in bars:
            assert app.query_one(selector).styles.background.ansi is None, selector
        for selector in boxes:
            assert app.query_one(selector).styles.background.ansi is None, selector
        # 合成之后也一样：三条横栏是实色（`default` 一格都没有）；那三个框各自那一行
        # 上**有框自己的底色**（同一行剩下的格子是框外的对话区，那一层在 `A-T` 里
        # 本来就是透的，所以这里认的是"框的那一格在不在"，不是"整行都是实色"）。
        for selector in ("#top", "#session", "#status"):
            backgrounds = composited_backgrounds(selector)
            assert backgrounds and all(bg.type != ColorType.DEFAULT for bg in backgrounds), \
                (selector, backgrounds)
        for selector in boxes:
            backgrounds = composited_backgrounds(selector, 1)  # +1 跳过顶边那条框线
            assert RichColor.parse(app.palette.surface) in backgrounds, \
                (selector, backgrounds)
        # 输入框上下那两条线是 accent（浅的那套没动它）。
        assert app.query_one("#input-box").styles.border_top[1].hex.lower() \
            == app.palette.accent.lower()

        app.theme = "A-T2"
        await _settle(app, pilot)
        # 对话区那一大片照旧交给终端……
        assert app.screen.styles.background.ansi == -1
        # ……而横栏和那三个框也终于和它一样了（在此之前它们是实色）。
        for selector in bars + boxes:
            assert app.query_one(selector).styles.background.ansi == -1, selector
        # **合成之后整行都是"别涂"**：这就是用户在终端里看到的那一件事，也是这一套
        # 和 `A-T` 的全部差别。`#log` 那一行也在里面 —— 那一行上正好摆着那两个框。
        all_clear(("#top", "#session", "#status", "#log"))
        all_clear(boxes, 1)
        # 框线还是主题色：输入框上下两条是 `accent`，那三个框的圆角边也是 `accent`，
        # 左栏右边那条是 `hairline`。**它们一个都没被顺手透掉。**
        input_box = app.query_one("#input-box").styles
        assert input_box.border_top[1].hex.lower() == app.palette.accent.lower()
        assert input_box.border_bottom[1].hex.lower() == app.palette.accent.lower()
        for selector in boxes:
            assert app.query_one(selector).styles.border_top[1].hex.lower() \
                == app.palette.accent.lower(), selector
        assert app.query_one("#rail").styles.border_right[1].hex.lower() \
            == app.palette.hairline.lower()
        # `#input` 那一格是个**有意的例外**：光标本身就是一个反白色块（Textual 的
        # caret），它是"我现在在这儿"的唯一信号，不该跟着透。所以这里只要求它别把
        # 整行涂上底 —— 有底的段最多一格，也就是那个光标。
        caret = [segment.style.bgcolor
                 for segment in app.query_one("#input").render_line(0)
                 if segment.style is not None and segment.style.bgcolor is not None
                 and segment.style.bgcolor.type != ColorType.DEFAULT]
        assert len(caret) <= 1, caret

        # 换回实色那套，横栏跟着变回实色（"透"不是一个装上去就摘不掉的开关）。
        app.theme = "A"
        await _settle(app, pilot)
        for selector in bars:
            assert app.query_one(selector).styles.background.ansi is None, selector
        for selector in boxes:
            assert app.query_one(selector).styles.background.ansi is None, selector


def test_every_palette_has_an_english_name_and_source():
    """每一套都要有英文名和英文出处（透明版也是）。

    英文名**不走 `i18n` 的两张目录表**（它是长在主题上的数据，和色值同级），所以
    "两张表键一致"那条测试盯不到它 —— 缺一套的症状是英文界面里冒出一个中文色卡名，
    而它会一直没人发现（中文是默认值）。
    """
    for key in theme_mod.ORDER:
        palette = theme_mod.get(key).palette
        assert palette.name_en.strip(), f"{key} 没有英文名"
        assert palette.source_en.strip(), f"{key} 没有英文出处"


def test_the_theme_names_and_matching_follow_the_language():
    """名字按语言出，而**匹配两套名字都认**。

    后半句是这个功能里最容易漏的一条：切到英文之后，中文用户肌肉记忆里的
    `/theme 靛夜` 如果突然失灵，看起来像"这个功能被我改坏了"。
    """
    from agent_runtime import i18n

    assert theme_mod.get("A").name_in(i18n.ZH) == "石墨琥珀"
    assert theme_mod.get("A").name_in(i18n.EN) == "Graphite Amber"
    assert theme_mod.get("A").source_in(i18n.EN) == "F7-A · default"
    assert theme_mod.get("A-T").name_in(i18n.EN) == "Graphite Amber · Clear"

    # 英文名能选中，而且大小写不敏感。
    assert theme_mod.resolve("graphite") == "A"
    assert theme_mod.resolve("Indigo Night") == "P7"
    # 中文名照旧（这条是"不许为了英文把中文弄丢"）。
    assert theme_mod.resolve("靛") == "P7"
    assert theme_mod.resolve("墨绿") == "C"

    with i18n.with_language(i18n.EN):
        english = theme_mod.listing()
    assert "1 P3 Pink Violet" in english and "6 A Graphite Amber" in english
    assert "1 P3 粉紫" in theme_mod.listing()


def test_the_theme_flag_parses_and_resolves():
    """`--theme` 收的是"人能写出来的一段字"，而认它的是 `theme.resolve` ——
    和 `/theme` 用的是**同一个函数**，所以两条入口对"什么算一套配色"的判断不会分家
    （分家的话，"启动时能用的名字"和"运行中能用的名字"会慢慢漂成两套）。
    """
    from agent_runtime.frontends.cli import build_parser

    args = build_parser().parse_args(["--tui", "--theme", "墨绿仪器"])
    assert args.tui is True and args.theme == "墨绿仪器"
    assert theme_mod.resolve(args.theme) == "C"
    assert build_parser().parse_args(["--tui"]).theme is None


@pytest.mark.anyio
async def test_the_top_bar_right_half_is_not_starved(monkeypatch):
    """顶栏右半（工作区 + `Ctrl+K` 提示）**必须真的画出来**。

    这条是**用户看截图时发现的 bug**：只给 `bar-right` 写 `1fr` 而 `bar-left`
    不写宽度时，左边那个 `Static` 会按默认的 `1fr` 把整行吃掉，右边被挤成 1 列 ——
    而画面上看起来只是"右边空着"，像设计就是这么留白的。所以这里量的不是字符串，
    是**两个控件实际分到的列数**。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(122, 26)) as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "protocol": 1,
            "session_id": "s", "resumed": False, "model": "deepseek-flash",
            "workspace": "C:/w", "max_steps": 80, "context_tokens": 1_000_000,
            "tools": [], "permissions": {}, "audit_path": "C:/w/.tudouni/logs/s.jsonl",
            "notices": [],
        }))
        await _settle(app, pilot)

        right = app.query_one("#top .bar-right")
        assert right.region.width > 10, f"顶栏右边被挤没了：{right.region}"
        assert "Ctrl+K" in str(right.render())

        top = app.query_one("#top")
        left = app.query_one("#top .bar-left")
        assert left.region.width + right.region.width <= top.region.width


@pytest.mark.anyio
async def test_startup_notices_are_quiet_and_do_not_repeat_the_rail(monkeypatch):
    """启动那几行说明：**左栏已经常驻显示的不再抄一遍**，其余的原样说。

    `permissions` / `skills` / `todos` 三个 code 说的正是上下文栏那三块（设计稿 F2
    的空态里一条都没有）；而 `mcp` / `web` / `autopilot` 那些**没有别的出口** ——
    丢掉它们就等于把"文件明明在却不起作用"这类话藏起来。
    """
    assert view_state.notice_is_redundant("permissions") is True
    assert view_state.notice_is_redundant("skills") is True
    assert view_state.notice_is_redundant("todos") is True
    assert view_state.notice_is_redundant("mcp") is False
    assert view_state.notice_is_redundant("web") is False
    # AGENT.md 那条**必须显示**：它说的是"这次启动读到了哪几份项目说明"，是一次启动
    # 事件而不是左栏那三块的状态；而读失败/被截断那两条只在 notice 里有出口。
    assert view_state.notice_is_redundant("agent_md") is False

    app = _build_app(monkeypatch)
    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "protocol": 1,
            "session_id": "s", "resumed": False, "model": "m",
            "workspace": "C:/w", "max_steps": 80, "context_tokens": None,
            "tools": [], "permissions": {}, "audit_path": "C:/w/a.jsonl",
            "notices": [
                {"level": "err", "code": "permissions",
                 "text": "[权限] 按等级自动放行 low；点名免问 fetch_web"},
                {"level": "warn", "code": "mcp",
                 "text": "[MCP] 忽略了工作区里那份 mcp.json"},
            ],
        }))
        await _settle(app, pilot)
        text = _log_text(app)
        assert "[MCP] 忽略了工作区里那份 mcp.json" in text
        assert "点名免问 fetch_web" not in text, "左栏已经显示着它"
        # **不自己拼 `[code]` 前缀**：runtime 给的那句话开头已经写着 `[权限]`。
        assert "[mcp]" not in text and "[permissions]" not in text


@pytest.mark.anyio
async def test_the_hint_box_holds_the_key_row(monkeypatch):
    """键位提示**在欢迎屏底下那个「提示」框里**，不再挂在输入行下面。

    两件事一起钉，因为它们是同一次改动：

      * 那个框的宽度 = 上面两个框加起来（32 + 1 间距 + 42 = 75），三条框线对得上；
      * 界面里**没有** `#keys` 那一条了 —— 挪走了却留着控件的话，它会白占一行把
        输入行往上顶（而画面上只是"下面空了一行"，看不出是哪儿多出来的）。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)
    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)

        assert not app.query("#keys"), "键位提示行已经搬进欢迎屏了"
        hint = app.query_one(widgets_module.HintPanel)
        start = app.query_one(widgets_module.StartPanel)
        recent = app.query_one(widgets_module.RecentPanel)
        assert hint.region.width == widgets_module.WELCOME_HINT_WIDTH
        assert hint.region.width == start.region.width + 1 + recent.region.width, \
            "提示框要横跨上面那两个框"
        assert hint.region.y > recent.region.y
        # 两行键位，每行四条。
        assert len(hint.children) == 2
        assert "发送" in _welcome_text(app) and "中断本轮" in _welcome_text(app)


@pytest.mark.anyio
async def test_the_hint_box_grows_for_the_english_keys(monkeypatch):
    """英文那一版键位更长：**框要跟着长高，行数也要跟着变少**。

    这一条量的是几何（真起一个 App）：`HintPanel.on_mount` 里按语言盖上去的那个高度
    真的生效了吗、行数真的排成了三行吗 —— 只算不量的话，"高度设了但没生效"是看不出来
    的（屏幕上只是少了几条提示）。
    """
    from agent_runtime import i18n
    from agent_runtime.frontends.tui import widgets as widgets_module

    with i18n.with_language(i18n.EN):
        app = _build_app(monkeypatch, lang="en")
        async with app.run_test(size=(140, 40)) as pilot:
            app._inbox.put(("message", _init_message("s")))
            await _settle(app, pilot)

            hint = app.query_one(widgets_module.HintPanel)
            assert hint.region.height == widgets_module.hint_box_height() == 6
            # 一行三条 → 七条键位排成三行（中文那一版是两行，见上一条测试）。
            assert len(hint.children) == 3
            assert "Interrupt this turn" in _welcome_text(app)


def test_the_thinking_block_is_a_quote():
    """思考正文 = **引用块**：底色划范围（控件给），`│` 定边界（行给）。

    两根一起才像一个块。这条钉的是行那一半：竖线单独一档颜色（`ROLE_QUOTE` →
    主题的 `line`），正文仍是 `ROLE_THINK_BODY`。**折叠/展开两条路径用的是同一个
    构造函数**（`quote_line`）—— 各写一遍的话，反复按 `Ctrl+T` 会长出两种长相。

    **换行会被丢掉**（`thinking_body`）—— 这一条有具体来路：流式收到的思考链是
    一块一个词、每块自带换行，逐行存的话展开时是**一个词一行**（实测：401 字符的
    思考过程竖着排了 100 多行）。而**"丢换行"不等于"按空白重新切分"**：
    分块本身就带着它要的空格（"The" + " user"），拿 `" ".join(text.split())` 去压
    会把那些空格一起吃掉（"Theuser"，实测踩过）。所以这里断言的是"两行首尾相接"。
    """
    line = view_state.quote_line("先读 view_state。")
    assert str(line).startswith(view_state.QUOTE_BAR)
    assert line.segments[0] == (view_state.QUOTE_BAR, view_state.ROLE_QUOTE)
    assert line.segments[1] == ("先读 view_state。", view_state.ROLE_THINK_BODY)

    state = view_state.ViewState(thinking={"r1": ("甲\n乙", True)})
    lines = view_state.render_event(state, {
        "kind": "model_call", "run_id": "r1", "status": "ok",
        "duration_ms": 5, "reasoning": "甲\n乙"})
    body = [line for line in lines if line.role == view_state.ROLE_QUOTE]
    assert [str(line) for line in body] == ["  │ 甲乙"], \
        "思考链是一个词一行来的，展开时必须是一段（否则读不了）"

    # 而词与词之间的空格是**分块自己带的**，压平不该碰它。
    assert view_state.thinking_body("The\n user\n says\n") == [
        "  │ The user says"]


def test_the_folded_thinking_line_has_one_source():
    """折叠那一行（`▸ 思考过程（N 字符 · Ctrl+T 展开）`）**三个地方要一致**。

    首屏（非流式）、`Ctrl+T` 收起、流式收尾 —— 三处各写一遍的话，展开再折叠之后
    字数口径能不能对上全靠运气，而那种不一致只有反复按 `Ctrl+T` 才看得见。
    """
    folded = view_state.folded_thinking("一二三四五")
    assert str(folded) == "  ▸ 思考过程（5 字符 · Ctrl+T 展开）"
    assert folded.role == view_state.ROLE_THINK_HEAD

    expanded = view_state.expanded_thinking_head()
    assert str(expanded) == "  ▾ 思考过程（展开 · Ctrl+T 收起）"
    assert expanded.segments[1][1] == view_state.ROLE_RULE


def _welcome_text(app) -> str:
    """空态那一屏上**所有画出来的字**（两个框的标题也在里面）。

    标题走的是 `border_title`，而它**不是** `Static.render()` 那一份内容 —— 只读
    `render()` 的话"开始 / 最近"两个标题永远断言不到，而那正是这一屏的骨架。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    parts = [str(app.query_one(widgets_module.WelcomeBlock).render())]
    for panel in app.query(widgets_module.BorderedPanel):
        if panel.border_title is not None:
            parts.append(str(panel.border_title))
        parts.extend(str(child.render()) for child in panel.children)
    return "\n".join(parts)


@pytest.mark.anyio
async def test_the_welcome_screen_is_two_titled_boxes(monkeypatch):
    """空态那一屏 = **两个带标题的方框**：左边身份（方块标 + 版本 + 模型与工作区），
    右边"最近活动" + 一句箴言。

    方块标**只用半块/全块字符**（▄▀█）：它们在等宽字体里都是一个字符宽的实心格，
    不会像某些图形字符那样在 CJK 字体下变双宽而把右边的字顶歪。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)
    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "protocol": 1,
            "session_id": "s", "resumed": False, "model": "m",
            "workspace": "C:/w/agent_runtime", "max_steps": 80,
            "context_tokens": None, "tools": [], "permissions": {},
            "audit_path": "C:/w/a.jsonl", "notices": [],
        }))
        await _settle(app, pilot)
        rendered = _welcome_text(app)
        assert "██▀▀██" in rendered
        assert "tudouni" in rendered and app._version in rendered
        assert "开始" in rendered and "最近" in rendered
        # 身份那一行给的是**最后一段目录名**（完整路径在顶栏那一行）。
        assert "agent_runtime" in rendered and "C:/w" not in rendered
        # 箴言那一格**总是有字**（它每天轮换，所以这里不钉具体哪一句）。
        assert view_state.motto_of_day() in rendered
        # 三行等宽：右边的字才对得齐（这是它能当标志用的前提）。
        rows = widgets_module.WelcomeBlock.LOGO
        assert len({len(row) for row in rows}) == 1
        assert all(set(row) <= set(" ▄▀█") for row in rows)
        # 两个框的正文一样多行 —— 等高、里面不留会随内容变形的空档靠的就是这一条。
        assert len(app.query_one(widgets_module.StartPanel).children) \
            == widgets_module.WelcomeBlock.BOX_LINES
        assert len(app.query_one(widgets_module.RecentPanel).children) \
            == widgets_module.WelcomeBlock.BOX_LINES


@pytest.mark.anyio
async def test_the_boxes_stay_inside_their_height(monkeypatch):
    """**框高和框里的行数是一对**（CSS 里那个高度和 `BOX_LINES`）。

    实测踩过：`height` 少两行时 Textual 会把框底两行内容**裁掉**，而画面上看起来只是
    "框里少了两行字"—— 没有报错、没有滚动条，谁也不会往版式上想。所以这里量的是几何：
    框高必须容得下"正文 + padding"，而正文必须真的画在自己的框里。

    提示框不在这一条里：它没有上下 `padding`、高度也不是按 `BOX_LINES` 定的（它只装
    两行键位），见 `.hint-box` 那条 CSS。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module
    from agent_runtime.frontends.tui import app as app_module

    css_height = int(app_module.TuiApp.CSS.split(".welcome-box {")[1]
                     .split("height:")[1].split(";")[0].strip())
    assert css_height == widgets_module.WELCOME_BOX_HEIGHT, \
        "CSS 里那个高度和这条常数是一对，改一处就要改另一处"

    app = _build_app(monkeypatch)
    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)
        panels = [*app.query(widgets_module.StartPanel),
                  *app.query(widgets_module.RecentPanel)]
        assert len(panels) == 2
        for panel in panels:
            assert panel.region.height == css_height
            # 内容区（去掉边框和 padding）装得下全部正文行。
            assert panel.content_size.height >= widgets_module.WelcomeBlock.BOX_LINES
            inside = panel.region.shrink(panel.styles.gutter)
            for child in panel.children:
                assert child.region.height == 1
                assert inside.contains_region(child.region), \
                    f"{child.region} 画到框外去了（框在 {panel.region}）"


@pytest.mark.anyio
async def test_the_welcome_screen_shows_recent_titles_not_ids(monkeypatch):
    """右栏"最近活动"两列：左边**多久以前**（清单的 `modified_at`），右边**标题**。

    三条一起测，因为它们是同一件事的三面：

      * 清单到了 → 欢迎屏重画（**不弹选择面板** —— 用户还没按过任何键）；
      * 右列是**第一条用户消息的开头**（`preview`，runtime 算好的），**不是会话 id**
        —— id 是时间戳，人对着它认不出这是哪一次对话；
      * 顺序按"最后一次聊"排，而不是清单本来的创建时间顺序。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)
    now = time.time()
    async with app.run_test(size=(140, 30)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)
        assert "（还没有会话）" in _welcome_text(app), "空着也要占住那一格"

        app._inbox.put(("message", {
            "v": 1, "t": "sessions",
            "items": [
                # 清单按创建时间排（这个最老），但它刚刚才被动过 —— 所以它该排第一。
                {"session_id": "20260101-000000", "messages": 9, "steps": 4,
                 "todos": "", "preview": "把欢迎屏改成左右两个框",
                 "modified_at": now - 30},
                {"session_id": "20260901-000000", "messages": 3, "steps": 1,
                 "todos": "", "preview": "看看协议", "modified_at": now - 5 * 86400},
            ],
        }))
        await _settle(app, pilot)

        assert not isinstance(app.screen, widgets_module.SessionPicker), \
            "启动时那份清单不该弹出选择面板"
        rendered = _welcome_text(app)
        assert "刚刚" in rendered and "5天前" in rendered
        assert "把欢迎屏改成左右两个框" in rendered and "看看协议" in rendered
        assert "20260101-000000" not in rendered and "20260901-000000" not in rendered, \
            "会话 id 是时间戳，认不出是哪次对话 —— 这一列要的是标题"
        assert rendered.index("把欢迎屏改成左右两个框") < rendered.index("看看协议")


def test_a_long_title_is_cut_by_columns_not_by_characters():
    """标题按**显示列数**截断（一个汉字两列），不是一个字符一列。

    截断的那几行要能对齐，靠的就是"每一行右边那条竖线在同一个位置"。用 `len` 数的话，
    中文标题会被多留一倍宽度，而画面上看起来只是"这一行比上面那行长一截"。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    stamp = " " * widgets_module.STAMP_WIDTH
    short = widgets_module._recent_row(
        {"preview": "短标题"}, _palette(), now=None)
    long_cn = widgets_module._recent_row(
        {"preview": "一" * 40}, _palette(), now=None)
    long_en = widgets_module._recent_row(
        {"preview": "a" * 40}, _palette(), now=None)

    assert short.cell_len <= widgets_module.WelcomeBlock.RIGHT_WIDTH
    assert long_cn.cell_len == widgets_module.WelcomeBlock.RIGHT_WIDTH
    assert long_en.cell_len == widgets_module.WelcomeBlock.RIGHT_WIDTH
    assert str(long_cn).endswith("…") and str(long_en).endswith("…")
    # 没有时间戳时那一列**照样占着** —— 否则标题会贴到最左边，四行对不齐。
    assert str(short).startswith(stamp)


def _palette():
    from agent_runtime.frontends.tui import theme as theme_mod

    return theme_mod.get(theme_mod.DEFAULT_THEME)


def _hex_of(color) -> str:
    """Textual 的 `Color` 打出来是 `Color(118, 112, 239)`，比较要用 `.hex`。"""
    return str(getattr(color, "hex", color)).lstrip("#").upper()


def _input(app):
    from agent_runtime.frontends.tui import widgets

    return app.query_one("#input", widgets.PromptArea)


@pytest.mark.anyio
async def test_the_input_is_a_two_row_box_with_highlighted_edges(monkeypatch):
    """输入框 = **两行内容 + 上下两条 accent 线**。

    为什么值得一条测试：它由三处数字凑出来的（`#input-box` 高度 4、`#input-row`
    高度 2、`#input` 高度 2），**任何一处对不上都会静默地少一行或把线吃掉** ——
    而画面上只是"看着有点挤"，不会报错。所以这里量的全是几何。
    """
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        app._pump()
        await pilot.pause()

        box = app.query_one("#input-box")
        field = _input(app)
        assert box.region.height == 4, "1 上边框 + 2 内容 + 1 下边框"
        assert field.region.height == 2, "真的能看两行"
        assert field.content_size.height == 2, "两行都得是内容区，别被 padding 吃掉"

        accent = _hex_of(app.palette.accent)
        for side in ("border_top", "border_bottom"):
            style, color = getattr(box.styles, side)
            assert style == "solid"
            assert _hex_of(color) == accent, (side, _hex_of(color), accent)
        # 左右不封边：设计稿里这是一条通栏的输入行，不是一张卡片。
        assert box.styles.border_left[0] == ""
        assert box.styles.border_right[0] == ""

        # 两行内容：一句话长到超过一行时**软换行到第二行**，而不是横向滚走。
        field.text = "很长的一句话" * 12
        field.cursor_position = len(field.text)
        await pilot.pause()
        assert field.wrapped_document.height >= 2, "该换行"
        assert field.region.height == 2, "再长也只占两行，多出来的靠滚动"


@pytest.mark.anyio
async def test_enter_sends_and_shift_enter_breaks_the_line(monkeypatch):
    """`Enter` 发送、`Shift+Enter` 换行。

    这条钉的是一个**踩过的坑**：`TextArea._on_key` 里硬编了 `enter -> "\\n"` 并当场
    `stop()`，所以"回车发送"写在 `BINDINGS` 里是**没用的**（绑定表里查得到那条，
    按下去还是换行）。只能在 `_on_key` 那一层拦 —— 而这里测的是行为，不是实现，
    所以哪怕以后 Textual 改了内部做法，这条断言仍然是对的。
    """
    app = _build_app(monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        app._pump()
        await pilot.pause()
        field = _input(app)

        await pilot.press("你", "好")
        await pilot.pause()
        await pilot.press("shift+enter")
        await pilot.pause()
        await pilot.press("再", "说", "一", "句")
        await pilot.pause()
        assert field.text == "你好\n再说一句", repr(field.text)

        await pilot.press("enter")
        await pilot.pause()
        assert app._client.sent == [{"t": "user_message", "text": "你好\n再说一句"}]
        assert field.text == "", "发完要清空"


@pytest.mark.anyio
async def test_ctrl_k_opens_the_palette_without_eating_the_draft(monkeypatch):
    """`Ctrl+K` 打开面板，**但不动你正在写的那句话**。

    两个坑都在这一条里：无条件把输入框替换成 `/` 会**吃掉草稿**；而且 `text` 设完之后
    光标落在 0（`cursor_position` 也救不回来），接着打的字会插到 `/` 前面，面板立刻
    又关上 —— 实测过 `Ctrl+K` 再按 `t` 得到 `t/`。
    """
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)
    async with app.run_test(size=(100, 24)) as pilot:
        app._pump()
        await pilot.pause()
        field = _input(app)
        palette = app.query_one("#palette", widgets.CommandPalette)

        await pilot.press("ctrl+k")
        await pilot.pause()
        assert field.text == "/"
        assert field.cursor_location == (0, 1), "光标要在那个 / 后面"
        assert palette.display is True

        await pilot.press("t")
        await pilot.pause()
        assert field.text == "/t", "接着打的字要接在 / 后面"
        assert palette.display is True, "面板不该被自己关掉"

        # 有草稿时：面板盖上去，草稿一个字都不动。
        field.text = ""
        await pilot.pause()
        await pilot.press("写", "草", "稿")
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert field.text == "写草稿", "草稿不许被吃掉"
        assert palette.display is True
        assert app.query_one("#input").has_focus


@pytest.mark.anyio
async def test_arrows_move_the_cursor_then_fall_back_to_the_log(monkeypatch):
    """`↑↓` 一个键三种用法：面板选候选 / 光标移动 / 翻会话流。

    "到底了"不能靠行号看（软换行时一行占好几个可视行），所以 `↓` 的做法是
    **先试一次光标下移、没动就让给会话流**。这条测试把三种情况都走一遍。
    """
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)
    async with app.run_test(size=(100, 14)) as pilot:
        app._pump()
        await pilot.pause()
        field = _input(app)

        # 1) 两行内容时：下移光标，不翻会话流。
        field.text = "甲\n乙"
        field.cursor_position = 0
        await pilot.pause()
        before = app.query_one("#log", widgets.ConversationLog).scroll_offset.y
        await pilot.press("down")
        await pilot.pause()
        assert field.cursor_location == (1, 0)
        assert app.query_one("#log").scroll_offset.y == before

        # 2) 已经在最后一个可视行：让给会话流（这里内容不够长，滚不动，
        #    但**不能把光标挪到不存在的下一行**，也不能抛）。
        await pilot.press("down")
        await pilot.pause()
        assert field.cursor_location == (1, 1)

        # 3) 面板开着：选候选，光标不动。
        field.text = ""
        await pilot.press("ctrl+k")
        await pilot.pause()
        palette = app.query_one("#palette", widgets.CommandPalette)
        assert palette.selected.name == "/new"
        await pilot.press("down")
        await pilot.pause()
        assert palette.selected.name == "/resume"
        await pilot.press("up")
        await pilot.pause()
        assert palette.selected.name == "/new"


@pytest.mark.anyio
async def test_each_rail_block_has_a_left_colour_bar(monkeypatch):
    """左栏每块左边一条色条 —— 眼睛顺着它就能看出"这一栏有几段"。

    颜色取**弱化过的主题描边色**（`rail_bar` = `line` 往底色压 30%）：每块各来一条
    满血描边色会跟正文抢眼睛，而"锚点"该是安静的那一层。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)
    async with app.run_test(size=(140, 40)) as pilot:
        app.state.todos = [{"content": "写测试", "status": "pending"}]
        app.state.rail_pinned = True
        app.state.rail_open = True
        app._refresh_chrome()
        await pilot.pause()

        blocks = list(app.query(widgets_module.RailBlock))
        assert len(blocks) == 6, "六块（任务/技能/权限/会话/后台任务/MCP）"
        expected = _hex_of(app.palette.rail_bar)
        for block in blocks:
            style, color = block.styles.border_left
            assert style == "solid"
            assert _hex_of(color) == expected, (_hex_of(color), expected)
        assert app.palette.rail_bar != app.palette.line, "锚点要比描边色安静"


@pytest.mark.anyio
async def test_the_selected_option_is_marked_and_reversed(monkeypatch):
    """F4 的选中项：`▌` 标记 + **整行反白**（底色铺满，不是只有文字那一段）。

    反白在每一套主题下都自带对比（它就是前景背景互换），而色块底要和 13 套主题的
    正文色逐一对一遍。`▌` 是给单色终端的形状信号。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)
    async with app.run_test(size=(110, 30)) as pilot:
        app._inbox.put(("question", {
            "v": 1, "t": "question_request", "id": "q1",
            "question": "默认展开还是收起？", "header": "rail",
            "options": ["默认展开", "默认收起", "只在有任务时展开"],
            "multi_select": False,
        }))
        await _settle(app, pilot)

        rows = list(app.screen.query(".option"))
        assert len(rows) == 3
        assert str(rows[0].render()).startswith("▌")
        assert "▌" not in str(rows[1].render())
        # 反白 = 控件整行的底色是交互色（铺满由 `width: 1fr` 保证）。
        selected_bg = rows[0].styles.background
        assert selected_bg is not None
        assert _hex_of(selected_bg) == _hex_of(app.palette.accent)
        assert rows[1].styles.background != selected_bg

        # ↓ 之后标记跟着走（不是只有底色在动）。
        await pilot.press("down")
        await _settle(app, pilot)
        rows = list(app.screen.query(".option"))
        assert "▌" not in str(rows[0].render())
        assert str(rows[1].render()).startswith("▌")


def test_the_command_palette_filters_by_prefix_only():
    """**只按前缀匹配**：命令一共十三条，模糊匹配会让"我打错了"和"它猜对了"长得一样。"""
    assert [c.name for c in view_state.filter_commands("/")] == \
        [c.name for c in view_state.COMMANDS]
    assert [c.name for c in view_state.filter_commands("/re")] == ["/resume"]
    assert view_state.filter_commands("/zz") == []
    # 带了参数就选不出东西 —— 于是回车走的是"整行命令"那条路（`/resume abc`）。
    assert view_state.filter_commands("/resume abc") == []
    # 设计稿 F2 那五条还在原位，新增的排在末尾。
    names = [c.name for c in view_state.COMMANDS]
    assert names[:5] == ["/new", "/resume", "/audit", "/exit", "/help"]
    assert "/theme" in names and "/skills" in names
    # 末尾这几条"看/改当前设置"的命令**都在面板里** —— 一个"打得出来但面板里
    # 看不见"的命令，等于把发现它的成本推给记忆。
    assert names[-6:] == ["/status", "/tools", "/model", "/thinking", "/effort",
                          "/mcp"]
    # **`/list` 在第二期被去掉了**：它和"`/resume` 不带参数"是同一个出口，而两条
    # 命令指向同一件事时，人要先猜哪一条才对。这条断言钉的就是"别再把它加回来"。
    assert "/list" not in names


def test_commands_with_arguments_explain_them_in_help_not_in_the_palette():
    """带参数那条命令的用法**进 `/help`、不进面板那一列**。

    面板那一列是"一句短语"的预算（见 `COMMANDS` 上面那段），而"`/model` 的名字要精确、
    打错不猜"这种话放不进去。所以它住 `Command.detail`，只有 `/help` 读 —— 而这条
    测试钉的是"两者都写上了，别只写一半"。
    """
    with_args = [c for c in view_state.COMMANDS if c.takes_arg]
    assert {c.name for c in with_args} == {
        "/resume", "/theme", "/model", "/thinking", "/effort", "/mcp", "/quiet"}
    assert all(c.detail for c in with_args), "带参数的命令要在 /help 里说清怎么用"
    # 不带参数的那些没有 detail —— 空字符串不会被 `_help_lines` 渲染成空行。
    assert all(not c.detail for c in view_state.COMMANDS if not c.takes_arg)


def test_the_waiting_line_lists_only_the_keys_the_backend_offered():
    """审批请求到了之后，会话流里那一行**只列后端真的提供了的键**。

    面板上的按钮是条件渲染的，这一行也必须是 —— 两边不一致的话，用户会照着一个
    不存在的键去按（而按下去什么都不发生，看起来像卡了）。
    """
    bare = view_state.waiting_line({"remember_hint": None, "allow_trust_all": False})
    assert "等待你的批准" in bare
    assert "[y] 允许" in bare and "[n] 拒绝" in bare and "[Esc] 拒绝" in bare
    assert "[t]" not in bare and "[a]" not in bare

    full = view_state.waiting_line({"remember_hint": "以后别再问",
                                    "allow_trust_all": True,
                                    "trust_all_hint": "以后这一组都直接执行"})
    assert "[t] 总是允许" in full and "[a] 都允许" in full
    # 分段着色：整行的 role 是"在等人"，键位那几段是提示色。
    assert full.role == view_state.ROLE_WAITING
    assert any(role == view_state.ROLE_RULE for _text, role in full.segments)


# --- 第二层：Textual 应用骨架 -------------------------------------------------

class FakeClient:
    """替掉真的 `ProtocolClient`：**不起子进程**。

    为什么必须替掉而不是"起了再说"：这层要测的是**界面**，而起一个真子进程会让
    每条测试慢几百毫秒、还依赖环境变量和设备上的 `.tudouni`。**而且第一版我是
    在 `run_test()` 之前给 `app._client` 赋值的 —— 那没用**：`run_test()` 会跑
    完整的生命周期（`on_mount` 在里面），真 client 会把假的覆盖掉，
    于是断言恒为空（实测踩过：`_client is fake` 打出来是 False）。
    所以替的是类，不是属性。
    """

    exit_code = 0

    def __init__(self, hooks, *, session=None, autopilot=False, debug=False,
                 stream=True, lang=None, stderr_to=None):
        self.hooks = hooks
        self.session = session
        self.stream = stream
        # 界面语言：**它必须被记下来**。子进程那半边要按照同一个值写通知和回话，
        # 而"父进程定了一套、传下去的是另一套"是这个功能里最难发现的一类错。
        self.lang = lang
        self.sent: list[dict] = []
        self.started = False
        self.closed = False
        self.interrupts = 0
        self.shutdowns = 0

    def start(self) -> None:
        self.started = True

    def user_message(self, text: str) -> None:
        self.sent.append({"t": "user_message", "text": text})

    def interrupt(self) -> None:
        # **它和 `shutdown` 是两件事**（见 `ProtocolClient.interrupt` 的 docstring），
        # 所以这里也分开记 —— 否则"Esc 到底发了什么"这件事测不出来。
        self.interrupts += 1

    def answer_permission(self, request_id: str, decision: str) -> None:
        self.sent.append({"t": "permission_response", "id": request_id,
                          "decision": decision})

    def answer_question(self, request_id: str, status: str, text: str) -> None:
        self.sent.append({"t": "question_response", "id": request_id,
                          "status": status, "text": text})

    def switch_session(self, session_id: str | None = None) -> None:
        # 和真客户端一样只发一条 —— **界面不许在这里自己清屏**（换会话可能失败），
        # 所以"发了什么"和"界面变成什么样"是两件可分别断言的事。
        self.sent.append({"t": "session_switch", "session_id": session_id})

    def list_sessions(self) -> None:
        self.sent.append({"t": "session_list"})

    def set_autopilot(self, on: bool) -> None:
        # 界面发的是**绝对状态**，所以这里也照原样记下来 —— "按一下切一次"和
        # "把状态设成 X"在下一条断言里长得很不一样。
        self.sent.append({"t": "set_autopilot", "on": on})

    def set_model(self, model: str) -> None:
        # 和 `set_autopilot` 同一条规矩：发的是**名字**，认不认识由 runtime 判。
        self.sent.append({"t": "set_model", "model": model})

    def set_thinking(self, on: bool) -> None:
        # 协议上那一格是**布尔**（不是"on"/"off"两个字）—— 替身照协议发，
        # 所以"界面发错了类型"这件事在测试里就会现形。
        self.sent.append({"t": "set_thinking", "on": on})

    def set_effort(self, effort: str) -> None:
        self.sent.append({"t": "set_effort", "effort": effort})

    def ask_status(self) -> None:
        self.sent.append({"t": "status"})

    def ask_tools(self) -> None:
        self.sent.append({"t": "tools"})

    def mcp(self, action: str, servers: tuple[str, ...] = ()) -> None:
        self.sent.append({"t": "mcp", "action": action,
                          "servers": [str(name) for name in servers]})

    def refresh_state(self) -> None:
        # 后台任务悬着时界面会主动来问一次（见 `app._maybe_refresh_state`）——
        # 替身要认这条消息，否则那条路一被走到就是 AttributeError。
        self.sent.append({"t": "refresh_state"})

    def send(self, message: dict) -> None:
        """真客户端那一层的出口。**这里只记账**：替身不该去编信封（`v` 那一段）。"""
        self.sent.append({k: v for k, v in message.items() if k != "v"})

    def shutdown(self) -> None:
        self.shutdowns += 1

    def close(self) -> None:
        self.closed = True

    def wait(self) -> int:
        return 0


def _build_app(monkeypatch, **kwargs):
    """一个 App 实例，**协议客户端已被替成 `FakeClient`**。"""
    from agent_runtime.frontends.tui import app as app_module

    monkeypatch.setattr(app_module, "ProtocolClient", FakeClient)
    return app_module.TuiApp(session="tui-test", **kwargs)


@pytest.mark.anyio
async def test_the_ui_language_reaches_both_sides(monkeypatch):
    """界面语言**一层都不能丢**：`TuiApp(lang=)` → `ProtocolClient(lang=)` → 子进程。

    这条链和 `--stream` 那条同一个形状，也同样是"丢了不报错、只是行为不对"：
    父进程按英文画界面、子进程按中文写通知和 `/model` 的回话，用户看到的是**一屏
    两种语言** —— 而那看起来像"翻译做了一半"，不像"有个参数没传下去"。

    `with_language` 包着整条用例：`TuiApp(lang=)` 改的是**进程级**的语言，
    不复原的话后面每一条断言中文的用例都会以英文跑。
    """
    from agent_runtime import i18n

    with i18n.with_language(i18n.ZH):
        app = _build_app(monkeypatch, lang="en")
        assert i18n.current() == "en", "界面这一侧要认出英文"
        async with app.run_test(size=(140, 30)) as pilot:
            await _settle(app, pilot)
            assert app._client.lang == "en", "子进程那一侧也要拿到同一个值"


async def _settle(app, pilot, rounds: int = 4) -> None:
    """等消息泵把队列排空、并且界面处理完。

    ## 为什么不能只 `await pilot.pause()`

    泵是一个 **50ms 的定时器**（`set_interval`），而 `pause()` 只让出一轮事件循环
    —— 它**不保证**等过一个定时器周期。症状是**偶发红**：同一个测试这次过、下次
    挂在断言上，而重跑一次又好了（实测：两条面板测试交替红，打印一行调试输出就
    变成了绿的 —— 典型的时序依赖）。

    所以这里主动把泵推一下（`app._pump()` 可以直接调），再让出事件循环去处理它
    引发的屏幕切换（`push_screen` 是排进消息队列的，必须等）。
    """
    for _ in range(rounds):
        app._pump()
        await pilot.pause()


@pytest.mark.anyio
async def test_the_app_mounts_and_draws_an_init(monkeypatch):
    """`init` 到了之后，界面该有的东西都在。

    这条用 Textual 自己的测试台跑一个**真的** App 实例（真挂载、真布局、真渲染），
    但**不连子进程** —— 直接把 `init` 塞进它的消息队列。这样测到的是"界面能不能
    把一条协议消息画出来"，而不是"子进程能不能起来"（那是 `test_protocol.py` 的事）。
    """
    app = _build_app(monkeypatch)

    # **宽屏跑**：会话头在 120 列以下会把模型名和步数预算收起来（F5 的降级），
    # 而这条测试要看的是完整形态。
    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", {
            "v": 1, "t": "init", "protocol": 1,
            "session_id": "tui-test", "resumed": False,
            "model": "fake", "workspace": "C:/w", "max_steps": 80,
            "context_tokens": 1_000_000,
            "tools": [{"name": "read_file", "risk": "low",
                       "parallel_safe": True, "interactive": False}],
            "permissions": {}, "audit_path": "C:/w/.tudouni/logs/tui-test.jsonl",
            "notices": [{"level": "info", "code": "skills", "text": "可用 1 个"}],
        }))
        await _settle(app, pilot)

        # 状态是"界面的第二份事实"里最要紧的那几个字段 —— 它们直接决定状态栏。
        assert app.state.session_id == "tui-test"
        assert app.state.model == "fake"
        assert app.state.max_steps == 80
        assert app.state.audit_path.endswith(".jsonl")
        assert app.state.tool_risks == {"read_file": "low"}
        assert app.state.tool_info["read_file"]["parallel_safe"] is True

        # 会话头确实被更新了。**`Static` 上没有 `.renderable`**（Textual 8 实测），
        # 要拿它现在的文本得走 `render()`。
        session = app.query_one("#session")
        rendered = " ".join(str(w.render()) for w in session.query("Static"))
        assert "tui-test" in rendered
        assert "fake" in rendered


@pytest.mark.anyio
async def test_a_permission_request_opens_the_panel_with_exactly_the_buttons(monkeypatch):
    """**审批面板的按钮集合必须跟着后端给的字段走。**

    三种情况（决策 16 的推论）：
      * 有 `remember_hint` → 才有 [总是允许]；
      * 有 `allow_trust_all` → 才有 [都允许]；
      * 都没有 → 只有 [允许] [拒绝]。

    **不许自己补一个"总是允许整个 shell"** —— 那正是 runtime 刻意堵掉的东西。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p1", "call_id": "c1",
            "tool": "shell", "risk": "high", "arguments": {"command": "rm -rf x"},
            # **两条都为空**：这次既不提供 t 也不提供 a。
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"allow", "deny"}, f"不该出现别的按钮，实际 {ids}"

        # 会话流里也要留下"这里停过一次"的痕迹（面板是盖住的，回头看记录时
        # 只有它能解释那一轮为什么断在那儿）。
        assert "等待你的批准" in _log_text(app)
        assert "[t]" not in _log_text(app), "后端没给 t，界面就不许提它"


@pytest.mark.anyio
async def test_a_trust_all_request_shows_the_fourth_button(monkeypatch):
    """有 `allow_trust_all` 时才出现 [都允许] —— 而且它的说明原样显示。"""
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        hint = "以后 MCP server github 的 12 个工具都直接执行（快照：以后新加的仍然会问）"
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p2", "call_id": "c2",
            "tool": "mcp__github__x", "risk": "high", "arguments": {},
            "remember": {"tool": "mcp__github__x"}, "remember_hint": "以后别再问",
            "allow_trust_all": True, "trust_all_hint": hint,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"allow", "deny", "always", "always_group"}

        # 那句说明**原样**在界面上（一个字都没改）。
        # `Static` 上没有 `.renderable`（Textual 8 实测），文本走 `render()`。
        rendered = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert hint in rendered


@pytest.mark.anyio
async def test_escape_in_the_approval_panel_denies(monkeypatch):
    """决策 16：`Esc` = **拒绝**，不是"关掉再说"。

    fail-closed 的方向和 `cli_asker` 读不到输入那一支一致（默认拒绝才是安全的失败
    方向）。这条测的是**键位绑定**本身（上一条测的是按钮集合），因为绑定写错的话
    症状是"按了没反应"，而面板看起来完全正常。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p7", "call_id": "c7",
            "tool": "shell", "risk": "high", "arguments": {"command": "rm -rf x"},
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)
        assert type(app.screen).__name__ == "PermissionPanel"

        await pilot.press("escape")
        await _settle(app, pilot)
        assert {"t": "permission_response", "id": "p7",
                "decision": "deny"} in app._client.sent


@pytest.mark.anyio
async def test_the_ui_answers_permission_itself_instead_of_a_fallback(monkeypatch):
    """`on_permission` **必须返回 `None`**（"界面稍后自己回"），不许给兜底答案。

    这条测试是**为一个真实的 bug** 写的，而它的症状很误导：审批面板正常弹出、
    按钮也点了，但工具结果是"用户拒绝"。

    原因是第一版让 `on_permission` 返回一个兜底 `DENY`，想的是"稍后用真答案覆盖"。
    而客户端**立刻**就把那个 DENY 发出去了 —— 子进程据此拒绝并继续往下跑；等用户
    点 [允许] 时那条回应已经没人要（更糟：中间那次拒绝进了审计，记成
    `user_denied`，也就是**伪造了一条"用户拒绝过"的记录**）。

    所以这里钉的是那个返回值本身。它看起来像个细节，但它是"谁有权回答"这件事的
    全部 —— 协议层那边是配套的另一半（`ClientHooks.on_permission` 的 docstring、
    `client._read_loop` 里那个 `if decision is not None`）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test():
        request = {"t": "permission_request", "id": "p", "tool": "shell"}
        assert app.on_permission(request) is None, \
            "不许给兜底答案 —— 它会抢在用户前面发出去"
        assert app.on_question({"t": "question_request", "id": "q"}) is None

        # 而且请求真的进了队列（不是把它丢掉了）。
        assert app._inbox.qsize() == 2


@pytest.mark.anyio
async def test_clicking_allow_sends_allow_not_deny(monkeypatch):
    """点 [允许] 之后，发出去的是 `allow`。

    和上一条配套：一个保证"不由界面之外的人回答"，这一个保证"界面回答的是对的那个"。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("permission", {
            "v": 1, "t": "permission_request", "id": "p9", "call_id": "c9",
            "tool": "shell", "risk": "high", "arguments": {"command": "echo hi"},
            "remember": None, "remember_hint": None,
            "allow_trust_all": False, "trust_all_hint": None,
        }))
        await _settle(app, pilot)

        from textual.widgets import Button
        app.screen.query_one("#allow", Button).press()
        await _settle(app, pilot)

        assert {"t": "permission_response", "id": "p9",
                "decision": "allow"} in app._client.sent


@pytest.mark.anyio
async def test_slash_commands_do_not_reach_the_runtime(monkeypatch):
    """`/` 命令由界面处理，**不进 runtime**。

    这条挡的是一个很容易犯的错：把 `/help` 当成一句话发给模型 —— 于是它花钱去回答
    一个本地就能答的问题，而且用户看不出区别。

    它调的是 `app.submit(...)` 而不是伪造一条 `Input.Submitted`：后者测的是
    Textual 的消息路由（那是它的事），而这里要测的是我们的判断。
    """
    app = _build_app(monkeypatch)

    async with app.run_test():
        client = app._client
        app.submit("/help")
        app.submit("   ")
        assert client.sent == [], "斜杠命令和空行都不该发给 runtime"

        app.submit("你好")
        assert client.sent == [{"t": "user_message", "text": "你好"}]

        app.submit("  /exit  ")
        assert len(client.sent) == 1, "带空格的命令也要认得出来"


# --- 换会话（`/new` `/resume`）-------------------------------------------------
#
# 这一组钉的是第二期改掉的那条设计决策（旧 9.2："`/new` / `/resume` 都是重开进程"）。
# 关键在于**两件事要分开测**：
#
#   1. 命令发出去的是什么（`session_switch`，而且**界面此刻不许清屏**）；
#   2. 换成功了界面变成什么样（由 `init` 那条消息决定）。
#
# 合起来测的话，"界面抢在 runtime 前面清屏"这个 bug 会藏过去 —— 而它的后果很具体：
# 换会话失败（权限文件坏了）时，用户会看到一个空界面，而他的会话其实还在。

def _init_message(session_id: str, **overrides) -> dict:
    """一条 `init`。字段照 `protocol/schema/outbound.schema.json`。"""
    return {
        "v": 1, "t": "init", "protocol": 2,
        "session_id": session_id, "resumed": False,
        "model": "fake", "workspace": "C:/w", "max_steps": 80,
        "stream": False,
        "context_tokens": 1_000_000, "tools": [], "permissions": {},
        "audit_path": f"C:/w/.toudouni/logs/{session_id}.jsonl", "notices": [],
        **overrides,
    }


def _sessions_payload(*session_ids: str) -> dict:
    return {
        "v": 1, "t": "sessions",
        "items": [
            {"session_id": name, "messages": 4, "steps": 2,
             "todos": "", "preview": f"{name} 的第一句话"}
            for name in session_ids
        ],
    }


@pytest.mark.anyio
async def test_slash_new_asks_the_runtime_to_switch_and_keeps_the_screen(monkeypatch):
    """`/new` 发一条 `session_switch`（`session_id=None`），**并且不清屏**。

    清屏的时机是这条测试的重点：它只能发生在 `init` 到达之后。在这里就清的话，
    换会话失败时用户会失去当前会话的画面（而 runtime 那边其实原样保留着它）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", _init_message("old-one")))
        await _settle(app, pilot)
        assert app.state.session_id == "old-one"
        # 空态那一屏会主动问一次会话清单（右栏"最近活动"要它）—— **这是开场唯一
        # 一条界面自己发出去的消息**，而它之后不该再有别的。
        assert app._client.sent == [{"t": "session_list"}]

        app.submit("/new")
        assert app._client.sent[-1] == {"t": "session_switch", "session_id": None}
        # **还没换成功** —— 所以会话还是老的、流里的话还在。
        assert app.state.session_id == "old-one"
        assert "old-one" in _log_text(app)


@pytest.mark.anyio
async def test_a_new_init_rebuilds_the_screen_for_the_new_session(monkeypatch):
    """换成功之后：**旧回合、旧左栏、旧答案全清掉**，然后画新会话的空态。

    这里的每一条断言都对应一个"漏了就很难看"的字段（见 `ViewState.reset_for_session`
    的 docstring）：漏 `turns` 就会在新会话里看见旧回合，漏 `todos` 就会让左栏显示
    上一个会话的任务列表 —— 而后者看起来完全正常。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        app._inbox.put(("message", _init_message("old-one")))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "第一轮"})
        app.state.todos = [{"content": "旧会话的任务", "status": "pending"}]
        await _settle(app, pilot)
        assert "第一轮" in _log_text(app)

        app._inbox.put(("message", _init_message("new-two")))
        await _settle(app, pilot)

        assert app.state.session_id == "new-two"
        assert app.state.turns == [] and app.state.todos == []
        log = _log_text(app)
        assert "第一轮" not in log, "上一个会话的回合必须从画面上消失"
        assert "旧会话的任务" not in log
        assert "new-two" in log, "新会话空态要说明自己是谁"


@pytest.mark.anyio
async def test_resume_with_an_id_switches_straight_away(monkeypatch):
    """`/resume <id>` 不发 `session_list`：id 已经在手里了，再列一次清单是白跑一趟。"""
    app = _build_app(monkeypatch)

    async with app.run_test():
        app.submit("/resume 20250101-120000")
        assert app._client.sent == [
            {"t": "session_switch", "session_id": "20250101-120000"}]


@pytest.mark.anyio
async def test_escape_in_the_picker_switches_nothing(monkeypatch):
    """`Esc` = **什么都不做**。换会话会把当前 runtime 收掉，误触的代价比"没换"大。"""
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", _sessions_payload("a-one", "b-two")))
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.SessionPicker)

        await pilot.press("escape")
        await _settle(app, pilot)
        assert app._client.sent == []
        assert app.state.session_id == ""


def test_the_fake_client_covers_the_real_one():
    """替身必须覆盖真客户端的每一个方法 —— 否则测试测的是一个**不存在的**接口。

    这一条很值：`TuiApp` 只通过 `self._client` 说话，而那个属性在测试里是替身。
    真客户端加了一个方法、替身没跟上时，测试会绿着通过，而界面上那条路一跑就
    `AttributeError`（只在用户按那个键时才炸）。
    """
    from agent_runtime.protocol.client import ProtocolClient

    real = {name for name in dir(ProtocolClient) if not name.startswith("_")}
    fake = {name for name in dir(FakeClient) if not name.startswith("_")}
    missing = real - fake - {"hooks", "exit_code"}
    assert not missing, f"FakeClient 少了真客户端的这些方法：{sorted(missing)}"


@pytest.mark.anyio
async def test_resetting_for_a_session_keeps_the_ui_switches(monkeypatch):
    """换会话清掉一切**属于会话**的东西，但**不动界面自己的开关**（`Ctrl+B`）。

    这条钉的是那个分寸：左栏开合是"我要不要看它"，和聊的是哪个会话无关 —— 换一次
    会话就把用户手动收起的栏顶开，是最容易被当成 bug 的那种"贴心"。
    """
    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        app._inbox.put(("message", _init_message("old-one")))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "第一轮"})
        app.state.todos = [{"content": "旧任务", "status": "pending"}]
        app.state.skills = [{"name": "旧技能"}]
        await _settle(app, pilot)

        # 任务列表出现 → 栏自动顶开（决策 26）；用户按一下 Ctrl+B **收起**它。
        # 这里刻意用"收起"来表达手动选择：`run_test()` 默认 80 列，而窄屏现在也会
        # 自动开，所以"按一下 = 打开"那个旧写法不再成立（旧规则下 80 列从不自动开）。
        assert app.state.rail_open is True, "任务列表出现就顶开"
        app.action_toggle_rail()          # 用户手动收起
        assert app.state.rail_pinned is True
        assert app.state.rail_open is False

        app._inbox.put(("message", _init_message("new-two")))
        await _settle(app, pilot)

        assert app.state.rail_open is False, "左栏的开合是用户的选择，换会话不该动它"
        assert app.state.rail_pinned is True
        for field in ("turns", "answers", "thinking", "calls", "todos", "skills",
                      "risk_scope", "granted_tools", "granted_prefixes",
                      "denied_tools"):
            value = getattr(app.state, field)
            assert not value, f"{field} 里还留着上一个会话的东西：{value!r}"
        assert app.state.steps == 0 and app.state.messages == 0


# --- 流式（`t:"delta"` / `t:"delta_reset"`）-----------------------------------

def _delta_msg(app, text: str, *, channel: str = "text", step: int = 1,
               run_id: str = "r1") -> dict:
    return {
        "v": 1, "t": "delta", "session_id": app.state.session_id,
        "run_id": run_id, "step": step, "channel": channel, "text": text,
        "reset": False,
    }


def _delta(app, text: str, *, channel: str = "text", step: int = 1,
           run_id: str = "r1") -> None:
    app._inbox.put(("message", _delta_msg(app, text, channel=channel, step=step,
                                          run_id=run_id)))


def _reset(app, *, step: int = 1, run_id: str = "r1") -> None:
    app._inbox.put(("message", {
        "v": 1, "t": "delta_reset", "session_id": app.state.session_id,
        "run_id": run_id, "step": step,
    }))


@pytest.mark.anyio
async def test_streamed_text_accumulates_and_renders_as_markdown(monkeypatch):
    """逐字来的正文进**流式那一版** Markdown 块，而且累计按 `run_id` 记着。

    两件事一起钉，因为它们是"流式这一段真的接上了"的两个必要条件：
      * 屏幕上有个正文块（`StreamAnswerBlock`，不是一次性的 `AnswerBlock`）；
      * `state.stream_text` 攒着**这一轮的全文** —— 收尾时就是靠它判断"答案要不要
        再画一遍"（见下一条）。少了它，那个判据只能退回"收到过 delta 没有"，
        而那个判据在一轮里一个正文块都没吐的时候是错的。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "写点东西"})
        await _settle(app, pilot)

        for piece in ("# 标题\n", "\n正文 **加粗**。\n"):
            _delta(app, piece)
            await _settle(app, pilot)

        assert app.state.stream_text == "# 标题\n\n正文 **加粗**。\n"
        blocks = list(app.query(widgets_module.StreamAnswerBlock))
        assert len(blocks) == 1, "同一段正文只该有一个流式块"
        assert not list(app.query(widgets_module.AnswerBlock)), \
            "流式那段不该用一次性的 AnswerBlock（那个喂不了第二块）"

        # 子控件是异步挂的，等它长出来再断言。
        #
        # **每一块 delta 只按它自己那一段解析**（`Markdown.append` 的语义是"接着
        # 上次解析到的地方往下解析"），所以这里喂的是**整行整块**的片段（标题那条
        # 自带换行）。一个 `#` 单独来、标题的字下一块才到，解析器只会先看到一个
        # 段落 —— 那是流式的正常中间态，不是 bug（第一次渲染出来的是"未完成的
        # Markdown"，收尾时最后一块会把它补齐）。
        for _ in range(20):
            if len(blocks[0].children) >= 2:
                break
            await pilot.pause()
        kinds = [type(child).__name__ for child in blocks[0].children]
        assert "MarkdownH1" in kinds, f"流式正文没被解析成 Markdown：{kinds}"
        assert "MarkdownParagraph" in kinds, f"段落也该在：{kinds}"


@pytest.mark.anyio
async def test_the_final_answer_is_not_drawn_a_second_time_after_streaming(monkeypatch):
    """**流过了就不再画第二份。**

    `ui(run_finished)` 照样带完整答案（协议不变、老前端靠它），而流式那一轮里
    它已经在屏幕上了。再画一遍的话，用户看到的是同一段回答连着出现两次 ——
    而它看起来像模型说了两遍，不像协议发重了。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        _delta(app, "你好，我是 agent。")
        await _settle(app, pilot)

        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "run_finished", "run_id": "r1",
            "answer": "你好，我是 agent。",
        }))
        await _settle(app, pilot)

        assert not list(app.query(widgets_module.AnswerBlock)), \
            "流过的答案不许再画一遍"
        # 但答案照旧按 run_id 记着（`/history` 那类和 verify 脚本看的是它）。
        assert app.state.answers["r1"] == "你好，我是 agent。"
        # 累计清干净了：下一轮不该捡到这一轮的字。
        assert app.state.stream_text == ""


@pytest.mark.anyio
async def test_a_retry_discards_the_half_written_answer_on_screen(monkeypatch):
    """`t:"delta_reset"` 把这一步画出来的那半截**整块拿掉**。

    屏幕上留着它、而会话历史里查不到它，是流式这一版最容易让人困惑的状态
    （"我刚才明明看到它说了那句话"）。所以那条消息到达时界面要真的清掉，
    而且**只清这一步**：前几步已经定下来的内容不在重试范围内。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        _delta(app, "半截的回答")
        await _settle(app, pilot)
        assert list(app.query(widgets_module.StreamAnswerBlock))

        _reset(app)
        await _settle(app, pilot)

        assert not list(app.query(widgets_module.StreamAnswerBlock)), \
            "重试之前那半截必须从屏幕上消失"
        assert app.state.stream_text == ""

        # 重试之后新的一段照常画。
        _delta(app, "完整的回答")
        await _settle(app, pilot)
        assert app.state.stream_text == "完整的回答"


@pytest.mark.anyio
async def test_a_reset_for_another_step_or_run_does_not_touch_the_screen(monkeypatch):
    """reset 的判据是 **(run_id, step) 都要对上**。

    只按 run_id 清的话，第 2 步的重试会把第 1 步那句"我看看文件"也抹掉 ——
    屏幕上少了内容，而历史上还在。反过来（只按 step）在连开两轮时会清错回合。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "看看目录"})
        _delta(app, "我看看。", step=1)
        await _settle(app, pilot)

        # 别的回合、别的步：都不该动它。
        _reset(app, step=2)
        _reset(app, run_id="r-other", step=1)
        await _settle(app, pilot)

        assert list(app.query(widgets_module.StreamAnswerBlock)), \
            "不属于这一步的 reset 不该动屏幕上的东西"
        assert app.state.stream_text == "我看看。"


@pytest.mark.anyio
async def test_think_deltas_go_to_their_own_block(monkeypatch):
    """思考链走 `reasoning` 通道，落在**带底色的那一块**里，不混进正文。

    两条通道的内容都是字符串，混错的症状是"答案里混进了一段自言自语" ——
    看起来像模型的问题，不像协议分错了。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "42?"})
        _delta(app, "先算一下", channel="reasoning")
        _delta(app, "答案是 42", channel="text")
        await _settle(app, pilot)

        turn = app.query_one(widgets_module.ConversationLog).current_turn_block
        assert turn is not None
        # `plain` 那一块是回合头底下那行"用户说的话"（`run_started` 画的），
        # 它不属于讨论范围 —— 这里看的是流式那两块的位置关系。
        streamed = [chunk["kind"] for chunk in turn.chunks
                    if chunk["kind"] in ("think", "answer")]
        assert streamed == ["think", "answer"], f"两块各归各的：{streamed}"
        assert app.state.stream_reasoning == "先算一下"
        assert app.state.stream_text == "答案是 42"
        # 思考链的字不该出现在流式正文块里。
        assert turn.chunks[-1]["kind"] == "answer"
        assert "先算一下" not in str(getattr(turn.chunks[-1]["block"], "_markdown", ""))


@pytest.mark.anyio
async def test_think_deltas_do_not_become_one_line_per_word(monkeypatch):
    """**一个词一行的 bug**（实测踩过，用户看出来的）。

    provider 吐思考链时一块往往就是一个词，而每一块还自带一个换行 ——
    逐块 `splitlines()` 再 `append()` 的结果是界面上竖着排 100 多行
    （"The / user / says / ..."）。所以思考链那一块**只有两行**：块头 + 这一段。

    正文那条通道**不能**这么压：它是 Markdown 源文，换行是有意义的语法。
    两个方向一起钉，免得"修好了思考链、弄坏了正文"。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        # 照真实的形状来：一块一个词、每块一个换行，而且**词前那个空格跟着前一块
        # 走**（真网关就是这样）。
        for piece in ("The\n", " user\n", " says\n", ' "hi".\n'):
            _delta(app, piece, channel="reasoning")
        _delta(app, "# 标题\n", channel="text")
        await _settle(app, pilot)

        turn = app.query_one(widgets_module.ConversationLog).current_turn_block
        think = [c for c in turn.chunks if c["kind"] == "think"][0]["block"]
        assert len(think.lines) == 2, \
            f"思考链该是「块头 + 一段」，实际 {len(think.lines)} 行：{think.lines!r}"
        assert str(think.lines[1]) == f"{view_state.QUOTE_BAR}The user says \"hi\"."

        # 正文那一块仍然按 Markdown 源文走（换行没被压掉）。
        answer = [c for c in turn.chunks if c["kind"] == "answer"][0]["block"]
        assert "# 标题" in answer._text and "\n" in answer._text


@pytest.mark.anyio
async def test_the_streamed_thinking_is_folded_back_when_the_turn_ends(monkeypatch):
    """一轮结束：铺开的思考过程**收成折叠那一行**（和没开流式时一样）。

    两个后果都不只是审美：
      * 不收的话每一轮的思考过程都糊在屏幕上（非流式那一轮是折叠的，两种模式对不上）；
      * **`Ctrl+T` 会失灵** —— 它按 `ROLE_THINK_HEAD` 那一行找块，而流式那块的行
        全是 `THINK_BODY`，展开键会从它上面滑过去。

    收完之后**展开仍然拿得到全文**（`state.thinking` 里有一份），而那正是
    `Ctrl+T` 要读的东西 —— 所以这里连展开也一起点一遍。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=True)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        for piece in ("先看", "工作区。"):
            _delta(app, piece, channel="reasoning")
        await _settle(app, pilot)

        # 收尾：审计那条 `model_call`（带完整 reasoning）+ `run_finished`。
        _events(app, {"kind": "model_call", "run_id": "r1", "step": 1, "status": "ok",
                      "duration_ms": 5, "reasoning": "先看工作区。"})
        _events(app, {"kind": "run_finished", "run_id": "r1", "step": 1,
                      "stop_reason": "answered", "duration_ms": 6})
        await _settle(app, pilot)

        turn = app.query_one(widgets_module.ConversationLog).current_turn_block
        folded = _think_blocks(turn)
        assert len(folded) == 1, "收尾之后该只剩**一个**思考块（没有两个折叠行）"
        head = _think_lines(turn)
        assert len(head) == 1, f"只该有一个折叠块头：{head}"
        assert "Ctrl+T 展开" in str(head[0])
        assert f"{len('先看工作区。')} 字符" in str(head[0])

        # `Ctrl+T` 展开：还能拿到全文（`state.thinking` 那份）。
        # 走的是 App 真正的那条路（`action_toggle_thinking` → 回合块的
        # `toggle_thinking`），而不是自己去翻控件 —— 否则"键位接错对象"这类
        # bug 测不出来（那正是设计稿第 4 条改动要修的）。
        assert app.state.thinking["r1"][0] == "先看工作区。"
        assert turn.toggle_thinking("先看工作区。") is True
        body = [line for line in _think_blocks(turn)[-1]["block"].lines
                if line.role == view_state.ROLE_QUOTE]
        assert [str(line) for line in body] == [f"{view_state.QUOTE_BAR}先看工作区。"]


@pytest.mark.anyio
async def test_without_streaming_there_is_no_live_think_block(monkeypatch):
    """`--no-stream` 那条路一个字都不该变：思考过程**只有**审计画的那个折叠行。

    收尾时那个"把流式块收起来"的动作因此必须是**空操作** —— 否则它会把非流式
    那一轮唯一的思考过程抹掉（或者收出一个重复的折叠行）。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        app._inbox.put(("message", _init_message("s", stream=False)))
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "你好"})
        _events(app, {"kind": "model_call", "run_id": "r1", "step": 1, "status": "ok",
                      "duration_ms": 5, "reasoning": "想一下。"})
        _events(app, {"kind": "run_finished", "run_id": "r1", "step": 1,
                      "stop_reason": "answered", "duration_ms": 6})
        await _settle(app, pilot)

        turn = app.query_one(widgets_module.ConversationLog).current_turn_block
        heads = _think_lines(turn)
        assert len(heads) == 1, f"非流式那条路该只有一个折叠块头：{heads}"
        assert "4 字符" in str(heads[0])


# --- 第三层：设计稿新增的那几件交互 -------------------------------------------

def _events(app, *messages) -> None:
    """把几条协议消息塞进泵（**和真消息走同一条路**）。"""
    for message in messages:
        app._inbox.put(("message", {"v": 1, "t": "event", **message}))


def _log_text(app) -> str:
    """会话流里**行**的文本。

    **它看不见 agent 正文** —— 正文是 `AnswerBlock`（一棵 Markdown 子控件树），
    不是 `LineBlock`。要断言正文就得直接查控件（见下面那条渲染测试）。
    """
    from agent_runtime.frontends.tui import widgets

    parts = []
    for block in app.query(widgets.LineBlock):
        parts.extend(str(line) for line in block.lines)
    return "\n".join(parts)


def _screen_text(app) -> str:
    """屏幕上**所有** `Static` 画出来的字（栏、左栏、会话流、欢迎屏、面板）。

    和 `_log_text` 的分工：那个只看会话流里那几块行，这个把整屏扫一遍 —— 用在
    "整屏不许出现汉字"这类判据上（漏了一个控件就漏了一处漏翻的文案）。
    """
    from textual.widgets import Static

    parts = []
    for widget in app.query(Static):
        try:
            parts.append(str(widget.render()))
        except Exception:  # noqa: BLE001 - 画不出来的控件不该让这条判据失败
            continue
    return "\n".join(parts)


@pytest.mark.anyio
async def test_the_whole_screen_is_english_in_en_mode(monkeypatch):
    """英文模式下**整屏**一个汉字都没有 —— 这是"英文界面"最直接的一条验收。

    喂的是**全 ASCII 的假数据**（会话 id、工具名、命令、答案），所以任何汉字都只能来自
    界面文案本身（模型/用户的中文数据是真数据，不该被这条判据管 —— 用 ASCII 就是为了
    把这条线划干净）。扫描范围是屏幕上每一个 `Static`：三条栏、左栏六块、会话流那几行、
    欢迎屏三框。
    """
    import re

    from agent_runtime import i18n

    han = re.compile("[\u4e00-\u9fff]")
    with i18n.with_language(i18n.EN):
        app = _build_app(monkeypatch, lang="en")
        async with app.run_test(size=(140, 40)) as pilot:
            app._inbox.put(("message", _init_message("s1")))
            await _settle(app, pilot)
            for event in (
                {"kind": "run_started", "run_id": "r1", "step": 0,
                 "user_input": "do the thing"},
                {"kind": "model_call", "run_id": "r1", "step": 1, "status": "ok",
                 "duration_ms": 1200, "prompt_tokens": 900, "cached_tokens": 100},
                {"kind": "tool_call", "run_id": "r1", "step": 1, "tool": "read_file",
                 "tool_index": 0, "call_id": "c1", "arguments": '{"path": "a.py"}'},
                {"kind": "tool_result", "run_id": "r1", "step": 1, "tool": "read_file",
                 "tool_index": 0, "call_id": "c1", "status": "ok", "chars": 1024,
                 "duration_ms": 4},
                {"kind": "run_finished", "run_id": "r1", "step": 2,
                 "stop_reason": "answered", "duration_ms": 4200},
            ):
                app._inbox.put(("message", {"v": 1, "t": "event",
                                            "session_id": "s1", **event}))
            await _settle(app, pilot)

            text = _screen_text(app) + "\n" + _log_text(app)
            assert "Turn 1" in text or "Turn" in text
            assert not han.search(text), text[:400]


@pytest.mark.anyio
async def test_the_answer_is_rendered_as_markdown(monkeypatch):
    """agent 正文走 `AnswerBlock`：它解析成**一棵子控件树**，而不是一串行。

    这条钉的是"渲染"本身。只断言"有个块在"是不够的 —— 把正文塞进一个 `Static`
    也能过。所以看的是**解析出来的子控件**（`MarkdownH1` / `MarkdownParagraph` /
    `MarkdownFence`）：它们出现，才说明 `#`、`**`、三个反引号真的被当成语法了，
    而不是原样画出来。

    顺带钉住"两条通路是分开的"：正文不进 `LineBlock`（`_log_text` 里看不到它），
    否则工具行那类 `[` `*` 会混进 Markdown 的解析范围。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 40)) as pilot:
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "举个例子"})
        app._inbox.put(("message", {
            "v": 1, "t": "ui", "kind": "run_finished", "run_id": "r1",
            "answer": "# 标题\n\n正文**加粗**。\n\n```python\nprint(1)\n```\n",
        }))
        await _settle(app, pilot)

        blocks = list(app.query(widgets_module.AnswerBlock))
        assert len(blocks) == 1, "一次 run_finished 只该有一个正文块"
        block = blocks[0]

        # `Markdown` 的子控件是**分批异步挂**的（`update()` 走 executor +
        # `mount_all`），所以这里等它长出来再断言 —— 不等就是偶发红。
        for _ in range(20):
            if len(block.children) >= 3:
                break
            await pilot.pause()
        kinds = [type(child).__name__ for child in block.children]
        assert "MarkdownH1" in kinds, f"标题没被解析成 H1：{kinds}"
        assert "MarkdownParagraph" in kinds, f"段落没被解析：{kinds}"
        assert "MarkdownFence" in kinds, f"代码块没被解析成 Fence：{kinds}"

        # **模型写一行链接不该能拉起浏览器**：`open_links` 是这道闸的唯一开关，
        # 而 Textual 没给它公开的读法（它只是个内部字段），所以只能这样钉。
        assert block._open_links is False, "链接自动打开会让模型的输出直接触发外部动作"

        assert app.state.answers["r1"].startswith("# 标题"), "答案照样要按 run_id 记账"
        assert "# 标题" not in _log_text(app), "正文不该同时掉进行那条通路"

        # --- 正文块插进回合块之后，两条老路径不能被它绊倒 ---------------------
        turn = app.query_one(widgets_module.ConversationLog).current_turn_block
        assert turn is not None
        assert [chunk["kind"] for chunk in turn.chunks] == ["plain", "answer"], \
            "正文是独立的块类型，不并进前面那些行里"

        # `has_thinking()` 会**遍历所有块**。正文块没有 `.lines` —— 少了那条跳过，
        # 这一句就抛 AttributeError，而界面上只是"没有思考过程"这一块画不出来。
        assert turn.has_thinking() is False

        # `/theme` 换配色会走 `ConversationLog.repaint → TurnBlock.repaint →
        # 每个块`：正文块只记账、不重画（配色在 CSS 里，Textual 自己会重算）。
        # 少了那个方法，换配色会在正文这一块上抛 AttributeError。
        app._set_theme("P3")
        await _settle(app, pilot)
        assert app.theme == "P3"


@pytest.mark.anyio
async def test_every_answer_gets_a_left_bar_in_the_input_box_colour(monkeypatch):
    """agent 正文左边那条竖线 —— **流式和非流式两条路都得画出来**。

    这条是**为一个真实的 bug** 写的：`StreamAnswerBlock` 原来把累计文本写进文档的
    那个方法叫 `_render`，**正好盖住了 `Widget._render`**（Textual 的钩子），于是
    `Widget._render_content` 拿到 `None`。当时是用一个 `BLANK = True` 绕过去的，
    而 `BLANK` 让 `render_lines` 直接回空白条 —— **连控件自己的边框一起跳过**。

    所以症状是"同一段回答，非流式有竖线、流式没有"，而流式恰好是**默认**那条路
    —— 画面上看起来只像"这一版就是没画"。这里量的是**屏幕上的字符**，不是样式表：
    样式设上了而没画出来，正是那个 bug 的全部内容。

    颜色和输入框上下那两条线是**同一个 token**（`accent`）：这一屏上"结构线"是同一类
    东西，同一个颜色才像一套。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    def bars(app):
        """正文块左边那一列上**真的画出来的字**（逐行）。"""
        strips = app.screen._compositor.render_strips()
        out = []
        for block in [*app.query(widgets_module.AnswerBlock),
                      *app.query(widgets_module.StreamAnswerBlock)]:
            style, color = block.styles.border_left
            assert style == "solid"
            assert _hex_of(color) == _hex_of(app.palette.accent), \
                (type(block).__name__, _hex_of(color))
            for y in range(block.region.y, block.region.y + block.region.height):
                if 0 <= y < len(strips):
                    out.append(str(strips[y].text)[block.region.x])
        return out

    # 1) 非流式：`ui(run_finished)` 一条整段答案。
    plain = _build_app(monkeypatch)
    async with plain.run_test(size=(100, 30)) as pilot:
        plain._inbox.put(("message", _init_message("s", stream=False)))
        _events(plain, {"kind": "run_started", "run_id": "r1", "step": 0,
                        "user_input": "你好"})
        plain._inbox.put(("message", {"v": 1, "t": "ui", "kind": "run_finished",
                                      "run_id": "r1", "answer": "正文一句。\n"}))
        await _settle(plain, pilot)
        assert list(plain.query(widgets_module.AnswerBlock)), "非流式该走 AnswerBlock"
        assert set(bars(plain)) == {"│"}, "非流式那条路的竖线没画出来"

    # 2) 流式（**默认**那条路）：逐块 delta。
    streamed = _build_app(monkeypatch)
    async with streamed.run_test(size=(100, 30)) as pilot:
        streamed._inbox.put(("message", _init_message("s", stream=True)))
        _events(streamed, {"kind": "run_started", "run_id": "r1", "step": 0,
                           "user_input": "你好"})
        for piece in ("正文", "一句", "。\n"):
            _delta(streamed, piece)
            await _settle(streamed, pilot)
        assert list(streamed.query(widgets_module.StreamAnswerBlock)), \
            "流式该走 StreamAnswerBlock"
        assert set(bars(streamed)) == {"│"}, "流式那条路的竖线没画出来"


@pytest.mark.anyio
async def test_the_autopilot_command_waits_for_the_runtime_before_showing_it_as_on(monkeypatch):
    """`/autopilot` 只**发请求**，指示灯等 runtime 那条快照 —— 不许乐观更新。

    反面的做法在这里格外危险：灯亮着、其实还在逐条问你，于是真的审批面板会被当成
    误报点掉。所以这条测试钉三件事：默认是关的、发出去的是绝对状态、以及"说了开"
    必须发生在收到 `ui state` 之后。
    """
    from agent_runtime.frontends.tui import widgets as widgets_module

    app = _build_app(monkeypatch)

    async with app.run_test(size=(120, 30)) as pilot:
        app._inbox.put(("message", _init_message("s")))
        await _settle(app, pilot)
        assert app.state.autopilot is False, "默认是关的"

        bar = app.query_one("#status", widgets_module.StatusBar)
        _left, right = bar.render_parts(app.state, app.palette, (time.time(), 120))
        text = str(right)
        assert "自动放行 关" in text
        assert text.index("自动放行 关") < text.index("上下文"), \
            "它要挨着「上下文」的左边（输入框上面那一行的右段开头）"

        app.submit("/autopilot")
        await _settle(app, pilot)
        assert [m for m in app._client.sent if m["t"] == "set_autopilot"] \
            == [{"t": "set_autopilot", "on": True}], "发出去的是绝对状态"
        assert app.state.autopilot is False, "还没收到 runtime 的确认，界面不许先改"
        assert "自动放行：开" not in _log_text(app), "也不许先说出来"

        # runtime 的确认（真跑时就是那条 `ui state` 快照）。
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "autopilot": True}))
        await _settle(app, pilot)
        assert app.state.autopilot is True
        assert "自动放行：开" in _log_text(app)

        # 再执行一次 → 关，而且这一次的措辞说的是"恢复逐条询问"。
        app.submit("/autopilot")
        await _settle(app, pilot)
        assert app._client.sent[-1] == {"t": "set_autopilot", "on": False}
        app._inbox.put(("message", {"v": 1, "t": "ui", "kind": "state",
                                    "autopilot": False}))
        await _settle(app, pilot)
        assert app.state.autopilot is False
        assert "自动放行：关" in _log_text(app)


@pytest.mark.anyio
async def test_the_command_palette_opens_on_slash_and_enter_runs_the_selection(monkeypatch):
    """设计稿改动 6：`/` 打开面板、`↑↓` 选、**回车执行的是选中的那条**。

    而命令本身照旧**不进 runtime** —— 面板改的是"怎么挑命令"，不是"命令由谁执行"。
    """
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        field = app.query_one("#input", widgets.PromptArea)
        field.text = "/"
        await _settle(app, pilot)

        palette = app.query_one("#palette", widgets.CommandPalette)
        assert palette.display is True
        assert palette.selected is not None and palette.selected.name == "/new"

        field.text = "/the"
        await _settle(app, pilot)
        assert palette.selected.name == "/theme"
        assert [c.name for c in palette.commands] == ["/theme"]

        app.submit("/the")
        await _settle(app, pilot)
        assert palette.display is False, "执行完要收起来"
        assert app._client.sent == [], "命令不该进 runtime"


@pytest.mark.anyio
async def test_escape_interrupts_the_running_turn_instead_of_quitting(monkeypatch):
    """F6 的键位表：`Esc` = **中断本轮**（没有弹层时）。

    它发的是 `interrupt` 而不是 `shutdown`：收摊会让当前这一轮跑完，而"我改主意了"
    要的恰恰是停下这一轮、会话留着。**界面不自己宣布"已停止"** —— 那由
    `run_finished(cancelled)` 那条事件说（第二份事实是这里最容易犯的错）。
    """
    app = _build_app(monkeypatch)

    async with app.run_test():
        app.action_escape_key()
        assert app._client.interrupts == 0, "没在跑的时候不该发中断"

        app.state.agent = agent_state.reduce(
            agent_state.initial(), {"t": "event", "kind": "run_started", "step": 0})
        app.action_escape_key()
        assert app._client.interrupts == 1


@pytest.mark.anyio
async def test_the_rail_opens_when_a_todo_list_appears_then_obeys_ctrl_b(monkeypatch):
    """决策 26 落到界面上：任务列表一出现就顶开（**窄屏也是**），之后 `Ctrl+B` 说了算。"""
    from textual.widgets import Static

    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        rail = app.query_one("#rail", widgets.ContextRail)
        assert rail.display is False, "默认收起"

        app.state.todos = [{"content": "写测试", "status": "in_progress"}]
        app._refresh_chrome()
        await pilot.pause()
        assert rail.display is True, "任务列表出现就顶开"

        # 收起之后**不许**被下一次刷新顶回来（边沿和电平的区别就在这一条）。
        app.action_toggle_rail()
        assert rail.display is False
        assert app.state.rail_pinned is True, "手动按过之后不再自动开合"
        app._refresh_chrome()
        await pilot.pause()
        assert rail.display is False, "同一批任务不该把收起的栏顶回来"

    # 窄屏同样顶开（决策 26 去掉了"窄屏一律不展开"）；那一行摘要让位。
    app2 = _build_app(monkeypatch)
    async with app2.run_test(size=(80, 24)) as pilot:
        app2.state.todos = [{"content": "写测试", "status": "in_progress"}]
        app2._refresh_chrome()
        await pilot.pause()
        assert app2.query_one("#rail").display is True, "窄屏也开"
        assert app2.query_one("#rail-summary", Static).display is False, "摘要让位给栏"


@pytest.mark.anyio
async def test_ctrl_t_toggles_the_turn_you_are_looking_at(monkeypatch):
    """设计稿改动 4：`Ctrl+T` 作用于**光标所在回合**，不再是"最后一段"。

    v1 那个写法（`list(state.thinking)[-1]`）从第二回合起就会作用到错的那一段上，
    而画面看起来完全正常 —— 所以这条测试用两个回合来钉它。
    """
    app = _build_app(monkeypatch)

    async with app.run_test(size=(140, 40)) as pilot:
        _events(app, {"kind": "run_started", "run_id": "r1", "step": 0,
                      "user_input": "第一轮"})
        _events(app, {"kind": "model_call", "run_id": "r1", "step": 1,
                      "status": "ok", "duration_ms": 5, "reasoning": "甲" * 300})
        # **把第一回合撑到超过一屏**：不然"视口在第一回合"这件事根本不成立
        # （内容全在屏幕上方，中心点落在空白处，那就退化成"最后一个回合"了）。
        for index in range(30):
            _events(app, {"kind": "tool_call", "run_id": "r1", "step": 1,
                          "tool": "read_file", "call_id": f"c{index}",
                          "tool_index": index, "arguments": f"a{index}.py"})
        _events(app, {"kind": "run_finished", "run_id": "r1", "step": 1,
                      "stop_reason": "answered", "duration_ms": 100})
        _events(app, {"kind": "run_started", "run_id": "r2", "step": 0,
                      "user_input": "第二轮"})
        _events(app, {"kind": "model_call", "run_id": "r2", "step": 1,
                      "status": "ok", "duration_ms": 5, "reasoning": "乙" * 300})
        # 第二回合也要够长 —— 否则滚到底时视口中心仍然落在第一回合里（它占了
        # 屏幕上大半），而那是**正确**的行为，不是 bug。
        for index in range(30):
            _events(app, {"kind": "tool_call", "run_id": "r2", "step": 1,
                          "tool": "read_file", "call_id": f"d{index}",
                          "tool_index": index, "arguments": f"b{index}.py"})
        await _settle(app, pilot)

        log = app.query_one("#log")
        log.scroll_home(animate=False)
        await pilot.pause()

        app.action_toggle_thinking()
        await pilot.pause()

        expanded = {run_id: flag for run_id, (_text, flag) in app.state.thinking.items()}
        assert expanded["r1"] is True, "视口在第一回合，动的就该是第一回合"
        assert expanded["r2"] is False
        assert "甲" * 300 in _log_text(app)
        assert "乙" * 300 not in _log_text(app)

        # 再按一次收回去：折叠行回来，正文消失。
        app.action_toggle_thinking()
        await pilot.pause()
        assert app.state.thinking["r1"][1] is False
        assert "甲" * 300 not in _log_text(app)
        assert "思考过程" in _log_text(app)

        # **滚到底之后，作用对象换成第二回合** —— 这就是 v1 那个写法做不到的事。
        log.scroll_end(animate=False)
        await pilot.pause()
        app.action_toggle_thinking()
        await pilot.pause()
        assert app.state.thinking["r2"][1] is True
        assert app.state.thinking["r1"][1] is False


@pytest.mark.anyio
async def test_theme_command_switches_all_thirteen_live(monkeypatch):
    """`/theme`：13 套在运行中换，而且**换完立刻重画**（不是等下一次事件）。

    不带参数从前是"列一张清单"，现在弹选择面板（`OptionPicker`）。所以这条测试
    跟着改成：候选**把每一套都摆出来**（含序号和 key），选中之后配色**当场**换掉、
    面板**立刻收掉** —— 配色是本地事实，没有"等 runtime 回话"那一段（和 `/model`
    的差别见设计稿 18.2）。
    """
    from agent_runtime.frontends.tui import widgets

    app = _build_app(monkeypatch)

    async with app.run_test() as pilot:
        assert app.theme == "A" and app.palette.name == "石墨琥珀"

        app.submit("/theme 墨绿")
        await _settle(app, pilot)
        assert app.theme == "C"
        assert "墨绿" in app.palette.name
        # 状态栏那一行的颜色跟着换了（它是手绘颜色的那些零件之一）。
        status = app.query_one("#status")
        assert status.display is True

        # 透明版也能在运行中换（`bg` 那一格交给终端）；深一档那套连横栏也交出去。
        app.submit("/theme a-t")
        await _settle(app, pilot)
        assert app.theme == "A-T"
        assert app.palette.transparent is True
        assert app.palette.clear_roles == ()
        app.submit("/theme 深")
        await _settle(app, pilot)
        assert app.theme == "A-T2"
        assert app.palette.transparent is True
        assert app.palette.clear_roles == ("chrome", "surface")

        # 认不出的名字不改配色，只说话。
        app.submit("/theme 不存在的颜色")
        await _settle(app, pilot)
        assert app.theme == "A-T2"
        assert "没有这套配色" in _log_text(app)

        # 不带参数 = 弹选择面板，13 套都在（序号 + key + 名字）。
        app.submit("/theme")
        await _settle(app, pilot)
        assert isinstance(app.screen, widgets.OptionPicker)
        picker_text = "\n".join(str(child.render())
                                for child in app.screen.query(".option"))
        for index, key in enumerate(theme_mod.ORDER, 1):
            assert f"{index:>2} {key} {theme_mod.get(key).name}" in picker_text
        # 当前那一套带 `●`（候选里一定有它，不标出来"选了却没反应"看起来像坏了）。
        # **认的是"标出来的就是现在这套"**，而不是某一套的名字：名字写死的话，
        # 上面换了哪一套这条就得跟着改（深透明那套的名字还含"石墨琥珀 · 透明"的
        # 前半段，写死会变成一个看着对、其实指着别处的断言）。
        from agent_runtime.frontends.tui import view_state
        marked = [str(option.line) for option in app._theme_options()
                  if option.line.role == view_state.ROLE_WAITING]
        assert len(marked) == 1, marked
        assert app.palette.name in marked[0]

        # 选中当场生效：把光标挪回原版那一套（`A`），按下去之后配色换回去、面板收掉
        # —— "选了一个已经在用的"不该变成一个什么都没发生。
        app.screen._index = theme_mod.ORDER.index("A")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert not isinstance(app.screen, widgets.OptionPicker), "选完立刻收掉"
        assert app.theme == "A"
