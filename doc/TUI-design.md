# TUI 设计（结合当前实现）

> 对象是 `doc/TUI.md` 那份初步思路。它的**方向是对的**，但它是在不知道这个仓库
> 长什么样的时候写的，所以有几处结论和现有代码**直接冲突** —— 按它做会撞墙。
>
> 这一份文档做三件事：
>
> 1. 逐条列出"初步思路 vs 现有实现"的差异，**每条都指到具体文件和函数**；
> 2. 指出实现里**已经为 TUI 准备好的部分**（比初步思路以为的多）；
> 3. 给出结合现有实现的方案、协议定义、阶段划分，以及每一处的代价。
>
> 阅读前请先看 README 的「架构」和「贯穿全局的三个设计原则」两节 —— 那份文档是
> 这个项目的设计宪法，本文的每一处取舍都尽量和它保持一致，不一致的地方会写明
> 为什么必须不一致。

---

## 零、已经拍板的决策（本文的约束）

下面十条是**人做的决定**，不是本文的建议。后面每一节都必须服从它们；
被否决的做法会连同否决理由一起留档 —— 它们以后可能因为别的原因重新成立，
那时需要看到当初为什么没做。

| # | 决策 | 影响 |
|---|---|---|
| 1 | **不做流式**（第一版） | 模型层不动：`models/` 三件套、`agents/retry.py` **一行不改**。回答整段出现。<br>**已经补上了**（决策 1 保留原样，实现走的就是本文预留的插入点：`complete(on_delta=…)`、适配层 `stream=True` 分支、`agents/retry.py` 透传）。落地形态与本文设想的三处差异见 `doc/protocol.md` 第 3.8 节：**delta 不进审计**（只走协议，审计里记汇总）、**消息种类是 `t:"delta"` + `t:"delta_reset"` 而不是 event 里多一种 kind**、**协议版本是 `init.protocol=2`** |
| 2 | **不新造协议** | 事件流**就是** `audit/events.py` + `Agent._emit` 那一套，逐字节复用 |
| 3 | **v1 不渲染工具卡片**（不做 diff / 不做工具结果卡片） | **不需要第二条事件通道**、不需要 `_emit_ui`、不需要 `*_full` 字段 |
| 4 | **给 `tool_call` / `tool_result` 加 `call_id`** | 现有审计事件加一个字段（唯一的协议改动） |
| 5 | **思维链写进审计** | `model_call` 加一个 `reasoning` 字段，见 D2；`--debug` 那份同时删掉 |
| 6 | stdin 三个读取点必须替换（见下面的澄清） | D3：`ProtocolAsker` / `ProtocolQuestioner` / 协议循环 |
| 7 | stdout 那些说明文字改为结构化字段（见下面的澄清） | D4：7 处 print 改道 |
| 8 | **不重做 Session** | `state/store.py` 与 `audit/jsonl.py` 原样复用 |
| 9 | 目录与依赖按第十节的三层结构 | 前端层不许 import runtime（决策 18 把它收紧了） |
| 10 | 依赖图照第十节的完整版（含 `tools → skills`） | 不改动现有依赖方向 |
| 11 | **两个进程，保留协议** —— TUI 只写"Python 客户端"这一半 | 协议、`init`、`session_load`、`notice`、版本号全部照第六节实现；**runtime 侧一个字段都不用为"客户端换语言"而改** |
| 12 | **TUI 用 Python + Textual** | TUI 落到 `agent_runtime/frontends/tui/`（**和 runtime 同一个仓库、同一个 venv**），由 `main.py --tui` 拉起；它是唯一依赖 textual 的包 |
| 13 | **协议抽成 `doc/protocol.md`** | 第一期结束时落地，第六节是它的草稿；它同时是第二期客户端的施工图 |
| 14 | **权限范围进状态栏，只显示非默认项** | `init.permissions` 与默认值比对后再显示 —— 默认（只有 `low`）时那一行是空的 |
| 15 | **`/` 命令集 v1 = `/new` `/resume` `/list` `/audit` `/exit`** | **不给 `/autopilot`**：它是"一次没有人可问"，在有人看着的界面里语义矛盾 —— **这一半在决策 25 里被推翻了**（那句话的前提可以拆成两个问题） |
| 16 | **`t` 用三个按钮 + 一行后果说明** | `[允许] [拒绝] [总是允许]`；后果那句话从 `asker.py:95` 的 `_remember_hint` 传过来，UI 不许自己写 |
| 17 | **`reasoning` 默认折叠** | 一行"思考过程（N 字符）"，点开看全文 |
| 18 | **前端不许 import runtime 内部** | 三层结构的核心不变式（见第十节）；**这是能长出 Web 前端的前提**，比原来那条"ui 不许被内核 import"更严 |
| 19 | **v1 CLI 保留直连 runtime，但冻结** | 现有 `cli.py` 与那 30 个测试文件不动；**但不许往直连那条路上加新功能**，否则它永远不会消失 |
| 20 | **四个不需要模型的子命令先归 CLI** | `--list` / `--skills` / `--audit` / `--history` 留在 `frontends/cli/`；它们只读 store/logs/技能目录、不装配 Runtime。**Web 期要重新想**（会话列表在 Web 上应该是协议里的一个请求，不是"前端自己去读那个目录"） |
| 21 | **协议形状的权威在 `protocol/schema/*.json`，TS 类型由脚本生成** | Python 从 schema 读、TS 从 schema 生成、`doc/protocol.md` **只讲语义**（时机/顺序/错误处理），不再抄一遍字段表 —— 否则 Web 期会出现第三个来源 |
| 22 | **`Runtime` 只接受 channels、不创建** | `ProtocolServer` 同时实现 `Channels` 并持有 `Runtime`；`attach` 之前不可能收到请求（第十节那条不变式） |
| 23 | **子进程用绝对路径 + `cwd=仓库根` 拉起，不用 `python -m`** | `package = false` 下 `-m agent_runtime.main` **不工作**（`main.py` 的 `sys.path` 补丁在 `-m` 下失效），见 6.1 |
| 24 | **（第二期）换会话在原地做，不重启进程** | 协议加 `session_switch` / `session_list`，runtime 重新装配自己再重发开场三连。它**推翻 9.2 那条"能做但不该在第一版做"**，见 9.2 与 13.3 |
| 25 | **（第三期）`/autopilot` 进命令集，状态栏常驻那一格** | 它**推翻 15 里"不给 `/autopilot`"那一半**：`--autopilot` 说的是"没有人可问"，而这个开关说的是"有人看着、但选择不看每一条"—— 前提被拆开之后就不矛盾了。协议加 `set_autopilot`，回执是 `ui(state)` 快照（界面不许乐观更新），见 13.3.2 |
| 26 | **（第三期）任务列表一出现就自动顶开上下文栏，窄屏也开** | 它**推翻决策 1 的两个附加条件**（"≥120 列才展开"和"手动按过 Ctrl+B 就不再自动开合"）。判据是**边沿**：任务列表**从无到有**顶开一次，之后听用户的 —— 按电平做的话，用户在有任务时按 `Ctrl+B` 收起会被下一次刷新（50ms 后）顶回来，那个键就等于坏了。代价是窄屏上栏吃掉 32 列（80 列终端里会话流只剩 ~46 列），嫌挤就 `Ctrl+B`，收起后仍有一行摘要 |

**决策 1 + 3 的合并效果，值得单独说清楚**：它把本文原先的七个缺口砍到四个。
原先排在最前面的两个（"模型层要能出流"、"UI 事件通道要带全文"）**整个消失了** ——
所以这份设计的规模比第一版小得多，而且**`models/` 和 `agents/retry.py` 完全不用碰**。

**决策 11 的关键点是"哪些东西没变"**：选了两个进程之后，第六节的协议设计
（含 `t:"event"` 原样转发审计、`t:"ui"` 只带 `answer`、`permission_request`
阻塞等回应）**一个字都不用改** —— 变的只是"谁来读这些 JSON"。
从 TypeScript/Ink 换成 Python/Textual，对 runtime 而言是**完全不可见**的。
这正是第五节那条"协议是一道真的边界"的兑现方式。

**决策 12 有一个不太明显的收益**：Textual 那个进程自己管着终端，所以
**冲突 10（cp936 编码）在 TUI 这一侧被消掉了** —— 框线字符由 Textual 自己
按 UTF-8 输出，不需要我们在 Windows 上做任何特殊处理。代价是它进了
`pyproject.toml`（Textual 带一串传递依赖，见第五节）。

---

## 一、先说结论

| 项 | 初步思路（`doc/TUI.md`） | 现有实现要求 | 判定 |
|---|---|---|---|
| Agent 内核语言 | Python（保留） | Python，**同步阻塞**，无 async | 一致 |
| TUI 技术栈 | React + Ink | 无约束 | **改用 Python + Textual（决策 12）**，见第五节 |
| 进程模型 | Python + Node 两进程，stdio 传 JSONL | 现有 CLI 占着终端 stdin/stdout | **两进程、协议不变（决策 11）**；只是两侧都是 Python |
| 事件协议 | **新设计**一套 `AgentEvent` | **已经存在**：`audit/events.py` + `Agent._emit` | **改设计：不要新造一套**（决策 2） |
| 事件种类 | message/tool/thinking/error/agent 五组 | 已有 7 种 kind，形状不同 | 见第四节缺口 |
| **流式** | `async for event in agent.run()` | `ChatModel.complete()` 同步一次性返回 | **v1 不做（决策 1）**；模型层不动，回答整段出现。**后来补上了**（见决策 1 那一行下面的说明），走的正是本文预留的插入点 |
| 权限 | `PermissionPolicy.check()` | `security/gate.py` 已经是这条路 | 一致，接线即可 |
| Session | JSONL（`~/.myagent/sessions/`） | JSON 状态文件 + JSONL 审计，都在 `.tudouni/` | **改设计：复用现有两层**（决策 8） |
| 状态机 | UI 内部定义 | 可从事件流**完全推导** | 一致，但要写成推导表 |
| 审批在 TUI 里做 | 是 | `asker` 已经是注入点 | 一致，但必须换掉 `input()` |
| **工具卡片 / diff** | 第六、七节整整两节在设计它 | 现有事件只带 200 字符预览 | **v1 不做（决策 3）**；见第四节开头那段为什么"不做也没有损失" |
| Session 格式 | 从零设计 | 有版本号、原子写、控制面保护 | **不要重做**（决策 8） |

一句话：**初步思路里"该新造"的东西（事件协议、session 格式、权限策略）现在都已经
有了；它着墨最多的两件事（流式、工具卡片）按决策 1 和 3 都不做；剩下真正要动的是
四条接线和小改动（`call_id`、审计里的思维链、三条人机通道、协议循环）。**

**它对"TUI 用什么写"的判断（React + Ink）也不采纳** —— 但那条判断换掉之后，
**runtime 侧一个字都不用改**（决策 11）。这就是把协议放在两个进程之间的回报：
技术栈的选择被关在客户端那一半里。

---

## 二、初步思路与现有实现的冲突清单

这一节是本文最有用的部分。**每一条都是"照着 `doc/TUI.md` 做会踩到的东西"**，
按危害从大到小排。

### 冲突 1：Agent 不是异步的，`async for event in agent.run()` 现在写不出来

`doc/TUI.md` 第四节的核心建议是：

```python
async for event in agent.run():
    yield event
```

**这个形状和现有代码不相容，而且不是"加个 async 关键字"能补的：**

- `agents/agent.py` 的 `Agent.run()` 是一个同步的 `for step in range(max_steps)`
  循环（`agent.py:465`），中间调 `self._complete_with_retry(...)`（`agent.py:473`），
  而真正落到模型上的那一句在 `agents/retry.py:71`（`model.complete(...)`）；
- `models/base.py` 的 `ChatModel.complete()` 是同步、非流式的，一次返回完整
  `ModelResponse`；
- `models/openai_compatible.py:118` 调的是 `chat.completions.create(...)`，
  **没有 `stream=True`**，所以模型层现在**根本产生不出 delta**；
- `agents/retry.py` 的 `call_with_retry` 是同步函数，`sleep` 是注入进来的
  `time.sleep`。

所以"Event Protocol"这件事，**瓶颈不在协议的形状上，而在模型层拿不到流**。
把 `complete()` 改成 `async def` 会连带把 retry、Agent、CLI 全部改成 async —— 那是
一次大改，而且**没有必要**（见第五节方案）。

### 冲突 2：事件协议已经存在，而且比初步思路设计的更完整

初步思路第四节给出的 `{type, data}` 形状：

```json
{"type": "message_delta", "data": {"text": "我先检查"}}
{"type": "tool_start", "data": {"name": "bash", "input": {...}}}
```

**这个项目里已经有一套等价物，而且已经在生产使用了**：

```python
# agents/agent.py:215
def _emit(self, kind, session, run_id, step, **data) -> None: ...

# audit/events.py:16
def event(kind, *, session_id, run_id, step, **data) -> dict:
    return {"ts": ..., "kind": kind, "session_id": ..., "run_id": ..., "step": step, **data}
```

现有的事件种类（全部在 `audit/events.py` / `agents/agent.py` 里，这是**全部**）：

| kind | 发出位置 | 关键字段 |
|---|---|---|
| `run_started` | `agent.py:456` | `user_input`（预览） |
| `model_call` | `agent.py:391` | `status`、`attempt`、`duration_ms`、`backoff_ms`、`tool_calls`、`prompt_tokens`/`cached_tokens`/`miss_tokens`/`completion_tokens` |
| `tool_call` | `agent.py:603` | `tool`、`arguments`（预览） |
| `tool_result` | `agent.py:614` | `tool`、`status`、`chars`、`duration_ms`、工具自带 audit 字段、`parallel` |
| `permission` | `agent.py:835` | `tool`、`risk`、`decision`、`outcome`、`arguments`、`waited_ms`、`remembered`、`rule` |
| `tool_batch` | `agent.py:690` | `calls`、`wall_ms`、`tools`（只在并发批次发） |
| `run_finished` | `agent.py:255` | `stop_reason`（`answered`/`max_steps`/`model_error`/`model_fatal`）、`duration_ms` |

这张表里**已经有 `run_id` 和 `step`**（`event()` 强制加），所以 TUI 天然可以按
"回合 / 第几步"分组渲染 —— 这是初步思路里没想到、但白捡的一条。

**结论：不要新造协议。** 那会立刻产生两份事实（一份给审计、一份给 UI），而
README「第 3 条设计原则」明确反对这件事，后果是"某个字段只在一边被更新"。

### 冲突 3：现有事件**故意只带预览**，不足以渲染 Tool UI

初步思路第六、七节要的是这种卡片：

```text
╭─ Read File ──────────────────────────╮
│ src/main.py                          │
│ 1  import os                         │
...
╭─ Edit ───────────────────────────────╮
│ - token = get_token()                │
│ + token = await get_token()          │
```

而现有事件里：

```python
# agents/agent.py:606
arguments=self._preview(call["arguments"], AUDIT_PREVIEW_LIMIT),   # 200 字符

# agents/agent.py:618
chars=len(outcome.text),    # 只记长度，不记全文 —— 全文已经在会话文件里了
```

**这是刻意的，而且不能改**（`agent.py:54` 那段的理由）：`write_file` 的 content 和
`read_file` 的结果动辄几十万字符，把它们逐条写进 jsonl 会让审计文件变成另一个
会话文件，而且审计的写入路径**故意**是"每条一次 open/write/close"（`audit/jsonl.py:35`），
写大载荷的代价会立刻显形。

同理，**diff 需要的 old_string / new_string 是被截断掉的**，所以按现状 TUI 渲染不出
初步思路里那张 Edit 卡片。

→ **决策 3：v1 不渲染这些卡片。** 所以这条冲突**不需要解决** —— 审计里那份
200 字符预览一个字都不用改。想看工具到底读到了什么，去翻会话历史
（`role=="tool"` 那些消息存的是全文，见第四节开头那段）。

### 冲突 4：`tool_call` / `tool_result` 事件里**没有 tool_call_id**，并行批次对不上

现在的事件只有 `tool`（名字）。同一批里两个 `read_file`（`a.py` 和 `b.py`）在
事件流里长得一模一样，只能靠"`tool_call` 按顺序发完，`tool_result` 也按顺序发"
这个隐式约定配对（`agent.py:663-684`）。

而 TUI 要回答的是"这次调用后来怎么样了"，一旦不对应就是"结果贴错调用"这种
看起来完全正常、实际全错的展示。`messages` 里是有 `tool_call_id` 的
（`agent.py:541`），事件里却没有。

→ **决策 4：加 `call_id`**（第四节 D1）。这是**唯一一处协议改动**，收益很大。

### 冲突 5：`model_call` 事件里没有思考过程的位置

`--debug` 里把思维链整段打出来（`agent.py:494`，`_debug_lazy`），但**审计事件里
没有它** —— 一个字节都没有。所以 `--audit` 看不出模型想过什么。

→ **决策 5：写进审计**（第四节 D2）。这是本次唯一一处**扩大审计载荷**的改动，
体积和敏感性两笔代价都写在那一条里。附带一个必须一起做的清理：`--debug` 那份
要删掉，否则同一段正文有两条出口。

### 冲突 6：stdin 是一根管子，只能有一个读者

**这条要展开说，因为它看起来像细节，实际是协议能不能跑起来的前提。**

程序的标准输入不是"一个可以被多方共享的输入源"，它是**一条字节流**：谁先读到
哪一行，那一行就从流里消失了，别人再也读不到。

现在这个项目里有**三个地方**都在从 stdin 读一行：

| 通道 | 现在在哪 | 现状 |
|---|---|---|
| 用户输入 | `cli.py:614` `line = input().strip()` | 走 stdin |
| 审批 | `security/asker.py:153` `answer = input()` | 走 stdin，**在 Agent 主线程里阻塞** |
| 提问 | `tools/ask.py:154` `line = input().strip()` | 走 stdin，**在工具 handler 里阻塞** |

今天它们相安无事，因为**同一时刻只有一个人在读**：循环读一句 → 调 Agent →
Agent 里要审批时才去读下一句。是**串行**的，所以永不冲突。

而协议循环要做的是：

```python
line = sys.stdin.readline()      # 等 TUI 发来一行 JSON
```

**它和那三处在抢同一根管子。** 具体会怎么坏：

```text
TUI 发出：  {"t":"user_message","text":"改一下 a.py"}

若没换掉 cli.py:614：
  协议循环的 readline() 和它之间，谁读到这行 JSON 是不确定的 ——
  读到的那一方会把它当成"用户敲的一行字"。

若没换掉 security/asker.py:153（这一种更坏）：
  模型要跑 shell → 协议循环阻塞在 readline() 等 TUI 的消息
                → cli_asker 同时阻塞在 input() 等你在终端按 y
  两个读者。你在终端按的 "y" 可能被协议循环当成一行坏 JSON 吃掉；
  而 TUI 发来的下一条消息可能被 cli_asker 当成"用户批准了"。
  一次错误的放行，而且审计里会记成 outcome=approved。
```

**所以"协议循环"和"老 CLI 的 `input()`"不能共存** —— 这不是概率问题，
是同一条流上有两个读者，必然互相吞行。

修法就是 **D3**：把这三处**全部**换成注入的实现，让它们不再读 stdin，
而是"写一条 `permission_request` 到 stdout、然后在原地阻塞等
`permission_response` 回来"。这样 stdin 上只剩协议循环一个读者。

**两条连带约束**（都由这一条推出来，不是独立的洁癖）：

1. **v1 必须一个进程一个会话**（9.2）—— 换会话要重启进程，正是为了避免
   "拆掉旧协议循环、装一个新的"这种有两个读者的中间状态；
2. **v1 不给工具做取消**（9.3）—— 取消要在运行中插一条读取命令，
   那是往 stdin 上加第三个读者。

### 冲突 7：stdout 现在既装"给模型的话"，又装"给你看的字"

**这条也要展开。** 这个项目有一条明确的承诺（`README.md:72`）：

```powershell
uv run main.py > 对话.txt     # 拿到的应该是干净的答案
```

它能成立，是因为约定了 **stdout 只放对话正文**，所有杂七杂八的话（横幅、提示符、
审批、统计）都走 stderr。

但 `main.py` / `cli.py` 里有 7 行**没守住这条约定**：

