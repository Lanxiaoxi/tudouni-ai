不要一开始就实现“完整 Runtime”。建议按照下面的顺序，从一个**单 Agent、单进程、命令行交互、少量工具**开始，逐步扩展。

## 总体路线

模型调用
  ↓
单轮工具调用
  ↓
多轮 Agent Loop
  ↓
工具注册与参数校验
  ↓
状态与上下文管理
  ↓
权限、安全与审批
  ↓
错误恢复与可观测性
  ↓
任务编排、子 Agent、长任务
  ↓
服务化和生产部署

---

# 第一阶段：先实现最小模型适配层

目标是：无论使用 DeepSeek、GLM 还是其他 OpenAI-compatible API，上层 Agent 都调用统一接口。

建议不要让业务代码直接依赖具体 SDK。

agent_runtime/
├── models/
│   ├── base.py
│   ├── openai_compatible.py
│   └── types.py
├── tools/
├── loop/
├── state/
└── main.py

定义统一的模型接口：

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[dict[str, Any]]
    raw: Any = None

class ChatModel:
    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        raise NotImplementedError
```

然后实现一个 OpenAI-compatible 适配器：

```python
from openai import OpenAI

class OpenAICompatibleModel(ChatModel):
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

    def complete(self, messages, tools=None) -> ModelResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or [],
        )

        message = response.choices[0].message

        tool_calls = []
        for call in message.tool_calls or []:
            tool_calls.append({
                "id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
            })

        return ModelResponse(
            content=message.content,
            tool_calls=tool_calls,
            raw=response,
        )
```

这一阶段暂时只解决：

- 如何调用模型
- 如何传递消息
- 如何接收文本
- 如何接收工具调用
- 如何屏蔽不同模型 API 的差异

暂时不要实现记忆、规划、子 Agent 和复杂权限。

---

# 第二阶段：实现工具系统

工具系统是 Runtime 的核心基础。建议先实现三个工具：

- `read_file`
- `write_file`
- `list_files`

不要一开始就开放任意 Shell。

## 1. 统一工具接口

```python
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Any]

    def execute(self, arguments: dict[str, Any]) -> Any:
        return self.handler(**arguments)
```

## 2. 工具注册表

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already exists: {tool.name}")

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")

        return self._tools[name]

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
            for tool in self._tools.values()
        ]
```

## 3. 先实现安全的文件工具

```python
from pathlib import Path

class FileSystem:
    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def safe_path(self, path: str) -> Path:
        target = (self.workspace / path).resolve()

        if target != self.workspace and self.workspace not in target.parents:
            raise PermissionError("Path escapes workspace")

        return target

    def read_file(self, path: str) -> str:
        target = self.safe_path(path)
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = self.safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path}"
```

这里要从第一天就加入路径隔离，避免模型通过类似下面的路径访问工作区之外的文件：

../../.ssh/id_rsa

---

# 第三阶段：实现最小 Agent Loop

这是 Runtime 真正开始工作的地方。

核心循环只有几步：

接收用户请求
  ↓
调用模型
  ↓
模型返回文本？
  ├── 是：结束
  └── 否：返回工具调用
          ↓
      校验工具
          ↓
      执行工具
          ↓
      把结果加入消息
          ↓
      再次调用模型

简化实现如下：

```python
import json

class Agent:
    def __init__(self, model: ChatModel, tools: ToolRegistry):
        self.model = model
        self.tools = tools

    def run(self, user_input: str, max_steps: int = 20) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个可靠的开发助手。"
                    "需要读取或修改文件时，必须使用工具。"
                ),
            },
            {
                "role": "user",
                "content": user_input,
            },
        ]

        for step in range(max_steps):
            response = self.model.complete(
                messages=messages,
                tools=self.tools.schemas(),
            )

            assistant_message = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in response.tool_calls
                ]

            messages.append(assistant_message)

            if not response.tool_calls:
                return response.content or ""

            for call in response.tool_calls:
                result = self._execute_tool(call)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

        return "任务超过最大执行步数，已停止。"

    def _execute_tool(self, call: dict) -> str:
        try:
            tool = self.tools.get(call["name"])
            arguments = json.loads(call["arguments"])

            result = tool.execute(arguments)

            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)

            return result

        except Exception as exc:
            return f"工具执行失败：{type(exc).__name__}: {exc}"
```

这一阶段完成后，你已经有了一个最小 Agent Runtime：

- 模型适配器
- 工具定义
- 工具注册
- 工具调用
- 工具结果反馈
- 多轮循环
- 最大步数限制
- 基础异常处理

建议先用这个版本完成以下任务：

读取一个文件并总结
查找某个关键词
创建一个新文件
修改一个指定文件

---

# 第四阶段：增加参数校验

不要完全相信模型返回的参数。工具参数必须经过 Schema 校验。

可以使用 Pydantic：

```python
from pydantic import BaseModel, Field

class ReadFileArgs(BaseModel):
    path: str = Field(min_length=1)

class WriteFileArgs(BaseModel):
    path: str = Field(min_length=1)
    content: str
```

让工具显式绑定参数模型：

