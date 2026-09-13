"""协议服务器：**这一层的中心，也是唯一把三样东西接起来的地方。**

它同时是：

  1. `Transport` 的读端循环（`serve()`）；
  2. `Runtime` 的持有者；
  3. **`Channels` 的提供者** —— 审批和提问那两条人机通道走的就是这块协议。

第 3 条解开了一个顺序上的环，值得写清楚（它决定了为什么是这个形状）：

    Agent 构造时就要 asker / questioner
    而协议版的它们要能收发消息 —— 那需要一条活着的通道
    而通道要等 Runtime 起来（`open_runtime` 会发事件）
    → 环

解法是让"这条通道"在 Runtime 之前就存在：`ProtocolServer` 先是一个**能收发**的
对象（`bind()` 给了它 memory 和 trust group 查询口），Runtime 拿它造 asker /
questioner，然后 `attach(runtime)` 把另一半补上。

## 不变式：`attach()` 之前不可能收到任何请求

这不是巧合 —— 只有 runtime 会发请求，而 runtime 要等 `open_runtime(...)` 返回、
再由 `attach()` 接上。`Pending` 在没接上时**抛异常而不是阻塞**：一个静静等着的
通道会把整个服务挂死，而那种 bug 没有任何症状。

## 阻塞是刻意的

asker / questioner 是**同步**端口（`Callable[[Tool, Mapping], bool]` /
`Callable[[AskUserArgs], Answer]`），而 `serve()` 也在同一个线程上。所以"等回应"
就是"让 `serve()` 的下一次循环去收那一行"。这不是限制 —— 它正是这个设计能成立的原因：
子进程里没有第二个线程，而 Agent 的全部时序假设（事件由主线程发、裁决和执行分开）
原样成立。
"""

import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

from agent_runtime.agents import RunCancelled
from agent_runtime.models.types import ModelError
from agent_runtime.protocol import codec, messages
from agent_runtime.protocol.transport_stdio import StdioTransport
from agent_runtime.runtime.channels import Channels, TrustGroupLookup
from agent_runtime.runtime.config import ConfigError
from agent_runtime.security.commands import format_rule
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.state import reasoning
from agent_runtime.state import status as status_summary
from agent_runtime.state.session import is_valid_session_id
from agent_runtime.tools.builtin.ask import ANSWERED, SKIPPED, UNAVAILABLE, Answer, AskUserArgs
from agent_runtime.tools.tool import Tool


# 造一个已装配的 runtime：给会话 id，还一个能跑的东西。
#
# 它的存在理由和 `runtime/channels.py` 里那份 `AskerFactory` 是同一个：**打破一个
# 顺序上的环**。换会话要"收旧 runtime → 造新 runtime"，而"怎么造"的知识在装配层；
# 可是造出来的 runtime 又要挂回这个 server（asker 从它身上取 memory）。所以 server
# 拿的是一份**工厂**，而不是一个现成的 runtime。
RuntimeFactory = Callable[[str], Any]

# 列会话清单的做法：给一个 store，还一份从新到旧的 summary list（见 `Bootstrap`）。
SessionLister = Callable[[Any], list[dict[str, Any]]]


class Bootstrap(NamedTuple):
    """开一个会话需要、但**整个进程只做一次**的那些东西。

    `boot()` 的产物（store / logs / 技能扫描）加上两件装配层的做法：

      * `session_lister` —— "把已保存的会话读成一份清单"。**做成一份可调用的东西，
        而不是让协议层 import `composition.session_summaries`**：那会把整个装配层
        （模型 client、httpx、工具注册表）拖进 `protocol/` 的加载路径，而协议层
        只需要"一份清单"这个结果。它和 `runtime_factory` 是同一条规矩的两次应用；
      * `autopilot` / `debug` / `stream` —— 进程级的开关，换会话要用同一份。
        （`stream` 是"这次运行开不开流式"，和 `--stream` / `--no-stream` 同一个值：
        换会话换的是会话，不是这次运行的形态。）

    **它和"会话是谁"是两件事**，所以要分开传：换会话的工厂拿到的必须是同一份
    bootstrap，否则第二个会话会是另一套技能目录、另一个审计目录。

    **字段顺序不要动**：`booted` 在前面，因为它是唯一必填的那个。
    """

    booted: Any                    # runtime.composition.Booted
    autopilot: bool = False
    debug: bool = False
    stream: bool = True
    session_lister: SessionLister | None = None


# 默认那份工厂的签名：`(bootstrap, session_id) -> runtime`。测试注入的就是它。
BootstrapFactory = Callable[[Bootstrap, str], Any]


class _Pending:
    """一条已经发出去、还在等回应的请求。

    普通类而不是 dataclass：`done` 必须是**每个实例自己的** `threading.Event`，
    而 dataclass 里放它需要 `field(default_factory=...)`，在 frozen + slots 下又多一层
    别扭。这里就三个属性，手写 `__init__` 比绕那些更清楚。
    """

    __slots__ = ("request_id", "groups", "answer", "done")

    def __init__(self, request_id: str, groups: Any = None):
        self.request_id = request_id
        self.groups = groups       # 审批里的 a 要用的 TrustGroup（可能为 None）
        self.answer: Any = None    # 回应的内容；done 一 set 就说明它有效了
        self.done = threading.Event()


class _PendingTable:
    """按 id 记着"发出去的请求"。

    **id 是这次交互的唯一凭据**（6.3）：`always_group` 回过来时，放行哪些工具由
    这里按 id 查出来 —— 前端不许自己带名单。让客户端指定放行范围就等于让客户端能改
    策略（写 `auto_approve_tools`），而诚实的前端和恶意的前端在那一步没有区别。
    """

    def __init__(self) -> None:
        self._items: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def open(self, groups: Any = None) -> _Pending:
        pending = _Pending(request_id=uuid4().hex[:8], groups=groups)
        with self._lock:
            self._items[pending.request_id] = pending
        return pending

    def resolve(self, request_id: str, answer: Any) -> bool:
        """填上回应并放行等待方。返回"这个 id 认识吗"。"""
        with self._lock:
            pending = self._items.pop(request_id, None)
        if pending is None:
            return False
        pending.answer = answer
        pending.done.set()
        return True

    def abandon_all(self) -> None:
        """通道断了：把所有还在等的都叫醒，让它们拿到"没有答案"。

        不这么做的话，`serve()` 退出前最后一个 asker 会永远挂在那个 `wait()` 上 ——
        子进程不会退，而父进程看到的是一个不回应也不退出的孩子。
        """
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for pending in items:
            pending.answer = None
            pending.done.set()


# `_Pending` 上面的辅助函数已经不需要了 —— 见那个类自己的 docstring。