```python
print_banner()                          # main.py:210  → 内部是 stderr，OK
print("已注册工具:")                     # main.py:288  ← stdout
    print(f"  - {tool.name:12} ...")     # main.py:290  ← stdout
print(f"[权限] ...未注册...")            # main.py:296  → stderr
print(f"审计日志写到 ...")               # main.py:369  ← stdout
print(f"继续会话 ...")                   # cli.py:484  ← stdout
print(f"新建会话 ...")                   # cli.py:487  ← stdout
print(f"新会话 ...")                     # cli.py:491  ← stdout
print(f"  想回来继续它： --session ...")  # cli.py:492  ← stdout
```

**今天这不算 bug**：你人在终端前，看到一行"已注册工具:"完全正常。

但协议模式要把 stdout 变成"只装 JSON、一行一条"的通道。那时候上面这些行的后果是：

```text
{"v":1,"t":"init",...}          ← TUI 能解析
已注册工具:                       ← TUI 按 JSON 解析 → 报错，或者静默丢掉
  - read_file   风险=low         ← 同上
{"v":1,"t":"event",...}          ← 这行还能解析，但 TUI 已经乱了
```

所以这 7 行必须改道 —— 而且**不是简单地"改成打到 stderr"**：

- TUI 也要显示"这次有哪些工具、哪些免审批"。打到 stderr 的话 TUI 读不到
  （stderr 按 6.1 的约定是给人看的原始日志，**不参与协议**）；
- 所以正确的做法是**变成结构化字段**：`init.tools`（工具名单）、
  `init.audit_path`（日志路径）、`init.session_id` + `init.resumed`（会话身份）。
  这就是 7.2 那张表在做的事。

**一句话把两条合起来**：冲突 6 说的是"**进的管子只能有一个读者**"，
冲突 7 说的是"**出的管子只能有一种内容**"。前者靠替换三条人机通道解决（D3），
后者靠把那 7 行改成结构化字段解决（D4）。

### 冲突 8：`doc/TUI.md` 的 Session 设计与现有两层持久化重复

初步思路第十一、十二节提议：

```text
~/.myagent/sessions/2026-09-12/abc123.jsonl
{"type":"user","content":"..."}
{"type":"tool_end","output":"..."}
```

现有实现是**两层，而且分得很清楚**：

| 文件 | 写模式 | 内容 | 代码 |
|---|---|---|---|
| `.tudouni/sessions/<id>.jsonl` | 只追加（原先：整份重写 + `os.replace`） | 会话的全部事实（messages + metadata），**唯一的真相来源** | `state/store.py:164` |
| `.tudouni/logs/<id>.jsonl` | 只追加、天然抗崩溃 | 审计轨迹（谁批准了什么、花了多少 token） | `audit/jsonl.py:35` |

> **后面改过一次，但结论没变。** 会话那一层后来从"整份重写"改成了"只追加"
> （2026-09，见 `state/store.py` 的模块 docstring）：理由是 checkpoint 每步一次，
> 而整份重写让落盘量变成**步数的平方**（实测 80 步写出去 863MB，放大 41 倍）。
> 但那次改动**只换了写入模式，没有合并两层** —— 上面这个"不要重做"的判断说的是
> "别把会话和审计合成一个 JSONL"，而这一条仍然成立。下面列的四项代价里，
> 只有第二项真的发生了（原子性换成了"半截行跳过"，见 store 那边的说明）；
> `STATE_VERSION` 保留着，三条子命令一行没改（它们走的是 `store.load` /
> `list_ids`，那两个签名没动），控制面守的是整个 `.tudouni/` 目录所以也没受影响。

两者的分工不是随手分的：`store.py` 那段注释解释了"写入模式不同，所以放在不同的
地方、用不同的策略"。**初步思路提议的 JSONL session 会把这两层合成一层**，代价是：

- 丢掉 `STATE_VERSION` 那份防呆（`store.py:94` 的版本检查）；
- 丢掉 `os.replace` 的原子性；
- `--history` / `--list` / `--audit` 三条子命令全部要重写；
- 控制面（`tools/filesystem.py` 的 `CONTROL_PLANE`）要重新想一遍。

→ **不要重做。** TUI 需要的"恢复会话并重建画面"用现有两层就够（9.1）。
唯一缺的是**会话消息 → 渲染模型**那一步的映射（`cli.py:400` 的 `print_history`
现在只打预览），那是新代码，但不是新格式。

### 冲突 9：`doc/TUI.md` 的目录结构（`agent/`、`tui/`、`cli/`）和现有布局不符

现有布局是**分包、依赖单向、无环**（README「架构」有完整图）：

```
models   （无内部依赖）
state    （无内部依赖）
skills   （无内部依赖 —— 它谁也不 import，所以能独立成包而不和 tools 成环）
tools    → skills
config   → skills, security.commands
security → tools
audit    → state
agents   → audit, models, security, state, tools
main     → 全部
```

**这张图照 README 的「架构」抄，但有一行最容易漏，而它就写在 README 里**：
`tools → skills`（`builtin.py:20`、`filesystem.py:3`、`skills.py` 都 import 了 skills 包）。
所以"tools 无内部依赖"是个**看起来对、实际错**的印象 —— `skills` 是**谁也不 import
的叶子**，而 `tools` 会去 import 它。

`tests/test_imports.py` 里有一条测试盯着"每个模块都能导入"和**一条**方向边（只针对
`skills/` 包 —— 见 7.3 末尾，它盯的比想象中窄）。初步思路提议的
`agent/runtime/loop.py` 那种布局会打乱现有这张图。

→ TUI 相关代码要挂在**现有依赖图的正确位置**上（第十节给了具体位置和依赖方向）。
具体到决策 12：**不新建仓库外的 `tui/`**，TUI 就是 `agent_runtime/frontends/tui/`
这个包 —— 和 runtime 同一个仓库、同一个 venv、同一份 `pyproject.toml`。
代价是前端和内核住在同一个包里，所以"不许互相 import"这条线只能靠测试守
（10.2 里那三条）。

### 冲突 10：Windows 编码 —— 项目已经在这件事上吃过亏

`cli.py:27` 那段注释：

> 纯 ASCII，一个非 ASCII 字符都没有 —— 这不是审美选择，是兼容性。Windows 控制台在
> 中文区域设置下是 cp936，框线字符（─│╭╯）和 emoji 要么直接抛 UnicodeEncodeError、
> 要么显示成乱码。

而初步思路里的界面（第六、七、八节）**全是框线字符**：

```text
╭─ Read File ──────────────────────────╮
│ src/main.py                          │
```

在 **Node/Ink** 那边这不是问题（Node 自己管着终端，输出 UTF-8），而**换成
Textual 之后它也不是问题**（Textual 自己管着终端，同样输出 UTF-8）—— 所以
决策 12 顺带把这一条从 TUI 这一侧消掉了。剩下两个真实的连带后果：

1. 协议管道必须是 **UTF-8 且显式指定**：Python 子进程从管道写非 ASCII 时，标准输出
   的编码由环境决定，不显式管就可能半路炸（6.1 那几条给了具体做法）。
   **这一条不会因为"两侧都是 Python"而消失** —— 子进程的 stdout 是管道，
   不是终端，所以它仍然必须自己管编码；
2. **`--runtime-stdio` 那条路永远不要直接对着终端跑**。它的 stdout 是协议，
   在 cp936 的终端上把 JSONL 打出来就是乱码 —— 这不是 bug，是它的设计用途
   （给人看的是父进程）。想手验协议就用 6.1 那种 `echo | ... --runtime-stdio` 的
   管道方式（管道里显式 UTF-8），别直接在终端敲。

### 冲突 11：初步思路的"5 个阶段"里，有 3 个其实已经做完了

| 初步思路的阶段 | 现状 |
|---|---|
| 阶段 2：`tool_start` / `tool_end` | **已有** `tool_call` / `tool_result` / `tool_batch` |
| 阶段 3：Permission | **已有**完整的 `security/`（策略、关卡、记忆、命令规则）+ `permission` 事件 |
| 阶段 4：Session / Resume / History | **已有** `state/store.py` + `--list` / `--session` / `--history` |

所以真正的阶段划分不是初步思路那 5 步。第十一节给了一份**按现有实现重排的**。

---

## 三、现有实现里已经为 TUI 准备好的东西

这一节是"白捡的"，做的时候不要重新发明。

### 3.1 五个注入点（外加下沉一层的第六个）—— 这就是 UI 契约的雏形

README「第 1 条设计原则」列了四个（`asker` / `memory` / `on_checkpoint` / `on_event`），
`session_notes` 被它称作"第五个"。下表**前五行就是那五个**（也就是 `Agent.__init__`
的参数表），最后一行 `questioner` 从表格里断开一格，因为 README 特意把它排除在
那张表之外 —— 它**比 Agent 还下沉一层**（详见 3.3 后面那段）。

| 注入点 | Agent 知道 | 现在注入的实现 | TUI 要换成的 |
|---|---|---|---|
| `asker`（`agent.py:159`） | 该不该问 | `partial(cli_asker, memory=memory)`（`main.py:356`） | 发 `permission_request`、阻塞等 `permission_response` |
| `memory`（`agent.py:160`） | 人说过哪些"别再问" | `ApprovalMemory` + 落盘 `permissions.json` | **不换**（它只是在 TUI 里显示成"t 记住了什么"） |
| `on_checkpoint`（`agent.py:161`） | 什么时候保存是安全的 | `store.save`（`main.py:358`） | **不换** |
| `on_event`（`agent.py:162`） | 发生了什么 | `logs`（`JsonlSink`，`main.py:359`） | **再加一个** UI sink（不是替换） |
| `session_notes`（`agent.py:202`） | 每轮末尾要贴会话状态 | 技能目录 + 正文 + 任务列表（`main.py:342`） | **不换** |
| --- | --- | --- | --- |
| `questioner`（`tools/ask.py:89`，**不属于 Agent 的注入点**） | —— | `cli_questioner`（`main.py:250`） | 发 `question_request`、阻塞等 `question_response` |

**`Agent` 的判定逻辑一行都不用改** —— 要加的只有事件字段（`call_id` /
`tool_index` / `reasoning` / 一个 `cancelled` 的 stop_reason）。这正是把
`asker` / `questioner` 做成注入实现换来的回报：README 那句"换实现不动 Agent 一行"
在**权限和提问这两条通道上**是真的兑现了 —— 加事件字段不算改逻辑。

### 3.2 `on_event` 已经是一个可替换的 sink，而且 `JsonlSink` 是它的第一个实现

```python
# agents/agent.py:37
EventSink = Callable[[dict[str, Any]], None]
```

`audit/jsonl.py` 的 `JsonlSink` 已经是"把同一批事件换成另一种呈现"的样板 ——
它甚至实现了 `read()`（`jsonl.py:46`），用来给 `--audit` 和每轮末尾那句统计
（`cli.py:426` 的 `print_audit`、`cli.py:570` 的 `_stats_note`）喂数据。

**这已经是"一个事件流、多个消费者"了**，只是第二个消费者（TUI）还没写。

### 3.3 权限的判定和沟通已经分开了

`security/policy.py` 是纯函数、`security/gate.py` 只决定不沟通
（`gate.py:3` 那句"只决定，不沟通"）。所以 TUI 的审批面板**不需要碰任何安全逻辑**，
它只是 `asker` 的另一种实现，而"谁批准了什么"照旧由 `gate.py` 记成
`outcome=approved`。

**这一点很要紧**：它意味着 TUI 里的"允许 / 拒绝 / 总是允许"三个按钮**没有能力**
放宽任何边界。控制面写入（`filesystem.py` 的 `CONTROL_PLANE`）、工作区边界、
`deny_tools` 都不经过 `asker`，所以 UI 上按什么都动不了它们。

### 3.4 `run_id` / `step` 已经强制在每个事件上

`audit/events.py:32` 无条件加这五个字段（`ts` / `kind` / `session_id` / `run_id` /
`step`，后两个正是这里要用的）。TUI 可以据此做：

- 按 `run` 分组（一次提问 = 一个 run）；
- 显示"第 3/40 步"（`max_steps` 默认 40，`agent.py:439`）；
- 用 `run_finished.stop_reason` 区分"答完了 / 步数用尽 / 模型失败" —— 这三件事在
  UI 上**必须长得不一样**（`StepLimitExceeded` 那段 `agent.py:129` 的整个理由就是
  "不要让人分不清答完了和被砍断了"）。

### 3.5 会话是"参数"而不是"Agent 的身份"

`agent.py:445` 那段注释：`Agent` 是无会话、可复用的能力组合，`Session` 是独立数据。
所以同一进程内切会话在架构上是允许的 —— 但**运行时装配不允许**（技能 board 和
todo board 都绑在 `session.metadata` 上，见 `main.py:253`/`main.py:282`）。

→ 这条决定了：**v1 一个进程一个会话**（9.2）。

### 3.6 任务列表已经有"给人看的一行"

`tools/todo.py:128` 的 `progress_line()` 和 `todo_note()` 是**刻意分开的两份渲染**：
一份给模型（每轮贴在载荷尾部），一份给人（`2/5 完成，当前：写测试`）。
TUI 的进度条直接用它，不用新写一份。

---

## 四、缺口清单

按决策 1–10，**原来七个缺口只剩四个**（外加一条收尾清理）。按"必须改"排序，
**每一条都标了动哪个文件的哪一段。**

被砍掉的两个留档在这里，因为否决理由比结论更值钱：

| 被砍的缺口 | 为什么砍 | 代价（要认下来） |
|---|---|---|
| 模型层要能出流（原 D1） | 决策 1：不做流式 | 回答**整段出现**，没有"逐字蹦"的效果。想要时的插入点是现成的：`complete()` 加一个 `on_delta` 参数、适配层加 `stream=True` 分支，而 `agents/retry.py` 到那时才需要改 |
| UI 事件通道要带全文（原 D2） | 决策 3：v1 不渲染工具卡片 | 完全**不需要第二条事件通道**、不需要 `_emit_ui`、不需要 `*_full` 字段。代价是 v1 看不到工具结果卡片和 diff —— 而工具结果**在会话文件里是全文**（`role=="tool"` 那些消息），`--history` 反过来就能看，所以这不是"数据丢了"，是"界面没画" |

**决策 3 为什么可以不要第二条通道，值得说清一层**：会话文件里的 `messages` 本来
就存着工具的完整输出（`agent.py:539` 那条 `role="tool"` 消息，`content` 就是全文），
而 v1 的 `session_load` 会把整份 `messages` 发给前端（9.1）。所以"想看工具结果"
这条路是通的 —— 它只是没有专门的卡片，得自己去翻会话历史。而审计里那份
200 字符预览**依然只给审计**，一个字都不用动。

---

### D1【必须】`tool_call` / `tool_result` 加 `call_id`（决策 4）

```python
# agents/agent.py:596  _report_tool_call
self._emit("tool_call", ..., tool=call["name"], call_id=call["id"], arguments=预览)

# agents/agent.py:609  _report_tool_result
self._emit("tool_result", ..., tool=call["name"], call_id=call["id"], chars=len(text), ...)
```

`call["id"]` 现在就在手里（`agent.py:511` 用的就是它），加一个字段而已。
**收益**：TUI 的"这次调用发生了什么"不再依赖事件顺序。并行批次也正确
（`_run_parallel` 里结果的顺序虽然和输入一致，但"顺序一致"是个实现细节而不是契约）。

顺带：`tool_call` 里应该带上 `tool_index`（这一批里的序号，UI 能显示 `[2/3]`）。
它不是必须的，但和 `call_id` 一起加的成本一样。

**这是唯一一处协议改动**（决策 2 说"不新造协议"），加完之后审计那条流就是协议。

---

### D2【必须】思维链写进审计（决策 5）

**现状**：`reasoning` 只在 `--debug` 时整段打进 **stderr**（`agent.py:494`
的 `_debug_lazy`），审计事件里一个字节都没有。所以 `--audit` 看完一圈，
看不出模型想过什么。

**决策 5 定的做法**：`model_call` 事件加一个 `reasoning` 字段，装**全文**。

```python
# agents/agent.py:376  _attempt_reporter 的 report()
data["reasoning"] = attempt.response.reasoning      # 整段，不截断
```

**同时必须改掉 `--debug` 那一份** —— 否则同一段正文有两条出口：

- `--debug` 打进 stderr（只在那一次运行可见、重定向就丢）；
- `--audit` 进 jsonl（永久、可 grep、可给别的进程读）。

留着 `--debug` 那一段的后果是**同一份事实写两遍** —— 而这个项目的第 3 条设计
原则明确反对它。所以：`agent.py:494` 那段 `_debug_lazy` **删掉**，
想读思维链就去读审计。这也顺带解决了它现在的一个别扭：`debug=False` 时那段
正文根本留不下来，而"模型当时在想什么"恰恰是排查问题时最想要的东西。

**三件要说清楚的代价**（都是"认下来"而不是"能规避"）：

1. **审计文件的体积。** 思维链和一次 `read_file` 的输出是同一个量级（实测几千
   字符/步），而一个典型长任务是 19 步。假设每步 3k 字符，一个会话就是约 60KB
   —— 以审计文件来说不算大，但它是**审计里第一个"内容型"字段**：在此之前审计
   只有数字、枚举、和 200 字符预览，所以评审它的人会下意识以为它很小。
   写进 README 的「数据落在哪里」那张表里。
2. **它是敏感内容。** 思维链里会原样出现它读到的代码、路径、`ask_user` 的答案、
   以及网页/技能正文的片段。审计文件因此**从"元数据"变成了"可能含工作区内容"**
   —— 它仍然是控制面（`tools/filesystem.py` 的 `CONTROL_PLANE` 里，
   agent 写不了），但人复制日志出去分享时要有这个意识。
3. **它和答案的关系是"模型的主张"，不是事实。** 和 `todos_*` 那几个数一样：
   记下来是为了事后回答"它当时怎么想的"，不是用来判定它对不对。

顺带把**已知的偏离**记在这里（改动这块时最容易顺手"修"错）：`models/types.py:60`
写着"官方文档说携带 tools 的请求必须完整回传 `reasoning_content`，否则 400，
而本项目每轮都带 tools"。当前端点没严格执行。**决策 1 不做流式，所以这次改动
不碰发送侧** —— `reasoning` 依然只读进来、不回传。那不是本次遗漏，是另一件事。

---

### D3【必须】三条人机通道换成 UI 实现（决策 6）

| 通道 | 现在的实现 | 要做的 |
|---|---|---|
| 用户输入 | `cli.py:592` 的 `run_repl` | 换成协议循环（第九节） |
| 审批 | `security/asker.py:91` 的 `cli_asker` | 新写一个 `ProtocolAsker`：发请求、**在同一个线程里阻塞**等回应 |
| 提问 | `tools/ask.py:130` 的 `cli_questioner` | 新写一个 `ProtocolQuestioner`，形状同上 |

**为什么必须全换**：冲突 6 里拆开讲过 —— stdin 是一根管子，协议循环和任何一个
残留的 `input()` 是两个读者，必然互相吞行。**最坏的那一种是审批**：你在终端按的
`y` 可能被协议循环当成坏 JSON，而 TUI 发来的下一条消息可能被 `cli_asker` 当成
"用户批准了" —— 审计里还会记成 `outcome=approved`。

`ProtocolAsker` 的签名必须**一模一样**（`Callable[[Tool, Mapping], bool]`），因为
`gate.py:125` 调它的那一行不能改。`t`（以后别再问）这件事**留在 Python 侧**：
`cli_asker` 现在是"返回 True 之前先写 memory"（`asker.py:160`），`ProtocolAsker` 照抄这个
模式，UI 只需要回 `{"decision": "always"}` 这样一个枚举 —— **UI 不该知道
"命令前缀"这种粒度**，那是 `security/commands.py` 的知识。

三个选项的映射：

| UI 按钮 | 回给 Python | Python 侧动作 |
|---|---|---|
| 允许 | `allow` | 返回 True，不写 memory |
| 拒绝 | `deny` | 返回 False（`gate` 记 `user_denied`） |
| 总是允许 | `always` | 先 `memory.grant*()` 再返回 True（`gate` 用问前问后快照差记 `remembered`，`gate.py:122`） |

**`always` 的提示文案必须跟着风险走**：`asker.py:95` 的 `_remember_hint`
（它再读 `asker.py:89` 的 `_REMEMBER_CONSEQUENCE`）已经写好了
（高风险是"以后每次都直接执行，你不会再看到它要做什么"）—— UI 要显示
这句话，不要自己另写一句。这是"同一份事实只写一遍"在一个按钮文案上的形态。

---

### D4【必须】协议要带版本和握手（决策 2 的落地形态）

`state/store.py` 有 `STATE_VERSION` 并做了版本检查（`store.py:94`），
**审计日志没有**。而 UI 协议比这两者都更需要它：协议的两端**可能分别升级**
（父进程和子进程虽然同一个仓库，但会话可以跨越一次 `git pull` —— 一个跑着旧版的
父进程接上一个新版子进程，那条线上跑的就是两个版本的字段）。

