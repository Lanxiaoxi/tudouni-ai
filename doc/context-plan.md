可以。结合我们前面讨论的 **Artifact、ContextManager、ToolResultProcessor、Token Budget，以及最关键的 Prompt Cache 稳定性**，我建议你现在把 Context 系统正式定成下面这个版本。

我会把它当成一个 **V1 可落地、以后能演进到 Claude Code 类 Agent 的 Context Architecture**，而不是一开始就做得特别复杂。

---

# 一、最终架构

核心原则只有一句：

> **Tool 负责产生信息，Artifact 负责保存信息，ContextManager 负责决定哪些信息进入当前上下文，Renderer 负责在调用 LLM 时把它们渲染出来。**

整体：

```text
                         ┌─────────────────────┐
                         │     AgentRuntime    │
                         └──────────┬──────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                  │
                 ▼                  ▼                  ▼
          ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
          │ ToolRuntime │    │ContextManager│    │    LLM      │
          └──────┬──────┘    └──────┬──────┘    └──────▲──────┘
                 │                  │                  │
                 │ ToolResult       │ ContextState     │
                 ▼                  │                  │
        ┌─────────────────┐         │          ┌───────┴───────┐
        │ToolResultProcessor│        │          │ContextRenderer│
        └────────┬────────┘         │          └───────▲───────┘
                 │                  │                  │
                 │ Artifact         │                  │
                 ▼                  ▼                  │
        ┌──────────────────────────────────┐           │
        │          ArtifactStore           │───────────┘
        └──────────────────────────────────┘
```

但真正重要的是：

```text
ToolResult
    │
    ▼
ToolResultProcessor
    │
    ├── Artifact A
    ├── Artifact B
    └── Artifact C
            │
            ▼
       ArtifactStore
            │
            │ artifact_id
            ▼
      ContextManager
            │
            │ ContextItem
            ▼
      ContextRenderer
            │
            ▼
        LLM Request
```

---

# 二、四个核心对象彻底分开

这是整个设计最重要的地方。

## 1. ToolResult

表示：

> **这次 Tool 执行发生了什么。**

例如：

```python
ToolResult(
    tool_name="read_file",
    success=True,
    output="...",
    metadata={
        "path": "/project/main.py",
        "start_line": 1,
        "end_line": 5000,
    }
)
```

它是**运行时事件**。

生命周期：

```text
Tool call
   ↓
ToolResult
   ↓
Processor
   ↓
Artifact
```

不要让 ToolResult 直接成为 Context。

---

# 三、Artifact：真正的信息载体

Artifact 表示：

> **Agent 产生、发现或者获取的一份可以被以后引用的信息。**

例如：

```text
Artifact
├── id
├── type
├── source
├── content_ref
├── metadata
└── created_at
```

Python：

```python
@dataclass
class Artifact:
    id: str
    type: str
    source: ArtifactSource
    content_ref: str | None
    metadata: dict
    created_at: datetime
```

例如一个文件：

```json
{
  "id": "art_01",
  "type": "file",
  "source": {
    "tool": "read_file",
    "path": "/project/main.py"
  },
  "content_ref": "artifact://art_01/content",
  "metadata": {
    "path": "/project/main.py",
    "start_line": 1,
    "end_line": 5000,
    "size": 120000
  }
}
```

注意：

## Artifact 不应该把超大内容直接塞在对象里

不要：

```python
Artifact(
    id="xxx",
    content="120000 tokens..."
)
```

而是：

```text
Artifact
   │
   └── content_ref
          │
          ▼
     ArtifactStore
          │
          ▼
      actual data
```

这样以后：

* 文件
* 图片
* 搜索结果
* command output
* diff
* 网页
* patch
* execution log

都可以统一成 Artifact。

---

# 四、ArtifactStore

ArtifactStore 只解决一个问题：

> **Artifact 放在哪里，以及怎么取出来。**

例如：

```python
class ArtifactStore:

    async def create(
        self,
        content,
        metadata,
    ) -> Artifact:
        ...

    async def get(
        self,
        artifact_id: str,
    ) -> Artifact:
        ...

    async def read(
        self,
        artifact_id: str,
        start: int | None = None,
        end: int | None = None,
    ):
        ...

    async def delete(
        self,
        artifact_id: str,
    ):
        ...
```

