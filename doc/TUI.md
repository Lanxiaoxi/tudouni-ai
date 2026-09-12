如果你的目标不是“做一个能在终端里打印 Agent 输出的 CLI”，而是做成 **Claude Code 这种真正的 Agent TUI**，我非常建议你把它理解成：

> **Agent Runtime + Event Protocol + TUI Renderer**

而不是单纯“给 Agent 加个终端界面”。

Claude Code 这类产品的核心其实是：模型持续流式输出 → 调工具 → 工具产生结果 → 再回模型 → TUI 实时把整个过程以结构化方式呈现出来。公开的逆向分析和一些 Claude Code 风格 TUI 项目也基本都采用了这种思路。([GitHub][1])

---

# 一、我最推荐你的总体路线

结合你现在已经在做 **Python Agent 框架**，我会建议你：

```text
                 ┌──────────────────────┐
                 │      用户输入         │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │       TUI 层         │
                 │   React + Ink        │
                 └──────────┬───────────┘
                            │
                     Event / Command
                            │
                            ▼
                 ┌──────────────────────┐
                 │    Agent Runtime     │
                 │       Python         │
                 │                      │
                 │  LLM                 │
                 │  Tool                │
                 │  Memory              │
                 │  Planner             │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │     Tool System      │
                 │ shell / file / git   │
                 │ browser / search ... │
                 └──────────────────────┘
```

最重要的一点：

**不要让 TUI 直接依赖你的 Agent 内部类。**

而是让 Agent 对外暴露一种稳定的 **Event Stream**。

比如：

```text
Agent
  ↓
AgentEvent
  ↓
TUI
```

这样以后你不仅能有 TUI，还能很容易增加：

```text
TUI
Web UI
VSCode Extension
HTTP API
Desktop GUI
```

它们全部复用同一个 Agent Runtime。

---

# 二、第一步不要急着做 Claude Code 那么复杂

建议分成 5 个阶段。

---

## Phase 1：先做最小 TUI

第一版只实现：

```text
╭────────────────────────────────────────────╮
│ MyAgent                                    │
╰────────────────────────────────────────────╯

You:
> 帮我分析这个项目

Agent:
我先检查项目结构……

  └─ tool: list_files
     src/
     tests/
     package.json

我发现这是一个 React 项目……

──────────────────────────────────────────────
> 输入消息...
```

只需要实现：

### 1. 输入框

```text
>
```

### 2. 消息区域

```text
User message
Assistant message
```

### 3. 流式输出

例如模型返回：

```text
Hello
```

然后：

```text
Hello, I
```

然后：

```text
Hello, I will
```

最终：

```text
Hello, I will analyze your project.
```

TUI 要不断刷新。

### 4. 简单 Markdown

至少支持：

````text
# heading

**bold**

`code`

```python
print("hello")
````

````

---

# 三、技术选型：我反而建议你用 React + Ink

你之前已经在学 React，所以这里非常适合。

目前 Claude Code 风格 TUI 的生态里，React/Ink 也是一个非常典型的路线；公开资料对 Claude Code 的 UI 描述就是 React/Ink 风格的 retained UI + Yoga 布局。:contentReference[oaicite:1]{index=1}

你的结构可以直接变成：

```text
React
   ↓
Ink
   ↓
Terminal
````

例如：

```tsx
<App>
    <Header />
    <Conversation />
    <ToolCalls />
    <Input />
</App>
```

你会发现它和 React Web 开发思想非常接近：

```tsx
function ChatMessage() {
  return (
    <Box flexDirection="column">
      <Text>User</Text>
      <Text>Hello</Text>
    </Box>
  )
}
```

---

# 四、第二步：把 Agent 变成 Event Stream

这个才是整个项目最重要的架构。

不要让 TUI 收这种：

```python
agent.run()
```

然后等：

```python
result = agent.run()
```

最后一下把结果给 UI。

这样做不了 Claude Code 那种体验。

你应该变成：

```python
async for event in agent.run():
    yield event
```

例如：

```python
class AgentEvent:
    type: str
    data: dict
```

事件类型：

```text
message_start
message_delta
message_end

tool_start
tool_input
tool_output
tool_end

thinking_start
thinking_delta
thinking_end

error

agent_start
agent_end
```

例如：

```json
{
  "type": "message_delta",
  "data": {
    "text": "我先检查"
  }
}
```