每条消息带 `v: 1`，第一条固定是 `init`（字段见 6.3）。它同时解决了冲突 7 那 7 行：
它们变成 `init` 的字段，不用再各打一行。

---

### D5【建议】进程级错误协议化

初步思路的事件表里有 `error`，但现有事件流**不需要**新造它 —— 错误已经写在
它该在的地方：

- 模型失败 → `model_call.status in {error, fatal}` + `run_finished.stop_reason`
  in `{model_error, model_fatal}`；
- 工具失败 → `tool_result.status in {error, denied, invalid_args}`；
- 步数用尽 → `run_finished.stop_reason=max_steps`（并且 `run()` 抛
  `StepLimitExceeded`，`agent.py:561`）。

UI 侧需要的是一张**映射表**（第八节），不是新事件。

唯一真正缺的是**非工具、非模型的进程级错误**（配置错、落盘失败、`_warn` 打出来的
那些，`agent.py:283`）。这些现在直接 `print(..., file=sys.stderr)`。协议化时它们
应该变成一条 `notice` 消息：

```json
{"v":1,"t":"notice","level":"warn","text":"会话落盘失败（已忽略）：..."}
```

**理由**：README 反复强调"不能静默失败"（`agent.py:278`）。TUI 里这些必须进一个
可见的日志面板，否则协议化之后它们会**变成管道里的垃圾而被丢掉** ——
那就是把一个"大声说"改成了"没人听得见"。具体做法见第十二节 R2。

---

## 五、进程模型与 TUI 技术栈

决策 11（两进程）和决策 12（Python + Textual）已经拍板，所以这一节不再是"选哪个"，
而是**把这两个决定讲清楚，以及那个被它们排除掉的方案为什么危险** ——
后者值得留档，因为"一个 Python 仓库为什么要开两个进程"是个会被反复问的问题。

### 采用的形状

```text
main.py --tui                     ← 父进程（Textual 应用，frontends/tui/）
  │  asyncio.create_subprocess_exec(
  │      sys.executable, "-u", <repo>/main.py, "--runtime-stdio", cwd=<repo>)
  │  stdin  ← 写 JSONL（user_message / permission_response / question_response / shutdown）
  │  stdout → 读 JSONL（init / session_load / event / ui / notice / *_request）
  ▼
main.py --runtime-stdio           ← 子进程（runtime/ + protocol/）
  │  完全不知道对面是 Textual 还是将来的 Web
  ▼
Agent.run(...)                    ← 同步、阻塞，一个字都不改
```

**两侧都是 Python，但仍然是两个进程。** 两个进程和"用什么语言写"是两件独立的事 ——
这一点是决策 11 的核心：协议被夹在中间，所以**客户端换语言对 runtime 不可见**，
而 Web 前端将来只是同一份协议的第三个客户端。

### 为什么不用单进程（那个被排除的方案，留档）

单进程（TUI 直接 `import` Agent、自己调 `agent.run()`）看起来更简单 ——
没有协议、没有序列化、能直接拿到 `Session` 对象。**但它会引入一个
这个项目至今没有的失败类别：两个读者抢同一个终端加一条阻塞的调用栈。**

具体是三件事叠在一起：

1. **`Agent.run()` 是同步阻塞的**（`agent.py:465` 的 `for step in range(...)`），
   一次调用会把线程占住几秒到几分钟；
2. **Textual 的事件循环必须待在主线程**（它管着终端、信号和 asyncio loop）；
3. 所以 Agent 只能去工作线程 —— 而 **asker / questioner 会在那个线程上被调用**，
   它们却要弹一个模态框、等主线程上的按键。

于是每条人机通道都变成一次**跨线程握手**（工作线程 → `call_from_thread` → 主线程
弹屏幕 → `threading.Event` 回传）。能做，但代价是：

- 这一段**没法用现有测试口径测**（`tests/` 里那套注入的假 asker / 假 questioner
  是同步的、当场返回的，跨线程握手测不到它真正会出问题的地方）；
- 失败形态是**偶发**的：用户关窗时工作线程还在等 `Event`、`call_from_thread`
  在 app 已经退出后抛异常、模态框和审批撞在一起 —— 这些都不会稳定复现；
- 而**两进程方案里这些问题一个都不存在**。子进程的 asker 就是"写一行 JSON、
  阻塞读一行"，是纯同步代码，用现有的假 asker 就能测。

**换句话说：对 Python TUI 而言，两进程不但不是额外成本，反而是把最脏的那部分
（线程 + 终端所有权）整个删掉了。** 换来的成本只有两处，都很局部：

1. 每个跨进程字段要定义清楚 —— 而这件事第六节已经做完了，而且**已经定稿**；
2. 启动多一跳（一个 Python 解释器 + import），大约几百毫秒。

### 为什么是 Textual（决策 12）

`pyproject.toml` 现在只有 4 个运行时依赖，而 Textual 会带一串传递依赖
（`rich`、`markdown-it-py`、`linkify-it-py`、`platformdirs`、`typing-extensions` …）。
这是一个**要认下来的代价**，理由只有一条：另一个选择是自己写。

| 选择 | 它给你什么 | 代价 |
|---|---|---|
| **Textual**（采用） | 完整的应用框架：输入框、列表、模态框、键盘导航、异步模型、屏幕栈 | 一串传递依赖；一套新的 API 要学 |
| `rich` 自己拼 | 渲染和布局 | **不是应用框架** —— 键盘导航、输入框、模态框、焦点管理全要自己写，工作量反超 |
| 零依赖自己写 ANSI | 不碰 `pyproject.toml` | 等于自己写一个 TUI 框架 |

**这个依赖被关在 `frontends/tui/` 里**（第十节）：`pyproject.toml` 里 `textual`
出现在依赖组的一行，而**只有 `frontends/tui/app.py` 和 `widgets.py` 会 import 它**。
所以 `uv run main.py`（老 CLI）、`--runtime-stdio`（子进程）、
`--list` / `--audit` / `--history` 这些路径**完全不加载 textual** ——
这一条由 10.2 里那条测试盯着（它是唯一能保证这件事的东西）。

**一个不太明显但真实的收益**：Textual 自己管着终端，所以**冲突 10（cp936）在
TUI 这一侧消失了** —— 框线字符和 emoji 由 Textual 按它自己的方式输出，
不需要我们在 Windows 上做任何特殊处理。而子进程那一侧仍然必须显式 UTF-8
（6.1 那三条），因为它的 stdout 是管道。

### 这条路对初步思路的偏离，要说清楚

`doc/TUI.md` 第十七节建议的是 **React + Ink**。换掉它的唯一代价是
"放弃 React 这条你在学的路"—— 而这件事**现在几乎不影响 runtime**：
协议已经把两者解耦了。如果以后想把客户端换成 Ink，**唯一要重写的是
`frontends/tui/` 里那个客户端**（Textual 应用），而 `protocol/`、`runtime/`
和整个内核一个字都不用动。这不是安慰话 —— 它是第六节那个协议设计存在的全部意义，
而且**它已经被"将来要加 Web"这件事预演过一次了**（第十节）。

---

## 六、协议定义（v1）

### 6.1 传输

- 一条子进程，`stdin` / `stdout` 都是 **UTF-8 的 JSONL**（一行一条，`\n` 结尾）；
- **`stderr` 不参与协议**：它是人看的诊断（现有 CLI 的警告、traceback 都走它），
  TUI 可以原样接到一个"原始日志"面板里。这条分法**沿用现有的 stdout/stderr 分工**
  （`README.md:72`、`cli.py:611`），只是 stdout 的角色从"对话正文"变成了"协议"；
- 每条消息**必须 flush**（具体做法就在下面那三条里）。

**两侧都是 Python 之后，这条管道怎么起**（决策 11 + 12）：

```python
# frontends/tui/client.py —— 父进程（Textual）
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-u", str(MAIN_PY), "--runtime-stdio",
    cwd=str(REPO_ROOT),                    # ← 见下面第 3 条，非有不可
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    # stderr 故意不接：让它直接落到终端（父进程的 stderr），
    # 这样 traceback 和 [warn] 在 TUI 外面也看得见 —— 恰好是 6.1 第二条的用法。
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
)
```

四个细节，每一个都是踩过才知道的：

1. **`sys.executable`，不是 `"python"`。** 父进程跑在哪个解释器里（venv、uv 管的
   那个），子进程就必须是同一个 —— 写 `"python"` 会走到系统 PATH 上另一个解释器，
   而那个里面**没装 openai / pydantic**，症状是子进程立刻退出、父进程读到 EOF。
2. **`-u`**：Python 的 stdout 接管道时是块缓冲，不关掉就会出现"事件攒在缓冲区里、
   界面几秒不动"（下面第 3 条的读端是好的也没用，问题在写端）。
3. **必须是"绝对路径 + `cwd=仓库根`"，不能用 `python -m agent_runtime.main`。**
   这一条是**实测过的**：项目是 `package = false`（`pyproject.toml` 那段写了为什么），
   `agent_runtime` 根本没被安装，`-m` 找不到它。而 `main.py` 里那句
   `sys.path.insert(0, str(REPO_ROOT))`（`main.py:23`）在**当脚本跑**时生效、
   在 `-m` 下不生效 —— 所以 `-m` 这条路是坏的，而它坏起来的样子是
   `No module named agent_runtime`，看起来像环境装错了。
   `cwd=仓库根` 是给 `main.py` 那句 `sys.path` 补丁兜底的（它算的是
   `Path(__file__).parent.parent`，与 cwd 无关，但 `/main.py` 里后续的相对路径
   读取是有关系的）。
4. **`PYTHONIOENCODING=utf-8`** 兜住写端的编码（下面第 1 条是自己 reconfigure 的，
   两个都做是双保险）；**父进程这一侧也要 `encoding="utf-8"`** 去解
   `proc.stdout`，否则 Windows 上按 cp936 解会炸在第一条中文上。
5. **stderr 不进协议、也不吞掉**：`stderr=None`（继承）会让子进程的 traceback
   直接打在 TUI 的终端上 —— 界面上会花掉，但**这是刻意选的失败方向**：
   一个什么都不显示的 traceback 比花屏更坏。想干净就在第一期先这么放着，
   等有了"原始日志"面板再改成 `PIPE` + 转发（R2 那条会把 `[warn]` 变成
   `t:"notice"`，那时候 stderr 上剩下的就真的只有 traceback 了）。

三件必须在**子进程**启动时做掉的事（少一件都会得到一个"跑起来但时序全乱"的 TUI）：

1. **`sys.stdout.reconfigure(encoding="utf-8", newline="\n")`。** 现有 CLI 打的全是
   ASCII（`cli.py:27` 那段解释过为什么），所以它从来没碰到过这个问题；协议里第一条
   就带中文（`session_id` 是 ASCII，但 `notice` 的文字不是），而 Python 从管道写
   非 ASCII 时用的是**环境决定的编码** —— 不显式管就会在某些机器上炸在第一次出
   中文的那一刻。`newline="\n"` 同理：Windows 上默认会写成 `\r\n`，而 JSONL 的
   行分隔符必须是 `\n`（否则前端按 `\n` 切会得到一堆尾部带 `\r` 的行）。
2. **每条消息 `flush=True`**，而且**不能依赖行缓冲**：stdout 接的是管道时 Python
   用块缓冲（4~8KB），于是事件会在缓冲区里攒着，前端的"实时"变成"每 8KB 一跳"。
   这正是现有 `cli.py:613` 那个提示符要显式 `flush=True` 的原因 —— 它现在是对的，
   但那是因为人的眼睛等得起。
3. **启动时把 `-u` 也带上**（`python -u`）作为双保险。两条都做，是因为哪一条单独
   失守都会表现成同一件难查的事："界面偶尔不刷新"。

### 6.2 前端 → runtime（4 种）

```json
{"v":1,"t":"user_message","text":"帮我分析这个项目"}
{"v":1,"t":"permission_response","id":"p-7","decision":"allow"}
{"v":1,"t":"permission_response","id":"p-7","decision":"always"}
{"v":1,"t":"permission_response","id":"p-7","decision":"always_group"}
{"v":1,"t":"question_response","id":"q-3","text":"1","status":"answered"}
{"v":1,"t":"shutdown"}
```

- `decision` ∈ `allow` / `deny` / `always` / `always_group`；
- **`always_group` 是一份请求级的一次性授权，不是一条策略** —— 见 6.3 里
  `allow_trust_all` 那段（哪个 `id` 就只对这个 `id` 有效）；
- `status` ∈ `answered` / `skipped`（和 `tools/builtin/ask.py` 的三个常量对齐，
  `unavailable` 是 runtime 自己产生的，前端不会回这个）；
- **没有 `interrupt` 消息**：`Ctrl+C` 由前端**关掉子进程的 stdin**（或发
  `shutdown`）表达，runtime 在下一个安全检查点退出（9.3 有详细理由）。

### 6.3 runtime → 前端

**握手（1 条，永远是第一条）**

```json
{"v":1,"t":"init","protocol":1,
 "session_id":"20260912-101530","resumed":false,
 "model":"deepseek-flash","workspace":"C:\\Users\\...\\agent_runtime",
 "max_steps":40,
 "audit_path":".tudouni\\logs\\20260912-101530.jsonl",
 "tools":[{"name":"read_file","risk":"low","parallel_safe":true}, ...],
 "permissions":{"auto_approve":["low"],"auto_approve_tools":["shell"],
                "deny_tools":[],"shell_allow":["git add"]},
 "notices":[{"level":"info","text":"没找到 TAVILY_API_KEY，web_search 未注册"}]}
```

`notices` 装的是现在 `main.py` 里那些 `[权限]` / `[技能]` / `[任务]` / `[联网]` /
`[上下文]` 行：`main.py:315`（`report_permissions`）、`main.py:316`（`report_todos`）、
`main.py:317`（`report_skills`）、`main.py:296`（权限名单里有没注册的工具）、
`main.py:240`（缺 TAVILY_API_KEY）、`main.py:216`。

**注意 `main.py:288`-`290`（已注册工具那份清单）不在这个列表里**：它走的是 stdout，
而且它是**结构化数据**（名字 + 风险），该进 `init.tools` 而不是一句文字 notice ——
7.2 那张表是这条的权威版本，两处不要各写一份。

**会话恢复（0 或 1 条，紧跟 init）**

```json
{"v":1,"t":"session_load","messages":[ ...原样的 session.messages... ]}
```

**原样的 messages**，因为 UI 需要自己决定怎么画（而且这是**唯一**能让"恢复会话后
画面和历史一致"的做法：`--history` 的预览式渲染是给终端用的，不是给 TUI 用的）。
代价写明：恢复一个长会话时这一条消息可能几 MB —— 所以它是**一条**、只发一次，
而不是每条消息一个事件。

**活事件（N 条）**

```json
{"v":1,"t":"event","kind":"run_started","run_id":"a1b2c3d4","step":0,
 "ts":"2026-09-12T10:15:30.123","session_id":"...","user_input":"帮我分析这个项目"}

{"v":1,"t":"event","kind":"model_call","run_id":"a1b2c3d4","step":1,
 "status":"ok","attempt":1,"duration_ms":1234,
 "prompt_tokens":2035,"cached_tokens":1792,"miss_tokens":243,"completion_tokens":88,
 "tool_calls":2,
 "reasoning":"先看目录结构……（决策 5：整段进审计，不截断）"}

{"v":1,"t":"event","kind":"tool_call","run_id":"a1b2c3d4","step":1,
 "call_id":"call_abc","tool":"edit_file",
 "arguments":"{\"path\":\"src/a.py\",\"old_string\":\"...\",\"ne…(共 210 字符)"}

{"v":1,"t":"event","kind":"permission","run_id":"a1b2c3d4","step":1,
 "call_id":"call_abc","tool":"edit_file","risk":"medium",
 "decision":"ask","outcome":"approved","arguments":"{...}",
 "waited_ms":3200,"remembered":["edit_file"],"rule":null}

{"v":1,"t":"event","kind":"tool_result","run_id":"a1b2c3d4","step":1,
 "call_id":"call_abc","tool":"edit_file","status":"ok",
 "chars":42,"duration_ms":3,"parallel":false}

{"v":1,"t":"event","kind":"run_finished","run_id":"a1b2c3d4","step":3,
 "stop_reason":"answered","duration_ms":8100}

{"v":1,"t":"ui","kind":"run_finished","run_id":"a1b2c3d4","step":3,
 "answer":"已改好 src/a.py：把同步调用改成了 await。"}
```

**说明**：

- `t:"event"` **就是审计那条流的原样转发**（一个字段都不加、一个字段都不减）——
  这一条让"审计 = 协议"这句话**在字节层面成立**：`t:"event"` 的消息就是
  `.tudouni/logs/<id>.jsonl` 里那一行，只是包了一层；
- `kind` 的取值**就是现有那 7 种**（`run_started` / `model_call` / `tool_call` /
  `tool_result` / `permission` / `tool_batch` / `run_finished`），一个都不新造
  （决策 2）；唯一的新字段是 `call_id`（决策 4）；
- `reasoning` 也在 `model_call` 上（决策 5）—— 它是审计里第一个"内容型"字段，
  体积和敏感性的代价见第四节 D2；
- **只有 `t:"ui"` 一条**：`run_finished` 的 `answer`。审计事件里**没有**正文
  （`agent.py:530` 的返回值只交给调用方，从不进事件），所以非流式模式下这是
  UI 拿到答案的**唯一**途径。它就是 `Agent.run()` 的返回值，只在
  `stop_reason == "answered"` 时有；
- 未知 `kind` 的处理：**UI 必须忽略并保留**，不许崩 —— 和
  `JsonlSink.read()` 跳过坏行（`jsonl.py:66`）、`--audit` 缺字段显示 `?`
  （`cli.py:444`-`445` 那两个 `?` 兜底）是同一个取向。协议的两端会分别升级。

**这里没有"流式增量"那一节** —— 决策 1 把 `t:"delta"` 整组消息推到了 v1 之外
（要加时是 `t:"delta"` + `t:"delta_reset"` 两条，理由见第五节末尾）。

**人机交互（前端必须回应，会一直阻塞）**

```json
{"v":1,"t":"permission_request","id":"p-7","call_id":"call_abc",
 "tool":"mcp__github__create_issue","risk":"high",
 "arguments":{"repo":"me/x","title":"..."},
 "remember":"mcp__github__create_issue",
 "remember_hint":"以后每次都直接执行，你不会再看到它要做什么（写进 permissions.json，下次启动仍然有效）",
 "allow_trust_all":true,
 "trust_all_hint":"以后 MCP server github 的 12 个工具都直接执行，你不会再看到它们要做什么（快照：这个 server 以后新加的工具仍然会问你；写进 permissions.json，下次启动仍然有效）"}

{"v":1,"t":"question_request","id":"q-3",
 "question":"用哪个数据库？","header":"数据库",
 "options":["PostgreSQL：沿用现有实例","SQLite：本地文件，零运维"],
 "multi_select":false}
```

`permission_request` 里的 `arguments` **是全文**，这是刻意的：它**不是一个事件的
伴随字段**，而是"给人做判断的那份参数"本身 —— 和 `cli_asker` 现在打的东西是
同一个东西（`asker.py:129`，高风险工具不截断）。它和审计里那条
`permission.arguments`（预览）**不是同一条消息的两个字段**，所以不存在
"同名谁覆盖谁"的问题。**代价要写清楚**：读协议的人必须知道
"`permission_request.arguments` 是全文"，而事件里那个 `arguments` 是预览 ——
这一条在协议文档里要单独标出来。

`remember` 和 `remember_hint` **是两样东西，不要合并**：

| 字段 | 是什么 | 谁用 |
|---|---|---|
| `remember` | **记什么**：`"shell"`（工具名）或 `{"prefix": ["rm","-rf","build"]}`（命令前缀）。它是子进程真正会写进 `permissions.json` 的东西 | **UI 不用懂它**，只在 `null` 时决定"不显示 [总是允许]" |
| `remember_hint` | **怎么把这件事说给人听**：那一整句后果说明。文案出自 `asker.py:95` 的 `_remember_hint`（它再读 `asker.py:89` 的 `_REMEMBER_CONSEQUENCE`） | **UI 原样显示，一个字都不许改**（13.4） |

分开的理由是决策 16 那条：**"命令前缀"是 `security/commands.py` 的知识，
不该漏进界面**。UI 拿到 `remember` 只是为了知道"这次能不能按 t"，
而它要显示的那句话**已经在 `remember_hint` 里了**。

