"""TUI 的**显示状态**与纯渲染 helper。

## 这个文件和 `protocol/state.py` 的分工

两个都叫"状态"，但它们是两件事，混在一起就出问题：

| | `protocol/state.py` | 这里 |
|---|---|---|
| 回答 | **agent 现在在干什么**（working / 等人审批 / 已结束） | **界面长什么样**（哪些折叠着、左栏开没开、焦点在哪） |
| 来源 | 协议消息（纯函数推导） | 本地的用户操作 + 协议消息的**呈现形态** |
| 谁用 | 三个前端共用 | 只有 TUI |
| 能不能参与判定 | **能**（它就是判定） | **绝对不能** |

第二行那句"绝对不能"是这份文件存在的理由：显示状态一旦参与判定，界面就会和
事件流分家，而那种 bug 极难查 —— 画面看起来完全正常。

所以这里的东西都是**纯函数或纯数据**：给一段文本还一段文本，给一个事件还几行字。
没有一个函数需要 Textual、需要网络、需要文件。这让它们能被直接单测 —— 而 TUI 的
其它部分（布局、键盘）很难自动测。

## 为什么返回 `Line` 而不是 `str`（v2 的改动 7）

第一版是 `list[str]`：一行一个字，颜色由调用方猜。而设计稿要的是**一套有语法的行**
（工具行要按风险着色、回合头要带分隔线、思考块要有自己的底），那就必须让"这一行
属于哪一类"跟着行一起回来。

`Line` 是 `str` 的子类，**这是刻意的**：老的单测写的是 `"没有执行" in line`，而
`in` 对 `str` 子类照样成立 —— 于是"升级成结构化行"这件事没有把已有断言全推倒。
多出来的两样东西：

  * `role`：这一行是**哪一类**（正文 / 过程行 / 工具行 / 风险行 / 回合头……）。
    调用方按它选颜色，**不许按它做判定**（它是显示状态）；
  * `segments`：一行里分段着色（`→ [1] read_file(...)` 的高风险尾巴是另一档色）。
    为 None 表示"整行一个 role"，这也是绝大多数行。
"""

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.protocol import state as agent_state

# 工具结果正文在界面上**一个字符都不显示**，只有一行"多少字符、多久"（决策 3：
# v1 不渲染工具结果卡片）。要看内容去会话历史 —— 审计里那份 200 字符预览比这里
# 该显示的多，但它是给审计的，前端不该去拿它当正文用。所以这个模块里**没有**
# "结果预览多少字符"这种常数：那个数只属于审计（`agent.AUDIT_PREVIEW_LIMIT`）。

# 上下文栏的宽度。**272px 折算成列**：设计稿的栏宽 272px、正文字号 14px，
# 而终端里"一列"就是正文字号的一半宽 —— 32 列是照着 F1 的量出来的（它和 F5 那张
# 80 列窄屏图是同一件事的两面）。低于 `NARROW_COLUMNS` 列时它降级成一行摘要。
RAIL_WIDTH = 32
NARROW_COLUMNS = 120


# --- 行的角色 ------------------------------------------------------------------

ROLE_USER = "user"            # 用户说的话（> …）
ROLE_ANSWER = "answer"        # agent 的正文（● …）
ROLE_PROCESS = "process"      # 过程行（· 模型 1.2s …）
ROLE_TOOL = "tool"            # 工具调用行（→ [1] read_file(…)）
ROLE_RESULT = "result"        # 工具结果行（← [1] ✓ 8,412 字符 41ms）
ROLE_RISK_MEDIUM = "risk_medium"   # MEDIUM 风险那一小段
ROLE_RISK_HIGH = "risk_high"       # HIGH 风险那一小段
ROLE_THINK_HEAD = "think_head"   # 思考折叠行（▸ 思考过程（1,284 字符 …））
ROLE_THINK_BODY = "think_body"   # 思考正文（在 sunk 底上）
ROLE_TURN_START = "turn_start"   # 回合头（回合 1   进行中 · 第 1 步）
ROLE_TURN_END = "turn_end"       # 回合头的**最终形态**（回合 1   3 步 · 4.2s · 已答）
ROLE_WAITING = "waiting"      # 等待你的批准 [y] 允许 [n] 拒绝 …
ROLE_DENIED = "denied"        # （被拒绝，没有执行）
ROLE_NOTICE = "notice"        # 运行期旁白
ROLE_WARN = "warn"            # 旁白里该看一眼的那些
ROLE_RULE = "rule"            # 提示与分隔（（恢复 12 条历史…））
ROLE_SKILL = "skill"          # 已加载技能的名字
ROLE_QUOTE = "quote"          # 引用块左边那条竖线（思考正文）

# 审批结果的措辞。**key 是 `security/gate.py` 里那几个 outcome 字面量**，
# 而值是给人看的说法 —— 界面不认识"approve 是什么意思"，它只是翻译。
OUTCOME_TEXT = {
    "approved": "批准",
    "autopilot": "自动放行",
    "command_allowed": "批准",
    "user_denied": "拒绝",
    "policy_denied": "策略禁止",
    "no_asker": "没有审批通道",
}

# 回合的结局。**它必须和"答完了"分得清**（`StepLimitExceeded` 那个类存在的
# 全部理由），所以三种结局的措辞一个都不重合。
STOP_REASON_TEXT = {
    "answered": "已答",
    "max_steps": "步数用尽",
    "cancelled": "已中断",
    "model_error": "模型失败",
    "model_fatal": "模型失败",
}

