"""界面文案：两套目录的一致性，以及"这次用哪一套"的判定。

## 这个文件盯的是三件事

1. **两张目录表的键不许分家** —— 漏一条的症状是"英文界面里冒出一行中文"，而它
   恰好是最容易发生、最难在测试里发现的那类缺陷（中文是默认值，中文路径永远绿）；
2. **语言从哪儿来、认不出怎么办** —— 命令行的错要报、配置的错要报、配置**读不动**
   时不报（那份错误有它自己的那一站，见 `i18n.activate`）；
3. **模型那边的字一个都不许变** —— 这是这个功能唯一的硬边界：界面换英文，
   `prompts/system.zh.md`、`todo_note`/`job_note`/`skill_note` 必须逐字节不变。
   没有这条测试，"顺手把提示词也翻一下"是迟早的事，而症状是模型的回答语言变了。
"""

import json
import re

import pytest

from agent_runtime import i18n
from agent_runtime.protocol import state as agent_state

# 一条汉字都不许出现在英文文案里（工具名、路径、命令那种"数据"另说 —— 那些不经过
# `i18n`，见 module docstring 第 3 条的边界）。
HAN = re.compile("[\u4e00-\u9fff]")


@pytest.fixture(autouse=True)
def _back_to_chinese():
    """每条用例从"中文 + 一条都没漏"开始，结束时也复原。

    **自动复原是必须的**：漏了它，一条失败在断言上的用例会把语言留在英文，而后面的
    用例开始以英文跑 —— 红的是它们，而原因在几百行之外。
    """
    i18n.set_language(i18n.ZH)
    i18n.clear_missing()
    yield
    i18n.set_language(i18n.ZH)
    i18n.clear_missing()