`remember` 为 `null` 表示**这次不提供"总是允许"**（对应 `cli_asker` 里
`target is None` 那一支，`asker.py:175`）—— 比如命令行里有重定向、
`security/commands.py` 推不出前缀的时候。UI 必须照着隐藏那个按钮，
**不要自己给一个"总是允许整个 shell"** 的选项：那正是 `asker.py:183` 那段
刻意堵掉的东西（"记不住就不提供这个键"）。

**`allow_trust_all` / `trust_all_hint`：MCP 那第二个"记住"按钮（`a`）**

`security/asker.py` 现在有两个"以后别再问"的粒度（`TrustGroup`，`asker.py:37`）：

| | 粒度 | 提示文案来自 |
|---|---|---|
| `t` | 这一个工具（或一条命令前缀） | `_remember_hint`（`asker.py:95`） |
| `a` | **同一个 MCP server 此刻的全部工具**（一份名字快照） | `_trust_all_hint`（`asker.py:111`） |

所以 `permission_request` 上多一对字段，形状和 `remember` / `remember_hint` 完全
对称：

- **`allow_trust_all: bool`** —— 这次能不能按 `a`。它在 `mcp_trust_group()` 返回
  `None` 时为 `false`（`main.py:417`：工具不属于任何 server，**或者那个 server
  只有 1 个工具** —— 那时 `a` 的效果和 `t` 一模一样，多一个键只会让人多读一行）；
- **`trust_all_hint: str`** —— `a` 那一整句说明，**必须原样显示**。它比 `t` 那行多
  担一件事：**说清这是快照**（"这个 server 以后新加的工具仍然会问你"）。不说的话，
  人的理解会变成"这个 server 从此随便用"，而实际发生的是"此刻这 N 个工具进了名单"
  —— 两者在这个 server 下次升级时分开。

**为什么要发 `allow_trust_all` 而不是发工具名单**：UI 只需要知道"能不能按"，
而**放行哪些名字是子进程的事**。让 UI 回一份工具名单，就等于让客户端能指定策略
—— 那和"控制面只有人能写"（README）是同一个担心，只不过这次要写的是
`auto_approve_tools`。所以前端只回 `decision: "always_group"`，
由**子进程查它自己手里那个 `id` 对应的 `TrustGroup`**。诚实的前端和恶意的前端
在这一步没有区别，这正是我们要的。

**通知（N 条，不阻塞）**

```json
{"v":1,"t":"notice","level":"warn","text":"审计事件写入失败（已忽略）：OSError: ..."}
{"v":1,"t":"notice","level":"info","text":"[任务] 2/5 完成，当前：写测试"}
```

### 6.4 一份完整的时序（这就是 UI 要画的东西）

```text
TUI                                    Python
 │  {"t":"user_message","text":"改一下 a.py"}
 ├──────────────────────────────────────►
 │                                        event(run_started)
 │  ◄─── event(model_call, step=1, reasoning="先读一遍 a.py…")
 │                                        （等模型，界面上就是"在想/在写"）
 │  ◄─── event(tool_call, call_id=abc, tool=edit_file)
 │  ◄─── permission_request(id=p-7, tool=edit_file, arguments={
 │            "path":"src/a.py", "old_string":"token = get_token()",
 │            "new_string":"token = await get_token()"} )   ← 全文，给人判断
 │  ┌──────────────────────────┐
 │  │ ╭─ 需要权限 ───────────╮ │   ← UI 画面板、等人按键
 │  │ │ edit_file  medium    │ │
 │  │ │ 路径 src/a.py        │ │
 │  │ │ [允许] [拒绝] [总是] │ │
 │  │ ╰──────────────────────╯ │
 │  └──────────────────────────┘
 │  {"t":"permission_response","id":"p-7","decision":"allow"}
 ├──────────────────────────────────────►
 │                                        event(permission, outcome=approved)
 │  ◄─── event(tool_result, call_id=abc, chars=42, status="ok")
 │  ◄─── event(model_call, step=2)
 │  ◄─── event(run_finished, stop_reason=answered)
 │  ◄─── ui(run_finished, answer="已改好 src/a.py。")
 │
 │                                        （等下一句 user_message）
```

**注意最后两行**：`Agent.run()` 的**返回值**（`agent.py:530` 的 `response.content`）
**不在任何审计事件里** —— 它只交给调用方，而调用方现在是协议循环。
所以非流式模式下，`run_finished` 的那条 `t:"ui"` 消息**是 UI 拿到答案的唯一途径**。
它是现有 `cli.py:624` 的 `print(agent.run(session, line))` 在协议里的对应物，
少发它 = 界面上什么都没有。

**"没有任何 delta"这一档现在是唯一的一档**（决策 1）。将来接上流式时，
要在这儿加一条判定：已经通过 `delta` 累积出正文 → 丢弃 `answer`（同一份文本的
两条来路，以到达顺序那份为准）。**现在不要预先处理这个分支** —— 写一个永远
走不到的 `if` 比不写更坏。

**界面上"AI 正在干活"这个观感从哪来**：没有流式，`model_call` 到 `run_finished`
之间是一段安静期（模型往返 + 工具执行）。所以 UI 必须**自己在本地转圈**
（按 `run_started` 亮起、按 `run_finished` 熄灭，中间靠 `tool_call` /
`tool_result` / `permission` 更新那一行在做什么）。这不是"锦上添花的动画"：
一次 `read_file` 级别的往返是秒级，而一段完全静止的界面会被当成卡死。

---

## 七、接线：具体改哪里

### 7.1 新增一个入口，不动老入口（三条路，不是两条）

```powershell
uv run main.py --tui                # 父进程：起 Textual，它自己去拉一个子进程
uv run main.py --runtime-stdio        # 子进程：stdout 变成 JSONL 协议通道（一般不由人直接跑）
uv run main.py                      # 老路，一行不变
```

`main.py` 保持"薄入口"（它的 docstring 第 4 行："这个文件只做一件事：把各个部件接起来"），
把装好的部件交给三条不同的驱动：

```python
# 第零期之后：装配全部在 runtime/ 里，main.py 只做分派
args = build_parser().parse_args()

if args.tui:
    # 父进程：**不装配任何东西**，直接交给 TUI 客户端去拉起子进程。
    # 这一支必须在最前面（早于 _check_session_id / --list / --skills / 配置检查）——
    # 否则父进程会白白扫一遍技能、建一遍日志目录、然后什么都不干。
    from agent_runtime.frontends.tui.client import run_tui
    return run_tui(args.session)

# 不需要模型的子命令（--list / --skills / --audit / --history）：留在 cli（决策 20）
# ...（原 main.py:187-222 那一段，一字不改）

# 以下需要模型：开一个 Runtime
with Runtime.open(args) as runtime:
    if args.runtime_stdio:
        return serve_stdio(runtime)      # 子进程：协议通道
    return run_repl(runtime)             # 老 CLI：直连（决策 19）
```

**注意 `Runtime.open` 不再单独出现在 `--runtime-stdio` 那一支上** —— 两条路都开
Runtime，区别只在"谁来驱动它"。这也顺带说明第零期是**纯粹的搬移**：
`run_repl` 拿到的从"散装的 agent + session + logs + context_tokens"变成一个
`Runtime` 对象，而它内部做的事一个字不变。

**`--tui` 必须排在配置检查之前。** 它自己不需要 `DEEPSEEK_API_KEY`
（真正要密钥的是子进程），而且它必须能在"密钥没配"的情况下把子进程的报错
**显示在界面上**而不是让父进程先崩掉 —— 否则用户看到的是 TUI 还没起来就退出了，
而"没配 DEEPSEEK_API_KEY"那句提示在父进程的 stderr 上一闪而过。
子进程会以退出码 2 结束并留下那句提示（`main.py:204`），父进程要把它当
**一条 `notice` 显示出来再正常退出**，不是当成崩溃。

**两个开关的关系**：`--tui` 和 `--runtime-stdio` 是父子，人只会用前者。
后者留成显式开关是为了可测 —— 第一期的验收就是"用 `echo` 拼几行 JSON 喂给它"
（第十二节），那时不需要 Textual 在场。

### 7.2 打印改道清单（对应冲突 7，行号按**当前 main.py**）

`main.py` 现在 465 行。/ 下表是**逐条核对过的**（比我第一版准 —— 那时候它还是 384 行，
行号全偏了）：

| 位置 | 现状 | 改成 |
|---|---|---|
| `main.py:243` `print_banner()` | stderr | 不动（协议模式下跳过） |
| `main.py:248`-`251` `[上下文]` 表里没这个模型 | stderr | 进 `init.notices` |
| `main.py:272`-`274` `[联网]` 没 TAVILY_API_KEY | stderr | 进 `init.notices` |
| `main.py:296`-`297` 权限名单里有没注册的工具 | stderr | 进 `init.notices`，**stderr 也保留**（人直接跑时还得看得见） |
| `main.py:347` `report_mcp(...)` | stderr | 进 `init.notices`；**"外部工具每条都要审批"那句必须留** |
| **`main.py:349`-`351` 已注册工具那份清单** | **stdout** | 进 `init.tools`（它是结构化数据：名字 + 风险） |
| `main.py:376`-`378` `report_permissions` / `report_todos` / `report_skills` | stderr | 进 `init.notices`；`[任务]` 那条是**会话状态**，恢复会话时要发 |
| `main.py:383`-`387` autopilot 那行 | stderr | 进 `init.notices`（`level: "warn"` —— 它意味着不问了） |
| **`main.py:446` 审计日志写到哪** | **stdout** | 进 `init.audit_path` |
| `cli.py:484`（继续会话）、`cli.py:487`（新建会话） | stdout | 进 `init.session_id` + `init.resumed` |
| `cli.py:491`-`492`（新会话 id + "想回来继续它"） | stdout | 同上 —— **`resolve_session` 这条路上四个 print 全是 stdout**，别只改前两个 |

**只有三处是 stdout**（加粗那三行）—— 其余本来就是 stderr，那部分不是"污染协议"，
而是**"TUI 看不见"**。两者要分开处理：

- stdout 那三行必须改道，否则协议通道里混进纯文本，前端解析就崩（冲突 7）；
- stderr 那些**改道不是为了修 bug，而是为了让界面能显示它们**。它们在 CLI 里一直
  是对的，但在 TUI 里"没人看得见" —— 尤其是"哪些工具免审批"。

所以：这些**不是"从 stdout 挪到 stderr"**，而是"变成结构化字段"。
README 那条"每次启动都说一遍"的理由（`main.py:76`）在 TUI 里只会更强。

### 7.3 `ui/` 包已被三层结构取代

**这一节原来写的 `ui/` 包（protocol / channels / state / app / widgets 平铺）已经被
第十节的三层结构取代** —— 因为 Web 前端也要来，而原来的排法里 `state.py` 归 TUI、
`channels.py` 和 `app.py` 平级，这两条在三个前端面前都站不住。

一句话版本：**`protocol/` 是跨进程契约（runtime 的"远程 API"），`frontends/` 是
各前端，二者之间不许互相 import 内部**。完整布局与依赖方向见第十节。

被这一节取代的还有两处结论，它们现在写在第十节里：

1. `protocol/state.py`（"事件 → 状态"那张表）**归协议层**，不是 TUI 的 ——
   三个前端共用同一份推导，TUI 自己只留 `view_state.py`（滚动、折叠、焦点）；
2. `frontends/tui/` 里仍然不许在模块顶层 import textual（老 CLI 和子进程都不该加载
   一个 TUI 框架），但这条现在是"前端层的通则"的一部分，见第十节末尾那三条测试。

---

## 八、前端状态机：从事件**推导**，不是另一份事实

初步思路第十节的 `AgentState` 是对的，但它的画法（一个独立的状态机）会变成
**第二份事实** —— 状态机一旦和事件流不一致，UI 会显示一个不存在的状态，
而那种 bug 极难查（画面看起来完全正常）。

**正确做法：状态是事件的纯函数。** 因为决策 1（无流式）和决策 3（不渲染工具卡片），
这张表比有流式时**短得多**：

| 状态 | 进入条件 | 退出条件 |
|---|---|---|
| `IDLE` | 进程启动 / 某个终态之后 | 收到 `user_message` |
| `WORKING` | `run_started` | `run_finished` 或 `run_finished` 那条 `ui` 消息 |
| `WAITING_PERMISSION` | `permission_request` | `permission` 事件 |
| `WAITING_HUMAN` | `question_request` | `tool_result`（`question_status` 会说结论） |
| `FINISHED` | `run_finished(stop_reason=answered)` | 下一次 `user_message` |
| `LIMITED` | `run_finished(stop_reason=max_steps)` | 下一次 `user_message` |
| `FAILED` | `run_finished(stop_reason∈{model_error,model_fatal})` | 下一次 `user_message` |

**四件要注意的**：

1. **`WORKING` 是一个粗粒度的状态，里面包含了模型往返和工具执行两段。**
   没有流式，就没法从事件流里区分"模型在想"和"工具在跑"—— 硬要区分只能靠
   `tool_call` / `tool_result` 之间那段间隔去猜，而那是**用事件去反推事件**，
   正是本节反对的"第二份事实"。**正确做法：`WORKING` 里显示一行"当前在做什么"，
   它的文本直接来自最近一条事件**（`model_call` → "模型在想"、
   `tool_call` → "要调用 edit_file"、`tool_result` → "edit_file 返回了"）。
   这是投影，不是状态机。
2. **提示链（`reasoning`）是一个"事后可读"的东西，不是一个实时状态。**
   决策 5 把它写进了审计，所以它在界面上最自然的形态是"展开这一步的思考过程"，
   而不是"正在思考…"的直播 —— 因为事件到达时它已经想完了。
3. **`LIMITED` 不能长得像 `FINISHED`。** 这是 `StepLimitExceeded` 那个类存在的
   全部理由（`agent.py:129`）：不许让人分不清"答完了"和"被砍断了"。
   UI 上必须是两种颜色 + 一句"接着跑：`/continue`"。
4. **`FAILED` 不退出会话。** 现有 `run_repl` 就是这么做的（`cli.py:636`：
   "一个回合失败不等于整个会话结束"），TUI 必须照抄 —— 这条很容易在写前端时
   顺手写成"报错就退出"。

**并行批次**（`tool_batch` 事件）在 v1 里没有专属画面（决策 3 不渲染工具卡片），
但如果 UI 显示"当前在做什么"，它应该显示成"3 个只读工具并发执行中"而不是
一列先后完成的。`tool_batch.wall_ms` 就是给这个用的
（`agent.py:690` 那段解释了它为什么必须单独记）。

**这张表是"agent 现在处于什么状态"，不是"界面长什么样"。** 界面上"正在干活"
的转圈动画属于显示状态（7.4），它由本地定时器驱动，**不参与任何判定**。

---

## 九、会话、恢复、中断

### 9.1 启动与恢复

`init` + `session_load` 两条消息就够，**不需要新格式**（冲突 8 的结论）。
TUI 重建画面的方式：

| 画面元素 | 数据来源 |
|---|---|
| 用户消息 / 助手回答 | `session_load.messages` 里 `role ∈ {user, assistant}` 的 `content` |
| 工具调用卡片 | 同上，`assistant.tool_calls` |
| 工具结果 | 同上，`role == "tool"`，按 `tool_call_id` 配对 |
| 权限裁决的历史 | **不在会话里**，只有 `--audit` 的 jsonl 有 → v1 不显示（见下） |
| 任务列表 | `session.metadata.todos`（`tools/todo.py:32` 的 `TODOS_KEY`）或 `init.notices` 里那行 |
| 已加载技能 | `session.metadata`（`skills/` 的 loader） |
| 累计 token / 本轮耗时 | **不在会话里**，只有审计 jsonl 有 |

**最后一行是一个真实的选择**：TUI 要不要读 `.tudouni/logs/<id>.jsonl` 来重建
"这个会话花了多少 token"？

- 要：多一层文件访问，而且要处理"日志比会话长"（上次崩在半路）的边界；
- 不要：恢复会话后底部统计是空的，直到第一轮跑完才有数。

**建议 v1 不要**（简单，且不显示比显示错的数字好），但要**在 UI 上说明**
"本轮统计从这一轮开始"。这一条和 `--audit` 那边"错的百分比比没有百分比更坏"
（`config.py:47`）是同一个取向。

### 9.2 一个进程一个会话

> **第二期改了这一节（决策 24）。** 下面这段是 v1 的原判断，保留着是因为它把
> **代价**说清楚了（那部分今天仍然成立）；但结论已经反过来：`/new` 和 `/resume`
> 现在是**原地换会话**，不重启进程。改动的落点是协议加了两条消息
> （`session_switch` / `session_list`），runtime 收到之后收掉自己的 runtime、
> 重新装配、重发 `init` + `session_load` + `ui(state)`——**界面进程活着**。
> 见 `doc/protocol.md` 的 3.6、`protocol/channels.py` 的 `_session_switch`、
> `protocol/serve.py` 的 `make_session_opener`。

`/new`、`/resume`、`/list` 这三条命令**在 v1 里都是"重开进程"**：

- `/new` → 前端杀掉子进程，不带 `--session` 重启；
- `/resume <id>` → 杀掉，带 `--session <id>` 重启；
- `/list` → 前端**独立起一个短命进程**跑 `main.py --list`（`cli.py:346`），
  解析它的输出。

理由（这是第三节 3.5 那条的后果）：`TodoBoard` 和 `SkillBoard` 都绑在
`session.metadata` 上（`main.py:253`、`main.py:282`），`session_notes` 闭包也捕获了
它们（`main.py:342`）。同进程切会话要重新装配**整张工具注册表**，
而 `ToolRegistry.register` 在重名时抛异常（`tool.py:163`）—— 也就是说得造一个新的
注册表、一个新的 `Agent`、还要保证旧的那个不被复用。

**能做，但不该在第一版做。** 一个会话一个进程的代价是"切会话要重启"，
而收益是"装配期不变"，后者是 `main.py` 整个设计的前提（薄入口、单向装配）。

**第二期为什么又做了**：上面那段"能做"的两个前提都变了。

  1. 装配**已经只有一处**了（`runtime/composition.py` 的 `open_runtime`，由
     `protocol/serve.py` 的 `make_session_opener` 包成一份工厂）—— v1 当时担心的
     "两个入口各装出一套不一样的运行时"现在不成立：换会话和开第一个会话走的是
     同一个函数；
  2. 用户按 `/new` 的**那一刻**正是最不该退出重来的时候。代价那一侧也不是白付的：
     换会话要重新连一次模型 client 和 MCP 子进程，**而且会等当前这一轮跑完**
     （和 `shutdown` 同一条规矩，理由见 protocol.md 第 5 节）。

### 9.3 中断（Ctrl+C）

**必须协作式，不能杀进程。**

理由：`messages` 的**不变量**（README「几条硬约束」第一条）——
一条带 `tool_calls` 的 assistant 消息后面**必须**紧跟全部对应 id 的 tool 结果，
否则 API 直接 400，而且此后每一轮都发不出去。

现有代码只有一个**每步**安全落盘点，就是 `agent.py:548` 那个 ★（代码自己的注释
说的是"唯一的**常规**落盘点"—— 另外三处落盘是 `agent.py:451` 追加用户消息之后、
`agent.py:529` 给出最终回答时、`agent.py:560` 抛步数上限之前，它们都不在
"assistant 带 tool_calls 但结果还没跟上"那个危险窗口里）。所以：

| 中断时刻 | 处理 |
|---|---|
| 模型往返中 | **现在能停了**（流式接上之后）：`on_delta` 每收到一块就问一次 `should_stop`，所以按了 Esc 立刻停。**这一条是本文写完之后补的**，见下面那段 |
| 工具执行中 | **中断不了**（`tool.execute(arguments)`，`agent.py:748`，直接调 handler，Agent 里没有任何工具级超时；`shell.run` 在 `tools/shell.py` 里同步阻塞）。要么给 `Tool.handler` 加取消令牌（新契约），要么就等它跑完 —— **选后者**，并在 UI 上明确显示"正在执行，等待完成" |
| 两步之间 | **安全**：这里 `messages` 一致，可以走 `run_finished(stop_reason="cancelled")` 然后回 `IDLE` |

**决策 1 认下的第三笔代价（"没有随时打断"）已经还掉了。** 还的方式就是本文预言的
那一条：**在 `on_delta` 里抛异常就是中断点**。要点：