# 任务状态的记号。和 `tools/builtin/todo.py` 的三个状态一一对应。
TODO_MARK = {"completed": "✓", "in_progress": "◐", "pending": "○"}


class Line(str):
    """一行字 + 它属于哪一类 + （可选）分段着色。

    **它是 `str`**：`"没有执行" in line` 对老断言照样成立（见模块 docstring）。
    """

    role: str
    segments: list[tuple[str, str]] | None

    def __new__(cls, text: str, role: str = ROLE_PROCESS,
                segments: list[tuple[str, str]] | None = None) -> "Line":
        obj = super().__new__(cls, text)
        obj.role = role
        obj.segments = segments
        return obj


def seg(*parts: tuple[str, str]) -> Line:
    """**分段行**：`seg(("→ ", ROLE_PROCESS), ("read_file", ROLE_TOOL), …)`。

    文本是各段的拼接，所以 `in` / `len` 这些仍然按整行算 —— 分段只是画法。
    第一段的 role 兼作整行的 role（当整行需要一档兜底色时用它）。
    """
    return Line("".join(text for text, _ in parts), parts[0][1], list(parts))


# 引用块左边那条竖线。**它跟着思维链正文一起走**，所以两条构造路径（事件到达时
# 首屏、`Ctrl+T` 展开时重画）用的是同一个函数 —— 两边各写一遍的话，展开之后再
# 折叠、再展开就会长出两种长相（那种 bug 只有反复按 `Ctrl+T` 才看得见）。
QUOTE_BAR = "  │ "


def quote_line(text: str) -> Line:
    """思考正文的一行：`  │ 正文`，竖线单独一档颜色（`ROLE_QUOTE` → 主题的 line 色）。

    底色那件事由控件做（`.think-body` 用 `sunk`）—— 这里只负责"这一行看起来像引用"。
    两根一起才是设计稿里那个块：**底色划范围、竖线定边界**。
    """
    return seg((QUOTE_BAR, ROLE_QUOTE), (text, ROLE_THINK_BODY))


# --- 数字的写法 ----------------------------------------------------------------
#
# 这两个格式化函数在 `frontends/cli/` 里也有一份（`_tokens_text` / `_ms_text`）。
# **这一版没有合并它们**：CLI 那条路按决策 19 是冻结的（不许往直连那条路上加新
# 功能），而为了三个 f-string 去改一个冻结前端的输出格式，代价比重复大。
# 口径必须一致，所以两处的规则写在这里：token 用 k/M 两位有效数字，毫秒在 1s
# 以下用 ms、以上用 s 一位小数。

def _trim(value: float, suffix: str) -> str:
    """`1.0` → `1`，`12.4` → `12.4`。**整数不写小数点**：状态栏那一格很窄，
    而 `1.0M` 和 `1M` 说的是同一件事（多出来的两个字符买不到任何信息）。"""
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + suffix


def tokens_text(count: int | None) -> str:
    """`12.4k` / `1M` / `985`。**不用科学计数法**：终端里没人读 `1.2e+06`。"""
    if count is None:
        return "—"
    if count >= 1_000_000:
        return _trim(count / 1_000_000, "M")
    if count >= 1_000:
        return _trim(count / 1_000, "k")
    return str(count)


def ms_text(ms: int | None) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def count_text(count: int) -> str:
    """`8,412` —— 千位分隔。工具结果的字符数是唯一会长到五位的数。"""
    return f"{count:,}"


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


# --- 回合 ----------------------------------------------------------------------

@dataclass
class Turn:
    """一个回合（一次提问 = 一个回合 = 一个 `run_id`）。

    设计稿的对话流是**按回合分块**的（每块有自己的头、自己的折叠状态），所以
    "哪些行属于哪个回合"必须在数据里有一份 —— 这正是当初把渲染抽成纯函数的回报：
    这里只是多记一个结构，单测覆盖的那些"事件 → 行"的判断一行都不用改。
    """

    run_id: str
    index: int                      # 回合 1、回合 2……
    user_input: str = ""
    steps: int = 0                  # 这一轮跑了几次模型往返
    duration_ms: int = 0
    outcome: str = ""               # `STOP_REASON_TEXT` 里的那个说法
    finished: bool = False
    # 界面自己数秒用的起点。**它由 App 写**（`time.monotonic()`）—— 纯函数里
    # 不取时间，"本轮 1.4s"这种实时读数才不至于让渲染函数变成不纯的。
    started_at: float | None = None


