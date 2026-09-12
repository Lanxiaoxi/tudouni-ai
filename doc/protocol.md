# 协议（v1）

> **这份文档讲语义，不讲形状。** 字段名和类型在
> `agent_runtime/protocol/schema/*.schema.json` 里（机器可读，将来的 TS 类型由它
> 生成），这里只回答 schema 回答不了的问题：**什么时候发、收到怎么办、错了怎么办、
> 哪些事客户端不许自己决定。**
>
> **不要在这份文档里抄字段表。** 抄了就是第三个来源（schema 一个、这里一个、
> 生成的类型一个），而它一定会漂 —— 而且漂得看不出来。
>
> 设计背景和取舍的来龙去脉在 `doc/TUI-design.md`（那份是设计文档，这份是契约）。

## 0. 一句话

**runtime 是一个子进程，stdin/stdout 是 JSONL。** 每行一条 JSON、UTF-8、`\n` 结尾、
每条 flush。你（客户端）负责画界面和回答人机交互，runtime 负责别的一切。

客户端可以换语言、换框架、换进程拓扑 —— runtime 看不见。这不是宣传：它已经被
"从 Ink 换成 Textual"和"将来要加 Web"预演过两次了。

## 1. 传输

| 项 | 约定 |
|---|---|
| 通道 | 子进程的 **stdout**（runtime → 你）和 **stdin**（你 → runtime） |
| 编码 | **UTF-8**，两端都显式指定（子进程的 stdout 是管道，编码由环境决定 —— 不显式管会在第一次出中文时炸） |
| 分行 | 一行一条，`\n` 结尾。**`ensure_ascii=False`**：中文按原样写 |
| 缓冲 | 每条 flush。不 flush 的话事件攒在 4~8KB 缓冲里，"实时"变成"每 8KB 一跳" |
| stderr | **不参与协议。** 它是人看的诊断（traceback、`[warn]`）。你可以继承它、也可以收进一个"原始日志"面板 |

**起子进程的三件事**（每一件都是踩过才知道的）：

```python
subprocess.Popen(
    [sys.executable, "-u", "<仓库>/agent_runtime/main.py", "--runtime-stdio"],
    cwd="<仓库>",                      # ← 见第 3 条
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    stdin=PIPE, stdout=PIPE, encoding="utf-8",
)
```

1. **`sys.executable`，不是 `"python"`。** 你在哪个解释器里（venv、uv 管的那个），
   子进程就必须是同一个。写 `"python"` 会走到系统 PATH 上另一个解释器，而那个里面
   没装 `openai` / `pydantic` —— 症状是子进程立刻退出、你读到 EOF。
2. **`-u`**：stdout 接管道时 Python 用块缓冲。
3. **绝对路径 + `cwd=仓库根`，不要用 `python -m agent_runtime.main`。** 项目是
   `package = false`，包没被安装，`-m` 找不到它（而报错是
   `No module named agent_runtime`，看起来像环境装错了）。

现成的实现：`agent_runtime/protocol/client.py`（Python，同步）。

## 2. 信封

每条消息都带 `v`（当前是 `1`）和 `t`（消息种类）。

**`v` 和 `init.protocol` 是两件事，别合并**：`v` 是"这一行的信封长什么样"
（能不能解析），`protocol` 是"这一整轮会话谈定的语义版本"（能不能对上话）。
分开之后，将来在 `v` 不变的情况下演进语义是可能的。

**版本对不上是唯一该硬失败的地方。** runtime 收到不认识的消息版本时会往 stderr
说一句、然后退出。你收到 `v` 不等于 1 的 `init` 时也该停下 —— 继续下去只会拿一堆
看不懂的字段去驱动界面。

**不认识的 `t` / `kind` / 字段一律忽略并继续**。两端会分别升级，而死在不认识的
消息上是最没必要的兼容性损失。runtime 这一侧就是这么做的（`protocol/channels.py`
的 `_dispatch` 最后那一段）。