- 抛的那一刻 `messages` 是一致的（assistant 消息要等 `complete()` 返回才 append），
  所以**半截正文不进历史** —— 这是认下的取舍：屏幕上那半句在会话里查不到，
  另一条路（把被砍断的答案当 assistant 消息存下来）更坏，因为恢复会话时它看起来和
  一次正常回答一模一样，而模型接下来会拿它当自己说过的话；
- 收尾照常（`run_finished(cancelled)` + 落盘），否则界面会一直等一条永远不来的事件；
- **关掉流式（`--no-stream`）时这两条都不成立**，那时仍然只有"两步之间"一个中断点，
  所以按钮文案要分这两种情况（`doc/protocol.md` 第 5 节）。

**v1 的实现**：`shutdown` 消息 = 置一个标志，让 `agent.py` 的步循环在**顶部**
（那个 ★ 之后、`messages` 一致的位置）检查它，然后走
`run_finished(stop_reason="cancelled")` 并 `raise` 一个专门的异常类型。

**那个异常必须继承 `BaseException`**（像 `KeyboardInterrupt` 那样）—— 继承
`Exception` 的话会被 `_run` 里那个 `except Exception`（`agent.py:772`）吞掉，
变成"工具执行失败：RunCancelled"，然后会话继续跑，而用户以为已经停下了。
**这条对流式也一样成立**（适配层的 `except Exception` 也抓不到它 —— 有一条测试
专门盯着它）。

**一个必须补的事件**：`run_finished.stop_reason` 现在只有
`answered` / `max_steps` / `model_error` / `model_fatal` 四种（都在
`_finish_run` 收口，`agent.py:238`）。取消要加一个 `cancelled`，否则 UI 只能显示成
一个它认不出的 `stop_reason` —— 而 `--audit` 那边也会少一种情况。

---

## 十、目录与依赖：三层（决策 18–23）

前端会长到三个（CLI、TUI、Web），所以"前端"必须是一**层**，不是"cli.py 加一个
TUI"。这一节是决策 18–23 的落地形态。

### 10.1 布局

```text
agent_runtime/                      ← 唯一可导入的包，一个 venv
│
├── main.py                         ← 极薄启动器：--tui / --runtime-stdio / 老 CLI
├── args.py                         ← build_parser()：从 cli.py 提出来（runtime 的公开面）
│
├── runtime/                        ← 【第一层：装配】只造对象，不解释任何消息
│   ├── composition.py   Runtime    ← 唯一持有 http / mcp / store / logs / skills /
│   │                                 policy / memory / tools / agent；有 close()
│   ├── config.py                   ← 从根上搬进来
│   ├── notices.py                  ← 7 个 report_* 变成结构化数据（见 10.4）
│   └── events.py                   ← 7 种 kind 的字段清单 = 协议的输入侧
│
├── protocol/                       ← 【第二层：跨进程契约】runtime 的"远程 API"
│   ├── messages.py                 ← 消息形状（入站 4 种 / 出站 6 种）+ v 常量
│   ├── codec.py                    ← 一行 JSON ↔ 消息对象；**唯一的编解码器**
│   ├── state.py                    ← 事件 → 状态（那张推导表，纯函数）
│   ├── channels.py                 ← ProtocolServer：把传输、runtime、asker/questioner 绑起来
│   ├── transport_stdio.py          ← Transport：stdin/stdout
│   ├── transport_http.py           ← （Web 期再加）
│   ├── serve.py                    ← 只做 stdio 那一件事的进程主循环
│   └── schema/*.json               ← 机器可读的消息形状（见 10.3）
│
├── frontends/                      ← 【第三层：界面】各前端之间**不共享代码**
│   ├── cli/                        ← 现有 cli.py（含四个"不需要模型"的子命令，决策 20）
│   ├── tui/                        ← Python + Textual
│   │   ├── client.py               ← 起子进程、逐行读 JSONL（唯一碰 subprocess 的地方）
│   │   ├── app.py                  ← Textual App
│   │   ├── widgets.py              ← 会话视图 / 权限面板 / 提问面板 / 状态栏
│   │   └── view_state.py           ← **只放显示状态**：滚动、折叠、焦点
│   └── web/                        ← （将来）TypeScript
│       └── src/
│
├── agents/ models/ tools/ state/ security/ audit/ skills/ prompts/   ← 内核，全不动
└── doc/protocol.md
```

这一节和 7.3 是同一份东西的两个视角：7.3 讲"哪些文件搬到哪"，这一节讲"为什么这么分"。

### 10.2 依赖方向，和那三条要新加的测试

```text
runtime/*    → 内核（agents / models / tools / state / security / audit / skills）
               + tools/mcp（McpToolset 的进程生命周期）
protocol/*   → runtime/*（装好的 Runtime）+ 内核的类型（Session / AskUserArgs / Tool）
frontends/*  → protocol/messages + protocol/state + protocol/schema
内核          → ✗ 不许 → protocol、frontends
frontends/*  → ✗ 不许 → runtime/*
```

**关键那条是最后一条：前端不许 import runtime 内部。** 只要它成立，前端就只是
"协议的一个客户端" —— 这正是能长出 Web 的性质。原来那条"`ui` 不许被内核 import"
太窄了（它只管一个方向，而且把 UI 当成一个包而不是一层）。

`tests/` 里要新加三条（顺着现有那条 skills 方向测试的写法）：

1. 内核不许 import `protocol` / `frontends`；
2. `frontends/*` 不许 import `runtime.*`（**决策 18**）；
3. `protocol/` 和 `frontends/` 里除了 `frontends/tui/app.py` / `widgets.py`，
   **任何模块都不许 import textual** —— 它保证老 CLI 和子进程不加载一个 TUI 框架。

注意 `tests/test_imports.py` **现在盯的比想象中窄**：它有一条"每个模块都能导入"的
冒烟测试，以及一条只管 `skills/` 包的方向测试
（`test_the_skills_package_imports_no_internal_module`）—— **没有任何测试守着
"tools 不许 import agents"这类边**。所以上面三条是**新加**的，不是既有机制的延伸。

### 10.3 协议的三种读者，和"形状只有一份"

| 读者 | 它需要的 | 从哪来 |
|---|---|---|
| `protocol/codec.py`（Python，运行时） | 消息形状 | `protocol/schema/*.json` |
| `frontends/web/`（TypeScript） | **TS 类型和常量** | 由脚本从同一份 schema **生成** |
| 人（写新前端、审协议） | **语义**：什么时候发、收到怎么办 | `doc/protocol.md` |

**决策 21**：形状的权威放 `protocol/schema/*.json`，Python 从它读、TS 从它生成。
让 TS 手抄一份字段名就是"同一份事实写两遍"，而且这次连字段名比对测试都做不了
（一个 Python 测试读不到 TS 的类型）。

**`doc/protocol.md` 里不要再抄一遍字段表** —— 那是第三个来源，一定漂。它只回答
schema 回答不了的问题：什么时机、什么顺序、错了怎么办、哪些字段是可选的、
`allow_trust_all` 那类"客户端不许自己决定"的边界在哪。

代价说明白：多一个生成脚本要维护。**但 Web 一进来，这是唯一能避免"两边字段名
不一致"的做法** —— 而那种 bug 的表现是"偶发解析失败"，最难查。

### 10.4 `runtime/notices.py`：把 7 个 `report_*` 变成数据

现在 `main.py:73-155` 有 `report_permissions` / `report_todos` / `report_skills` /
`report_mcp`（分别定义在 `main.py:73` / `main.py:93` / `main.py:107` / `main.py:132`），
各自直接 `print(..., file=sys.stderr)`。**它们必须变成返回结构化数据**，
理由就是已经有三个前端：

- CLI 想打 `[权限] 按等级自动放行 low；点名免问 shell`；
- TUI 想在状态栏显示（决策 14 说只显示非默认项）；
- Web 想渲染成一张可展开的卡。

三个前端三个样子，但**内容只有一份**。所以 `Runtime` 暴露
`notices() -> list[Notice]`（`Notice = {level, code, text, data}`），谁来打印/渲染
各管各的。这是"同一份事实只写一遍"在一个横幅上的形态。

**`[MCP] 忽略了工作区里那份 mcp.json` 这种话尤其不能丢**（`main.py:149`）：它在 CLI 里
是一行 stderr，在 TUI 里必须是一条可见的 notice，否则那类"文件明明在却不起作用"的
症状在图形界面上完全不可见。

### 10.5 一个真问题：注入顺序（决策 22）

`asker` 和 `questioner` 是在 **`Agent` 构造时**注入的（`main.py:433`、
`main.py:283`），而它们要发消息、等回应 —— 那是 `ProtocolServer` 的能力。
但 `ProtocolServer` 需要 `Runtime` 才能工作。**环。**

解法是让 `Runtime` **不创建** channels，只接受一个"通道包"：

```python
# runtime/composition.py
class Runtime:
    @classmethod
    def open(cls, args, channels: Channels) -> Runtime: ...
        # channels 提供 asker / questioner，Runtime 在造 Agent 时用它们

# protocol/channels.py
class ProtocolServer:
    """既实现 Channels，又持有 Runtime —— 唯一把两侧接起来的地方。"""
```

于是顺序是：

```text
Transport（stdio）
  → Runtime.open(args, channels=server)      # server 先存在，但还没连上 runtime
  → server.attach(runtime)                   # 补上另一半
  → 循环
```

**不变式：`attach` 之前不可能收到任何请求。** 这不是巧合 —— 只有 runtime 会发请求，
而 runtime 要等 `open` 返回。这个不变式值得写一句注释，它是这套接法成立的全部理由。

### 10.6 `Runtime` 的生命周期（为什么它必须是"持有者"）

这次装配里有两样东西**不是纯对象、有进程生命周期**，它们决定了 `Runtime` 必须是
一个能被显式关闭的持有者，而不是一个纯函数：

| 持有物 | 什么时候必须关 | 漏了会怎样 |
|---|---|---|
| `httpx.Client`（`main.py:267`） | 会话结束 | keep-alive 的 socket 活到进程退出 |
| `McpToolset`（`main.py:333`） | 会话结束 | **我们起的 server 子进程会活过这个进程**（`npx` 起的 node） |

`McpToolset.close()`（`tools/mcp.py:738`）逐个关连接、失败只打一行不让收摊盖住任务结果。
它**不由 `create_tool_registry` 持有**（那是个纯装配函数、不碰 I/O，
`tools/builtin/__init__.py` 开头那段写了为什么），所以 `Runtime` 是它唯一合理的家。

这一条也解释了 `Runtime.open()` 为什么要能失败且**失败时自己收摊**：`main.py:340` 那段
"注册失败时必须把这个进程收掉再往外抛"就是它的原型。

### 10.7 两个例外（决策 19、20）

**决策 19：CLI v1 保留直连 runtime。** 按第 18 条它本该也是协议客户端，但现有 `cli.py`
的输出（横幅、提示符、每轮统计那行、`--audit` 的表格）是 README 用几千字描述过的，
改动风险集中在这里。所以 v1 不动它 —— 但要守一条纪律：**不许往直连那条路上加新功能**，
否则它永远不会消失。

**决策 20：四个"不需要模型"的子命令先归 CLI。** `--list` / `--skills` / `--audit` /
`--history` 现在在 `cli.py` 里，它们**只读 store / logs / 技能目录，不装配 Runtime**，
而且有意排在配置检查之前（"没配密钥也能查历史"）。v1 留在 `frontends/cli/`。

**但 Web 期要重新想**：如果 Web 要会话列表，那它该是协议里的一个请求
（`list_sessions`），而不是"前端自己去读那个目录" —— 后者会让每个前端各自实现一遍
目录扫描和容错，而 `JsonSessionStore` 的 id 白名单与容错只有一份。

---
## 十一、风险与代价

### R1：审计文件会变大，而且开始含内容

决策 5 把思维链写进审计，这是审计里**第一个"内容型"字段** —— 在此之前它只有数字、
枚举和 200 字符预览，所以读这份设计的人会下意识以为审计很小。量级估算和三条处置
写在第四节 D2，这里只补一句**怎么盯住它**：审计文件的大小应该像 `--audit` 里的
token 一样，是"能一眼看见"的 —— 所以 `--audit` 末尾那行汇总里应该加一项
"思维链 N 字符"，否则文件涨到几十 MB 之前没人会发现。

### R2：协议化会把 stderr 上的警告弄丢

现在 `Agent._warn`（`agent.py:278`）直接 `print`。这些行**会掉进子进程的 stderr**，
而 stderr 按 6.1 的约定"不参与协议" —— 前端要么把它接进一个原始日志面板（那它
就有救），要么干脆不接（那就真的没人听得见了）。无论哪种，它都**不再是界面上的
一个可见事件**，而"会话落盘失败（已忽略）"这种事必须被看见。

**处理**：`_warn` 要能同时进协议（D5 的 `notice`）。**但注意 `_warn` 是
`@staticmethod`**（`agent.py:277`）—— 它没有 `self`，所以拿不到 sink。
这里要么改成实例方法，要么在外面装一个 `sys.stderr` 的转发器。
**建议前者**（显式比隐式好），代价是 `agent.py` 里几处 `self._warn` 调用点不用改。

### R3：协议通道是一条数据外流通道

**这一条比决策 3 之前轻得多，但没消失。** 工具参数和工具结果的全文**不再走**
协议（决策 3 否掉了全文通道），所以协议里剩下的内容型字段只有两个：

- `model_call.reasoning` —— 决策 5 让它进了审计，因此也进了协议。它里面会原样
  出现它读到的代码、路径、`ask_user` 的答案、网页和技能正文的片段；
- `permission_request.arguments` —— 审批面板要给人看全，所以它是全文
  （`asker.py:129` 那段已经为高风险工具做了同样的判断）。

所以：**v1 限定 stdio（父子进程管道），不开放任何网络监听。** 这条限制必须写进
代码注释和协议文档，不能只写在设计文档里 —— 半年后有人加一个 `--ui-tcp 7777`
的时候，他需要看到这条。而且那时候要重新算的，正是 `fetch_web` 为什么定 MEDIUM
的那笔账（`builtin.py:455`）。

### R4：老入口会被慢慢荒废

两条驱动（`run_repl` 和协议循环）共享 `Agent`，但**不共享展示逻辑**。
半年后很可能出现"CLI 能看到的东西 TUI 看不到"。

**处理**：**每一期的验收都要包含"再跑一遍 `uv run main.py`"**。
更根本的一点：`ui/` 里**不许有判定逻辑** —— 所有判定（该不该问、能不能并行、
步数够不够）都已经在 `Agent` 里了，`ui/` 只做投影。这一条守住了，
两条驱动就不会漂。

### R5：会话载入的体积

`session_load` 会把整份 `messages` 发过去，而 `read_file` **不分页**
（`builtin.py:241`），一个 8MB 的文件正文就在 `messages` 里。恢复一个长会话时
这一条消息可能几十 MB。

**处理**：v1 接受（本地管道，几十 MB 是几百毫秒），但**要在 UI 上显示一个
"正在载入会话…"**。真到了不能接受的那天，正确的解法是给那条消息做一个
"分段拉取"的协议，而不是去改会话格式。

### R6：没有流式 ⇒ 三轮观感问题必须靠 UI 自己补

> **这一节的前提已经不成立了**（流式做完了）：上表第 1、2、3 条各自换了形态 ——
> 模型往返那一段现在是逐字的正文，转圈只剩工具执行那一段；"停止"在流式开着时
> 立刻生效。**留着这一节是因为它当初定下的那条规矩仍然有效**：
> "没做某件事"必须在 UI 上被补回来，否则会被当成"坏了"。

决策 1 认下的三笔代价，都要在 UI 上补回来（否则"没做流式"会被当成"坏了"）：

| 现象 | 用户的直觉 | UI 必须做的 |
|---|---|---|
| 界面上有一段**完全静止**的秒级空档 | "卡死了" | 本地转圈 + 一行"当前在做什么"（6.4 末尾、第八节第 1 条） |
| 回答**整段蹦出来** | "刚才是不是卡了一下" | 拿到 `answer` 时给一个明确的落位（新气泡），不要覆盖状态行 |
| "停止"按下去**不能立刻停** | "按钮没生效" | 按钮文案写"停止（等当前步完成）"+ 中间态（9.3） |

**这三条不是打磨，是 v1 的验收项** —— 因为它们正是"没有流式"这件事唯一的用户可见面。

### R7：思维链进了审计，但 `--debug` 那份必须同时删掉

否则同一段正文有两条出口（stderr 和 jsonl），而 README 第 3 条设计原则明确反对
"同一份事实写两遍"。删 `--debug` 那段之后有一个**行为变化要写进 README**：
以前 `--debug` 能在终端看见思维链，现在要去 `--audit` 看（或者直接看日志文件）。
不提这一句的话，会有人以为"思维链功能没了"。

---

## 十二、按现有实现重排的阶段

初步思路的 5 个阶段里，3 个已经做完了（冲突 11）。按**现有缺口**重排 ——
只剩三期，因为决策 1 和 3 各砍掉了原来的一期；而决策 18 加上了一个"零期"。

### 第零期：抽 `Runtime` + 搬目录（**不改任何行为**）—— ✅ 已完成

**为什么它必须排在最前面**：协议层要有一个东西可以"服务"，而那个东西现在散在
`main.py` 里（465 行）。不先抽出来，第一期就得把协议代码写进 `ui/`、之后
再搬一次 —— 而"搬两次"的代价是两次 import 变更和两次 review。

- [x] `runtime/composition.py`：把 `main.py:191-461` 的装配变成 `boot()` +
      `open_runtime(...)` + `Runtime.close()`。`main.py` 只留下分派
- [x] `runtime/notices.py` 的职责并入 `Runtime.notices()`：7 个 `report_*` 变成数据
      （`Notice`，带 `stream` 标记保住那条历史契约）
- [x] `frontends/cli/args.py`：`build_parser()` 从 `cli.py` 提出来
- [x] `runtime/config.py`：`config.py` 搬进来
- [x] `frontends/cli/`：`cli.py` 搬进来（**行为不改**，决策 19）
- [x] `tests/test_imports.py` 加三条分层测试（10.2）
- [x] `run_repl` 改收一个 `Runtime`（而不是五个散参数）
- [x] **验收**：`tests/` 全绿；四条子命令和真启动都验过

**这一期没有新功能，所以它是可验证的** —— 那正是它值得单独成期的理由。

**期间发生的两件值得记下来的事**（都写在代码注释和测试里了）：

1. **分层测试第一次跑就抓到了我放错的位置**：`build_parser` 我一开始放进
   `runtime/`，而 CLI 前端要 import 它 —— 正好违反刚写下的决策 18。参数是前端的
   契约，所以它该在 `frontends/cli/args.py`。
2. **"逐字节对比基线"这个验收方式本身是错的**：PowerShell 的 `2>&1 |` 不保证跨流
   顺序（两个流各有各的缓冲），所以比出来的 diff 大半是噪声。改成按**每条流里有
   什么**来断言（`tests/test_banner.py::test_the_two_streams_carry_the_two_kinds_of_text`），
   而那正好就是 README 那句承诺的全部内容。

### 第一期：协议地基（无界面）—— ✅ 已完成

**目标**：`uv run main.py --runtime-stdio` 能和一个假前端来回对话。

- [x] `call_id` + `tool_index`（两条路径都带上了）
- [x] `model_call.reasoning` 进审计；**同时删掉 `agent.py` 里那段思维链 debug**
- [x] `ProtocolAsker` / `ProtocolQuestioner`（**三条通道都换掉了**，用交互式客户端验过）
- [x] `init` / `session_load` / `notice` + 每条消息的 `v`
- [x] 进程级警告协议化（协议层的 `notice`；`packaging` 那几条进 `init.notices`）
- [x] `run_finished` 那条 `t:"ui"` 的 `answer`
- [x] `stop_reason=cancelled` + 步循环顶部的检查点 + `RunCancelled(BaseException)`
- [x] `protocol/`：`messages` / `codec` / `state` / `channels` / `transport_stdio` /
      `serve` / **`client`**（客户端的公共层，见下）
- [x] `permission_request` 的 `allow_trust_all` / `trust_all_hint`
- [x] `init.notices` 替代 7 处 print（第零期做的一半，第一期补上 `code` / `level` /
      `is_default`）
- [x] `protocol/schema/*.json` + 分层测试 + **schema 漂移测试**
- [x] 一个 200 行的 ANSI 冒烟渲染器（`frontends/ansi/`）
- [x] **`doc/protocol.md`**（决策 13）—— 只讲语义，形状归 schema
- [x] **回归**：`tests/` 全绿；两条入口都验过