它**绝对不要知道 Context 的存在**。

也就是说不要出现：

```python
artifact_store.add_to_context(...)
```

这种 API。

因为：

```text
ArtifactStore
    ↓
只管理 Artifact

ContextManager
    ↓
只管理 Context
```

职责非常干净。

---

# 五、ContextItem：Artifact 和 Context 之间的桥梁

这是之前设计里面非常关键的一个东西。

Artifact 是：

> “我有什么信息？”

ContextItem 是：

> “我现在想让 LLM 怎么看到这个信息？”

例如：

```python
@dataclass
class ContextItem:
    artifact_id: str

    representation: str

    priority: int = 0
    pinned: bool = False

    sequence: int = 0

    options: dict = field(default_factory=dict)
```

例如：

```python
ContextItem(
    artifact_id="art_001",
    representation="range",
    options={
        "start_line": 1800,
        "end_line": 1900,
    }
)
```

这意味着：

> Artifact `art_001` 本身可能是一个 5000 行文件，但当前 Context 只展示 1800～1900 行。

所以：

```text
Artifact
    = 数据

ContextItem
    = 当前 Context 对数据的使用方式
```

---

# 六、Representation 设计

V1 我建议就定义四种：

```text
metadata
preview
range
full
```

例如一个文件 Artifact：

```text
metadata
    ↓
/project/main.py
5000 lines
120 KB

preview
    ↓
前 100 行

range
    ↓
1800 - 1900 行

full
    ↓
整个文件
```

这样未来 Context 压力大的时候就可以：

```text
full
 ↓
range
 ↓
preview
 ↓
metadata
 ↓
remove
```

这比简单粗暴地：

```text
删除旧 Context
```

高级很多。

---

# 七、ContextManager：整个系统的大脑

ContextManager 不负责存数据。

它负责：

> **决定当前 LLM 应该看到什么。**

核心状态：

```text
ContextState

Stable
Dynamic
```

我建议从 V1 就把这两个概念设计进去。

---

# 八、为什么要 Stable / Dynamic

这是我们前面讨论 **cache hit** 后，我认为最终架构必须加入的东西。

Context 不应该被理解成：

```text
一个不断 append 的 message list
```

而应该是：

```text
                    Context
                       │
          ┌────────────┴────────────┐
          │                         │
      Stable Zone              Dynamic Zone
          │                         │
          ▼                         ▼
    稳定的信息                  经常变化的信息
```

例如：

```text
Stable
├── System Prompt
├── Agent Instructions
├── Project Information
├── User Task
└── Important Artifacts

Dynamic
├── 最近 Tool Result
├── 当前搜索结果
├── 当前读取的代码
└── 临时 Artifact
```

---

# 九、这样设计的核心目的：Cache Stability

你之前担心：

> Artifact 架构是不是反而会降低 cache hit rate？

最终设计里，我们反而主动把这个问题考虑进 Context Architecture。

核心原则：

> **Artifact 化本身不会导致 Cache 降低，真正影响 Cache 的是最终 Render 出来的 Prompt 是否稳定。**

所以我们规定：

## 规则 1：Context 顺序稳定

不要：

```text
A B C
↓
C A B
↓
B C A
```

而尽量：

```text
A
A B
A B C
A B C D
```

也就是：

> 已经进入 Context 的东西尽量不要重新排序。

---

# 十、规则 2：Artifact ID 稳定

例如：

```text
art_001
```

代表某个确定的信息。

不要每次 Render 都产生：

```text
art_random_123
art_random_456
art_random_789
```

如果同一个 Artifact 还是同一个信息，就尽可能保持身份稳定。

---

# 十一、规则 3：Representation 不要无意义变化

比如：

```text
full
↓
range
↓
full
↓
range
```

这种变化会导致 Render 出来的 Prompt 不稳定。

所以 Representation 的变化应该由：

```text
Token Budget
Context Compaction
Agent Decision
```

这些明确事件驱动。

而不是每次调用 LLM 都重新计算一遍。

---

# 十二、最终的 ContextState

我建议：

