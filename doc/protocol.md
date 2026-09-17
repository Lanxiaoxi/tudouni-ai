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
│  ◄──── delta(text) … ───────────────  流式开着时才有（见 3.8）
│  ◄──── event(model_call) ───────────
│  ◄──── event(tool_call) ────────────
│  ◄──── permission_request           ← 要你回应，会一直阻塞
│  ──── permission_response ─────────►
│  ◄──── event(permission) ───────────
│  ◄──── event(tool_result) ──────────
│  ◄──── event(run_finished) ─────────
│  ◄──── ui(run_finished, answer) ────  **答案在这里**（流过的话前端不必再画）
│  （等下一句 user_message）
│
│  ──── interrupt ───────────────────►  停下**这一轮**，会话留着（见第 5 节）
│  ──── set_autopilot ───────────────►  运行中开关"不再问审批"（见 3.7）
│  ◄──── ui(state, autopilot) ────────  回执：以这条快照为准
│  ──── set_model ───────────────────►  换这个会话用哪个模型（见 3.9）
│  ◄──── notice + ui(state, model) ───  说明 + 快照：以那条快照为准
│  ──── set_thinking / set_effort ───►  思考模式那两个旋钮（见 3.11）
│  ◄──── notice + ui(state, …) ───────  同上：说明 + 快照
│  ──── status ──────────────────────►  问一次现状（见 3.10）
│  ◄──── ui(status, counters, usage) ─  那一屏的账（读一遍审计日志）
│  ──── tools ───────────────────────►  要一份工具清单（见 3.10）
│  ◄──── ui(tools) ───────────────────  每个工具 + 它会不会问你
│  ──── refresh_state ───────────────►  **空消息**：现在重算一份面板快照（见 3.10）
│  ◄──── ui(state) ───────────────────  和开场那条同形，前端只在有东西悬着时问
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

**`protocol` 现在是 `2`**（信封版本 `v` 还是 1，两者不是一回事）。2 加的是这一节后面
讲的流式那两条消息（`delta` / `delta_reset`）。**老客户端什么都不用改**：按第 2 节
那条规矩忽略不认识的 `t` 就行 —— 完整答案照旧在 `ui(run_finished).answer` 里发一份，
delta 只是让它更早出现。

**`stream` 是"这一次运行开不开流式"**（`--stream` / `--no-stream`，TUI 默认开）。
它是**运行期事实，不是你该猜的东西**：一轮里一个字都没吐的时候（模型直接调工具），
"收到过 delta 没有"这个判据是错的 —— 而它错的那一次，恰好是"要不要用 `answer`
兜底"这个决定。

### 3.2 `session_load` —— 恢复会话的画面

**原样的 session.messages**，因为你要自己决定怎么画。

代价说明白：恢复一个长会话时这一条可能几 MB（`read_file` 不分页，一个 8MB 的文件
正文就躺在里面），所以它是**一条、只发一次**，而不是每条消息一个事件。**载入时给
一个"正在载入会话…"的提示** —— 本地管道几十 MB 也就几百毫秒，但界面不能静止。

### 3.3 `event` —— 审计的原样转发

**这就是 `.tudouni/logs/<id>.jsonl` 里那一行，只是包了一层信封。** 字段一个不多、
一个不少。这一条让"审计 = 协议"在字节层面成立，而好处很实际：`--audit` 能看到的东西
你的界面都能看到，两边永远对得上。

八种 `kind`：

| kind | 你会关心的字段 |
|---|---|
| `run_started` | `user_input`（预览） |
| `model_call` | `status`（ok/error/fatal）、`attempt`、`duration_ms`、`backoff_ms`、`prompt_tokens`/`cached_tokens`/`miss_tokens`/`completion_tokens`、`tool_calls`、**`reasoning`（全文）**、`streamed`/`stream_chunks`/`streamed_chars`（流式时才有） |
| `tool_call` | `tool`、`call_id`、`tool_index`、`arguments`（**200 字符预览**） |
| `tool_result` | `tool`、`call_id`、`tool_index`、`status`、`chars`、`duration_ms`、`parallel`、工具自带字段 |
| `permission` | `tool`、`risk`、`decision`、`outcome`、`waited_ms`、`remembered`、`rule` |
| `tool_batch` | `calls`、`wall_ms`、`tools`（只有并发批次才有这条） |
| `run_finished` | `stop_reason`、`duration_ms` |
| `delta_reset` | 只有 `run_id` / `step`：**这一步的流式正文作废了**（重试、或者网关不接受 `stream_options` 时的重发）。见 3.8 |

**四件要记住的**：

1. **`tool_call.arguments` 是预览（200 字符），不是全文。** 全文只在
   `permission_request` 上（那是给人做判断的）。想渲染工具卡片得另想办法 ——
   工具结果**全文在 `session_load.messages` 的 `role=="tool"` 那条里**。
