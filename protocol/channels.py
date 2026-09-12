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
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_runtime.agents import RunCancelled
from agent_runtime.models.types import ModelError
from agent_runtime.protocol import codec, messages
from agent_runtime.protocol.transport_stdio import StdioTransport
from agent_runtime.runtime.channels import Channels, TrustGroupLookup
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.tools.builtin.ask import ANSWERED, SKIPPED, UNAVAILABLE, Answer, AskUserArgs
from agent_runtime.tools.tool import Tool


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
    """协议服务器。见模块 docstring 的那三件事。"""

    def __init__(self, transport: StdioTransport):
        self.transport = transport
        self.pending = _PendingTable()
        self.runtime: Any = None

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

        # 当前这一轮的工作线程。**同时只有一个**（新的一条 user_message 会先 join），
        # 所以发给前端的事件流仍然严格有序。
        self._turn_thread: threading.Thread | None = None

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
        self.transport.send(message)

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
        }


    def _notice(self, level: str, code: str, text: str) -> None:
        self.send({
            "v": messages.VERSION, "t": messages.OUT_NOTICE,
            "level": level, "code": code, "text": text,
        })

    # -- 出站构造 --------------------------------------------------------------

    def _init_message(self) -> dict[str, Any]:
        runtime = self.runtime
        return {
            "v": messages.VERSION,
            "t": messages.OUT_INIT,
            "protocol": messages.VERSION,
            "session_id": runtime.session_id,
            "resumed": runtime.resumed,
            "model": runtime.model_cfg.model,
            "workspace": str(runtime.workspace),
            "max_steps": runtime.max_steps,
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
        """
        # 把当前 call_id 和 run_id 记下来：审批请求要带上"这是哪一次工具调用"
        # （asker 的签名里没有它），而答案那条 `t:"ui"` 要带上同一个 run_id。
        if record.get("kind") == "tool_call":
            self.current_call_id = record.get("call_id", "")
        if record.get("kind") == "run_started":
            self._last_run_id = record.get("run_id", "")

        self.send({"v": messages.VERSION, "t": messages.OUT_EVENT, **record})

        # 一条工具返回之后补一份面板快照：**`todo_write` 和 `load_skill` 改的正是
        # 左栏那两块**，而它们在事件流里没有自己的 kind（`tool_call` / `tool_result`
        # 是全部）。不按工具名特判（那会让协议层认识具体工具），代价是每次工具返回
        # 多发一个几百字节的 dict —— 换的是"左栏在回合进行中也是对的"。
        if record.get("kind") == "tool_result":
            self.send(self._state_message())

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


__all__ = ["ProtocolAsker", "ProtocolQuestioner", "ProtocolServer"]
