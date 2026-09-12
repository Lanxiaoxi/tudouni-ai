"""TUI 的**显示状态**与纯渲染helper。

## 这个文件和 `protocol/state.py` 的分工

两个都叫"状态"，但它们是两件事，混在一起就出问题：

| | `protocol/state.py` | 这里 |
|---|---|---|
| 回答 | **agent 现在在干什么**（working / 等人审批 / 已结束） | **界面长什么样**（滚动位置、哪些折叠着、焦点在哪） |
| 来源 | 协议消息（纯函数推导） | 本地的用户操作 |
| 谁用 | 三个前端共用 | 只有 TUI |
| 能不能参与判定 | **能**（它就是判定） | **绝对不能** |

第二行那句"绝对不能"是这份文件存在的理由：显示状态一旦参与判定，界面就会和
事件流分家，而那种 bug 极难查 —— 画面看起来完全正常。

所以这里的东西都是**纯函数或纯数据**：给一段文本还一段文本，给一个事件还一行字。
没有一个函数需要 Textual、需要网络、需要文件。这让它们能被直接单测 —— 而 TUI 的
其它部分（布局、键盘）很难自动测。
"""

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.protocol import state as agent_state

# 工具结果正文在界面上最多显示这么多字符。**它和审计的 200 字符预览是两回事**：
# 那个限制的理由是"jsonl 里不该抄全文"，这里限制的理由是"一次 read_file 能把屏幕
# 刷掉" —— 两者数值不同是有意的，不是没对齐。
RESULT_PREVIEW = 600

# 思维链默认折叠。它常常是答案的好几倍长，铺开会把回答挤出屏幕。
# 折叠那一行显示字符数，而**字符数由前端自己算**（`len()`）——
# 不让子进程多发一个计数字段，那是同一份事实的第二个来源。
THINKING_PREVIEW = 0


@dataclass
class ViewState:
    """只有界面关心的那一小块状态。

    **它不参与任何判定**（见模块 docstring）。`agent` 是**协议推导出来的**那一份，
    这里只是留一份引用好渲染状态栏 —— 不在这里改它、也不据它做决定。
    """

    agent: agent_state.State = field(default_factory=agent_state.initial)
    # 会话身份/模型/步数上限，来自 `init`。
    session_id: str = ""
    model: str = ""
    max_steps: int = 0
    workspace: str = ""
    audit_path: str = ""
    # 非默认权限那一行（决策 14：runtime 发什么显示什么）。
    permissions: dict[str, Any] = field(default_factory=dict)
    # 工具名 → 风险，来自 `init.tools`。审批面板要显示风险。
    tool_risks: dict[str, str] = field(default_factory=dict)
    # 这一轮开始之后已经画了多少行 —— 用来决定滚到底还是保住用户的位置。
    rendered: list[str] = field(default_factory=list)
    # 见过的 run_id → 那一条 `t:"ui"` 带来的答案。按 run_id 配对（不靠到达顺序）。
    answers: dict[str, str] = field(default_factory=dict)
    # 思维链：`run_id` → (全文, 是否展开)。默认折叠。
    thinking: dict[str, tuple[str, bool]] = field(default_factory=dict)

    def toggle_thinking(self, run_id: str) -> None:
        """折叠/展开。**这是纯界面操作** —— 它不改变任何 agent 的事实。"""
        text, expanded = self.thinking.get(run_id, ("", False))
        if text:
            self.thinking[run_id] = (text, not expanded)

    def status_line(self) -> str:
        """底部状态栏那一行。

        它是**投影**：`agent.activity` 直接来自最近一条事件，这里只是加上会话规模和
        步数。没有流式的时候，"模型在想"和"工具在跑"从事件流里分不出更细的粒度，
        硬分只能靠时间间隔去猜 —— 那是用事件反推事件。
        """
        parts = []
        if self.agent.phase == agent_state.IDLE:
            parts.append("空闲")
        else:
            mark = {
                agent_state.WORKING: "…",
                agent_state.WAITING_PERMISSION: "?",
                agent_state.WAITING_HUMAN: "?",
                agent_state.FINISHED: "✓",
                agent_state.LIMITED: "!",
                agent_state.FAILED: "✗",
                agent_state.CANCELLED: "-",
            }.get(self.agent.phase, "·")
            parts.append(f"{mark} {self.agent.activity or self.agent.phase}")
        if self.agent.step and self.max_steps:
            parts.append(f"第 {self.agent.step}/{self.max_steps} 步")
        if self.session_id:
            parts.append(self.session_id)
        if self.model:
            parts.append(self.model)
        if self.permissions:
            parts.append("权限：" + "；".join(
                f"{k}={v}" for k, v in self.permissions.items()
            ))
        return "  ".join(parts)


