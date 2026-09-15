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

from agent_runtime import i18n
from agent_runtime.protocol.channels import Bootstrap, ProtocolServer
from agent_runtime.protocol.transport_stdio import open_stdio
from agent_runtime.runtime.composition import (
    boot,
    open_runtime,
    resolve_session,
    session_summaries,
)
from agent_runtime.userconfig import UserConfigError

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


# --- 将来要报"启动到哪一步了"的话，接口在这里 ---------------------------------
#
# **今天不接，而且是有意的。** 界面那 1.9 秒的空窗里，真正"在装配"的阶段只占
# 73 毫秒 —— 实测（本机，走 `tests/fakes.model_registry` 那条离线路由）：
#
#     boot()                3.2 ms     （扫技能目录 + 建 store/logs）
#     resolve_session()     0.6 ms
#     open_runtime()       68.8 ms     （读配置 → 模型 client → 工具注册表 →
#                                        权限 → 记忆 → Agent）
#     ui_state(catalog)     0.2 ms
#     其余 ≈ 1.8 s                     （Python 冷启动 + import：`openai` 一项
#                                        861 ms，其中 `openai.types` 517 ms）
#
# 也就是说阶段报出来是**几毫秒一闪**：屏幕上什么都不会变，而每多一条协议消息就
# 多一份要维护的两端契约。所以启动期那句"正在启动 runtime…"由父进程自己说就够了
# （见 `view_state.ViewState.booting`）。
#
# ## 真正需要它的是 MCP 的惰性加载
#
# `composition.McpHost.load()` 要起一个子进程（远程是建连接、stdio 是 `npx` 冷启动），
# 那是这个代码库里**唯一已知会花好几秒**的操作（`mcp.host.loaded` 那条注释里写着）。
# 它今天由 `/mcp load` 触发，将来也可能在启动时预挂 —— 两种情况下"正在连 kb…"
# 都是有内容可报的，而它也是这套通道唯一值得的客户。
#
# ## 接线点与协议改动（真要做时按这个顺序）
#
#   1. 这里给 `boot()` / `open_runtime()` 各加一个 `on_progress(stage: str)` 回调。
#      两处都是纯同步调用，所以在**每个阶段之前**报（报"要做什么"，而不是"做完了"）
#      —— MCP 那种"报完还要等 3 秒"的场景才不会看起来像卡在上一句上；
#   2. `boot()` 现在只扫一次目录（`skills/loader.py` 的 `SkillCatalog.reload()`），
#      要细分就把回调透传进那里 —— 这也是唯一一处"扫目录"的代价所在。
#   3. 协议**复用 `ui` 那条消息**（`kind="boot"` + `stage`），不新增消息类型 ——
#      这样 `tests/test_protocol_schema.py` 里"真实消息逐字段比对"那三条不用动
#      （`ui` 不在那组参数里）。要同步改三处：`protocol/messages.py` 的 `UI_BOOT`
#      常量、`protocol/schema/outbound.schema.json` 里 `ui.fields.kind.enum`、
#      `doc/protocol.md` 的 3.x 那节；
#   4. **文案不进协议**：`stage` 只发机器可读的标记（`config` / `boot` / `tools` /
#      `mcp:<名字>`），前端自己映射 i18n —— "前端只讲协议"那条设计原则（决策 18）
#      的意思就是文本不该从 runtime 流过去；
#   5. 父进程那一侧：`_on_ui` 里多一个分支把它存进 `ViewState`，启动态那行改用它
#      （`_on_init` 一收就清 —— 之后来的 `boot` 阶段一律没有意义）。


def main(session_id: str | None = None, *, autopilot: bool = False,
         debug: bool = False, stream: bool = True,
         lang: str | None = None) -> int:
    """跑一个协议会话。返回进程退出码。

    `stream` 默认**开**（这个入口只服务界面，而界面要的就是逐字）。协议的老客户端
    收不到伤害：delta 是两条新消息，不认识的 `t` 按协议约定忽略就行，而
    `ui(run_finished).answer` 照旧发一份完整的。

    `lang` 是**界面语言**，由父进程（`--tui`）传下来；直接手跑这个入口时它为空，
    那就按配置文件那一格定（见 `i18n.activate`）。它必须在**任何一句文案产生之前**
    定下来 —— 启动通知就是第一句（`channels` 发 `init` 时现算的）。
    """
    # 认不出的语言要**当场报**，而不是回默认：它是"用户得先做点事"那一档，
    # 和配置写坏同一个处置（stderr + 退出码 2）。stdout 是协议通道，不能碰。
    try:
        i18n.activate(lang)
    except i18n.LangError as exc:
        print(exc, file=sys.stderr)
        return 2

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
    except UserConfigError as exc:
        # 捕**基类**：`ConfigError`（缺密钥）和 `CatalogError`（配置文件读不懂）都算。
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
