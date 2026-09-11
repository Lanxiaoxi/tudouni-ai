# tudouni-ai

一个**从零逐步构建的 Agent Runtime**，用 DeepSeek（OpenAI 兼容接口）驱动，带工具调用、
参数校验、权限审批、会话持久化、审计日志和错误恢复。

不是框架，是一份**可以读懂全部代码**的最小实现 —— 目标是每一个设计决定都有明确的
理由，而不是"业界都这么写"。

---

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)（本项目用它管环境和依赖）。

```powershell
# 1. 装依赖（会按 .python-version 准备 Python 3.12）
uv sync

# 2. 提供密钥 —— 源码里不留密钥
Copy-Item .env.example .env
# 然后编辑 .env，填上：  DEEPSEEK_API_KEY=sk-...
# .env 已经被 .gitignore 忽略，不会被提交。

# 3. 跑起来
uv run main.py
```

密钥也可以走环境变量，而且**环境变量的优先级高于 `.env`**：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."      # 当前终端
setx DEEPSEEK_API_KEY "sk-..."        # 永久（重开终端生效）
```

优先级是 **真实环境变量 > `.env` > 默认值**。这个方向不能反 —— 反了会让某天部署时
被一个遗留的 `.env` 悄悄改到别的网关，而那种问题从源码里完全看不出来。

可选配置项（`.env` 或环境变量都行）：`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`。

跑测试：

```powershell
uv run pytest
```

## 用法

```
uv run main.py                          # 开一个新会话，进入多轮对话
uv run main.py --session demo           # 接着 demo 这个会话聊（不存在则新建）
uv run main.py --list                   # 列出已保存的会话
uv run main.py --session demo --history # 看对话历史（不调用模型）
uv run main.py --session demo --audit   # 看审计轨迹：token、权限裁决、耗时（不调用模型）
uv run main.py --debug                  # 把中间过程打到 stderr
```

不带 `--session` 时**每次都是新会话**，但**聊过之后就会落盘**（第一次写盘发生在你说出
第一句话之后），所以开了不用不会留下空文件。

交互时：提示符和调试信息走 **stderr**，Agent 的回答走 **stdout**。所以
`uv run main.py > 对话.txt` 拿到的是干净的答案。

## 权限与审批

需要审批的调用（默认是 medium / high 那些）长这样：

```
[审批] 工具 shell  风险 high
[审批] 参数 command=git status
[审批] t = 以后每次都直接执行，你不会再看到它要做什么（写进 .tudouni.json，下次启动仍然有效）
[审批] 是否执行？[y/N/t]
```

- **`y` 批准这一次；`N`（回车）拒绝。** 默认是拒绝不是批准：连续审批里最容易做的
  动作就是一路回车，而"回车即执行"等于把最危险的那条路改成手滑也能过。
- **`t` = 以后不再问这个工具**，写进 `.tudouni.json` 并且**立刻生效**，下次启动仍然
  有效。提示会写清它到底把什么给出去了 —— 对 `shell` 那不是"少一次确认"，而是你
  **再也看不见它要执行什么**，而命令原文正是那道关唯一的判断依据。
- 一条规则**只记工具名、不记参数**：作用范围必须一眼看得懂。（参数级规则如"只放行
  `git status`"签名早就留好了，但前缀匹配能被 `git status; rm -rf x` 绕过。）

### `.tudouni.json`（工作区根目录）

```json
{
  "auto_approve": ["low"],
  "auto_approve_tools": ["shell"],
  "deny_tools": ["git_commit"]
}
```

| 键 | 含义 |
|---|---|
| `auto_approve` | 按风险等级直接放行。**只收 `low` / `medium`** |
| `auto_approve_tools` | 按工具名直接放行 —— `high` 只能这样点名 |
| `deny_tools` | 按工具名直接拒绝，问都不问 |

- **为什么 `high` 不能按等级放行。** 等级是工具自己声明的，所以"放行所有 high"会随着
  将来新加的工具自动变宽：新加一个 high 的 `delete_file`，它一注册就已经是免审批的，
  而写规则的人从没听说过它。按名字点名不会 —— 名单里写了哪个工具，放行的就是哪个。
  这正是 `Tool.risk` 不给默认值要防的那件事。
- 不认识的键、坏 JSON、同一个工具既放行又拒绝 —— 一律报错停下（stderr + 退出码 2，
  和没配密钥同一档）。写错一个键名而它静默不生效，是最坏的失败形态。
- 文件不存在不是错误：缺省就是内置默认（只有 `low` 自动放行）。
- 每次启动都会把生效范围打到 stderr：`[权限] 按等级自动放行 low；点名免问 shell`。
  「按一次 t 就永久生效」是最容易忘掉的那类设置，而这份文件攒上几条之后，光盯着它
  已经答不出"现在到底还有什么会问我"。

## 架构

```
main.py            组装：把下面这些接起来（薄入口）
cli.py             参数解析、会话选择、交互循环、历史与审计的展示
config.py          配置：密钥走环境变量（源码里不留密钥），权限策略走 .tudouni.json

