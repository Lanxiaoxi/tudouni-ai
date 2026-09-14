"""事件 → 状态。**一张纯函数的推导表。**

## 为什么它在协议层，不在某个前端里

"agent 现在处于什么状态"是 **agent 的性质**，不是某个界面的性质。三个前端（CLI、
TUI、将来的 Web）都要回答同一个问题，而它们各写一份推导的下场是：同一份事件流在
两个界面上显示成两个状态，而"哪个对"没有第三方可以裁决。

第一版设计把它放在 `ui/state.py`（当时以为只有一个前端）。那是错的 —— 见
doc/TUI-design.md 的 10.1。

## 为什么是"推导"而不是"状态机"

一个独立的状态机（`if loading: ...` / `if tool_calling: ...`）会变成**第二份事实**：
它一旦和事件流不一致，界面会显示一个不存在的状态，而那种 bug 极难查 ——
因为画面看起来完全正常。所以这里只有一条函数：给状态和一条事件，还一个新状态。

## 边界：这里不管"界面长什么样"

滚动位置、哪些面板折叠着、焦点在哪 —— 那些是**显示状态**，属于前端自己
（TUI 的 `view_state.py`）。它们**不许参与任何判定**，也**不许**出现在这个模块里。
"""

from dataclasses import dataclass, replace

# 状态。**刻意比第一版的十二个少得多**，因为决策 1（无流式）和决策 3（不渲染工具
# 卡片）拿掉了它们的来源：没有 delta 就没有 STREAMING/THINKING，不渲染卡片就不需要
# 区分"工具在排队"和"工具在跑"。
IDLE = "idle"
WORKING = "working"
WAITING_PERMISSION = "waiting_permission"
WAITING_HUMAN = "waiting_human"
FINISHED = "finished"
LIMITED = "limited"
FAILED = "failed"
CANCELLED = "cancelled"

# 终态：收到它们之后这一轮就结束了，下一次 user_message 才重新开始。
_TERMINAL = (FINISHED, LIMITED, FAILED, CANCELLED)


@dataclass(frozen=True, slots=True)
class State:
    """某一刻的状态，以及"现在在做什么"那一行字。

    `activity` 是**投影，不是状态机的一部分**：它直接来自最近一条事件。
    没有流式的时候，"模型在想"和"工具在跑"在事件流里分不出更细的粒度，硬要分只能
    靠时间间隔去猜 —— 那是用事件反推事件。所以这里只老实说"最近发生了什么"。
    """

    phase: str = IDLE
    run_id: str = ""
    step: int = 0
    activity: str = ""
    # 这一轮的最终答案（`t:"ui"` 那条 `run_finished`）。没有流式时它是界面唯一的
    # 答案来源。
    answer: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL

    @property
    def is_busy(self) -> bool:
        return self.phase == WORKING


def initial() -> State:
    return State()


def reduce(state: State, message: dict) -> State:
    """吃一条协议消息，还一个新状态。**纯函数**：不打印、不做 I/O、不改入参。

    不认识的 `t` 或 `kind` 一律**原样返回**（不崩、不变状态）—— 和
    `JsonlSink.read()` 跳过坏行、`--audit` 缺字段显示 `?` 是同一个取向：
    协议的两端会分别升级，老客户端必须能活在一条它不认识的消息旁边。
    """
    kind = message.get("t")

    if kind == "event":
        return _on_event(state, message)
    if kind == "ui":
        return _on_ui(state, message)
    # init / session_load / notice / *_request 不改变 phase：
    #   * init / session_load 是开场数据，状态本来就是 idle；
    #   * notice 是旁白；
    #   * *_request 的"在等人"由各自的事件体现（permission 事件 / tool_result），
    #     而把 phase 押在一条**要我们回应**的消息上会让"忘了回"变成一个查不出的挂起。
    return state


def _on_event(state: State, message: dict) -> State:
    kind = message.get("kind")
    run_id = message.get("run_id", state.run_id)
    step = message.get("step", state.step)

    if kind == "run_started":
        return State(phase=WORKING, run_id=run_id, step=step, activity="准备中")

    if kind == "model_call":
        if message.get("status") == "error":
            # 重试退避中。**它仍然属于 working** —— 加一个 RETRYING 状态只会让
            # 界面多一个必须和 working 保持同步的维度，而 activity 已经说清了。
            return replace(state, run_id=run_id, step=step, activity="模型调用失败，重试中")
        return replace(state, run_id=run_id, step=step, activity="模型在想")

    if kind == "tool_call":
        n = message.get("tool", "工具")
        idx = message.get("tool_index")
        at = f"（第 {idx + 1} 个）" if isinstance(idx, int) else ""
        return replace(state, run_id=run_id, step=step, activity=f"要调用 {n}{at}")

    if kind == "tool_result":
        n = message.get("tool", "工具")
        status = message.get("status", "")
        verb = {"ok": "返回了", "denied": "被拒绝", "invalid_args": "参数不合法"}.get(
            status, "出错"
        )
        return replace(state, run_id=run_id, step=step, activity=f"{n} {verb}")

    if kind == "tool_batch":
        calls = message.get("calls", 0)
        return replace(state, run_id=run_id, step=step,
                       activity=f"{calls} 个只读工具并发执行中")

    if kind == "permission":
        outcome = message.get("outcome", "")
        waited = {"approved": "已批准", "user_denied": "已拒绝",
                  "policy_denied": "策略禁止", "no_asker": "没有审批通道"}.get(outcome, "")
        return replace(state, run_id=run_id, step=step,
                       activity=f"{message.get('tool', '工具')} {waited}".strip())

    if kind == "run_finished":
        reason = message.get("stop_reason", "")
        phase = {
            "answered": FINISHED,
            "max_steps": LIMITED,
            "cancelled": CANCELLED,
            "model_error": FAILED,
            "model_fatal": FAILED,
        }.get(reason, FAILED)
        return replace(state, phase=phase, run_id=run_id, step=step, activity="")

    return state


def _on_ui(state: State, message: dict) -> State:
    """只给界面的那一条：`run_finished` 带正文。

    它可能比 `event` 那条 `run_finished` 先到或后到（两条消息），所以这里**不改
    phase** —— phase 只由 `event` 那条定。这样"答案到了但状态还没转"这种中间态
    不会存在，前端也不必去做顺序上的补偿。
    """
    if message.get("kind") == "run_finished":
        return replace(state, answer=message.get("answer", ""))
    return state