2. **`outcome` 有八种**（见 `security/gate.py` 的模块 docstring），它们事后要回答的
   问题不同：`approved` 是"这一次有人看过"，`rule_allowed` / `command_allowed` /
   `auto_allowed` 是三种"没有人在场"的放行，`autopilot` 是"这一轮没人可问"。
   别合并显示。
3. **`reasoning` 是全文**（决策：思维链进审计）。它是审计里唯一的"内容型"字段，
   所以它可能很长、也可能含模型读到的代码。**默认折叠**成一行"思考过程（N 字符）"。
   流式开着时它**不是逐块来的**（那条流在 `delta` 上），这里照旧给完整的一份 ——
   界面**只该用一条**，别把两份都画出来（见 3.8）。
4. **`delta_reset` 是审计里唯一"内容被丢掉了"的痕迹。** delta 的正文不进审计
   （一次回答上千块，`JsonlSink` 每条一次 open/write/close —— 抄进去等于把日志变成
   第二个会话文件），所以"屏幕上那段后来作废了"只有这一条能证明。而**它只在真的
   吐过东西时才发**：一次没来得及出字的失败重试不会留下它（记了就是假的）。

### 3.4 `ui` —— 只给界面的东西，七种 `kind`

它和 `t:"event"` 的分工是**一件事**：**`ui` 不进审计**。七种 `kind` 都服从这一条。

**`kind:"run_finished"` 带 `answer`**（`Agent.run` 的返回值）。

**审计里没有正文**（`Agent.run` 的返回值只交给调用方），所以这条**永远**是完整答案的
唯一权威来源。少发它，界面就一片空白。

**而流式开着时，你大概率不该再画一遍它。** 正文已经在 `t:"delta"` 里逐字到过了，
两份都画的话屏幕上是同一段回答出现两次。判据是"**这一轮真的流过正文吗**"——
也就是"累计出来的正文非空"，不是"收到过 delta 没有"（模型直接调工具那一轮一个
字都不吐，而那时候 `answer` 正是你要画的那份）。完整规则见 3.8。

它和 `event` 那条 `run_finished` 是**两条消息**，靠 `run_id` 配对。**顺序不保证**
（一个来自回合线程的收尾、一个在它之后），所以别去补偿顺序：**用 `answer` 拿正文、
用 `event` 改状态**。

**`kind:"state"` 是面板数据快照**（任务列表 / 已加载技能 / 可用技能清单 / 后台任务 /
权限范围 / 会话规模 / **当前模型与它的窗口**）。它存在的原因很具体：**这些状态住在
子进程里**（`session.metadata`、`PermissionPolicy`、还有那张攥着进程的后台任务表），
而你是另一个进程 —— 没有这条消息，"agent 现在在做哪几件事""我现在放行了什么"
"我机器上还挂着什么""它在用哪个模型"就只剩"另开一个终端跑 `--skills` / `--audit`"
这一条出口（后台任务连那个出口都没有 —— 它此前只存在于一条工具结果里）。

**什么时候发**：开场一条（紧跟 `session_load`，**这一条带 `skill_catalog`**），
以及每一条 `tool_result` 之后（`todo_write` / `load_skill` 改的正是那两块，而后台任务
会在那里被 `poll()` 一遍），回合收尾时再补一条。3.9 那条 `set_model` 处理完也补一条。
**别按工具名去挑** —— 那会让协议层认识具体工具。

**`jobs` 的 `state` 是算好的**（`running|uncollected|done|killed`），和 `risk_scope`
的 `disposition` 同一条规矩：**"什么样的组合算『结果还没收』"是 runtime 的判定**，
界面拿 `exit_code` 和别的字段自己推一遍就是第二份定义 —— 而它漂掉的样子是
**面板上少了一个警告**，那个警告恰恰是后台任务唯一会静默出错的地方（见「后台命令」）。
`uncollected`（跑完了、结果还没收）是其中**唯一需要有人动手**的一档，界面该让它显眼。
顺序也是算好的：没完的在前，组内按起的先后。

**`model` 和 `model_window` 必须一起用。** 前者是"现在真正在用的那个模型名"，
后者是它的上下文窗口（状态栏那个百分比的分母）。只跟着更新名字的话，界面会拿新模型
的用量去比旧窗口 —— 那看起来完全正常，只是数错了。**这两个字段只在 state 和 status
里有语义**（`run_finished` 那条没有它们）。

**`risk_scope` 里的处置是算好的**（`{"risk":"medium","disposition":"ask"}`），
不是让你拿 `auto_approve` 自己推：**"哪几档自动放行"是 `PermissionPolicy` 的判定**，
界面重做一遍就是第二份事实（第 7 节那条）。

**`autopilot` 是"现在这个模式开着没有"**，而且它**只有这一个来源**：界面按它显示，
**不许在自己发出 3.7 那条请求时就先把灯点亮** —— 那会让"灯亮着、其实还在逐条问你"
变成可能，而这一格的语义恰恰是"接下来还会不会问你"。用词上它是**布尔**：`true`
就是"不问"，别把它渲染成"切换按钮的当前值"再去推一次。

