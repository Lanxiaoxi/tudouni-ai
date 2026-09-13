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

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# `cell_len` 是**这个文件里唯一的"宽度"口径**：它数的是终端列数（一个汉字两列），
# 而 `len()` 数字符。欢迎屏上"把一行字居中"这件事只有按列数算才是对的。
from rich.cells import cell_len

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


# --- 欢迎屏（空态）的那几段文字 ------------------------------------------------

def center_text(text: str, width: int) -> str:
    """把一段文字在 `width` 列里居中。放不下就**原样返回**（让调用方/边框去裁）。

    **按显示列数算，不按 `len()`**：`center()` 用的是 `len`，而它数的是字符 ——
    一个汉字占两列，于是"欢迎回来 Meaghan"这种中英混排的居中会歪一格（实测：
    方块标看着比它下面那行字偏了半格，而那种歪法很难说清是哪里不对）。
    宽度用 `rich.cells.cell_len` 量，那是这套代码里唯一的"列数"口径。
    """
    if width <= 0:
        return text
    span = cell_len(text)
    if span >= width:
        return text
    left = (width - span) // 2
    return " " * left + text


def time_ago(seconds: float) -> str:
    """`刚刚` / `12分钟前` / `3小时前` / `2天前` / `2026-09-01`。

    **两级单位就够**：欢迎屏那一行要说的是"这是不是刚才那个"，而"3小时前"
    和"3小时12分前"在判断这件事上没有区别 —— 多出来的两个字只是把会话 id 挤掉。

    超过一周换成**日期**：那时候"多少天前"已经不再让人想起是哪一次对话了，日期才行。
    """
    if seconds < 0:
        # 时钟回拨（或文件的 mtime 来自另一台机器）→ 当成"刚刚"，别显示"-3分钟前"。
        seconds = 0
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}小时前"
    if seconds < 7 * 86_400:
        return f"{int(seconds // 86_400)}天前"
    # 一周以前用**日期**。`time_ago` 拿到的是一个时长，所以这里要回推一个时刻 ——
    # 这一档说的是"哪天"（相对时间到这儿已经不说明问题了），那几个小时的时区/夏令时
    # 误差看不出来。
    return datetime.fromtimestamp(time.time() - seconds).strftime("%Y-%m-%d")


# 箴言。**它是欢迎屏右侧那一小块的全部内容**（设计上对应别家 CLI 的"What's new"）：
# 一个每 15 天就有内容过期的区块，在天天用的工具里只会变成噪声，而一句读得进去的
# 话至少每天说一次"这一屏是给人看的"。
#
# 规矩：**一句话，别超一行**（窄屏会折成两行，那也还行）。不要写项目自夸，也不要
# 写需要上下文才懂的话 —— 它是开机问候，不是公告。
MOTTOS: tuple[str, ...] = (
    "先想清楚要什么，再动手。",
    "能写下判据的，才算想明白了。",
    "改一处，就只改那一处。",
    "看不懂的代码，先别改。",
    "小步走，常回头。",
    "把话说给下一个读它的人听。",
    "失败要响，别悄悄吞掉。",
    "重复第三遍的时候，就该抽出来了。",
    "先让它对，再让它快。",
    "名字错了，代码就跟着错。",
    "留下的注释要解释为什么，不是是什么。",
    "没跑过的东西，不算做完。",
    "接口比实现活得久。",
    "删掉一行代码，和写一行一样值钱。",
    "今天的决定，明天的默认值。",
)