# --- 显示状态 ------------------------------------------------------------------

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
    # 上下文窗口（分母），来自 `init.context_tokens`。None = 只报用量、不报占比。
    context_tokens: int | None = None
    resumed: bool = False
    # 非默认权限那一行（决策 14：runtime 发什么显示什么）。
    permissions: dict[str, Any] = field(default_factory=dict)
    # 工具名 → 风险，来自 `init.tools`。审批面板要显示风险，工具行要按它上色。
    tool_risks: dict[str, str] = field(default_factory=dict)
    # 工具名 → `init.tools` 里那一整条（risk / parallel_safe / interactive）。
    # 审批面板要用它说清"这是内置工具还是外部工具""能不能和别的工具并发" ——
    # 那两句是 F3 面板上的字，而它们的来源是这一份，不是界面自己猜的。
    tool_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 见过的 run_id → 那一条 `t:"ui"` 带来的答案。按 run_id 配对（不靠到达顺序）。
    answers: dict[str, str] = field(default_factory=dict)
    # 思维链：`run_id` → (全文, 是否展开)。默认折叠。
    thinking: dict[str, tuple[str, bool]] = field(default_factory=dict)
    # 回合流。最后一个是"正在跑的那一轮"。
    turns: list[Turn] = field(default_factory=list)
    # `call_id` → (tool_index, 工具名)：结果行靠它配对（决策 4）。
    calls: dict[str, tuple[int, str]] = field(default_factory=dict)
    # 最近一次模型往返的用量（状态栏那三个数）。
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    # 面板数据快照（`t:"ui", kind:"state"`）。
    todos: list[dict[str, str]] = field(default_factory=list)
    skills: list[dict[str, str]] = field(default_factory=list)
    skill_catalog: list[dict[str, str]] = field(default_factory=list)
    messages: int = 0
    steps: int = 0
    risk_scope: list[dict[str, str]] = field(default_factory=list)
    granted_tools: list[str] = field(default_factory=list)
    granted_prefixes: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    # 用户刚发出去的那句话。**回显要用它而不是事件里那份预览** ——
    # `run_started.user_input` 是审计的 200 字符预览，而用户敲的字界面本来就有。
    pending_input: str = ""
    # 上下文栏开着吗。**默认收起**（决策 1）：宽屏且真的有任务/技能时才自动展开。
    rail_open: bool = False
    # 用户手动按过 Ctrl+B 之后就不再自动开合 —— 一次明确的操作不该被下一次更新推翻。
    rail_pinned: bool = False

    def reset_for_session(self) -> None:
        """把**属于某一个会话**的东西全清掉，只留下界面自己的开关。

        换会话（`/new` / `/resume`）时调它。**这份清单必须完整**，因为"漏了哪个字段"
        的症状全都很难看而且各不相同：

          * 漏 `turns` / `answers` / `thinking` / `calls` —— 新会话里混着旧会话的回合，
            而且 `Ctrl+T` 会去展开一个已经不在的 run（它按 run_id 查，查得到旧的那份）；
          * 漏 `todos` / `skills` / `skill_catalog` —— 左栏显示**上一个会话**的任务列表。
            这一条最坏：它看起来完全正常，而用户会以为那些任务是现在这个会话的；
          * 漏 `agent` —— 状态栏按上一个会话的 phase 显示"正在跑"或"已答"，而新会话
            一步都没走（协议那边是干净的，所以这个"正在跑"永远不会结束）；
          * 漏 `pending_input` —— 上一句话贴到新会话的第一个回合头上。

        **rail_open / rail_pinned 不清**：那是"我要不要看左栏"，和聊的是哪个会话无关
        —— 换一次会话就把用户手动收起的栏顶开，是最容易被当成 bug 的那种"贴心"。
        """
        self.agent = agent_state.initial()
        self.answers.clear()
        self.thinking.clear()
        self.turns.clear()
        self.calls.clear()
        self.todos = []
        self.skills = []
        self.skill_catalog = []
        self.risk_scope = []
        self.granted_tools = []
        self.granted_prefixes = []
        self.denied_tools = []
        self.messages = 0
        self.steps = 0
        self.prompt_tokens = None
        self.cached_tokens = None
        self.pending_input = ""

    # -- 回合 ------------------------------------------------------------------

    @property
    def current_turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def turn_for(self, run_id: str) -> Turn | None:
        for turn in reversed(self.turns):
            if turn.run_id == run_id:
                return turn
        return None

    def begin_turn(self, message: dict[str, Any]) -> Turn:
        """`run_started` 到了：开一个新回合。**同一个 run_id 不重复开**（重放安全）。"""
        run_id = message.get("run_id", "")
        existing = self.turn_for(run_id)
        if existing is not None:
            return existing
        text = self.pending_input or message.get("user_input", "")
        self.pending_input = ""
        turn = Turn(run_id=run_id, index=len(self.turns) + 1, user_input=text)
        self.turns.append(turn)
        return turn

    def toggle_thinking(self, run_id: str) -> None:
        """折叠/展开。**这是纯界面操作** —— 它不改变任何 agent 的事实。"""
        text, expanded = self.thinking.get(run_id, ("", False))
        if text:
            self.thinking[run_id] = (text, not expanded)

    # -- 状态栏 ----------------------------------------------------------------

    def status_left(self) -> str:
        """状态栏左边：**agent 在干什么 + 走到第几步**。

        它是**投影**：`agent.activity` 直接来自最近一条事件，这里只是加上步数。
        没有流式的时候，"模型在想"和"工具在跑"从事件流里分不出更细的粒度，
        硬分只能靠时间间隔去猜 —— 那是用事件反推事件。

        **终态要说得出话**：`run_finished` 会把 `activity` 清空（回合结束了，没有
        "正在做什么"），所以那几种情况由 phase 补一个说法 —— 否则状态栏会剩下一个
        光秃秃的记号（"✓ "），看起来像坏了。
        """
        mark = {
            agent_state.IDLE: "○",
            agent_state.WORKING: "●",
            agent_state.WAITING_PERMISSION: "●",
            agent_state.WAITING_HUMAN: "●",
            agent_state.FINISHED: "✓",
            agent_state.LIMITED: "!",
            agent_state.FAILED: "✗",
            agent_state.CANCELLED: "—",
        }.get(self.agent.phase, "·")
        settled = {
            agent_state.FINISHED: "已答",
            agent_state.LIMITED: "步数用尽",
            agent_state.FAILED: "本轮失败",
            agent_state.CANCELLED: "已中断",
        }.get(self.agent.phase, "")
        text = self.agent.activity or settled
        if self.agent.phase == agent_state.IDLE:
            text = "空闲 · 说出一句话后才开始" if not self.turns else "空闲"
        parts = [f"{mark} {text}".rstrip()]
        if self.agent.step and self.max_steps:
            parts.append(f"第 {self.agent.step} / {self.max_steps} 步")
        return "    ".join(parts)

    def status_right(self, now: float | None = None,
                     compact: bool = False) -> str:
        """状态栏右边：**这一轮烧了多少上下文、命中多少、跑了多久、审计写在哪**。

        四个数放在一起是因为它们是同一类东西（"这次运行的成本与去处"），而且它们
        此前只有"回合结束时那一条统计"这一条出口 —— 也就是**只有跑完才看得见**。

        `compact=True`（窄屏）只留前两格：审计路径和"本轮"是最长也最次要的两项，
        而窄屏上把四格都留着的结果是**左段被裁成半句**（实测：状态栏显示
        `● 要调用 edit_file（第`，那句话的意思整个没了）。F5 那张 80 列的图里
        右边也就只剩用量。
        """
        used = tokens_text(self.prompt_tokens)
        if self.prompt_tokens is None:
            context = "上下文  —"
        elif self.context_tokens and not compact:
            percent = self.prompt_tokens / self.context_tokens * 100
            context = (f"上下文 {used} / {tokens_text(self.context_tokens)}"
                       f"（{percent:.1f}%）")
        elif self.context_tokens:
            context = f"上下文 {used} / {tokens_text(self.context_tokens)}"
        else:
            # 表里没有这个模型：**只报用量，不猜分母**（错的百分比比没有百分比更坏）。
            context = f"上下文 {used}"

        if self.prompt_tokens and self.cached_tokens is not None:
            hit = f"命中 {self.cached_tokens / self.prompt_tokens * 100:.0f}%"
        else:
            hit = "命中  —"

        if compact:
            return "  ·  ".join([context, hit])

        turn = self.current_turn
        if self.agent.is_busy and turn is not None and turn.started_at is not None \
                and now is not None:
            span = f"本轮 {ms_text(int((now - turn.started_at) * 1000))}"
        elif turn is not None and turn.duration_ms:
            span = f"本轮 {ms_text(turn.duration_ms)}"
        elif self.steps or self.messages > 1:
            # **"还没有会话"和"会话里有东西"要分得开**：新会话的 `messages` 是 1
            # （那条 system 消息），照抄成"会话 1 条 · 0 步"会让人以为已经聊过了
            # （实测：用户截图里那一行就是这么读的）。所以 `>1` 才算有内容 ——
            # 和 `resumed` 那条"新会话也带一条 system 消息"的坑是同一个。
            span = f"会话 {self.messages} 条 · {self.steps} 步"
        else:
            span = "会话  —"
        return "  ·  ".join([context, hit, span, f"审计 {self.audit_dir_short()}"])

    def audit_short(self) -> str:
        """审计路径的短写法：**能省掉工作区前缀就省掉**。

        状态栏那一行右边已经很挤，而完整路径的前 30 列在这台机器上是常量
        （`C:\\Users\\…\\agent_runtime\\.tudouni\\logs`）。省掉之后剩下的
        `.tudouni/logs` 恰好是设计稿 F2 里写的那一段。
        """
        if not self.audit_path:
            return "—"
        path = self.audit_path.replace("\\", "/")
        marker = "/.tudouni/"
        index = path.find(marker)
        if index >= 0:
            return ".tudouni/" + path[index + len(marker):]
        parts = path.strip("/").split("/")
        return "/".join(parts[-2:]) if len(parts) > 1 else path

    def audit_dir_short(self) -> str:
        """审计**目录**的短写法 —— 左栏那一行要的是它。

        文件名就是会话 id，而它已经在同一块的上一行写着（F2 里那一行写的就是
        `.tudouni/logs`）。在 32 列的栏里再抄一遍文件名会折成三行，把"权限范围"
        那块顶下去。
        """
        short = self.audit_short()
        if short == "—" or "/" not in short:
            return short
        return short.rsplit("/", 1)[0]