Tool：

```json
{
  "type": "tool_start",
  "data": {
    "name": "bash",
    "input": {
      "command": "ls -la"
    }
  }
}
```

结束：

```json
{
  "type": "tool_end",
  "data": {
    "name": "bash",
    "output": "src\npackage.json\nREADME.md"
  }
}
```

---

# 五、为什么这个 Event Protocol 极其重要

假设你的 Agent 以后有：

```text
Python Agent
       ↓
AgentEvent
       ↓
┌───────────────┬───────────────┬───────────────┐
│ Ink TUI       │ Web UI        │ VSCode        │
└───────────────┴───────────────┴───────────────┘
```

那么：

### TUI

```text
> fix login bug

⠋ Thinking...

🔧 bash
$ grep -R "login" src/

✓ 12 files found

✏ editing
src/login.ts

✓ done
```

### Web

完全可以显示成：

```text
Thinking...
────────────────
Tool Call
bash
────────────────
Editing login.ts
```

### VSCode

甚至可以把：

```text
tool_end
```

转化成 IDE 中的 diff。

所以：

> **Event Protocol 实际上就是你的 Agent UI API。**

---

# 六、第三步：开始实现 Claude Code 风格的 Tool UI

这是 TUI 真正开始“像 Agent”的地方。

比如 Agent 执行：

```text
read_file
```

不要简单输出：

```text
reading src/main.py
```

而应该渲染成一个组件：

```text
╭─ Read File ──────────────────────────╮
│ src/main.py                          │
│                                      │
│ 1  import os                         │
│ 2  from agent import Agent            │
│ 3                                    │
│ 4  def main():                       │
│ 5      ...                            │
╰──────────────────────────────────────╯
```

---

# 七、工具至少设计成这几类 UI

我建议：

```text
ToolCall
 ├── BashTool
 ├── FileReadTool
 ├── FileWriteTool
 ├── FileEditTool
 ├── SearchTool
 └── GenericTool
```

例如 Bash：

```text
╭─ Bash ───────────────────────────────╮
│ $ npm test                            │
│                                      │
│ PASS src/test/login.test.ts           │
│ PASS src/test/user.test.ts            │
│                                      │
│ ✓ exit code 0                         │
╰──────────────────────────────────────╯
```

文件修改：

```text
╭─ Edit ───────────────────────────────╮
│ src/auth/login.py                    │
│                                      │
│ - token = get_token()                │
│ + token = await get_token()          │
│                                      │
╰──────────────────────────────────────╯
```

---

# 八、第四步：一定要做 Approval System

这是 Claude Code 类 Agent 非常核心的一部分。

例如 Agent 想执行：

```bash
rm -rf build/
```

不能直接跑。

TUI：

```text
╭─ Permission Required ────────────────╮
│                                      │
│ Agent wants to execute:              │
│                                      │
│ $ rm -rf build/                      │
│                                      │
│ Allow?                               │
│                                      │
│ [y] Yes                              │
│ [n] No                               │
│ [a] Always allow                     │
╰──────────────────────────────────────╯
```

架构：

```text
Agent
  │
  ▼
ToolRequest
  │
  ▼
Permission Manager
  │
  ▼
TUI
  │
  ▼
User
  │
  ▼
Approve / Reject
```

你的 Agent Framework 后期甚至可以统一定义：

```python
class PermissionPolicy:
    async def check(tool_call):
        ...
```

比如：

```text
read_file      → auto
grep           → auto
git diff       → auto
npm test       → auto
git push       → ask
rm             → ask
sudo           → deny
```

这会让你的框架突然从：

> “一个调用 LLM 的程序”

升级成：

> **真正的 Agent Runtime。**

---

# 九、第五步才是复杂的 TUI 交互

等前面的功能稳定以后，再做这些：

### 滚动

```text
↑ ↓
```

### 快速滚动

```text
PageUp
PageDown
```

### Ctrl+C

不是直接退出，而是：

```text
interrupt current generation
```

### Ctrl+O

切换：

```text
tool output collapsed
tool output expanded
```

一些 Claude Code 风格实现也采用类似的工具调用折叠/展开交互。([GitHub][2])

### `/` 命令

例如：

```text
/help
/clear
/compact
/model
/tools
/config
/exit
```

---

