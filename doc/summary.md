# Agent Runtime 实现路线要点

> 核心原则：先让 Runtime 可靠地执行一个工具调用，再执行多轮工具调用；先保证单 Agent 稳定，再扩展到规划、并发和多 Agent。不要一开始就实现"完整 Runtime"。

## 总体路线

模型调用 → 单轮工具调用 → 多轮 Agent Loop → 工具注册与参数校验 → 状态与上下文管理 → 权限/安全/审批 → 错误恢复与可观测性 → 任务编排与子 Agent → 服务化和生产部署。

起点：单 Agent、单进程、命令行交互、少量工具，逐步扩展。

## 各阶段要点

### 一、最小模型适配层
- 上层 Agent 调用统一接口，业务代码不直接依赖具体 SDK，屏蔽不同模型 API 差异。
- 定义 `ModelResponse`（content / tool_calls / raw）与 `ChatModel` 抽象接口 `complete(messages, tools)`。
- 实现 `OpenAICompatibleModel` 适配器（兼容 DeepSeek、GLM 等）。
- 只解决：调用模型、传递消息、接收文本、接收工具调用。暂不做记忆、规划、子 Agent、复杂权限。

### 二、工具系统
- Runtime 的核心基础。先只实现三个工具：`read_file`、`write_file`、`list_files`。**不要一开始就开放任意 Shell。**
- 统一工具接口 `Tool`（name / description / schema / handler）。
- 工具注册表 `ToolRegistry`：注册、获取、导出 schemas。
- 文件工具从第一天就做 **workspace 路径隔离**，防止越界访问（如 `../../.ssh/id_rsa`）。

### 三、最小 Agent Loop
- 循环：接收请求 → 调用模型 → 有文本则结束 / 有工具调用则校验、执行、回填结果 → 再次调用模型。
- 要点：模型适配器、工具定义/注册/调用、结果反馈、多轮循环、**最大步数限制**、基础异常处理。
- 建议先用它完成任务：读取并总结文件、查找关键词、创建新文件、修改指定文件。

### 四、参数校验
- **不要完全相信模型返回的参数**，工具参数必须经过 Schema 校验。
- 用 Pydantic 定义参数模型，工具显式绑定（`ValidatedTool`）。
- 解决：缺参、类型错误、多余参数、空路径、非法枚举、参数过长。

### 五、权限与审批
- 工具按风险分级：低（读文件/列目录/搜索）、中（写文件/编辑/git diff）、高（Shell/删除/git commit/网络）。
- `PermissionPolicy` 按风险等级决定自动批准或人工确认。
- 限制能力边界：文件仅限 workspace、Shell 限制工作目录与超时、禁危险命令、限制环境变量与网络、记录每次调用。
- **禁止直接用于生产**：`subprocess.run(command, shell=True)`。若需 Shell，至少要加命令解析、白/黑名单、超时、目录限制、输出长度限制、用户审批、沙箱隔离。

### 六、状态抽离
- 把 `messages` 从 `run()` 局部变量抽离为 `AgentState`（session_id / messages / step_count / metadata）。
- Session 维护用户与 Assistant 消息、工具调用与结果、任务状态、步数、错误、元数据。
- 先用 JSON 或 SQLite 存储，暂不上 Redis 或复杂数据库。
- 支撑：中断恢复、多轮对话、查看历史、失败重试、暂停继续。

### 七、上下文管理
- 分三层：① 限制工具输出（超出长度截断）；② 限制历史消息（保留系统消息、当前任务、最近工具调用、关键状态、错误）；③ 历史过长时生成摘要。
- **压缩不是简单删除旧消息**，必须保留事实性状态：已改文件、已跑测试、失败操作、剩余任务、用户原始目标。

### 八、错误恢复与重试
- 分级处理：可自动重试（网络/超时/限流/服务端不可用）；交给模型修正（参数错/文件不存在/非零退出/JSON 错）；直接停止（权限拒绝/越界/高风险未批准/超步数/危险命令）。
- 工具结果结构化：`{ok, error_type, message, retryable}`，便于判断重试 / 修正 / 请求确认 / 终止。不要只返回无结构错误文本。

### 九、日志与可观测性
- 关键不是看最终回答，而是查看完整执行轨迹。
- 记录事件：run_started、model_request/response、tool_call_requested/approved、tool_started/finished/failed、context_compacted、run_finished/stopped。
- 事件字段：session_id、run_id、step、event、tool_name、duration_ms、success。
- 可用于分析：哪个工具最易失败、是否重复调用、哪些任务常超时、上下文何时过长、成本最高工具、任务失败原因。

### 十、并发、子 Agent 与任务编排
- 放到后期，因为会显著增加状态管理与错误处理复杂度。
- 顺序：① 任务计划（分析→生成列表→逐个执行→检查→汇总）；② 并行只读任务（相互独立、不写同一文件、可单独汇总、失败不破坏整体）；③ 子 Agent（主/搜索/测试/文档各司其职，**不给所有子 Agent 全部工具和权限**）。

## 推荐最终项目结构

```
agent_runtime/
├── app/          main.py, cli.py
├── models/       base.py, openai_compatible.py, streaming.py, types.py
├── tools/        base.py, registry.py, filesystem.py, search.py, shell.py, schemas.py
├── runtime/      agent.py, loop.py, permissions.py, retry.py, context.py, events.py
├── state/        models.py, store.py, sqlite_store.py
├── planning/     planner.py, task.py, scheduler.py
├── security/     path_policy.py, command_policy.py, sandbox.py
└── tests/        test_tools.py, test_loop.py, test_permissions.py, test_state.py
```

## 先做的 MVP（13 项）
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

## 暂时不要做
多 Agent、自动规划、向量数据库记忆、浏览器自动化、任意 Shell、分布式任务调度、复杂工作流引擎、MCP/A2A 集成。

当 MVP 能稳定完成"读取文件、修改文件、检查结果"后，再增加状态恢复、上下文压缩、权限审批和任务规划。