## 3. 会话过程

```
你                                     runtime
│  （起子进程）
│  ◄──── init ────────────────────────  永远是第一条
│  ◄──── session_load ────────────────  0 或 1 条，紧跟 init
│
│  ──── user_message ────────────────►
│  ◄──── event(run_started) ──────────
│  ◄──── event(model_call) ───────────
│  ◄──── event(tool_call) ────────────
│  ◄──── permission_request           ← 要你回应，会一直阻塞
│  ──── permission_response ─────────►
│  ◄──── event(permission) ───────────
│  ◄──── event(tool_result) ──────────
│  ◄──── event(run_finished) ─────────
│  ◄──── ui(run_finished, answer) ────  **答案在这里**
│  （等下一句 user_message）
│
│  ──── interrupt ───────────────────►  停下**这一轮**，会话留着（见第 5 节）
│  ──── session_list ────────────────►
│  ◄──── sessions ────────────────────  已保存会话的清单（选会话用）
│  ──── session_switch ──────────────►  原地换一个会话（见 3.6）
│  ◄──── init + session_load + ui ────  **重发开场三连**，进程没重启
│  ──── shutdown ─────────────────────►  收摊：跑完当前这一轮再退出
```

`ui(kind:"state")` 在 `session_load` 之后还有一条（面板数据），以及每条
`tool_result` 之后各一条 —— 上面没画，因为它们不参与回合的时序（3.4 有说明）。

### 3.1 `init` —— 握手，永远是第一条

它带着你在画第一屏之前需要的一切：会话是谁、模型是什么、有哪些工具、权限范围、
审计日志在哪、以及启动时那几行说明（`notices`）。

**`permissions` 只含非默认项。** 默认（只有 `low` 自动放行、其余三个键都空）时它是
**空对象** —— 于是你那一行什么都不用显示。**别在界面里硬编码一份默认值**：判断
"什么算非默认"是 runtime 的 config 知识，你硬编码一份就是第二份事实，而它漂掉的
症状是"该显示的没显示"。

**`notices` 是这次运行的事实**（技能扫到了几个、权限放行了什么、MCP 连上了没有、
缺哪个密钥），不是诊断。CLI 把它们打到 stderr 是因为**它**的历史契约（`> 对话.txt`
要干净），你照自己的界面来。但**别丢掉它们** —— 尤其 `code == "mcp"` 里那条
"忽略了工作区里那份 mcp.json"：那是"文件明明在却不起作用"唯一的症状。

**`context_tokens` 是给你算占比的分母**（"上下文 14.1k / 1M（1.4%）"）。它**不在模型
的响应里** —— 那个形状里根本没有"上下文窗口"这个字段 —— 所以它来自 runtime 的一张
按模型名的表。**表里没有这个名字时它是 `null`**，那时候只报用量、不报占比：
**错的百分比比没有百分比更坏**（它会被当成真的）。这一条和 CLI 末尾那句统计同一个
口径（`frontends/cli` 的 `_context_note`）。

### 3.2 `session_load` —— 恢复会话的画面

**原样的 session.messages**，因为你要自己决定怎么画。

代价说明白：恢复一个长会话时这一条可能几 MB（`read_file` 不分页，一个 8MB 的文件
正文就躺在里面），所以它是**一条、只发一次**，而不是每条消息一个事件。**载入时给
一个"正在载入会话…"的提示** —— 本地管道几十 MB 也就几百毫秒，但界面不能静止。

### 3.3 `event` —— 审计的原样转发

**这就是 `.tudouni/logs/<id>.jsonl` 里那一行，只是包了一层信封。** 字段一个不多、
一个不少。这一条让"审计 = 协议"在字节层面成立，而好处很实际：`--audit` 能看到的东西
你的界面都能看到，两边永远对得上。

七种 `kind`：