```python
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class ValidatedTool:
    name: str
    description: str
    schema: dict[str, Any]
    args_model: type[BaseModel]
    handler: Callable

    def execute(self, arguments: dict[str, Any]) -> Any:
        validated = self.args_model.model_validate(arguments)
        return self.handler(**validated.model_dump())
```

这一阶段主要解决：

- 缺少参数
- 参数类型错误
- 多余参数
- 空路径
- 非法枚举值
- 参数长度过大

---

# 第五阶段：加入权限与审批机制

工具调用不能全部自动执行。建议把工具按照风险分级：

低风险：
- list_files
- read_file
- search_code

中风险：
- write_file
- edit_file
- git_diff

高风险：
- run_shell
- delete_file
- git_commit
- 网络请求

定义简单的权限策略：

```python
from enum import Enum

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class PermissionPolicy:
    def __init__(self, auto_approve: set[RiskLevel]):
        self.auto_approve = auto_approve

    def check(self, tool_name: str, risk: RiskLevel) -> bool:
        if risk in self.auto_approve:
            return True

        answer = input(
            f"工具 {tool_name} 风险等级为 {risk}，是否执行？[y/N] "
        )

        return answer.lower() == "y"
```

更重要的是限制工具的能力边界：

- 文件工具只能访问 workspace
- Shell 工具限制工作目录
- 设置命令超时时间
- 禁止危险命令
- 限制环境变量
- 限制网络访问
- 记录每一次工具调用

不要把下面这种实现直接用于生产环境：

```python
subprocess.run(command, shell=True)
```

如果以后需要 Shell，至少要加入：

- 命令解析
- 白名单或黑名单
- 超时控制
- 工作目录限制
- 输出长度限制
- 用户审批
- 沙箱隔离

---

# 第六阶段：把状态从局部变量中抽离

当前 `messages` 放在 `run()` 内部，任务结束后就消失了。接下来需要引入 Session 和 State。

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class AgentState:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    step_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
```

Runtime 应该维护：

Session
├── 用户消息
├── Assistant 消息
├── 工具调用
├── 工具结果
├── 当前任务状态
├── 执行步数
├── 错误信息
└── 元数据

建议先使用 JSON 或 SQLite 保存，暂时不需要上 Redis 或复杂数据库。

```python
import json
from pathlib import Path

class JsonStateStore:
    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, state: AgentState):
        path = self.directory / f"{state.session_id}.json"
        path.write_text(
            json.dumps(state.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, session_id: str) -> AgentState:
        path = self.directory / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))

        return AgentState(**data)
