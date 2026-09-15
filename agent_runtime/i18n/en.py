"""英文目录表。**键和 `zh.py` 一一对应**，缺一条就是英文界面里冒一行中文。

写法上的两条取舍（用户拍的板）：

  * **完整措辞，不缩写** —— `12 messages` 而不是 `12 msgs`、`Ctrl+T Thinking`
    而不是 `Ctrl+T think`。代价是更宽，所以版式那一批按中文列宽写死的常数要跟着
    按语言取（见 `widgets.py` 里 `WELCOME_*` 那几条）；
  * **数字那一格用单复数两条**（`.one` / `.other`），`tn()` 按 `n` 选。中文表只写
    一条，所以这不是"两份事实"，是"英文真的有两句话"。
"""

CATALOG: dict[str, str] = {
    # --- 状态栏那一行（`protocol/state.py` 的 `activity`） --------------------
    "activity.preparing": "Preparing",
    "activity.thinking": "Model is thinking",
    "activity.retrying": "Model call failed, retrying",
    "activity.tool_call": "Calling {tool}{index}",
    "activity.tool_call_index": " (call {n})",
    "activity.tool_result": "{tool} {verb}",
    "activity.result.ok": "returned",
    "activity.result.denied": "was denied",
    "activity.result.invalid_args": "had invalid arguments",
    "activity.result.error": "failed",
    "activity.tool_batch": "{n} read-only tools running in parallel",
    # 数字那一格要分单复数：`1 read-only tools` 是错的。
    "activity.tool_batch.one": "{n} read-only tool running in parallel",
    "activity.tool_batch.other": "{n} read-only tools running in parallel",
    "activity.permission": "{tool} {outcome}",
    "activity.permission.approved": "approved",
    "activity.permission.user_denied": "denied by you",
    "activity.permission.policy_denied": "blocked by policy",
    "activity.permission.no_asker": "has no approval channel",
    "activity.some_tool": "tool",

    # --- `/theme`（配色）-----------------------------------------------------
    "theme.picker_title": "Theme",
    "theme.unknown": "No such theme: {name} (/theme)",
    "theme.switched": "Theme set to ",
    "theme.only_this_run": " (this run only)",

    # --- 命令面板与 `/help`（`view_state.COMMANDS`）--------------------------
    "cmd.new.hint": "Start a new session",
    "cmd.resume.hint": "Switch session",
    "cmd.resume.detail": "Without an argument it lists them; /resume <id> switches directly",
    "cmd.audit.hint": "Where the audit log is",
    "cmd.exit.hint": "Quit",
    "cmd.help.hint": "Commands and keys",
    "cmd.theme.hint": "Change the theme",
    "cmd.theme.detail": "Without an argument it opens the picker; "
                        "/theme <name|key|number> switches directly",
    "cmd.skills.hint": "See all skills",
    "cmd.autopilot.hint": "Auto-approve switch",
    "cmd.quiet.hint": "Quiet mode switch",
    "cmd.quiet.detail": "Without an argument it toggles; /quiet on or /quiet off sets it",
    "cmd.status.hint": "See the current status",
    "cmd.tools.hint": "Tools and permissions",
    "cmd.model.hint": "Change the model",
    "cmd.model.detail": "Without an argument it opens the picker; /model <name> switches "
                        "directly (the name must be exact)",
    "cmd.thinking.hint": "Thinking switch",
    "cmd.thinking.detail": "Without an argument it shows the setting; "
                           "/thinking on or /thinking off changes it",
    "cmd.effort.hint": "Thinking effort",
    "cmd.effort.detail": "Without an argument it opens the picker; "
                         "/effort <level> changes it directly",
    "cmd.mcp.hint": "MCP server switch",
    "cmd.mcp.detail": "Without an argument it opens the panel; "
                      "/mcp load|unload <name> changes one directly",

    # --- 时间那一格（欢迎屏「最近活动」的左边一列）----------------------------
    # 英文要单复数两条；中文只给一条（见 `tn()`）。
    "time.just_now": "just now",
    "time.minutes_ago.one": "{n} minute ago",
    "time.minutes_ago.other": "{n} minutes ago",
    "time.hours_ago.one": "{n} hour ago",
    "time.hours_ago.other": "{n} hours ago",
    "time.days_ago.one": "{n} day ago",
    "time.days_ago.other": "{n} days ago",

    # --- 箴言（欢迎屏右下那一句，按日期轮换）----------------------------------
    "motto.1": "Decide what you want before you start.",
    "motto.2": "If you can write the test, you understand it.",
    "motto.3": "Change one thing, and only that thing.",
    "motto.4": "Don't edit code you don't understand.",
    "motto.5": "Small steps, look back often.",
    "motto.6": "Write for the next person who reads it.",
    "motto.7": "Failures should be loud, never swallowed.",
    "motto.8": "Repeated a third time? Extract it.",
    "motto.9": "Make it right first, then make it fast.",
    "motto.10": "A wrong name leads to wrong code.",
    "motto.11": "Comments explain why, not what.",
    "motto.12": "Nothing is done until it has run.",
    "motto.13": "Interfaces outlive implementations.",
    "motto.14": "Deleting a line is worth as much as writing one.",
    "motto.15": "Today's decision is tomorrow's default.",

    # --- 枚举 → 说法（`view_state.outcome_text` / `stop_reason_text`）---------
    "outcome.approved": "Approved",
    "outcome.autopilot": "Auto-approved",
    "outcome.user_denied": "Denied",
    "outcome.policy_denied": "Blocked by policy",
    "outcome.no_asker": "No approval channel",
    "stop.answered": "Answered",
    "stop.max_steps": "Step limit reached",
    "stop.cancelled": "Interrupted",
    "stop.model_error": "Model failed",

    # --- 权限那一行（`view_state._permission_line`）---------------------------
    "permission.line_prefix": "  · permission ",
    "permission.rule_hit": "matched rule {rule}",
    "permission.remembered": "remembered {remembered}",
    "permission.waited": "you took {duration}",
    "permission.tail": " ({extras})",

    # --- 回合头 --------------------------------------------------------------
    "turn.head": "Turn {index}",
    "turn.state.one": "{n} step · {duration} · {outcome}",
    "turn.state.other": "{n} steps · {duration} · {outcome}",
    "turn.max_steps_warning": "  ! Step limit reached — this turn did **not** wrap up; "
                             "the session is fine, you can keep going.",
    "turn.cancelled_warning": "  ! Stopped at your request "
                              "(it stopped between two steps; the session is intact).",

    # --- 拼一串东西时那个分隔符 ----------------------------------------------
    "list.separator": ", ",
    "list.separator_semicolon": "; ",

    # --- 状态栏：左段（agent 在干什么）+ 右段（成本与去处）--------------------
    "status.settled.answered": "Answered",
    "status.settled.limited": "Step limit reached",
    "status.settled.failed": "This turn failed",
    "status.settled.cancelled": "Interrupted",
    "status.idle": "Idle",
    "status.idle.new": "Idle · starts after your first message",
    "status.step": "step {step} / {total}",
    "status.context.none": "context  —",
    "status.context.plain": "context {used} / {total}",
    "status.context.percent": "context {used} / {total} ({percent}%)",
    "status.context.used": "context {used}",
    "status.hit": "cache hit {percent}%",
    "status.hit.none": "cache hit  —",
    "status.turn": "this turn {duration}",
    "status.session.messages.one": "{n} message",
    "status.session.messages.other": "{n} messages",
    "status.session.steps.one": "{n} step",
    "status.session.steps.other": "{n} steps",
    "status.session.span": "session {messages} · {steps}",
    "status.session.none": "session  —",
    "status.audit": "audit {path}",
    "status.autopilot.on": "auto-approve on",
    "status.autopilot.off": "auto-approve off",
    "status.autopilot.on_short": "auto on",
    "status.autopilot.off_short": "auto off",
    "status.quiet": "quiet",
    "status.jobs.collected": "background: {n} collected",
    "status.jobs.running.one": "{n} background job",
    "status.jobs.running.other": "{n} background jobs",
    "status.jobs.uncollected": " · {n} to collect",

    # --- 过程行：模型调用 / 思维链 / 工具调用 / 工具结果 ----------------------
    "event.model_retry": "  · Model call failed (attempt {attempt}){tail}",
    "event.model_retry_wait": ", retrying in {backoff}ms",
    "event.denied": "      (denied, not executed)",
    "event.tool_batch.one": "{n} read-only tool finished in parallel",
    "event.tool_batch.other": "{n} read-only tools finished in parallel",
    "event.tool_batch_wall": " ({wall}ms)",
    "event.model_prefix": "  · model ",
    "event.context_tokens": "  context {tokens} tokens",
    "event.cache_hit": " (cache hit {cached} · {percent}%)",
    "turn.running": "running · step 1",
    "think.prefix_folded": "  ▸ Thinking",
    "think.folded_tail.one": " ({chars} char · Ctrl+T to expand)",
    "think.folded_tail.other": " ({chars} chars · Ctrl+T to expand)",
    "think.prefix_expanded": "  ▾ Thinking",
    "think.expanded_tail": " (expanded · Ctrl+T to collapse)",
    "think.live_chars.one": " {chars} char",
    "think.live_chars.other": " {chars} chars",
    "risk.high": "   HIGH risk",
    "risk.medium": "   MEDIUM risk",
    "tool.ok_tail.one": "{chars} char   {span}",
    "tool.ok_tail.other": "{chars} chars   {span}",
    "tool.result_chars.one": "{chars} char",
    "tool.result_chars.other": "{chars} chars",
    "tool.denied": "denied, not executed",
    "tool.invalid_args": "invalid arguments, not executed",
    "tool.error.one": "failed ({chars} char)",
    "tool.error.other": "failed ({chars} chars)",
    "brief.items.one": "{n} item",
    "brief.items.other": "{n} items",
    "brief.todo_items.one": "{n} task",
    "brief.todo_items.other": "{n} tasks",
    "brief.yes": "yes",
    "brief.no": "no",

    # --- 左栏六块（标题、空态、每一行）---------------------------------------
    "rail.jobs": "Background jobs",
    "rail.jobs.empty": "No background jobs",
    "rail.jobs.empty_hint": "Jobs started by shell_background show up here",
    "rail.job.running": "running {span}",
    "rail.job.uncollected": "finished (exit {code}) · result not collected",
    "rail.job.killed": "collected",
    "rail.job.done": "finished (exit {code}) · collected",
    "rail.tasks": "Tasks",
    "rail.tasks.empty": "No tasks yet",
    "rail.tasks.empty_hint": "Tasks the agent creates show up here",
    "rail.skills": "Loaded skills",
    "rail.skills.empty": "No skills loaded",
    "rail.skills.empty_hint": "Anything load_skill reads stays loaded",
    "rail.permissions": "Permissions",
    "rail.permission.auto": "auto",
    "rail.permission.ask": "ask",
    "rail.permission.granted": "always allowed",
    "rail.permission.prefixes": "command rules",
    "rail.permission.denied": "denied outright ",
    "rail.permission.by_level": "by risk level (runtime reported no scope)",
    "rail.session": "Session",
    "rail.session.empty": "No session yet",
    "rail.session.empty_hint": "The file appears after your first message",
    "rail.session.thinking_off": "thinking off",
    "rail.session.effort": "  effort {effort}",
    "rail.session.messages.one": "{n} message",
    "rail.session.messages.other": "{n} messages",
    "rail.session.steps.one": "{n} step",
    "rail.session.steps.other": "{n} steps",
    "rail.session.size": "{messages} · {steps}",
    "rail.session.agent_md_lines.one": "{n} line",
    "rail.session.agent_md_lines.other": "{n} lines",
    # 收起左栏时那一行摘要（窄屏）。
    "rail.summary.expand": "Ctrl+B to show the context rail",
    "rail.summary.jobs.one": "{n} background job",
    "rail.summary.jobs.other": "{n} background jobs",
    "rail.summary.jobs_collected.one": "{n} background job (all collected)",
    "rail.summary.jobs_collected.other": "{n} background jobs (all collected)",
    "rail.summary.mcp.one": "{n} MCP server",
    "rail.summary.mcp.other": "{n} MCP servers",
    "rail.summary.tasks.one": "{done}/{total} task",
    "rail.summary.tasks.other": "{done}/{total} tasks",
    "rail.summary.skills.one": "{n} skill",
    "rail.summary.skills.other": "{n} skills",
    "rail.summary.asking": "{risks} ask",
    "rail.summary.all_auto": "all auto-approved",

    # --- `/status` 那一屏（`render_status`）-----------------------------------
    "status.title": "Status",
    "status.no_state": "(no status yet: this session has not run a single step)",
    "status.kv.session": "Session",
    "status.kv.workspace": "Workspace",
    "status.kv.size": "Size",
    "status.kv.model": "Model",
    "status.kv.endpoint": "Endpoint",
    "status.kv.thinking": "Thinking",
    "status.kv.context": "Context",
    "status.kv.input_total": "Total input",
    "status.kv.output_total": "Total output",
    "status.kv.usage_total": "Total usage",
    "status.kv.turns": "Turns",
    "status.kv.run": "This run",
    "status.kv.tools": "Tools",
    "status.kv.audit": "Audit",
    "status.session_id": "{name} ({started})",
    "status.started.resumed": "started: resumed",
    "status.started.new": "started: new",
    "status.model.pending": "{current} (switching to {selected} on the next request)",
    "status.thinking.on": "on · {effort}",
    "status.thinking.off": "off (the effort is remembered; /thinking on brings it back)",
    "status.context.unknown": "— (no successful model call yet)",
    "status.context.ratio": "{used} / {window} ({percent}%)",
    "status.context.no_window": "{used} (this model's window is not in the catalog, "
                               "so no percentage)",
    "status.usage.input": "{tokens} tokens (cache hit {cached}, rate {rate})",
    "status.usage.output": "{tokens} tokens",
    "status.usage.none": "No successful model call yet",
    "status.turns.value": "{runs} runs · {model_calls} model calls · "
                          "{tool_calls} tool calls{tail}",
    "status.turns.waits": " (of which {waits} approval waits",
    "status.turns.asks": ", {asks} questions)",
    "status.turns.close": ")",
    "status.run.max_steps": "up to {n} steps",
    "status.run.stream": "streaming",
    "status.run.no_stream": "not streaming",
    "status.run.autopilot": "auto-approve",
    "status.run.ask": "ask each time",
    "status.tools.count": "{n} tools (/tools for the list)",

    # --- MCP（左栏那一块 + `/mcp` 面板与会话流）------------------------------
    "rail.mcp": "Background MCP",
    "rail.mcp.empty": "No MCP server is mounted",
    "rail.mcp.empty_hint": "/mcp lists them and mounts them one by one",
    "rail.mcp.tools.one": "  {n} tool",
    "rail.mcp.tools.other": "  {n} tools",
    "mcp.tools.one": "{n} tool",
    "mcp.tools.other": "{n} tools",
    "mcp.not_connected": "not connected: {error}",
    "mcp.no_reason": "(no reason given)",
    "mcp.not_loaded": "not loaded",
    "mcp.summary": "MCP: {running} running / {total} configured "
                   "(/mcp opens the panel to toggle them)",

    # --- 会话清单里的一行（`session_row`，`/resume` 面板）---------------------
    "session.row": "  {mark}{name} {messages} · {steps}   {preview}{todos}",
    "session.row.messages.one": "{n:>4} message",
    "session.row.messages.other": "{n:>3} messages",
    "session.row.steps.one": "{n:>4} step",
    "session.row.steps.other": "{n:>3} steps",
    "session.row.untitled": "(nothing said yet)",
    "session.row.todos": "   [tasks {todos}]",

    # --- 等审批那一行（`waiting_line`）---------------------------------------
    "waiting.prompt": "  · waiting for your approval",
    "waiting.allow": "   [y] allow",
    "waiting.deny": "   [n] deny",
    "waiting.always": "   [t] always allow",
    "waiting.allow_all": "   [a] allow all",
    "waiting.escape": "   [Esc] deny",

    # --- `/tools` 那一屏（`render_tools`）------------------------------------
    "tools.none": "No tool was registered this run (this happens when an engine or "
                  "a key is missing; the startup lines say why)",
    "tools.disposition.auto": "auto",
    "tools.disposition.ask": "needs approval",
    "tools.disposition.deny": "denied",
    "tools.mark.granted": "you pressed t",
    "tools.mark.external": "external",
    "tools.mark.interactive": "asks you",
    "tools.mark.parallel": "parallel-safe",
    "tools.prefixes": "  command rules (allowed by prefix; only for tools that take "
                      "a command line, like shell): {rules}",
    "tools.footer": "  Change these in .tudouni/permissions.json; pressing t at an "
                    "approval writes them",

    # --- `/model`（面板那一行 + 兜底清单）------------------------------------
    "model.window": " · context {window}",
    "model.detail_tail": "   ({detail})",
    "model.no_catalog": "(the runtime sent no model catalog: a child process started "
                        "before this protocol version?)",
    "model.current": "Current model: {name}",
    "model.aliases": "  legacy names: {old} → {new}",
    "model.howto": "Switch with /model <name> (the name must be exact; write "
                   "provider/model when two routes share a name)",
    "model.unknown": "(the catalog has no such name)",

    # --- `/thinking` 与 `/effort` --------------------------------------------
    "thinking.mode": "Thinking: {state}",
    "thinking.on": "on",
    "thinking.off": "off",
    "thinking.effort": "  effort: {effort}",
    "thinking.effort_off_note": " (unused while off, but remembered)",
    "thinking.howto": "Change it: /thinking on   ·   /thinking off",
    "thinking.unknown": "(unrecognized value: {rest})",
    "effort.current": "Thinking effort: {effort}",
    "effort.off_note": " (thinking is off; this applies once you turn it on)",
    "effort.no_catalog": "(the runtime sent no effort levels: a child process started "
                         "before this protocol version?)",
    "effort.howto": "Change it: /effort {levels}",
    "effort.howto_bare": "Change it: /effort <level>",
    "effort.unknown": "(no such level: {rest})",
    # 清单标题（`/model` 和 `/effort` 共用）。
    "list.available": "Available:",

    # --- 顶栏与会话头 --------------------------------------------------------
    "top.command_palette": "    Ctrl+K command palette",
    "session.bar.session": "Session {name}",
    "session.bar.unnamed": "(unnamed)",
    "session.bar.resumed": " (resumed)",
    "session.bar.max_steps": "  ·  up to {n} steps",
    "session.bar.rail_hint": "Ctrl+B context rail",
    "session.bar.rail_hint_indent": "    Ctrl+B context rail",
    "session.bar.no_permissions": "permissions —",

    # --- 键位提示（欢迎屏那个框 + `/help`）-----------------------------------
    "hint.enter": "Send",
    "hint.slash": "Command palette",
    "hint.thinking": "Thinking",
    "hint.rail": "Context rail",
    "hint.escape": "Interrupt this turn",
    "hint.skills": "All skills",
    "hint.palette": "Command palette",
    "hint.enter_short": "Send",
    "hint.slash_short": "Commands",
    "hint.thinking_short": "Thinking",
    "hint.rail_short": "Rail",
    "hint.escape_short": "Stop",
    "hint.skills_short": "Skills",
    "hint.palette_short": "Palette",
    "hint.shift_enter": "Newline in the input (Enter sends)",
    "hint.arrows": "Pick a candidate in panels / move the cursor / scroll the log",

    # --- 欢迎屏三框（`widgets.WelcomeBlock`）---------------------------------
    "welcome.back": "Welcome back, {name}",
    "welcome.palette_hint": "Command palette: /",
    "welcome.box.start": "Start",
    "welcome.recent.title": "Recent activity",
    "welcome.recent.empty": "(no sessions yet)",
    "welcome.recent.motto": "Motto",
    "welcome.box.recent": "Recent",
    "welcome.box.hint": "Keys",

    # --- 命令面板浮层 --------------------------------------------------------
    "palette.title": "Command palette",
    "palette.hint": "    Type to filter  ·  ↑↓ pick  ·  Enter run  ·  Esc close",

    # --- 审批面板（`PermissionPanel`）----------------------------------------
    "permission_dialog.head": "⛨ Needs your approval",
    "permission_dialog.risk_badge": "{risk} risk",
    "permission_dialog.kind_builtin": "built-in tool",
    "permission_dialog.kind_external": "external tool (MCP)",
    "permission_dialog.title": "   {kind}  ·  risk level {risk}",
    "permission_dialog.no_parallel": "  ·  cannot run alongside other tools",
    "permission_dialog.interactive": "  ·  will take over your input",
    "permission_dialog.no_args": "(no arguments)",
    "permission_dialog.allow": "Allow y",
    "permission_dialog.deny": "Deny n",
    "permission_dialog.always": "Always allow t",
    "permission_dialog.allow_all": "Allow all a",
    "permission_dialog.footer": "Esc = deny: fail-closed, the same direction as the "
                                "unreadable-input path  ·  the verdict is written to "
                                "the audit's permission event",

    # --- 提问面板（`QuestionPanel`）------------------------------------------
    "question_dialog.head": "▣ the agent needs your judgement",
    "question_dialog.no_options": "(this question came with no options; answer it in "
                                  "the input line)",
    "question_dialog.skip": "Skip Esc",
    "question_dialog.footer1": "Enter is the easiest key to press, so skipping has to "
                               "be an explicit key",
    "question_dialog.footer2": "What goes back is the option text, not its number — "
                               "numbers are only how this panel shows them",

    # --- 技能面板 ------------------------------------------------------------
    "skills.title": "Skills",
    "skills.empty": "No skills in this workspace (.tudouni/skills/<name>/SKILL.md)",
    "skills.footer": "✓ = loaded  ·  Esc to close  ·  full list: main.py --skills",

    # --- 选择面板（`OptionPicker`）与会话面板 --------------------------------
    "option.count.one": "{n} item",
    "option.count.other": "{n} items",
    "option.footer": "↑↓ pick  ·  Enter confirm  ·  Esc cancel  ·  ● = current",
    "session_dialog.head": "◱ Switch session",
    "session_dialog.empty": "No session has been saved yet — the file appears after "
                            "your first message.",
    "session_dialog.footer": "↑↓ pick  ·  Enter switch  ·  Esc cancel  ·  "
                             "● = current session",

    # --- MCP 面板 ------------------------------------------------------------
    "mcp_dialog.head": "◱ MCP servers",
    "mcp_dialog.empty": "No MCP server is configured. The list lives in "
                        "~/.tudouni/mcp.json (server URLs or the commands to start "
                        "are written there).",
    "mcp_dialog.running": "{loaded} / {total} running",
    "mcp_dialog.footer": "↑↓ pick  ·  Enter mount / unmount  ·  Esc close  ·  "
                         "the switch only affects this run, mcp.json is not touched",
    "mcp_dialog.pending": "Waiting for the runtime to handle `{name}`… "
                          "(connecting can take a few seconds; if a turn is running "
                          "it waits for that too)",

    # --- 键位说明（只出现在 Textual 自带的按键面板和它的 Ctrl+P 里）----------
    "bindings.deny": "Deny",
    "bindings.allow": "Allow",
    "bindings.always": "Always allow",
    "bindings.allow_all": "Allow all",
    "bindings.skip": "Skip",
    "bindings.choose": "Choose {n}",
    "bindings.close": "Close",
    "bindings.cancel": "Cancel",
    "bindings.newline": "Newline",
    "bindings.palette": "Command palette",
    "bindings.quit": "Quit",
    "bindings.up": "Previous",
    "bindings.down": "Next",
    "bindings.thinking": "Thinking",
    "bindings.rail": "Context rail",
    "bindings.skills": "All skills",
    "bindings.escape": "Interrupt / close",

    # --- 输入框与开场那几行（`app.py`）---------------------------------------
    "input.placeholder": "Say something and press Enter "
                         "(/ for commands, /resume to switch session, "
                         "Shift+Enter for a newline)",
    "init.session_new": " (new)",
    "init.session_id": "(session {name}{state})",
    "init.return_hint": "To come back to this session: /resume "
                        "(pick it from the list; ● marks the current one)",
    "session_load.restored.one": "(restored {n} message; below are what you said and "
                                 "what the agent answered)",
    "session_load.restored.other": "(restored {n} messages; below are what you said and "
                                   "what the agent answered)",

    # --- `app.py` 里剩下的那些提示 -------------------------------------------
    "autopilot.report_on": "Auto-approve: on (tools that need approval run directly; "
                           "the audit records autopilot; run /autopilot again to turn "
                           "it off)",
    "autopilot.report_off": "Auto-approve: off (back to asking one by one)",
    "cmd.audit.line": "Audit log: {path}",
    "cmd.unknown": "No such command: {name} (/help)",
    "cmd.thinking.unknown": "Unrecognized value: {rest} "
                            "(use /thinking on or /thinking off)",
    "cmd.effort.title": "Thinking effort",
    "cmd.model.title": "Change model",
    "cmd.quiet.unknown": "Unrecognized value: {rest} (use /quiet on or /quiet off)",
    "cmd.mcp.unknown": "Unrecognized value: {rest} "
                       "(use /mcp load <name> or /mcp unload <name>)",
    "quiet.name": "Quiet mode ",
    "quiet.on": "on",
    "quiet.on_note": " (only short lines for tools, thinking and results)",
    "quiet.on_extra": "  Only affects what is drawn from now on; /quiet again turns "
                      "it off",
    "quiet.off": "off",
    "quiet.off_note": " (back to one line per item)",
    "mcp.pending": "({action} {name}: asking the runtime to handle it…)",
    "resume.loading": "Fetching the session list…",
    "switch.to_session": "Switching to session {name}…",
    "switch.new_session": "a new session",
    "help.commands_title": "Commands (type / to open the palette; ↑↓ to pick, "
                           "Enter to run):",
    "help.keys_title": "Keys:",
    "thinking.no_turn": "(no turn yet)",
    "thinking.no_reasoning": "(this turn has no reasoning)",
    "thinking.gone": "(this turn's reasoning is no longer on screen)",
    "escape.interrupting": "Asked to stop this turn (it stops after the current step)",
    "escape.idle": "(no turn is running — inside a dialog Esc means deny/skip)",

    # --- 给人看的进度/状态行（runtime 那一侧）--------------------------------
    "reasoning.summary": "{state} · {effort}",
    "todo.progress": "{done}/{total} done",
    "todo.progress.current": ", now: {what}",
    "jobs.progress.running.one": "{n} running",
    "jobs.progress.running.other": "{n} running",
    "jobs.progress.uncollected.one": "{n} result not collected",
    "jobs.progress.uncollected.other": "{n} results not collected",
    "jobs.progress.line": "{parts} ({ids}) — details: job_list",
    "skills.active_label": "Loaded skills: ",

    # --- 审批面板里那两句"按下去会记住什么"（`security/asker.py`）-------------
    "asker.remember.high": "every future call runs directly; you will no longer see "
                           "what it does",
    "asker.remember.default": "this tool will not ask again",
    "asker.remember.prefix": "commands starting with {prefix} will run directly "
                             "without showing you",
    "asker.remember.tail": " (written to {label}; still in effect next time you start)",
    "asker.trust_all": "{group} will run directly from now on; you will no longer see "
                       "what they do",
    "asker.trust_all.snapshot": " (snapshot: tools this server adds later will still "
                                "ask; written to {label}, still in effect next start)",
    "asker.trust_group": "{n} tools of MCP server {server}",

    # --- runtime 下发的启动通知（`composition.notices()`）--------------------
    "notice.context.missing_window": "[context] model {model} is not in the catalog "
                                     "(or its context_window is not configured), so "
                                     "the summary reports usage without a percentage; "
                                     "add that line and it will.",
    "notice.web.no_key": "[web] No TAVILY_API_KEY found, so web_search is not "
                         "registered (fetch_web is unaffected). To enable it, write "
                         "it into the \"env\" section of {path}: "
                         "\"TAVILY_API_KEY\": \"tvly-...\"",
    "notice.grep.unsupported_platform": "[search] this platform ({platform}) is not in "
                                        "the grep engine's support list (only x86_64 "
                                        "Windows / Linux so far), so grep is not "
                                        "registered (text search falls back to shell, "
                                        "which asks for approval every time). "
                                        "Supporting it takes two steps, see "
                                        "tools/vendor/rg/README.md.",
    "notice.grep.missing_binary": "[search] tools/vendor/rg/ is missing the {triple} "
                                  "ripgrep build, so grep is not registered (text "
                                  "search falls back to shell, which asks for approval "
                                  "every time). Run "
                                  "`uv run python scripts/fetch_rg.py` to fetch it.",
    "notice.mcp.configured": "[MCP] {n} servers are configured in {file}: {names} "
                             "(none of them is mounted yet — /mcp lists them and "
                             "mounts them one by one)",
    "notice.mcp.loaded": "[MCP] server {name}: connected, offering {n} tools "
                         "(always high risk, every call needs your approval)",
    "notice.mcp.ignored_workspace_file": "[MCP] ignored {path}: the server list is only "
                                         "read from the user-level {file}. The reason "
                                         "is that `command` here is code executed at "
                                         "startup, and files in the workspace can be "
                                         "cloned in with a repository (see the note "
                                         "above config.McpConfig). Move it to {file} "
                                         "if you want to use it",
    "notice.config.legacy_env": "[config] ignored {path}: this program now reads **no "
                                "environment variables and no .env**. The key goes on "
                                "that route's \"api_key\" in {config}; once you have "
                                "moved it you can delete this file.",
    "notice.jobs.leftovers": "[background] the previous session left the output of {n} "
                             "background jobs behind; they have been cleaned up. That "
                             "session did not exit cleanly (window closed, or the "
                             "process was killed), so **those commands may still be "
                             "running** and they are outside this session's control — "
                             "check yourself if a port or the CPU does not add up.",
    "notice.jobs.no_job_object": "[background] the Windows guarantee that \"closing the "
                                 "window also collects the background jobs\" was not "
                                 "established ({problem}). A normal exit still cleans "
                                 "up, but **killing this process can orphan background "
                                 "commands**.",
    "notice.tools.header": "Registered tools:",
    "notice.tools.row": "  - {name} risk={risk}",
    "notice.permissions.unknown_tools": "[permissions] these tools in {file} are not "
                                        "registered, so their rules do nothing: {names}",
    "notice.permissions.levels": "[permissions] auto-approved by level: {levels}; "
                                 "never asked by name: {named}",
    "notice.permissions.deny": "[permissions] denied outright: {names}",
    "notice.permissions.rules": "[permissions] command rules (allowed by prefix) "
                                "{rules}",
    "notice.permissions.none": "(none)",
    "notice.model.session": "[model] this session uses {route} ({base_url}) — /model "
                            "switches, /status shows the current one.",
    "notice.reasoning.session": "[thinking] this session: {summary} (the default is on · "
                                "{default}) — /thinking toggles it, /effort changes the "
                                "strength.",
    "notice.todos": "[tasks] {line}",
    "notice.skills.available": "[skills] {n} available: {names}",
    "notice.skills.shadowed": "[skills] shadowed by name: {item}",
    "notice.skills.problem": "[skills] {problem}",
    "notice.skills.active": "[skills] {line}",
    "notice.autopilot": "[permissions] autopilot: no approval is asked, tools that "
                        "need it run directly; you will not be asked questions either "
                        "— a model calling ask_user gets \"nobody answered\" and is "
                        "told to decide on its own and state its assumptions (the "
                        "deny list, workspace boundaries and control-plane writes "
                        "still apply)",

    # --- `/model` `/thinking` `/effort` 的回话（runtime 那一侧）--------------
    "model.select.no_name": "No model name given. /model without an argument lists them.",
    "model.select.no_route": "No such route: {route} — /model without an argument "
                             "lists the models.",
    "model.select.route_empty": "route {route} declares no models.",
    "model.select.ambiguous": "{name} exists on more than one route ({names}) — "
                              "write it out: /model provider/model",
    "model.select.unknown": "The catalog has no such model: {name} — /model without an "
                            "argument lists them. (The catalog is the handful of names "
                            "written in your config; arbitrary names are not forwarded "
                            "to the gateway — a typo would only blow up on the next "
                            "request.) Available: {known}",
    "model.select.none_known": "(none at all)",
    "model.select.route_gone": "That route is gone: {route}",
    "model.select.no_key": "route {route} has no API key, so its models cannot be "
                           "selected — put an \"api_key\" on that route in {path}.",
    "model.select.already": "Already on {name}.",
    "model.select.unknown_previous": "(unknown)",
    "model.select.no_switch": "This session's model adapter cannot switch models "
                              "mid-session ({kind}) — change the default in {path} and "
                              "restart.",
    "model.select.switched": "Switched to {name} (previous: {previous}) — it takes "
                             "effect on the next request.",
    "thinking.select.no_support": "This session's model adapter cannot change thinking "
                                  "mode.",
    "thinking.select.on": "Thinking: on (effort {effort}) — it takes effect on the "
                          "next request.",
    "thinking.select.off": "Thinking: off (the effort is remembered; /thinking on "
                           "brings it back) — it takes effect on the next request.",
    "effort.select.no_support": "This session's model adapter cannot change thinking "
                                "effort.",
    "effort.select.off_word": "`none` turns thinking off, it is not an effort level — "
                              "use /thinking off (the effort stays), or "
                              "/effort {levels}.",
    "effort.select.unknown": "No such effort level: {effort} — the levels are {levels} "
                             "(the endpoint also accepts the equivalents {aliases}).",
    "effort.select.ok": "Thinking effort: {level} — it takes effect on the next "
                        "request.",
    "effort.select.ok_off": "Thinking effort set to {level}, but thinking mode is off "
                            "(it applies once you turn it on).",

    # --- AGENT.md 那一块（`state/agents_md.py`）------------------------------
    "agents_md.reason.is_dir": "this is a directory, not a file",
    "agents_md.reason.too_big": "{size} bytes, over the {limit}-byte limit; nothing was "
                                "injected",
    "agents_md.reason.not_utf8": "not UTF-8 text; nothing was injected (saving it as "
                                 "UTF-8 once is enough)",
    "agents_md.reason.permission": "no read permission",
    "agents_md.notice.loaded_item": "{path} ({n} lines)",
    "agents_md.notice.loaded": "[AGENT.md] loaded {listed}",
    "agents_md.notice.truncated_lines": "only the first {lines} lines were injected "
                                        "(out of {total})",
    "agents_md.notice.truncated_chars": "another {omitted} characters were cut off the "
                                        "end, so the text stops mid-sentence",
    "agents_md.notice.truncated": "[AGENT.md] {path} is over the injection budget, "
                                  "{detail}; the model has to read_file it for the "
                                  "full text.",
    "agents_md.notice.failed": "[AGENT.md] cannot read {path}: {reason}",
    "session.preview.unreadable": "(unreadable: {kind})",
    "notice.audit_line": "Audit log written to {path}",

    # --- 模型目录的校验与说明（`state/catalog.py`）---------------------------
    "catalog.where.provider": "providers.{name} in {file}",
    "catalog.where.model": "models[{index}] in {where}",
    "catalog.error.not_string": "\"{key}\" in {where} must be a string, got {kind}",
    "catalog.error.not_bool": "\"{key}\" in {where} must be true/false",
    "catalog.error.not_positive_int": "\"{key}\" in {where} must be a positive integer "
                                      "(a token count), got {value}; delete the whole "
                                      "line if you do not know it — without it the "
                                      "interface reports usage but no percentage",
    "catalog.error.models_not_list": "\"models\" in {where} must be an array",
    "catalog.error.not_object": "{spot} must be an object",
    "catalog.error.unknown_keys": "{spot} has unknown keys: {names}\n"
                                  "  the known ones are: {known}",
    "catalog.error.missing_id": "{spot} is missing \"id\" (the model name sent to the "
                                "endpoint)",
    "catalog.error.duplicate_id": "{spot} repeats the id {id} from an earlier entry",
    "catalog.error.bad_effort": "{spot} has an unknown reasoning_effort {effort}; "
                                "the levels are {levels} (plus the equivalents "
                                "{aliases})",
    "catalog.error.missing_base_url": "{where} is missing \"base_url\" (where requests "
                                      "go)",
    "catalog.problem.no_models": "[model] route {route} declares no models (\"models\" "
                                 "is empty), so it will not show up in /model",
    "catalog.problem.no_key": "[model] route {route} has no API key ({where}), so its "
                              "models cannot be selected — put an \"api_key\" **in "
                              "that route**",
    "catalog.problem.no_usable_route": "[model] not a single usable route (every one "
                                       "is missing its key) — /model will show a list "
                                       "you cannot pick from",

    # --- 配置文件的校验（`runtime/config.py`）--------------------------------
    "config.error.unknown_keys": "{where} has unknown keys: {names}\n"
                                 "  the known ones are: {known}\n"
                                 "  (a misspelled key that silently does nothing is "
                                 "the worst kind of failure, so this stops here)",
    "config.error.unknown_web_keys": 'the "web" section of {path} has unknown keys: '
                                     "{names}\n"
                                     "  the known ones are: {known}\n"
                                     "  (a misspelled key that silently does nothing "
                                     "is the worst kind of failure, so this stops here)",
    "config.error.auto_approve_high": "{file} does not accept \"high\" in "
                                      "auto_approve: risk levels are declared by each "
                                      "tool, so\n"
                                      "  \"auto-approve everything high\" would silently "
                                      "widen as new tools are added. Name the tool "
                                      "instead:\n"
                                      "  "
                                      '{{"auto_approve_tools": ["shell"]}}',
    "config.error.auto_approve_unknown": "{file} has unknown levels in auto_approve: "
                                         "{levels}; the only levels that can be "
                                         "auto-approved are {allowed}",
    "config.error.contradictory": "{file} lists {names} in both auto_approve_tools and "
                                  "deny_tools — those two contradict each other, fix it "
                                  "here instead of making the policy guess which one "
                                  "wins",
    "config.error.bad_shell_rule": "{file} has a malformed rule in shell_allow: "
                                   "{problem}",
    "config.error.mcp_problem": "{file} has a problem: {problem}",
    "config.error.not_utf8": "{path} is not UTF-8, the text comes out garbled. Choose "
                             "UTF-8 when saving as… in Notepad.",
    "config.error.unreadable": "cannot read {path}: {error}",
    "config.error.bad_json": "{path} is not valid JSON: line {line}, column {column}: "
                             "{message}",
    "config.error.not_object": "the outermost value of {path} must be a JSON object "
                               "({{...}}), got {kind}",
    "config.error.not_string_list": "\"{key}\" in {file} must be an array of strings, "
                                    "for example [\"shell\"]",
    "config.error.empty_string_in_list": "\"{key}\" in {file} contains an empty string",

    # --- 协议层回给界面的那几条（`protocol/channels.py`）---------------------
    "channels.direction.frontend": "from the frontend",
    "channels.write_failed": "[warn] failed to write a line to the frontend "
                             "(ignored): {problem}",
    "channels.model.needs_string": "[model] switching models needs a string name. "
                                   "/model lists them.",
    "channels.model.no_session": "[model] no session yet, cannot switch models.",
    "channels.model.reply": "[model] {message}",
    "channels.model.not_changed": "[model] not switched: {message}",
    "channels.effort.needs_string": "[thinking] the effort must be a string "
                                    "(low / high / max).",
    "channels.effort.no_session": "[thinking] no session yet.",
    "channels.effort.reply": "[thinking] {message}",
    "channels.effort.not_changed": "[thinking] not changed: {message}",
    "channels.thinking.no_session": "[thinking] no session yet.",
    "channels.thinking.reply": "[thinking] {message}",
    "channels.thinking.not_changed": "[thinking] not changed: {message}",
    "channels.session.needs_string": "[session] the session id must be a string",
    "channels.session.bad_id": "[session] invalid id: {name} — only letters, digits, "
                               "underscores and hyphens (1-64 characters). /resume "
                               "without an argument lets you pick from a list.",
    "channels.session.switch_failed": "[session] could not switch (the current session "
                                      "is unchanged): {problem}",
    "channels.session.close_failed": "error closing the previous session's runtime: "
                                     "{problem}",
    "channels.run_failed": "[turn failed] {problem}",
    "channels.status.no_session": "[status] no session yet.",
    "channels.tools.no_session": "[tools] no session yet.",
    "channels.mcp.no_session": "[MCP] no session yet.",
    "channels.mcp.no_host": "[MCP] this runtime has no MCP host, mounts cannot be "
                            "changed.",
    "channels.mcp.unknown_action": "[MCP] unrecognized action: {action} (only {actions})",
    "channels.mcp.list_note": "[MCP] current mounts (changing the config needs a "
                              "restart)",
    "channels.mcp.needs_name": "[MCP] {action} needs a server name, one at a time",

    # --- MCP：配置解析与连接（`tools/mcp.py`）--------------------------------
    "mcp.cfg.unknown_top_keys": "unknown keys: {names}; the outermost object has only "
                                "the `servers` key",
    "mcp.cfg.servers_not_object": '"servers" must be an object: '
                                  '{{"name": {{"command": ...}}}}',
    "mcp.cfg.bad_name": "server name {name} is invalid: only letters, digits, "
                        "underscores and hyphens, 1-24 characters — it is spliced "
                        "into the tool names the model sees ({prefix}<name>__<tool>)",
    "mcp.cfg.server_not_object": 'servers["{name}"] must be an object',
    "mcp.cfg.unknown_server_keys": 'servers["{name}"] has unknown keys: {names}\n'
                                   "  the known ones are: {known}",
    "mcp.cfg.command_not_string": 'servers["{name}"].command must be a string',
    "mcp.cfg.url_not_string": 'servers["{name}"].url must be a string',
    "mcp.cfg.both_given": "both were given",
    "mcp.cfg.neither_given": "neither was given",
    "mcp.cfg.both_or_neither": 'servers["{name}"] must give exactly one way to '
                               "connect ({given}):\n"
                               '  local: {{"command": "npx", "args": [...]}}; '
                               'remote: {{"url": "https://example.com/mcp", '
                               '"headers": {{...}}}}',
    "mcp.cfg.empty_scheme": "empty",
    "mcp.cfg.bad_url": 'servers["{name}"].url must be a full http(s) address '
                       "(this one's scheme is {scheme}): {url}",
    "mcp.cfg.local_only_key": 'servers["{name}"] is a remote server (it has a url), '
                              "and {key} only means something for a local server "
                              "(command)",
    "mcp.cfg.args_not_list": 'servers["{name}"].args must be an array of strings, '
                             'for example ["-y", "package-name"]',
    "mcp.cfg.env_not_map": 'servers["{name}"].env must be a "string → string" object',
    "mcp.cfg.headers_not_map": 'servers["{name}"].headers must be a "string → string" '
                               "object (credentials go here, for example "
                               '{{"Authorization": "Bearer …"}})',
    "mcp.cfg.bad_header_name": 'the header name {header} in servers["{name}"].headers '
                               "is invalid: HTTP header names must be ASCII",
    "mcp.cfg.bad_timeout": 'servers["{name}"].timeout_seconds must be between '
                           "{low} and {high}",
    "mcp.spawn_failed": "cannot start `{command}`: {problem}",
    "mcp.timeout": "no response to {method} after {seconds} seconds",
    "mcp.write_failed": "failed to write to server `{name}`: {problem}",
    "mcp.stdout.not_json": "[MCP] server `{name}` printed a line on stdout that is not "
                           "JSON; skipped",
    "mcp.stdout.closed_with_code": "MCP server `{name}` closed its stdout (process "
                                   "exit code {code})",
    "mcp.stdout.closed": "MCP server `{name}` closed its stdout",
    "mcp.close_failed": "[MCP] server `{name}` could not be collected (the process is "
                        "still there); it may keep holding the pipes",
    "mcp.closed": "MCP server `{name}` is closed",
    "mcp.close_error": "[MCP] error closing server `{name}`: {problem}",
    "mcp.http.connect_timeout": "cannot reach server `{name}` (connect timeout): "
                                "{problem}",
    "mcp.http.timeout": "no response to {method} after {seconds} seconds",
    "mcp.http.connect_failed": "cannot reach server `{name}` ({problem})",
    "mcp.http.status": "server `{name}` returned HTTP {status}: {body}",
    "mcp.http.body_too_big": "the response body is over {mb}MB, giving up",
    "mcp.http.no_sse_response": "the SSE stream of server `{name}` carried no "
                                "JSON-RPC response",
    "mcp.http.not_json": "server `{name}` returned a body that is not JSON: {body}",
    "mcp.http.not_object": "server `{name}` did not return a JSON object",
    "mcp.result.no_message": "(the server gave no explanation)",
    "mcp.result.refused": "{who} refused {method}: {text} (code={code})",
    "mcp.list_tools.not_object": "the tools/list response must be an object",
    "mcp.list_tools.too_many_pages": "tools/list went through {pages} pages without "
                                     "ending, giving up",
    "mcp.call.not_object": "the tools/call response must be an object",
    "mcp.call.failed": "{name} failed (the server gave no explanation)",
    "mcp.no_tools_capability": "[MCP] the capabilities of server `{name}` have no "
                               "tools, so it offers none (connected, 0 tools)",
    "mcp.load_failed": "[MCP] server `{name}` did not connect ({problem}); none of its "
                       "tools is available this time",
    "mcp.name_clash": "[MCP] the exposed name of `{tool}` on server `{name}` collides "
                      "with another tool ({other}); this one is not installed this "
                      "time",

    # --- 技能文件的问题（`skills/loader.py`）---------------------------------
    "skills.problem.no_frontmatter": "the file must start with frontmatter: the first "
                                     "line is three hyphens (---)",
    "skills.problem.unclosed_frontmatter": "the frontmatter is not closed: the second "
                                           "line of three hyphens (---) is missing",
    "skills.problem.indented": "this line is indented: {line}. This parser only "
                               "accepts top-level `key: value` and does not take "
                               "nested structures or `- ` list items (except under "
                               "`metadata`)",
    "skills.problem.unparsable": "cannot read this line: {line} (only top-level "
                                 "`key: value` is supported)",
    "skills.problem.missing_key": "this line has no key: {line}",
    "skills.problem.unknown_key": "unknown key {key}; the only keys this program knows "
                                  "are {known}",
    "skills.problem.duplicate_key": "the key {key} appears twice — there is no answer "
                                    "to which one counts",
    "skills.problem.multiline_block": "{key} uses a multi-line block ({value}), which "
                                      "this parser does not support; write it on one "
                                      "line",
    "skills.problem.bad_chars": "the value of {key} contains characters this parser "
                                "does not support ({chars}); write the value as plain "
                                "one-line text, or wrap the whole thing in quotes, and "
                                "put any explanation in the body",
    "skills.problem.missing_name": "the frontmatter is missing name",
    "skills.problem.name_mismatch": "name={name} does not match the directory name "
                                    "{directory} — they must be the same, otherwise the "
                                    "skill name the model sees and the file path do "
                                    "not line up",
    "skills.problem.bad_name": "name={name} is invalid: only lowercase letters, digits "
                               "and hyphens, and it may not start or end with a hyphen "
                               "or contain two in a row (for example pdf-extract)",
    "skills.problem.name_too_long": "name is longer than 64 characters ({length})",
    "skills.problem.missing_description": "the frontmatter is missing description — it "
                                          "is the only thing the model has for judging "
                                          "when to use this skill (the body is "
                                          "invisible until the skill is loaded)",
    "skills.problem.too_big": "the file is {size} bytes, over the {limit}-byte limit — "
                              "the body is spliced into every request, so an oversized "
                              "skill is refused rather than truncated; move the "
                              "details into another file next to it and let the model "
                              "read_file it when needed",
    "skills.problem.escape": "the skill directory escapes its root (a symlink points "
                             "outside): {path}",
    "skills.problem.unreadable": "cannot read {file} ({problem})",
    "skills.problem.skipped": "{name} was skipped: {reason}",
    "skills.problem.shadowed": "{name} uses {winner}, {losers} is shadowed by it",
    "skills.scan_dirs": "  scanned directories (low to high priority):",
    "skills.scan_none": "    (none of them exists)",
    "skills.shadowed_line": "  [shadowed] {item}",

    # --- 启动前的几道检查（`composition.check_*` / 无模型提示）----------------
    "check.workspace.home": "{here} is your home directory",
    "check.workspace.root": "{here} is the root of the filesystem",
    "check.workspace.above_home": "{here} is above home (everyone's home lives under "
                                  "it)",
    "check.workspace.refused": "this cannot be the workspace: {what}.\n"
                               "  The workspace is the current directory, and the "
                               "agent's file tools **can only touch what is inside "
                               "it** —\n"
                               "  starting here hands over everything below it "
                               "(.ssh, other projects' .env files, browser data…),\n"
                               "  and read_file needs no approval, so you will not be "
                               "asked twice.\n"
                               "  cd into a concrete project directory first.",
    "check.session_id.invalid": "invalid --session: {name}\n"
                                "  a session id may only contain letters, digits, "
                                "underscores and hyphens, 1-64 characters —\n"
                                "  because it is spliced into file names "
                                "({dir}/sessions/<id>.jsonl and "
                                "{dir}/logs/<id>.jsonl).\n"
                                "  Use --list to see the ids you already have.",
    "no_model.created": "No usable model route — without a model there is nothing to "
                        "do.\n"
                        "\n"
                        "I have created a config for you here; open it and fill in "
                        "your model and key:\n"
                        "    {path}\n",
    "no_model.missing": "No usable model route — without a model there is nothing to "
                        "do.\n"
                        "\n"
                        "The config lives at {config} (template: {example}).\n",
    "no_model.intro": "The model layer is abstract: endpoint, model names and keys all "
                      "come from `providers` in your config;\n"
                      "nothing is hard-coded. Add a route under \"providers\" and you "
                      "are set:",
    "no_model.route_fields": "    A route gives three things: base_url (where requests "
                             "go), api_key (the key goes on that route),\n"
                             "    models (what is available on it). **The first route "
                             "with a key is the default**, the order is yours.",
    "no_model.notes": "Routes read this time:",
    "no_model.problems": "Problems, one by one:",

    # --- MCP 宿主（`composition.McpHost`，`/mcp` 面板回的那几句话）------------
    "mcp.host.already_loaded": "server `{name}` is already mounted ({n} tools), not "
                               "loading it twice",
    "mcp.host.unknown_server": "no server named `{name}` in the list: {known}",
    "mcp.host.load_failed": "server `{name}` did not connect ({problem}); press again "
                            "to retry",
    "mcp.host.loaded": "server `{name}` mounted: {n} tools (always high risk, every "
                       "call needs your approval)",
    "mcp.host.not_running": "server `{name}` was not running anyway",
    "mcp.host.unloaded": "server `{name}` unmounted ({n} tools removed); the line is "
                         "still in the config (it will not mount by itself next "
                         "start — nothing is auto-mounted at startup, load it again "
                         "when you need it)",
    "mcp.host.no_servers": "(no server is configured in {file})",
    "mcp.host.close_failed": "error closing MCP server `{name}`: {problem}",
    "mcp.host.reread_failed": "error re-reading {file} (new entries are not recognized "
                              "this time): {problem}",

    # --- 落盘与收摊时的警告（失败路径，但会打到 stderr 上）--------------------
    "save.action.model": "switching the model",
    "save.action.thinking": "changing thinking mode",
    "save.action.effort": "changing thinking effort",
    "save.failed": "saving after {what} failed (it still applies this run; reopening "
                   "the session goes back to what the config says): {problem}",
    "close.jobs_failed": "error collecting background jobs: {problem}",
    "close.http_failed": "error closing the http client: {problem}",

    # --- 会话文件（`state/store.py`）与系统提示词缺失（`state/session.py`）----
    "store.bad_session_id": "invalid session_id: {name}",
    "store.shrunk": "session {name} went from {before} messages to {after}; this store "
                    "only appends, it does not delete or rewrite.",
    "store.newer_version": "session {name} was written by a newer version (file "
                           "version={version}, the highest this program knows is "
                           "{known}); upgrade the program before opening it, "
                           "otherwise the format may be misread.",
    "store.missing_file": "session file does not exist: {path}",
    "session.prompt_missing": "the system prompt file does not exist: {file}\n"
                              "it is not an optional file — the agent sends it to the "
                              "model on every turn.",
}
