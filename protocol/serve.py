"""`--runtime-stdio` 那个进程的主循环。

**它只做三件事**：开传输、装配、把控制权交给 `ProtocolServer.serve()`。
任何别的逻辑（怎么解释消息、什么时候发什么）都属于 `channels` / `state` /
Agent —— 放这里会让"协议"和"启动"缠在一起，而那正是第零期把装配拆出去要避免的事。

## 顺序，以及为什么是这个顺序

    open_stdio()                          # 1. 三件"开工前必须做掉"的事（见 transport_stdio）
    ProtocolServer(transport)             # 2. 先有一个能收发的东西
    boot()                                # 3. store / logs / 技能扫描（不需要密钥）
    open_session(session_id)              # 4. 装配：settings → 模型 → 工具 → Agent
    server.attach(runtime)                # 5. 补上另一半 —— 这之前不可能收到请求
    server.serve()                        # 6. 转起来

第 2 步在第 4 步之前，是这一段的核心：`ProtocolServer` 既是传输的读端、又是通道的
提供者，所以它必须比 Runtime 先存在。**不变式**：`attach()` 之前不可能收到请求，
因为只有 runtime 会发请求，而 runtime 要等 `open_session` 返回。完整推导见
`protocol/channels.py` 的 docstring。

## 为什么装配是**一份交给 server 的函数**，而不是这里直接调一次

因为 `session_switch` 允许**运行中换会话**，而换会话就是"收掉旧 runtime、按另一个
会话把上面第 4 步重做一遍（连带第 5 步）"。那份"怎么装配、怎么接线"的做法只能有
一份，而它有两个调用点：这里的第一个会话、以及 `channels.py` 里那条消息 —— 后者在
进程里，拿不到这里的局部变量。所以 `open_session` 随 server 一起进去。

## 配置错误怎么出去

它**不能**打到 stdout（那是协议通道，一行非 JSON 就会毒了它）。所以 `ConfigError`
只打到 stderr，然后**以退出码 2 结束** —— 和 `main.py` 那条老路同一档
（"用户得先做点事"）。父进程会把 stderr 收起来当一条 notice 显示（决策：TUI 必须
看得见它，而不是"界面没起来就退出了"）。

**换会话时的配置错误走另一条路**：不退出，只发一条 `notice`（见
`ProtocolServer._session_switch`）。那时候用户已经在界面里了，把进程退掉等于让
"选错了一个会话"这件事毁掉整个界面。
"""

import sys
from collections.abc import Callable

from agent_runtime.protocol.channels import Bootstrap, ProtocolServer
from agent_runtime.protocol.transport_stdio import open_stdio
from agent_runtime.runtime.composition import (
    boot,
    open_runtime,
    resolve_session,
    session_summaries,
)
from agent_runtime.runtime.config import ConfigError

# 按一个（可能为空的）会话 id 装出一个 runtime。见上面那一节。
OpenSession = Callable[[str], object]


def make_session_opener(server: ProtocolServer, booted) -> OpenSession:
    """造出那份"按会话 id 装配"的做法。**这是唯一一份。**

    三样东西各自为什么在：

      * `booted` 提供 store / logs / 技能目录。**换会话时它必须复用** ——
        重扫技能目录换不来任何好处，而换一个 store 会让新会话写在别的目录里；
      * `server` 提供人机通道（`server.channels()`）与取消标志。它们只能从 server
        上拿 —— 那正是"先有 server 再有 runtime"那个顺序的用处；
      * **审计和协议的那条扇出**（`_fanout`）在这里接上，而不是在 `main()` 里接一次：
        换会话造出来的是**另一个 Agent**，它默认只有审计那一个出口。在这里接，
        每条会话（包括换过去的那些）都自动是对的；在 `main()` 里接，换一次会话就会
        让界面**再也收不到任何事件**，而症状只是"切过去之后界面不动了"。

    `want` 是空串时表示**新会话**（id 由 store 分配）。这是 `session_switch` 那条
    消息的 null 语义，和 `--session` 不传是同一件事 —— 两处都不该由调用方自己
    `new_session_id()`：分配 id 会碰磁盘（要确认没撞名），那是 store 的知识。
    """
    def open_session(want: str) -> object:
        session_id, session, resumed = resolve_session(booted.store, want or None)
        bootstrap = server.bootstrap
        runtime = open_runtime(
            booted=booted,
            session_id=session_id,
            session=session,
            channels=server.channels(),
            autopilot=bool(bootstrap and bootstrap.autopilot),
            debug=bool(bootstrap and bootstrap.debug),
            stream=bool(bootstrap and bootstrap.stream),
            resumed=resumed,
            should_stop=server.should_stop,
            # 流式增量：**它必须在这里接，不能像 on_event 那样事后挂** ——
            # `Agent.__init__` 就把它收下了（它是"Agent 往哪儿吐"的一部分），
            # 而事后挂上去的那个（`on_event`）能成立只是因为"事件由 Agent 主动发"。
            on_delta=server.on_delta,
        )
        # 事件要同时进审计（`Runtime.logs`，由 Agent 的 on_event 负责）和协议。
        # 两条出口是**故意的**，而且不违反"同一份事实只写一遍"：审计写的是它自己的
        # 那一行，协议转发的是**同一个 dict**（`on_event` 里原样转发，一个字段都不加）。
        # 所以它不是两份事实，是同一份事实的两个消费者。
        runtime.agent.on_event = _fanout(runtime.logs, server.on_event)
        server.attach(runtime)
        return runtime

    return open_session


def main(session_id: str | None = None, *, autopilot: bool = False,
         debug: bool = False, stream: bool = True) -> int:
    """跑一个协议会话。返回进程退出码。

    `stream` 默认**开**（这个入口只服务界面，而界面要的就是逐字）。协议的老客户端
    收不到伤害：delta 是两条新消息，不认识的 `t` 按协议约定忽略就行，而
    `ui(run_finished).answer` 照旧发一份完整的。
    """
    transport = open_stdio()
    server = ProtocolServer(transport)
    booted = boot()
    # bootstrap 要在**第一个 runtime 之前**挂上去：`open_session` 里的 autopilot /
    # debug / stream 是它读的，而换会话读的是同一份。
    #
    # `session_lister` 走同一条路（而不是让 `protocol/channels.py` 自己 import
    # `composition.session_summaries`）：协议层只要"一份清单"这个**结果**，而
    # "怎么从 store 读出来"是装配层的知识。这样一个 import 也不会把整个装配层
    # （模型 client、httpx、工具注册表）拖进协议层的加载路径。
    server.bootstrap = Bootstrap(
        booted=booted, autopilot=autopilot, debug=debug, stream=stream,
        session_lister=session_summaries,
    )

    # 收摊时要收掉的是**当前**那一个会话的 runtime，而换会话会把"当前"换掉 ——
    # 所以记在一个列表里，退出时收最后那一个（中间那些由 `_session_switch` 自己收）。
    opened: list[object] = []
    opener = make_session_opener(server, booted)

    def factory(bootstrap: Bootstrap, want: str) -> object:
        runtime = opener(want)
        opened.append(runtime)
        return runtime

    server.set_runtime_factory(factory)

    try:
        server.open_session(session_id)
    except ConfigError as exc:
        # **只走 stderr**：stdout 是协议通道，混一行人话进去前端就解析崩了。
        print(exc, file=sys.stderr)
        return 2

    try:
        return server.serve()
    finally:
        if opened:
            opened[-1].close()


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