**`kind:"status"` / `kind:"tools"` / `kind:"mcp"` 是"回答"**：它们分别回应 3.10 和
3.12 那几条入站消息，**只在被问的时候才发**（第一个要读一遍审计日志、第二个要遍历
工具注册表并问一次策略、第三个要问一次 MCP 宿主）。和 `state` 那个"每轮都补一份"的
常驻快照不是一类东西，所以没有合成一种 `kind`。
（3.10 里那条 `refresh_state` 答的**也是 `state`** —— 它要的就是把那几条"由交互触发"
的时刻之外的那一段补上。）
两份数据都是**算好的**：`counters` / `usage` 从 `.tudouni/logs/<id>.jsonl` 数出来
（和 `--audit` 同一套口径），工具的 `disposition` 来自 `PermissionPolicy`。
**别在前端自己再算一遍** —— 那就是第 7 节那四条里的第二条。

**`tools` 和 `mcp` 不是同一件事的两半**，虽然它们都关于"有哪些工具"：**问的人不同**。
`tools` 回答"这个工具会不会问我"（策略与记忆），`mcp` 回答"哪个 server 在跑"（生命周期
与连接）。合成一条的话，每次挂一个 server 都要把几十行权限清单重发一遍。而左栏那种
"机器上挂着什么"的面板读的是 **`state` 里那一格 `mcp`**（形状和 `ui(mcp)` 的
`mcp_servers` 一样）—— 它和后台任务并排，因为它们是唯一两块**对应着活东西**的数据
（stdio 的 server 是我们起的子进程，远程的是一条活着的连接）。

**它不该混进对话流**（`status` / `tools` / `mcp` 除外）：任务列表每更新一次就往会话里
插一段，会把"你问的 + 它答的"冲稀。它属于常驻面板（左栏那种）。而那三条是
**用户按出来的**，它们该出现在用户看的那个流里。

### 3.5 `notice` —— 运行期的旁白

模型失败、协议层丢了一行、等等。`level` 是 `info` / `warn`，`code` 是机器认的类别。
**`warn` 必须比其余的更显眼**（它是"出事了"和"就是提一句"的分界）。

换会话失败也走它（见 3.6）：`code == "session"` 那条出现时，**当前会话一点都没变**，
你什么都不用收拾。

`code == "model"` 那条是换模型的结果（见 3.9）：**成功是 `info`、没换成是 `warn`**，
而文字里含"上一个是谁""什么时候生效"这两个**你拼不出来**的事实 —— 原样显示，别改写。
`code == "thinking"` / `"effort"` 是思考那两个旋钮的结果（见 3.11），同一条规矩。

**开场那批说明走 `init.notices`，不是这条消息。** 它们形状一样（`level` / `code` /
`text`），区别只在时机：`init.notices` 是"这个会话开局是什么样"，而这条是"跑着跑着
发生了什么"。**已经常驻在你面板里的那几类**（权限范围 / 已加载技能 / 任务列表 /
当前模型 / 思考模式）**不必再抄一遍进对话流** —— 那只会让开局看起来像一屏日志。

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

### 3.7 `set_autopilot` —— 运行中开关"不再问审批"

```json
你 → {"v":1,"t":"set_autopilot","on":true}
你 ← {"v":1,"t":"ui","kind":"state","autopilot":true,…}   // 回执就是那条快照
```

**`on` 是绝对状态，不是"切一下"。** 重发同一条是幂等的，所以不存在"两条消息各切
一次"的竞态；界面也**不需要先知道** runtime 现在是什么状态（那是 3.4 里 `autopilot`
那一格的活）。

**它改的是"要不要逐条问审批"，不是"有没有人在"。** 这一条是两个东西被分开之后的
结果：`--autopilot`（启动时那个）说的是"这一次没有人可问"，而运行中这个开关说的是
"有人在看着，但他选择不看每一条"。两者在审计里仍然分得开 —— 这样放行的每一次都记
`outcome=autopilot`，和 `approved`（人按过 y）不是一回事，所以"这次会话到底有没有人
看着"这个问题事后仍然答得出来。

**它管不着的东西**：工作区边界、控制面写入、拒绝名单（那些是"不许做"，不是"要不要
问"）；**提问通道也不受影响** —— `ask_user` 照旧问界面（人在键盘前，它就该问）。
**换会话不会把它丢掉**：换过去的新 runtime 照同一份 bootstrap 装，所以模式跟着走。

**回执是那条 `ui(kind:"state")` 快照，别自己乐观更新**（理由见 3.4 那段）。它改完
立刻发一条，所以你的灯不会等到下一轮才有反应。**只认真正的 `true`**：`"false"` / `1`
这类东西不会被当成开 —— 猜错的两个方向代价不对称，这一档的后果是"没有人在上面点过
头就执行了"。

### 3.8 `delta` / `delta_reset` —— 流式（`v` 不变，`init.protocol` 是 2）