```

有了状态存储后，才能支持：

- 任务中断后恢复
- 多轮对话
- 查看执行历史
- 失败后重试
- 任务暂停和继续

---

# 第七阶段：实现上下文管理

当工具调用越来越多，消息会快速增长。上下文管理建议分成几个层次。

## 第一层：限制工具输出

例如文件过大时不要一次性返回全部内容：

```python
def truncate(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text

    return (
        text[:max_chars]
        + "\n\n[内容已截断，原始长度超过限制]"
    )
```

## 第二层：限制历史消息

保留：

- 系统消息
- 用户当前任务
- 最近几轮工具调用
- 关键任务状态
- 错误信息

## 第三层：摘要

当历史消息过长时，让模型生成任务摘要：

任务目标：
已完成：
当前文件：
已尝试的操作：
遇到的问题：
下一步建议：

注意：上下文压缩不是简单删除旧消息。必须保留事实性状态，例如：

- 哪些文件已经修改
- 哪些测试已经执行
- 哪些操作失败
- 当前任务还剩什么
- 用户的原始目标是什么

## 另加一层：工作区说明（`AGENT.md`）

上面三层管的都是"输入太长往回收"，这一层方向相反：**把一小段必须常驻的东西提前塞进去**。

工作区根上放一份 `AGENT.md`，写这个工作区是什么（目录结构、构建/测试命令、代码约定），
在**每个新会话**建的时侯注入到系统提示词的最末尾。这一层值得单独做，因为它是"人写的、
描述这个工作区的背景"，而模型在**决定第一步做什么之前**就需要它 —— 让模型自己 `read_file`
去读的话，每开一个会话都要先花一步猜"这里有没有说明、要不要读"，而猜错的症状是
**它按通用习惯干活**，用户看不出哪里不对。

四个容易做错的地方：

1. **放在系统提示词的最末尾。** 提示词的切分点是"变不变"，不是语义：静态部分（准则）和
   运行环境都稳定，而 AGENT.md 是这两段之外**唯一会被人手改的**。放最后，它改一个字也只
   作废尾巴那一小段；插在前面则会让后面所有 token 每轮按未命中计费。
2. **围栏 + 一句"这不是用户的指令"。** 这份文件可能随仓库一起被 clone，来源不可信。它是
   这个功能里唯一的一道注入防线（和技能正文、`fetch_web` 那两处同一条）。
3. **有额度，而且截断要说出来。** 行数、字符数两道闸，超了从尾部截，并在注入块里和启动
   通知里各说一句 —— 静默截断比不截断更坏：模型以为看到了整份约定，于是按半份办事。
4. **失败一律降级，但都要出声。** 这是可选文件：不存在就一个字都不注入（也不报），而
   编码错了、同名的是目录、文件大得离谱这些**要说给用户听** —— 否则他会一直以为自己那份
   说明生效了。

还有一条属于"沿用已有惯例"的：**改了它只影响此后新建的会话**（system 消息在会话创建时
写一次），和静态提示词完全一致。想中途换版会让同一段对话里前后两半依据两份不同的说明
办事，事后完全看不出来。

---

# 第八阶段：加入错误恢复和重试

错误恢复建议分级处理：

## 可自动重试

- 临时网络错误
- API 超时
- 速率限制
- 服务端暂时不可用

## 交给模型修正

- 工具参数错误
- 文件不存在
- 命令返回非零状态
- JSON 参数格式错误

## 直接停止

- 权限拒绝
- 越界路径
- 高风险操作未批准
- 超过最大执行步数
- 检测到危险命令

工具结果最好结构化：

```python
{
    "ok": False,
    "error_type": "FileNotFoundError",
    "message": "文件不存在",
    "retryable": False,
}
```

不要只返回一段没有结构的错误文本。结构化结果更方便 Runtime 判断接下来应该：

- 重试
- 让模型修正
- 请求用户确认
- 终止任务

---

# 第九阶段：加入日志、事件和可观测性

Runtime 调试的关键不是只看最终回答，而是查看完整执行轨迹。

建议记录这些事件：

run_started
model_request
model_response
tool_call_requested
tool_call_approved
tool_started
tool_finished
tool_failed
context_compacted
run_finished
run_stopped

每个事件至少包含：

```python
{
    "session_id": "...",
    "run_id": "...",
    "step": 3,
    "event": "tool_finished",
    "tool_name": "read_file",
    "duration_ms": 120,
    "success": True,
}
```

后续可以基于这些日志分析：

- 哪个工具最容易失败
- 模型是否重复调用工具
- 哪些任务经常超时
- 上下文何时过长
- 哪些工具成本最高
- Agent 为什么没有完成任务

---

# 第十阶段：再实现并发、子 Agent 和任务编排

这些功能应该放到后面，因为它们会显著增加状态管理和错误处理复杂度。

建议顺序是：

## 1. 先支持任务计划

分析需求
  ↓
生成任务列表
  ↓
逐个执行
  ↓
检查任务结果
  ↓
汇总

## 2. 再支持并行只读任务

例如：

同时搜索三个目录
同时分析多个文件
同时收集不同模块的信息

只有满足以下条件时才适合并行：

- 任务之间相互独立
- 不会同时修改同一个文件
- 结果可以单独汇总
- 失败不会破坏整体状态

## 3. 最后支持子 Agent

子 Agent 应该拥有明确边界：

主 Agent：负责总体任务
代码搜索 Agent：只负责搜索和分析
测试 Agent：只负责运行测试和报告
文档 Agent：只负责生成文档

不要让所有子 Agent 都拥有全部工具和全部权限。

---

# 推荐的最终项目结构

agent_runtime/
├── app/
│   ├── main.py
│   └── cli.py
│
├── models/
│   ├── base.py
│   ├── openai_compatible.py
│   ├── streaming.py
│   └── types.py
│
├── tools/
│   ├── base.py
│   ├── registry.py
│   ├── filesystem.py
│   ├── search.py
│   ├── shell.py
│   └── schemas.py
│
├── runtime/
│   ├── agent.py
│   ├── loop.py
│   ├── permissions.py
│   ├── retry.py
│   ├── context.py
│   └── events.py
│
├── state/
│   ├── models.py
│   ├── store.py
│   └── sqlite_store.py
│
├── planning/
│   ├── planner.py
│   ├── task.py
│   └── scheduler.py
│
├── security/
│   ├── path_policy.py
│   ├── command_policy.py
│   └── sandbox.py
│
└── tests/
    ├── test_tools.py
    ├── test_loop.py
    ├── test_permissions.py
    └── test_state.py

---

# 你现在最应该实现的版本

建议先完成这个 MVP：

1. OpenAI-compatible 模型适配器
2. Tool 数据结构
3. ToolRegistry
4. read_file
5. write_file
6. list_files
7. Agent Loop
8. 最大循环次数
9. JSON 参数解析
10. Pydantic 参数校验
11. workspace 路径隔离
12. 工具执行日志
13. 命令行交互

暂时不要实现：

× 多 Agent
× 自动规划
× 向量数据库记忆
× 浏览器自动化
× 任意 Shell
× 分布式任务调度
× 复杂工作流引擎
× MCP/A2A 集成          （MCP 后来做了，见 README 的「外部工具（MCP）」；A2A 仍然不做）

当这个 MVP 能够稳定完成“读取文件、修改文件、检查结果”时，再继续增加状态恢复、上下文压缩、权限审批和任务规划。

最重要的设计原则是：

> 先让 Runtime 能够可靠地执行一个工具调用，再让它执行多轮工具调用；先保证单 Agent 稳定，再扩展到规划、并发和多 Agent。