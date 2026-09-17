"""Context 管理：从"工具产生了什么"到"这一轮让 LLM 看见什么"。

    ToolResult ──▶ ToolResultProcessor ──▶ Artifact ──▶ ArtifactStore
                                                            │
                                                            ▼
                                                     ContextManager
                                                            │
                                                     ContextBudget
                                                            │
                                                            ▼
                                                     ContextRenderer
                                                            │
                                                            ▼
                                                       LLM Request

一句话概括每个模块的分工：

| 模块 | 职责 |
|---|---|
| `models.py` | `Artifact` / `ContextItem` / `ContextState` —— 纯数据，JSON 可序列化 |
| `artifact_store.py` | Artifact 的落盘、读取、切片（**不认识 Context**） |
| `processor.py` | 一次工具执行 → Artifact（按工具名可定制） |
| `manager.py` | Context 状态：谁在里面、什么档位、超预算时动谁 |
| `budget.py` | token 估算与降级顺序 |
| `compaction.py` | 历史压缩：较早的详细历史 → 一份结构化工作记忆 |
| `renderer.py` | Context → LLM messages |
| `ref.py` | 历史里那句引用的格式（**唯一定义处**） |

**边界是这一层最要紧的东西**（设计原则第 2 条）：

    Artifact = Information（我有什么）
    Context  = Current Visibility（现在让模型看见什么）

所以 `ArtifactStore` 里永远不会出现 `add_to_context`，而 `ContextManager` 里
永远不会出现正文。这两句话各自成立，整条链才拆得开。

**`compaction.py` 是这一层里唯一认识"历史长什么样"的模块**，而它也只回答两个问题：
折叠到第几条、摘要是什么。真正把"摘要把原文替换掉"这件事做出来的是
`agents/agent.py` 的载荷构造 —— 那一处是历史与请求之间唯一的门。
"""

from agent_runtime.context.artifact_store import ArtifactStore, Snippet
from agent_runtime.context.budget import ContextBudget, Degraded, estimate_tokens
from agent_runtime.context.compaction import Compaction, fold_point
from agent_runtime.context.manager import ContextManager
from agent_runtime.context.models import (
    Artifact,
    ArtifactSource,
    ContextItem,
    ContextNote,
    ContextState,
    Representation,
    Zone,
)
from agent_runtime.context.processor import (
    ToolExecution,
    ToolResultProcessor,
    default_processor,
)
from agent_runtime.context.renderer import ContextRenderer, Rendered

__all__ = [
    "Artifact",
    "ArtifactSource",
    "ArtifactStore",
    "Compaction",
    "ContextBudget",
    "ContextItem",
    "ContextManager",
    "ContextNote",
    "ContextRenderer",
    "ContextState",
    "Degraded",
    "Rendered",
    "Representation",
    "Snippet",
    "ToolExecution",
    "ToolResultProcessor",
    "Zone",
    "default_processor",
    "estimate_tokens",
    "fold_point",
]