# --- 事件 → 行 -----------------------------------------------------------------

def render_event(state: ViewState, message: dict[str, Any]) -> list[Line]:
    """一条 `t:"event"` → 要画的行。**纯函数。**

    返回 `list[Line]` 而不是直接往控件里写，是为了让它能被单测 —— 而"事件怎么变成
    给人看的字"恰恰是这个界面里最值得测的部分（它全是判断，没有布局）。
    """
    kind = message.get("kind")
    out: list[Line] = []

    if kind == "run_started":
        turn = state.begin_turn(message)
        out.append(_turn_header(turn))
        if turn.user_input:
            out.append(seg(("  > ", ROLE_PROCESS), (turn.user_input, ROLE_USER)))

    elif kind == "model_call":
        turn = state.current_turn
        status = message.get("status")
        if status != "ok":
            attempt = message.get("attempt", 1)
            backoff = message.get("backoff_ms")
            tail = f"，{backoff}ms 后重试" if backoff else ""
            out.append(Line(f"  · 模型调用失败（第 {attempt} 次）{tail}", ROLE_WARN))
        else:
            if turn is not None:
                turn.steps += 1
            state.prompt_tokens = message.get("prompt_tokens", state.prompt_tokens)
            state.cached_tokens = message.get("cached_tokens", state.cached_tokens)
            out.append(_model_line(state, message))
            reasoning = message.get("reasoning")
            if reasoning:
                out.extend(_thinking_lines(state, message, reasoning))

    elif kind == "tool_call":
        index = message.get("tool_index")
        tool = message.get("tool", "?")
        call_id = message.get("call_id", "")
        if call_id and isinstance(index, int):
            state.calls[call_id] = (index, tool)
        out.append(_tool_call_line(state, index, tool, message.get("arguments", "")))

    elif kind == "tool_result":
        call_id = message.get("call_id", "")
        known = state.calls.get(call_id)
        index = known[0] if known else message.get("tool_index")
        tool = known[1] if known else message.get("tool", "?")
        status = message.get("status")
        out.append(_tool_result_line(index, tool, message))
        if status == "denied":
            # **拒绝要单独说一句**：它是"没执行"，和"执行了但出错"完全不同，
            # 而两者在 `chars` 上看不出来。
            out.append(Line("      （被拒绝，没有执行）", ROLE_DENIED))

    elif kind == "permission":
        out.append(_permission_line(message))

    elif kind == "tool_batch":
        calls = message.get("calls")
        wall = message.get("wall_ms")
        out.append(seg(
            ("  · ", ROLE_PROCESS),
            (f"{calls} 个只读工具并发执行完毕", ROLE_PROCESS),
            (f"（{wall}ms）", ROLE_RULE),
        ))

    elif kind == "run_finished":
        out.extend(_finish_turn(state, message))

    return out