| kind | 你会关心的字段 |
|---|---|
| `run_started` | `user_input`（预览） |
| `model_call` | `status`（ok/error/fatal）、`attempt`、`duration_ms`、`backoff_ms`、`prompt_tokens`/`cached_tokens`/`miss_tokens`/`completion_tokens`、`tool_calls`、**`reasoning`（全文）** |
| `tool_call` | `tool`、`call_id`、`tool_index`、`arguments`（**200 字符预览**） |
| `tool_result` | `tool`、`call_id`、`tool_index`、`status`、`chars`、`duration_ms`、`parallel`、工具自带字段 |
| `permission` | `tool`、`risk`、`decision`、`outcome`、`waited_ms`、`remembered`、`rule` |
| `tool_batch` | `calls`、`wall_ms`、`tools`（只有并发批次才有这条） |
| `run_finished` | `stop_reason`、`duration_ms` |

**三件要记住的**：

1. **`tool_call.arguments` 是预览（200 字符），不是全文。** 全文只在
   `permission_request` 上（那是给人做判断的）。想渲染工具卡片得另想办法 ——
   工具结果**全文在 `session_load.messages` 的 `role=="tool"` 那条里**。
2. **`outcome` 有八种**（见 `security/gate.py` 的模块 docstring），它们事后要回答的
   问题不同：`approved` 是"这一次有人看过"，`rule_allowed` / `command_allowed` /
   `auto_allowed` 是三种"没有人在场"的放行，`autopilot` 是"这一轮没人可问"。
   别合并显示。
3. **`reasoning` 是全文**（决策：思维链进审计）。它是审计里唯一的"内容型"字段，
   所以它可能很长、也可能含模型读到的代码。**默认折叠**成一行"思考过程（N 字符）"。

### 3.4 `ui` —— 只给界面的东西，两种 `kind`

它和 `t:"event"` 的分工是**一件事**：**`ui` 不进审计**。两种 `kind` 都服从这一条。

**`kind:"run_finished"` 带 `answer`**（`Agent.run` 的返回值）。

**这是非流式模式下你拿到答案的唯一途径** —— 审计里**没有**正文（`Agent.run` 的返回
值只交给调用方）。少发它，界面就一片空白。

它和 `event` 那条 `run_finished` 是**两条消息**，靠 `run_id` 配对。**顺序不保证**
（一个来自回合线程的收尾、一个在它之后），所以别去补偿顺序：**用 `answer` 拿正文、
用 `event` 改状态**。

**`kind:"state"` 是面板数据快照**（任务列表 / 已加载技能 / 可用技能清单 / 权限范围 /
会话规模）。它存在的原因很具体：**这些状态住在子进程里**（`session.metadata` 和
`PermissionPolicy`），而你是另一个进程 —— 没有这条消息，"agent 现在在做哪几件事"
"我现在放行了什么"就只剩"另开一个终端跑 `--skills` / `--audit`"这一条出口。

**什么时候发**：开场一条（紧跟 `session_load`，**这一条带 `skill_catalog`**），
以及每一条 `tool_result` 之后（`todo_write` / `load_skill` 改的正是那两块），
回合收尾时再补一条。**别按工具名去挑** —— 那会让协议层认识具体工具。

**`risk_scope` 里的处置是算好的**（`{"risk":"medium","disposition":"ask"}`），
不是让你拿 `auto_approve` 自己推：**"哪几档自动放行"是 `PermissionPolicy` 的判定**，
界面重做一遍就是第二份事实（第 7 节那条）。

**它不该混进对话流**：任务列表每更新一次就往会话里插一段，会把"你问的 + 它答的"
冲稀。它属于常驻面板（左栏那种）。

### 3.5 `notice` —— 运行期的旁白

模型失败、协议层丢了一行、等等。`level` 是 `info` / `warn`，`code` 是机器认的类别。
**`warn` 必须比其余的更显眼**（它是"出事了"和"就是提一句"的分界）。