```json
你 ← {"v":1,"t":"delta","session_id":"…","run_id":"…","step":2,
      "channel":"text","text":"我先看一眼","reset":false}
你 ← {"v":1,"t":"delta_reset","session_id":"…","run_id":"…","step":2}
```

**它为什么不是"`event` 里多一种 kind"。** `t:"event"` 那条流**同时进审计**
（`.tudouni/logs/<id>.jsonl` 就是那一行），而一次回答是上千块 —— `JsonlSink` 每条
事件一次 open/write/close，抄进去等于把审计日志变成第二个会话文件，"事后能完整回放"
这件事也就被稀释了。所以 delta 是一级独立的消息：**只走协议、不进审计**。
审计里记的是**汇总**（`model_call.streamed` / `stream_chunks` / `streamed_chars`）。

**`text` 是新增的那一段，不是累计正文**：累计是你的事。而 `channel` 必须按字段名
分流（`text` = 正文，`reasoning` = 思考链）—— 两条通道的内容都是字符串，猜错一次的
症状是"答案里混进了一段自言自语"，看起来像模型的问题，不像协议分错了。

**`step` 是"这是第几步的正文"。** 一个回合里可能有好几步（模型先说要调工具、拿到
结果再答），所以按步分块是对的。**它可能比你以为的多一**：`event(model_call)` 是模型
调用**结束之后**才记的账，而 delta 在那之前就到了 —— 换句话说你收到第一块 delta 时
最近一条事件还是**上一步**的。这不是 bug，别拿它去反推。

**`delta_reset` = 把这一步已经画出来的增量丢掉。** 两种时候会来：

1. 一次**重试**之前（第 1 次尝试可能已经吐了半句，第 2 次会把整段重说一遍；
   不丢的话屏幕上是两段回答首尾相接 —— 而它看起来像模型说了两遍）；
2. **适配层自己重发**之前：网关回 400 说它不认 `stream_options`（那是要 token 用量
   必须带的参数），runtime 会丢掉它重发一次。

四条要记住的：

1. **按 `(run_id, step)` 成对判定，两个都要对上。** 只按 `run_id` 清会把前几步
   已经定下来的内容（"我看看文件"那句）也抹掉 —— 那几句不在重试范围内，
   抹掉就是在伪造历史：屏幕上少了内容，而 `session_load` 里还在；
2. **清空是整块拿掉，不是"标灰"**。半截正文**从不进历史**（assistant 消息要等
   模型调用返回才 append），所以留着它就是留一个"恢复会话时查无此物"的东西；
3. **中途取消（`interrupt`）之后屏幕上那半句也会留着** —— 那是同一个取舍的另一面：
   我们选择"历史里只有完整的回答"，代价是屏幕和历史会短暂对不上。事件里
   `run_finished(stop_reason=cancelled)` 照常到，界面**按它收尾**（别自己宣布
   "已停止"，那是第二份事实）；
4. **重复的 `delta_reset` 是无害的**（一次重试会经过两个层），但**漏掉一条就难看了**。
   所以宁可多发。

**`answer` 和累计正文的关系**（3.4 那条的展开）：`ui(run_finished)` 里的 `answer`
**照旧发**，一个字节都不少 —— 它是协议 v1 就有的东西。而流式开着时前端要自己判断
"这一轮流过没有"：**流过就不画**（屏幕上已经有了），**没流过就画**（模型直接调工具、
`--no-stream`、或者网关不支持流式）。判据是"累计出来的正文非空"，不是
"收到过 delta 没有"。

### 3.9 `set_model` —— 换这个会话用哪个模型

```json
你 → {"v":1,"t":"set_model","model":"deepseek-v4-pro"}
你 ← {"v":1,"t":"notice","level":"info","code":"model",
      "text":"[模型] 换成 deepseek-v4-pro（上一个：deepseek-flash）—— 下一次请求生效。"}
你 ← {"v":1,"t":"ui","kind":"state","model":"deepseek-v4-pro","model_window":1000000,…}
```

**模型名必须是 `init.model_catalog` 里列出的那一个**（精确匹配，不做模糊）。不在目录里
的会被拒绝：`level:"warn"` 的那条 notice 会说明原因，而**什么都没变**。

**为什么由 runtime 校验、而不是前端自己按清单挑。** 清单是从 runtime 那份目录渲染的，
但"认不认识"这个判断只能有一处 —— 前端拿到什么就发什么，所以一个手写的客户端可以发
任何字符串。接受一个目录外的名字等于让那份清单变成一句谎话，而"选了一个它没列出来的
模型"这件事没有任何地方会报。

**它不回一条"成功"消息，回的是两份东西**：

1. 一条 `ui(state)` 快照 —— **界面按它显示，不许在自己发请求的时候就改**（同 3.7）。
   这里的证据尤其重要：换模型只改一个字符串，没有任何别的地方会报出"其实没换成"；
2. 一条 `notice`（`code:"model"`）—— 里面那句人话**含两个前端拼不出的事实**：
   "上一个是谁"和"什么时候生效"。原样显示。