**验收**：`tests/test_protocol.py` 起真子进程喂 JSONL，把一轮"发消息 → 模型 → 审批 →
答案"跑完（用本地假端点，不碰真网关）。

**这一期完全不碰 Textual** —— 协议是独立的，先把它验穿。

#### 落地时发现的四件事（都改了设计）

1. **`serve()` 必须把回合放进工作线程 —— 这是正确性，不是性能。** 第一版把
   `agent.run(...)` 直接调在读循环里，结果是一个**必然的死锁**：Agent 要审批 →
   asker 发请求后原地阻塞 → 而那条回应只有读循环能读到，它正阻塞着。
   **每一次需要审批的调用都会撞上它**，而且不报错、只挂住。
2. **`shutdown` 不能顺手取消当前回合。** 客户端"把话说完就 shutdown"是最自然的
   用法，而两者混在一起时当前回合会在第一个安全点被取消 —— 界面永远拿不到答案，
   任何地方都不报错。现在：`shutdown` 只让循环退出，退出前把当前回合**跑完**；
   "中断"是另一条路（`request_stop`）。
3. **答案不能在 `on_event` 里发。** 看到 `run_finished` 时 `agent.run()` 还没返回，
   所以发出去的是空串。改到 `_run_turn` 拿到返回值之后发。
4. **客户端的公共层住在 `protocol/client.py`，不在 `frontends/`。** 它认的是
   "协议怎么走"（起子进程、拆行、回 `permission_response`），不是"界面怎么画"。
   放 `frontends/` 的后果是每个前端都要 import `protocol.messages` 去认消息种类 ——
   而前端只该实现四个回调。

**还有一件和第零期同源的教训**：`subprocess.run(input=...)` 那种"把所有话一次说完"
的喂法**验不了审批**（两种实现都会挂住，分不清是哪一种）。所以那条测试自己当客户端：
读一行、判断、再写一行 —— 这也正是第二期要做的事的缩影。

### 第二期：Textual 客户端 —— ✅ 已完成

- [x] `pyproject.toml` 加 `textual`（连带 rich / markdown-it-py / linkify-it-py /
      platformdirs 一串，见第五节那张表）
- [x] `frontends/tui/client.py` 的职责并进 **`protocol/client.py`**（见下面第 4 条）
- [x] `frontends/tui/app.py` + `widgets.py`：会话视图（用户消息 / 助手回答 /
      工具调用与结果行）
- [x] `frontends/tui/view_state.py`：显示状态 + **纯渲染函数**（可单测）
- [x] 权限面板：`[允许] [拒绝] [总是允许]` + `[都允许]`，按钮集合**跟着后端字段走**
- [x] 提问面板 / 输入框
- [x] `reasoning` 折叠成一行"思考过程（N 字符）"，`Ctrl+T` 展开
- [x] 状态栏：非默认权限 + 当前在做什么 + 第 N/M 步 + 会话 + 模型
- [x] `/exit` `/help` `/audit` `/new` `/resume` `/list`（决策 15 那五条 + `/help`）
      —— `/list` 在第二期删掉了（13.3.1），`/new` `/resume` 改成原地换会话
- [x] R6 那三条观感项（在做什么 / 答案落位 / 不能立刻停的措辞）
- [x] 子进程异常退出时报出来

**验收**：`scripts/verify_tui.py` —— 用 Textual 的测试台跑一次**真** TUI：
真子进程、真协议、真 HTTP 桩。三件事都过：

```
=== 握手 ===   会话 acceptance  模型 deepseek-flash  最多 80 步  工具 11 个
=== 一轮对话 ===  phase=finished  answers={'...': '我很好，谢谢。'}
=== 审批 ===   面板 PermissionPanel  按钮 ['allow', 'always', 'deny']
               工具结果：'退出码 0\nhi'      ← 点了允许之后命令**真的执行了**
=== 通过 ===
```

最后一行是**三条人机通道换掉了**唯一的外部证据。

`tests/test_tui.py` 是轻量的那一半（`FakeClient` 替掉子进程，验界面逻辑），
`tests/` 全绿；老路径（`main.py`、`--list`、`--skills`、`--runtime-stdio`）
都验过，而且 `--runtime-stdio` 子进程**不加载 textual**。

#### 落地时发现的四件事

1. **协议回调不能直接改界面，也不能用 `call_from_thread`。** Textual 的线程检查
   认不出我们的读线程，而它在 None 屏幕上还会抛 `ScreenError`（`init` 到达时界面
   还没挂载）。改成一个 **50ms 的消息泵**：回调只往 `queue.Queue` 里放东西
   （什么线程都能放），界面定时排空它。代价是最多 50ms 的排版延迟。
2. **`on_permission` 绝不能用兜底答案。** 第一版返回 `DENY` 当占位，想"稍后覆盖"
   —— 而客户端立刻把它发出去了：子进程据此拒绝并继续跑，用户点 [允许] 时那条回应
   已经没人要。**更糟的是中间那次拒绝进了审计，记成 `user_denied`**，也就是伪造了
   一条"用户拒绝过"的记录。修法是让钩子可以返回 `None` = "界面稍后自己回"。
3. **`App._ready` 这个名字不能用。** `App` 自己有一个 `_ready()` 方法，被一个 bool
   盖住之后 `run_test()` 报 `TypeError: 'bool' object is not callable` —— 从栈上
   完全指不到赋值那一行。
4. **客户端的公共层该在协议层，所以 `frontends/tui/client.py` 没有出现。**
   它认的是"协议怎么走"（起子进程、拆行、回回应），不是"界面怎么画"。放前端里的
   后果是每个前端都要 import `protocol.messages` 去认消息种类。

**还有两条 Textual 8 的 API 事实**（都是实测，写下来免得下次再踩）：
`RichLog.write()` **没有** `markup` 参数（要用 `rich.text.Text` 关掉 markup 解析）；
`Static` 上**没有** `.renderable`（要拿文本走 `render()`）。

### 明确不做（v1）

- **流式输出**（决策 1）—— 插入点见第五节末尾；连带**没有"随时打断"**（9.3）；
- **工具卡片 / diff 渲染**（决策 3）—— 数据在会话历史里，只是没画；
- **多会话同进程**（9.2）；
- **工具级取消**（9.3）；
- **网络化的协议通道**（R3）—— `protocol/transport_http.py` 是给**将来**留的位置，
  不是 v1 要做的事；
- **`/compact`**：上下文管理在 README「尚未实现」里明确写着"按实测成本，
  当前规模下截断不划算"（窗口 1M，实测典型长任务 19 步）。做它之前要先有
  "什么时候该压"的数据，而 `context` 用量已经在每轮末尾报了。

### 第四期那种事（滚动、折叠、`/` 命令、主题）什么时候做

初步思路的"第五步"仍然排在最后，理由不变（初步思路第十六节说对了：
"不要先研究怎么把 TUI 做得漂亮"）。但它的**门槛比原来低**：协议在第一期就定完了，
第二期只画界面，所以"改协议就意味着改界面"这个风险到第二期结束时已经基本消失。

---

## 十三、已经定下的实现规格（原"待决策"）

十七条决策之后，**没有悬而未决的问题了**。这一节留下的是那五条产品决定
**具体长什么样** —— 它们是第二期的施工图，也是"当初为什么这么定"的记录。

### 13.1 协议文档：第一期结束时抽 `doc/protocol.md`（决策 13 + 21）

现在协议只存在于本文第六节里。第一期结束时 `protocol/serve.py`（子进程）和
`frontends/tui/client.py`（父进程）会成为它的两个实现 —— 两侧都是 Python，所以
**类型和状态表只有一份**（决策 12 白捡的好处），但**"这条消息什么时候发、什么语义"
仍然只写在代码和本文里**。所以第一期结束时把它抽成 `doc/protocol.md`，和 README 里
「数据落在哪里」同档。

**但决策 21 把"形状"和"语义"拆开了，这一点必须照做**：

| 内容 | 放哪 | 谁读它 |
|---|---|---|
| 字段名、类型、枚举值、`v` 常量 | `protocol/schema/*.json` | Python 运行时；**TS 由脚本生成** |
| 什么时候发、什么顺序、错了怎么办、客户端不许自己决定什么 | `doc/protocol.md` | 人 |

**`doc/protocol.md` 里不要抄字段表** —— 那是第三个来源，一定漂，而且 Web 期会同时
存在三份（schema、md、生成的 TS）。它只写 schema 表达不了的东西：
`allow_trust_all` 为什么客户端不许自己决定放行名单（6.3）、`delta_reset` 那种
"将来才有的消息"该怎么被老客户端忽略、`shutdown` 为什么要等当前步走完（9.3）。

三条理由，按重要性排：

1. **第二期要照着它写客户端** —— 对着文档写，不用去读 `serve.py` 的实现；
2. **它是跨进程的接口**，不是实现说明 —— 比代码注释值得多；
3. **它是将来换客户端语言的唯一凭据** —— Web 版（以及万一要重写的 Ink 版）
   的作者只有这份文档 + schema 可看。

代价说明白：多一份要同步维护的文档。**缓解办法是让它成为唯一权威**：
`protocol/serve.py` 里那些消息类型和常量应当是从文档抄下来的、有测试钉住的，
而不是"文档跟着代码走"。

### 13.2 权限范围进状态栏，只显示非默认项（决策 14）

`init.permissions` 带过去四个键（`auto_approve` / `auto_approve_tools` /
`deny_tools` / `shell_allow`），README 里那条"每次启动都说一遍"的理由
（`main.py:76`）在 TUI 里只会更强 —— 因为图形界面不像终端那样天然把这几行留在
屏幕上。

**具体做法**：状态栏一行可展开，**只列与默认值不同的那些**。默认是
`auto_approve=("low",)` 且其余三个为空（`config.py:164` 的 `DEFAULT_AUTO_APPROVE`，
搬完之后是 `runtime/config.py`），
所以**默认情况下这一行是空的、不占注意力**；只有你按过 `t` 或手改过
`.tudouni/permissions.json` 时它才有内容。

**比对基准从哪来**：不要在 UI 里硬编码一份默认值 —— 那是第二份事实。
让 `init` 多带一个 `permissions_default`，或者更简单：**让子进程只发"非默认项"**
（它本来就知道自己加载了什么、默认是什么），UI 拿到什么就显示什么。
**建议后者** —— 判断"什么算非默认"是 `config.py` 的知识，不是界面的。

**MCP 那一档要一起想**：`runtime/notices.py` 里 `[MCP]` 那几行（连上了几个 server、
各提供几个工具、以及"工作区里那份 `mcp.json` 被忽略了"）是同一类"这次启动的事实"，
`main.py:132` 的 `report_mcp` 现在直接打 stderr。它该进 `init.notices` ——
而且**"外部工具每条都要审批"那句话必须留着**（`main.py:147`）：它是"为什么它又问我了"
唯一的解释。

### 13.3 `/` 命令集 v1（决策 15）

| 命令 | 做什么 | 对应现在的哪个开关 |
|---|---|---|
| `/new` | 重开进程，不带 `--session` | 等价于 `uv run main.py` |
| `/resume <id>` | 重开进程，带 `--session <id>` | 等价于 `--session` |
| `/list` | 列出已保存会话（供 `/resume` 挑） | 等价于 `--list` |
| `/audit` | 打出这一轮的审计摘要 | 等价于 `--audit`，**只针对当前会话/当前轮** |
| `/exit` | 退出 | 等价于 `exit` / `quit` |

三条说明：

- **前三条都是"重开进程"**（9.2），因为 `TodoBoard` / `SkillBoard` 绑在
  `session.metadata` 上，同进程换会话要重新装配整张工具注册表；
- **`/audit` 必须做**，尽管它看起来像调试功能：R1 那个"思维链 N 字符"是审计体积
  唯一能被看见的地方，而没有它，审计涨到几十 MB 之前没人会发现；
- **`--autopilot` 曾经刻意不给 `/` 命令** —— 它是"这一次没有人可问"，而在一个有人
  看着的 TUI 里按它，语义是矛盾的。**这条判断已被决策 25 推翻**（见 13.3.2）。
  想无人值守仍然直接 `uv run main.py --autopilot`。
  同理 `--debug` 也不进 TUI：它打的是 stderr，而 stderr 在 TUI 里由子进程继承、
  直接落在终端上（6.1），本来就不是界面的一部分。

**没进 v1 的**：`/skills`、`/history`、`/help`。前两个各有独立的终端子命令
（`--skills` / `--history`），在 TUI 里是"再画一个面板"的事，属于第四期；
`/help` 等命令多到需要它的时候再说。

#### 13.3.1 第二期改的样子（决策 24）

| 命令 | 现在做什么 | 界面上的形态 |
|---|---|---|
| `/new` | **原地换一个新会话**（发 `session_switch` + `session_id=null`） | 会话流清空、重画新会话的空态；左栏跟着换 |
| `/resume` | **不带参数**：弹出会话面板（`↑↓` 选、`Enter` 切、`Esc` 取消）；**带 id**：直接切 | `SessionPicker`，每行 = id + 条数/步数 + 第一句话的预览，当前会话带 `●` |
| ~~`/list`~~ | **删掉了** | 它和"`/resume` 不带参数"是同一个出口，而两条命令指向同一件事时，人要先猜哪一条才对 |

两处**刻意**的做法，改动时别顺手"优化"掉：

- **发出 `session_switch` 之后界面什么都不做**（连"正在切换…"之外的一句话也不加）。
  清屏只发生在 `init` 到达之后 —— 换会话可能失败（配置坏了、id 非法），而 runtime
  失败时**原样保留旧会话**。界面抢先清屏就会把一次失败变成"我的会话没了"；
- **列表按创建时间排、最新的在最上面**（`session_summaries`），默认选中就是第一条。
  判据是 `session.metadata["created_at"]`（新建会话时写进去）—— **不是 id 倒序**：
  自动分配的 id 恰好是时间戳，所以按 id 排在那批会话上看不出问题，而 `--session demo`
  这种自己起的名字会被排错位置。老会话文件没有那个键时退到文件 mtime；
- **`Ctrl+R`（重开会话）撤掉了**，连带 `KeyHintBar` 里那一格。它的存在理由是
  "v1 换不了会话，所以给你一句提醒"；现在提醒变成了一条真命令，留着它只会多一个
  和 `/resume` 抢注意力的键。

**命令后面那句话是一句短语**（`Command.hint`），不是说明书：它同时是面板里那一列和
`/help` 那一列，而**一行的宽度就是它的预算**。所以"带了参数会怎样""有几套配色"
"立刻生效不用退出"这类要么在那一行里读得出来、要么属于别处（`/help` 末尾那几行）
的东西，一律不进那一段。13.3 那张表里的"做什么"是给维护者看的，不是界面文案。

#### 13.3.2 第三期：`/autopilot` 与状态栏那一格（决策 25）

```text
● 要调用 read_file（第 2 个）    第 1 / 80 步      自动放行 关  ·  上下文 12.4k / 1.0M（1.2%）  ·  命中 88%
```

**那一格永远在，两个状态都写出来**（`自动放行 开` / `自动放行 关`），不做成"开着才
显示"：那样"这一格空着"既可能是关掉了、也可能是没画出来，而这一格的语义是"接下来
还会不会问你" —— 它不允许有歧义。开着用 `warn`（工具会在没有人点头的情况下执行），
关着用最暗那档。**窄屏缩成两个字**（`放行 开`）：状态栏右边是 `width: auto`，多出来
的每一列都从左段身上扣，而左段被裁成半句正是 F5 那次踩过的坑。

三条**刻意**的做法：

1. **界面不许乐观更新。** 发完 `set_autopilot` 只等那条 `ui(state)` 快照，在它到达
   之前灯不变、话也不说。"灯亮着、其实还在逐条问你"会让人把真的审批面板当成误报
   点掉 —— 那比晚半秒亮灯贵得多。同理 `_report_autopilot` 说的是**快照里的值**，
   请求没被受理时它什么都不说（说"已开启"是假话）；
2. **发的是绝对状态**（`on: true/false`），不是"切一下"。于是重发幂等，界面也不必
   先知道 runtime 现在是什么状态 —— 竞态从设计上就没有了；
3. **换会话跟着走。** 开关同时写进 `bootstrap`（新会话照它装配）和当前 `Agent`
   （gate 每次都读它）。只改一处的话，`/new` 之后就会出现"灯还亮着、工具却开始问"
   这种两边各说各话的状态。

**它管不着什么**（免得被读成"关掉权限"）：工作区边界、控制面写入、拒绝名单照旧；
**`ask_user` 照旧问界面** —— 人在键盘前，它就该问。审计里每次放行记的是
`outcome=autopilot` 而不是 `approved`，所以"这次会话到底有没有人看着"事后仍然答得
出来：`--autopilot` 和 `/autopilot` 都是"没有逐条点头"，而不是"有人按过同意"。

### 13.4 `t` 的呈现：三个按钮 + 一行后果说明（决策 16）

```text
╭─ 需要权限 ──────────────────────────────╮
│ shell  风险 high                        │
│ command = rm -rf build/                 │
│                                         │
│ t = 以后每次都直接执行，你不会再看到它   │
│     要做什么（写进 permissions.json，    │
│     下次启动仍然有效）                   │
│                                         │
│ [允许]  [拒绝]  [总是允许]               │
╰─────────────────────────────────────────╯
```

**那行后果说明必须从子进程传过来**（`permission_request.remember_hint`，
内容出自 `asker.py:95` 的 `_remember_hint`），UI **不许自己写**。
理由：它是"按下去会发生什么"的唯一说明，而高风险工具那一句
（"以后每次都直接执行，你不会再看到它要做什么"）**不能省** —— 它和低风险那句
（"以后不再询问这个工具"）的后果差着量级，用同一句话盖住就是省掉了最要紧的半句。

`remember_hint` 为 `null` 时（命令行里推不出前缀，见 6.3）**三个按钮要变成两个**，
[总是允许] 必须消失 —— 不要自己补一个"总是允许整个 shell"，那正是
`asker.py:120` 那段刻意堵掉的东西。

### 13.5 `reasoning` 默认折叠（决策 17）

决策 5 让它进了审计，所以它**一定拿得到**；问题只是默认怎么显示。

**具体做法**：默认一行 `▸ 思考过程（1840 字符）`，点开看全文。

理由和 `--debug` 那边"思维链整段打、`content` 只给预览"的取舍是同一条
（`agent.py:486` 那段）：思维链是**排查**用的，不是每次都要读的；而它的长度常常
是答案的好几倍，默认铺开会把回答挤到屏幕外。

**一处实现细节**：折叠状态下显示的字符数从哪来 —— 直接 `len(reasoning)`
（它是 Python 字符串，`len` 是 O(1)），**不要**让子进程再算一个
`reasoning_chars` 发过来。那是同一份事实的第二个来源，而 U+4E00 之外的字符
（emoji、代理对）在两侧的计数口径未必一致 —— 一个"字符数对不上"的 bug
查起来毫无价值。

---

## 十四、v2：设计稿改版（2026-09 落地）

v1 上线之后又做了一轮**界面设计**（九张界面稿 + 两页配色方案 + 一份设计系统），
落地时改了七处。这一节记的是"改了什么、为什么"，以及**三处和前面决策冲突的地方** ——
冲突的地方按人拍板的新决定办，但理由要留档。

### 14.1 七处改动，按改动量从小到大

| # | 改动 | 落到哪 |
|---|---|---|
| 1 | **工具行有自己的语法** | `view_state.render_event`：`→ [n] tool(args)` / `← [n] ✓ 8,412 字符 41ms`，`[n]` 是 `tool_index`，`←` 用 `call_id` 配对（决策 4 白捡的） |
| 2 | **风险靠颜色，不靠文字** | LOW 不着色也**不写"低风险"**（它占多数，写出来只是噪声）；MEDIUM → warn、HIGH → danger。`init.tools` 里的 `risk` 前端早就拿到了 |
| 3 | **回合分隔线** | 按 `run_id` 分组，一条头写着"进行中 · 第 1 步"，`run_finished` 到了回填成"3 步 · 4.2s · 已答" |
| 4 | **思维链按回合折叠** | `Ctrl+T` 作用于**光标所在回合**（`turn_under_viewport`），不再是 `list(state.thinking)[-1]` |
| 5 | **上下文栏（左栏）** | 任务 / 已加载技能 / 权限范围 / 本次会话四块，**外加后台任务一块**（后台是后加的，排在最后，见 14.3），**以及 MCP 一块**（见第十七节）。**数据来自新加的 `ui(kind:"state")`**，见 14.3 |
| 6 | **命令面板浮层** | `/` 打开、↑↓ 选、Enter 执行。命令集本身不动（决策 15 那六条还在原位，新增三条排在末尾） |
| 7 | **对话流按回合分块** | `RichLog`（只追加）→ `VerticalScroll` + 每回合一个 `TurnBlock`。**这一条绕不过去**：回合头要能被回填、思考块要能单独展开，只追加的日志没有"块"这个概念 |
| 8 | **配色层** | 13 套主题（见 14.2），`/theme` 运行中换 |
| 9 | **四处视觉锚点** | 左栏每块一条色条、思考正文做成引用块、选项选中项反白、空态一块方块标（见 14.6） |
| 10 | **输入框两行 + 上下高亮线** | 单行 `Input` → 两行 `TextArea`（软换行），回车发送、`Shift+Enter` 换行（见 14.6） |