换会话失败也走它（见 3.6）：`code == "session"` 那条出现时，**当前会话一点都没变**，
你什么都不用收拾。

### 3.6 `session_switch` / `session_list` —— 换会话，不重启进程

**这一节替换的是"杀掉子进程、带另一个 `--session` 重启"。** 那条路能用，但它让
"换一个会话"变成了一件要退出界面的事 —— 而界面里最需要它的时刻（刚聊完一个话题）
恰恰是最不该退出重来的时候。

```json
你 → {"v":1,"t":"session_switch","session_id":"20250101-120000"}
你 → {"v":1,"t":"session_switch","session_id":null}      // 新会话，id 由 runtime 分配
你 → {"v":1,"t":"session_list"}
你 ← {"v":1,"t":"sessions","items":[{"session_id":"…","messages":12,"steps":7,
                                     "todos":"","preview":"帮我把 TUI 的…"}]}
```

**它为什么不只是一条"前端自己刷新一下"的消息**：会话状态（任务列表、已加载技能）住在
**子进程的 `session.metadata`** 里，而且绑在一个工具注册表上（`TodoBoard` /
`SkillBoard` 是它的成员）。所以换会话 = runtime **重新装配一遍自己**（模型 client →
工具注册表 → Agent → MCP 子进程），然后重发 `init` + `session_load` + `ui(state)`。

四条要记住的：

1. **判据是 `init.session_id` 变了，不是"我发过那条请求"。** 换会话**可能失败**
   （配置坏了、id 非法），而失败时 runtime **原样保留旧会话**。所以界面**不许**在
   发出请求时就把画面清掉 —— 那会让一次失败变成"我的会话没了"；
2. **它是同步的，而且会先等当前这一轮跑完**（和 `shutdown` 同一条规矩）。理由和
   第 5 节那句一样：中断会留下一条带 `tool_calls` 却没有结果的 assistant 消息；
3. **`session_id` 不存在 = 新会话**（和 CLI 的 `--session` 同一条语义），**id 由 runtime
   分配** —— 前端不许自己编：分配 id 要碰磁盘确认没撞名，那是 store 的知识；
4. **`session_list` 是只读的**，而且**别自己去读 `.tudouni/sessions/`**：目录布局不是
   协议的一部分（store 是留着换实现的余地的），而"每条会话长什么样、预览怎么截"
   必须只有一份。清单**按创建时间、最新的在前**给 —— 要接着聊的几乎总是最近那个，
   而"创建时间"是这个协议键的语义，**不是"id 倒序"**：`--session demo` 那种自己起的
   名字不是时间戳，按 id 排会把它排错位置。

## 4. 人机交互：两条会阻塞的消息

**收到 `permission_request` / `question_request` 之后，runtime 会一直等你回应。**
不回应 = 整个会话停在那里（它不会超时 —— 人就在键盘前）。

**这两条和别的消息有一条本质区别：它们要一个回答。** 所以客户端对它们的处理方式是
"回答"而不是"显示"：

| 你的界面 | 怎么做 |
|---|---|
| 行式（ANSI / CLI） | `on_permission` 当场问、当场返回一个 `decision` —— 它就在读线程上，而人就在键盘前 |
| 图形（TUI） | **返回 `None`**，然后由界面在用户点完按钮之后调 `client.answer_permission(...)` |

**返回 `None` 是"我稍后自己回"，不是"我不回"。** 一个返回 `None` 之后再也没回答的
界面，会让子进程永远等下去 —— 所以那条路径上"用户关掉面板"必须有明确归宿
（TUI 里是 `dismiss(None)` → 按**拒绝**处理，fail-closed）。