prompts/           系统提示词（给人读、给人改的文本，不是代码）
  system.zh.md       静态部分；动态那几行由 state/session.py 拼在末尾

models/            模型适配层
  base.py            ChatModel 抽象：complete(messages, tools) -> ModelResponse
  types.py           ModelResponse / TokenUsage / 三个领域异常
  openai_compatible.py  OpenAI 兼容实现：把 SDK 的响应结构和异常都归一化掉

tools/             工具层
  tool.py            Tool(名字/描述/风险/参数模型/handler) + ToolRegistry
  builtin.py         内置工具的装配（参数模型 + 风险等级）
  filesystem.py      文件操作 + safe_path（工作区边界）+ 控制面拒绝写
  clock.py           当前时间（无参数、无状态、不需要注入任何东西）
  grep.py            工作区内按正则搜文本 —— 走文件沙箱那条边界，因此是 LOW 风险
  shell.py           命令执行 —— 唯一不受工作区边界约束的工具，因此只能走人工审批

security/          权限层
  policy.py          PermissionPolicy：纯函数，只裁定 ALLOW / DENY / ASK
  gate.py            关卡：把策略 + asker + memory 变成一次裁决（六种来路分开记）
  asker.py           询问方式（CLI 版走终端，认 y/N/t；测试版是脚本化的假实现）
  memory.py          按 t 记住的工具名 —— 唯一可变的那份权限状态

state/             状态层
  session.py         Session（会话的全部事实）+ session_id 合法性
  store.py           JsonSessionStore：原子写、id 白名单、容忍未知字段

audit/             审计层
  events.py          事件构造
  jsonl.py           JsonlSink：只追加的 .jsonl，天然抗崩溃

agents/            编排层
  agent.py           Agent：一个回合的循环
  retry.py           重试策略（只重试暂时性失败）