```python
@dataclass
class ContextState:

    stable_items: list[ContextItem]

    dynamic_items: list[ContextItem]

    system_items: list[ContextItem]

    version: int = 0
```

或者如果你想更加统一：

```python
@dataclass
class ContextState:

    items: list[ContextItem]

    version: int = 0
```

然后 ContextItem 增加：

```python
zone: Literal["stable", "dynamic"]
```

V1 我其实更推荐第二种：

```python
@dataclass
class ContextItem:
    artifact_id: str
    representation: str

    zone: str = "dynamic"

    priority: int = 0
    pinned: bool = False

    sequence: int = 0

    options: dict = field(default_factory=dict)
```

这样以后扩展更加容易。

---

# 十三、ContextManager API

最终我建议保持非常简单：

```python
class ContextManager:

    def add(
        self,
        artifact_id: str,
        representation: str = "auto",
        *,
        zone: str = "dynamic",
        priority: int = 0,
        pinned: bool = False,
        options: dict | None = None,
    ):
        ...

    def remove(
        self,
        artifact_id: str,
    ):
        ...

    def update(
        self,
        artifact_id: str,
        *,
        representation: str | None = None,
        priority: int | None = None,
        pinned: bool | None = None,
        options: dict | None = None,
    ):
        ...

    def get_items(self):
        ...

    def get_stable_items(self):
        ...

    def get_dynamic_items(self):
        ...

    async def build(self):
        ...
```

但是这里有一个重要设计：

## `build()` 不应该直接生成最终 prompt

因为：

```text
ContextManager
```

不应该负责：

```text
Artifact → LLM text
```

这个事情交给 Renderer。

---

# 十四、ContextRenderer

所以：

```text
ContextManager
      │
      │ ContextItems
      ▼
ContextRenderer
      │
      │ resolve Artifact
      │ resolve representation
      ▼
LLM Messages
```

例如：

```python
class ContextRenderer:

    async def render(
        self,
        context_items: list[ContextItem],
    ) -> list[Message]:
        ...
```

内部：

```python
for item in context_items:

    artifact = await artifact_store.get(
        item.artifact_id
    )

    content = await render_representation(
        artifact,
        item.representation,
        item.options,
    )

    messages.append(
        Message(
            role="user",
            content=content,
        )
    )
```

---

# 十五、这样就解决你现在最大的问题

你现在大概率是：

```text
read_file
   ↓
ToolResult
   ↓
messages.append(
    huge_file_content
)
```

所以：

```text
Context
├── User
├── read_file 5000 lines
├── assistant
├── search result 3000 lines
├── assistant
├── read_file 8000 lines
└── ...
```

最终：

```text
Token ↑↑↑
```

而新的架构：

```text
read_file
   ↓
ToolResult
   ↓
ToolResultProcessor
   ↓
Artifact
   ↓
ArtifactStore

ContextManager
   ↓
ContextItem(
    artifact_id="xxx",
    representation="range"
)
```

最终 LLM 看到的可能只是：

```text
Relevant code from main.py
Lines 1800-1900:

...
```

而不是整个 5000 行文件。

---

# 十六、ToolResultProcessor

这里再加一层：

```python
class ToolResultProcessor:

    async def process(
        self,
        result: ToolResult,
    ) -> list[Artifact]:
        ...
```

例如：

```text
read_file
    ↓
ToolResult
    ↓
Processor
    ├── File Artifact
    └── Metadata Artifact
```

或者：

```text
search
    ↓
ToolResult
    ↓
Processor
    ├── SearchResult Artifact
    ├── MatchingFile Artifact
    └── Summary Artifact
```

这样以后 Tool 可以越来越复杂，而 Context 层完全不用跟着改。

---

# 十七、完整的一次 Agent Loop

最终一次 Tool 调用应该变成：

```text
                    Agent
                      │
                      ▼
                 LLM Request
                      │
                      ▼
                  Tool Call
                      │
                      ▼
                ToolRuntime
                      │
                      ▼
                  ToolResult
                      │
                      ▼
             ToolResultProcessor
                      │
             ┌────────┴────────┐
             ▼                 ▼
         Artifact A        Artifact B
             │                 │
             └────────┬────────┘
                      ▼
                ArtifactStore
                      │
                      ▼
                ContextManager
                      │
                decide / add
                      │
                      ▼
                ContextState
                      │
                      ▼
               TokenBudget
                      │
                      ▼
               ContextRenderer
                      │
                      ▼
                 LLM Request
```

