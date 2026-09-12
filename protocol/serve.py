"""`--runtime-stdio` 那个进程的主循环。

**它只做三件事**：开传输、装配、把控制权交给 `ProtocolServer.serve()`。
任何别的逻辑（怎么解释消息、什么时候发什么）都属于 `channels` / `state` /
Agent —— 放这里会让"协议"和"启动"缠在一起，而那正是第零期把装配拆出去要避免的事。

## 顺序，以及为什么是这个顺序

    open_stdio()                          # 1. 三件"开工前必须做掉"的事（见 transport_stdio）
    ProtocolServer(transport)             # 2. 先有一个能收发的东西
    boot()                                # 3. store / logs / 技能扫描（不需要密钥）
    channels = server.channels()          # 4. 通道要 memory，所以它在 open_runtime 之后才真能造
    runtime = open_runtime(...)           # 5. 装配：settings → 模型 → 工具 → Agent
    server.attach(runtime)                # 6. 补上另一半 —— 这之前不可能收到请求
    server.serve()                        # 7. 转起来

第 2 步在第 5 步之前，是这一段的核心：`ProtocolServer` 既是传输的读端、又是通道的
提供者，所以它必须比 Runtime 先存在。**不变式**：`attach()` 之前不可能收到请求，
因为只有 runtime 会发请求，而 runtime 要等 `open_runtime` 返回。完整推导见
`protocol/channels.py` 的 docstring。

## 配置错误怎么出去

它**不能**打到 stdout（那是协议通道，一行非 JSON 就会毒了它）。所以 `ConfigError`
只打到 stderr，然后**以退出码 2 结束** —— 和 `main.py` 那条老路同一档
（"用户得先做点事"）。父进程会把 stderr 收起来当一条 notice 显示（决策：TUI 必须
看得见它，而不是"界面没起来就退出了"）。
"""

import sys

from agent_runtime.protocol.channels import ProtocolServer
from agent_runtime.protocol.transport_stdio import open_stdio
from agent_runtime.runtime.channels import resolve_memory_factory
from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
from agent_runtime.runtime.config import ConfigError


def main(session_id: str | None = None, *, autopilot: bool = False,
         debug: bool = False) -> int:
    """跑一个协议会话。返回进程退出码。"""
    transport = open_stdio()
    server = ProtocolServer(transport)
    booted = boot()

    session_id, session, resumed = resolve_session(booted.store, session_id)

    try:
        runtime = open_runtime(
            booted=booted,
            session_id=session_id,
            session=session,
            channels=server.channels(),
            autopilot=autopilot,
            debug=debug,
            resumed=resumed,
            should_stop=server.should_stop,
        )
    except ConfigError as exc:
        # **只走 stderr**：stdout 是协议通道，混一行人话进去前端就解析崩了。
        print(exc, file=sys.stderr)
        return 2

    with runtime:
        # 事件要同时进审计（`Runtime.logs`，由 Agent 的 on_event 负责）和协议。
        # 两条出口是**故意的**，而且不违反"同一份事实只写一遍"：审计写的是它自己的
        # 那一行，协议转发的是**同一个 dict**（`on_event` 里原样转发，一个字段都不加）。
        # 所以它不是两份事实，是同一份事实的两个消费者。
        runtime.agent.on_event = _fanout(runtime.logs, server.on_event)
        server.attach(runtime)
        return server.serve()


def _fanout(audit, protocol):
    """审计先写，协议后写。**顺序有意**：审计是必须成功的那个。

    审计写失败时 `Agent._emit` 会吞掉并大声说（见那里），所以这里的顺序保证的是
    "先落盘、再上前端"—— 前一端坏了不影响后端，反过来就不一定了。
    """
    def emit(record: dict) -> None:
        audit(record)
        protocol(record)
    return emit


if __name__ == "__main__":  # pragma: no cover - 由 main.py 分派
    raise SystemExit(main())