tests/             全套测试，跑完不到一秒（含"每个模块都能导入"的冒烟测试）
```

依赖方向是单向的，无环：

```
models   （无内部依赖）
tools    （无内部依赖）
state    （无内部依赖）
config   （无内部依赖，只读 .env 和 .tudouni.json）
security → tools, config
audit    → state
agents   → audit, models, security, state, tools
main     → 全部
```

## 贯穿全局的三个设计原则

### 1. 判定留在内部，沟通交给注入的实现

四个注入点，同一条原则：

| 注入点 | Agent 知道 | 注入的实现知道 |
|---|---|---|
| `asker` | 该不该问 | 怎么问（终端 / 测试 / 将来的 Web） |
| `memory` | 人说过哪些"别再问" | 记住的东西落在哪（`.tudouni.json` / 只在内存里） |
| `on_checkpoint` | 什么时候保存是安全的 | 存到哪、什么格式 |
| `on_event` | 发生了什么 | 记到哪、什么格式 |

好处是具体的：权限策略变成纯函数可以单测；多轮循环留在 Agent 外面，所以 Web 版
（每回合一次 HTTP 请求、根本没有循环）不需要改 Agent。

### 2. provider 的细节在适配层归一化

`tool_calls` 的形状、`usage` 的字段、SDK 的异常类型 —— 全部在
`models/openai_compatible.py` 里翻译成项目自己的类型。上层认不出任何 OpenAI SDK
的东西，换 provider 时它们一行都不用改。

### 3. 同一份事实只写一遍

工具的「参数格式」定义一次（Pydantic 模型），两用：生成给模型的 schema（预防）
和校验模型给的参数（兜底）。手写第二份 schema 就会漂移 —— 这是项目里反复出现的
模式，也是 `Session` 从 `AgentState` 改名的原因（名字不该编码已经不存在的耦合）。

## 几条硬约束

**会话历史不能任意截断。** 一条带 `tool_calls` 的 assistant 消息，后面必须紧跟
**全部**对应 id 的 tool 结果，否则 API 直接 400，而且之后每一轮都发不出去。
所以落盘点只能在一整个 step 之后 —— 半截状态一次都不许写出去。

**确定性失败不重试。** 401、模型名错这类失败重试只是把同一个失败重复三遍。
只有网络、超时、限流、5xx 才值得退避重试。

**审计写入失败不能影响主流程。** `on_event` 的调用点有些落在历史不一致的窗口里
（`tool_call` 事件就夹在 assistant 消息和它的 tool 结果之间），抛出去会让会话
永久损坏。所以它必须被吞掉 —— 但要在 stderr 大声说出来，不能变成静默失败。

**控制面只有人能写。** `.tudouni.json`、`.sessions/`、`.logs/` 都在工作区里，而
`write_file` 的边界正好是整个工作区 —— 能写它们就等于能给自己发权限、伪造"用户批准过"
的记录、抹掉"谁批准了什么"的证据。所以 `safe_path`（别出去）之外还有一张拒绝表
（`tools/filesystem.py` 的 `CONTROL_PLANE`，别进来）：**读可以，写一律拒绝，和审批
无关 —— 人批准了也不行。** 这条边界是 `t` 能存在的前提：没有它，"按一次 t 永久免问"
和"agent 改一次策略文件"合起来就是一条从一次写文件审批走到 shell 全权的路。

**步数用尽不是答案。** 撞到 `max_steps` 时 `run()` 抛 `StepLimitExceeded`，而不是
返回一句"任务超过最大执行步数，已停止。" —— 返回值会被 `cli.py` 打进 **stdout**，
于是 `> 对话.txt` 里那句话跟真答案长得一模一样，用户分不出"答完了"和"被砍断了"。
它也不算失败：抛之前 `run_finished` 和落盘都已经完成，会话是完好的，`cli.py` 会
把"接着跑：--session X"说到 stderr。这正是 `--audit` 里那句 `stop_reason=max_steps`
属于审计、不属于输出的原因。默认步数 **40**：实测把这个项目最典型的长任务
（"参考现有实现加一个工具"）跑到收尾需要 19 步，20 只剩最后一步的余量，任何一次
返工都会撞墙。

**成本和"聊了多少轮"关系不大。** 实测一轮 5 步的任务里，一次 `read_file` 返回
12524 字符，占了整轮成本的 86%。未命中缓存的输入比命中贵约 50 倍，所以
`--audit` 里那个缓存命中率比总 token 数更值得看。

## 数据落在哪里

| 目录 | 内容 | 进版本库吗 |
|---|---|---|
| `.env` | 本地密钥与配置（模板见 `.env.example`） | 否 |
| `.tudouni.json` | 权限策略：哪些工具免审批、哪些直接拒绝 | 否 |
| `.sessions/` | 会话状态（含工具读到的文件正文） | 否 |
| `.logs/` | 审计轨迹（含工具参数预览） | 否 |

这几样都是本机的东西，不是源码 —— `.gitignore` 里全排除了。
`.env.example` 是例外：它是给人看的模板、不含真密钥，所以**它是要提交的**（`gitignore`
里那条 `.env.*` 后面跟了一句 `!.env.example` 把它重新包含回来）。

`.tudouni.json` 是**工作区本地配置**：它记的是"这个工作区信任什么"，而且按一次 `t`
程序就会往里写 —— 一个程序自己会改的文件不该进版本库。想把它变成随仓库走的团队策略，
把 `.gitignore` 里那两行删掉即可（写入走临时文件 + `os.replace`，所以磁盘上永远不会
出现半份策略）。

## 已知的取舍

- **`shell` 工具打破了工作区边界。** `safe_path` 拦得住 `../../evil.txt`，拦不住
  `cd .. && rm -rf x` —— 后者走的是操作系统，不是 Python。所以它的风险等级是 HIGH：
  默认策略（`auto_approve=("low",)`）下**每条命令都要人工审批**，而且审批提示里命令
  原文不截断（用旧的那张 120 字符预览，`git status && … && rm -rf /` 的危险半句正好
  被切掉，用户会在看不全的情况下签字）。**唯一能放开它的是点名** —— `.tudouni.json`
  的 `auto_approve_tools`，或者审批时按 `t`；按下 `t` 的瞬间提示会直说"以后你不会再
  看到它要做什么"。真正的解法是操作系统级沙箱 —— Codex 的
  read-only / workspace-write 就是 seatbelt 和 landlock 做的，本项目还没有。在那之前，
  「每次都要人看一眼」是唯一诚实的默认值。
- **`package = false` + `sys.path` 修补。** `pyproject.toml` 位于包目录内，所以项目
  根就是包本身，uv 无法把它当包安装。`main.py` 因此自己把父目录塞进 `sys.path`，
  `conftest.py` 做同一件事。想彻底解决要把项目根上移一级或改成嵌套布局，代价是
  一次大搬迁 —— 暂时不值得。
- **`doc/` 里的文件。** `guide.md` 是本项目的分阶段设计文档；`summary.md` 是 Agent
  自己读 `guide.md` 之后写的摘要 —— 顺便当作"它真的能干活"的样例。
- **`tools/filesystem.py` 里的 `safe_path`。** 它其实是一条安全策略，按职责该住在
  `security/`。留在工具里的原因是它和文件操作绑得太紧，搬走会让两边都变难读。
- **`get_current_time` 只给本机时区。** 想看任意时区得引入 IANA 时区库（Windows 上
  还要额外的 `tzdata` 依赖），那是"本来零依赖、不会失败"的工具凭空多出的失败点。
  模型拿到带偏移的时间后可以自己换算，所以暂时不做。

## 尚未实现

- 上下文管理（按实测成本，当前规模下截断不划算）
- 任务编排、子 Agent、并发
- 服务化（Web / API）

## 许可

MIT，见 [LICENSE](LICENSE)。