def _turn_header(turn: Turn) -> Line:
    """回合分隔线：`回合 2   进行中 · 第 1 步`。

    **它恰好两段**：左边是标题，右边是状态。中间那条横线由控件按实际宽度画
    （`TurnBlock.refresh_head`）—— 设计稿 F1 里那一行是"标题 ──── 状态"，
    而"───"要多少格只有布局之后才知道。分成两段是给控件一个稳定的切点：
    它不该去猜"哪一段是标题"（按 `str` 找第一个空格会在标题里带空格时切错）。
    """
    return seg(
        (f"回合 {turn.index}", ROLE_TURN_START),
        ("进行中 · 第 1 步", ROLE_WAITING),
    )


def turn_head_parts(line: Line) -> tuple[str, str, str]:
    """回合头 → `(左, 右, 右边的 role)`。**两段是约定**（见 `_turn_header`）。

    只有一段（`TurnBlock` 刚建出来时那个占位）时右边是空的 —— 那时候还没有
    "第几步"可说。
    """
    if not line.segments:
        return str(line), "", line.role
    left = line.segments[0][0]
    if len(line.segments) < 2:
        return left, "", line.role
    right = "".join(text for text, _role in line.segments[1:])
    return left, right, line.segments[1][1]


def _model_line(state: ViewState, message: dict[str, Any]) -> Line:
    """过程行：`· 模型 1.2s  上下文 12.4k token（命中 10.1k · 88%）`。"""
    parts: list[tuple[str, str]] = [
        ("  · 模型 ", ROLE_PROCESS),
        (ms_text(message.get("duration_ms")), ROLE_PROCESS),
    ]
    tokens = message.get("prompt_tokens")
    cached = message.get("cached_tokens")
    if tokens is not None:
        parts.append((f"  上下文 {tokens_text(tokens)} token", ROLE_PROCESS))
        if cached:
            percent = cached / tokens * 100
            parts.append((f"（命中 {tokens_text(cached)} · {percent:.0f}%）", ROLE_RULE))
    return seg(*parts)


def _thinking_lines(state: ViewState, message: dict[str, Any],
                    reasoning: str) -> list[Line]:
    """思维链那一段。**默认折叠成一行**（决策 17）。

    折叠那行的字符数由这里 `len()` 出来 —— 不让子进程多发一个 `reasoning_chars`：
    那是同一份事实的第二个来源，而两侧对"一个字符"的口径未必一致（emoji、代理对），
    一个"字符数对不上"的 bug 查起来毫无价值。
    """
    run_id = message.get("run_id", "")
    expanded = state.thinking.get(run_id, ("", False))[1]
    state.thinking[run_id] = (reasoning, expanded)
    if expanded:
        head = seg(
            ("  ▾ 思考过程", ROLE_THINK_HEAD),
            ("（展开 · Ctrl+T 收起）", ROLE_RULE),
        )
        return [head, *[quote_line(line) for line in reasoning.splitlines()]]
    return [seg(
        ("  ▸ 思考过程", ROLE_THINK_HEAD),
        (f"（{len(reasoning)} 字符 · Ctrl+T 展开）", ROLE_RULE),
    )]


def _risk_suffix(state: ViewState, tool: str) -> list[tuple[str, str]]:
    """工具行的风险尾巴。**LOW 什么都不加**（决策 3）。

    不给 low 上色有两个理由，而且第二个更要紧：`risk=low` 的工具占多数，全上色
    等于没有色；而**不给它加文字**是因为"低风险"这个词在一个一切正常的界面上
    只是噪声 —— 需要看到它的是异常，不是常态。
    """
    risk = state.tool_risks.get(tool, "")
    if risk == "high":
        return [("   HIGH 风险", ROLE_RISK_HIGH)]
    if risk == "medium":
        return [("   MEDIUM 风险", ROLE_RISK_MEDIUM)]
    return []


def _tool_call_line(state: ViewState, index: Any, tool: str, arguments: str) -> Line:
    at = f"[{index + 1}] " if isinstance(index, int) else ""
    parts: list[tuple[str, str]] = [
        ("  → ", ROLE_PROCESS),
        (at, ROLE_RULE),
        (tool, ROLE_TOOL),
        (f"({arguments})", ROLE_RULE),
    ]
    parts.extend(_risk_suffix(state, tool))
    return seg(*parts)


