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

## 架构

```
main.py            组装：把下面这些接起来（薄入口）
cli.py             参数解析、会话选择、交互循环、历史与审计的展示
config.py          配置从环境变量来（源码里不留密钥）

models/            模型适配层
  base.py            ChatModel 抽象：complete(messages, tools) -> ModelResponse
  types.py           ModelResponse / TokenUsage / 三个领域异常
  openai_compatible.py  OpenAI 兼容实现：把 SDK 的响应结构和异常都归一化掉

tools/             工具层
  tool.py            Tool(名字/描述/风险/参数模型/handler) + ToolRegistry
  builtin.py         内置文件工具的装配（参数模型 + 风险等级）
  filesystem.py      文件操作 + safe_path（工作区边界）

security/          权限层
  policy.py          PermissionPolicy：纯函数，只裁定 ALLOW / DENY / ASK
  gate.py            关卡：把策略 + asker 变成一次裁决
  asker.py           询问方式（CLI 版走终端；测试版是脚本化的假实现）

state/             状态层
  session.py         Session（会话的全部事实）+ session_id 合法性
  store.py           JsonSessionStore：原子写、id 白名单、容忍未知字段

audit/             审计层
  events.py          事件构造
  jsonl.py           JsonlSink：只追加的 .jsonl，天然抗崩溃

agents/            编排层
  agent.py           Agent：一个回合的循环
  retry.py           重试策略（只重试暂时性失败）

tests/             77 个测试，跑完 0.3 秒
```

依赖方向是单向的，无环：

```
models   （无内部依赖）
tools    （无内部依赖）
state    （无内部依赖）
security → tools
audit    → state
agents   → audit, models, security, state, tools
main     → 全部
```

## 贯穿全局的三个设计原则

### 1. 判定留在内部，沟通交给注入的实现

三个注入点，同一条原则：

| 注入点 | Agent 知道 | 注入的实现知道 |
|---|---|---|
| `asker` | 该不该问 | 怎么问（终端 / 测试 / 将来的 Web） |
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

**成本和"聊了多少轮"关系不大。** 实测一轮 5 步的任务里，一次 `read_file` 返回
12524 字符，占了整轮成本的 86%。未命中缓存的输入比命中贵约 50 倍，所以
`--audit` 里那个缓存命中率比总 token 数更值得看。

## 数据落在哪里

| 目录 | 内容 | 进版本库吗 |
|---|---|---|
| `.env` | 本地密钥与配置（模板见 `.env.example`） | 否 |
| `.sessions/` | 会话状态（含工具读到的文件正文） | 否 |
| `.logs/` | 审计轨迹（含工具参数预览） | 否 |

前两者是本地配置和运行数据，不是源码 —— `.gitignore` 里都排除了。
`.env.example` 是例外：它是给人看的模板、不含真密钥，所以**它是要提交的**（`gitignore`
里那条 `.env.*` 后面跟了一句 `!.env.example` 把它重新包含回来）。

## 已知的取舍

- **`package = false` + `sys.path` 修补。** `pyproject.toml` 位于包目录内，所以项目
  根就是包本身，uv 无法把它当包安装。`main.py` 因此自己把父目录塞进 `sys.path`，
  `conftest.py` 做同一件事。想彻底解决要把项目根上移一级或改成嵌套布局，代价是
  一次大搬迁 —— 暂时不值得。
- **`doc/` 里的文件。** `guide.md` 是本项目的分阶段设计文档；`summary.md` 是 Agent
  自己读 `guide.md` 之后写的摘要 —— 顺便当作"它真的能干活"的样例。
- **`tools/filesystem.py` 里的 `safe_path`。** 它其实是一条安全策略，按职责该住在
  `security/`。留在工具里的原因是它和文件操作绑得太紧，搬走会让两边都变难读。

## 尚未实现

- 上下文管理（按实测成本，当前规模下截断不划算）
- 任务编排、子 Agent、并发
- 服务化（Web / API）

## 许可

MIT，见 [LICENSE](LICENSE)。