def motto_of_day(day: int | None = None) -> str:
    """今天的箴言：**按日期轮换**（同一天进来看到的是同一句）。

    `day` 是"从 epoch 起第几天"（`day=None` 就现取本地日期），做成参数是为了让它
    能在单测里被钉住 —— 这个函数里唯一会变的东西就是"今天几号"，而它恰好是最不
    该混进断言的那一样。
    """
    if day is None:
        day = int(time.time() // 86_400)
    return MOTTOS[day % len(MOTTOS)]


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
    # 这条模型在哪条路由上（`deepseek` / `acme` …），来自 `init.model_provider`。
    #
    # **和 `model` 分开存**：两条路由可以有同名模型，而"请求发到哪儿"在账单上、
    # 在合规上都是另一件事 —— 界面上只写一个模型名答不出它到底在哪跑。
    provider: str = ""
    # 思考模式那两个旋钮（`/thinking` `/effort`）。**它们只有 `ui state` 一个来源** ——
    # 和 autopilot 同一条规矩：界面按 runtime 说显示，不在自己发请求的时候就改。
    #
    # 字段名带 `_on` 后缀是**必须的**，不是审美：这个类里已经有一个 `thinking`，
    # 它是"这一个回合的思维链正文"（`Ctrl+T` 折叠那一块）—— 两个 `thinking` 撞上之后
    # `reset_for_session` 会去 `.clear()` 一个布尔，而报错点在换会话那条路上。
    thinking_on: bool = True
    effort: str = "high"
    # 可选档位，来自 `init.effort_levels`。**它是 runtime 给的，界面不写死** ——
    # 写死的话，端点加一档就得改两个地方，而漏改的那一处只表现为"这一档选不了"。
    # 也和"前端不许 import runtime 内部"那条规矩一致（决策 18）。
    effort_levels: tuple[str, ...] = ()
    max_steps: int = 0
    workspace: str = ""
    audit_path: str = ""
    # 上下文窗口（分母），来自 `init.context_tokens`。None = 只报用量、不报占比。
    context_tokens: int | None = None
    resumed: bool = False
    # 非默认权限那一行（决策 14：runtime 发什么显示什么）。
    permissions: dict[str, Any] = field(default_factory=dict)
    # 已保存的会话清单（`sessions` 那条消息的 `items`，最新在前）。
    #
    # **它是欢迎屏右栏那一段的数据，而且它属于界面而不是会话** —— 所以
    # `reset_for_session()` 不清它（换会话不改变"硬盘上有哪些会话"）。启动时
    # 向 runtime 要一次（`TuiApp._ask_for_recent_sessions`），到了就重画欢迎屏。
    recent_sessions: list[dict[str, Any]] = field(default_factory=list)
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
    # 后台任务（`shell_background` 起的那几条）。每项形如
    # `{"id", "command", "state": "running|uncollected|done|killed", "seconds", "exit_code"}`。
    #
    # **它和上面那几块有一个根本区别：它对应的是活着的进程。** 任务列表过期只是信息旧，
    # 而后台任务过期意味着"我以为已经收掉的服务还在占着端口"。所以它除了左栏那一块，
    # 还有一个常驻的状态栏徽标（`jobs_badge`）—— 收起上下文栏时那是唯一的出口。
    jobs: list[dict[str, Any]] = field(default_factory=list)
    # 这个会话的 system 消息里那份 AGENT.md 的去向（`ui_state` 从
    # `session.metadata` 读出来的报告）：每项形如
    # `{"path": "AGENT.md", "lines": 12, "status": "loaded" | "failed", "problem": "…"}`。
    #
    # **它属于会话，不属于进程** —— 恢复一个旧会话时它说的必须是那份旧提示词里的事
    # （见 `agents_md.Report`）。`failed` 那几条也在这里，而不是只当成一条 notice：
    # notice 会随对话滚走，而"我那份 AGENT.md 为什么没生效"是随时会想再看一眼的问题。
    agents_md: list[dict[str, Any]] = field(default_factory=list)
    messages: int = 0
    steps: int = 0
    risk_scope: list[dict[str, str]] = field(default_factory=list)
    granted_tools: list[str] = field(default_factory=list)
    granted_prefixes: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    # 现在是不是 autopilot（`--autopilot` 或 `/autopilot`）。**它只有 `ui state`
    # 一个来源** —— 界面按 runtime 说显示，不在自己发请求的时候就改：那会让"写着开、
    # 其实还在逐条问你"变成可能，而这一格的意思恰恰是"接下来会不会问你"。
    #
    # 它**不属于某一个会话**（它是整个进程的模式，换会话时 bootstrap 带着它走），
    # 所以 `reset_for_session()` 不清它。
    autopilot: bool = False
    # `/model` 那张清单（`init.model_catalog`）：`[{"id", "label", "window",
    # "summary", "note", "current"}]` + 认下的旧名字。
    #
    # **它是 runtime 给的数据，界面不写死模型名** —— 写死的话，加一个模型要改两个
    # 仓库里的地方，而漏改的那一处（界面）只表现为"这个模型选不了"。
    # 它和 `recent_sessions` 同一条规矩：属于进程（目录不随会话变），
    # `reset_for_session()` 不清它。
    model_catalog: list[dict[str, Any]] = field(default_factory=list)
    model_aliases: list[dict[str, Any]] = field(default_factory=list)
    # `/status` / `/tools` 的回包。**它们不是常驻状态**：来了就渲染一次进会话流，
    # 所以这里只存"最近一次问答"。`apply_state` 也读后者：换了模型之后
    # `ui(state)` 会把它一起送回来，于是状态栏那一行的分母跟着变。
    status: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    # 用户刚发出去的那句话。**回显要用它而不是事件里那份预览** ——
    # `run_started.user_input` 是审计的 200 字符预览，而用户敲的字界面本来就有。
    pending_input: str = ""
    # 上下文栏开着吗。**默认收起**（决策 1）：任务列表**从无到有**时自动展开一次
    # （决策 26，见 `should_auto_open`）。
    rail_open: bool = False
    # 用户手动按过 Ctrl+B 之后就不再自动开合 —— 一次明确的操作不该被下一次更新推翻。
    rail_pinned: bool = False
    # "上一帧已经有任务列表了吗"。它让 `should_auto_open` 把"任务列表产生"认成一次
    # **边沿事件**（顶开一次就完），而不是一个持续成立的电平（那会让 Ctrl+B 收起失效）。
    rail_todos_seen: bool = False

    # --- 流式（`t:"delta"` / `t:"delta_reset"`）-------------------------------
    #
    # 这三样是**这一轮**的累计：正文、思考链、跑到第几步。它们决定了回合收尾时
    # "还要不要再画一份完整答案"（见 `streamed_answer`）。
    stream_text: str = ""
    stream_reasoning: str = ""
    stream_step: int = 0
    # 这份累计属于哪个 run / 哪个会话。**两条判据都要**：只有 run_id 的话，
    # 换会话之后新会话的 run_id 恰好重名（理论上）就会把旧累计认成自己的；
    # 只有会话的话，同一个会话里连开两轮会互相串。
    stream_run_id: str = ""
    stream_session: str = ""
    # `run_finished` 那条 `ui` 处理过了吗。它只防"同一条消息被重放时画两遍"。
    stream_answered: bool = False
    # **这次运行开不开流式**，来自 `init.stream`（运行期事实，不是界面的偏好）。
    #
    # 它在界面上的用处只有一个：**收尾时那行提示说不说"答案只出现了半截"**。
    # 判据不能是"收到过 delta 没有"—— 一次一个字都没吐的流式回合（比如模型直接
    # 调工具）没有 delta，而它并不需要任何提示。
    stream_enabled: bool = False

    def reset_for_session(self) -> None:
        """把**属于某一个会话**的东西全清掉，只留下界面自己的开关。

        换会话（`/new` / `/resume`）时调它。**这份清单必须完整**，因为"漏了哪个字段"
        的症状全都很难看而且各不相同：

          * 漏 `turns` / `answers` / `thinking` / `calls` —— 新会话里混着旧会话的回合，
            而且 `Ctrl+T` 会去展开一个已经不在的 run（它按 run_id 查，查得到旧的那份）；
          * 漏 `todos` / `skills` / `skill_catalog` —— 左栏显示**上一个会话**的任务列表。
            这一条最坏：它看起来完全正常，而用户会以为那些任务是现在这个会话的；
          * 漏 `jobs` —— 面板上留着上一个会话的后台任务，而**它们的进程已经在上一个
            会话收尾时被杀掉了**（`Runtime.close()` 收的）。于是界面会一直显示一个
            早就不存在的服务"在跑"，而用户会照着它去 debug 一个假问题；
          * 漏 `agent` —— 状态栏按上一个会话的 phase 显示"正在跑"或"已答"，而新会话
            一步都没走（协议那边是干净的，所以这个"正在跑"永远不会结束）；
          * 漏 `pending_input` —— 上一句话贴到新会话的第一个回合头上；
          * 漏 `stream_*` —— 新会话收到的**第一块 delta** 会被当成本会话累计的一部分
            （判据是 `stream_session != session_id`，而它只在"会话变了"时才清），
            于是那一轮的正文会和上一个会话的最后一段拼在一起。

        **rail_open / rail_pinned 不清**：那是"我要不要看左栏"，和聊的是哪个会话无关
        —— 换一次会话就把用户手动收起的栏顶开，是最容易被当成 bug 的那种"贴心"。

        **`autopilot` 也不清**（同样在上面那段之外）：它是整个进程的模式，而且
        runtime 那边换会话时是**带着它**装的（`bootstrap.autopilot`）—— 这里清成
        False 会和 runtime 分家，症状是"换了个会话，灯灭了但工具照样不问"。
        """
        self.agent = agent_state.initial()
        self.answers.clear()
        self.thinking.clear()
        self.turns.clear()
        self.calls.clear()
        self.todos = []
        self.skills = []
        self.skill_catalog = []
        self.jobs = []
        self.agents_md = []
        self.risk_scope = []
        self.granted_tools = []
        self.granted_prefixes = []
        self.denied_tools = []
        self.messages = 0
        self.steps = 0
        self.prompt_tokens = None
        self.cached_tokens = None
        self.pending_input = ""
        # 流式累计属于某一个会话，所以要清。**`stream_session` 也清成空串**：
        # 留着上一个会话的 id 会让新会话第一块 delta 撞上"会话变了"那条判据 ——
        # 那是对的，但只在换会话时对；这里直接清干净，让判据只剩 run_id 一条路。
        self.stream_text = ""
        self.stream_reasoning = ""
        self.stream_step = 0
        self.stream_run_id = ""
        self.stream_session = ""
        self.stream_answered = False

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

    def autopilot_badge(self, compact: bool = False) -> Line:
        """状态栏左边那枚 autopilot 指示灯：`自动放行 开` / `自动放行 关`。

        **两个状态都要显示**，不做成"开着才显示"：那样"这一格空着"既可能是关掉了、
        也可能是没画出来，而这一格的语义恰恰是"接下来还会不会问你" —— 它不允许有
        歧义。用词跟着 `OUTCOME_TEXT` 里那条（`autopilot` → 「自动放行」），
        所以它和审批结果里那个说法是同一句话。

        颜色：开着是 `warn`（它意味着工具会在没有人点头的情况下执行），关着是
        最暗那档 —— 常态不该抢眼。

        `compact=True`（窄屏）把词缩成两个字：状态栏右边是 `width: auto`，多出来的
        每一列都从**左段**身上扣，而左段被裁成半句正是 F5 那次踩过的坑
        （实测：显示成 `● 要调用 edit_file（第`，那句话的意思整个没了）。
        """
        word = "放行" if compact else "自动放行"
        if self.autopilot:
            return Line(f"{word} 开", ROLE_WARN)
        return Line(f"{word} 关", ROLE_RULE)

    def jobs_badge(self, compact: bool = False) -> Line | None:
        """状态栏那一枚后台任务徽标；**一件都不悬着时返回 None**（一格都不占）。

        它为什么必须存在，而不是只靠左栏那一块：**上下文栏默认是收起的**，而收起时
        宽屏上唯一还提这件事的地方就是这里（窄屏那一行摘要是另一条，见 `rail_summary`）。
        后台任务和任务列表不一样 —— 任务列表过期只是信息旧，而后台任务是一条**还在
        占着端口的进程**，它不允许"收起左栏就等于看不见"。

        ## 两个状态用两种颜色，而且说的不是同一件事

          * 只有"在跑"→ 安静那档：有东西在跑是**正常状态**，不值得一路喊；
          * 有"结果还没收"→ `warn`：那一档的含义是**你（或模型）还有一步没做**，
            而这一步不做的话，那条命令成没成永远没有答案（见 `jobs.py` 开头第 3 条）。

        `compact=True`（窄屏）只留条数：状态栏右边是 `width: auto`，多出来的每一列
        都从左段身上扣，而左段被裁成半句正是 F5 那次踩过的坑。
        """
        if not self.jobs:
            return None
        outstanding = [job for job in self.jobs
                       if str(job.get("state", "")) in ("running", "uncollected")]
        if not outstanding:
            # 全都收过了：留一枚安静的"历史"记号，而不是整格消失 —— 消失之后
            # "起过又收干净了"和"从来没起过"长得一模一样，而前者是值得知道的事实。
            return Line(f"后台 {len(self.jobs)} 已收", ROLE_RULE)
        uncollected = sum(1 for job in self.jobs
                          if str(job.get("state", "")) == "uncollected")
        text = f"后台 {len(outstanding)}"
        if uncollected and not compact:
            text += f" · {uncollected} 条待收"
        return Line(text, ROLE_WARN if uncollected else ROLE_PROCESS)

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
                # **流式已经铺过一遍的，不要再画一个折叠行**（见 `_thinking_lines`）。
                # 判据是"这一轮的思考过程是不是从 delta 来的"，而那个事实记在
                # `stream_reasoning` 里 —— 它是 delta 累计出来的，不是猜的。
                live = bool(state.stream_run_id == message.get("run_id", "")
                            and state.stream_reasoning)
                out.extend(_thinking_lines(state, message, reasoning, live_block=live))

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


def folded_thinking(text: str) -> Line:
    """折叠形态的那一行：`  ▸ 思考过程（401 字符 · Ctrl+T 展开）`。

    **四个地方要用它**，所以它必须只有一个来源（实测踩过：折叠这一行在
    `_thinking_lines`、`TurnBlock.toggle_thinking` 的收起分支、以及流式收尾的
    `close_stream` 里各写了一遍，于是展开再折叠之后字数口径能不能对上全靠运气）：

      * 事件到达时的首屏（`_thinking_lines`，非流式那条路）；
      * `Ctrl+T` 把铺开的思考收起来时；
      * `Ctrl+T` 把折叠的思考展开时那个块头（措辞不同，见 `expanded_thinking_head`）；
      * 流式那一轮收尾时（`TurnBlock.close_stream`）。

    字符数由调用方 `len()` 出来 —— 不让子进程多发一个 `reasoning_chars`：
    那是同一份事实的第二个来源，而两侧对"一个字符"的口径未必一致（emoji、代理对），
    一个"字符数对不上"的 bug 查起来毫无价值。
    """
    return seg(
        ("  ▸ 思考过程", ROLE_THINK_HEAD),
        (f"（{len(text)} 字符 · Ctrl+T 展开）", ROLE_RULE),
    )


def expanded_thinking_head() -> Line:
    """展开形态的块头：`  ▾ 思考过程（展开 · Ctrl+T 收起）`。"""
    return seg(
        ("  ▾ 思考过程", ROLE_THINK_HEAD),
        ("（展开 · Ctrl+T 收起）", ROLE_RULE),
    )


def thinking_body(text: str) -> list[Line]:
    """展开时那一段正文的每一行（带引用竖线）。

    **换行先压平**（`stream_chunk_text` 那条规矩）：流式收到的思考链是一块一个词、
    每块自带换行，逐块存下来的话展开时会是**一个词一行**（实测：401 字符的思考过程
    竖着排了 100 多行）。压平之后它是一段随宽度重排的 prose —— 那是思考过程该有的
    样子。代价是展开后看到的换行和 provider 给的不一样（审计里那份是原样的）。
    """
    flat = stream_chunk_text("think", text)
    return [quote_line(line) for line in (flat.splitlines() or [""])]


def record_thinking(state: ViewState, run_id: str, reasoning: str) -> None:
    """把完整的一份思考过程记下来（`Ctrl+T` 要读它）。**记一次，两处调用。**

    两个来路：`model_call` 那条事件（审计里那一份，模型想完之后才到），
    以及流式开着时的收尾（`ui(run_finished)` —— 那时候正文是从 delta 来的，
    但**完整的一份仍然要从这里记**，否则 `Ctrl+T` 展开不出东西）。

    `expanded` 保留用户当前的选择：展开状态是**界面的**，不该因为新数据到了就翻回去。
    """
    if not reasoning:
        return
    _text, expanded = state.thinking.get(run_id, ("", False))
    state.thinking[run_id] = (reasoning, expanded)


def _thinking_lines(state: ViewState, message: dict[str, Any],
                    reasoning: str, *, live_block: bool = False) -> list[Line]:
    """思维链那一段。**默认折叠成一行**（决策 17）。

    折叠那行的字符数由这里 `len()` 出来 —— 不让子进程多发一个 `reasoning_chars`：
    那是同一份事实的第二个来源，而两侧对"一个字符"的口径未必一致（emoji、代理对），
    一个"字符数对不上"的 bug 查起来毫无价值。

    `live_block=True` 表示这一轮的思考过程**已经从 delta 铺在屏幕上了**
    （`stream_lines` 画的），这时候不再画一个折叠行 —— 画了就是同一段内容两遍，
    而且 `Ctrl+T` 会在两个块之间挑错对象。收尾由 `TuiApp._close_live_thinking`
    把那一块收成折叠形态，所以最终屏幕上仍然只有一行，和没开流式时一样。
    """
    run_id = message.get("run_id", "")
    record_thinking(state, run_id, reasoning)
    if live_block:
        return []
    expanded = state.thinking.get(run_id, ("", False))[1]
    if expanded:
        return [expanded_thinking_head(), *thinking_body(reasoning)]
    return [folded_thinking(reasoning)]


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

    **`model` 也在里面**（这一条是后加的）："这个会话选的是哪个模型"从此常驻在左栏的
    「本次会话」块里（见 `_session_block` 里那两行）。把同一件事再说一遍进会话流的代价
    不只是多一行 —— 它会随对话滚走，而这一格是"我上次 `/model` 换的那个到底还生效着
    吗"唯一能随时看一眼的地方。

    **`agent_md` 也不在这里**（它读的是工作区那份 AGENT.md，不是左栏那三块）：用户
    问了"启动的时候说一句加载了 xxx/AGENT.md"，而"加载了哪些别人写的说明"和 [技能]
    那条是同一类事实，该在开场看得见 —— 左栏那几块说的是当前会话的**状态**，而这是
    一次**启动事件**。读失败和被截断那两条尤其必须出现在这里：它们是 warn，左栏没有
    任何一块会显示它们。
    """
    return code in ("permissions", "skills", "todos", "model")


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


@dataclass(frozen=True)
class Answer:
    """agent 的正文 —— **原文，不拆成行**。

    它是这个模块里唯一"不变成行"的东西，理由和 `Line` 存在的理由正好相反：
    过程行、工具行是"一行一个说法"的排版，而正文**有自己的语法**（标题、列表、
    表格、代码块）。把它压成一行一段 `str` 就等于把那份语法丢掉 —— 界面于是只能
    原样显示原始 MD（"回复没被渲染"的根因就在这里）。所以这一层只把原文交出去，
    解析交给控件那一侧（`widgets.AnswerBlock`，即 Textual 的 `Markdown`）。

    代价是**它的渲染结果在这一层不可断言**：能测的只有"给什么还什么"和"空答案
    返回 None"。这是刻意的取舍 —— 渲染质量由控件保证，而这一层继续不做 Textual
    的梦（`tests/test_imports.py` 有一条测试盯着这条边界）。
    """

    text: str


def answer_body(state: ViewState, message: dict[str, Any]) -> Answer | None:
    """`t:"ui"` 那条 `run_finished` → 答案正文。**纯函数。**

    审计里没有正文（`Agent.run` 的返回值只交给调用方），所以非流式模式下这是界面
    拿到答案的唯一途径。按 `run_id` 记下来 —— 它和 `event` 那条 `run_finished` 是
    两条消息，**顺序不保证**，所以不许靠到达顺序配对。

    空答案（模型失败）返回 `None`：那时候不该画一个空的答案块。判据留在这里而不是
    留给控件，是因为"要不要画"是判断、"画成什么样"才是渲染 —— 前者这一层能测。
    """
    run_id = message.get("run_id", "")
    answer = message.get("answer") or ""
    state.answers[run_id] = answer
    return Answer(answer) if answer else None


# --- 流式增量 ------------------------------------------------------------------

# `t:"delta"` 的两条通道 → 界面上的两种块。
#
# **按名字映射，不靠"有没有 reasoning 字段"去猜。** 两条通道的内容都是字符串，
# 猜错一次就把思考链当成了答案，而那看起来像模型在自言自语。
_STREAM_KIND = {"text": "answer", "reasoning": "think"}
_STREAM_BODY_ROLE = {"text": ROLE_ANSWER, "reasoning": ROLE_THINK_BODY}
# 每块流式内容前面那一行记号。**正文那一行是这一屏上"答案开始"唯一的标记**
# （非流式那边由 `ui(run_finished)` 那条单独画一次），所以它和 `render_event`
# 里那些行的记号是同一套审美，也放在同一个模块里。
#
# 正文用的是 `ROLE_ANSWER` 而不是另立一个角色：流式正文块本身走 Markdown，
# 这几个字符被包成行内代码（见 `TurnBlock.add_stream`），颜色由 CSS 的
# `.answer MarkdownBlock > .code_inline` 决定 —— 再立一个角色也没人去用。
STREAM_HEAD = {
    "text": ("  ● ", ROLE_ANSWER),
    "reasoning": ("  ▸ 思考过程", ROLE_THINK_HEAD),
}


def stream_delta(state: ViewState, message: dict[str, Any]) -> None:
    """`t:"delta"` → 流式记账。**纯记账，不排版**（排版在 `stream_lines`）。

    记的是**累计正文**，不是"收到过 delta 没有"这个布尔：`run_finished` 那一步要
    拿它和完整答案对一下（见 `streamed_answer`），而"累计"才使得那件事做得成。

    **中途换会话/换轮要清。** 两条判据分开记（会话、run_id）而不是合成一个字段：
    合起来的话，"同一个会话里开了新一轮"和"换到了另一个会话"会看起来一样，
    而它们该做的事不同 —— 前者只清累计，后者连流式开关都要重问（`init.stream`）。
    """
    run_id = message.get("run_id", "")
    if state.stream_session != state.session_id or state.stream_run_id != run_id:
        state.stream_session = state.session_id
        state.stream_run_id = run_id
        state.stream_text = ""
        state.stream_reasoning = ""
        state.stream_answered = False

    # 步号跟着最近一块走：`delta_reset` 按它定点清（见那里的说明）。**累加过就
    # 不再回退** —— 乱序到达的旧块不该把"现在第几步"改回上一步。
    step = message.get("step")
    if isinstance(step, int) and step >= state.stream_step:
        state.stream_step = step

    channel = message.get("channel")
    text = message.get("text") or ""
    if not text:
        return
    if channel == "reasoning":
        state.stream_reasoning += text
    elif channel == "text":
        state.stream_text += text


def stream_lines(message: dict[str, Any]) -> list[tuple[str, Line]]:
    """`t:"delta"` → 往回合流里追加的行。**带块身份**（`("answer", …)` / `("think", …)`）。

    块身份就是控件那侧的块类型（`TurnBlock` 的 `chunks[i]["kind"]`），所以重试/重发
    要作废这一步的正文时，能**定点**清掉它、而不是把整个回合重画一遍（见
    `delta_reset`）。

    **这里不产生"块头"那一行**（`● ` / `▸ 思考过程`）：那一行只该出现一次，而
    "出现过了没有"只有控件知道（它拿着块的清单）。放在这一层的话，每一块都会带上
    一个头，去重就得在这一层维护一份和控件重复的状态。

    **正文块的每一行都原样保留**（`splitlines()`）：它是 Markdown 源文，
    换行是有意义的语法。而**思考链不能那么干** —— 见 `stream_chunk_text`。
    """
    channel = message.get("channel")
    text = message.get("text") or ""
    kind = _STREAM_KIND.get(channel or "")
    if not text or kind is None:
        return []
    role = _STREAM_BODY_ROLE[channel]
    line = Line(stream_chunk_text(kind, text), role)
    return [(kind, line)]


def stream_chunk_text(kind: str, text: str) -> str:
    """一块 delta → 喂给控件的那一行。**两条通道的规矩不同，这是实测踩出来的。**

    * **正文**：原样。它是 Markdown 源文（`# `、列表、代码块都靠换行分隔），
      压平了就不是 Markdown 了；
    * **思考链**：**换行压成空格**。这一条是必须的 —— provider 吐思考链时，
      一块 delta 往往就是一个词（"The" / " user" / " says"），而它们各自还带着
      一个 `\\n`。逐块 `splitlines()` 的结果就是**一个词一行**（实测：界面上
      400 个字符的思考过程竖着排了 100 多行）。

      **但"压平"不等于 `" ".join(text.split())`** —— 那个写法会把词与词之间的空格
      一起吃掉（"The" + " user" 变成 "Theuser"，实测踩过）。分块本身带着它要的
      空格，换行才是那个不该留下的东西：所以这里**只去掉换行本身**，
      词之间那个空格留在原地（下一块开头的空格也要留着 —— 它属于前一个词）。

      也**不能顺手 `strip()`**：那样每块开头的空格都会被吃掉，而"块边界的那个空格"
      正是词与词之间唯一的分隔（实测：删掉它之后是 `Theusersays…`）。

    压平之后它是一段会随宽度重排的 prose —— 思考过程本来就是一段自言自语，
    不是有语法的正文。代价说白：**展开之后看到的换行和 provider 给的不一样**
    （审计里那份是原样的）。这一条值得，因为"一词一行"是没法读的，
    而"少了几个换行"只是排版差异。
    """
    if kind != "think":
        return text
    # 只丢换行，别的一律不动（顺序要紧：先按 `\n` 切、再把各段接起来，
    # 段与段之间的边界上那个空格就自然留在原地了）。
    return "".join(text.split("\n"))



def streamed_answer(state: ViewState, message: dict[str, Any]) -> Answer | None:
    """`run_finished` 那条 `t:"ui"` → 这一轮还要不要再画一份正文。**纯函数。**

    ## 为什么"流过了就不再画"

    开了流式之后，正文有**两条来路**：逐字那些 `t:"delta"`，以及这一条里的完整
    `answer`。两份都画的话，屏幕上是同一个答案出现两遍 —— 而它看起来像模型说了
    两遍，不像协议发重了。设计稿把这条判据写在第五节末尾（"已经通过 delta 累积出
    正文 → 丢弃 answer"），这里就是那一句的实现。

    ## 三条出口，各自的理由不同

      * **流过**（这一轮真的吐过正文）→ 不画。但答案仍然记进 `state.answers`
        （`/history` 那类和 `scripts/verify_tui.py` 看的是它），而且把累计清掉
        —— 下一轮不该捡到上一轮的字；
      * **没流过** → 画。**这是老行为，一个字没变**：`--no-stream`、模型不支持流式、
        或者这一步是纯工具调用（一个字都没吐）时，界面拿答案的唯一途径就是这里；
      * 流过了但答案是空串（被取消、模型失败）→ 也不画：屏幕上那半截由
        `delta_reset` 或回合收尾负责，这里再补一个空块只会多一条空行。

    **判据是 `stream_text` 本身，不是 `stream_answered`。** 后者只防"同一条消息被
    重放时画两遍"（协议两端会分别升级，重放不是不可能）；拿它当"流过了"的判据会
    把"流过但没内容"和"没流过"混成一件事，而前者该画、后者也该画。
    """
    run_id = message.get("run_id", "")
    answer = message.get("answer") or ""
    state.answers[run_id] = answer
    state.stream_answered = True

    # 这一轮的流式累计属于这个 run 吗？不是就当没流过 —— 那种情况只会出现在
    # "上一轮的字还留着、这一轮的 `ui` 先到"这种乱序上，而那时候拿旧累计去
    # 抑制新答案，结果是**答案永远不显示**。
    if state.stream_run_id == run_id and state.stream_text:
        state.stream_text = ""
        state.stream_reasoning = ""
        state.stream_run_id = ""
        return None

    return Answer(answer) if answer else None


def delta_reset(state: ViewState, message: dict[str, Any]) -> bool:
    """`t:"delta_reset"` → 这一步的累计作废。返回"确实清掉了东西没有"。

    返回值给控件用：**没清掉东西就别去动 DOM**（一次没吐任何字的重试也会发一条
    reset，那时候重画是白工，而且在块还没挂上的时候还可能抛）。

    **按 step 清，不按整个回合清。** 前几步已经定下来的内容（"我看看文件"那种）
    不该被这一步的重试抹掉 —— 它们不在重试的范围内，屏幕上和历史上都是对的。
    """
    if state.stream_run_id != message.get("run_id", ""):
        return False
    if message.get("step") != state.stream_step:
        return False
    cleared = bool(state.stream_text or state.stream_reasoning)
    state.stream_text = ""
    state.stream_reasoning = ""
    return cleared


def apply_state(state: ViewState, message: dict[str, Any]) -> None:
    """`t:"ui"` 那条 `state` 快照 → 显示状态。**只有更新，没有输出。**

    它是**面板数据**（左栏那几块），不进对话流：任务列表每更新一次就在流里插一段，
    会把"你问的 + 它答的"冲稀。设计稿把这块放进常驻的左栏，正是为了这个。
    """
    for key in ("todos", "skills", "risk_scope", "jobs"):
        if key in message:
            setattr(state, key, [dict(item) for item in message[key] or []])
    # AGENT.md 那份报告：**它只在开场那一条里有**（会话创建时读一次盘，之后不会变），
    # 所以和 `skill_catalog` 用同一条规矩 —— 后来的快照没带它时要保住已经拿到的那份。
    if "agents_md" in message:
        state.agents_md = [dict(item) for item in message["agents_md"] or []]
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
    # autopilot 是**布尔**，并不进上面那两条（它们各自按列表形状复制）。判据用
    # `is True` 而不是真假值：和 `protocol/channels.py` 收到的那个方向一致 ——
    # 这一格决定"接下来会不会问你"，猜错的方向必须是"照旧问你"。
    if "autopilot" in message:
        state.autopilot = message["autopilot"] is True
    # **当前模型和它的窗口一起跟过来。** 这两格由 `ui(state)` 快照带（`_state_message`
    # 里那两行）而不是只由 `init` 带 —— `/model` 换完之后那个百分比的分母跟着变，
    # 只更新名字的话，界面会拿新模型的用量去比旧窗口：看起来完全正常，只是数错了。
    #
    # 判据用 `in` 而不是取默认值：一份老 runtime 发来的快照没有这两个键，那时候
    # 保住已经拿到的那份（和 `skill_catalog` / `agents_md` 同一条规矩）。
    if "model" in message:
        state.model = str(message["model"] or "")
    if "model_provider" in message:
        state.provider = str(message["model_provider"] or "")
    if "model_window" in message:
        state.context_tokens = message["model_window"]
    # 思考那两个旋钮：**只认布尔/字符串本身**，不做真值转换 —— 和 autopilot 那一格
    # 同一条（`is True` 而不是真假值），因为猜错的方向必须是"照旧开着"。
    if "thinking" in message:
        state.thinking_on = message["thinking"] is True
    if "effort" in message:
        state.effort = str(message["effort"] or state.effort)
    # `/model` 那张清单里的 `current` 标记也要跟着走：不带参数调 `/model` 时它标着
    # "现在用的是哪个"，而那个标记是 runtime 按当前模型算好的（别名折算也在里面）。
    current = state.model
    for item in state.model_catalog:
        item["current"] = bool(current) and item.get("id") == current


# --- 上下文栏（左栏） ----------------------------------------------------------

def rail_blocks(state: ViewState) -> list[tuple[str, str, list[Line]]]:
    """左栏五块：`(标题, 右侧计数, 行)`。

    五块的数据来源在设计稿的推导表里写死了：任务来自 `todo_write`、技能来自
    `load_skill`、权限来自 `init.permissions` + `PermissionPolicy`、会话来自
    `Session` + 审计、**后台任务来自 `ui(state).jobs`**。它们此前只有"另开一个终端"
    的出口（`--skills` / `--audit` / `--list`），放进栏里之后"agent 为什么这么做"
    "我现在放行了什么""我机器上还挂着什么"变成常驻可见，而不是翻日志考古。

    **后台那一块排在最后，不是因为最不重要**，而是因为前面四块是设计稿定下来的
    顺序（F1 那张图上从上到下就是任务/技能/权限/会话），插在中间会把它们全部挪位 ——
    而"哪一块在第几行"是用户在两次看之间形成的肌肉记忆。它自己的可见性由状态栏
    那枚徽标保证（`jobs_badge`），所以排最后不至于被漏掉。
    """
    return [
        _todo_block(state),
        _skill_block(state),
        _permission_block(state),
        _session_block(state),
        _jobs_block(state),
    ]


def _jobs_block(state: ViewState) -> tuple[str, str, list[Line]]:
    """后台任务那一块。

    **它是这一栏里唯一有"活的进程"含义的一块**（见 `ViewState.jobs`）。所以三种
    `state` 的记号必须一眼分得开，尤其是 `uncollected`（结果还没收）—— 那一条的含义
    是"这条命令已经跑完了，而你还不知道它成没成"，正是后台化唯一会静默出错的地方。

    右侧那个计数**不是"共几条"**：那是块头那行 `n` 条里已经有的数。它报的是
    **还没收场的条数**（在跑 + 结果还没收）—— 因为"还有几件事悬着"才是这一块要回答
    的问题，而"一共起过 7 条"不是。
    """
    jobs = state.jobs
    if not jobs:
        return ("后台任务", "", [Line("当前没有后台任务", ROLE_RULE),
                                 Line("shell_background 起的会在这里", ROLE_RULE)])
    outstanding = sum(1 for job in jobs if _job_role(job) != ROLE_RULE)
    lines = [
        seg((f"{_JOB_MARK.get(str(job.get('state', '')), '·')} ", _job_role(job)),
            (str(job.get("command", "")), ROLE_PROCESS),
            (f"  {_job_tail(job)}", ROLE_RULE))
        for job in jobs
    ]
    return ("后台任务", f"{outstanding} / {len(jobs)}", lines)


# 后台任务的记号。**四档各有各的形状**，因为它们要回答的问题不同：还在跑的、
# 跑完了但你还没收结果的（那一档要你动手）、收过的、被收掉的。
_JOB_MARK = {
    "running": "◐",
    "uncollected": "✓",
    "done": "·",
    "killed": "—",
}


def _job_role(job: dict[str, Any]) -> str:
    """这一条的强调色。**只有"结果还没收"那一档值得抢眼睛** —— 其余按安静处理。

    和任务列表那块同一条取向（`in_progress` 用 `waiting`、其余用 `process`）：
    一栏里同时有五个东西在喊就等于没有东西在喊。
    """
    state = str(job.get("state", ""))
    if state == "uncollected":
        return ROLE_WAITING
    if state == "running":
        return ROLE_PROCESS
    return ROLE_RULE


def _job_tail(job: dict[str, Any]) -> str:
    """任务名后面那一小段：跑了多久 / 退出了没有 / 还差一步收结果。

    **`uncollected` 那一条必须自己说出"结果还没收"** —— 光有一个 `✓` 记号，
    读的人会以为这件事已经了了，而它恰恰是这一块里唯一需要动作的一条。
    """
    state = str(job.get("state", ""))
    seconds = job.get("seconds")
    span = f"{int(seconds)}s" if isinstance(seconds, int) else ""
    if state == "running":
        return f"在跑 {span}".strip()
    if state == "uncollected":
        code = job.get("exit_code")
        return f"已结束（退出码 {code}）· 结果还没收"
    if state == "killed":
        return "已被收掉"
    code = job.get("exit_code")
    return f"已结束（退出码 {code}）· 已收"


def _todo_block(state: ViewState) -> tuple[str, str, list[Line]]:
    todos = state.todos
    if not todos:
        return ("任务", "", [Line("当前还没有任务", ROLE_RULE),
                             Line("agent 创建的任务会在这里", ROLE_RULE)])
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
    """已加载技能：**这个会话按哪几份说明在做**。

    ## 它和 `Ctrl+S` 那个面板看的不是同一个东西（这一条很容易被当成 bug）

    | | 数据 | 回答的问题 |
    |---|---|---|
    | 这一块 | `session.metadata` 里 `load_skill` 写下的指针（`state.skills`） | **这次会话已经读了哪几份步骤** |
    | `Ctrl+S` | 工作区里扫出来的全部技能（`state.skill_catalog`） | **有哪些技能可以读** |

    所以"左栏 0 个、`Ctrl+S` 里列着好几个"是**正常的**：那说明模型还没读过任何一份，
    而工作区里确实有货。项目里同一对区分还有一处（`--skills` 打的是可用清单，
    `active_line` 说的是已加载）—— 这不是实现走岔了，是两个真的不同的事实。

    **空态不写"可用有几个"**（曾经写过一版，去掉了）：那一块的标题就是"**已**加载"，
    计数写的是已加载的个数，而"可用几个"是另一个事实、另一份清单（`Ctrl+S` 那份）。
    把它塞进这里，读起来像在替那一块解释自己为什么是 0 —— 而这个栏里每一行都该说
    自己那一块的事。要看可用清单，`Ctrl+S` 就是那个出口。
    """
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
    # 这两行**不再是 `warn`（橙）**：`warn` 按设计文档 §14.1 是"MEDIUM 风险"那一档，
    # 而这里是**用户自己记住的例外** —— 它不是风险，一栏里出现两行橙字之后，真正
    # 该看一眼的风险色就不响了。改成"标签 `ink4` + 值 `ink3`"，和上面三行同一套写法。
    # **文本一个字没变**（`str(line)` 仍是 `点名免问 fetch_web`），所以"左栏已经显示
    # 着它、旁白就别再说一遍"那条断言照旧成立。
    if state.granted_tools:
        lines.append(seg(("点名免问", ROLE_RULE),
                         (" " + "、".join(state.granted_tools), ROLE_PROCESS)))
    if state.granted_prefixes:
        lines.append(seg(("命令规则", ROLE_RULE),
                         (" " + "、".join(state.granted_prefixes), ROLE_PROCESS)))
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
    # **这个会话在用哪个模型**，常驻在它下面一行。
    #
    # 它此前只有两个出口：启动那一条 notice（会随对话滚走）和状态栏——而状态栏那格写
    # 的是**用量**（`上下文 2k / 1M`），里面那个模型名只在会话头那一行、窄屏还会被收起。
    # 加了 `/model` 之后这一格变成了"我上次换的那个还生效着吗"唯一能随时看一眼的地方，
    # 所以它进「本次会话」这一块（和会话 id、规模、审计同一档事实：都由这个会话决定）。
    if state.model:
        # 窗口跟着一起说：看用量而不看分母，等于只说了半句话。窗口不在目录里时
        # **只说名字**（不猜一个分母）。
        window = f"  {tokens_text(state.context_tokens)}" if state.context_tokens else ""
        lines.append(seg((state.model, ROLE_SKILL), (window, ROLE_RULE)))
    # 思考模式**只在关着的时候占一行**。开着是常态（端点的默认行为就是开），为它常驻
    # 一行会让左栏那几块里的信息密度掉下来 —— 而"关着"是个例外，值得被看见。
    if not state.thinking_on:
        lines.append(seg(("思考 关", ROLE_WARN),
                         (f"  强度 {state.effort}", ROLE_RULE)))
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
    # 这个会话的 system 消息里注入了哪几份 AGENT.md。**放进"本次会话"这一块**（而不是
    # 新开一块）：它和"这个会话 id 是什么、走了几步"是同一档事实 —— 都由会话创建那一刻
    # 决定，也都在会话之间各不相同。开场那条 notice 说的是同一次加载，但那一条会随
    # 对话滚走，而"我这份 AGENT.md 到底生效了没有"是随时会想再看一眼的问题。
    for item in state.agents_md:
        name = item.get("path", "?")
        failed = bool(item.get("failed"))
        # 截断过的用 `…` 标出来：**它和"读失败"不是一回事**（内容进去了，只是不全），
        # 所以不能和 failed 共用那个 `!` —— 那会让人以为这份说明整个没生效。
        mark = "! " if failed else ("… " if item.get("truncated") else "")
        detail = item.get("reason") if failed else f"{item.get('lines', 0)} 行"
        lines.append(seg(
            (f"{mark}{name}", ROLE_WARN if failed else ROLE_RULE),
            (f"  {detail}" if detail else "", ROLE_RULE),
        ))
    return ("本次会话", "", lines)


def rail_summary(state: ViewState) -> str:
    """上下文栏收起时那一行摘要（F5 的窄屏形态）。

    它必须**说清收起之后少了什么**：任务几条、技能几个、权限是什么档 ——
    否则"收起"就等于"看不见"，而左栏存在的全部理由就是让它们常驻可见。
    """
    parts = ["Ctrl+B 展开上下文栏"]
    if state.jobs:
        outstanding = sum(1 for job in state.jobs
                          if str(job.get("state", "")) in ("running", "uncollected"))
        parts.append(f"{outstanding} 个后台任务" if outstanding
                     else f"{len(state.jobs)} 个后台任务（都收过了）")
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


def should_auto_open(state: ViewState) -> bool:
    """上下文栏该不该自动展开（决策 1，第三期改成决策 26）。

    **默认收起**（贴 Claude Code 的克制），而"真有任务/技能时自动展开"是那句话的另一
    半 —— 一条被维护的任务列表意味着这个会话值得看全局。

    ## 触发条件是「任务列表**从无到有**」，不是「有任务」

    这是个**边沿**而不是电平，而且必须有这个区别：按电平做的话，用户在有任务时按
    `Ctrl+B` 收起，下一次刷新（50ms 后）条件仍然成立，栏会被立刻顶回来 —— **收起
    从此失效**。所以这里记一个 `rail_todos_seen`：任务列表第一次出现时顶开一次，
    之后听用户的；列表被清空就重新武装（下一次出现又算一次新事件）。

    列表**内容更新**（某个任务变成 completed）不重开：一次长任务里 `todo_write` 会被
    调很多次，每次都顶开会变成一个自己弹开的栏。

    ## v1 的两个附加条件为什么去掉了

      * ~~窄屏（< 120 列）一律不展开~~：栏宽 32 列在 80 列的终端里吃掉 40%，这个代价
        是真的，但"模型刚写下任务列表"比它更要紧 —— 那一栏装着"它打算做哪几件事"，
        是用户唯一能提前看出"它理解得对不对"的地方。窄屏嫌挤就 `Ctrl+B` 收掉，
        收起后还有那一行摘要；
      * ~~用户按过 `Ctrl+B` 之后不再自动开合~~：手动操作仍然被尊重（**收起之后不会
        因为别的状态更新自己弹开**），但**任务列表出现是一个新的、明确的事件** ——
        用户当初收起它时，前提是"那时候没有任务"。所以它够格把栏顶开一次。

    **它不是纯函数**：会推进 `state.rail_todos_seen` 那个边沿标记。
    """
    has_todos = bool(state.todos)
    if has_todos and not state.rail_todos_seen:
        state.rail_todos_seen = True
        return True
    if not has_todos:
        # 清空了就重新武装：下一次出现是**新事件**，不是"同一批还在"。
        state.rail_todos_seen = False
    if state.todos or state.skills:
        # 那次顶开之后听用户的（包括"又有技能"这种情况，它不算新事件）。
        return state.rail_open
    return state.rail_open if state.rail_pinned else False


# --- 命令面板 ------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """一条 `/` 命令。**它是数据，不是分支**：面板按它渲染、按它执行。"""

    name: str
    hint: str
    takes_arg: bool = False
    # 带参数时会怎样、有哪几种写法。**只有 `/help` 读它** —— 面板那一行是一句短语
    # （见下面 `hint` 那段），而"`/model flash` 打错一个字会怎样"这种话面板放不下，
    # 也不该放：那属于单条命令的详细说明。
    #
    # 它和 `takes_arg` 分开是有意的：`takes_arg` 是**功能上的事实**（这条命令收参数），
    # 而 `detail` 是**文案**。合成一个的话，"参数必须精确、不做模糊匹配"这类只在
    # `/model` 上成立的规矩就会被硬塞进一个通用字段里。
    detail: str = ""


# 命令集。v1 那六条是决策 15 定下来的，**顺序也照设计稿 F2 的面板**；
# 后面五条是后来加的（面板、技能清单、配色、状态/工具/模型），加在末尾而不是插在
# 中间 —— 那六条的位置是用户已经见过的肌肉记忆。
#
# ## 带参数的那三条：`detail` 是给 `/help` 的，`hint` 仍然是一句短语
#
# `/resume` `/theme` `/model` 都收参数，而"参数写错的后果"各不相同（切到一个不存在的
# 会话 = 开一个新会话；配色名认不出来 = 就近提示；模型名认不出来 = **拒绝**）。
# 那三句都放不进面板那一列，所以它们进 `detail`，只有 `/help` 读 —— 而 `/help`
# 是那个"详细说明"本来就该在的地方。
#
# **`/list` 在第二期被去掉了**（设计决策，见 doc/TUI-design.md 13.3）：它和
# "`/resume` 不带参数"说的是同一件事，而两条命令指向同一个出口时，人会先猜哪一条
# 才是对的。留 `/resume` 一条，它自己负责列出候选。
#
# ## `hint` 是一句短语，不是说明书
#
# 它是面板里跟在命令名后面的那一列、也是 `/help` 那一列 —— **一行的宽度就是它的
# 预算**。所以：不带参数会怎样（`/resume` 不带参数就列清单）、有几套配色
# （14 套）、"立刻生效不用退出"这类**消失之后就不必再说的话**，一律不写在这里。
# 这些要么在那一行里读得出来（按一下就知道），要么属于单条命令的详细说明
# （`/help` 末尾那几行、`doc/TUI-design.md`）。
COMMANDS: tuple[Command, ...] = (
    Command("/new", "开一个新会话"),
    Command("/resume", "换一个会话", True,
            "不带参数弹出会话清单；/resume <id> 直接切过去"),
    Command("/audit", "审计日志在哪"),
    Command("/exit", "退出"),
    Command("/help", "命令与键位"),
    Command("/theme", "换配色", True,
            "不带参数列出 14 套；/theme 石墨琥珀 或 /theme a 直接换"),
    Command("/skills", "看全部技能"),
    # **这一条推翻了决策 15 的一部分**（那一版明确不给 `/autopilot`，理由是"它是
    # 一次没有人可问，在有人看着的界面里语义矛盾"）。现在它是"**有人在看着，但他
    # 选择不看每一条**"—— 语义变了所以结论才改，理由留在 app.py 的
    # `_command_autopilot` 和 doc/TUI-design.md 那一节里。
    Command("/autopilot", "自动放行开关"),
    # 下面三条是**只读**的（`/model` 带参数才会改一个会话级设置）。
    #
    # `/status` 和 `/tools` 此前只有"另开一个终端跑 `--audit` / 看启动横幅"这两条
    # 出口 —— 而"它现在到底在用什么、放行了什么、花了多少"是随时会想看一眼的问题，
    # 不该需要离开这个界面。
    Command("/status", "看现在的状态"),
    Command("/tools", "工具与权限"),
    Command("/model", "换模型", True,
            "不带参数列出可选模型；/model deepseek-v4-pro 直接换"
            "（名字要精确，打错不猜）"),
    # 思考模式那两个旋钮。**分两条命令**（而不是 `effort=off` 兼作开关）：它们是两个
    # 问题 —— "要不要想"和"想多用力" —— 而合成一个之后，"关着的时候强度是什么"就
    # 变成一个必须回答、又没人关心的问题。
    Command("/thinking", "思考模式开关", True,
            "不带参数看现在是开还是关；/thinking on 或 /thinking off 改它"
            "（关掉不清强度，再打开还是原来那个）"),
    Command("/effort", "思考强度", True,
            "不带参数看现在是哪一档；/effort low、/effort high、/effort max 改它"
            "（端点还接受 minimal/medium/xhigh/ultra 这些等价写法）"),
)

# 命令名那一列的宽度。**从最长的那条算出来，不手写数字。**
#
# 手写过一次（`f"{name:<9}"`），而 `/autopilot` 是 **10 个字符** —— 它一加进来就把
# 那一列顶穿了，于是"命令名"和"后面那句文案"之间**一个空格都不剩**，两段粘成一条
# 读（实测：`/autopilot自动放行开关`）。这不是"有点挤"：那个面板的全部价值就是
# "名字一列、说明一列"，粘在一起就只剩一列了。
#
# 从表里算出来之后，下一个更长的命令只会让整列一起右移，不会再出现同一类事故。
# `+ 3` 是两列之间那个间隙（最短的命令名因此有 9 格，最长的那条有 3 格）。
COMMAND_NAME_WIDTH = max(len(command.name) for command in COMMANDS) + 3


# --- `/status` `/tools` `/model` 的渲染 ----------------------------------------
#
# 三条命令的答案**都进会话流**（不弹面板）。理由是同一条：它们是"看一眼就走"的
# 东西，而弹层会挡住正在读的对话 —— 而 `/status` 最有用的时候恰恰是"它跑着、我想
# 看一眼花了多少"，那时候屏幕上的东西正是你要看的。
#
# 渲染是**纯函数**（给 runtime 给的那份数据，还几行 `Line`），所以它们能被直接单测
# —— 这个项目里"事件怎么变成给人看的字"一向这么处理（见 `render_event`）。

# 值那一列的左边界。**按终端列数算，不按字符数** —— 标签里"会话"占 4 列而"规模"
# 占 4 列、"累计输入"占 8 列而"累计输出"占 8 列，但 `f"{'会话':<10}"` 会补 8 个空格、
# `工作区` 补 7 个，于是值那一列**歪一格**（实测：`会话        2026…` 和
# `工作区       C:/…` 差一列）。这个文件里唯一的宽度口径是 `cell_len`（见文件头）。
_LABEL_WIDTH = 10


def _kv(label: str, value: str, role: str = ROLE_PROCESS) -> Line:
    """`模型        deepseek-flash` 这样的一行。标签和值分色。"""
    pad = max(1, _LABEL_WIDTH - cell_len(label))
    return seg((f"  {label}{' ' * pad}", ROLE_RULE), (value, role))


def workspace_short(path: str, home: str = "") -> str:
    """把一个绝对路径说短一点（`~` / 相对当前目录）。

    `/status` 那一行是给人扫的，而一长串 `C:\\Users\\谁\\repo\\...` 会把值那一列
    挤到屏幕外面 —— 而"我在哪个工作区"这个问题只需要认出是哪一个，不需要完整路径。
    **只做替换、不做解析**：路径不是这里的知识，原样显示永远是对的。
    """
    text = (path or "").replace("\\", "/")
    if home:
        root = home.replace("\\", "/").rstrip("/")
        if root and text.lower().startswith(root.lower()):
            return "~" + text[len(root):]
    return text or "—"


def render_status(state: ViewState, message: dict[str, Any]) -> list[Line]:
    """`/status` 的回包 → 会话流里那几行。

    ## 它回答四个问题，顺序就是这个顺序

      1. **我在哪个会话里**（id / 工作区 / 多长）；
      2. **它在用什么**（模型 + 上下文窗口用量）；
      3. **跑了多少活、花了多少钱**（轮次计数 + token）；
      4. **这次运行的环境**（步数上限、流式、autopilot、审计写到哪）。

    顺序是刻意的：`/status` 最常被问的是"这是哪个会话、在用什么模型"，而"花了多少"
    跟着它 —— 把审计路径排在第一位的话，每次都要先跳过一行路径才看到想看的。

    ## 三个数各自的口径，一个都不能混

      * `上下文` 是**上一次请求实际发出去多少**（不是"现在"：下一次请求还要加上
        这一轮的回答和工具结果，所以它是**下界**）；
      * `累计输入/输出` 是**整个会话**的（那是钱）；
      * `轮次` 数的是 `run_started`（用户按了几次回车）。
    """
    status = message.get("status") or {}
    session = status.get("session") or {}
    model = status.get("model") or {}
    counters = status.get("counters") or {}
    usage = status.get("usage") or {}
    meta = status.get("meta") or {}

    # 会话还没起来（一轮都没跑过）：不编一份空状态出来 —— 那会让人以为
    # "0 轮 0 调用"是事实，而事实是"还没问过"。
    if not session:
        return [Line("（还没有状态：这个会话一步都没走过）", ROLE_RULE)]

    out: list[Line] = [Line("状态", ROLE_RULE)]

    where = workspace_short(str(session.get("workspace", "")))
    length = f"{session.get('messages', 0)} 条消息 · {session.get('steps', 0)} 步"
    # **"这次启动：继续/新建"说的是这次进程怎么开起来的，不是这个会话有多满。**
    # 写成"（继续）"会让"继续一个新会话"读起来自相矛盾（实测：恢复一个从没聊过的
    # 会话时那一行长这样），而规模和步数就在下面一行，那才是"有多满"的答案。
    started = "这次启动：继续" if session.get("resumed") else "这次启动：新建"
    out.append(_kv("会话", f"{session.get('id', '?')}（{started}）"))
    out.append(_kv("工作区", where))
    out.append(_kv("规模", length, ROLE_RULE))

    current = str(model.get("current") or "—")
    provider = str(model.get("provider") or "")
    selected = str(model.get("selected") or "")
    # **"想用的"和"在用的"不一样时要写出来。** 那个差别只在一种情况下出现：刚按了
    # `/model`、下一次请求还没发出去（见 state/model.py 的 SessionModel）。不写的话，
    # 用户会以为"换了但没生效"是坏了 —— 而它其实是设计好的时序。
    pending = bool(selected and selected != current)
    if pending:
        current = f"{current}（换成 {selected} 的，下一次请求生效）"
    if provider:
        # 路由名跟在一起。**两条路由可以有同名模型**，所以"它到底在哪跑"必须看得见
        # —— 而它不只是个装饰：账单和合规都跟着它走。
        current = f"{current}  @{provider}"
    out.append(_kv("模型", current, ROLE_WAITING if pending else ROLE_PROCESS))

    # 端点**只在不是默认那个时**单独写一行。理由和"默认权限不占一行"一样：官方端点
    # 是绝大多数会话的样子，常驻一行只会把真正该看一眼的东西（自建网关、代理）淹掉 ——
    # 而那种情况下"请求发到哪儿"正是最该确认的一件事。
    base_url = str(model.get("base_url") or "")
    if base_url and "api.deepseek.com" not in base_url:
        out.append(_kv("端点", base_url, ROLE_RULE))

    # 思考模式那两个旋钮。**关着的时候不写强度**：`关 · high` 会让人以为 high 还在
    # 生效。强度并没有被丢掉（`/thinking on` 之后还是它），只是这一行不撒谎。
    reasoning = model.get("reasoning") or {}
    thinking = reasoning.get("thinking", True)
    effort = str(reasoning.get("effort") or "")
    out.append(_kv(
        "思考",
        f"开 · {effort}" if thinking else "关（强度记着，/thinking on 回来）",
        ROLE_PROCESS if thinking else ROLE_WAITING,
    ))

    # 上下文那一行：分子是**最近一次请求**，分母是**当前模型**的窗口。
    # 分母为 None（模型不在目录里）时只报分子 —— 错的百分比比没有百分比更坏。
    used = message.get("last_prompt_tokens")
    window = message.get("context_tokens")
    if used is None:
        context = "—（还没成功调用过模型）"
    elif window:
        context = (f"{tokens_text(used)} / {tokens_text(window)}"
                   f"（{used / window * 100:.1f}%）")
    else:
        context = f"{tokens_text(used)}（这个模型的窗口不在目录里，不报占比）"
    out.append(_kv("上下文", context))

    if usage.get("prompt"):
        rate = f"{usage.get('cached', 0) / usage['prompt']:.0%}"
        out.append(_kv("累计输入", f"{tokens_text(usage.get('prompt'))} token"
                                   f"（命中缓存 {tokens_text(usage.get('cached'))}、"
                                   f"命中率 {rate}）"))
        out.append(_kv("累计输出", f"{tokens_text(usage.get('completion'))} token", ROLE_RULE))
    else:
        out.append(_kv("累计用量", "还没有成功调用过模型", ROLE_RULE))

    # 轮次与调用。**"工具调用 N 次"里含被拒绝的那几次** —— 这个数回答的是"跑了多少活"，
    # 不是"成功了几次"（后者 `--audit` 里逐条看得到）。
    waits = counters.get("permission_waits", 0)
    asks = counters.get("asks", 0)
    tail = ""
    if waits or asks:
        tail = f"（其中审批 {waits} 次"
        tail += f"、提问 {asks} 次）" if asks else "）"
    out.append(_kv("轮次", f"{counters.get('runs', 0)} 轮 · "
                           f"{counters.get('model_calls', 0)} 次模型调用 · "
                           f"{counters.get('tool_calls', 0)} 次工具调用{tail}"))

    flags = [f"最多 {meta.get('max_steps', 0)} 步",
             "流式" if meta.get("stream") else "非流式"]
    flags.append("自动放行" if meta.get("autopilot") else "逐条审批")
    out.append(_kv("这次运行", " · ".join(flags), ROLE_RULE))
    out.append(_kv("工具", f"{meta.get('tool_count', 0)} 个（/tools 看清单）", ROLE_RULE))
    out.append(_kv("审计", str(meta.get("audit_path") or "—"), ROLE_RULE))
    return out


# 权限那一列的说法。**"会问你"和"不问"要一眼分得开** —— 这一列的全部价值就是
# "接下来这条会不会弹审批"。
_TOOL_DISPOSITION = {
    "auto": ("自动放行", ROLE_RULE),
    "ask": ("需要审批", ROLE_WAITING),
    "deny": ("直接拒绝", ROLE_DENIED),
}


def render_tools(state: ViewState, message: dict[str, Any]) -> list[Line]:
    """`/tools` 的回包 → 会话流里那几行。

    ## 为什么每一条都显示，而不是只显示"可用"的

    因为**"这个工具存在吗"和"它会不会问我"是两个问题**，而这条命令要一次回答两个。
    只列自动放行的（用户例子里那种 `✓`）会把拒绝名单藏起来 —— 而"我明明配了
    deny_tools，它怎么还……"正是最该在这里看见答案的问题。

    ## `external` 单独标出来

    MCP 工具的风险**一律 high、每次都要你按键**（见 README「外部工具」那一节），
    点名的写法也是全名（`mcp__server__tool`）。不标的话，用户看到一长串
    `mcp__…` 只会以为名字起得怪。
    """
    rows = message.get("tools") or []
    if not rows:
        # 一个都没有：`load_skill` / `web_search` / `grep` 都会因为缺件而不注册。
        # 这不是空清单，而是"这次运行什么都没注册"—— 说清楚，别让屏幕空着。
        return [Line("这次运行一个工具都没注册（缺引擎/密钥时会这样，启动那几行里有原因）",
                     ROLE_WARN)]

    out: list[Line] = [Line("工具", ROLE_RULE)]
    # 名字那一列也按**列数**对齐：`mcp__kb__search` 是 ASCII（一层 `cell_len` 就等于
    # 字符数），而将来若出现非 ASCII 的工具名，`len()` 会把它算短、整张表跟着歪。
    width = max(cell_len(str(row.get("name", ""))) for row in rows)
    for row in rows:
        name = str(row.get("name", "?"))
        word, role = _TOOL_DISPOSITION.get(str(row.get("disposition")), ("?", ROLE_RULE))
        marks: list[str] = []
        if row.get("granted"):
            marks.append("按过 t")
        if row.get("external"):
            marks.append("外部")
        if row.get("interactive"):
            marks.append("会问你")
        elif row.get("parallel_safe"):
            marks.append("可并发")
        out.append(seg(
            (f"  {name}{' ' * max(2, width - cell_len(name) + 2)}", ROLE_PROCESS),
            (f"{row.get('risk', '?'):<7}", ROLE_RULE),
            (word, role),
            (("  ·  " + "、".join(marks)) if marks else "", ROLE_RULE),
        ))

    prefixes = message.get("granted_prefixes") or []
    if prefixes:
        # 命令前缀规则只对**参数里有命令行**的工具生效（今天只有 shell）。
        # 那句话必须跟着一起说：不说的话，用户会以为 "git add" 这条规则能放开
        # read_file。
        out.append(Line(f"  命令规则（按前缀放行，只对 shell 这类有命令行的工具生效）："
                        f"{'、'.join(prefixes)}", ROLE_RULE))
    out.append(Line("  改这些去 .tudouni/permissions.json；审批时按 t 会写进去",
                    ROLE_RULE))
    return out


def render_models(state: ViewState, rest: str = "") -> list[Line]:
    """`/model` 不带参数时那张清单。

    ## 为什么打错名字不是"就近匹配一个"

    它的处置和 `/theme` 一致：**认不出来就说认不出来**，然后列出清单。模糊匹配在这里
    比 `/theme` 更坏 —— 配色选错一眼就看得出来，而模型选错只会在账单上体现
    （Pro 的未命中输入是 Flash 的四倍多）。所以 `/model flsh` 得到的是"没有这个模型"，
    不是"猜你想选 flash"。

    ## 为什么要写 provider

    同名模型可以在多条路由上（官方一条、自建网关一条）。只列模型名的话，那两行长得
    一模一样 —— 而"选了哪一个"决定了请求发到哪个账号上。所以名字那一列写
    `provider/model`，这是唯一不歧义的形式（也是 `/model` 收的形式）。

    `current` 那一格由 runtime 标好（它要对账别名折算），界面不自己比字符串。
    """
    if not state.model_catalog:
        return [Line("（runtime 没给模型清单：这一版协议之前起的子进程？）", ROLE_WARN)]
    here = f"{state.provider}/{state.model}" if state.provider and state.model else (
        state.model or "—")
    out: list[Line] = [Line(f"当前模型：{here}", ROLE_WAITING), Line("可选：", ROLE_RULE)]
    names = [f"{item.get('provider')}/{item.get('id')}"
             if item.get("provider") else str(item.get("id", ""))
             for item in state.model_catalog]
    width = max(cell_len(name) for name in names) if names else 0
    for item, name in zip(state.model_catalog, names):
        mark = "●" if item.get("current") else " "
        window = item.get("window")
        detail = f"{item.get('label', '')}"
        if window:
            detail += f" · 上下文 {tokens_text(window)}"
        out.append(seg(
            (f"  {mark} {name}{' ' * max(2, width - cell_len(name) + 2)}",
             ROLE_WAITING if item.get("current") else ROLE_PROCESS),
            (item.get("summary", ""), ROLE_PROCESS),
            (f"   （{detail}）" if detail else "", ROLE_RULE),
        ))
        if item.get("note"):
            out.append(Line(f"      {item['note']}", ROLE_RULE))
    for alias in state.model_aliases:
        # 旧名字单独列：它们是**认下的名字**，不是能选的选项（官方已把对应的模型
        # 下线，请求由新模型提供服务）。列进主清单会摆出两个效果一样的选项。
        out.append(Line(f"  认下的旧名字：{alias.get('id')} → {alias.get('of')}", ROLE_RULE))
    out.append(Line("换一个：/model <名字>（名字要精确；两条路由同名时写 provider/model）",
                    ROLE_RULE))
    if rest:
        # 带参数走到这里 = 名字没认出来（`app._command_model` 只在没换成时才调它）。
        # 那句话由 app 负责说，这里只补一句"清单在上面"。
        out.append(Line("（清单里没有那个名字）", ROLE_WARN))
    return out


def render_thinking(state: ViewState, rest: str = "") -> list[Line]:
    """`/thinking` 不带参数时那两行：现在是开还是关、怎么改。

    **两个状态都要写出来**（不做成"关着才显示"）：那样"这一格空着"既可能是开着、
    也可能是没画出来 —— 而这一格决定下一次请求花多少钱、想多久。

    关着时**顺便说清强度还在**：用户按了 `/thinking off` 之后最自然的疑问是
    "我刚才设的 max 是不是没了"。
    """
    lines = [
        Line(f"思考模式：{'开' if state.thinking_on else '关'}", ROLE_WAITING),
        Line(f"  强度：{state.effort}"
             + ("" if state.thinking_on else "（关着时用不上，但记着）"), ROLE_RULE),
        Line("改：/thinking on   ·   /thinking off", ROLE_RULE),
    ]
    if rest:
        lines.append(Line(f"（认不出这个写法：{rest}）", ROLE_WARN))
    return lines


def render_effort(state: ViewState, levels: tuple[str, ...], rest: str = "") -> list[Line]:
    """`/effort` 不带参数时那几行：现在是哪一档、有哪几档。

    档位清单**由 runtime 给**（`levels`），界面不写死 —— 写死的话，将来端点加一档
    就得改两个地方，而漏改的那一处只表现为"这一档选不了"。
    """
    lines = [
        Line(f"思考强度：{state.effort}"
             + ("" if state.thinking_on else "（思考关着，打开才用得上）"), ROLE_WAITING),
        Line("可选：", ROLE_RULE),
    ]
    for level in levels:
        mark = "●" if level == state.effort else " "
        lines.append(Line(f"  {mark} {level}", ROLE_PROCESS if mark == " " else ROLE_WAITING))
    lines.append(Line(f"改：/effort {'  ·  /effort '.join(levels)}", ROLE_RULE))
    if rest:
        lines.append(Line(f"（没有这一档：{rest}）", ROLE_WARN))
    return lines


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

    **只按名字前缀匹配**，不做模糊搜索：命令一共十三条，而模糊匹配会让"我打错了"
    和"它猜对了"长得一样 —— 一个按下去不是你想的那条命令的面板比没有面板更坏。
    """
    text = query.strip().lstrip("/").lower()
    if not text:
        return list(COMMANDS)
    return [command for command in COMMANDS if command.name[1:].startswith(text)]