def _tool_result_line(index: Any, tool: str, message: dict[str, Any]) -> Line:
    status = message.get("status")
    mark, role = {
        "ok": ("✓", ROLE_RESULT),
        "denied": ("✗", ROLE_DENIED),
        "invalid_args": ("✗", ROLE_DENIED),
    }.get(status, ("!", ROLE_WARN))
    at = f"[{index + 1}] " if isinstance(index, int) else ""
    return seg(
        ("  ← ", ROLE_PROCESS),
        (at, ROLE_RULE),
        (f"{mark} ", role),
        (f"{count_text(message.get('chars', 0))} 字符", ROLE_PROCESS),
        (f"   {ms_text(message.get('duration_ms'))}", ROLE_RULE),
    )


def _permission_line(message: dict[str, Any]) -> Line:
    """`· 权限 shell → 批准（你看了 2.4s · 已记住 python -m pytest）`。"""
    outcome = message.get("outcome", "")
    extras: list[str] = []
    if message.get("rule"):
        extras.append(f"命中规则 {' '.join(message['rule'])}")
    if message.get("remembered"):
        extras.append(f"已记住 {'、'.join(message['remembered'])}")
    if message.get("waited_ms"):
        extras.append(f"你看了 {ms_text(message['waited_ms'])}")
    tail = f"（{' · '.join(extras)}）" if extras else ""
    role = ROLE_WARN if outcome in ("user_denied", "policy_denied", "no_asker") \
        else ROLE_PROCESS
    return seg(
        ("  · 权限 ", ROLE_PROCESS),
        (str(message.get("tool", "")), ROLE_TOOL),
        (" → ", ROLE_PROCESS),
        (OUTCOME_TEXT.get(outcome, outcome or "?"), role),
        (tail, ROLE_RULE),
    )


def _finish_turn(state: ViewState, message: dict[str, Any]) -> list[Line]:
    """`run_finished`：把回合头改成最终形态，并说清结局。"""
    turn = state.turn_for(message.get("run_id", ""))
    reason = message.get("stop_reason", "")
    outcome = STOP_REASON_TEXT.get(reason, reason or "?")
    duration = message.get("duration_ms", 0)
    if turn is not None:
        turn.finished = True
        turn.duration_ms = duration
        turn.outcome = outcome
    index = turn.index if turn is not None else len(state.turns)
    head = seg(
        (f"回合 {index}", ROLE_TURN_END),
        (f"{turn.steps if turn else 0} 步 · {ms_text(duration)} · {outcome}",
         ROLE_RULE),
    )
    if reason == "max_steps":
        # **必须和 answered 长得不一样。** 这是 `StepLimitExceeded` 那个类存在的
        # 全部理由：不许让人分不清"答完了"和"被砍断了"。
        return [head, Line(
            "  ! 步数用尽，这一轮**没有**收尾 —— 会话是好的，可以接着跑。", ROLE_WARN)]
    if reason == "cancelled":
        return [head, Line(
            "  ! 已按你的要求停下（停在两步之间，会话是完好的）。", ROLE_WARN)]
    return [head]


def notice_is_redundant(code: str) -> bool:
    """这条 `notice` 是不是"左栏已经常驻显示着"的那一类。

    `permissions` / `skills` / `todos` 三个 code 说的正是上下文栏那三块 —— 启动时
    再把它们抄进会话流，只会让空态看起来像一堆日志（设计稿 F2 里一条都没有）。
    **按 code 分流而不是按文字猜**：`init.notices` 的 `code` 就是为这件事准备的
    （协议文档里写明它是"机器认的类别"）。

    剩下的照样要显示：`mcp` 那条"忽略了工作区里的 mcp.json"、`web` 那条缺密钥、
    `autopilot` 那条警告，**它们没有别的出口** —— 丢进左栏就等于把它们藏起来。
    """
    return code in ("permissions", "skills", "todos")


def waiting_line(request: dict[str, Any]) -> Line:
    """审批请求到了 → 会话流里那一行：**先说清"我在等你"，再说清有哪些键**。

    它不是装饰，而是那个模态层的**回声**：面板盖住之后，会话流里得留下"这里停过一次"
    的痕迹（面板关掉、你回头翻记录时，那一轮为什么断在那儿就是靠这一行解释的）。
    设计稿 F1 里那一行也在同一个位置。

    **只列后端真的提供了的键**（`t` 要有 `remember_hint`、`a` 要有 `allow_trust_all`）：
    面板上的按钮是条件渲染的，这一行也是 —— 两边不一致的话，用户会照着一个不存在的
    键去按。
    """
    parts: list[tuple[str, str]] = [
        ("  · 等待你的批准", ROLE_WAITING),
        ("   [y] 允许", ROLE_RULE),
        ("   [n] 拒绝", ROLE_RULE),
    ]
    if request.get("remember_hint"):
        parts.append(("   [t] 总是允许", ROLE_RULE))
    if request.get("allow_trust_all") and request.get("trust_all_hint"):
        parts.append(("   [a] 都允许", ROLE_RULE))
    parts.append(("   [Esc] 拒绝", ROLE_RULE))
    text = "".join(chunk for chunk, _role in parts)
    return Line(text, ROLE_WAITING, list(parts))


def render_ui_answer(state: ViewState, message: dict[str, Any]) -> list[Line]:
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
    lines = answer.splitlines() or [answer]
    out = [seg(("  ● ", ROLE_ANSWER), (lines[0], ROLE_ANSWER))]
    out.extend(Line(f"    {line}", ROLE_ANSWER) for line in lines[1:])
    return out