class ProtocolAsker:
    """审批通道的协议实现。签名和 `cli_asker` 一模一样（`ApprovalAsker`）。

    它做三件事，顺序要紧：

      1. 发一条 `permission_request`（参数**全文**、外加两条"记住"的说明）；
      2. 阻塞等 `permission_response`；
      3. 按 decision 写 memory（`always` / `always_group`）并回答 True/False。

    第 3 步**留在这里而不是前端**：`remember` 里的"命令前缀"是
    `security/commands.py` 的知识，而放行名单是 runtime 按 id 查出来的。
    前端只回一个枚举。
    """

    def __init__(self, server: "ProtocolServer"):
        self._server = server

    @property
    def _memory(self) -> ApprovalMemory:
        """从**已经接上的** runtime 上取那份记忆。

        为什么不在这里存一份：memory 是 `open_runtime` 造出来的，而它在造的时候要用
        我们（channels）—— 所以那一份必须只有一处。存一份副本的下场是两处记忆各自
        记着"人按过 t"，而其中一个永远不会被问到。
        """
        runtime = self._server.runtime
        assert runtime is not None, "asker 只能在 attach() 之后被调用"
        return runtime.memory

    @property
    def _trust_group(self):
        runtime = self._server.runtime
        return None if runtime is None else runtime.mcp_trust_group

    def __call__(self, tool: Tool, arguments: Mapping[str, Any]) -> bool:
        # 按 t 该记住什么：命令类工具记前缀，其余工具记工具名。推不出来就是 None ——
        # None 表示"这次不提供 t"，而不是"记住整个工具"。这段逻辑和 cli_asker 同源
        # （都是 security/commands.py 的判断），只是出口不同。
        from agent_runtime.security.asker import (
            _preview,
            _remember_hint,
            _trust_all_hint,
            _PREVIEW_LIMIT_BY_RISK,
            command_parameter,
        )
        from agent_runtime.security.commands import command_of, suggest_prefix

        target = None
        if command_parameter(tool.name) is None:
            target = tool.name
        else:
            command = command_of(tool.name, arguments)
            target = suggest_prefix(command) if command is not None else None

        group = self._trust_group(tool.name) if self._trust_group is not None else None

        # 参数**按风险决定打多全**：中低风险给预览（write_file 的 content 动辄几千
        # 字符，全打出来会把 path 挤没），高风险**原样全文**（shell 命令的重点常在
        # 后半句，截断等于让用户在看不全的情况下签字）。这条和 cli_asker 一字不差。
        limit = _PREVIEW_LIMIT_BY_RISK.get(tool.risk)
        rendered = {
            name: _preview(value, limit) for name, value in arguments.items()
        }

        pending = self._server.pending.open(groups=group)
        self._server.send({
            "v": messages.VERSION,
            "t": messages.OUT_PERMISSION_REQUEST,
            "id": pending.request_id,
            "call_id": self._server.current_call_id,
            "tool": tool.name,
            "risk": tool.risk.value,
            "arguments": rendered,
            "remember": (
                {"prefix": list(target)} if isinstance(target, tuple)
                else ({"tool": target} if target is not None else None)
            ),
            "remember_hint": (
                _remember_hint(tool, target, self._memory.label)
                if target is not None else None
            ),
            "allow_trust_all": group is not None,
            "trust_all_hint": (
                _trust_all_hint(group, self._memory.label) if group is not None else None
            ),
        })

        decision = self._server.wait(pending)

        if decision == messages.ALWAYS and target is not None:
            if isinstance(target, tuple):
                self._memory.grant_prefix(target)
            else:
                self._memory.grant(target)
            return True
        if decision == messages.ALWAYS_GROUP and pending.groups is not None:
            # **放行哪些名字由这里决定**（pending.groups 是 runtime 自己存的那份
            # 快照），前端只是说了"我同意放行这一组"。
            for tool_name in pending.groups.tools:
                self._memory.grant(tool_name)
            return True
        return decision == messages.ALLOW


class ProtocolQuestioner:
    """提问通道的协议实现。签名和 `cli_questioner` 一样（`Questioner`）。

    和 asker 的分工必须一直分得清（见 tools/builtin/ask.py 的模块 docstring）：
    这里**不写 memory、不产生任何权限效果** —— 拿到"用户同意了"不会让下一次 shell
    调用免审。能靠提问换放行的话，模型自己问一句再自己念一句"用户同意了"就等于
    给自己发了一张放行条。
    """

    def __init__(self, server: "ProtocolServer"):
        self._server = server

    def __call__(self, question: AskUserArgs) -> Answer:
        pending = self._server.pending.open()
        self._server.send({
            "v": messages.VERSION,
            "t": messages.OUT_QUESTION_REQUEST,
            "id": pending.request_id,
            "question": question.question,
            "header": question.header,
            "options": list(question.options),
            "multi_select": question.multi_select,
        })

        answer = self._server.wait(pending)
        if not isinstance(answer, tuple):
            # 通道断了、或者回了一条看不懂的 —— 一律按"没有人可问"处理，
            # **绝不按空答案**：空串会被模型读成"用户没有意见"，而它根本不知道
            # 有没有人在看（见 tools/builtin/ask.py 的三常量）。
            return Answer("", UNAVAILABLE, 0)
        status, text, waited_ms = answer
        return Answer(text if status == ANSWERED else "", status, waited_ms)