**兜底答案是个陷阱。** 读线程上返回一个 `deny` 当占位、想"稍后覆盖"，看起来能跑，
实际必然坏：客户端会把它**立刻**发出去，子进程据此拒绝并继续跑，等用户点 [允许]
时那条回应已经没人要；更糟的是中间那次拒绝会进审计、记成 `user_denied`
—— 等于**伪造了一条"用户拒绝过"的记录**。（这是实测踩出来的，见
`frontends/tui/app.py` 里 `on_permission` 的注释。）

### 4.1 `permission_request` / `permission_response`

```json
{"v":1,"t":"permission_request","id":"p-7","call_id":"call_abc",
 "tool":"shell","risk":"high",
 "arguments":{"command":"rm -rf build/"},
 "remember":{"prefix":["rm","-rf","build"]},
 "remember_hint":"以后 rm -rf build 开头的命令都直接执行，不会再给你看（写进 permissions.json，下次启动仍然有效）",
 "allow_trust_all":false,"trust_all_hint":null}
```

回：

```json
{"v":1,"t":"permission_response","id":"p-7","decision":"allow"}
```

`decision` ∈ `allow` / `deny` / `always` / `always_group`。

**四条不能破的**：

1. **`arguments` 是全文，不截断。** 它是"给人做判断的那份参数"本身，不是某个事件的
   伴随字段。高风险工具尤其不能截断：`git status && rm -rf /` 的危险那半句就在后面。
2. **`remember_hint` 和 `trust_all_hint` 原样显示、一个字都不许改。** 它们是
   "按下去会发生什么"唯一的说明，出自 `security/asker.py` 的 `_remember_hint` /
   `_trust_all_hint`。**不要自己写一句** —— 尤其 `trust_all_hint` 里那半句"这是快照
   （这个 server 以后新加的工具仍然会问你）"不能省：省的后果是用户以为"这个 server
   从此随便用"。
3. **`remember` 为 `null` 时不显示 [总是允许]；`allow_trust_all` 为 `false` 时
   不显示 [都允许]。** 不要自己补一个"总是允许整个 shell" —— 那正是 runtime 刻意
   堵掉的东西（推不出命令前缀时它宁可不提供这个键）。
4. **`always_group` 只说"我同意放行这一组"，不许带名单。** 放行哪些工具由 runtime
   按 `id` 查它自己手里那份名字快照。让客户端指定名单就等于让客户端能改策略
   （写 `auto_approve_tools`）—— 而诚实的前端和恶意的前端在那一步没有区别。

### 4.2 `question_request` / `question_response`

```json
{"v":1,"t":"question_request","id":"q-3",
 "question":"用哪个数据库？","header":"数据库",
 "options":["PostgreSQL：沿用现有实例","SQLite：本地文件，零运维"],
 "multi_select":false}
```

回：

```json
{"v":1,"t":"question_response","id":"q-3","status":"answered","text":"1"}
```

`status` 只有 `answered` / `skipped` 两种是**你**能回的。第三种 `unavailable`
（没有人可问）由 runtime 自己产生 —— 界面断连、CI、`--autopilot` 都是这一种。

**回车 = `skipped`，不是同意。** 连续交互里最容易做的动作就是一路回车，而"回车即
同意"会把最危险的那条路改成手滑也能过。

**提问不是审批。** 拿到"用户同意了"**不会**让下一次 `shell` 调用免审 —— 答案只是
内容，不产生任何权限效果。这一条在 runtime 那一侧是硬保证（`ask_user` 的答案连
gate 都进不去），你这一侧只要别把它显示成"已授权"就行。

## 5. 取消与收摊

这里有**两条**路，它们做的是两件事 —— 混在一起过的人会撞上一个很难查的症状。

**`shutdown`（收摊）**：你该说的都说了。runtime 会**跑完当前这一轮**再退出
（不是打断它）—— 所以退出可能要等几秒。

**`interrupt`（中断这一轮）**：停下**正在跑的这一轮**，会话留着。它**不是**打断：
模型往返和工具执行都是同步的，没有天然的打断点（这一版不做流式）。所以它唯一的
作用点是**两步之间** —— `Agent.run` 每进入下一轮循环时问一次 `should_stop`。
语义是"**停止（等当前步完成）**"。