def apply_state(state: ViewState, message: dict[str, Any]) -> None:
    """`t:"ui"` 那条 `state` 快照 → 显示状态。**只有更新，没有输出。**

    它是**面板数据**（左栏那几块），不进对话流：任务列表每更新一次就在流里插一段，
    会把"你问的 + 它答的"冲稀。设计稿把这块放进常驻的左栏，正是为了这个。
    """
    for key in ("todos", "skills", "risk_scope"):
        if key in message:
            setattr(state, key, [dict(item) for item in message[key] or []])
    for key in ("granted_tools", "granted_prefixes", "denied_tools"):
        if key in message:
            setattr(state, key, list(message[key] or []))
    for key in ("messages", "steps"):
        if key in message:
            setattr(state, key, message[key] or 0)
    # **可用清单只在开场那一条里**（目录扫描有代价），所以后来的快照没有它时
    # 要保住已经拿到的那一份 —— 直接覆盖会让"全部技能"那个弹层过一会儿就空了。
    if "skill_catalog" in message:
        state.skill_catalog = [dict(item) for item in message["skill_catalog"] or []]


# --- 上下文栏（左栏） ----------------------------------------------------------

def rail_blocks(state: ViewState) -> list[tuple[str, str, list[Line]]]:
    """左栏四块：`(标题, 右侧计数, 行)`。

    四块的数据来源在设计稿的推导表里写死了：任务来自 `todo_write`、技能来自
    `load_skill`、权限来自 `init.permissions` + `PermissionPolicy`、会话来自
    `Session` + 审计。**它们此前只有"另开一个终端"的出口**（`--skills` /
    `--audit` / `--list`），放进栏里之后"agent 为什么这么做""我现在放行了什么"
    变成常驻可见，而不是翻日志考古。
    """
    return [
        _todo_block(state),
        _skill_block(state),
        _permission_block(state),
        _session_block(state),
    ]


def _todo_block(state: ViewState) -> tuple[str, str, list[Line]]:
    todos = state.todos
    if not todos:
        return ("任务", "", [Line("还没有任务", ROLE_RULE),
                             Line("agent 调用 todo_write 后出现在这里", ROLE_RULE)])
    done = sum(1 for item in todos if item.get("status") == "completed")
    lines = [_bar(done, len(todos))]
    for item in todos:
        mark = TODO_MARK.get(item.get("status", ""), "·")
        role = ROLE_ANSWER if item.get("status") == "completed" else ROLE_PROCESS
        if item.get("status") == "in_progress":
            role = ROLE_WAITING
        lines.append(seg((f"{mark} ", role), (item.get("content", ""), ROLE_PROCESS)))
    return ("任务", f"{done} / {len(todos)}", lines)


def _bar(done: int, total: int, width: int = 20) -> Line:
    """进度条：一格一个任务，超过 `width` 就压缩。

    **按任务数出格，不是固定十格**：F1 那张图里五条任务就是五格 —— 因为这个条
    回答的问题是"还剩几条"，而固定格数会把这个问题翻译成一道除法。
    """
    cells = min(total, width)
    filled = round(done * cells / total) if total else 0
    text = "▰" * filled + "▱" * (cells - filled)
    if total > width:
        text += f" +{total - width}"
    return Line(text, ROLE_ANSWER if done else ROLE_RULE)


def _skill_block(state: ViewState) -> tuple[str, str, list[Line]]:
    if not state.skills:
        return ("已加载技能", "0", [Line("还没有加载技能", ROLE_RULE),
                                    Line("load_skill 读过的会一直生效", ROLE_RULE)])
    lines = [Line(entry.get("name", "?"), ROLE_SKILL) for entry in state.skills]
    return ("已加载技能", str(len(state.skills)), lines)


def _permission_block(state: ViewState) -> tuple[str, str, list[Line]]:
    """权限范围：**三档的处置 + 已经记住的那几条**。

    "low 自动放行 / medium 询问 / high 询问"这三行里的处置**由 runtime 算**
    （`risk_scope`），界面不认识"默认只有 low"这件事 —— 那是 config 的知识。
    """
    lines: list[Line] = []
    label = {"auto": "自动放行", "ask": "询问"}
    for item in state.risk_scope:
        risk = item.get("risk", "")
        disposition = item.get("disposition", "")
        # 只给**中高风险**着色（决策 3）：`low` 是常态，全上色等于没有色。
        if risk == "high":
            role = ROLE_RISK_HIGH
        elif risk == "medium":
            role = ROLE_RISK_MEDIUM
        else:
            role = ROLE_RULE
        lines.append(seg(
            (f"{risk:<7}", ROLE_PROCESS),
            (label.get(disposition, disposition), role),
        ))
    if state.granted_tools:
        lines.append(Line("点名免问 " + "、".join(state.granted_tools), ROLE_WARN))
    if state.granted_prefixes:
        lines.append(Line("命令规则 " + "、".join(state.granted_prefixes), ROLE_WARN))
    if state.denied_tools:
        lines.append(Line("直接拒绝 " + "、".join(state.denied_tools), ROLE_DENIED))
    if not lines:
        lines.append(Line("按等级（runtime 没报范围）", ROLE_RULE))
    return ("权限范围", "", lines)