这个结构就非常完整了。

---

# 十八、TokenBudget 放在哪里

我建议：

```text
ContextManager
       │
       ▼
 ContextSelector
       │
       ▼
  TokenBudget
```

但是 **V1 不需要真的做一个复杂 Selector**。

先：

```python
class ContextBudget:

    max_tokens: int
    reserved_tokens: int

    def estimate(...):
        ...

    def fit(...):
        ...
```

当 Context 超过限制：

```text
full
 ↓
range
 ↓
preview
 ↓
metadata
 ↓
remove
```

按照：

```text
priority
pinned
zone
sequence
```

综合处理。

---

# 十九、Pined Artifact

这个也建议 V1 就加入。

例如：

```python
ContextItem(
    artifact_id="project_spec",
    representation="full",
    pinned=True,
)
```

代表：

> 这个东西不能因为 Token 不够就随便删。

例如：

```text
System
Project Instructions       pinned
Current Task               pinned

Recent Search
Read File
Shell Output
Temporary Result
```

Token 不够的时候：

```text
Temporary Result
    ↓
remove

Shell Output
    ↓
preview

Read File
    ↓
range

Project Instructions
    ↓
保留
```

---

# 二十、一个非常重要的原则：Context ≠ History

我建议你以后把：

```text
Conversation History
```

和：

```text
Context
```

概念分开。

因为：

```text
History
```

回答：

> Agent 过去发生过什么？

而：

```text
Context
```

回答：

> Agent 当前这一轮需要让 LLM 看到什么？

这两个概念不是一回事。

最终：

```text
Agent History
       │
       ├── Tool executions
       ├── Assistant messages
       ├── User messages
       └── Artifacts
       
       ↓

ContextManager
       │
       └── Select what is relevant now
```

这样以后你做：

* Context compaction
* memory
* long-running agent
* session resume
* sub-agent
* tool result compression

都会舒服很多。

---

# 二十一、我建议最终目录结构

结合你现在 Python Runtime，我建议直接：

```text
runtime/
│
├── agent/
│   ├── agent.py
│   └── loop.py
│
├── tools/
│   ├── runtime.py
│   ├── result.py
│   └── ...
│
├── llm/
│   ├── client.py
│   └── message.py
│
└── context/
    │
    ├── models.py
    │
    ├── manager.py
    │
    ├── artifact_store.py
    │
    ├── tool_result_processor.py
    │
    ├── renderer.py
    │
    └── budget.py
```

---

# 二十二、每个文件只负责这些事情

| 模块                         | 职责                                    |
| -------------------------- | ------------------------------------- |
| `models.py`                | Artifact / ContextItem / ContextState |
| `artifact_store.py`        | Artifact 持久化、读取                       |
| `tool_result_processor.py` | ToolResult → Artifact                 |
| `manager.py`               | Context 状态管理                          |
| `renderer.py`              | Context → LLM Messages                |
| `budget.py`                | Token 预算、压缩/降级                        |

非常清晰。

---

# 二十三、V1 不要做的东西

这一点我反而觉得很重要。

现在先**不要**加入：

```text
❌ LLM 自动判断 Context relevance
❌ Vector DB
❌ Embedding
❌ 自动 Summary
❌ 自动 Memory
❌ Semantic Context Retrieval
❌ Context Graph
❌ 多级缓存系统
❌ 复杂的 Context Optimizer
```

这些都属于后续能力。

你的 V1 只需要做到：

```text
ToolResult
     ↓
Artifact
     ↓
ContextItem
     ↓
ContextManager
     ↓
Budget
     ↓
Renderer
     ↓
LLM
```

先把这条链跑通。

---

# 二十四、以后可以自然演进成这样

等 V1 稳定之后：

```text
                    ContextManager
                          │
             ┌────────────┼────────────┐
             │            │            │
             ▼            ▼            ▼
         Selector      Budget      Compactor
             │            │            │
             └────────────┼────────────┘
                          ▼
                     ContextState
                          │
                          ▼
                     Renderer
```

