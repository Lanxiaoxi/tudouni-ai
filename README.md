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

每轮末尾还有一行统计（也走 stderr）：

```
（会话 '20260911-165811'：3 条消息、1 步；累计输入 2035 token（命中缓存 1792、命中率 88%）；本轮 1.3s；上下文 2.0k/1M（0.2%）。）
```

- **`N 条消息` 是 `session.messages` 的长度，不是"你说了几句"。** 它含会话创建时写下的
  那一条 system 提示词（`prompts/system.zh.md` + 一行运行环境）—— 所以只说一句 "hi" 也是
  3 条：system + 你的话 + 助手的回答。带工具的回合里，每条 assistant 和每条 tool 结果
  各算一条。想看逐条的角色，用 `--history`。
- **`N 步` 就是 assistant 消息的条数**（`step_count()` 是派生值，刻意不存成字段），
  一步 = 一次模型往返。所以 `3 条消息、1 步` 本身已经隐含了"这 3 条里有 1 条是回答"。
- **`--debug` 里那句 `消息数=2 [system, user]` 和它不矛盾**：那句量的是**发请求那一刻**的
  长度（回答还没落进历史），而且不含每次请求临时拼上去的"剩余步数"提醒 —— 那条不写回
  历史，所以真实的请求载荷其实比它多一条。
- **`累计输入 … token` 是会话累计**（那是钱），**`本轮 X` 是刚结束的那一轮**（不累计）。
  两者口径不同，理由见下面「几条硬约束」里那条耗时说明。
- **`上下文 2.0k/1M（0.2%）` 是"上一次请求实际发出去多少"**，不是"现在"：下一次请求要加上
  这一轮的回答和工具结果，所以它是个**下界** —— 判断"离窗口还有多远"够用，但别当成精确值。
  分子是 provider 实测的 `prompt_tokens`（不是本地估算：项目没有 tokenizer 依赖，而且估算
  还得自己把 tool schemas 算进去，估偏十几个百分点比不报更坏）；**分母来自
  `config.CONTEXT_WINDOWS`，一张按模型名的表**，因为响应里没有这个字段。表里没有的模型名
  **只报用量、不报占比** —— 错的百分比比没有百分比更坏，启动时也会说一句该往哪加。
  占比保留一位小数，**超过 100% 也照实报**（不夹平：那一轮就是发不出去了，抹平会让人以为
  "刚好卡住"）。它含命中缓存的那部分：看窗口够不够要看总数，看钱要看未命中，后者在同一行
  的命中率里。
- 这一行**只说刚才发生了什么**，不负责教你怎么续聊：新会话在启动时已经说过一次
  （`想回来继续它：--session X`），恢复会话时启动那行也带着 id，`--list` 随手可查。
  「步数用尽」那种没走完的回合仍然会额外说一句 `接着跑：--session X`。

## 权限与审批

需要审批的调用（默认是 medium / high 那些）长这样：

```
[审批] 工具 shell  风险 high
[审批] 参数 command=git add -p x.py
[审批] t = 以后 git add 开头的命令都直接执行，不会再给你看（写进 .tudouni.json，下次启动仍然有效）
[审批] 是否执行？[y/N/t]
```

- **`y` 批准这一次；`N`（回车）拒绝。** 默认是拒绝不是批准：连续审批里最容易做的
  动作就是一路回车，而"回车即执行"等于把最危险的那条路改成手滑也能过。
- **`t` 记住"以后别再问"**，写进 `.tudouni.json` 并且**立刻生效**，下次启动仍然有效。
  提示会把记住的东西原样写出来。对 `shell` 记的是**命令前缀**（`git add` 开头），不是
  整个工具 —— 粒度差着量级，所以那一行必须看得清；解析不出前缀（命令里有重定向之类）
  时干脆不提供 `t`。其余工具记的是工具名。
- 一条规则**只记工具名或命令前缀，从不记参数**：作用范围必须一眼看得懂。

### `.tudouni.json`（工作区根目录）

```json
{
  "auto_approve": ["low"],
  "auto_approve_tools": ["shell"],
  "deny_tools": ["git_commit"],
  "shell_allow": ["git add", "ls", "python -m pytest"]
}
```

| 键 | 含义 |
|---|---|
| `auto_approve` | 按风险等级直接放行。**只收 `low` / `medium`** |
| `auto_approve_tools` | 按工具名直接放行 —— `high` 只能这样点名 |
| `deny_tools` | 按工具名直接拒绝，问都不问 |
| `shell_allow` | 按**命令前缀**直接放行（只对带命令行的工具即 `shell` 有意义） |

- **为什么 `high` 不能按等级放行。** 等级是工具自己声明的，所以"放行所有 high"会随着
  将来新加的工具自动变宽：新加一个 high 的 `delete_file`，它一注册就已经是免审批的，
  而写规则的人从没听说过它。按名字点名不会 —— 名单里写了哪个工具，放行的就是哪个。
  这正是 `Tool.risk` 不给默认值要防的那件事。
- 不认识的键、坏 JSON、同一个工具既放行又拒绝、规则里带分隔符 —— 一律报错停下
  （stderr + 退出码 2，和没配密钥同一档）。写错一个键名而它静默不生效，是最坏的失败形态。