def _session_block(state: ViewState) -> tuple[str, str, list[Line]]:
    if not state.session_id:
        return ("本次会话", "", [Line("还没有会话", ROLE_RULE),
                                 Line("说出第一句话之后才有文件", ROLE_RULE)])
    lines = [Line(state.session_id, ROLE_PROCESS)]
    if state.messages:
        lines.append(Line(f"{state.messages} 条消息 · {state.steps} 步", ROLE_RULE))
    if state.prompt_tokens is not None:
        used = tokens_text(state.prompt_tokens)
        if state.context_tokens:
            percent = state.prompt_tokens / state.context_tokens * 100
            lines.append(Line(
                f"上下文 {used} / {tokens_text(state.context_tokens)}"
                f"（{percent:.1f}%）", ROLE_RULE))
        else:
            lines.append(Line(f"上下文 {used}", ROLE_RULE))
    lines.append(Line(f"审计 {state.audit_dir_short()}", ROLE_RULE))
    return ("本次会话", "", lines)


def rail_summary(state: ViewState) -> str:
    """上下文栏收起时那一行摘要（F5 的窄屏形态）。

    它必须**说清收起之后少了什么**：任务几条、技能几个、权限是什么档 ——
    否则"收起"就等于"看不见"，而左栏存在的全部理由就是让它们常驻可见。
    """
    parts = ["Ctrl+B 展开上下文栏"]
    if state.todos:
        done = sum(1 for item in state.todos if item.get("status") == "completed")
        parts.append(f"{done}/{len(state.todos)} 个任务")
    if state.skills:
        parts.append(f"{len(state.skills)} 个技能")
    asking = [item.get("risk", "") for item in state.risk_scope
              if item.get("disposition") == "ask"]
    if asking:
        parts.append("、".join(asking) + " 询问")
    elif state.risk_scope:
        parts.append("全部自动放行")
    return " · ".join(parts)


def should_auto_open(state: ViewState, width: int) -> bool:
    """上下文栏该不该自动展开（决策 1）。

    **默认收起**（贴 Claude Code 的克制），而"检测到有任务/技能时自动展开"是那句话
    的另一半 —— 一条被维护的任务列表意味着这个会话值得看全局。两个附带条件：

      * 窄屏（< 120 列）一律不展开：栏宽 32 列在 80 列的终端里要吃掉 40%，
        那是灾难性的（F5 给的就是那种终端的降级形态）；
      * 用户按过 Ctrl+B 之后**不再自动开合**（`rail_pinned`）—— 一次明确的操作
        不该被下一次状态更新推翻。
    """
    if state.rail_pinned:
        return state.rail_open
    return width >= NARROW_COLUMNS and bool(state.todos or state.skills)


# --- 命令面板 ------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """一条 `/` 命令。**它是数据，不是分支**：面板按它渲染、按它执行。"""

    name: str
    hint: str
    takes_arg: bool = False


# 命令集。v1 那六条是决策 15 定下来的，**顺序也照设计稿 F2 的面板**；
# 后面三条是这一版新增的（面板、技能清单、配色），加在末尾而不是插在中间 ——
# 那六条的位置是用户已经见过的肌肉记忆。
#
# **`/list` 在第二期被去掉了**（设计决策，见 doc/TUI-design.md 13.3）：它和
# "`/resume` 不带参数"说的是同一件事，而两条命令指向同一个出口时，人会先猜哪一条
# 才是对的。留 `/resume` 一条，它自己负责列出候选。
COMMANDS: tuple[Command, ...] = (
    Command("/new", "换一个新会话（立刻生效，不用退出）"),
    Command("/resume", "换一个会话：不带参数从列表里挑，带 id 直接切", True),
    Command("/audit", "审计日志在哪"),
    Command("/exit", "退出"),
    Command("/help", "命令列表"),
    Command("/theme", "换配色（14 套，不带参数就看清单）", True),
    Command("/skills", "看全部技能（可用的 + 已加载的）"),
)


def session_row(item: dict[str, Any], *, conflict: bool = False) -> Line:
    """会话清单里的一行：`20250101-120000   12 条消息 · 7 步   第一句话…`

    三段各自解决一个"选不出来"的问题：**id** 是切过去要用的东西，**条数/步数**
    说明它有多长（"那个聊了很久的"），**预览**说明它是哪一次对话（会话 id 是时间戳，
    人对不上号）。任务进度跟在最后 —— 它是"哪个会话还剩着活"唯一看得见的地方。

    `conflict=True` 给当前会话那一行加一枚记号：不带参数调 `/resume` 时，候选里
    一定有正在用的这一个，而不标出来的话"点进去什么都没发生"看起来像坏了。
    """
    name = str(item.get("session_id", ""))
    count = int(item.get("messages") or 0)
    steps = int(item.get("steps") or 0)
    preview = str(item.get("preview") or "") or "（还没说过话）"
    todos = str(item.get("todos") or "")
    return Line(
        f"  {'● ' if conflict else '  '}{name:<22} "
        f"{count:>3} 条消息 · {steps:>3} 步   {preview}"
        + (f"   [任务 {todos}]" if todos else ""),
        ROLE_WAITING if conflict else ROLE_PROCESS,
    )


def filter_commands(query: str) -> list[Command]:
    """面板里的候选。`/` → 全部；`/re` → 名字以 `re` 开头的那些。

    **只按名字前缀匹配**，不做模糊搜索：命令一共八条，而模糊匹配会让"我打错了"
    和"它猜对了"长得一样 —— 一个按下去不是你想的那条命令的面板比没有面板更坏。
    """
    text = query.strip().lstrip("/").lower()
    if not text:
        return list(COMMANDS)
    return [command for command in COMMANDS if command.name[1:].startswith(text)]