class ProtocolServer:
    """协议服务器。见模块 docstring 的那三件事。

    `bootstrap` / `runtime_factory` 这两个参数是**换会话**（`session_switch`）加进来的，
    而它们的位置很讲究：`bootstrap` 是"整个进程只做一次的那几件事"，`runtime_factory`
    是"按一个会话 id 造出 runtime 的做法"。默认那份做法（`open_session`）住在
    `serve.py` 那一侧 —— 协议层不许 import 装配层，这条依赖方向有测试盯着
    （`tests/test_imports.py`）。
    """

    def __init__(self, transport: StdioTransport, *,
                 bootstrap: Bootstrap | None = None,
                 runtime_factory: BootstrapFactory | None = None):
        self.transport = transport
        self.pending = _PendingTable()
        self.runtime: Any = None
        self.bootstrap = bootstrap

        # 怎么按一个会话 id 造出一个新 runtime。**换会话要重来一遍装配**，而"装配"
        # 这件事的知识住在 `runtime/composition.py`。所以它是一份可注入的工厂：
        # 默认那份拒绝工作（见 `_no_runtime_factory` 的 docstring），真跑时由
        # `serve.py` 交进来，测试交一份离线的。
        self._factory: BootstrapFactory = runtime_factory or _no_runtime_factory

        # 审批里那个 `a` 的查询口，由 `bind()` 从装配层接过来（它要等 MCP 连上）。
        self._trust_group: TrustGroupLookup | None = None
        self._memory: ApprovalMemory | None = None

        # 当前的 call_id：审批请求要带上"这是哪一次工具调用"，而 asker 的签名里
        # 没有它（那是 Agent 内部的标识）。所以由 `on_event` 在收到 tool_call 时
        # 记下来，asker 读它 —— 这是唯一一处"顺手记一下"的状态，而它只影响
        # 前端能不能把审批面板挂到对应的那张卡片上。
        self.current_call_id: str = ""

        # 待发送的答案（run_finished 那条 `t:"ui"`）。见 `_run_turn` 里为什么它
        # 不能在 `on_event` 里发。
        self._answer: str = ""
        self._last_run_id: str = ""
        # 当前这一轮跑到第几步。`t:"delta"` 要带上它 —— 一个回合里可能有好几步
        # （模型先说要调工具、拿到结果再答），界面按步分块，而"这一步的正文被
        # 重试作废了"也只能按步说（见 `t:"delta_reset"`）。
        self._last_step: int = 0

        # 当前这一轮的工作线程。**同时只有一个**（新的一条 user_message 会先 join），
        # 所以发给前端的事件流仍然严格有序。
        self._turn_thread: threading.Thread | None = None

        # **两根锁，管的不是一件事。**
        #
        #   * `_send_lock` —— 写 stdout。回合跑在 `turn` 线程，而读循环在主线程
        #     （它会发 `ui(state)` / `notice` / `permission_request`）。一次
        #     `send` 是"一行 + flush"，两个线程同时写就会交错成半行 JSON，而
        #     前端那一侧的症状是"协议丢了一行读不懂的输出"。**流式之前侥幸没事**
        #     （发的只有回合线程）、流式之后是必然 —— 所以从今天起它必须有锁；
        #   * `_state_lock` —— 下面那三个"顺手记一下"的字段（run_id / step /
        #     call_id）。读也在主线程（`_state_message()` 会翻 `runtime.agent`），
        #     写主要在回合线程。**不要用一把锁**：持着发送锁去读状态会让"回合线程
        #     正在往管道里写"变成"读循环发不出审批面板"，而那个症状是死锁。
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()

        # 取消：`shutdown` 到这里置位，Agent 在下一个安全点退出（见 agents/agent.py）。
        self._stop = threading.Event()

    # -- 接线 ------------------------------------------------------------------

    def channels(self) -> Channels:
        """交出那一对人机通道。

        **不需要先 `bind()`。** 它只是把两个类包起来 —— memory 和 trust group
        在**被调用时**才从 `attach()` 到的 runtime 上取（见 `ProtocolAsker._memory`），
        而那时候装配早就完成了。

        这个顺序是刻意的：`open_runtime(...)` 要 `channels` 才能造 Agent，所以
        channels 必须比 runtime 先存在；而它依赖的两样东西是 runtime 造出来的。
        工厂形状（而不是现成对象）就是为这个环准备的。
        """
        return Channels(
            questioner=ProtocolQuestioner(self),
            asker_factory=lambda memory, trust: ProtocolAsker(self),
        )

    def attach(self, runtime: Any) -> None:
        """补上另一半。**在这之前不可能收到任何请求**（见模块 docstring）。"""
        self.runtime = runtime

    # -- 收发 ------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        """写一条出站消息。**加锁**（理由见 `__init__` 里那段）。

        粒度是"一整条消息"而不是"一行字节"：`Transport.send` 落成
        `write(line)` + `flush()`，两步之间被别人插进来，管道里就是两行交错。

        顺带把"给前端那条流的异常不许打死这一轮"也定在这里：`Transport.send`
        写的是父进程的管道，父进程没了就是 `BrokenPipeError` —— 那时候该发生的是
        "读到 EOF 之后收摊"，不是让一次 `todo_write` 的结果变成一条工具执行失败。
        和 `_emit` 吞审计异常是同一条理由（观测量坏了不影响被观测的过程），
        只不过这一条必须**大声说**（stderr 还是活的）。
        """
        with self._send_lock:
            try:
                self.transport.send(message)
            except (BrokenPipeError, OSError, ValueError) as exc:
                print(f"[warn] 往前端写一行失败（已忽略）：{type(exc).__name__}: {exc}",
                      file=sys.stderr)

    def wait(self, pending: _Pending) -> Any:
        """阻塞等下一条相关的回应。**由 `serve()` 的循环来唤醒。**

        通道断了时返回 None（而不是永远挂着）：那时候调用方各自按 fail-closed 处理
        （审批拒绝、提问"没有人可问"）—— 见 `_PendingTable.abandon_all`。
        """
        pending.done.wait()
        if pending.answer is None and not self._alive():
            return None
        return pending.answer

    def _alive(self) -> bool:
        return getattr(self, "_serving", False)

    # -- 主循环 ----------------------------------------------------------------

    def serve(self) -> int:
        """读到 EOF / `shutdown` 为止。返回进程退出码。

        ## 一个回合跑在工作线程里 —— 这不是性能优化，是**正确性**

        第一版把 `agent.run(...)` 直接调在循环里，结果是一个必然的死锁：

            循环读到 user_message → 直接在循环里跑这一轮
            → Agent 要审批 → asker 发出一条 permission_request、然后**原地阻塞**
            → 而那条回应只有这个循环能读到 —— 它正阻塞着呢
            → 双方互等，直到超时

        这不是边角情形：**每一次需要审批的工具调用都会撞上它**。而且它不会报错，
        只会挂住 —— 最难查的那种。

        所以：回合跑在工作线程，读循环**永远在转**。于是：

          * `permission_response` / `question_response` 随时都能被读到，
            唤醒那个阻塞的 asker；
          * `shutdown` 随时能被读到 —— 它只让循环退出，**当前这一轮会跑完**
            （`_join_turn`），因为"收摊"和"停止这一轮"是两件事（见 `_dispatch`
            里 `IN_SHUTDOWN` 那段：混在一起的后果是界面永远拿不到答案）；
          * 新的一条 `user_message` 会先等上一轮结束（不会两条回合叠着跑）。

        代价是**事件由工作线程发**。这里没有违反"事件由主线程发"那条规矩 ——
        那条规矩针对的是 `Agent._run_parallel` 的工具线程池（多个线程同时发会交错），
        而这里**同时只有一个回合线程**，所以发给前端的那条流仍然是严格有序的。
        """
        self.assert_attached()
        self._serving = True
        self._emit_opening()

        try:
            for message in self.transport.recv():
                if not self._dispatch(message):
                    break
        finally:
            self._serving = False
            # 等当前这一轮收尾：它可能正阻塞在等审批上，而 `abandon_all` 会把
            # 它叫醒（拿到"没有答案"→ fail-closed）。
            self.pending.abandon_all()
            self._join_turn()
            self.transport.close()
        return 0

    def _emit_opening(self) -> None:
        """把"一个会话的开场三连"发出去：`init` → `session_load` → `ui state`。

        **它是一段独立的方法，因为它有两个调用点**：`serve()` 的开头，以及换会话
        成功之后（`_session_switch`）。两处发的必须是同一组消息、同一个顺序 ——
        抄一遍的话，换会话之后的前端会缺一条（而缺哪一条取决于抄漏了哪个），
        症状是"切过去之后左栏/历史有一半是旧的"。
        """
        runtime = self.runtime
        self.send(self._init_message())
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_SESSION_LOAD,
            "messages": runtime.session.messages,
        })
        # 面板开场数据。**带可用技能清单**（这一条要扫目录，整个会话只发这一次）；
        # 之后的每一次快照都只有"已经在那儿"的那几个数。
        self.send(self._state_message(with_catalog=True))

    def assert_attached(self) -> None:
        if self.runtime is None:
            raise RuntimeError("ProtocolServer.serve() 之前必须先 attach(runtime)")

    def _join_turn(self) -> None:
        thread = self._turn_thread
        if thread is not None and thread.is_alive():
            thread.join()
        self._turn_thread = None

    def _dispatch(self, message: dict[str, Any]) -> bool:
        """处理一条入站消息。返回 False 表示该收摊了。"""
        try:
            codec.check_version(message, direction="前端发来")
        except codec.ProtocolError as exc:
            # 版本对不上是**唯一**该硬失败的地方：继续读下去只会拿一堆看不懂的消息
            # 去驱动 Agent。说清原因再退，比替对方猜好。
            print(f"[协议] {exc}", file=sys.stderr)
            return False

        kind = message.get("t")

        if kind == messages.IN_SHUTDOWN:
            # **不置 stop 标志。** 这是实测踩出来的：`shutdown` 的语义是"收摊"，
            # 而"停止这一轮"是另一件事。两者混在一起时，客户端发完 user_message
            # 紧跟着发 shutdown（"我该说的都说了"），当前这一轮会在**第一个安全点**
            # 就被取消 —— 于是界面永远拿不到答案，而任何地方都不报错。
            #
            # 所以：shutdown 只是让循环退出，而退出前 `_join_turn()` 会把当前这一轮
            # **跑完**。想中断当前这一轮，用取消（`request_stop`），那是另一条路。
            return False

        if kind == messages.IN_INTERRUPT:
            # **中断这一轮，而不是收摊。** 它和 `shutdown` 必须分开：把这两件事
            # 混在一起过（见下面 `IN_SHUTDOWN` 那段），后果是界面永远拿不到答案。
            #
            # 它只是**置位**：Agent 在下一个安全点退出（`agents/agent.py` 里那个
            # 两步之间的检查点）。模型往返和工具执行都打断不了 —— 打断它们会留下
            # 一条带 tool_calls 却没有对应结果的 assistant 消息，那种会话此后每一轮
            # 都发不出去（API 直接 400）。所以 Esc 的语义是"停在这一步之后"。
            self.request_stop()
            return True

        if kind == messages.IN_PERMISSION_RESPONSE:
            # 不认识这个 id 就忽略（重复回应、或者上一轮遗留的）—— 不崩。
            self.pending.resolve(message.get("id", ""), message.get("decision"))
            return True

        if kind == messages.IN_QUESTION_RESPONSE:
            status = message.get("status", SKIPPED)
            # 时间由 runtime 测（它是"人想了多久"），而这里拿不到那个数 ——
            # 前端回的是"什么时候答的"，不是"等了多久"。所以补 0，
            # 让 `human_wait_ms` 不谎报一个它没有的值（见 ProtocolQuestioner）。
            self.pending.resolve(
                message.get("id", ""),
                (status, message.get("text", ""), 0),
            )
            return True

        if kind == messages.IN_SESSION_SWITCH:
            # **就地换会话**（TUI 的 `/new` / `/resume`）。它同步做完：收旧 runtime、
            # 造新的、重发开场三连。做完之前这个循环不会去读别的消息 —— 这是有意的，
            # 换会话的中途读到一条 user_message 会把它投给一个正在被收掉的 runtime。
            self._session_switch(message.get("session_id"))
            return True

        if kind == messages.IN_SESSION_LIST:
            self._send_session_list()
            return True

        if kind == messages.IN_SET_AUTOPILOT:
            # **只认真正的 `true`。** 这一档的后果是"需要审批的工具直接执行"，所以
            # `"false"` / `1` 这类东西**不许**被猜成 true —— 猜错的两个方向代价不对称：
            # 该开没开只是维持现状，不该开却开了是"没人在上面点过头就执行了"。
            # schema 里 `on` 是 boolean，这条不信任是留给"手写的客户端"的。
            self._set_autopilot(message.get("on") is True)
            return True

        if kind == messages.IN_SET_MODEL:
            # 模型名**不在这里校验**（认不认识、base_url 对不对都是 runtime 的知识），
            # 但"它得是个字符串"是信封这一层的事 —— 一个 JSON 对象传进去，那句
            # "目录里没有这个模型"就会把整个 dict 打给用户看。
            name = message.get("model")
            if not isinstance(name, str):
                self._notice("warn", "model", "[模型] 换模型要一个字符串模型名。/model 看清单。")
                return True
            self._set_model(name)
            return True

        if kind == messages.IN_SET_THINKING:
            # 和 `set_autopilot` 同一条：**只认真正的 `true`**。这一档改的是"模型要不要
            # 先想一段"，猜错的代价不对称 —— 该开没开只是维持现状，不该开却开了是
            # 答案质量在没人察觉的情况下变差。
            self._set_thinking(message.get("on") is True)
            return True

        if kind == messages.IN_SET_EFFORT:
            level = message.get("effort")
            if not isinstance(level, str):
                self._notice("warn", "effort",
                             "[思考] 强度要一个字符串（low / high / max）。")
                return True
            self._set_effort(level)
            return True

        if kind == messages.IN_STATUS:
            self._send_status()
            return True

        if kind == messages.IN_TOOLS:
            self._send_tools()
            return True

        if kind == messages.IN_MCP:
            self._handle_mcp(message)
            return True

        if kind == messages.IN_REFRESH_STATE:
            # **它不改任何东西，只是把当前那份快照再算一遍发出去。** 处理它的地方在
            # 读循环那个线程 —— 而 `_state_message()` 本来就会被两个线程调
            # （`on_event` 走回合线程），所以这里没有引入新的并发形态。
            #
            # 它**不带 `skill_catalog`**：那是开场那一条才给的（扫目录有代价），
            # 和所有后续快照一样保住前端已经拿到的那一份（见 view_state.apply_state）。
            self.send(self._state_message())
            return True

        if kind == messages.IN_USER_MESSAGE:
            # 上一轮还没走完就先等它 —— 两条回合叠着跑会让事件顺序错乱，
            # 而"顺序"是这条协议唯一的同步手段。
            self._join_turn()
            self._turn_thread = threading.Thread(
                target=self._run_turn, args=(message.get("text", ""),),
                name="turn", daemon=True,
            )
            self._turn_thread.start()
            return True

        # 不认识的 `t`：**忽略并继续**。协议的两端会分别升级，而一个老客户端
        # 死在不认识的消息上是最没必要的兼容性损失。
        return True

    # -- 会话切换 --------------------------------------------------------------

    def open_session(self, session_id: str | None) -> Any:
        """按一个会话 id 造出 runtime 的**唯一入口**。见 `RuntimeFactory` 那段。

        两个调用点：`serve.py` 开第一个会话（它自己起了头，所以把 bootstrap 交进来
        一次），以及 `_session_switch` 换会话（复用同一份 bootstrap）。
        """
        if self.bootstrap is None:
            raise RuntimeError(
                "换会话需要一个 bootstrap（boot() 的产物 + autopilot/debug）——"
                "ProtocolServer 是从 serve.py 起来的吗？"
            )
        return self._factory(self.bootstrap, session_id or "")

    def set_runtime_factory(self, factory: BootstrapFactory) -> None:
        """接上"怎么按会话 id 装配"那一份做法。

        做成 setter 而不是构造参数，是因为**装配那一侧需要先有一个 server**：
        `serve.py` 得拿到 `server.channels()` 才能装出第一个 runtime，而 channels
        只有 server 有。构造参数版本会逼出一个"先造一个假的再换掉"的循环。
        """
        self._factory = factory

    def _send_session_list(self) -> None:
        """回一份会话清单（`sessions`）。**它不改变任何状态**，所以不算会话切换。"""
        lister = self.bootstrap.session_lister
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_SESSIONS,
            "items": lister(self.bootstrap.booted.store) if lister else [],
        })

    def _set_autopilot(self, on: bool) -> None:
        """运行中开关 autopilot（`/autopilot`）。**两处都要改，少一处就是半开半关。**

          * `self.bootstrap.autopilot` —— 换会话时新 runtime 照 bootstrap 装
            （`serve.make_session_opener`）。不改它，"开着 autopilot 再 `/new`"
            会得到一个又开始逐条问的会话，而界面上的指示灯还亮着；
          * `self.runtime.agent.autopilot` —— **当前这个会话真正生效的那份**：
            gate 每次都读它（`agents/agent.py`），改它下一步就生效。

        `Runtime.autopilot` 那个字段**不动**：`Runtime` 是 frozen 的，而类 docstring
        明令"运行中换策略该再 open_runtime 一个，不许就地改"。这里能就地改的是
        Agent 上那个开关，理由是**审计分得开**：每一条放行都记着当时那一次决定
        （`outcome=autopilot` 还是 `approved`），所以同一个回合里前后两种策略在
        jsonl 里一眼能分。而"就地换掉权限策略"（比如改 auto_approve）不留这种痕迹
        —— 那才是那条 docstring 真正防的东西。顺带：`Runtime.autopilot` 只用来渲染
        启动时那条警告，那本来就是"启动时是什么样"的事实，留着旧值是对的。

        **改完立刻回一条 state 快照**：界面按它显示、不许自己乐观更新，于是
        "现在开着没有"只有一个来源（`runtime.ui_state()` 读的是 Agent 那份）。
        """
        if self.bootstrap is not None:
            self.bootstrap = self.bootstrap._replace(autopilot=on)
        runtime = self.runtime
        if runtime is not None:
            runtime.agent.autopilot = on
        self.send(self._state_message())

    def _session_switch(self, session_id: Any) -> None:
        """收掉当前 runtime、按新会话装配、重发开场三连。**失败时保留旧会话。**

        ## 为什么这件事应该由 runtime 做，而不是前端重启进程

        在它之前，这条路的做法是"前端杀掉子进程、带另一个 `--session` 重启"。那让
        `/new` 和 `/resume` 变成"知道一个命令行开关"的人才用得动的东西，而界面里
        最需要它们的时刻（刚聊完一个话题、想换一个）恰恰是最不该退出重来的时候。

        代价说白：换会话要**完整重来一遍装配**（模型 client、工具注册表、Agent、
        MCP 子进程）。这不是优化问题，是正确性问题 —— `TodoBoard` / `SkillBoard` 绑在
        `session.metadata` 上，复用旧注册表就会让新会话看到旧会话的任务列表。

        ## 顺序，以及每一步为什么在那儿

          1. `_join_turn()` —— 先在**旧** runtime 上把当前这一轮跑完。中断它会在会话里
             留下一条带 `tool_calls` 却没有对应结果的 assistant 消息，那种会话此后每轮
             都发不出去（API 直接 400）。所以换会话的语义是"等这一轮回合结束"，和
             `shutdown` 是同一条规矩；
          2. `_factory(...)` —— 造新的。**它抛异常时旧 runtime 还活着**，所以失败路径
             只需要发一条 notice，界面那边什么都不用收拾（它按 `init` 才清屏）；
          3. `close()` 旧的那个 —— 放在新 runtime **造出来之后**：装配失败是真实会
             发生的（缺密钥、permissions.json 写坏、MCP 起不来），而那时候用户最不
             需要的就是"会话没了";
          4. `attach()` —— 在新 runtime 发任何事件之前接上（不变式，见模块 docstring）。
             asker 在**被调用时**才从 runtime 上取 memory，所以这一步晚了就会让第一次
             审批拿到上一个会话的记忆。
        """
        if session_id is not None and not isinstance(session_id, str):
            self._notice("warn", "session", "[会话] 换会话的 id 必须是一个字符串")
            return

        # 合法性在这里查一次（和 `main.py` 那条 `--session` 同一条规矩）。不查的话
        # `store.exists()` 会从 `_path` 里抛 ValueError —— 那是一条会打断读循环的异常，
        # 而"会话 id 打错了"是最常见的手滑，不该有这种后果。
        if session_id and not is_valid_session_id(session_id):
            self._notice(
                "warn", "session",
                f"[会话] 非法 id：{session_id!r} —— 只能用字母、数字、下划线、连字符"
                f"（1~64 个字符）。/resume 不带参数可以从列表里挑。",
            )
            return

        previous = self.runtime
        self._join_turn()
        try:
            runtime = self.open_session(session_id)
        except ConfigError as exc:
            self._notice("warn", "session",
                         f"[会话] 换不过去（当前会话没有变）：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 一个坏会话不该让整个进程退出
            self._notice(
                "warn", "session",
                f"[会话] 换不过去（当前会话没有变）：{type(exc).__name__}: {exc}",
            )
            return

        if previous is not None and previous is not runtime:
            try:
                previous.close()
            except Exception as exc:  # noqa: BLE001 - 收旧摊失败不该盖住新会话
                self._warn(f"收掉上一个会话的 runtime 时出错：{type(exc).__name__}: {exc}")

        self.runtime = runtime
        # 换会话时把这几个"上一个会话的残留"清掉：
        #   * `_stop`：不 clear 的话，在上一轮按过 Esc 之后，新会话的**每一轮**都会在
        #     第一个安全点被砍掉（症状是"发消息没反应"，没有任何地方报错）——
        #     和 `_run_turn` 里那句 clear 是同一个坑，只是这条路上更容易踩到；
        #   * `_answer` / `current_call_id`：它们属于上一个会话的那一轮。
        self._stop.clear()
        self._answer = ""
        self.current_call_id = ""
        self._last_run_id = ""
        self._last_step = 0

        self._emit_opening()

    def _run_turn(self, text: str) -> None:
        """跑一个回合（**在工作线程里**），并把该发的都发出去。"""
        runtime = self.runtime
        # **先把上一轮的取消标志清掉。** 不清的话，被 Esc 中断过一次之后，
        # 这个会话此后每一轮都会在第一个安全点被砍掉 —— 而症状是"发消息没反应"，
        # 没有任何地方报错。标志是每轮一份的，不是每次会话一份。
        self._stop.clear()
        try:
            answer = runtime.agent.run(runtime.session, text, max_steps=runtime.max_steps)
        except RunCancelled as exc:
            # 取消不是失败：会话是完好的，审计里已经记了 stop_reason=cancelled，
            # 那条 `run_finished` 也转发过了。这里只说一句人话。
            self._answer = ""
            self._notice("warn", "cancelled", str(exc))
            return
        except ModelError as exc:
            # 模型失败：`run_finished` 已经在 Agent 里发过了（stop_reason 会说明是
            # 哪一种），这里只说一句人话让界面能显示。
            self._answer = ""
            self._notice("warn", "model", f"[本轮失败] {exc}")
            return
        finally:
            # 面板数据在回合收尾时补一份：`todo_write` / `load_skill` 的结果会让
            # 左栏变样，而那条路（`on_event`）按事件推 —— 这里补的是"无论如何
            # 都对得上当前会话"的那一份。**放在 finally**：取消和失败两条路上
            # 左栏也该是刚才那一步之后的真相。
            self.send(self._state_message())

        self._answer = answer
        # **答案在这里发，不在 `on_event` 里** —— 这是实测踩出来的：`on_event` 看到
        # `run_finished` 时 `agent.run()` 还没返回，所以 `_answer` 还是空的，
        # 发出去的就是一个空串。位置换到这里之后，"先有完成事件、后有正文"这个顺序
        # 依然成立（同一个 `run_id`），而正文一定是完整的。
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_UI,
            "kind": messages.UI_RUN_FINISHED,
            "run_id": self._last_run_id,
            "answer": answer,
        })

    def _state_message(self, *, with_catalog: bool = False) -> dict[str, Any]:
        """面板数据快照（`t:"ui", kind:"state"`）。

        **它不进审计**：任务列表的变化在审计里已经有 `tool_call` 那条参数，技能加载
        也一样。再往 jsonl 里写一份就是同一份事实的第二个来源。

        `with_catalog` 只在开场那一条里为真：可用技能清单要扫一遍目录，而它几乎不变
        —— 每次工具返回都重扫一遍是白付的代价（`SkillBoard.catalog` 每次读都会重扫）。
        """
        runtime = self.runtime
        return {
            "v": messages.VERSION,
            "t": messages.OUT_UI,
            "kind": messages.UI_STATE,
            **runtime.ui_state(with_catalog=with_catalog),
            # **当前模型和它的窗口一起进快照。** 光发一个模型名不够：状态栏那个
            # 百分比的分母是窗口，而它随模型变 —— 换完模型只更新名字的话，界面会
            # 拿新模型的用量去比旧窗口，而那看起来完全正常，只是数错了。
            "model": runtime.current_model,
            "model_window": runtime.context_tokens,
            # 哪条路由。**和模型名分开是有意的**：两条路由可以有同名模型，而"请求
            # 发到哪儿"在账单上是另一件事。
            "model_provider": runtime.current_provider,
            # 思考模式那两个旋钮。它们和模型一样是会话级设置，改完立刻回一份快照 ——
            # 界面按它显示，不许乐观更新（理由见 `_set_thinking`）。
            "thinking": bool(runtime.agent.thinking),
            "effort": runtime.agent.effort,
            "effort_levels": list(reasoning.EFFORT_LEVELS),
        }


    def _set_model(self, name: str) -> None:
        """换这个会话用的模型（`/model`）。**成败都回话，回话里带证据。**

        ## 为什么请求回合不因它而中断

        一轮正跑着的时候按 `/model` 是**允许**的，而且本轮不受影响：那个回合的请求
        已经发出去了，模型的回答还在路上。真正被改变的是"下一个请求用谁"。

        这是刻意的，不是偷懒 —— 同一个回合里前后两步由两个模型生成的话，事后**完全
        看不出来**：审计里两条 model_call 长得一样（同一个 run_id、同一个 step 区间），
        而会话历史里那段话到底是谁写的就没有答案了。所以那条"模型换了"的说明留到
        **下一轮开头**（判据是 `selected != last_used`，见 `state/model.py`）。

        代价说白：界面上那条"换成 X 了"的回声在本轮就已经出现，而实际生效在下一轮。
        所以提示语里写的是"**下一次请求生效**"，而不是"已生效"。

        ## 为什么回一条 state 快照

        和 `_set_autopilot` 同一条规矩：界面按 runtime 说的话显示，不许自己在发请求的
        时候就先改 —— 那会让"状态栏写着 pro、请求还发给 flash"变成可能。这里的证据
        尤其重要：**换模型只改一个字符串**，没有任何别的地方会报出"其实没换成"。

        `model_since` 那一格也在这条快照里（`SessionModel.as_state`）：`/status` 要
        回答"这个会话什么时候换的"，而那个时间点只有选择本身知道。
        """
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "model", "[模型] 还没有会话，换不了模型。")
            return
        ok, message = runtime.select_model(name)
        # 消息**原样**发出去：那句话里含"上一个是谁""下一次请求生效"这些界面拼不出来的
        # 事实（拼的话就是第二份知识，而它漂掉的症状是"提示说换了、其实没换"）。
        self._notice("info" if ok else "warn", "model",
                     ("[模型] " if ok else "[模型] 没换：") + message)
        self.send(self._state_message())

    def _set_thinking(self, on: bool) -> None:
        """开关思考模式（`/thinking`）。**成败都回话，而且回一条 state 快照。**

        和 `_set_model` 同一条规矩：界面按 runtime 说的显示，不许自己在发请求的时候就
        先改 —— 这一格决定下一次请求花多少钱、想多久，而"灯亮着、其实没开"和
        autopilot 那一格是同一类错误。

        生效的时序也一样：**下一次请求**。正在跑的那一轮已经把参数发出去了。
        """
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "thinking", "[思考] 还没有会话。")
            return
        ok, message = runtime.select_thinking(on)
        self._notice("info" if ok else "warn", "thinking",
                     ("[思考] " if ok else "[思考] 没改：") + message)
        self.send(self._state_message())

    def _set_effort(self, effort: str) -> None:
        """改思考强度（`/effort`）。同上。"""
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "effort", "[思考] 还没有会话。")
            return
        ok, message = runtime.select_effort(effort)
        self._notice("info" if ok else "warn", "effort",
                     ("[思考] " if ok else "[思考] 没改：") + message)
        self.send(self._state_message())

    def _send_status(self) -> None:
        """回一份 `/status`（`ui` / `kind=status`）。**读一次审计日志。**

        日志读失败（文件被删了、权限没了）**不当成失败**：那几笔账是附加信息，而
        `/status` 的主要用途是"现在是什么状态"。所以那种情况下照发一份 counts/usage
        为空的快照 —— `state/status.summarize` 对空列表返回的就是零，而界面显示 0
        比显示一句"读不了日志"更接近事实（会话确实还没花过钱，或者我们数不出来，
        而两种情况的处置是一样的：继续用）。

        这里**不 join 正在跑的那一轮**：`/status` 是只读的，而"跑着的时候看状态"
        恰恰是它最有用的时候。
        """
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "status", "[状态] 还没有会话。")
            return
        events = list(runtime.logs.read(runtime.session_id))
        summary = status_summary.summarize(events)
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_UI,
            "kind": messages.UI_STATUS,
            "status": runtime.status(
                counts=summary["counters"], usage=summary["usage"],
            ),
            # "上一次请求实际发出去多少" —— 状态栏那个占比的分子。它和上面那份
            # usage 不是一回事（那个是**累计**，这个是**最近一次**），所以单独给。
            "last_prompt_tokens": summary["last_prompt_tokens"],
            "context_tokens": runtime.context_tokens,
        })

    def _send_tools(self) -> None:
        """回一份工具清单（`ui` / `kind=tools`）。"""
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "tools", "[工具] 还没有会话。")
            return
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_UI,
            "kind": messages.UI_TOOLS,
            "tools": runtime.tool_rows(),
            # 命令规则那几条单独给：`/tools` 末尾那句"按前缀放行了什么"要用它，
            # 而它是**人按 t 记住的东西**，和工具清单不是一个来源。
            "granted_prefixes": [
                format_rule(rule) for rule in sorted(runtime.memory.prefixes())
            ],
        })

    # -- MCP 的挂载（`/mcp`）---------------------------------------------------

    def _handle_mcp(self, message: dict[str, Any]) -> None:
        """看/改 MCP server 的挂载。**先等这一轮跑完再改。**

        ## 为什么先 `_join_turn()`

        和换会话（`_session_switch`）同一条语义，理由也一样具体：工具定义在
        `Agent.run` 开头取一次快照，而卸载会把工具从注册表里摘掉 —— 如果那一轮
        正在执行某个工具，半路摘掉它的 server 会让这次调用以 `McpServerDown` 收场。
        等一轮的代价是几十秒（用户看得见：面板上那行"等当前这一轮跑完…"），换来的是
        "不会把一个人正在用的工具抽走"。

        `list` 那条**不需要等**（它什么都不改）—— 但判据统一在这里做更省事：一次
        `join` 在没回合跑时是零成本的（`thread is None` 直接返回）。

        ## 认不出来的动作不当成 `list`

        一次打错字的 `load` 看起来像成功是最坏的失败形态（用户以为挂上了）。所以
        认不出就回一条 notice，什么都不做。
        """
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "mcp", "[MCP] 还没有会话。")
            return
        host = getattr(runtime, "mcp", None)
        if host is None:
            self._notice("warn", "mcp",
                         "[MCP] 这个 runtime 没有 MCP 宿主，改不了挂载。")
            return

        action = message.get("action")
        if action not in messages.MCP_ACTIONS:
            self._notice(
                "warn", "mcp",
                f"[MCP] 认不出这个动作：{action!r}（只有 "
                f"{' / '.join(messages.MCP_ACTIONS)}）",
            )
            return

        raw = message.get("servers")
        servers = (
            [name for name in raw if isinstance(name, str)] if isinstance(raw, list)
            else []
        )

        notes: list[str] = []
        if action == messages.MCP_LIST:
            notes.append("[MCP] 当前挂载情况（配置里改了要重启才生效）")
        else:
            self._join_turn()
            if not servers:
                notes.append(f"[MCP] {action} 要给出 server 名字，一次一个")
            for name in servers:
                if action == messages.MCP_LOAD:
                    notes.append(host.load(name))
                else:
                    notes.append(host.unload(name))

        self._send_mcp(notes)

    def _send_mcp(self, notes: list[str] | None = None) -> None:
        """回一份 MCP 清单（`ui` / `kind=mcp`）。

        **全量**（不是增量）：面板每次按它重画，所以"这一行现在是什么样"永远只有
        一个来源。`mcp_servers` 直接来自宿主（每一格的状态、工具数、失败原因都在
        那里），协议层不认识任何一种状态的含义 —— 和 `tool_rows` 同一条分工。

        顺便把那份清单也放进 `ui(state)` 里发一次：左栏那块读的是 `state`，而
        `/mcp` 改完之后它必须跟着变（前端不做乐观更新，所以"左栏什么时候变"这件事
        由这一条消息回答）。
        """
        runtime = self.runtime
        if runtime is None:
            self._notice("warn", "mcp", "[MCP] 还没有会话。")
            return
        host = getattr(runtime, "mcp", None)
        self.send({
            "v": messages.VERSION,
            "t": messages.OUT_UI,
            "kind": messages.UI_MCP,
            "mcp_servers": host.rows() if host is not None else [],
            "mcp_notes": list(notes or ()),
        })
        self.send(self._state_message())

    def _notice(self, level: str, code: str, text: str) -> None:
        self.send({
            "v": messages.VERSION, "t": messages.OUT_NOTICE,
            "level": level, "code": code, "text": text,
        })

    def _warn(self, text: str) -> None:
        """只在**收尾失败**那条路上用。走 stderr（和 `composition._warn` 同一条路）。

        **不能走 `_notice`**：那种失败发生在换会话**成功之后**，往界面上再发一条
        警告只会让人以为换会话出了问题 —— 而它其实好了，只是旧的 http client 或
        MCP 子进程没干净地收掉。
        """
        print(f"[warn] {text}", file=sys.stderr)

    # -- 出站构造 --------------------------------------------------------------

    def _init_message(self) -> dict[str, Any]:
        runtime = self.runtime
        rows, aliases = runtime.model_rows()
        session_model = runtime.agent.session_model
        return {
            "v": messages.VERSION,
            "t": messages.OUT_INIT,
            "protocol": messages.PROTOCOL,
            "session_id": runtime.session_id,
            "resumed": runtime.resumed,
            # **现在真正在用的那个**，不是配置里那个：恢复一个换过模型的会话时，
            # 这两个是不同的值，而界面那一行要显示的是"接下来会用谁"。
            "model": runtime.current_model,
            "provider": runtime.current_provider,
            # 思考模式那两个旋钮。**必须开场就发**：界面要在第一轮之前就能显示
            # "它现在想不想、想多用力"，而它们可能来自这个会话上次的选择
            # （`/thinking off` 之后恢复会话，那一格该还写着关）。
            "thinking": bool(runtime.agent.thinking),
            "effort": runtime.agent.effort,
            # 可选档位**随协议发**：界面要列它，而它不许 import 内核（决策 18 ——
            # "前端只讲协议"是它能长出 Web 前端的前提）。抄一份清单到前端就会漂，
            # 而漂掉的症状是"清单里列着它，打进去说没有这一档"。
            "effort_levels": list(reasoning.EFFORT_LEVELS),
            # `/model` 那张清单。**开场就发**（它是常量数据，不随会话变）；
            # `current` 那一格由 runtime 按当前模型标好 —— 界面不需要知道
            # "怎么算当前"（那要对账别名折算）。
            "model_catalog": {"models": rows, "aliases": aliases},
            "workspace": str(runtime.workspace),
            "max_steps": runtime.max_steps,
            # **这一次运行开不开流式。** 它是运行期事实，不是前端的偏好 ——
            # 界面按它决定"正文从哪儿来"（见 schema 里那一段）。
            "stream": bool(getattr(runtime, "stream", False)),
            # 上下文窗口（分母）。**它不是 runtime 猜的** —— 响应里没有这个字段，
            # 所以它来自 config 那张按模型名的表；表里没有就是 None，而界面按
            # "只报用量、不报占比"处理（错的百分比比没有百分比更坏）。
            "context_tokens": runtime.context_tokens,
            "tools": [
                {
                    "name": tool.name,
                    "risk": tool.risk.value,
                    "parallel_safe": bool(tool.parallel_safe),
                    "interactive": bool(tool.interactive),
                }
                for tool in runtime.tools.all()
            ],
            # **只含非默认项**（决策 14）：默认时是空 dict，界面那一行就什么都不用
            # 显示。判断"什么算非默认"由 runtime 做 —— 那是 config 的知识。
            "permissions": runtime.non_default_permissions(),
            # **裸路径，不是那句中文**：schema 说它是 `audit_path`，前端要拿它去打开
            # 文件。给人看的那句 `审计日志写到 …` 是 CLI 的事，它由
            # `Runtime.audit_log_line()` 拼（那条老路要的正是那句话）。
            "audit_path": str(Path(runtime.logs.directory) / f"{runtime.session_id}.jsonl"),
            "notices": [
                {"level": notice.level, "code": notice.code, "text": notice.text}
                for notice in runtime.notices(with_tools=False)
            ],
        }

    def on_event(self, record: dict[str, Any]) -> None:
        """Agent 的 `on_event`。**原样转发**，一个字都不加、一个都不减。

        这一条让"审计 = 协议"在字节层面成立：前端收到的 `t:"event"` 就是
        `.tudouni/logs/<id>.jsonl` 里那一行，只是包了一层信封。

        **它不发那条带正文的 `t:"ui"`** —— 那样做会发出一个空串（`on_event` 看到
        `run_finished` 时 `agent.run()` 还没返回）。正文由 `_run_turn` 在拿到返回值
        之后发，见那里的说明。

        **它也不发 `t:"delta"`**：那是另一条流（见 `on_delta`），因为这条流同时
        进审计，而 delta 的数量级完全不同。
        """
        # 把当前 call_id / run_id / step 记下来：审批请求要带上"这是哪一次工具调用"
        # （asker 的签名里没有它），而 delta 和答案那两条 `t:"ui"` 要带上同一个
        # run_id / step。
        kind = record.get("kind")
        with self._state_lock:
            if kind == "tool_call":
                self.current_call_id = record.get("call_id", "")
            if kind == "run_started":
                self._last_run_id = record.get("run_id", "")
            if kind in ("model_call", "tool_call", "tool_result", "tool_batch"):
                # 每一步都会经过 `model_call`，所以它就是"现在第几步"的来源。
                # **`run_started` 不算**：它的 step 是 0（那个回合还没开始跑），
                # 而模型的第一块 delta 在 `model_call` **之后**才吐出来 ——
                # 拿 0 当步号会让第一轮的正文被记成"第 0 步"。
                self._last_step = record.get("step", self._last_step)

        self.send({"v": messages.VERSION, "t": messages.OUT_EVENT, **record})

        # 一条工具返回之后补一份面板快照：**`todo_write` 和 `load_skill` 改的正是
        # 左栏那两块**，而它们在事件流里没有自己的 kind（`tool_call` / `tool_result`
        # 是全部）。不按工具名特判（那会让协议层认识具体工具），代价是每次工具返回
        # 多发一个几百字节的 dict —— 换的是"左栏在回合进行中也是对的"。
        if record.get("kind") == "tool_result":
            self.send(self._state_message())

    # -- 流式增量 --------------------------------------------------------------

    def on_delta(self, *, text: str = "", reasoning: str = "",
                 reset: bool = False) -> None:
        """Agent 的 `on_delta`：把模型吐出来的一块转给前端。

        **它由回合线程调用**（模型往返就在那个线程里），而它写的管道读循环也在写
        —— 所以真正干活的是 `send` 里那根锁，这里只负责拼消息。

        三种调用，各自的形状不一样：
          * 正文 / 思考链各是一条 `t:"delta"`（`channel` 字段分流，不靠猜）；
          * `reset=True` 是**另一条消息种类**（`t:"delta_reset"`）—— 它不带任何正文，
            混在 delta 里会让"一条消息有两种含义"，而"清空"和"追加"是相反的动作。
            一次重试会先后经过 `retry.on_retry` 和适配层自己的 `on_attempt_started`，
            所以**同一条 reset 可能发两次**：前端按"丢掉重来"处理，重复是无害的
            （漏掉一次才是问题）。

        `run_id` / `step` 取自最近一条事件（`on_event` 记的），**而且 step 要加一** ——
        这是实测踩出来的：delta 到达时 `model_call` **还没发**（那条事件是模型调用
        *结束后*才记的账），所以 `_last_step` 还是**上一步**的号，第一轮甚至还是 0
        （`run_started` 的 step 是 0，而它不算步号，见 `on_event`）。正在吐的这一块
        永远属于**进行中的那一步** = 上一步 + 1。

        这个算式对三种情况都成立：第一步（0+1）、工具之后的下一步（n+1）、
        同一步里的重试（`_last_step` 没动过，算出来还是那一步）。
        """
        with self._state_lock:
            run_id, step = self._last_run_id, self._last_step + 1

        if reset:
            self.send({
                "v": messages.VERSION, "t": messages.OUT_DELTA_RESET,
                "session_id": self._session_label(), "run_id": run_id, "step": step,
            })
            return

        for channel, value in ((messages.DELTA_TEXT, text),
                               (messages.DELTA_REASONING, reasoning)):
            if not value:
                continue
            self.send({
                "v": messages.VERSION, "t": messages.OUT_DELTA,
                "session_id": self._session_label(), "run_id": run_id, "step": step,
                "channel": channel, "text": value, "reset": False,
            })

    def _session_label(self) -> str:
        """这条消息属于哪个会话。**出错时也要能发出去**（换会话失败、或者
        runtime 还没接上都不该让一条 delta 抛异常把回合打死）—— 空串就够，
        前端本来就该按 `init`/`session_load` 认当前会话，不靠这个字段。
        """
        runtime = self.runtime
        return "" if runtime is None else runtime.session_id

    # -- 取消 ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请 Agent 在下一个安全点停下。

        **不打断正在进行的那一步**：模型往返和工具执行都是同步的，打断它们会留下
        一条带 tool_calls 却没有对应结果的 assistant 消息 —— 那种会话此后每一轮都
        发不出去（API 直接 400）。所以这个按钮的语义是"停止（等当前步完成）"。
        """
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()


def _no_runtime_factory(bootstrap: Bootstrap, session_id: str) -> Any:
    """没被交进工厂时的兜底：**大声拒绝，而不是假装能造**。

    默认拒绝（而不是在这里 import `composition` 自己造一个）是刻意的：那样会让
    "换会话怎么装配"多出第二个实现，而两个实现漂掉的时候症状是"换过去之后少了半个
    工具集"—— 那种不一致没人查得出来。所以工厂只有一份，由 `serve.py` 交进来
    （它本来就是唯一做装配的地方）。
    """
    raise RuntimeError(
        "这个 ProtocolServer 没有装配工厂，换不了会话 —— "
        "从 `protocol/serve.py` 起来（或测试里注入一份 runtime_factory）。"
    )


__all__ = ["Bootstrap", "ProtocolAsker", "ProtocolQuestioner", "ProtocolServer",
           "RuntimeFactory", "BootstrapFactory", "SessionLister"]