再往后：

```text
                    Context System
                          │
       ┌──────────────────┼──────────────────┐
       │                  │                  │
       ▼                  ▼                  ▼
   Artifacts          Memory             Retrieval
       │                  │                  │
       ▼                  ▼                  ▼
 ArtifactStore      MemoryStore        Vector/Search
       │                  │                  │
       └──────────────────┼──────────────────┘
                          ▼
                    ContextManager
```

这时候你的 Runtime 就已经开始接近真正的 Agent Runtime，而不只是：

```text
LLM + Tool Loop
```

---

# 二十五、最终我建议你定下来的架构原则

可以直接把下面这几个作为项目设计原则：

### ① ToolResult 不直接进入 Context

```text
ToolResult
    ↓
Processor
    ↓
Artifact
```

---

### ② Artifact 和 Context 完全解耦

```text
Artifact = Information

Context = Current Visibility
```

Artifact 可以存在，但不一定进入 Context。

---

### ③ ContextItem 描述“如何使用 Artifact”

```text
Artifact
    +
Representation
    +
Priority
    +
Pinned
    +
Zone
```

---

### ④ ContextManager 管状态，不管渲染

```text
ContextManager
    ↓
ContextState

ContextRenderer
    ↓
LLM Messages
```

---

### ⑤ 大数据永远不要直接塞进 ContextState

```text
ContextState
    ↓
artifact_id
```

而不是：

```text
ContextState
    ↓
100000 tokens content
```

---

### ⑥ Stable / Dynamic 从 V1 就保留

```text
Stable
    ↓
尽量不变

Dynamic
    ↓
允许频繁变化
```

这样为 Prompt Cache、Long Context、Compaction 都留下空间。

---

### ⑦ Context 优化优先“降级”，其次“删除”

```text
full
 ↓
range
 ↓
preview
 ↓
metadata
 ↓
remove
```

---

### ⑧ 保持 Context 顺序稳定

这是我们之前讨论 Cache 后，我认为应该正式写进架构设计的一条：

> **不要为了 Context 优化而频繁重排已有 Context。**

---

# 最终版，一张图

如果让我现在替你把整个设计拍板，我会定成：

```text
                         ┌─────────────────┐
                         │   AgentRuntime  │
                         └────────┬────────┘
                                  │
                ┌─────────────────┴─────────────────┐
                │                                   │
                ▼                                   ▼
         ┌─────────────┐                     ┌──────────────┐
         │ ToolRuntime │                     │ ContextManager│
         └──────┬──────┘                     └──────┬───────┘
                │                                   │
           ToolResult                         ContextState
                │                                   │
                ▼                            ┌──────┴──────┐
       ┌─────────────────┐                    │             │
       │ToolResultProcessor│               Stable        Dynamic
       └────────┬────────┘                    │             │
                │                             └──────┬──────┘
                ▼                                    │
          ┌───────────┐                              │
          │ Artifact  │                              │
          └─────┬─────┘                              │
                │                                    │
                ▼                                    │
       ┌────────────────┐                            │
       │ ArtifactStore  │                            │
       └───────┬────────┘                            │
               │                                     │
               └────────────────┬────────────────────┘
                                │
                                ▼
                         ┌──────────────┐
                         │ ContextBudget│
                         └──────┬───────┘
                                │
                    full → range → preview
                                │
                                ▼
                        ┌────────────────┐
                        │ContextRenderer │
                        └───────┬────────┘
                                │
                                ▼
                         ┌─────────────┐
                         │     LLM     │
                         └─────────────┘
```

**我认为这就是你现在最适合落地的 Context V1。**

尤其是你后面还要做类似 Claude Code 的 Agent/TUI，这个设计比较重要的一点是：**TUI、Agent Loop、Tool Runtime 都不需要知道 Context 的内部实现**。以后你甚至可以让 TUI 展示：

```text
Artifacts (23)
Context (8)
  ├── pinned × 2
  ├── stable × 3
  └── dynamic × 3

Token Budget
  82k / 128k
```

而 Runtime 内部依然保持这套边界。这样以后从 Python Runtime 逐步演进到更高性能的实现，也不会把 Context 层和 UI、Tool 层绑死。