def clip(text: str, limit: int) -> tuple[str, bool]:
    """截一段文本，返回 `(结果, 是否被截了)`。

    被截时**什么都不加** —— 加一句"…（共 N 字符）"是调用方的排版决定，而这个函数
    只回答"要不要加"。分开是因为调用方在两种场景下要的写法不同（工具结果和思维链
    的省略号位置不一样）。
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def indent(text: str, prefix: str = "  │ ") -> str:
    """给多行文本加前缀。工具结果是一整块，不缩进的话它和对话正文混在一起。"""
    return "\n".join(prefix + line for line in text.splitlines())


def render_event(state: ViewState, message: dict[str, Any]) -> list[str]:
    """一条 `t:"event"` → 要画的行。**纯函数。**

    返回 `list[str]` 而不是直接往控件里写，是为了让它能被单测 —— 而"事件怎么变成
    给人看的字"恰恰是这个界面里最值得测的部分（它全是判断，没有布局）。
    """
    kind = message.get("kind")
    out: list[str] = []

    if kind == "run_started":
        out.append(f"\n[你] {message.get('user_input', '')}")

    elif kind == "model_call":
        status = message.get("status")
        if status != "ok":
            attempt = message.get("attempt", 1)
            backoff = message.get("backoff_ms")
            tail = f"，{backoff}ms 后重试" if backoff else ""
            out.append(f"  · 模型调用失败（第 {attempt} 次）{tail}")
        else:
            tokens = message.get("prompt_tokens")
            cached = message.get("cached_tokens")
            detail = ""
            if tokens is not None:
                detail = f"  上下文 {tokens} token"
                if cached:
                    detail += f"（命中 {cached}）"
            out.append(f"  · 模型 {message.get('duration_ms')}ms{detail}")
            reasoning = message.get("reasoning")
            if reasoning:
                _thinking_lines(state, message, reasoning, out)

    elif kind == "tool_call":
        index = message.get("tool_index")
        at = f"[{index + 1}] " if isinstance(index, int) else ""
        out.append(f"  → {at}{message.get('tool')}({message.get('arguments', '')})")

    elif kind == "tool_result":
        status = message.get("status")
        mark = {"ok": "✓", "denied": "✗", "invalid_args": "✗"}.get(status, "!")
        out.append(f"  ← {mark} {message.get('tool')}  "
                   f"{message.get('chars')} 字符  {message.get('duration_ms')}ms")
        if status == "denied":
            # **拒绝要单独说一句**：它是"没执行"，和"执行了但出错"完全不同，
            # 而两者在 `chars` 上看不出来。
            out.append("      （被拒绝，没有执行）")

    elif kind == "permission":
        outcome = message.get("outcome", "")
        rule = message.get("rule")
        remembered = message.get("remembered")
        extra = ""
        if rule:
            extra = f"（命中规则 {' '.join(rule)}）"
        elif remembered:
            extra = f"（已记住：{'、'.join(remembered)}）"
        elif message.get("waited_ms"):
            extra = f"（你看了 {message['waited_ms']}ms）"
        out.append(f"  · 权限 {message.get('tool')} → {outcome}{extra}")

    elif kind == "tool_batch":
        out.append(f"  · {message.get('calls')} 个只读工具并发"
                   f"（{message.get('wall_ms')}ms）")

    elif kind == "run_finished":
        reason = message.get("stop_reason")
        out.append(f"  · 回合结束：{reason}  {message.get('duration_ms')}ms")
        if reason == "max_steps":
            # **必须和 answered 长得不一样。** 这是 `StepLimitExceeded` 那个类存在的
            # 全部理由：不许让人分不清"答完了"和"被砍断了"。
            out.append("  ！步数用尽，这一轮**没有**收尾 —— 会话是好的，可以接着跑。")

    return out


def _thinking_lines(state: ViewState, message: dict[str, Any],
                    reasoning: str, out: list[str]) -> None:
    """思维链那一段。**默认折叠成一行**（决策 17）。

    折叠那行的字符数由这里 `len()` 出来 —— 不让子进程多发一个 `reasoning_chars`：
    那是同一份事实的第二个来源，而两侧对"一个字符"的口径未必一致（emoji、代理对），
    一个"字符数对不上"的 bug 查起来毫无价值。
    """
    run_id = message.get("run_id", "")
    expanded = state.thinking.get(run_id, ("", False))[1]
    state.thinking[run_id] = (reasoning, expanded)
    if expanded:
        out.append("  ┌ 思考过程")
        out.append(indent(reasoning, "  │ "))
        out.append("  └")
    else:
        out.append(f"  ▸ 思考过程（{len(reasoning)} 字符，Ctrl+T 展开）")


def render_ui_answer(state: ViewState, message: dict[str, Any]) -> list[str]:
    """`t:"ui"` 那条 `run_finished` → 要画的行。**答案在这里。**

    审计里没有正文（`Agent.run` 的返回值只交给调用方），所以非流式模式下这是界面
    拿到答案的唯一途径。按 `run_id` 记下来 —— 它和 `event` 那条 `run_finished` 是
    两条消息，**顺序不保证**，所以不许靠到达顺序配对。
    """
    run_id = message.get("run_id", "")
    answer = message.get("answer") or ""
    state.answers[run_id] = answer
    if not answer:
        return []
    return ["", f"[agent] {answer}", ""]