**一轮正跑着的时候也可以发，而且本轮不受影响。** 那个回合的请求已经发出去了，改变的
是"下一个请求用谁"。这是刻意的：同一个回合里前后两步由两个模型生成的话，事后完全看
不出来（审计里两条 `model_call` 长得一样），而历史里那段话是谁写的就没有答案了。
所以那条"模型换了"的说明**留到下一轮开头**，作为一条 `role:"user"` 的消息进历史 ——
它说的是"上面那些轮次由 X 生成，从这个点开始用 Y"。

代价说明白：**notice 在本地就已经到了，而实际生效在下一轮**。所以那句话写的是
"下一次请求生效"，别把它渲染成"已生效"。

**持久性**：这个选择住在 `session.metadata` 里，跟着会话走。所以

* 恢复会话时还是它（`init.model` 报的就是那个，`init.notices` 里也有一条 `code:"model"`
  说明"这个会话选的是哪个"）；
* **新会话回到 `.env` 里那个** —— 换模型是一次会话级的选择，不是改全局默认。
  想改默认就改 `DEEPSEEK_MODEL`。

### 3.10 `status` / `tools` / `refresh_state` —— 问一次现状
```json
你 → {"v":1,"t":"status"}
你 ← {"v":1,"t":"ui","kind":"status",
      "status":{"session":{…},"model":{…},"counters":{…},"usage":{…},"meta":{…}},
      "last_prompt_tokens":203500,"context_tokens":1000000}

你 → {"v":1,"t":"tools"}
你 ← {"v":1,"t":"ui","kind":"tools",
      "tools":[{"name":"shell","risk":"high","disposition":"ask",…}],
      "granted_prefixes":["git add"]}

你 → {"v":1,"t":"refresh_state"}
你 ← {"v":1,"t":"ui","kind":"state", …}      // 和开场那条同形（除了 skill_catalog）
```

**三条都是只读的，而且都不打断当前这一轮。** `/status` 最有用的时刻恰恰是"它跑着，
我想看一眼花了多少" —— 所以它不 join 那一轮。代价是那一屏里的数字是**发快照那一刻**
的真值（这一轮的答案还没落进历史时，`session.messages` 就少一条）。这不是 bug，
是"不打断也不等待"的必然结果。

**`refresh_state` 是唯一"没有用户动作"的一条**，所以它有个额外纪律：**只在真有理由
的时候发**。它补的洞很具体 —— `ui(state)` 原本只在几条由交互触发的时刻发（开场、
每条 `tool_result`、回合收尾、几条命令之后），而**后台任务会在没人在看的时候改变
状态**：一段安静时间里一条两分钟的命令跑完了，面板上却还写着"在跑"，那句话是假的。
所以前端在自己知道有东西悬着时（TUI：`state.jobs` 里有 `running` / `uncollected`）
**按节流问一次**（TUI 是 2 秒），而不是让 runtime 挂一个定时器或者监视线程 ——
后台任务那套东西刻意做到"零后台线程"。**别把它变成心跳**：这条消息存在的理由只有
"安静的时候也有人问一句"，没有理由的轮询会把这个理由本身稀释掉。

**`status` 分成四组是有意的**：这一屏跨四个层次（会话 / 模型 / 账 / 这次运行的环境），
平铺成一个大字典之后字段名早晚撞车（"工具清单"和"工具个数"就是一对）。

| 组 | 回答什么 | 要注意的 |
|---|---|---|
| `session` | 我在哪个会话（id / 是否恢复 / 工作区 / 消息数与步数） | **`messages` / `steps` 是发快照那一刻的值** |
| `model` | 它在用哪个（`current` / `selected` / `last_used` / `window` / `base_url`） | `selected ≠ current` 只在"刚换完、下一次请求还没发"时出现（见 3.9） |
| `counters` | 跑了多少活（`runs` / `model_calls` / `model_ok` / `tool_calls` / `permission_waits` / `asks`） | 数的是 `run_started` 和**每一条**调用，不是"成功了几次" |
| `usage` | 花了多少（`prompt` / `cached` / `miss` / `completion`） | **只累加成功的调用**（失败的尝试没有用量字段） |
| `meta` | 这次运行的环境（步数上限 / 流式 / autopilot / 工具个数 / 审计路径 / 非默认权限） | `tool_count` 只是个**数**；清单在 `kind:"tools"` 里 |

**`last_prompt_tokens` 和 `usage.prompt` 是两个不同的数**，别混：前者是**最近一次**
请求实际发出去多少（状态栏那个占比的分子），后者是**整个会话**的累计（那是钱）。
两者都是 `null` / `0` 时说明"还没有成功调用过模型" —— 别把它渲染成"0 token 花掉了"。

