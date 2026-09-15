"""中文目录表。**这份是原文**：键的语义由它定，英文那份照着它翻。

规矩（`tests/test_i18n.py` 盯着前两条）：

  1. **每一条的键都必须在 `en.py` 里也有** —— 漏一条就是"英文界面里冒出一行中文"；
  2. **值不能是空串** —— 空串在界面上就是"什么都不显示"，而它和"忘了翻"长得一模一样；
  3. 中文**不用给**单复数那两条（`.one` / `.other`），`tn()` 会自动退回单条。

键的命名：`<区域>.<东西>`，区域就是它在界面上出现的地方（`activity` = 状态栏那一行、
`rail` = 左栏、`cmd` = 命令、`welcome` = 欢迎屏……）。**键名不写进界面**，它只是
"这两句话说的是同一件事"的凭据。
"""

CATALOG: dict[str, str] = {
    # --- 状态栏那一行（`protocol/state.py` 的 `activity`） --------------------
    # 它是"agent 现在在干什么"的投影，一秒内可能换好几次，所以每一句都短。
    "activity.preparing": "准备中",
    "activity.thinking": "模型在想",
    "activity.retrying": "模型调用失败，重试中",
    "activity.tool_call": "要调用 {tool}{index}",
    "activity.tool_call_index": "（第 {n} 个）",
    "activity.tool_result": "{tool} {verb}",
    "activity.result.ok": "返回了",
    "activity.result.denied": "被拒绝",
    "activity.result.invalid_args": "参数不合法",
    "activity.result.error": "出错",
    "activity.tool_batch": "{n} 个只读工具并发执行中",
    "activity.permission": "{tool} {outcome}",
    "activity.permission.approved": "已批准",
    "activity.permission.user_denied": "已拒绝",
    "activity.permission.policy_denied": "策略禁止",
    "activity.permission.no_asker": "没有审批通道",
    # 没拿到工具名时的兜底（协议里那个字段理论上一定在，但前端不许崩）。
    "activity.some_tool": "工具",

    # --- `/theme`（配色）-----------------------------------------------------
    "theme.picker_title": "换配色",
    "theme.unknown": "没有这套配色：{name}（/theme）",
    "theme.switched": "配色换成 ",
    "theme.only_this_run": "（只影响这次运行）",

    # --- 命令面板与 `/help`（`view_state.COMMANDS`）--------------------------
    # 键由命令名算出来（`/model` → `cmd.model.*`），所以加一条命令时**这里必须
    # 跟着加两格**：漏了 `hint` 会在面板那一行当场 `KeyError`（那是故意的，见
    # `i18n.t` 的 docstring），漏了 `detail` 只是 `/help` 少一句。
    #
    # 措辞的规矩：`hint` 是**一句短语**（面板里一行就是它的全部预算），`detail`
    # 只说"怎么用"，**不解释机制** —— 机制属于 `doc/TUI-design.md`。
    "cmd.new.hint": "开一个新会话",
    "cmd.resume.hint": "换一个会话",
    "cmd.resume.detail": "不带参数列清单；/resume <id> 直接切",
    "cmd.audit.hint": "审计日志在哪",
    "cmd.exit.hint": "退出",
    "cmd.help.hint": "命令与键位",
    "cmd.theme.hint": "换配色",
    "cmd.theme.detail": "不带参数开面板；/theme <名字|key|序号> 直接换",
    "cmd.skills.hint": "看全部技能",
    "cmd.autopilot.hint": "自动放行开关",
    "cmd.quiet.hint": "安静模式开关",
    "cmd.quiet.detail": "不带参数切换；/quiet on 或 /quiet off 直接设",
    "cmd.status.hint": "看现在的状态",
    "cmd.tools.hint": "工具与权限",
    "cmd.model.hint": "换模型",
    "cmd.model.detail": "不带参数开面板；/model <名字> 直接换（要精确）",
    "cmd.thinking.hint": "思考模式开关",
    "cmd.thinking.detail": "不带参数看状态；/thinking on 或 /thinking off 改它",
    "cmd.effort.hint": "思考强度",
    "cmd.effort.detail": "不带参数开面板；/effort <档位> 直接改",
    "cmd.mcp.hint": "MCP 服务器开关",
    "cmd.mcp.detail": "不带参数开面板；/mcp load|unload <名字> 直接改",

    # --- 时间那一格（欢迎屏「最近活动」的左边一列）----------------------------
    # **中文不用给单复数**：`tn()` 找不到 `.one`/`.other` 就退回这一条。
    "time.just_now": "刚刚",
    "time.minutes_ago": "{n}分钟前",
    "time.hours_ago": "{n}小时前",
    "time.days_ago": "{n}天前",

    # --- 箴言（欢迎屏右下那一句，按日期轮换）----------------------------------
    "motto.1": "先想清楚要什么，再动手。",
    "motto.2": "能写下判据的，才算想明白了。",
    "motto.3": "改一处，就只改那一处。",
    "motto.4": "看不懂的代码，先别改。",
    "motto.5": "小步走，常回头。",
    "motto.6": "把话说给下一个读它的人听。",
    "motto.7": "失败要响，别悄悄吞掉。",
    "motto.8": "重复第三遍的时候，就该抽出来了。",
    "motto.9": "先让它对，再让它快。",
    "motto.10": "名字错了，代码就跟着错。",
    "motto.11": "留下的注释要解释为什么，不是是什么。",
    "motto.12": "没跑过的东西，不算做完。",
    "motto.13": "接口比实现活得久。",
    "motto.14": "删掉一行代码，和写一行一样值钱。",
    "motto.15": "今天的决定，明天的默认值。",

    # --- 枚举 → 说法（`view_state.outcome_text` / `stop_reason_text`）---------
    # 左边那几个枚举来自 `security/gate.py` 和 Agent 的 `stop_reason`，它们是**协议
    # 里的机器标识**，不翻译；这里翻的只是"说给人听的那句"。
    "outcome.approved": "批准",
    "outcome.autopilot": "自动放行",
    "outcome.user_denied": "拒绝",
    "outcome.policy_denied": "策略禁止",
    "outcome.no_asker": "没有审批通道",
    "stop.answered": "已答",
    "stop.max_steps": "步数用尽",
    "stop.cancelled": "已中断",
    "stop.model_error": "模型失败",

    # --- 权限那一行（`view_state._permission_line`）---------------------------
    "permission.line_prefix": "  · 权限 ",
    "permission.rule_hit": "命中规则 {rule}",
    "permission.remembered": "已记住 {remembered}",
    "permission.waited": "你看了 {duration}",
    "permission.tail": "（{extras}）",

    # --- 回合头 --------------------------------------------------------------
    "turn.head": "回合 {index}",
    "turn.state": "{n} 步 · {duration} · {outcome}",
    "turn.max_steps_warning": "  ! 步数用尽，这一轮**没有**收尾 —— 会话是好的，可以接着跑。",
    "turn.cancelled_warning": "  ! 已按你的要求停下（停在两步之间，会话是完好的）。",

    # --- 拼一串东西时那个顿号 ------------------------------------------------
    # 中文用「、」，英文用「, 」—— 它出现在"已记住 a、b""技能有 a、b"这些地方，
    # 而把分隔符写死在代码里就等于替英文选了中文的标点。
    "list.separator": "、",
    # 分号那一版：AGENT.md 被截断时那两条说明之间用的是它（比顿号重一档）。
    "list.separator_semicolon": "；",

    # --- 状态栏：左段（agent 在干什么）+ 右段（成本与去处）--------------------
    # 这两段都**很窄**（右段还是 `width: auto`，多一格都从左段身上扣），所以英文
    # 也照完整措辞写，但不再加多余的词。
    "status.settled.answered": "已答",
    "status.settled.limited": "步数用尽",
    "status.settled.failed": "本轮失败",
    "status.settled.cancelled": "已中断",
    "status.idle": "空闲",
    "status.idle.new": "空闲 · 说出一句话后才开始",
    # 启动态那两句话（`ViewState.booting`）：**短到不被状态栏裁掉**——那一条是
    # `nowrap + clip`，长了就只剩半句，而半句真话比不说更坏。第二句点明"去看哪里"，
    # 因为那时能给出原因的地方只有终端的 stderr。
    "status.boot.starting": "正在启动 runtime…",
    "status.boot.slow": "runtime 还没回应（原因见终端 stderr）",
    "status.step": "第 {step} / {total} 步",
    "status.context.none": "上下文  —",
    "status.context.plain": "上下文 {used} / {total}",
    "status.context.percent": "上下文 {used} / {total}（{percent}%）",
    "status.context.used": "上下文 {used}",
    "status.hit": "命中 {percent}%",
    "status.hit.none": "命中  —",
    "status.turn": "本轮 {duration}",
    # 两个数各有各的单复数，所以拆成两段再套外层（见 `status_right` 里那段说明）。
    "status.session.messages": "{n} 条",
    "status.session.steps": "{n} 步",
    "status.session.span": "会话 {messages} · {steps}",
    "status.session.none": "会话  —",
    "status.audit": "审计 {path}",
    "status.autopilot.on": "自动放行 开",
    "status.autopilot.off": "自动放行 关",
    "status.autopilot.on_short": "放行 开",
    "status.autopilot.off_short": "放行 关",
    "status.quiet": "安静",
    "status.jobs.collected": "后台 {n} 已收",
    "status.jobs.running": "后台 {n}",
    "status.jobs.uncollected": " · {n} 条待收",

    # --- 过程行：模型调用 / 思维链 / 工具调用 / 工具结果 ----------------------
    "event.model_retry": "  · 模型调用失败（第 {attempt} 次）{tail}",
    "event.model_retry_wait": "，{backoff}ms 后重试",
    "event.denied": "      （被拒绝，没有执行）",
    "event.tool_batch": "{n} 个只读工具并发执行完毕",
    "event.tool_batch_wall": "（{wall}ms）",
    "event.model_prefix": "  · 模型 ",
    "event.context_tokens": "  上下文 {tokens} token",
    "event.cache_hit": "（命中 {cached} · {percent}%）",
    "turn.running": "进行中 · 第 1 步",
    "think.prefix_folded": "  ▸ 思考过程",
    "think.folded_tail": "（{chars} 字符 · Ctrl+T 展开）",
    "think.prefix_expanded": "  ▾ 思考过程",
    "think.expanded_tail": "（展开 · Ctrl+T 收起）",
    "think.live_chars": " {chars} 字符",
    "risk.high": "   HIGH 风险",
    "risk.medium": "   MEDIUM 风险",
    "tool.ok_tail": "{chars} 字符   {span}",
    "tool.result_chars": "{chars} 字符",
    "tool.denied": "被拒绝，没有执行",
    "tool.invalid_args": "参数不合法，没有执行",
    "tool.error": "执行出错（{chars} 字符）",
    "brief.items": "{n} 项",
    "brief.todo_items": "{n} 条任务",
    "brief.yes": "是",
    "brief.no": "否",

    # --- 左栏六块（标题、空态、每一行）---------------------------------------
    "rail.jobs": "后台任务",
    "rail.jobs.empty": "当前没有后台任务",
    "rail.jobs.empty_hint": "shell_background 起的会在这里",
    "rail.job.running": "在跑 {span}",
    "rail.job.uncollected": "已结束（退出码 {code}）· 结果还没收",
    "rail.job.killed": "已被收掉",
    "rail.job.done": "已结束（退出码 {code}）· 已收",
    "rail.tasks": "任务",
    "rail.tasks.empty": "当前还没有任务",
    "rail.tasks.empty_hint": "agent 创建的任务会在这里",
    "rail.skills": "已加载技能",
    "rail.skills.empty": "还没有加载技能",
    "rail.skills.empty_hint": "load_skill 读过的会一直生效",
    "rail.permissions": "权限范围",
    "rail.permission.auto": "自动放行",
    "rail.permission.ask": "询问",
    "rail.permission.granted": "点名免问",
    "rail.permission.prefixes": "命令规则",
    "rail.permission.denied": "直接拒绝 ",
    "rail.permission.by_level": "按等级（runtime 没报范围）",
    "rail.session": "本次会话",
    "rail.session.empty": "还没有会话",
    "rail.session.empty_hint": "说出第一句话之后才有文件",
    "rail.session.thinking_off": "思考 关",
    "rail.session.effort": "  强度 {effort}",
    "rail.session.messages": "{n} 条消息",
    "rail.session.steps": "{n} 步",
    "rail.session.size": "{messages} · {steps}",
    "rail.session.agent_md_lines": "{n} 行",
    # 收起左栏时那一行摘要（窄屏）。
    "rail.summary.expand": "Ctrl+B 展开上下文栏",
    "rail.summary.jobs": "{n} 个后台任务",
    "rail.summary.jobs_collected": "{n} 个后台任务（都收过了）",
    "rail.summary.mcp": "{n} 个 MCP server",
    "rail.summary.tasks": "{done}/{total} 个任务",
    "rail.summary.skills": "{n} 个技能",
    "rail.summary.asking": "{risks} 询问",
    "rail.summary.all_auto": "全部自动放行",

    # --- `/status` 那一屏（`render_status`）-----------------------------------
    # 标签那一列的宽度**按这里量出来**（`view_state._label_width`），所以加一个更长
    # 的标签时那一列会自己变宽，而不是把值挤到标签上。
    "status.title": "状态",
    "status.no_state": "（还没有状态：这个会话一步都没走过）",
    "status.kv.session": "会话",
    "status.kv.workspace": "工作区",
    "status.kv.size": "规模",
    "status.kv.model": "模型",
    "status.kv.endpoint": "端点",
    "status.kv.thinking": "思考",
    "status.kv.context": "上下文",
    "status.kv.input_total": "累计输入",
    "status.kv.output_total": "累计输出",
    "status.kv.usage_total": "累计用量",
    "status.kv.turns": "轮次",
    "status.kv.run": "这次运行",
    "status.kv.tools": "工具",
    "status.kv.audit": "审计",
    "status.session_id": "{name}（{started}）",
    "status.started.resumed": "这次启动：继续",
    "status.started.new": "这次启动：新建",
    "status.model.pending": "{current}（换成 {selected} 的，下一次请求生效）",
    "status.thinking.on": "开 · {effort}",
    "status.thinking.off": "关（强度记着，/thinking on 回来）",
    "status.context.unknown": "—（还没成功调用过模型）",
    "status.context.ratio": "{used} / {window}（{percent}%）",
    "status.context.no_window": "{used}（这个模型的窗口不在目录里，不报占比）",
    "status.usage.input": "{tokens} token（命中缓存 {cached}、命中率 {rate}）",
    "status.usage.output": "{tokens} token",
    "status.usage.none": "还没有成功调用过模型",
    "status.turns.value": "{runs} 轮 · {model_calls} 次模型调用 · {tool_calls} 次工具调用{tail}",
    "status.turns.waits": "（其中审批 {waits} 次",
    "status.turns.asks": "、提问 {asks} 次）",
    "status.turns.close": "）",
    "status.run.max_steps": "最多 {n} 步",
    "status.run.stream": "流式",
    "status.run.no_stream": "非流式",
    "status.run.autopilot": "自动放行",
    "status.run.ask": "逐条审批",
    "status.tools.count": "{n} 个（/tools 看清单）",

    # --- MCP（左栏那一块 + `/mcp` 面板与会话流）------------------------------
    "rail.mcp": "后台 MCP",
    "rail.mcp.empty": "当前没有挂载 MCP server",
    "rail.mcp.empty_hint": "/mcp 可以看清单并逐个挂载",
    "rail.mcp.tools": "  {n} 个工具",
    "mcp.tools": "{n} 个工具",
    "mcp.not_connected": "没连上：{error}",
    "mcp.no_reason": "（没说原因）",
    "mcp.not_loaded": "未加载",
    "mcp.summary": "MCP：{running} 个在跑 / 共 {total} 个（/mcp 打开面板逐个开关）",

    # --- 会话清单里的一行（`session_row`，`/resume` 面板）---------------------
    # 序号那一列的对齐写在模板里（`{n:>3}`）：它是**一列**，不是一句话里的数。
    "session.row": "  {mark}{name} {messages} · {steps}   {preview}{todos}",
    "session.row.messages": "{n:>3} 条消息",
    "session.row.steps": "{n:>3} 步",
    "session.row.untitled": "（还没说过话）",
    "session.row.todos": "   [任务 {todos}]",

    # --- 等审批那一行（`waiting_line`）---------------------------------------
    "waiting.prompt": "  · 等待你的批准",
    "waiting.allow": "   [y] 允许",
    "waiting.deny": "   [n] 拒绝",
    "waiting.always": "   [t] 总是允许",
    "waiting.allow_all": "   [a] 都允许",
    "waiting.escape": "   [Esc] 拒绝",

    # --- `/tools` 那一屏（`render_tools`）------------------------------------
    "tools.none": "这次运行一个工具都没注册（缺引擎/密钥时会这样，启动那几行里有原因）",
    "tools.disposition.auto": "自动放行",
    "tools.disposition.ask": "需要审批",
    "tools.disposition.deny": "直接拒绝",
    "tools.mark.granted": "按过 t",
    "tools.mark.external": "外部",
    "tools.mark.interactive": "会问你",
    "tools.mark.parallel": "可并发",
    "tools.prefixes": "  命令规则（按前缀放行，只对 shell 这类有命令行的工具生效）：{rules}",
    "tools.footer": "  改这些去 .tudouni/permissions.json；审批时按 t 会写进去",

    # --- `/model`（面板那一行 + 兜底清单）------------------------------------
    "model.window": " · 上下文 {window}",
    "model.detail_tail": "   （{detail}）",
    "model.no_catalog": "（runtime 没给模型清单：这一版协议之前起的子进程？）",
    "model.current": "当前模型：{name}",
    "model.aliases": "  认下的旧名字：{old} → {new}",
    "model.howto": "换一个：/model <名字>（名字要精确；两条路由同名时写 provider/model）",
    "model.unknown": "（清单里没有那个名字）",

    # --- `/thinking` 与 `/effort` --------------------------------------------
    "thinking.mode": "思考模式：{state}",
    "thinking.on": "开",
    "thinking.off": "关",
    "thinking.effort": "  强度：{effort}",
    "thinking.effort_off_note": "（关着时用不上，但记着）",
    "thinking.howto": "改：/thinking on   ·   /thinking off",
    "thinking.unknown": "（认不出这个写法：{rest}）",
    "effort.current": "思考强度：{effort}",
    "effort.off_note": "（思考关着，打开才用得上）",
    "effort.no_catalog": "（runtime 没给档位清单：这一版协议之前起的子进程？）",
    "effort.howto": "改：/effort {levels}",
    "effort.howto_bare": "改：/effort <档位>",
    "effort.unknown": "（没有这一档：{rest}）",
    # 清单标题（`/model` 和 `/effort` 共用）。
    "list.available": "可选：",

    # --- 顶栏与会话头 --------------------------------------------------------
    "top.command_palette": "    Ctrl+K 命令面板",
    "session.bar.session": "会话 {name}",
    "session.bar.unnamed": "（未命名）",
    "session.bar.resumed": "（继续）",
    "session.bar.max_steps": "  ·  最多 {n} 步",
    "session.bar.rail_hint": "Ctrl+B 上下文栏",
    "session.bar.rail_hint_indent": "    Ctrl+B 上下文栏",
    "session.bar.no_permissions": "权限 —",

    # --- 键位提示（欢迎屏那个框 + `/help`）-----------------------------------
    # 宽版进欢迎屏底下那个框，窄版给窄屏（那几列连"思考过程"都嫌长）。
    "hint.enter": "发送",
    "hint.slash": "命令面板",
    "hint.thinking": "思考过程",
    "hint.rail": "上下文栏",
    "hint.escape": "中断本轮",
    "hint.skills": "全部技能",
    "hint.palette": "命令面板",
    "hint.enter_short": "发送",
    "hint.slash_short": "命令",
    "hint.thinking_short": "思考",
    "hint.rail_short": "上下文",
    "hint.escape_short": "中断",
    "hint.skills_short": "技能",
    "hint.palette_short": "面板",
    "hint.shift_enter": "输入框里换行（回车是发送）",
    "hint.arrows": "面板选候选 / 光标移动 / 翻会话流",

    # --- 欢迎屏三框（`widgets.WelcomeBlock`）---------------------------------
    "welcome.back": "欢迎回来 {name}",
    "welcome.palette_hint": "命令面板：/",
    "welcome.box.start": "开始",
    "welcome.recent.title": "最近活动",
    "welcome.recent.empty": "（还没有会话）",
    "welcome.recent.motto": "箴言",
    "welcome.box.recent": "最近",
    "welcome.box.hint": "提示",

    # --- 命令面板浮层 --------------------------------------------------------
    "palette.title": "命令面板",
    "palette.hint": "    输入命令名筛选  ·  ↑↓ 选择  ·  Enter 执行  ·  Esc 关闭",

    # --- 审批面板（`PermissionPanel`）----------------------------------------
    "permission_dialog.head": "⛨ 需要审批",
    "permission_dialog.risk_badge": "{risk} 风险",
    "permission_dialog.kind_builtin": "内置工具",
    "permission_dialog.kind_external": "外部工具（MCP）",
    "permission_dialog.title": "   {kind}  ·  风险等级 {risk}",
    "permission_dialog.no_parallel": "  ·  不可与其他工具并发",
    "permission_dialog.interactive": "  ·  会占用你的输入",
    "permission_dialog.no_args": "（没有参数）",
    "permission_dialog.allow": "允许 y",
    "permission_dialog.deny": "拒绝 n",
    "permission_dialog.always": "总是允许 t",
    "permission_dialog.allow_all": "都允许 a",
    "permission_dialog.footer": "Esc = 拒绝：fail-closed，和读不到输入那一支同一个方向"
                                "  ·  裁决写进审计的 permission 事件",

    # --- 提问面板（`QuestionPanel`）------------------------------------------
    "question_dialog.head": "▣ agent 需要你的判断",
    "question_dialog.no_options": "（这个问题没有给选项，直接在输入行回答）",
    "question_dialog.skip": "跳过 Esc",
    "question_dialog.footer1": "回车是最容易做的动作，所以跳过必须是显式的一个键",
    "question_dialog.footer2": "回给后端的是选项原文，不是编号 —— 编号只是界面的表示法",

    # --- 技能面板 ------------------------------------------------------------
    "skills.title": "技能",
    "skills.empty": "工作区里没有技能（.tudouni/skills/<名字>/SKILL.md）",
    "skills.footer": "✓ = 已加载  ·  Esc 关闭  ·  完整清单：main.py --skills",

    # --- 选择面板（`OptionPicker`）与会话面板 --------------------------------
    "option.count": "{n} 个",
    "option.footer": "↑↓ 选择  ·  Enter 确认  ·  Esc 取消  ·  ● = 当前",
    "session_dialog.head": "◱ 换一个会话",
    "session_dialog.empty": "还没有保存过会话 —— 说过第一句话才会有。",
    "session_dialog.footer": "↑↓ 选择  ·  Enter 切过去  ·  Esc 取消  ·  ● = 当前会话",

    # --- MCP 面板 ------------------------------------------------------------
    "mcp_dialog.head": "◱ MCP 服务器",
    "mcp_dialog.empty": "没有配置任何 MCP server。清单在 ~/.tudouni/mcp.json"
                        "（服务器地址或要启动的命令都写在那里）。",
    "mcp_dialog.running": "{loaded} / {total} 在跑",
    "mcp_dialog.footer": "↑↓ 选择  ·  Enter 挂载 / 卸载  ·  Esc 关闭  ·  "
                         "开关只影响这次运行，不改 mcp.json",
    "mcp_dialog.pending": "正在等 runtime 处理 `{name}`…"
                          "（连服务器可能要几秒；撞上一轮在跑就等它跑完）",

    # --- 键位说明（只出现在 Textual 自带的按键面板和它的 Ctrl+P 里）----------
    # 它们**不能写在类体里**：Textual 在类创建时就把 BINDINGS 合并好了，写在类体里
    # 等于把语言冻在 import 那一刻（见 `widgets.localize_bindings`）。
    "bindings.deny": "拒绝",
    "bindings.allow": "允许",
    "bindings.always": "总是允许",
    "bindings.allow_all": "都允许",
    "bindings.skip": "跳过",
    "bindings.choose": "选 {n}",
    "bindings.close": "关闭",
    "bindings.cancel": "取消",
    "bindings.newline": "换行",
    "bindings.palette": "命令面板",
    "bindings.quit": "退出",
    "bindings.up": "上一条",
    "bindings.down": "下一条",
    "bindings.thinking": "思考",
    "bindings.rail": "上下文栏",
    "bindings.skills": "全部技能",
    "bindings.escape": "中断/关闭",

    # --- 输入框与开场那几行（`app.py`）---------------------------------------
    "input.placeholder": "说点什么，回车发送（/ 看命令，/resume 换会话，Shift+Enter 换行）",
    "init.session_new": "（新的）",
    "init.session_id": "（会话 {name}{state}）",
    "init.return_hint": "想回到这个会话：/resume（在列表里挑，● 标着当前这个）",
    "session_load.restored": "（恢复 {n} 条历史，下面是你说过的和 agent 答过的）",

    # --- `app.py` 里剩下的那些提示 -------------------------------------------
    # 措辞的规矩（用户提的那条）：**说它是什么，不解释机制** —— 机制属于 doc/。
    "autopilot.report_on": "自动放行：开（需要审批的工具直接执行，审计里记 autopilot；"
                           "再执行一次 /autopilot 关闭）",
    "autopilot.report_off": "自动放行：关（恢复逐条询问）",
    "cmd.audit.line": "审计日志：{path}",
    "cmd.unknown": "没有这个命令：{name}（/help）",
    "cmd.thinking.unknown": "认不出这个写法：{rest}（用 /thinking on 或 /thinking off）",
    "cmd.effort.title": "思考强度",
    "cmd.model.title": "换模型",
    "cmd.quiet.unknown": "认不出这个写法：{rest}（用 /quiet on 或 /quiet off）",
    "cmd.mcp.unknown": "认不出这个写法：{rest}（用 /mcp load <名字> 或 /mcp unload <名字>）",
    "quiet.name": "安静模式 ",
    "quiet.on": "开",
    "quiet.on_note": "（只显示工具/思考过程/结果的简短信息）",
    "quiet.on_extra": "  只影响之后的显示；再按一次 /quiet 关掉",
    "quiet.off": "关",
    "quiet.off_note": "（恢复逐条显示）",
    "mcp.pending": "（{action} {name}：正在请 runtime 处理…）",
    "resume.loading": "正在取会话列表…",
    "switch.to_session": "正在切到会话 {name}…",
    "switch.new_session": "新会话",
    "help.commands_title": "命令（输入 / 会打开面板，↑↓ 选、Enter 执行）：",
    "help.keys_title": "键位：",
    "thinking.no_turn": "（还没有回合）",
    "thinking.no_reasoning": "（这一轮没有思考过程）",
    "thinking.gone": "（这一轮的思考已经不在画面上了）",
    "escape.interrupting": "已请求停下这一轮（会在当前这一步结束后停）",
    "escape.idle": "（这一轮没在跑 —— Esc 在弹层里是拒绝/跳过）",

    # --- 给人看的进度/状态行（runtime 那一侧）--------------------------------
    # 这几句的读者分工写在各自的函数上：`progress_line` / `active_line` / `summary`
    # 是**给人看的**，而同一批模块里的 `todo_note` / `job_note` / `skill_note` 是
    # **给模型的** —— 后者一个字都不许翻（见 tests/test_i18n.py 最后那两条）。
    "reasoning.summary": "{state} · {effort}",
    "todo.progress": "{done}/{total} 完成",
    "todo.progress.current": "，当前：{what}",
    "jobs.progress.running": "{n} 个在跑",
    "jobs.progress.uncollected": "{n} 个结果还没收",
    "jobs.progress.line": "{parts}（{ids}）—— 明细 job_list",
    "skills.active_label": "已加载技能：",

    # --- 审批面板里那两句"按下去会记住什么"（`security/asker.py`）-------------
    # **它们必须在 runtime 这一侧翻**：协议把它们原样发给前端，而
    # `outbound.schema.json` 写着"前端一个字都不许改"。schema 里那句说明本身也
    # 跟着改了口（见那边 `remember_hint` 的 doc）。
    "asker.remember.high": "以后每次都直接执行，你不会再看到它要做什么",
    "asker.remember.default": "以后不再询问这个工具",
    "asker.remember.prefix": "以后 {prefix} 开头的命令都直接执行，不会再给你看",
    "asker.remember.tail": "（写进 {label}，下次启动仍然有效）",
    "asker.trust_all": "以后 {group}都直接执行，你不会再看到它们要做什么",
    "asker.trust_all.snapshot": "（快照：这个 server 以后新加的工具仍然会问你；"
                                "写进 {label}，下次启动仍然有效）",
    "asker.trust_group": "MCP server {server} 的 {n} 个工具",

    # --- runtime 下发的启动通知（`composition.notices()`）--------------------
    # 它们经 `init.notices` / `notice` **原样显示**在会话流里，所以在产生它的这一侧
    # （子进程）按语言取 —— 前端不翻（它拿到的已经是一整句）。
    "notice.context.missing_window": "[上下文] 模型 {model} 不在目录里（或者配置里没写"
                                     "它的 context_window），末尾只报上下文用量、"
                                     "不报占比；把它那一行补上即可。",
    "notice.web.no_key": "[联网] 没配置搜索密钥，web_search 未注册"
                         "（fetch_web 不受影响）。要启用就在 {path} 的 \"web\" 段里写一行："
                         "\"tavily_api_key\": \"tvly-...\"",
    "notice.grep.unsupported_platform": "[搜索] 这个平台（{platform}）不在 grep 引擎的"
                                        "支持列表里（现在只有 x86_64 的 Windows / "
                                        "Linux），grep 未注册（搜文本只能走 shell，"
                                        "每次都要审批）。要支持它是两步，见 "
                                        "tools/vendor/rg/README.md。",
    "notice.grep.missing_binary": "[搜索] tools/vendor/rg/ 里少了 {triple} 这一份 "
                                  "ripgrep，grep 未注册（搜文本只能走 shell，每次都要"
                                  "审批）。跑 `uv run python scripts/fetch_rg.py` 补上。",
    "notice.mcp.configured": "[MCP] {file} 里配了 {n} 个 server：{names}"
                             "（都还没挂载 —— /mcp 看清单并逐个挂上）",
    "notice.mcp.loaded": "[MCP] server {name}：连上了，提供 {n} 个工具"
                         "（风险一律 high，每次调用都要你批准）",
    "notice.mcp.ignored_workspace_file": "[MCP] 忽略了 {path}：server 清单只从用户级 "
                                         "{file} 读。理由是这里的 command 是启动时就要"
                                         "执行的代码，而工作区里的文件可能随仓库一起被 "
                                         "clone 进来（见 config.McpConfig 上面的说明）。"
                                         "要用就把它挪到 {file}",
    "notice.config.legacy_env": "[配置] 忽略了 {path}：这个程序现在**不读任何环境变量、"
                                "也不读 .env**。密钥写在 {config} 里那条路由的 "
                                "\"api_key\" 上，搬完就可以删掉这个文件了。",
    "notice.jobs.leftovers": "[后台] 上次会话留下了 {n} 个后台任务的输出，已经清掉了。"
                             "这说明那一次没有正常退出（关掉了窗口、或者进程被强杀），"
                             "所以**那几个命令可能还在跑**，而它们不在这次会话的管辖里 "
                             "—— 如果端口或 CPU 对不上，自己确认一下。",
    "notice.jobs.no_job_object": "[后台] Windows 上那层「关掉窗口也把后台任务一起收掉」的"
                                 "保证没建起来（{problem}）。正常退出仍然会收干净，"
                                 "但**强杀本进程时后台命令可能变成孤儿**。",
    "notice.tools.header": "已注册工具:",
    "notice.tools.row": "  - {name} 风险={risk}",
    "notice.permissions.unknown_tools": "[权限] {file} 里这些工具没有注册，"
                                        "规则不会生效：{names}",
    "notice.permissions.levels": "[权限] 按等级自动放行 {levels}；点名免问 {named}",
    "notice.permissions.deny": "[权限] 直接拒绝 {names}",
    "notice.permissions.rules": "[权限] 命令规则（按前缀放行）{rules}",
    "notice.permissions.none": "（无）",
    "notice.model.session": "[模型] 这个会话选的是 {route}（{base_url}）—— "
                            "/model 可以换，/status 看现在这个。",
    "notice.reasoning.session": "[思考] 这个会话：{summary}（默认是开 · {default}）—— "
                                "/thinking 开关、/effort 改强度。",
    "notice.todos": "[任务] {line}",
    "notice.skills.available": "[技能] 可用 {n} 个：{names}",
    "notice.skills.shadowed": "[技能] 同名遮蔽：{item}",
    "notice.skills.problem": "[技能] {problem}",
    "notice.skills.active": "[技能] {line}",
    "notice.autopilot": "[权限] autopilot：不询问任何审批，需要审批的工具会直接执行；"
                        "也不会向你提问 —— 模型调 ask_user 会拿到「没有人回答」，"
                        "并被告知自己决定、把假设说出来（拒绝名单、工作区边界、"
                        "控制面写入仍然生效）",

    # --- `/model` `/thinking` `/effort` 的回话（runtime 那一侧）--------------
    # `select_*` 返回的是"说给用户听的一句话"，界面上照贴（`notice` 那一条路）。
    "model.select.no_name": "没给模型名。/model 不带参数看清单。",
    "model.select.no_route": "没有这条路由：{route} —— /model 不带参数看清单。",
    "model.select.route_empty": "路由 {route} 一个模型都没声明。",
    "model.select.ambiguous": "{name} 在多条路由上都有（{names}）—— "
                              "写全一点：/model provider/model",
    "model.select.unknown": "目录里没有这个模型：{name} —— /model 不带参数看清单。"
                            "（清单是配置里写死的几个名字，不会把任意名字转给网关："
                            "那样打错一个字母只会在下一次请求时才炸。）现在有：{known}",
    "model.select.none_known": "（一条都没有）",
    "model.select.route_gone": "那条路由不见了：{route}",
    "model.select.no_key": "路由 {route} 没有密钥，选不了它下面的模型 —— 在 {path} 里"
                           "**那条路由上**写一个 \"api_key\"。",
    "model.select.already": "已经是 {name} 了。",
    "model.select.unknown_previous": "（未知）",
    "model.select.no_switch": "这个会话的模型适配器不支持中途换模型（{kind}）—— "
                              "只能重启时在 {path} 里改默认值。",
    "model.select.switched": "换成 {name}（上一个：{previous}）—— 下一次请求生效。",
    "thinking.select.no_support": "这个会话的模型适配器不支持改思考模式。",
    "thinking.select.on": "思考模式：开（强度 {effort}）—— 下一次请求生效。",
    "thinking.select.off": "思考模式：关（强度记着，/thinking on 回来还是它）"
                           "—— 下一次请求生效。",
    "effort.select.no_support": "这个会话的模型适配器不支持改思考强度。",
    "effort.select.off_word": "`none` 是关掉思考，不是一档强度 —— 用 /thinking off"
                              "（强度会留着），或者 /effort {levels}。",
    "effort.select.unknown": "没有这一档强度：{effort} —— 能写的只有 {levels}"
                             "（端点还接受 {aliases} 这些等价写法）。",
    "effort.select.ok": "思考强度：{level} —— 下一次请求生效。",
    "effort.select.ok_off": "思考强度记成 {level} 了，但思考模式关着"
                            "（/thinking on 才用得上）。",

    # --- AGENT.md 那一块（`state/agents_md.py`）------------------------------
    # **只翻"给人看"的那一半**：`SECTION_TITLE` / `SECTION_LEAD` / `text_block` /
    # `truncation_footer` 是**进 system 消息**的，一个字都不许动。
    "agents_md.reason.is_dir": "这是一个目录，不是文件",
    "agents_md.reason.too_big": "{size} 字节，超过 {limit} 字节的上限，没有注入",
    "agents_md.reason.not_utf8": "不是 UTF-8 文本，没有注入（用 UTF-8 重存一次就好）",
    "agents_md.reason.permission": "没有读取权限",
    "agents_md.notice.loaded_item": "{path}（{n} 行）",
    "agents_md.notice.loaded": "[AGENT.md] 读取了 {listed}",
    "agents_md.notice.truncated_lines": "只注入了前 {lines} 行（共 {total} 行）",
    "agents_md.notice.truncated_chars": "文本还被截掉 {omitted} 个字符，末尾是断的",
    "agents_md.notice.truncated": "[AGENT.md] {path} 超过注入额度，{detail}；"
                                  "完整的要模型用 read_file 去读。",
    "agents_md.notice.failed": "[AGENT.md] 读不了 {path}：{reason}",
    "session.preview.unreadable": "（读不出来：{kind}）",
    # 老 CLI 那句「审计日志写到哪」（`main.py` 单独把它拎到最后打）。
    "notice.audit_line": "审计日志写到 {path}",

    # --- 模型目录的校验与说明（`state/catalog.py`）---------------------------
    # 报错文案也在这一层翻：它们既会打在那份配置文件旁边（普通终端），也会作为
    # `[模型] …` 的通知出现在会话流里 —— 同一句话只有一处来源。
    "catalog.where.provider": "{file} 的 providers.{name}",
    "catalog.where.model": "{where} 的 models[{index}]",
    "catalog.error.not_string": "{where} 的 \"{key}\" 必须是字符串，实际是 {kind}",
    "catalog.error.not_bool": "{where} 的 \"{key}\" 必须是 true/false",
    "catalog.error.not_positive_int": "{where} 的 \"{key}\" 必须是一个正整数（token 数），"
                                      "实际是 {value}；不知道就整个删掉这一行 —— "
                                      "那种情况界面只报用量、不报占比",
    "catalog.error.models_not_list": "{where} 的 \"models\" 必须是一个数组",
    "catalog.error.not_object": "{spot} 必须是一个对象",
    "catalog.error.unknown_keys": "{spot} 里有不认识的键：{names}\n"
                                  "  认识的只有：{known}",
    "catalog.error.missing_id": "{spot} 少了 \"id\"（发给端点的模型名）",
    "catalog.error.duplicate_id": "{spot} 的 id {id} 和前面那条重复了",
    "catalog.error.bad_effort": "{spot} 的 reasoning_effort {effort} 不认识；"
                                "能写的只有 {levels}（＋ {aliases} 这些等价写法）",
    "catalog.error.missing_base_url": "{where} 少了 \"base_url\"（请求发到哪）",
    "catalog.problem.no_models": "[模型] 路由 {route} 一个模型都没声明（\"models\" 是"
                                 "空的），所以它不会出现在 /model 里",
    "catalog.problem.no_key": "[模型] 路由 {route} 没有密钥（{where}），"
                              "选不了它下面的模型 —— 在**这条路由里**写上 \"api_key\"",
    "catalog.problem.no_usable_route": "[模型] 一条可用的路由都没有（每条都缺密钥）"
                                       "—— /model 会摆出一张选不了的清单",

    # --- 配置文件的校验（`runtime/config.py`）--------------------------------
    # 分号/括号那些空行和缩进是**文案的一部分**（照着改的时候要能读）。
    "config.error.unknown_keys": "{where} 里有不认识的键：{names}\n"
                                 "  认识的只有：{known}\n"
                                 "  （写错一个键名而它静默不生效是最坏的失败形态，"
                                 "所以这里直接停下）",
    "config.error.unknown_web_keys": '{path} 的 "web" 里有不认识的键：{names}\n'
                                     "  认识的只有：{known}\n"
                                     "  （写错一个键名而它静默不生效是最坏的失败形态，"
                                     "所以这里直接停下）",
    "config.error.auto_approve_high": '{file} 的 auto_approve 不接受 "high"：'
                                      "等级是工具自己\n"
                                      '  声明的，"放行所有 high" 会随着将来新加的工具'
                                      "自动变宽。要放行 shell\n"
                                      "  就点名它："
                                      '{{"auto_approve_tools": ["shell"]}}',
    "config.error.auto_approve_unknown": "{file} 的 auto_approve 里有未知等级 {levels}；"
                                         "能按等级放行的只有 {allowed}",
    "config.error.contradictory": "{file} 里 {names} 同时出现在 auto_approve_tools 和 "
                                  "deny_tools —— 这两句互相矛盾，在这里改掉，"
                                  "别让策略去猜哪个算数",
    "config.error.bad_shell_rule": "{file} 的 shell_allow 里有一条写错的规则：{problem}",
    "config.error.mcp_problem": "{file} 有问题：{problem}",
    "config.error.not_utf8": "{path} 不是 UTF-8 编码，读出来是乱码。"
                             "用记事本「另存为」时选 UTF-8。",
    "config.error.unreadable": "读不了 {path}：{error}",
    "config.error.bad_json": "{path} 不是合法 JSON：第 {line} 行第 {column} 列 {message}",
    "config.error.not_object": "{path} 的最外层必须是一个 JSON 对象（{{...}}），"
                               "实际是 {kind}",
    "config.error.not_string_list": '{file} 的 "{key}" 必须是字符串数组，'
                                    '例如 ["shell"]',
    "config.error.empty_string_in_list": '{file} 的 "{key}" 里有空字符串',

    # --- 协议层回给界面的那几条（`protocol/channels.py`）---------------------
    # 它们大多数是**用户操作出错**（`/model` 给了个对象、`/mcp` 动作不认识），
    # 所以按 notice 发给前端显示 —— 自然要跟着界面语言。
    "channels.direction.frontend": "前端发来",
    "channels.write_failed": "[warn] 往前端写一行失败（已忽略）：{problem}",
    "channels.model.needs_string": "[模型] 换模型要一个字符串模型名。/model 看清单。",
    "channels.model.no_session": "[模型] 还没有会话，换不了模型。",
    "channels.model.reply": "[模型] {message}",
    "channels.model.not_changed": "[模型] 没换：{message}",
    "channels.effort.needs_string": "[思考] 强度要一个字符串（low / high / max）。",
    "channels.effort.no_session": "[思考] 还没有会话。",
    "channels.effort.reply": "[思考] {message}",
    "channels.effort.not_changed": "[思考] 没改：{message}",
    "channels.thinking.no_session": "[思考] 还没有会话。",
    "channels.thinking.reply": "[思考] {message}",
    "channels.thinking.not_changed": "[思考] 没改：{message}",
    "channels.session.needs_string": "[会话] 换会话的 id 必须是一个字符串",
    "channels.session.bad_id": "[会话] 非法 id：{name} —— 只能用字母、数字、下划线、"
                               "连字符（1~64 个字符）。/resume 不带参数可以从列表里挑。",
    "channels.session.switch_failed": "[会话] 换不过去（当前会话没有变）：{problem}",
    "channels.session.close_failed": "收掉上一个会话的 runtime 时出错：{problem}",
    "channels.run_failed": "[本轮失败] {problem}",
    "channels.status.no_session": "[状态] 还没有会话。",
    "channels.tools.no_session": "[工具] 还没有会话。",
    "channels.mcp.no_session": "[MCP] 还没有会话。",
    "channels.mcp.no_host": "[MCP] 这个 runtime 没有 MCP 宿主，改不了挂载。",
    "channels.mcp.unknown_action": "[MCP] 认不出这个动作：{action}（只有 {actions}）",
    "channels.mcp.list_note": "[MCP] 当前挂载情况（配置里改了要重启才生效）",
    "channels.mcp.needs_name": "[MCP] {action} 要给出 server 名字，一次一个",

    # --- MCP：配置解析与连接（`tools/mcp.py`）--------------------------------
    # **`render_content` 那几条不在里面**：它们进的是**工具结果**（模型读的），
    # 和这个文件里其它给人看的句子不是一回事。
    "mcp.cfg.unknown_top_keys": "不认识的键：{names}；最外层只有 servers 一个键",
    "mcp.cfg.servers_not_object": '"servers" 必须是一个对象：'
                                  '{{"名字": {{"command": ...}}}}',
    "mcp.cfg.bad_name": "服务器名 {name} 不合法：只能用字母、数字、下划线、连字符，"
                        "长度 1~24 —— 它要拼进给模型看的工具名（{prefix}<名字>__<工具>）",
    "mcp.cfg.server_not_object": 'servers["{name}"] 必须是一个对象',
    "mcp.cfg.unknown_server_keys": 'servers["{name}"] 里有不认识的键：{names}\n'
                                   "  认识的只有：{known}",
    "mcp.cfg.command_not_string": 'servers["{name}"].command 必须是一个字符串',
    "mcp.cfg.url_not_string": 'servers["{name}"].url 必须是一个字符串',
    "mcp.cfg.both_given": "两个都给了",
    "mcp.cfg.neither_given": "两个都没给",
    "mcp.cfg.both_or_neither": 'servers["{name}"] 必须恰好给出一种连接方式'
                               "（现在是{given}）：\n"
                               '  本地：{{"command": "npx", "args": [...]}}；'
                               '远程：{{"url": "https://example.com/mcp", '
                               '"headers": {{...}}}}',
    "mcp.cfg.empty_scheme": "空",
    "mcp.cfg.bad_url": 'servers["{name}"].url 要是一个完整的 http(s) 地址'
                       "（现在这个的 scheme 是 {scheme}）：{url}",
    "mcp.cfg.local_only_key": 'servers["{name}"] 是远程 server（给了 url），'
                              "而 {key} 只对本地 server（command）有意义",
    "mcp.cfg.args_not_list": 'servers["{name}"].args 必须是字符串数组，'
                             '例如 ["-y", "包名"]',
    "mcp.cfg.env_not_map": 'servers["{name}"].env 必须是"字符串 → 字符串"的对象',
    "mcp.cfg.headers_not_map": 'servers["{name}"].headers 必须是"字符串 → 字符串"'
                               '的对象（凭据写在这里，例如 '
                               '{{"Authorization": "Bearer …"}}）',
    "mcp.cfg.bad_header_name": 'servers["{name}"].headers 里的头名 {header} 不合法：'
                               "HTTP 头名只能是 ASCII",
    "mcp.cfg.bad_timeout": 'servers["{name}"].timeout_seconds 必须在 {low}~{high} 之间',
    "mcp.spawn_failed": "起不来 `{command}`：{problem}",
    "mcp.timeout": "等 {method} 超过 {seconds} 秒没有回应",
    "mcp.write_failed": "往 server `{name}` 写数据失败：{problem}",
    "mcp.stdout.not_json": "[MCP] server `{name}` 的 stdout 上有一行不是 JSON，已跳过",
    "mcp.stdout.closed_with_code": "MCP server `{name}` 的 stdout 关了"
                                   "（进程退出码 {code}）",
    "mcp.stdout.closed": "MCP server `{name}` 的 stdout 关了",
    "mcp.close_failed": "[MCP] server `{name}` 没能收掉（进程仍在），"
                        "它可能继续占着管道",
    "mcp.closed": "MCP server `{name}` 已关闭",
    "mcp.close_error": "[MCP] 关闭 server `{name}` 时出错：{problem}",
    "mcp.http.connect_timeout": "连不上 server `{name}`（连接超时）：{problem}",
    "mcp.http.timeout": "等 {method} 超过 {seconds} 秒没有回应",
    "mcp.http.connect_failed": "连不上 server `{name}`（{problem}）",
    "mcp.http.status": "server `{name}` 回了 HTTP {status}：{body}",
    "mcp.http.body_too_big": "回应的正文超过 {mb}MB，放弃",
    "mcp.http.no_sse_response": "server `{name}` 的 SSE 流里没有一条 JSON-RPC 回应",
    "mcp.http.not_json": "server `{name}` 回了不是 JSON 的正文：{body}",
    "mcp.http.not_object": "server `{name}` 回的不是一个 JSON 对象",
    "mcp.result.no_message": "（server 没有给出说明）",
    "mcp.result.refused": "{who} 拒绝了 {method}：{text}（code={code}）",
    "mcp.list_tools.not_object": "tools/list 的回应该是一个对象",
    "mcp.list_tools.too_many_pages": "tools/list 翻了 {pages} 页还没到底，放弃",
    "mcp.call.not_object": "tools/call 的回应该是一个对象",
    "mcp.call.failed": "{name} 执行失败（server 没有给出说明）",
    "mcp.no_tools_capability": "[MCP] server `{name}` 的 capabilities 里没有 tools，"
                               "它不提供任何工具（已连上，工具数为 0）",
    "mcp.load_failed": "[MCP] server `{name}` 没连上（{problem}）；"
                       "它提供的工具这次都不可用",
    "mcp.name_clash": "[MCP] server `{name}` 的 `{tool}` 暴露名和另一个工具撞了"
                      "（{other}），这一个这次装不上",

    # --- 技能文件的问题（`skills/loader.py`）---------------------------------
    # **它们是 code + 参数**，由消费方渲染（`skills.loader.render(problem, i18n.t)`）：
    # `skills/` 是叶子包，不许 import i18n（`tests/test_imports.py` 盯着那条边）。
    "skills.problem.no_frontmatter": "文件开头必须有 frontmatter，第一行是三个连字符"
                                     "（---）",
    "skills.problem.unclosed_frontmatter": "frontmatter 没有闭合：缺少第二行三个连字符"
                                           "（---）",
    "skills.problem.indented": "这一行有缩进：{line}。本解析器只认顶格的 `键: 值`，"
                               "不接受嵌套结构或 `- ` 列表项（`metadata` 之下除外）",
    "skills.problem.unparsable": "看不懂这一行：{line}（只支持顶格的 `键: 值`）",
    "skills.problem.missing_key": "这一行缺少键名：{line}",
    "skills.problem.unknown_key": "不认识的键 {key}；本程序认识的只有 {known}",
    "skills.problem.duplicate_key": "键 {key} 写了两遍 —— 哪一遍算数没有答案",
    "skills.problem.multiline_block": "{key} 用了多行块写法（{value}），"
                                      "本解析器不支持；写成一行",
    "skills.problem.bad_chars": "{key} 的值里有本解析器不支持的字符（{chars}）；"
                                "值写成简单的一行文本，或者整个用引号包起来，"
                                "需要说明就写进正文",
    "skills.problem.missing_name": "frontmatter 缺少 name",
    "skills.problem.name_mismatch": "name={name} 和目录名 {directory} 不一致 —— "
                                    "两者必须相同，否则模型看到的技能名和文件路径对不上",
    "skills.problem.bad_name": "name={name} 不合法：只能用小写字母、数字和连字符，"
                               "且不能以连字符开头/结尾或连着两个（例如 pdf-extract）",
    "skills.problem.name_too_long": "name 超过 64 个字符（{length}）",
    "skills.problem.missing_description": "frontmatter 缺少 description —— "
                                          "它是模型判断「什么时候该用这个技能」的"
                                          "唯一依据（技能正文在被加载之前是看不见的）",
    "skills.problem.too_big": "文件 {size} 字节，超过上限 {limit} —— "
                              "正文会拼进每一次请求，所以超限时拒绝加载而不截断；"
                              "把细节挪到同目录的另一个文件里，"
                              "让模型需要时自己用 read_file 读",
    "skills.problem.escape": "技能目录越界（软链指到了技能目录外面）：{path}",
    "skills.problem.unreadable": "读不了 {file}（{problem}）",
    "skills.problem.skipped": "{name} 被跳过：{reason}",
    "skills.problem.shadowed": "{name} 取 {winner}，{losers} 被它遮住了",
    "skills.scan_dirs": "  扫描目录（优先级从低到高）：",
    "skills.scan_none": "    （一个都不存在）",
    "skills.shadowed_line": "  [遮蔽] {item}",

    # --- 启动前的几道检查（`composition.check_*` / 无模型提示）----------------
    "check.workspace.home": "{here} 是你的 home 目录",
    "check.workspace.root": "{here} 是文件系统的根",
    "check.workspace.above_home": "{here} 在 home 的上层（它下面是所有人的 home）",
    "check.workspace.refused": "不能把这里当工作区：{what}。\n"
                               "  工作区就是当前目录，而 agent 的文件工具**只能读写"
                               "工作区里的东西** ——\n"
                               "  在这里启动等于把它下面的一切都交出去（.ssh、"
                               "别的项目的 .env、浏览器数据……），\n"
                               "  而 read_file 是免审批的，你不会被问第二次。\n"
                               "  先 cd 进一个具体的项目目录再跑。",
    "check.session_id.invalid": "非法的 --session：{name}\n"
                                "  会话 id 只能由字母、数字、下划线、连字符组成，"
                                "长度 1~64 ——\n"
                                "  因为它会被拿去拼文件名（{dir}/sessions/<id>.jsonl 和"
                                " {dir}/logs/<id>.jsonl）。\n"
                                "  用 --list 看一下有哪些现成的 id。",
    "no_model.created": "一条能用的模型路由都没有 —— 配不出模型就什么都干不了。\n"
                        "\n"
                        "我已经在这儿给你建好了一份配置，打开它、填上你的模型和密钥：\n"
                        "    {path}\n",
    "no_model.missing": "一条能用的模型路由都没有 —— 配不出模型就什么都干不了。\n"
                        "\n"
                        "配置写在 {config}（模板见 {example}）。\n",
    "no_model.intro": "模型层是抽象的：端点、模型名、密钥全由配置里的 providers 决定，\n"
                      "代码里没有写死任何一家。在 \"providers\" 里加一条路由就能用：",
    "no_model.route_fields": "    一条路由给三样东西：base_url（请求发到哪）、"
                             "api_key（密钥就写在这条路由里）、\n"
                             "    models（这条路上有什么）。"
                             "**第一条有密钥的路由就是默认路由**，顺序由你排。",
    "no_model.notes": "这次读到的路由：",
    "no_model.problems": "逐条问题：",

    # --- MCP 宿主（`composition.McpHost`，`/mcp` 面板回的那几句话）------------
    "mcp.host.already_loaded": "server `{name}` 已经挂上了（{n} 个工具），没有重复加载",
    "mcp.host.unknown_server": "清单里没有 server `{name}`：{known}",
    "mcp.host.load_failed": "server `{name}` 没连上（{problem}）；再按一次是重试",
    "mcp.host.loaded": "server `{name}` 挂上了：{n} 个工具"
                       "（风险一律 high，每次调用都要你批准）",
    "mcp.host.not_running": "server `{name}` 本来就没在跑",
    "mcp.host.unloaded": "server `{name}` 卸下了（摘掉 {n} 个工具）；"
                         "配置里那一行还在（下次启动不会自己挂上 —— "
                         "启动时不自动挂，要用就再 load 一次）",
    "mcp.host.no_servers": "（{file} 里一个 server 都没配）",
    "mcp.host.close_failed": "关闭 MCP server `{name}` 时出错：{problem}",
    "mcp.host.reread_failed": "重读 {file} 时出错（这一次的新增认不出来）：{problem}",

    # --- 落盘与收摊时的警告（失败路径，但会打到 stderr 上）--------------------
    "save.action.model": "换模型",
    "save.action.thinking": "改思考模式",
    "save.action.effort": "改思考强度",
    "save.failed": "{what}之后落盘失败（这一次仍然生效，重开会话会回到配置里那个）："
                   "{problem}",
    "close.jobs_failed": "收后台任务时出错：{problem}",
    "close.http_failed": "关闭 http client 时出错：{problem}",

    # --- 会话文件（`state/store.py`）与系统提示词缺失（`state/session.py`）----
    # `_env_block` 那一段（"## 运行环境"）**不在里面**：它是**进 system 消息**的，
    # 模型每轮都读它 —— 界面换语言不该动它。
    "store.bad_session_id": "非法 session_id: {name}",
    "store.shrunk": "会话 {name} 的消息从 {before} 条变成了 {after} 条；"
                    "这份存储只支持追加，不支持删改。",
    "store.newer_version": "会话 {name} 是更新版本写的（文件 version={version}，"
                           "本程序认识的最高版本是 {known}）；升级程序再打开它，"
                           "否则可能读错格式。",
    "store.missing_file": "会话文件不存在：{path}",
    "session.prompt_missing": "系统提示词文件不存在：{file}\n"
                              "它不是一个可选文件 —— agent 每轮都要把它发给模型。",
}