# 十、你最终会需要一个真正的状态机

这是我特别建议你注意的一点。

不要让 UI 里到处写：

```typescript
if (loading) ...
if (toolCalling) ...
if (thinking) ...
```

最终一定会失控。

最好定义：

```text
AgentState

IDLE

THINKING

STREAMING

TOOL_PENDING

TOOL_RUNNING

WAITING_PERMISSION

ERROR

COMPLETED
```

例如：

```text
                ┌──────────┐
                │   IDLE   │
                └────┬─────┘
                     │ user input
                     ▼
                ┌──────────┐
                │ THINKING │
                └────┬─────┘
                     │
              ┌──────┴──────┐
              │             │
              ▼             ▼
         STREAMING      TOOL_PENDING
                            │
                            ▼
                    WAITING_PERMISSION
                            │
                         approve
                            │
                            ▼
                       TOOL_RUNNING
                            │
                            ▼
                         THINKING
```

这个状态机后面会非常有用。

---

# 十一、还有一个非常重要的东西：Session

Claude Code 类产品之所以好用，很大程度上是因为：

```text
session
```

不是一次命令。

你应该从一开始就设计：

```text
Session
 ├── id
 ├── created_at
 ├── project
 ├── messages
 ├── tool_calls
 ├── state
 ├── model
 └── metadata
```

例如：

```text
~/.myagent/
    sessions/
        2026-09-12/
            abc123.jsonl
            def456.jsonl
```

然后：

```bash
myagent
```

可以：

```text
New Session
Resume Session
List Sessions
Search Sessions
```

---

# 十二、数据存储最好使用 JSONL

例如：

```text
session.jsonl
```

内容：

```json
{"type":"user","content":"帮我分析项目"}
{"type":"assistant","content":"我先检查项目结构"}
{"type":"tool_start","name":"ls"}
{"type":"tool_end","output":"src tests package.json"}
{"type":"assistant","content":"这是一个 React 项目"}
```

为什么我推荐 JSONL？

因为 Agent 是天然的 append-only stream。

而且：

```text
实时写入
崩溃恢复
session replay
调试
日志分析
```

都非常方便。

---

# 十三、你的 Agent 和 TUI 怎么通信？

这里其实有三个方案。

## 方案 A：同一个进程

```text
Node
 ├── TUI
 └── Agent
```

优点：

```text
简单
```

缺点：

```text
Python Agent 不好直接和 Ink 放在一起
```

---

## 方案 B：Python Agent + Node TUI

我更推荐这个。

```text
             stdin/stdout
Node TUI  <----------------> Python Agent
```

例如：

```text
Node
    ↓
python -m myagent
```

双方使用：

```text
JSONL
```

通信。

比如 TUI：

```json
{"type":"user_message","content":"分析项目"}
```

Agent：

```json
{"type":"assistant_delta","text":"我先"}
```

Agent：

```json
{"type":"assistant_delta","text":"检查项目结构"}
```

Agent：

```json
{"type":"tool_start","tool":"bash"}
```

---

# 十四、这种架构我非常推荐

最终你的项目可以长成：

```text
my-agent/
│
├── agent/
│   ├── runtime/
│   │   ├── loop.py
│   │   ├── state.py
│   │   └── events.py
│   │
│   ├── tools/
│   │   ├── bash.py
│   │   ├── file_read.py
│   │   ├── file_write.py
│   │   └── search.py
│   │
│   ├── memory/
│   ├── permission/
│   └── session/
│
├── protocol/
│   └── events.json
│
├── tui/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── Message.tsx
│   │   │   ├── ToolCall.tsx
│   │   │   ├── DiffView.tsx
│   │   │   ├── Permission.tsx
│   │   │   └── Input.tsx
│   │   │
│   │   ├── state/
│   │   └── protocol/
│   │
│   └── package.json
│
└── cli/
    └── main.py
```

然后：

```bash
myagent
```

启动：

```text
Node TUI
   │
   │ JSONL
   ▼
Python Agent
```

---

# 十五、真正做的时候，我建议你按这个顺序

不要一上来就做 20 个功能。

### 第 1 周

只完成：

```text
TUI
 ↓
User Input
 ↓
Python Agent
 ↓
Streaming Response
 ↓
Markdown
```

也就是：