**`tools` 里那一列 `disposition` 是策略的裁定**（`auto` 不问你 / `ask` 会问你 /
`deny` 问都不问）。**每一把工具都要列出来**，包括被拒绝的 —— "这个工具存在吗"和
"它会不会问我"是两个问题，而这条命令要一次回答两个。`granted` 是"你按过 `t`"，
`command` 说明这个工具的参数里有没有命令行（只有 `shell` 有），而 `external` 是 MCP
来的那些（它们风险一律 `high`、每次都要你按键）。

### 3.11 `set_thinking` / `set_effort` —— 思考模式那两个旋钮

```json
你 → {"v":1,"t":"set_thinking","on":false}
你 ← {"v":1,"t":"notice","level":"info","code":"thinking",
      "text":"[思考] 思考模式：关（强度记着，/thinking on 回来还是它）—— 下一次请求生效。"}
你 ← {"v":1,"t":"ui","kind":"state","thinking":false,"effort":"high",…}

你 → {"v":1,"t":"set_effort","effort":"max"}
你 ← {"v":1,"t":"ui","kind":"state","thinking":false,"effort":"max",…}
```

**它们是两个问题，所以是两条消息。** `thinking` 问"要不要先想一段"，`effort` 问"想的时候
花多大力气"。合成一条之后"只改其中一个"就得靠"没传的那个字段表示不动它"来表达 ——
那是一种每个客户端都要记住的约定，而漏记的症状是**悄悄把另一个也重置了**。

**`on` 是布尔，不是 `"on"` / `"off"` 两个字。** 认哪些词算开是**前端**的事（CLI 那边
在本地折算成这里要的布尔），协议上只有这一个形状 —— 否则"哪些写法算开"会有两份词表，
而它们漂开之后 `/thinking 开` 在一个界面里管用、在另一个里报错。

**`effort` 只认 `low` / `high` / `max`**（端点还接受 `minimal`/`medium`/`xhigh`/`ultra`，
但折算之后分别是这三档，所以不列出来 —— 它们的效果和价钱都一样）。**`"none"` 不是一档
强度**：它是"关掉思考"，走 `set_thinking`。认不出来的值回一条 `notice`，**什么都不改**。

**思考关着时 `set_effort` 照样接受**（记下来，等下次打开用）：拒绝的话，用户就得先记住
"要先把思考打开才能设强度" —— 而那是我们发明的顺序，不是任何地方要求的。所以 `ui(state)`
里 **`effort` 关着时也有值**，界面别把它渲染成"现在生效的强度"。

**回执同样是"状态快照 + 一句说明"**，和 3.9 一字不差：界面按快照显示（不许乐观更新）、
说明原样显示（它含"关掉不清强度"这类前端拼不出的事实）。生效时序也一样 ——
**下一个请求**。

`init` 也带这两个值（`thinking` / `effort`）加**可选档位**（`effort_levels`）：那一格
可能来自这个会话上次的选择，而档位清单**随协议发而不是让前端写死** —— 前端不许 import
内核（见第 7 节），抄一份清单到前端就会漂，漂掉的症状是"清单里列着它，打进去说没有这
一档"。

### 3.12 `mcp` —— 看/改 MCP server 的挂载

```json
你 → {"v":1,"t":"mcp","action":"list"}                       // 看
你 ← {"v":1,"t":"ui","kind":"mcp",
      "mcp_servers":[{"name":"github","state":"loaded","tools":12,"error":"",
                      "where":"npx -y @modelcontextprotocol/server-github"},
                     {"name":"kb","state":"unload","tools":0,"error":"",
                      "where":"https://kb.example.com"},
                     {"name":"broken","state":"failed","tools":0,
                      "error":"McpError: 起不来 npx","where":"npx -y broken"}],
      "mcp_notes":["[MCP] 当前挂载情况（配置里改了要重启才生效）"]}
你 ← {"v":1,"t":"ui","kind":"state", …, "mcp":[…同上…]}      // 顺带补一份面板快照

你 → {"v":1,"t":"mcp","action":"load","servers":["github"]}   // 挂一个
你 ← {"v":1,"t":"ui","kind":"mcp", …, "mcp_notes":["[MCP] server `github` 挂上了：12 个工具（…）"]}

你 → {"v":1,"t":"mcp","action":"unload","servers":["github"]} // 卸一个
```

**三个动作共用一条消息**，因为它们的回包完全相同（一份**全量**清单）：`list` 只看、
`load` 连上并列工具、`unload` 摘掉工具并断开。

**不做 `all` 这种批量写法**：一次一个 —— 它和面板里按一次开关是同一件事，而批量会把
"哪几个成了、哪几个没成"揉成一句话，而那句话正是用户要看的。`servers` 里的名字必须以
配置里那份为准（runtime 按名字找；找不到就回一句 notice + 清单，让用户看着清单重打）。

**`state` 是三个值，不是一个布尔**：`loaded`（在跑）、`unload`（配置里有、这会儿没跑）、
`failed`（试过，没成 —— 原因在 `error` 里）。**后两个不能合成一个**："我没开它"和
"我开了、它坏了"的下一步完全不同。`tools` 在非 `loaded` 时是 `0`（显示一个残留的数会被
读成"它还在跑"）。