**这两条必须分开**，而且这是实测踩出来的：`shutdown` 曾经顺手置了那个取消标志，
于是"客户端发完 user_message 紧跟一条 shutdown（我该说的都说了）"会让当前这一轮
在**第一个安全点**被砍掉 —— 界面永远拿不到答案，而任何地方都不报错。

**中断之后会照常发 `event(run_finished, stop_reason=cancelled)`**，所以界面**不要
自己宣布"已停止"**：那会是第二份事实。发完 `interrupt` 就等着那条事件。

**`interrupt` 是"请求"而不是"命令"**：可能什么都没发生，而且那是**正确**的 ——
如果模型这一步直接给出了答案（没有下一次循环），那就没有下一个安全点，这一轮会
正常 `answered` 收尾。**别把这种情况显示成失败**：它没有失败，只是没地方停。
同一件事的另一面：`interrupt` 之后**下一条 `user_message` 必须能正常跑**，
所以取消标志是**每轮一份**的（`_run_turn` 开头清掉它）—— 不清的话，这个会话此后
每一轮都会在第一个安全点被砍掉，症状是"发消息没反应"。

**为什么不能杀进程**：`messages` 的一致性只在"两步之间"成立。一条带 `tool_calls`
却没有对应结果的 assistant 消息会让那个会话**此后每一轮都发不出去**（API 直接 400）。
所以强杀可能在磁盘上留下一个永久损坏的会话。

**这一版仍然没有"随时打断"**（决策：不做流式）—— 见上面那段：唯一的中断点是两步
之间，而界面上的按钮文案要对得上这句话，否则用户会以为按键没生效。

## 6. 错误与边界

| 情形 | runtime 怎么做 | 你该怎么做 |
|---|---|---|
| 配置错（缺密钥、`permissions.json` 写坏） | 往 **stderr** 说一句，**stdout 一个字节都不发**，退出码 **2** | 把那段 stderr 当成一条 notice 显示出来再退出。**不要当成崩溃** |
| 模型失败 | `event(model_call, status=error/fatal)` + `event(run_finished, stop_reason=model_error/model_fatal)` + 一条 `notice` | 显示成"这一轮失败"，**不要退出会话** —— 一个回合失败不等于整个会话结束 |
| 步数用尽 | `event(run_finished, stop_reason=max_steps)` | **必须和 `answered` 长得不一样**：不许让人分不清"答完了"和"被砍断了" |
| 你发了一行坏 JSON | 跳过、计数，循环结束时报一句到 stderr | —— |
| 你发了不认识的 `t` | 忽略、继续 | —— |
| `session_switch` 的 id 非法 / 配置坏了 | 一条 `notice(code="session")`，**旧会话原样保留**，进程不退 | 把那条 notice 显示出来，**别清屏** —— 你现在还在原来的会话里 |
| `sessions` 里某一条读不出来 | 那一条的 `preview` 变成「读不出来：…」，其余照常 | 照着显示；**别因为一行就想重来一次** —— 坏文件不会自己好 |
| 版本对不上 | 说一句到 stderr，退出 | 自己也该停：继续下去没意义 |
| 子进程自己崩了 | stdout 关闭 = 你读到 EOF | 用 `wait()` 拿退出码，报出来 |

**`stop_reason` 的全部取值**：`answered` / `max_steps` / `cancelled` /
`model_error` / `model_fatal`。**认不出的取值不许崩** —— 当作 `failed` 显示，并原样
把那个字符串打出来（那是唯一的线索）。

## 7. 客户端不许自己决定的四件事

这四条是**边界**，不是风格：