- 文件不存在不是错误：缺省就是内置默认（只有 `low` 自动放行）。
- 每次启动都会把生效范围打到 stderr：
  `[权限] 按等级自动放行 low；点名免问 shell` / `[权限] 命令规则（按前缀放行）git add`。
  「按一次 t 就永久生效」是最容易忘掉的那类设置，而这份文件攒上几条之后，光盯着它
  已经答不出"现在到底还有什么会问我"。

### 命令规则：`shell_allow` 的粒度与边界

规则是**命令前缀**，按 token 匹配（不是字符串前缀）：

| 命令 | 规则 `git add` | 规则 `git` |
|---|---|---|
| `git add -p x.py` | ✓ | ✓ |
| `git commit -m x` | ✗ 问 | ✓ |
| `git commit-graph write` | ✗ 问 | ✓ |
| `git -C /tmp add` | ✗ 问 | ✓ |

三条不能妥协的（实现与理由在 `security/commands.py`）：

1. **整条命令行要逐段覆盖。** `git status && rm -rf build` 在规则 `git` 下**也要问**，
   因为第二段没人认领。段按 `;`、`&&`、`||`、`|`、`&`、换行拆开（引号内的不算）。
   只匹配第一段是这个功能最典型、也最危险的错法。
2. **看不懂就问。** 命令替换 `$( )`、反引号、重定向 `>` `<`、`${`、引号不成对，一律
   落到"问"。重定向尤其要挡：它能把一条只读命令变成写文件，而写文件该走 `write_file`
   那条审批。
3. **按 token 比，不按字符串比。** 规则 `git commit` 不能匹配 `git commit-graph` ——
   `startswith("git commit")` 会，那是一条静默放行。

**它不判断安全性，只做保守匹配。** 所以有一条必须先说清的事实：**写裸程序名
（`git`、`python`、`make`）约等于放开整个 `shell` 工具** —— `git -c alias.x='!rm -rf x' x`、
`git bisect run <任意命令>`、`git rebase --exec <任意命令>`、以及 `git commit` 会去跑的
`.git/hooks/*`，都能从"允许 git"里长出来。想让规则真的收窄什么，就写到子命令这一层。
这是**易用性换来的**：它减少打断，不是安全边界 —— 真正的边界是操作系统级沙箱，本项目
还没有（见下面「已知的取舍」）。

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
  gate.py            关卡：把策略 + asker + memory 变成一次裁决（七种来路分开记）
  asker.py           询问方式（CLI 版走终端，认 y/N/t；测试版是脚本化的假实现）
  memory.py          人按 t 记住的东西（工具名 / 命令前缀）—— 唯一可变的那份状态
  commands.py        命令行的拆解与规则匹配：纯函数，看不懂就返回"没覆盖"

state/             状态层
  session.py         Session（会话的全部事实）+ session_id 合法性
  store.py           JsonSessionStore：原子写、id 白名单、容忍未知字段

audit/             审计层
  events.py          事件构造
  jsonl.py           JsonlSink：只追加的 .jsonl，天然抗崩溃

agents/            编排层
  agent.py           Agent：一个回合的循环
  retry.py           重试策略（只重试暂时性失败）

tests/             全套测试（含"每个模块都能导入"的冒烟测试；时间靠注入的假时钟断言）
```

依赖方向是单向的，无环：

```
models   （无内部依赖）
tools    （无内部依赖）
state    （无内部依赖）
config   → security.commands（校验 shell_allow 里的规则语法；密钥那条路仍然是环境变量）
security → tools（commands.py 自己无内部依赖）
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

**耗时数据要分项看。** `--audit` 末尾那行把一轮拆成模型往返、工具执行、等人审批、
重试退避和未归因 —— 这几段**互不重叠**，所以能相加，也能拿"回合总"减出"未归因"
（没被埋点的部分：会话落盘、事件写入、解析与策略判定）。其中两条口径值得记住：
工具耗时**不含**等人审批（那段时间在 `permission.waited_ms` 里），否则"我看了 30 秒
才按 y"会显示成"这个工具要 30 秒"；重试退避**不属于任何一次请求**，不单独记的话
"这一轮为什么慢了 3 秒"在日志里根本看不出来。交互循环每轮末尾那句统计里也有一个
「本轮 X」—— 旁边那几个数（消息数、步数、token）都是**会话累计**，只有它是刚结束的
那一轮，所以标签写明了"本轮"。

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
  被切掉，用户会在看不全的情况下签字）。能放开它的有三条路，粗到细：`.tudouni.json`
  的 `auto_approve_tools` 点名整个 `shell`、按 `t` 记住一条**命令前缀**
  （`shell_allow`）、或者手写一条前缀规则。**这三条都是"人预先声明信任"，不是系统
  判断安全** —— 而且写裸程序名（`git`）约等于放开整个工具（理由见上面那节）。
  真正的解法是操作系统级沙箱 —— Codex 的 read-only / workspace-write 就是 seatbelt 和
  landlock 做的，本项目还没有。在那之前，「每次都要人看一眼」是唯一诚实的默认值。
- **`.git/` 仍然在 agent 的可写范围内。** `.git/config` 的 `core.pager` /
  `core.sshCommand` 和 `.git/hooks/*` 都能让一条**已被放行**的 `git` 命令去执行任意
  东西，而且不需要经过 shell 审批。控制面拒绝表（`.tudouni.json` / `.sessions/` /
  `.logs/`）目前不含 `.git/`。
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