**回包是全量而不是增量**：面板每次按它重画，省掉"增量怎么合并"那一整类 bug。所以
前端**不用**维护任何本地状态 —— 拿 `mcp_servers` 覆盖自己那一份就对了。`ui(mcp)` 后面
紧跟的那条 `ui(state)` 带着同一份（字段名是 `mcp`），左栏那块读的就是它。

**回执里那几句话由 runtime 拼**（`mcp_notes`，已带 `[MCP]` 前缀），前端**原样贴**：
"为什么没成"只有它知道。界面自己拼一句"已挂载"就是第二份事实 —— 而这一格尤其要紧：
`npx` 起不来时那句话会变成假话。

**两条纪律，都在 runtime 那一侧**：

  * **它是唯一能改"模型看得到什么工具"的入站消息**，所以**只由人按键触发**。前端不许
    在启动、回合结束、或者收到什么消息时自己发一条 —— 那会让"配置自己变宽"成为可能，
    而 `mcp.json` 只读用户级要防的正是这件事。**启动时一个都不自动挂**：配了不等于挂上，
    挂载要人明确按一下（本地那一档是"同意这段代码用你的权限跑起来"，远程那一档是
    "把工作区里的数据发给它"，两者都发生在任何审批之前）；
  * **生效时机是"当前这一轮跑完之后"**（和 3.6 的 `session_switch` 同一条语义）：工具
    定义在 `Agent.run` 开头取一次快照，半路把一个 server 摘掉会让正在跑的那次调用以
    `McpServerDown` 收场。所以 `load` / `unload` 会等一轮跑完 —— `list` 不等（它什么都不改）。

**`where` 是给人看的那句话，远程只到 `scheme://host`**：URL 里可能有令牌，而这一格会进
面板、进快照、进日志。凭据真正出现的地方只有配置里的 `headers`，而它**不进协议**。

### 3.13 `context` / `compact` —— 看上下文，或者压一次历史

```json
你 → {"v":1,"t":"context"}
你 ← {"v":1,"t":"ui","kind":"context",
      "context":{"active":true,"folded":24,"messages":61,"summary_id":"art_9f2c…",
                 "generation":2,"updated_at":1770000000.0,"summary_chars":1800,
                 "window":200000,
                 "context":{"artifacts":31,"items":18,"open":18,"removed":0,
                            "pinned":3,"estimated_tokens":151000,
                            "limit_tokens":176331,"compact_threshold":158697,
                            "degraded":0,"version":12}}}

你 → {"v":1,"t":"compact"}
你 ← {"v":1,"t":"ui","kind":"compacted",
      "compaction":{"status":"compacted","folded":24,"total_folded":24,
                    "summary_id":"art_9f2c…","summary_chars":1800,"generation":1,
                    "before":204000,"after":151000,"duration_ms":8400},
      "context":{…同上…}}
你 ← {"v":1,"t":"ui","kind":"state", …}      // 顺带补一份面板快照（消息数变了）
```

**`context` 是只读的，也不 join 当前这一轮**（和 3.10 那三条同一条规矩）。它回答的是
"模型现在看得见多少东西、有多少被预算压掉了、历史压到哪了"。`compact_threshold` 是
历史压缩那条线（预算上限的 90%）—— 到了它，runtime 会在**回合的每一步之前**自动压一次。

**`compact` 是一个动作，不是一句话。** 它不产生任何用户消息，却会调一次模型写摘要、
把一段历史折起来、改会话的持久化状态。所以：

  * **它先等当前这一轮跑完。** 折叠点必须落在一条 `user` 消息之前 —— 从中间切开会留下
    一条带 `tool_calls` 却没有配对结果的 assistant 消息，那种历史此后每一轮都发不出去
    （400，而那个错误看起来像"上下文太长"）；
  * **它跑在另一条线程上**，答复是异步来的（`ui` / `kind=compacted`）。摘要是一次真实的
    模型往返，几秒到几十秒；在那条线程上等它，界面这几十秒什么都发不出去；
  * **它不改任何一条历史消息。** 摘要是一份 `type=summary` 的 Artifact，而原始历史在磁盘
    上**一条都不动**（`--history` 照旧读得到全文）。前端因此**不要**去改自己那份
    `session_load` 的画面 —— 该刷新的是 `context` 和 `state` 那两条。

**压缩失败和"没有可折的区间"都是 `status:"nothing"`**，两者对用户的处置完全一样，而分开
报会让前端多出两条走了也白走的路。`busy` 是"上一次压缩还在跑"（连着按两下），
`no_context` 是"这个 runtime 没装配 Context"。**这三种都不是错误**，一条 notice 都不发。

**`folded` / `summary_id` / `generation` 住在会话里，`estimated_tokens` 那一组住在 Context
里** —— 合并发生在 runtime 那一侧（`Runtime.compaction`），前端拿到的是可以直接渲染的一份。

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