def _set_ui(path, value) -> None:
    """把隔离配置改一改（`value=None` 表示整段删掉）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        data.pop("ui", None)
    else:
        data["ui"] = value
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --- 1. 两张表 -----------------------------------------------------------------

def test_the_two_catalogs_cover_the_same_keys():
    """中文表里每一条，英文表里都要有（反之亦然）。

    单复数那两条（`.one` / `.other`）是**英文特有**的：中文一句话就够，所以它们
    **顶替**中文的基键 —— 判据是"去掉后缀之后中文表里有"。反过来，中文有一条而
    英文既没有基键、也没有那对后缀，就是漏翻（这条测试抓的正是它）。
    """
    zh, en = i18n.catalog(i18n.ZH), i18n.catalog(i18n.EN)
    plural = {key for key in en if key.endswith((".one", ".other"))}
    basis = {key.rsplit(".", 1)[0] for key in plural}
    base = set(en) - plural

    assert not (set(zh) - base - basis), \
        f"英文表缺这几条：{sorted(set(zh) - base - basis)}"
    assert not (base - set(zh)), f"中文表缺这几条：{sorted(base - set(zh))}"
    assert basis <= set(zh), f"单复数那两条没有对应的中文：{sorted(basis - set(zh))}"


def test_a_plural_pair_comes_in_pairs():
    """有 `.one` 就必须有 `.other` —— 少了的那一半会静默退回中文。"""
    en = i18n.catalog(i18n.EN)
    for key in [k for k in en if k.endswith(".one")]:
        assert key[: -len(".one")] + ".other" in en, f"{key} 没有配对的 .other"


def test_no_translation_is_empty():
    """空串在界面上就是"什么都不显示"，而它和"忘了翻"长得一模一样。"""
    for lang in i18n.LANGS:
        empty = sorted(k for k, v in i18n.catalog(lang).items() if not v.strip())
        assert not empty, f"{lang} 表里这几条是空的：{empty}"


def test_the_english_catalog_has_no_chinese_in_it():
    """英文表里**一个汉字都不该有** —— 那是"把中文原文抄过去当翻译"的现形处。

    它比"键齐不齐"更靠前一层：键齐了、值却是中文原文，两张表照样"一致"，
    而界面上看到的是中文。真有需要保留的中文（比如某个专有名词）时，往这里加
    一条例外并写清理由 —— 不要放宽整条判据。
    """
    bad = sorted(k for k, v in i18n.catalog(i18n.EN).items() if HAN.search(v))
    assert not bad, f"英文表里这几条还是中文：{bad}"


# --- 2. 取文案 -----------------------------------------------------------------

def test_a_missing_translation_falls_back_to_chinese(monkeypatch):
    """当前语言缺一条 ⇒ 回落中文、**不崩**，但要记账（不然它会永远没人发现）。"""
    monkeypatch.delitem(i18n.catalog(i18n.EN), "activity.thinking")
    i18n.set_language(i18n.EN)

    assert i18n.t("activity.thinking") == i18n.catalog(i18n.ZH)["activity.thinking"]
    assert "activity.thinking" in i18n.missing()


def test_a_key_nobody_has_is_a_loud_error():
    """写错一个键名是**编程错误**，必须当场响（和"多写一个配置键"同一条规矩）。"""
    with pytest.raises(KeyError) as caught:
        i18n.t("no.such.key")
    assert "no.such.key" in str(caught.value)


def test_placeholders_are_filled():
    assert i18n.t("activity.tool_call", tool="read_file", index="") == "要调用 read_file"


def test_a_placeholder_mismatch_is_a_loud_error():
    """表里写了 `{index}` 而调用方没给 —— 报出来，别渲染成 `{index}` 给用户看。"""
    with pytest.raises(KeyError):
        i18n.t("activity.tool_call", tool="read_file")


def test_numbers_pick_the_plural_form():
    i18n.set_language(i18n.EN)
    assert i18n.tn("activity.tool_batch", 1) == "1 read-only tool running in parallel"
    assert i18n.tn("activity.tool_batch", 3) == "3 read-only tools running in parallel"
    i18n.set_language(i18n.ZH)
    assert i18n.tn("activity.tool_batch", 3) == "3 个只读工具并发执行中"


def test_with_language_puts_it_back():
    with i18n.with_language(i18n.EN):
        assert i18n.current() == i18n.EN
    assert i18n.current() == i18n.ZH


# --- 3. 这次用哪一套 -----------------------------------------------------------

def test_the_config_section_decides_the_language(isolated_user_config):
    _set_ui(isolated_user_config, {"language": "en"})
    assert i18n.activate() == i18n.EN


def test_a_missing_ui_section_means_chinese(isolated_user_config):
    """**老配置照样跑** —— 这是加这个功能时唯一不能破的东西（行为逐字节不变）。"""
    _set_ui(isolated_user_config, None)
    assert i18n.activate() == i18n.ZH


def test_an_unknown_language_in_the_config_is_an_error(isolated_user_config):
    """写错一个语言值**不许静默回默认**：用户看到的是"我配的英文没生效"。"""
    _set_ui(isolated_user_config, {"language": "en_US"})
    with pytest.raises(i18n.LangError) as caught:
        i18n.activate()
    assert "en_US" in str(caught.value)


def test_an_unreadable_config_falls_back_instead_of_failing(isolated_user_config):
    """配置文件本身坏了 ⇒ 退回默认语言，**这一层不报**。

    它的报错有自己那一站（`composition.check_config` / `open_runtime`），在这里抢着说
    会让用户看到两句话，而其中一句还是关于一个次要问题的。
    """
    isolated_user_config.write_text("{ 这不是 JSON", encoding="utf-8")
    assert i18n.activate() == i18n.ZH


def test_the_command_line_wins_over_the_config(isolated_user_config):
    _set_ui(isolated_user_config, {"language": "zh"})
    assert i18n.activate("en") == i18n.EN


def test_the_language_name_is_case_insensitive_and_trimmed():
    assert i18n.validate(" EN ") == i18n.EN
    with pytest.raises(i18n.LangError):
        i18n.validate("fr")


# --- 4. 状态栏那一行（第一处真的翻过来的文案） ---------------------------------

# 一条事件一行，覆盖 `_on_event` 里所有会产生 activity 的分支。
_ACTIVITY_EVENTS = [
    {"t": "event", "kind": "run_started", "run_id": "r", "step": 0},
    {"t": "event", "kind": "model_call", "run_id": "r", "step": 1},
    {"t": "event", "kind": "model_call", "status": "error", "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_call", "tool": "read_file", "tool_index": 0,
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_call", "tool": "read_file", "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_result", "tool": "read_file", "status": "ok",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_result", "tool": "shell", "status": "denied",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_result", "tool": "shell", "status": "invalid_args",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "tool_batch", "calls": 3, "run_id": "r", "step": 1},
    {"t": "event", "kind": "permission", "tool": "shell", "outcome": "approved",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "permission", "tool": "shell", "outcome": "user_denied",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "permission", "tool": "shell", "outcome": "policy_denied",
     "run_id": "r", "step": 1},
    {"t": "event", "kind": "permission", "tool": "shell", "outcome": "no_asker",
     "run_id": "r", "step": 1},
]


def test_the_activity_line_speaks_english():
    """英文模式下那一行**一个汉字都没有**。

    它是"英文界面里冒中文"最容易漏的一处：这句话不在前端里，它由 `protocol/state.py`
    生成（三个前端共用），所以只翻`view_state.py` 是看不出来的。
    """
    with i18n.with_language(i18n.EN):
        for event in _ACTIVITY_EVENTS:
            state = agent_state.reduce(agent_state.initial(), event)
            assert not HAN.search(state.activity), \
                f"{event['kind']}/{event.get('status') or event.get('outcome') or ''}" \
                f" → {state.activity!r}"


def test_the_command_table_speaks_english():
    """命令面板和 `/help` 在英文下**一个汉字都没有**。

    这两处是用户最先看到的东西（输入一个 `/` 就出来），所以它们值得一条自己的
    判据：命令名是 ASCII、`takes_arg` 是事实，而 `hint`/`detail` 全是文案。
    """
    from agent_runtime.frontends.tui import view_state

    with i18n.with_language(i18n.EN):
        for command in view_state.COMMANDS:
            assert not HAN.search(command.hint), f"{command.name} 的 hint 还是中文"
            assert not HAN.search(command.detail), f"{command.name} 的 detail 还是中文"


def test_the_rendered_lines_speak_english():
    """已经搬过来的那些渲染函数，在英文下**一个汉字都没有**。

    这一条是"英文界面"最直接的判据，而且它能顶住以后新加的文案：`view_state` 的
    渲染全是纯函数（给事件还几行字），所以这里喂一份**人造的事件流**就能把状态栏、
    回合头、过程行、工具行、思维链、左栏六块、摘要、`/status` 那一屏全过一遍。

    用户输入和工具名是**数据**（ASCII 的 `read_file` / `git add`），模型正文根本
    不经过这些函数 —— 所以"整行不含汉字"成立。
    """
    from agent_runtime.frontends.tui import view_state

    state = view_state.ViewState()
    state.session_id = "s1"
    state.model = "deepseek-flash"
    state.messages = 12
    state.steps = 7
    state.prompt_tokens = 2000
    state.cached_tokens = 500
    state.context_tokens = 100000
    state.max_steps = 80
    state.todos = [{"content": "do the thing", "status": "in_progress"}]
    state.skills = [{"name": "pdf", "digest": "x"}]
    state.jobs = [{"id": "j1", "command": "sleep", "state": "uncollected",
                   "seconds": 3, "exit_code": 0}]
    state.mcp = [{"name": "kb", "state": "loaded", "tools": 2}]
    state.risk_scope = [{"risk": "low", "disposition": "auto"},
                        {"risk": "high", "disposition": "ask"}]
    state.tool_risks = {"read_file": "low", "shell": "high"}
    state.agents_md = [{"path": "AGENT.md", "lines": 12, "status": "loaded"}]

    events = [
        {"kind": "run_started", "run_id": "r1", "step": 0, "user_input": "do it"},
        {"kind": "model_call", "run_id": "r1", "step": 1, "status": "ok",
         "duration_ms": 1200, "prompt_tokens": 1200, "cached_tokens": 100},
        {"kind": "model_call", "run_id": "r1", "step": 1, "status": "error",
         "attempt": 2, "backoff_ms": 500},
        {"kind": "tool_call", "run_id": "r1", "step": 1, "tool": "read_file",
         "tool_index": 0, "call_id": "c1", "arguments": '{"path": "a.py"}'},
        {"kind": "tool_result", "run_id": "r1", "step": 1, "tool": "read_file",
         "tool_index": 0, "call_id": "c1", "status": "ok", "chars": 1024,
         "duration_ms": 5},
        {"kind": "tool_result", "run_id": "r1", "step": 1, "tool": "shell",
         "tool_index": 1, "call_id": "c2", "status": "denied", "chars": 0},
        {"kind": "tool_batch", "run_id": "r1", "step": 1, "calls": 3, "wall_ms": 12},
        {"kind": "permission", "run_id": "r1", "step": 1, "tool": "shell",
         "outcome": "approved", "rule": ["git", "add"], "remembered": ["shell"],
         "waited_ms": 2400},
        {"kind": "permission", "run_id": "r1", "step": 1, "tool": "shell",
         "outcome": "user_denied"},
        {"kind": "run_finished", "run_id": "r1", "step": 2,
         "stop_reason": "max_steps", "duration_ms": 4200},
        {"kind": "run_finished", "run_id": "r1", "step": 2,
         "stop_reason": "cancelled", "duration_ms": 4200},
    ]
    status_message = {
        "status": {
            "session": {"id": "s1", "resumed": True, "workspace": "C:/w",
                        "messages": 12, "steps": 7},
            "model": {"current": "deepseek-flash", "selected": "deepseek-v4-pro",
                      "provider": "deepseek", "base_url": "https://x.example/v1",
                      "reasoning": {"thinking": False, "effort": "high"}},
            "counters": {"runs": 3, "model_calls": 9, "tool_calls": 12,
                         "permission_waits": 2, "asks": 1},
            "usage": {"prompt": 12000, "cached": 3000, "completion": 800},
            "meta": {"max_steps": 80, "stream": True, "autopilot": False,
                     "tool_count": 14, "audit_path": "C:/w/.tudouni/logs/s1.jsonl"},
        },
        "last_prompt_tokens": 20000,
        "context_tokens": 100000,
    }

    with i18n.with_language(i18n.EN):
        i18n.clear_missing()
        chunks: list[str] = []
        for event in events:
            chunks.extend(str(line) for line in view_state.render_event(state, event))
        for _title, _count, lines in view_state.rail_blocks(state):
            chunks.extend(str(line) for line in lines)
        chunks.extend(str(line) for line in view_state.render_status(state, status_message))
        chunks.append(view_state.rail_summary(state))
        chunks.append(state.status_left(""))
        chunks.append(state.status_right(None, False))
        chunks.extend(str(state.autopilot_badge()))
        chunks.extend(str(state.jobs_badge()))
        chunks.append(view_state.motto_of_day(1))
        for seconds in (30, 300, 7200, 3 * 86400):
            chunks.append(view_state.time_ago(seconds))
        chunks.append(view_state.session_row({"session_id": "s1", "messages": 2,
                                              "steps": 1, "preview": "hi",
                                              "todos": "1/2"}))
        # 三条命令的答案（`/tools` `/model` `/thinking` `/effort`）。
        chunks.extend(str(line) for line in view_state.render_tools(state, {
            "tools": [{"name": "read_file", "risk": "low", "disposition": "auto",
                       "parallel_safe": True, "interactive": False},
                      {"name": "shell", "risk": "high", "disposition": "ask",
                       "granted": True, "external": True, "interactive": True}],
            "granted_prefixes": ["git add"],
        }))
        chunks.extend(str(line) for line in view_state.render_models(state))
        chunks.extend(str(line) for line in view_state.render_thinking(state))
        chunks.extend(str(line) for line in view_state.render_effort(
            state, ("low", "high")))
        chunks.extend(str(line) for line in view_state.render_mcp({
            "mcp_notes": ["[MCP] server kb mounted: 3 tools"],
            "mcp_servers": [{"name": "kb", "state": "loaded", "tools": 3},
                            {"name": "web", "state": "failed",
                             "error": "boom"}],
        }))
        chunks.append(str(view_state.waiting_line({"tool": "shell"})))
        chunks.append(str(view_state.mcp_line({"name": "kb", "state": "loaded",
                                               "tools": 3})))
        # 欢迎屏那几行（控件之外的纯函数部分）与键位表。
        from agent_runtime.frontends.tui import widgets as widgets_module

        chunks.append(str(widgets_module._recent_row(
            {"preview": "hello", "modified_at": None}, widgets_module.theme_mod.get("A"),
            None)))
        for key, what in [*widgets_module.hint_keys(),
                          *widgets_module.hint_keys(narrow=True),
                          *widgets_module.extra_hint_keys()]:
            chunks.extend([key, what])
        # **一条都没漏翻**：`missing()` 只要不是空的，上面那些行里就一定混着中文
        # （回落是静默的，只有这个集合会说）。
        assert not i18n.missing(), f"英文表缺这几条：{sorted(i18n.missing())}"

    bad = [text for text in chunks if HAN.search(text)]
    assert not bad, f"英文界面里还有中文：{bad[:5]}"


def test_the_activity_line_still_speaks_chinese_by_default():
    """默认那一套一个字都没变（老测试断的就是它）。"""
    state = agent_state.reduce(
        agent_state.initial(),
        {"t": "event", "kind": "tool_call", "tool": "read_file", "tool_index": 1,
         "run_id": "r", "step": 1},
    )
    assert state.activity == "要调用 read_file（第 2 个）"


# --- 5. 模型那边一个字都不许变 -------------------------------------------------

def test_the_hint_box_is_tall_and_wide_enough_in_both_languages():
    """提示框的**行数和列宽**都要装得下两套语言排出来的键位。

    这是英文模式下最容易"真的坏"的一处：中文 4 条一行正好两行，而英文那几条长得多
    （`Esc Interrupt this turn` 一条 22 列），4 条一行会折成四行 —— 而框只有两行高，
    多出来的被 Textual **静默裁掉**（没有报错、没有滚动条，看起来只是"少了几条提示"）。
    所以这里按显示列数量一遍：每条一行放得下，而且总行数装得进框高。
    """
    from rich.cells import cell_len

    from agent_runtime.frontends.tui import widgets

    for lang in i18n.LANGS:
        with i18n.with_language(lang):
            pairs = widgets.hint_keys()
            per_row = widgets.hint_per_row()
            rows = [pairs[index:index + per_row]
                    for index in range(0, len(pairs), per_row)]
            for row in rows:
                text = "   ".join(f"{key} {what}" for key, what in row)
                assert cell_len(text) <= widgets.WELCOME_HINT_TEXT, (lang, text)
            # 框高里那两行是边框（Textual 会扣掉），剩下的才是正文能用的行数。
            assert len(rows) <= widgets.hint_box_height() - 2, (lang, len(rows))


def test_the_system_prompt_does_not_follow_the_ui_language():
    """**界面语言和模型语言是两条线。**

    `prompts/system.zh.md` 是模型的行为准则：它换不换语言是**另一个决定**（而且会
    改变模型的回答语言），不该被"界面换成英文"顺手带走。这条测试就是那道闸。
    """
    from agent_runtime.state.session import load_system_prompt

    chinese = load_system_prompt()
    with i18n.with_language(i18n.EN):
        assert load_system_prompt() == chinese


def test_the_notes_we_hand_the_model_do_not_follow_the_ui_language():
    """`todo_note` / `progress_line` 那一对是现成的接缝：**只有给人看的那一半翻。**

    `todo_note` 每轮贴在载荷尾部交给模型，`progress_line` 是左栏和通知里那一行。
    这里把两者都钉住：前者两种语言下必须一样，后者必须真的跟着变 —— 不然"翻错了
    一半"（把给模型的那份翻了，或者该翻的没翻）两种错都不会被发现。
    """
    from agent_runtime.tools.builtin import todo

    metadata = {"todos": [{"content": "写测试", "status": "in_progress"}]}
    chinese_note = todo.todo_note(metadata)

    with i18n.with_language(i18n.EN):
        # 给模型的那一份：**不许跟着界面语言走**。这条断言就是"别顺手翻它"。
        assert todo.todo_note(metadata) == chinese_note
        # 给人看的那一份：跟着变，而且一个汉字都没有（"写测试"是**数据**，来自模型，
        # 所以这里只看那几句固定的字）。
        line = todo.progress_line(metadata)
        assert "done" in line and "now:" in line, line
        assert not HAN.search(line.replace("写测试", "")), line