代价说明白（改动 7）：滚动和裁切从"框架给的"变成"自己管的"，多出来的是
"哪些行属于哪个回合"这一份结构。**回报是 `view_state` 的纯函数形态保住了** ——
它的返回类型从 `list[str]` 升级成 `list[Line]`（带 role / 分段着色的 `str` 子类），
所以 v1 那些断言（`"没有执行" in line`）一行都没改。

### 14.2 配色：13 套，默认 ⑥ 石墨琥珀

设计稿先给了五套候选（F7），之后又给了九套色卡（F8/F9，每套从 5 色扩到界面九色）。
**落地的是 13 套**：色卡里留下 `P3 / P5 / P6 / P7 / P9` 五套（`P1 暖橄榄 / P2 海蓝橙 /
P4 暖光 / P8 藏青陶土` 四套是用户后来裁掉的，**key 里的号不重排**）、F7 的五套
（`A`–`E`），外加三套**透明版**（浅的 `P3-T` / `A-T`，深的 `A-T2`，见下）。
全部在 `frontends/tui/theme.py` 里是纯数据（一个 textual 的 import 都没有 ——
所以它不需要 `_TEXTUAL_ALLOWED` 那个口子）。

三条规则，对每一套一视同仁（**手写第二套色值是最容易犯的错**：13×8 = 104 个没人
校对过的颜色，而且每套的观感会开始漂）：

| 规则 | 用在 | 做法 |
|---|---|---|
| 抬高明度 | rail / elevated | 往正文色插值（深色主题变亮，亮色主题变暗 —— 方向由 `dark` 决定） |
| 压低明度 | sunk（思考块底） | 往底色之外的端点插值 |
| 弱化 | hairline / ink4 / accent_soft / danger_soft / skill | 往底色插值：保留色相、降对比 |

**默认是 ⑥ 石墨琥珀**（人后来改的板，取代了本节最初定的 ⑦ 靛夜）。⑦ 靛夜本身没动，
它的 `accent` 仍然必须是提亮过的 `#7670EF`：色卡原色 `#463DE8` 在 `#161616` 上只有
2.3:1，肉眼几乎看不见。

**透明版（`A-T` / `P3-T` / `A-T2`）**：很多 CLI 的 TUI 看起来是"透"的 —— 终端底板
什么色，对话区就什么色。做法是把那套配色的 `bg` 换成 `ansi_default`（"用终端自己的
底色"）。**透到哪几格由 `Palette.clear_roles` 记着**，现在有两档：

| 哪一格 | `A-T` / `P3-T`（浅） | `A-T2` 深透明 | 为什么 |
|---|---|---|---|
| `bg`（窗口底 / 对话区） | **不涂**（`ansi_default`） | **不涂** | 这一大片透出来，就是"终端底板什么色、它就是什么色" |
| `chrome`（顶栏 / 会话头 / 状态栏 / 输入框） | 照旧实色 | **也不涂** | 浅的那两套上最显眼的其实就是这四条横色带 —— 它们和对话区不是一片，深的那套把它们一起交出去，横栏于是和对话区连成一整块 |
| `surface`（开场那三个框：开始 / 最近 / 提示） | 照旧实色 | **也不涂** | 那三个方块是开场唯一占面积的东西（`clear_roles = ("chrome", "surface")`）。**代价**：`surface` 同时是 Textual 自己那套变量里的 `$surface`，而 `Button.-style-default`（弹层里的「始终允许」「跳过」）拿它当底 —— 深透明那套里那两个按钮于是变成只有上下半格边框的扁按钮（成功 / 拒绝那两个用 `$success` / `$error`，不受影响） |
| `rail` / `elevated` / `sunk` | 照旧实色 | 照旧实色 | 左栏、命令面板、思考块靠底色分层；全透了就只剩文字和边框，左栏那种"安静的一块"会消失 |
| 派生角色（rail_bar / hairline / …） | 按**原版那套的 `bg`** 算 | 同左 | `ansi_default` 不是一个能插值的色值；而且这样"透明版"和"原版"的差别只剩交出去的那几格 |

代价说清楚：透出来的是终端底板，**对比度就不再由我们保证**了（深色主题配亮底板、
或者亮色主题 `P3 粉紫` 配深底板都会难读）。这是这个功能的性质，不是能在这里修掉的
东西。

**落地时踩到的一处坑**（记在这里，因为它不直观）：`ansi_default` 的意图要活到最后，
得同时成立四件事 —— `theme.py` 里那一格是 `ansi_default`、`Screen.styles.background`
保住 `ansi=-1`、`$td-*` 变量别被解析成色值，以及**Textual 默认挂的
`ANSIToTruecolor` 过滤器不许把 `default` 底色换成真彩色**。最后一条最阴：它把每一个
没有 triplet 的颜色按自己那套终端主题猜一个（MONOKAI 猜 `#0C0C0C`），于是屏幕上出现
`48;5;232` —— 一块具体的黑，而不是"不涂"。前三处都对、只差这一处时，画面上看起来
**仍然是一块实心**（只是从套色变成了近黑），所以它值得一条专门的测试
（`test_the_clear_variant_leaves_the_screen_background_to_the_terminal`）。修法是
`app.py` 里一个只放过底色的过滤器子类 `_KeepDefaultBackground`
（前景色的 `default` 照旧换，那是 Textual 控件的常规行为）。**深透明那套白捡了这个
修法**：`chrome` / `surface` 交出去的也是"底色"（横栏和那三个框的 `background`），
所以它走的是同一条路，一行过滤器代码都不用加 —— 验收在
`test_the_deep_clear_variant_paints_neither_the_bars_nor_the_input_box`。

**框线不在"透明"的范围里**：透的只有"面"（`bg`，以及深透明那套的 `chrome` /
`surface`）。输入框上下那两条 `accent` 线、开场那三个框的圆角边、回合分隔线、
左栏色条、正文左边那条竖线**照旧是主题色** —— 否则界面上就只剩缩进和空行，分块
全靠猜。这条也写进了上面那条测试。

**F6 的 `11 / 12 / 14` 三档字号在终端里不存在**（一块画布只有一种字号），所以那三档
落到**颜色**上：正文 `ink`、过程行 `ink3`、区块标题与键位提示 `ink4`。这不是翻译
损失，是同一个层级意图在另一块画布上的形态。

### 14.3 协议为此补了三样（都记在 `doc/protocol.md`）

| 补的东西 | 为什么非补不可 |
|---|---|
| `init.context_tokens` | 状态栏那句"上下文 14.1k / 1M（1.4%）"要有分母，而模型响应里**没有**这个字段（它来自 `config.CONTEXT_WINDOWS` 那张表）。表里没有就是 `null`，界面只报用量 |
| `ui(kind:"state")` | 左栏那几块的数据住在**子进程**里（`session.metadata` / `PermissionPolicy` / 那张攥着进程的后台任务表），而 TUI 是另一个进程。没有这条消息，设计稿里那块"最值钱的加法"没有数据 |
| `interrupt`（入站） | `Esc` 要能停下正在跑的这一轮。**注意**：取消的机制（`RunCancelled` / `should_stop` / `stop_reason=cancelled`）**早就在**，只是从来没有一条协议消息去触发它 |

### 14.4 三处和前面决策的冲突（按新决定办）

| 冲突 | 原决策 | 现在 | 理由 |
|---|---|---|---|
| **中断** | 9.3 与 6.2 都写着"v1 不给工具做取消、没有 interrupt 消息" | **加 `interrupt`** | 设计稿的键位表把 `Esc` 定成"中断本轮"，而人拍板要做。落地方式是**复用已有的安全点**（两步之间），不是新造一套取消 —— 所以"随时打断"仍然没有 |
| **命令集** | 决策 15：`/` 命令 v1 就那六条 | **加密三条**：`/theme` `/skills`，而 `/help` 的内容跟着长 | 面板需要可切换的配色和技能清单。那六条的**位置和语义一个字没动**（设计稿 F2 的面板就是它们） |
| **上下文栏默认态** | 设计稿画的是"常显"（F1/F2 两张图里栏都开着） | **默认收起**（决策 1），但任务列表出现时顶开一次（决策 26） | 栏宽 32 列，80 列的终端里那是 40%。决策 1 原来的折中是"`≥120` 列且真的有任务/技能时才自动展开、窄屏降级成一行摘要"，决策 26 把这个折中改成"**任务列表从无到有就顶开一次**（宽度不参与判断），之后 `Ctrl+B` 说了算" —— 因为"它打算做哪几件事"是用户唯一能提前看出"它理解得对不对"的地方，比省那 32 列要紧 |

### 14.5 明确没做的（免得被当成漏了）

| 没做 | 为什么 |
|---|---|
| 窗口边框 / 阴影 / 圆角 | 设计稿里那圈圆角窗口是**终端窗口本身**（F6 写着"阴影：只有窗口与弹层投影"）。终端里那由终端程序画，TUI 不自绘外框；面板之间靠描边和明度分层（这一条做了） |
| 工具结果卡片 / diff / 文件树 / 会话列表 | **决策 3 未变**。事件里 `arguments` 只有 200 字符预览，而全文在会话文件里 —— 所以照旧"要看内容去会话历史" |
| 亮色主题的打磨 | `P3 粉紫`（和它的透明版 `P3-T`）是亮色（`dark=False`），它**能选、能跑**，但没有单独校过终端下的观感 —— 设计稿自己也说"值得单开一屏补" |
| 更多透明版 | 现在有三套（`P3-T` / `A-T` 浅的、`A-T2` 深的），都是用户点名的。它是一条通用规则（`_transparent(base, …, clear_roles=…)`），要加别的套只是再调一次 —— 但那意味着更多"深色主题配亮底板"的组合，不如按需加 |
| 输入框再长 | 现在是**两行 + 软换行**（见 14.6），写一整段话够用；"贴一整份文件进去"那种要十行，而十行的框会把会话流挤到不足十行 —— 那件事该走 `read_file`，不是走输入框 |

### 14.6 五处视觉锚点（终端做得到的"质感"）

设计稿里靠圆角、阴影、字号做出来的层次，终端给不了（见 14.5 那两张表）。能补的是
**结构性**的锚点 —— 五处都是同一个思路：用一个**字符级的形状**（色条、竖线、块字符、
方块标、边框线）去承担层次，而不是去模拟像素效果。

| 锚点 | 做法 | 为什么是这么做 |
|---|---|---|
| 输入框 = 两行 + 上下高亮线 | `#input-box { height: 4; border-top/bottom: solid $td-accent }`，里面 `TextArea` 两行软换行 | 上下两条 accent 线是**这一屏唯一的高亮边框**，因为它是"现在要你操作的地方"。两行是真的可编辑两行（长句不再横向滚走），顺带把 F6 的 `Shift+Enter 换行` 补上了 |
| 左栏每块一条色条 | `.rail-block { border-left: solid $td-rail-bar }` | 每块共用一条竖线，眼睛顺着它就能数出"这一栏有几段"。颜色是 `rail_bar = blend(line, bg, 0.30)` —— **弱化过的**主题描边色：每块一条满血描边色会跟正文抢眼睛，而锚点该是安静的那一层 |
| 思考正文 = 引用块 | 底色 `sunk`（控件给）+ 每行 `  │ `（`view_state.quote_line` 给） | **底色划范围、竖线定边界**，两根一起才像一个块。首屏和 `Ctrl+T` 展开两条路径共用同一个构造函数 —— 各写一遍的话，反复折叠会"长出"两种长相 |
| 选中项 = `▌` + 反白 | `.option.selected { background: $td-accent; color: $td-bg }` + `width: 1fr` | 用反白而不是"强调色当底 + 手调正文色"：反白在**每一套主题**下都自带对比，不用对 13 套逐一校对（透明版的 `$td-bg` 是 `ansi_default`，反白那一格的底色于是也交给终端 —— 这是这套做法白捡的好处）。`▌` 是给单色终端的形状信号；`width: 1fr` 是让那块底**铺满整行**的关键（不铺就只有文字那么长） |
| 空态一块方块标 | `WelcomeBlock.LOGO`：三行，只用 `▄▀█` | 终端里没有图片，而一屏空态没有任何视觉重量时，"这是哪个程序"得靠它承担。**只用半块/全块字符**：它们在一个等宽字体里都是"一格宽"的实心块，不会像某些图形字符那样在 CJK 字体下变双宽而把右边的字顶歪（三行还刻意等宽，右边的 `tudouni` 才对得齐） |

## 十五、v2.1：`/status` `/tools` `/model`（2026-09）

这三条加在命令集末尾（13.3.2 那条"加密三条"之后），**位置在末尾是有意的**：前面那些
是用户已经见过的肌肉记忆，新命令插在中间会把它们整体挪位。

| 命令 | 数据从哪来 | 界面形态 |
|---|---|---|
| `/status` | `Runtime.status()` + **读一遍审计日志**（`state/status.summarize`） | 八行进会话流（`view_state.render_status`） |
| `/tools` | `Runtime.tool_rows()`（策略、记忆、注册表三处合起来算） | 每工具一行 + 结尾那行命令规则（`render_tools`） |
| `/model` | `state/model.py` 的目录 + `Runtime.select_model()` | 不带参数列清单（`render_models`）；带参数发 `set_model`，等回包（**v2.4 起不带参数改为选择面板**，见 18） |

### 15.1 三条都不弹面板，答案进会话流

看着像"该有个浮层"，其实是**反过来的**：`/status` 最有用的时候是"它跑着、我想看一眼
花了多少"，而那时候屏幕上正在滚的东西恰恰是你要看的 —— 弹层会把它盖住。`/tools` 是
一次性读完就走的清单，`/model` 不带参数时已经有面板的替代品（`/` 那个命令面板本身）。
另外两条实际的：弹层要多两个控件类（这个文件已经 2300 行），而且**会话流里的东西可以
往回翻**，弹层关掉就没了。

> **`/model` 那半句在 v2.4 被推翻了**（见 18.1）：命令面板只补命令名，补不出模型名，
> 所以它从来不是"替代品"。`/status` `/tools` 两条**照旧不弹面板**，理由不变。

### 15.2 左栏「本次会话」多了一行：当前模型

`/model` 加进来之后，"我上次换的那个还生效着吗"变成了一个随时会想问的问题，而此前
模型名只在**会话头那一行**（窄屏还会被收起）和**启动那条 notice**（会滚走）里出现过。
所以它进「本次会话」那一块，和会话 id、规模、审计同一档事实（都由这个会话决定）。

**窗口跟着一起显示**（`deepseek-v4-pro  1M`）：看用量而不看分母等于只说了半句话。
窗口不在目录里时**只说名字** —— 不猜一个分母，和状态栏那条规矩一致。

### 15.3 `notice_is_redundant` 多了 `model`

那条函数判的是"左栏已经常驻显示着的那几类"，启动时不再往会话流里抄一遍。模型那一行
进左栏之后，`[模型]` 那条 notice **在恢复会话时**也归到这一类（它的内容就是左栏那一行）。
而 `/model` 换完模型回的那条 notice **不是开场说明**（它从 `t:"notice"` 走，不走
`init.notices`），所以它照旧要说 —— 里面那句"上一个是谁、下一次请求生效"是左栏
显示不出来的。

### 15.4 换模型为什么不在本轮生效（这一条最容易被当成 bug）

`/model` 换完之后，**这一轮已经发出去的请求不受影响**，改变的是下一个请求。所以：

* 界面上的回声（notice）**本轮就出现**，而提示语写的是"下一次请求生效"；
* `/status` 那一行会把"想用的"和"在用的"分开写（`deepseek-flash（换成 … 的，下一次
  请求生效）`）；
* 那句"模型换了"的说明**留到下一轮开头**，作为一条 `role:"user"` 的消息进历史。

理由在 `state/model.py` 的 `SessionModel` 那边写全了，这里只说它为什么必须在 TUI 层面
被**呈现**出来：不然用户看到的是"我说了换成 pro，界面也说换了，但状态栏还写着 flash"
—— 而那看起来完全像坏了。

### 15.5 老 CLI 那三条是行式的，和 TUI **不共用交互**

`run_repl` 里那三条直连 runtime（决策 19 的例外），答案打 stderr、按列对齐。两边共享的
是**数据口径**（同一批 `Runtime.*` 接口），不是渲染：`view_state` 那三个 render 函数
返回 `Line`（带颜色角色），CLI 那边是 `print`。合成一份的代价是让 `frontends/cli`
import `frontends/tui` —— 而两个前端互不依赖是 `frontends/` 的规矩。

老 CLI 那边有一条 TUI 没有的取舍，写在 `_handle_slash_command` 的 docstring 里：
**认不出来的 `/` 开头的行原样发给模型**（TUI 有命令面板兜错，CLI 没有）。丢掉一句
用户真的想说的话，比把一次打错字送给模型贵得多。

## 十六、v2.2：多 provider 与思考模式（2026-09）

两件事一起做，因为它们都落在**同一个位置**：`/model` 那一屏和它背后的"会话级设置"。

| 命令 | 数据从哪来 | 界面形态 |
|---|---|---|
| `/model` | 目录（`state/catalog.py`）+ `Runtime.select_model` | 名字写成 `provider/model`；两条路由同名时必须写全 |
| `/thinking` | `Runtime.select_thinking` | 不带参数报当前值；`on` / `off` 两个字（认不出就提示） |
| `/effort` | `Runtime.select_effort` + `init.effort_levels` | 不带参数列档位；档位**由 runtime 给**，界面不写死（**v2.4 起不带参数改为选择面板**，见 18） |

### 16.1 左栏与 `/status` 各多一行：思考模式

`/status` 里它和模型、路由同住一组（"它在用什么"就是这几样：谁、哪条路由、想不想、想
多用力）。**关着的时候不写强度** —— `关 · high` 会让人以为 high 还在生效；但要说清"强度
记着"，否则用户会以为刚设的 `max` 没了。

左栏「本次会话」块里那一行**只在关着时出现**：开着是常态（端点的默认行为就是开），为它
常驻一行会把那一块的信息密度拉下来。这一条和"权限范围只显示非默认项"（决策 14）是同一条
取舍。

### 16.2 档位清单随协议发，前端不写死

`init.effort_levels`。理由是决策 18 那条规矩的直接后果：前端不许 import 内核，所以它
拿不到 `reasoning.EFFORT_LEVELS`；而**抄一份到自己这边就会漂** —— 端点加一档要改两个
地方，漏改的那一处只表现为"这一档选不了"。同理 `/model` 那份清单也是随协议来的。

**但"哪些词算开"由前端自己折算。** `/thinking 开` 和 `/thinking on` 在 CLI 那一支都认
（它直连 runtime，可以调 domain 的折算函数），而 TUI 只认 `on` / `off` 两个字面量。这
看起来不一致，其实是**协议只要一个形状**（布尔）的代价：折算词表属于界面 —— 它是"你这
个界面收哪些写法"的问题。TUI 是图形界面（有面板、有提示），少认几个词的成本比多维护一份
词表的成本低；CLI 是行式界面，用户会敲中文，所以它折算。

### 16.3 换路由和换模型名是同一个命令

`/model acme/m2` 和 `/model deepseek-v4-pro` 都走 `Runtime.select_model`，由它决定
"要不要重造 SDK 客户端"（密钥或端点变了就要）。**判据取"变了没有"，而不是"调用方想走哪
条路"**：调用方知道的是目标，而"要不要重造客户端"是适配器的知识。让调用方选方法会出现
"换了路由却没重造客户端"这种半吊子状态，而它的症状是请求带着旧密钥发到新地址上。

界面上不做区分：写全了就是那条路由，没写全而名字唯一就认它，落在两条路由上就**拒绝并报出
候选**（`a/same、b/same`）。候选里那个 `provider/` 前缀是**唯一不歧义的写法**。

## 十七、v2.3：`/mcp` —— 面板 + 左栏一块（2026-09）