**`interrupt`（中断这一轮）**：停下**正在跑的这一轮**，会话留着。它**不是**打断
工具执行：那个是同步 handler，没有天然的打断点。作用点有**两个**：

  * **两步之间** —— `Agent.run` 每进入下一轮循环时问一次 `should_stop`；
  * **流式收到下一块之前**（只在 `init.stream` 为真时存在）—— 也就是"模型正在逐字
    回答时按 Esc，它立刻就停"。这一条是流式还掉的一笔债：没有它，一次正在生成的
    回答要等模型说完（几秒到几十秒）才停得下来。

所以界面上的文案分两种：流式关着时说"**停止（等当前步完成）**"，
开着时说"**停止（立刻）**"—— 后者仍然可能因为"这一步还没开始出字"而退化成前者。

**被中断的那半截正文不进历史**（见 3.8 第 3 条），这是认下的取舍：历史里只会留下
完整的回答，代价是屏幕和 `session_load` 会短暂对不上。

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

## 7. 客户端不许自己决定的七件事

这七条是**边界**，不是风格：

1. **放行哪些工具**（4.1 第 4 条）—— 你只回一个枚举。
2. **什么算"非默认权限"**（3.1）—— runtime 发什么你显示什么。
3. **`remember_hint` / `trust_all_hint` 那句话**（4.1 第 2 条）—— 原样显示。
4. **会话清单长什么样**（3.6）—— 别去读 `.tudouni/sessions/`，问 `session_list`。
5. **工具会不会问你**（3.10）—— `disposition` 是策略的裁定；别自己拿
   `auto_approve` 推一遍（那正是第 2 条的另一面，只是换到了工具清单上）。
6. **`/status` 里那几个数**（3.10）—— 累计 token、轮次、上下文用量都从审计日志数出来。
   前端去读 `.tudouni/logs/` 会让"日志怎么存"变成前端也认识的一件事实（和第四条同一个
   理由），而且它一定会和 `--audit` 报的数漂开。
7. **有哪些档强度、哪个模型在哪条路由上**（3.11 / 3.9）—— 两者都随协议发
   （`effort_levels` / `model_catalog`）。抄一份到自己这边就会漂，而漂掉的症状是
   "清单里列着它，打进去说没有这一档"。

前三条共同的理由是同一个：**它们是 runtime 的判定，客户端重做一遍就是第二份事实。**
而第二份事实漂掉的症状永远是"看起来正常，其实不一样"。第 4、6、7 条是同一个理由的另一个
方向 —— 那些目录布局、日志位置和清单内容是 runtime 的实现细节，不是协议的一部分。

**唯一"客户端自己算"的东西是排版**：把 `/status` 那份数据排成几行、`1.0M` 还是
`1000000`、哪一列用什么颜色 —— 那些是界面的自由（`state/status.py` 给了两个共用的
格式化函数，但用不用随你）。**以及把用户写的词折算成协议要的值**（`/thinking 开` →
`{"on": true}`）：那是"你这个界面收哪些写法"的问题，而它本来就该由界面自己定 ——
但**折算的目标值**（这里是布尔）必须照协议来。

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
那两条和其余所有出站消息的分界线。**`status` / `tools` / `mcp` 也走 `on_message`**：它们
虽然是被问出来的，但等你回的是一份数据，不是"一个要带回子进程的决定"。

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

- **工具执行期间仍然是安静的。** 流式只覆盖模型往返那一段（`t:"delta"`），
  工具执行（`tool_call` → `tool_result`）之间没有任何增量可给。
  所以**你仍然必须自己转圈**：一次 `read_file` + `shell` 的组合可以是好几秒，
  一个完全静止的界面会被当成卡死。
  顺带说清 `interrupt` 的边界：它**停不下正在跑的工具**（同步 handler，没有天然的
  打断点），模型那一段在流式开着时停得下来了（见第 5 节）。
- **思考链的"直播"只有 `delta(reasoning)` 一条路**，而它在界面上最自然的形态仍然是
  "事后折叠"。`event(model_call).reasoning` 给的是**完整的一份**，但那是模型**想完
  之后**才到的 —— 别把两份都画出来（3.3 第 3 条）。
- **换会话时的"随时打断"**（3.6）—— 换会话会**等**当前这一轮跑完再换。想快点过去，
  先发 `interrupt`、等 `run_finished(cancelled)` 到了再发 `session_switch`。
- **工具卡片 / diff** —— 事件里 `arguments` 只有预览。工具结果的全文在
  `session_load.messages` 里（`role=="tool"`），自己取。
- **网络监听** —— 通道**只允许父子进程管道**。`tool_call.arguments`（在审批请求里
  是全文）、`reasoning`、以及 `delta` 的正文字节都会经过它，也就是工作区内容会经过它。
  开一个 TCP 监听等于开了一条往外送数据的通道 —— 那要先回答和 `fetch_web` 定 MEDIUM
  同一串问题。