```text
┌─────────────────────────────┐
│ My Agent                    │
│                             │
│ User:                       │
│ 分析这个项目                 │
│                             │
│ Agent:                      │
│ 我先检查项目结构……           │
│                             │
│ >                           │
└─────────────────────────────┘
```

---

### 第 2 阶段

加入：

```text
tool_start
tool_end
```

然后：

```text
Agent
 ↓
Tool
 ↓
Tool Result
 ↓
Agent
```

TUI 能显示：

```text
🔧 bash

$ ls

src
tests
README.md

✓ done
```

---

### 第 3 阶段

加入：

```text
Permission
```

让：

```text
Bash
WriteFile
EditFile
Git
```

拥有权限等级。

---

### 第 4 阶段

加入：

```text
Session
Resume
History
```

---

### 第 5 阶段

才开始做：

```text
Diff
Autocomplete
Slash Command
Multi-line Input
Mouse
Scrolling
Themes
Token usage
Context usage
Sub-agent
Parallel tools
```

---

# 十六、一个很容易踩的大坑

**不要先研究怎么把 TUI 做得“漂亮”。**

你真正应该优先解决的是：

```text
事件模型
状态机
Tool 生命周期
Permission 生命周期
Session 生命周期
```

因为真正难的是这些：

```text
LLM
 ↓
stream
 ↓
tool call
 ↓
permission
 ↓
tool execution
 ↓
tool result
 ↓
LLM
 ↓
stream
 ↓
...
```

这个循环必须非常稳定。

UI 只是把这个状态实时投影出来。

Claude Code 的终端界面也不是简单的 `console.log`；它涉及 retained UI、键盘/鼠标输入处理、屏幕 diff、终端状态恢复等一整套渲染生命周期。([GitHub][3])

---

# 十七、如果是我来做你的框架

我会直接选择：

```text
Agent Runtime
    Python

Protocol
    JSONL

TUI
    TypeScript
    React
    Ink

Session
    JSONL

Terminal
    ANSI / Ink

Package
    npm + pip
```

整体：

```text
                   myagent
                      │
              ┌───────┴───────┐
              │               │
          TUI (Node)      Agent (Python)
              │               │
              └────── JSONL ──┘
                      │
               Agent Event
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Message       Tool      Permission
          │           │           │
          └───────────┼───────────┘
                      ▼
                   Session
```

而且这条路线非常适合你现在的技术背景：**React 负责 TUI，Python 保留你现有 Agent 核心**，两边通过协议解耦，不需要你重写 Agent。

你甚至可以把最终目标定成：

```text
              ┌───────────────┐
              │  Agent Core   │
              │    Python     │
              └───────┬───────┘
                      │
                Agent Protocol
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
   Claude-like       Web UI       VSCode
      TUI
```

这比“给 Python Agent 写一个 CLI”高一个层级——你实际上是在给 Agent 框架设计一套 **UI/Runtime Protocol**。

另外，社区里已经有一些 Claude Code 风格 TUI 实现可用来研究交互设计，例如基于 `pi-tui` 的项目，以及使用 Bubble Tea 做 Agent Dashboard 的实现，很适合拿来观察工具卡片、折叠、session、状态展示这些具体交互。([GitHub][2])

如果你现在准备正式动手，我建议下一步就直接做 **“Python Agent + React/Ink TUI + JSONL Event Protocol” 的最小可运行骨架**，先把目录结构、事件定义和第一版 `App.tsx` / Python `events.py` 搭起来。

[1]: https://github.com/Windy3f3f3f3f/how-claude-code-works/blob/main/en/docs/12-user-experience.md?utm_source=chatgpt.com "how-claude-code-works/en/docs/12-user-experience.md at main · Windy3f3f3f3f/how-claude-code-works · GitHub"
[2]: https://github.com/dsh-tui/dsh-tui?utm_source=chatgpt.com "GitHub - dsh-tui/dsh-tui: Claude Code-style terminal UI for DeepSeek Harness agents, as an out-of-tree dsh plugin bundle · GitHub"
[3]: https://github.com/vibewatch/claude-code-internals/blob/main/docs/01-runtime-lifecycle/terminal-ui-renderer-and-input.md?utm_source=chatgpt.com "claude-code-internals/docs/01-runtime-lifecycle/terminal-ui-renderer-and-input.md at main · vibewatch/claude-code-internals · GitHub"