| 命令 | 数据从哪来 | 界面形态 |
|---|---|---|
| `/mcp` | `Runtime.mcp.rows()`（`McpHost`：谁在跑、谁没跑、各自几个工具） | **弹面板**（`McpPanel`）：`↑↓` 选、`Enter` 开关、`Esc` 关 —— 见 17.1 |
| `/mcp load\|unload <名字>` | 同上（走 `McpHost.load` / `unload`） | 不弹面板，直接发请求 + 在会话流里留一行 |

### 17.1 面板为什么不关（它和会话选择面板的关键差别）

`SessionPicker` 是"选中一条 → 切过去 → 面板关"，而 `McpPanel` **按一次开关面板照旧开着**。
理由是用法不同：换会话是**一次性决定**，而"把这两个 server 都开上"是常态 —— 按一下就关
会逼人重打三次 `/mcp`。

代价是它必须能**就地刷新**：所以 `ui(kind:"mcp")` 的回包是**全量**清单（不是增量），面板
每次按全量重画。增量的话要维护"这一行怎么合并"的逻辑，而它漂掉的样子是**面板显示的状态
和左栏不一致** —— 那种 bug 最难被发现，因为两个地方都"看起来对"。

### 17.2 面板**不许乐观更新**，而且要显示"正在等"

那一行按 runtime 回来的快照画：按下去到快照回来之间，面板上写的是"正在等 runtime 处理
`x`…"而不是"已挂载"。理由是这一格的结果只有 runtime 知道（连上了没有、几个工具），
先画上去的话 `npx` 起不来时会变成一句假话 —— 和 `/autopilot` 那条"灯亮着、其实没开"同源。

那行字还有第二个用途：挂载 stdio server 要起进程（`npx` 冷启动几秒），而撞上一轮在跑时
runtime 会**先等那一轮跑完**（工具定义每轮取一次快照，半路摘掉会让正在跑的调用以
`McpServerDown` 收场）。那可能是几十秒 —— 屏幕一动不动只会让人以为自己没按到。

### 17.3 左栏多一块「后台 MCP」，和「后台任务」并排

它是这一栏里**第二块对应着活东西**的数据（stdio 的 server 是我们起的子进程，远程的是一条
活着的连接），所以它和后台任务挨着放、用同一套记号形状和同一套计数口径：

```
后台任务            2 / 3        ← 后台：还没收场的条数
  ◐ npx -y xxx        运行中
后台 MCP            1 / 3        ← MCP：在跑的 / 配了的总数
  ● github            12 个工具
```

**只列在跑的**：没加载的 server 不占进程，不该在这块抢地方（"我配了哪几个但没开"是 `/mcp`
面板要回答的）。分母留着，"配了三个只挂上一个"就不会看着像坏了。

**它永远不为自己顶开左栏**（不动 `should_auto_open`）：MCP 是背景设施，不是"这个会话值得
看全局"的信号 —— 而且第一个 server 是用户自己按的，他知道自己按了什么。窄屏收起时那一行
摘要里也只报在跑几个（`2 个 MCP server`），和后台任务那半句并排。

### 17.4 失败那一档在面板里，不在左栏

`/mcp` 里三种状态是**三句不同的话**（`●` 在跑 / `○` 未加载 / `✗` 没连上 + 原因），而在
左栏那块里**只有 `●` 会出现**。分工是清楚的：左栏回答"现在有几个东西是活的"，面板回答
"这几个各是什么状况、怎么改"。合成一种症状是用户分不清"我没开它"和"我开了、它坏了"，
而这两种情况的下一步完全不同。

`✗` 那一档再按一次 `Enter` 就是**重试**（它落在"没在跑"那一支）—— 所以不需要为"重试"
多一个键。

### 17.5 一个入口，两种写法

`/mcp`（面板）和 `/mcp load github`（行式）按的是**同一个出口**（`app.mcp_action` →
`client.mcp(...)`），而 CLI 那一侧只有行式那一种。所以"面板点了没反应"和"打命令没反应"
不可能是两种毛病 —— 这也是这一节里唯一一处**三个前端共享**的东西（共享的是数据口径和
出口，不是渲染：`view_state.mcp_line` 返回 `Line`，CLI 那边是 `print`，和 15.5 同一条规矩）。

## 十八、v2.4：`/theme` `/model` `/effort` 改成选择面板（2026-09）

| 命令 | 数据从哪来 | 界面形态 |
|---|---|---|
| `/theme` | `theme.py` 的 13 套（`ORDER` / `THEMES`） | **不带参数弹选择面板**：`↑↓` 选、`Enter` 换、`Esc` 取消；带参数照旧走 `theme.resolve` |
| `/model` | `state.model_catalog`（`init` 随协议发） | 同上；回给 runtime 的值写全成 `provider/model` |
| `/effort` | `init.effort_levels` | 同上；光标**停在当前那一档** |

三条共用一个控件（`OptionPicker`）+ 一个入口（`app._push_option_picker`）。

### 18.1 这一条推翻了 15.1 的一半

15.1 说"`/model` 不带参数时已经有面板的替代品（`/` 那个命令面板本身）"。**那句话是错的**：
命令面板只补**命令名**，它一个模型名都补不出来。于是"换模型"唯一的输入方式是**把名字一个
字符不差地打一遍**，而名字可以又长又带 `provider/` 前缀（`deepseek/deepseek-v4-pro`）——
打错一个字母的后果按 15 节那条是"只在账单上体现"。

15.1 另外两条理由（会话流能往回翻、弹层要多两个控件类）**在"选"这个动作上不成立**：清单要
的是"挑一个然后它消失"，不是"留在那儿以后再看"。所以 `/status` `/tools` 那两条**不动**
——它们是"看一眼就走"的信息，弹层盖住正在滚的东西正是它们最没用的时候。

**`/theme` 原本在 18 的草稿里被排除**（理由是"13 套各有中文名、换错了一眼看得出来"）。
后来一起做了，因为那句话只否掉了一半：配色换错确实看得见，但**清单上那些名字本来也只是
为了照着打一遍** —— 13 套里的序号最容易记错（`/theme 6` 是 `A`，不是 `P6`；裁掉四套
色卡之后 `/theme 7` 甚至变成了 `B`；后来追加的深透明 `A-T2` 是第 13 套）。而"看得见"
这条差别没消失，它落在 18.2 那张表的**关不关面板**那一格里。

所以取舍判据最后是这个：**错了看得见的东西可以当场生效（选完即关），错了看不见的东西
要等 runtime 确认（面板留着把那句话写出来）**。

### 18.2 一个面板，三条命令

`OptionPicker` 收的是 `view_state.Option` 列表（`value` + 那一行 + 那句 note），
三条命令只是喂给它的数据不同。三条约束照搬 `SessionPicker`：
`Enter` = 选、`Esc` = **什么都不做**、当前那一项带 `●`。

两处不一样，都是**这一格的结果谁说了算**决定的：

| | 初始光标 | 选完之后 |
|---|---|---|
| `/theme` | 停在当前那一套（和 `/effort` 同理） | **立刻关**（`_set_theme` 当场重画，已生效） |
| `/effort` | 停在当前那一档（三四个选项，"换一档"和"看现在是哪一档"按键成本一样） | **不关**：等 runtime 那条 notice |
| `/model` | 停在**当前的下一个**（打开它的人几乎总是想换一个，按 `Esc` 才是"留在原地"） | **不关**：等 runtime 那条 notice |

"不关"那两条的理由是：`/model` 的成败只有 runtime 知道（那条路由有没有密钥），先关掉的
话"没换成"就只表现为一张关掉的浮层 —— 和"换成了一下子没看出来"分不开。所以 notice 回来
时那句话**原样**写在面板底下，人看清了再按 `Esc`（同一条 notice 在会话流里另有一份）。
这是 17.1 那条"面板为什么不关"的同一类判断，而 `/theme` 不适用它：配色是本地的。

### 18.2.1 留在屏幕上的代价：它必须跟着最新的 state 重画

"选完不关"还有一半容易漏掉，而它被用户一眼看出来了：**面板画的是打开那一刻的候选快照**。
选了 `low` 之后，`●` 和那一档的高亮照旧停在 `high` 上 —— 看着正是"我刚才那一按没生效"，
而它其实生效了。

数据本来就没错（`apply_state` 会更新 `state.model` / `state.effort`），错的只有"面板没有
按新数据重画"。所以 `App` 在每份 `ui(state)` 快照之后调 `OptionPicker.reload_options()`：
候选按最新的 state 重算，**光标停在原来的序号上不动**（人常常还要再看一眼或再换一个，
把光标跳走会让"我刚才选的是哪一个"失去锚点）。

这和 17.1 那条"`McpPanel` 必须能就地刷新"是同一个形状的代价：**留着的面板都是活的**，
而活的东西必须跟着它显示的那份事实走。取数函数由 `_push_option_picker(kind=...)` 给
（`model` / `effort`），`/theme` 不给 —— 它选完就关，没有"等回话"那一段。

顺带一条踩过的坑：那个方法**不能叫 `refresh`** —— Textual 的 `Widget` 自己有一个
`refresh(*, repaint=…)`，覆盖它会在框架重画那一步炸 `TypeError`（而那时栈上指向的是
Textual 内部，看不出是自己覆盖的）。

### 18.3 名字那一列还是 `provider/model`

面板里回给 runtime 的值**就是行上写的那一串**（`Option.value`），界面不自己拼、也不猜。
理由和 15 节那条一样：同名模型可以在多条路由上，而"选了哪一个"决定请求发到哪个账号上。

清单为空时（老 runtime 没随 `init` 发 `model_catalog` / `effort_levels`）**退回纯文本**
（`render_models` / `render_effort`）：一个选项都没有的浮层看起来像界面坏了，而那其实是
runtime 那一版协议里没有这一格。

### 18.4 老 CLI 那边照旧是行式

`/model <名字>` `/effort <档位>` 两条命令行写法**一条都没去掉**（面板只是多出来的一条路），
所以 `frontends/cli` 不用动 —— 它没有浮层，15.5 那条"两个前端互不依赖"照旧成立。两边共享
的仍然是数据口径（同一批 `Runtime.*` 接口 + 同一份协议清单）。

---

## 十九、v2.5：安静模式（`--quiet` / `/quiet`）

一次工具调用占三行（调用 / 权限 / 结果）、思考过程在流式那一轮铺满屏幕 —— 那是"看得清
每一步"的形态，但在长任务里它会把对话正文冲得看不见。安静模式是**同一次运行的另一种
呈现**，开关只有两档（`--quiet` 起手，或运行中 `/quiet`）。

### 19.1 它是**前端的显示偏好**，所以不进协议

| | `/autopilot` | `/quiet` |
|---|---|---|
| 它改的是什么 | runtime 的行为（工具要不要问人） | 这个界面怎么画 |
| 真值在谁手里 | runtime（界面必须等 `ui state` 确认） | **界面自己**（当场改、当场回声） |
| 协议 | `set_autopilot` + 快照回来 | **一条消息都不发** |
| 换会话之后 | 由 `bootstrap` 带着走（进程级） | 由 `ViewState` 留着（进程级） |

两条都是"进程级的模式"，但第三行是本质差别：给 `/quiet` 加上"等确认"，就会有人顺手
给它接一条协议消息 —— 而那条消息没有任何东西可确认，只会让按一下键要等半拍。

**代价说白**：三个前端各画各的，Web 前端将来要有自己的安静模式，这一格不该被继承。

### 19.2 一行一个调用，结果**回填**到那一行

```
  → [write_file] hello.c   ✓ 16 字符   1ms
```

- 身份是 **`call_id`**（决策 4 那个），所以并行批次里两条一模一样的调用不会贴错；
- 判据是 `view_state.same_anchored_line`（**身份 + 族**，纯函数），拼接是
  `merge_anchored`（"工具"族往后接，"思考"族整行换）—— 控件只按身份找那一行，
  它不认识"工具"和"思考"；
- 找不到那一行时**照常画出来**（退化成一条只有结果的行）：那远好过"这次调用成没成"
  没有答案；
- **`Line` 多了一个 `anchor` 字段**（`call_id` / `run_id`，空 = 不参与回填），
  老模式画出来的每一行都不带它 —— 于是"加了回填"没有动到任何老路径。

### 19.3 「主要内容」由前端按工具名挑，并且留了兜底

表在 `view_state._BRIEF_KEYS`（`read_file` → `path`、`shell` → `command`、
`ask_user` → `question`…）。**为什么不让 runtime 在协议上多发一个字段**：这一格只影响
一个前端的排版，而协议的每一个字段要三个前端 + schema + 文档一起背 —— 代价和"少画几行
字"不成比例。

表会旧，所以兜底是三层：表 → 偏爱键序（`path`/`command`/`pattern`…）→ 第一个字符串。
另外**载荷被截断是常态**（`write_file` 的 content 动辄几千字符，事件里那份是 200 字符的
审计预览，`json.loads` 必然失败），所以还有一条按 `"键": "值"` 字面捞的路径 —— 捞不到
就原样贴。

### 19.4 静默的是"没有人参与"的那几种放行

`auto_allowed` / `autopilot` / `rule_allowed` / `command_allowed` 不画权限行；
`approved` / `user_denied` / `policy_denied` / `no_asker` 照旧。理由不对称，而且必须
不对称：前四种是噪声，后四种里"被拒绝/被禁止"是**没执行**——审批面板关掉之后，回翻记录
只剩这一行能解释那一轮为什么停在那儿。

### 19.5 动效：转圈 + 实时字符数，而且**只在真的在跑时**转

安静模式下工具行和思考行都只有一行，屏幕上没有别的东西在动 —— 而一次模型往返是秒级
（第八节末尾那条）。所以：

- 折叠那一行在流式期间是 `▸ 思考过程 ⠇ 1,284 字符`：帧号由时钟算（`spinner_frame`，
  纯函数 —— 两处同时画时才不会各转各的），字符数是这一轮累计的思考链长度；
- **回合收尾时定格**成 `▸ 思考过程（N 字符 · Ctrl+T 展开）`：它和 `Ctrl+T` 认的是同一行
  （role 相同、还带 `run_id` 身份），所以展开照旧管用。`ui(run_finished)` 先到（它会清掉
  流式累计）时由消息泵那一拍自愈（`App._tick_quiet`）；
- **等人按键时停下**（审批/提问面板压在最上面，`App._waiting_for_human`）：协议里没有
  "正在等你"这个 phase，硬编一个就是第二份事实，所以判据取自"屏幕上现在是谁在等你"；
- **非安静模式一个字都不改**：`spin` 是空串，状态栏那一格照旧是那个静态的 `●`。

### 19.6 开关只影响**之后**画出来的东西

重画已经画好的回合要留下"每一轮当时的原始事件"（这一版没有这个结构），所以回声里明说
"只影响之后画出来的东西"。回声本身**当场就说**（不等任何回包）—— 理由在 19.1。

状态栏那一格 `安静`**只在开着时占位**：它和 autopilot 那一枚的取舍刻意不同 ——
那一格决定"接下来还会不会问你"，两种状态都必须在场；而这一格关掉时，状态栏和加这个
功能之前一个字符都不差。

## 二十、v2.6：界面语言（`ui.language`）

界面可以整套换成英文。**它只换界面的语言**：模型读到的提示词、工具描述、以及模型自己
说什么语言，一个字都不变（这一条有测试咬着，见 20.4）。

### 20.1 配置项

```json
"ui": { "language": "zh" }
```

- 取值只有 `zh`（默认）和 `en`；**认不出来的值当场报错、退出码 2**，不静默回退 ——
  一个静默变回中文的 `en_US` 让人看到的是"我配的英文没生效"，而他会去查一个不存在的
  bug；
- **没写这一节 = 中文**，也就是和加这个功能之前逐字节一样（老配置照样跑）；
- 段的主人住在 `agent_runtime/i18n/`（和 `web` → `WebConfig`、`providers` → `catalog`
  同一条"一节一个主人"的规矩）；`userconfig` 仍然只保证它是"字符串 → 字符串"。
- `--lang` 是临时开关（测试与调试用），主入口是配置文件。

### 20.2 文案是**两个进程各翻自己产生的那一半**

| 谁产生 | 例子 | 在哪儿翻 |
|---|---|---|
| TUI 父进程 | 三条栏、左栏六块、回合头、命令面板、欢迎屏、五个弹层、状态栏那一行的 `activity` | `frontends/tui/*`（查目录表） |
| runtime 子进程 | `init.notices` 那些 `[权限]` / `[技能]` 说明、`/model` 的回话、审批面板里那两句"按下去会记住什么"、AGENT.md 的失败理由、技能/MCP 的问题 | `runtime/` `security/` `state/` `tools/`（同样查目录表） |

**为什么不是"前端统一翻译"**：子进程发给前端的是**已经拼好的整句**（`init.notices` 的
`text`、`asker` 的 `remember_hint`，后者在 schema 里写着"前端一个字都不许改"）。前端拿着
一句话去翻，只能按文字猜 —— 而"按 code 分流、不解析文字"是这个项目的既有规矩。

语言怎么到子进程：父进程读一次配置定下来，再用 `--lang` 传下去（和 `--stream` 同一个
形状）。两处各读一次的话，用户在两次读之间改了配置，界面和通知就会是两种语言。

### 20.3 三个约束，以及它们各自被怎么解掉

**（1）`skills/` 是叶子包，不许 import i18n。** 它有 `tests/test_imports.py` 一条测试
盯着（"只准 import `paths`"）。所以技能的问题改成**机器认的 code + 参数**：

```python
Problem(code="name_mismatch", params=(("name", …), ("directory", …)))
loader.render(problem, i18n.t)        # 译者由调用方注入
```

"x 被跳过：<内层原因>"是**嵌套的 `Problem`**（`render` 递归）。`tools/mcp.py` 里同一类
错误就直接 import i18n 翻了 —— **这不是两个地方各写一套，是同一个约束下的两种落点**：
谁能翻就在谁那儿翻，谁不能翻就把原料交出去。

**（2）Textual 在类创建时就把 `BINDINGS` 合并好了。** 所以在类体里写 `i18n.t(...)` 等于
把语言冻在 import 那一刻（真实那条路恰好对得上，测试里换语言就对不上）。现在的做法是类体
里只留 `(键, 动作, 文案键)`，实例化时用 `widgets.localize_bindings` 重算一遍。

**（3）CSS 是类属性，写不进语言判断。** 只有一处受影响：欢迎屏底部那个提示框的高度
（中文两行、英文四行 —— 英文键位长得多，4 条一行会折成四行被框高静默裁掉）。做法是
`HintPanel.on_mount` 里按当前语言盖 `styles.height`，而那个数由
`widgets.hint_box_height()` 算（配一条按列数量宽度的测试）。

### 20.4 模型侧一个字都不许变，而且有测试咬着

`prompts/system.zh.md`、工具描述、`todo_note` / `job_note` / `skill_note`、
`agents_md` 的提示词块、`session._env_block`（"## 运行环境"）—— 这些是**模型读的**，
和界面语言是两条线。三条测试：

- `test_the_system_prompt_does_not_follow_the_ui_language`（逐字节比）；
- `test_the_notes_we_hand_the_model_do_not_follow_the_ui_language`（`todo_note` 不变、
  而同一模块里给人看的 `progress_line` 跟着变 —— 这一对是现成的接缝）；
- `test_the_english_catalog_has_no_chinese_in_it` + `test_the_rendered_lines_speak_english`
  （英文界面里不许有汉字；后者喂一份人造事件流把渲染函数全过一遍）。

### 20.5 版式：英文更长，所以有几处按语言算

| 哪儿 | 中文 | 英文 |
|---|---|---|
| 右栏「最近活动」的时间列 / 标题列 | 12 / 25 列 | 14 / 23 列（**都保持奇数**："整字 + `…`"的截断只有奇数列才铺得满） |
| `/status` 的标签列 | 10 列 | 14 列（`Total input` 比"累计输入"宽） |
| 欢迎屏提示框 | 一行 4 条、高 4 | 一行 3 条、高 6 |

### 20.6 明确不翻的

- **老 CLI 与 ansi 前端**（决策 19 冻结的那条直连路径）：它们自己的交互文案仍是中文；
- **工具描述与工具结果**（模型看的）；
- **13 套配色的中文名**：它们是长在主题上的数据，英文名并存（`name_en` / `source_en`），
  而 `/theme` **两套名字都认** —— 切到英文之后 `/theme 靛夜` 突然失灵是最容易被当成
  bug 的那种回归；
- **报错里的技术细节**（异常类型名、路径、服务器原文）：只翻句子，不翻数据。