1. **放行哪些工具**（4.1 第 4 条）—— 你只回一个枚举。
2. **什么算"非默认权限"**（3.1）—— runtime 发什么你显示什么。
3. **`remember_hint` / `trust_all_hint` 那句话**（4.1 第 2 条）—— 原样显示。
4. **会话清单长什么样**（3.6）—— 别去读 `.tudouni/sessions/`，问 `session_list`。

前三条共同的理由是同一个：**它们是 runtime 的判定，客户端重做一遍就是第二份事实。**
而第二份事实漂掉的症状永远是"看起来正常，其实不一样"。第 4 条是同一个理由的另一个
方向 —— 那份目录布局是 store 的实现细节，不是协议的一部分。

## 8. 写一个新客户端要做什么

按 `protocol/client.py` 的 `ClientHooks` 实现那三个**需要回答**的回调：

```python
class MyClient:
    def on_message(self, message) -> None: ...
        # init / session_load / event / ui / notice / sessions ——
        # 不需要回答的都走这里

    def on_permission(self, request) -> str | None: ...
        # 返回 allow / deny / always / always_group；
        # **或者 None ="我的界面稍后自己回"**（那两条的区别见第 4 节）

    def on_question(self, request) -> tuple[str, str] | None: ...
        # (answered|skipped, text)；同样可以返回 None
```

**`sessions` 走 `on_message`**，不走单独的钩子：它**不需要回答** —— 那正是第 4 节
那两条和其余所有出站消息的分界线。

**那个 `None` 不是可选的便利，是必需的** —— 异步界面（TUI）没法在**读线程**上等人
点按钮：那会把读线程钉住，而它还要负责收别的消息。返回 `None` 之后，答案由界面在
用户操作完之后调 `client.answer_permission(...)` / `client.answer_question(...)` 发。

**绝不能用兜底答案代替它。** 客户端会把返回的字符串**立刻**发出去 —— 一个兜底的
`deny` 会让子进程据此拒绝并继续跑，等用户点 [允许] 时那条回应已经没人要；更糟的是
中间那次拒绝会进审计、记成 `user_denied`，也就是**伪造了一条"用户拒绝过"的记录**。

然后：

```python
client = ProtocolClient(MyClient(), session="...")
client.start()
client.user_message("你好")
client.wait()
```

**三个回调，没有别的。** 拼 `permission_response`、加版本号、flush、处理坏行 ——
都在 `ProtocolClient` 里，你不需要认识协议。

非 Python 的客户端（Web 那一侧）读 `schema/*.schema.json` 拿形状、读这份文档拿语义。
**将来的 TS 类型应当由脚本从 schema 生成**，不是手抄 —— 手抄的话连"字段名对不上"
都测不出来。

## 9. 这一版仍然**没有**的东西

写在这里免得被当成 bug：

- **流式增量**（`t:"delta"` / `t:"delta_reset"`）—— 不做流式，所以界面在模型往返
  和工具执行期间是安静的。
  **你必须自己转圈**：一次模型往返是秒级，一个完全静止的界面会被当成卡死。
  顺带说清 `interrupt` 的边界：它**停不下正在飞的那一次模型调用或工具执行**，
  只停"下一步"。
- **换会话时的"随时打断"**（3.6）—— 换会话会**等**当前这一轮跑完再换。想快点过去，
  先发 `interrupt`、等 `run_finished(cancelled)` 到了再发 `session_switch`。
- **工具卡片 / diff** —— 事件里 `arguments` 只有预览。工具结果的全文在
  `session_load.messages` 里（`role=="tool"`），自己取。
- **随时打断** —— 见第 5 节：只有两步之间那一个中断点。
- **网络监听** —— 通道**只允许父子进程管道**。`tool_call.arguments`（在审批请求里
  是全文）和 `reasoning` 都会经过它，也就是工作区内容会经过它。开一个 TCP 监听
  等于开了一条往外送数据的通道 —— 那要先回答和 `fetch_web` 定 MEDIUM 同一串问题。
