"""界面的零件。**这个文件和 `app.py` 是唯一两个准 import textual 的地方。**

那条规矩由 `tests/test_imports.py` 里的一条测试盯着，而它的意义是具体的：
老 CLI（`uv run main.py`）、协议子进程（`--runtime-stdio`）、四个不需要模型的
子命令**都不该加载一个 TUI 框架**。破掉它的症状不是报错，是启动变慢、以及在
一个没装 textual 的环境里直接崩 —— 而 `--list` 那种查会话的子命令不该有这种依赖。

## 这一版的零件比 v1 多，因为它们各自要"跟着主题重画"

配色可以在运行中换（`/theme`），所以每个零件都得能重画自己。做法统一成一条：
**零件记住"输入"（协议状态或行），重画 = 用新配色再算一遍**，而配色由调用方
（`App`）显式传进来。**没有任何控件去 `self.app.palette`** —— 理由是具体踩过的：
`mount()` 是排队的，子控件的 `on_mount` 可能在一个"还没挂上 DOM"的控件上跑，
那时候 `self.app` 拿不到，而症状是点开某个面板时偶发一个 NoActiveApp。显式传参
把这一类时序问题整个删掉了。

## 三条从设计稿抄下来的语法

  1. **工具行有自己的语法**（`→ [n] tool(args)` / `← [n] ✓ 8,412 字符 41ms`），
     而风险靠**颜色**不靠文字：LOW 不着色也不写"低风险"（它占多数，写出来只是噪声）；
  2. **审批面板的三条约束照搬后端语义**：`Esc` = 拒绝（fail-closed）、后果那句话
     原样显示（出自 `security/asker.py`）、推不出前缀时**不渲染** `t` 键；
  3. **提问面板回的是选项原文，不是编号**，而"跳过"必须是一个显式的键。
"""

from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.binding import BindingsMap
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static, TextArea

from agent_runtime import i18n
from agent_runtime.frontends.tui import theme as theme_mod
from agent_runtime.frontends.tui import view_state


def translated_bindings(pairs: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """`(键, 动作, 文案键)` → `(键, 动作, 说明)`，说明按当前语言取好。"""
    return [(key, action, i18n.t(text_key)) for key, action, text_key in pairs]


def localize_bindings(widget: Any, pairs: list[tuple[str, str, str]]) -> None:
    """把一个控件的 `BINDINGS` 换成**当前语言**的那一份。`pairs` 是 `(键, 动作, 说明)`。

    ## 为什么不能直接在类体里写 `i18n.t(...)`

    Textual 在**类创建时**就把 `BINDINGS` 合并进 `_merged_bindings`
    （`DOMNode.__init_subclass__` → `_merge_bindings`），而那发生在 import 的那一刻
    —— 写在类体里的文案会被冻成 import 时的语言。真实那条路（`main.py` 先
    `i18n.activate` 再 import TUI）恰好对得上，但测试里 `with_language` 换的那一套
    对不上，于是"界面上写着 A、按下去是 B"这种没人查得出来的不一致就有机会出现。

    所以：类体里只留 `(键, 动作, 文案键)`，实例化之后用 `translated_bindings` 换来
    这里。`DOMNode.__init__` 建的是 `_merged_bindings` 的一份**拷贝**，覆盖实例这
    一格不影响别的界面。

    （这些说明文字露面很少 —— Textual 自带的按键面板和它的 `Ctrl+P` 命令面板会显示
    它们，而这个界面自己有 `Ctrl+K` 那一套。但"很少露面"不等于可以写错语言。）
    """
    widget._bindings = BindingsMap(list(pairs))

# 行的角色 → 主题里的哪个角色。**这是整份设计稿"色彩 Token"那一节的落地处**：
# 上面（view_state）只回答"这一行是哪一类"，这里才回答"那一类是什么颜色"。
ROLE_ATTR: dict[str, str] = {
    view_state.ROLE_USER: "ink",
    view_state.ROLE_ANSWER: "ink2",
    view_state.ROLE_PROCESS: "ink3",
    view_state.ROLE_TOOL: "accent",
    view_state.ROLE_RESULT: "ok",
    # 安静模式那两条：颜色其实由分段给（工具名按风险上色、结果记号 `✓`/`✗` 各有各的
    # 色），这两条只在"整行一个 role"的时候才用得上 —— 留着是为了让"角色 → 颜色"
    # 这张表**没有落不下的角色**（缺一个的话，`paint` 会悄悄退回 `ink3`，
    # 而那种错看起来只是"这一行颜色有点淡"）。
    view_state.ROLE_TOOL_BRIEF: "accent",
    view_state.ROLE_TOOL_BRIEF_DONE: "accent",
    view_state.ROLE_RISK_MEDIUM: "warn",
    view_state.ROLE_RISK_HIGH: "danger",
    view_state.ROLE_THINK_HEAD: "ink4",
    view_state.ROLE_THINK_BODY: "ink3",
    view_state.ROLE_TURN_START: "ink3",
    view_state.ROLE_TURN_END: "ink3",
    view_state.ROLE_WAITING: "accent",
    view_state.ROLE_DENIED: "danger",
    view_state.ROLE_NOTICE: "ink3",
    view_state.ROLE_WARN: "warn",
    view_state.ROLE_RULE: "ink4",
    view_state.ROLE_SKILL: "skill",
    view_state.ROLE_QUOTE: "line",
}

# 加粗的那几类。终端里没有字号，所以"层级"落在颜色 + 粗细两件事上（见 theme.py）。
BOLD_ROLES = {
    view_state.ROLE_TURN_START,
    view_state.ROLE_TURN_END,
    view_state.ROLE_USER,
    view_state.ROLE_WAITING,
}


def color_of(palette: theme_mod.Theme, role: str) -> str:
    return getattr(palette, ROLE_ATTR.get(role, "ink3"))


def style_of(palette: theme_mod.Theme, role: str) -> str:
    style = color_of(palette, role)
    return style + " bold" if role in BOLD_ROLES else style


# agent 状态 → 那个记号的颜色。**它就是"转圈"**：没有流式，所以界面靠这一个字
# 告诉人"它在动"。在跑 = 交互色（accent，全界面只有它是交互信号）、答完 = 成功色、
# 被砍断/失败 = 警告/危险色、空闲 = 最暗那一档（常态不该抢眼）。
_PHASE_COLOR = {
    "working": "accent",
    "waiting_permission": "accent",
    "waiting_human": "accent",
    "finished": "ok",
    "limited": "warn",
    "failed": "danger",
    "cancelled": "warn",
}


def _phase_color(palette: theme_mod.Theme, phase: str) -> str:
    return getattr(palette, _PHASE_COLOR.get(phase, "ink4"))


def paint(palette: theme_mod.Theme, line: view_state.Line) -> Text:
    """一行 `Line` → 一段 `Text`。**外来文本一律走 `Text`**，所以 markup 不会被解析。

    模型和工具的输出里出现 `[` 是家常便饭（路径、列表、代码），而 `Text` 是唯一能
    让 rich 不去解析它的东西 —— 这是"渲染别人的文本"时最经典的坑。
    """
    if not line.segments:
        return Text(str(line), style=style_of(palette, line.role))
    text = Text()
    for chunk, role in line.segments:
        text.append(chunk, style=style_of(palette, role))
    return text


def paint_lines(palette: theme_mod.Theme, lines: list[view_state.Line]) -> Text:
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        text.append_text(paint(palette, line))
    return text


class Panel(Static):
    """会跟着主题重画自己的 `Static`。子类实现 `render_state`。"""

    def show(self, state: view_state.ViewState, palette: theme_mod.Theme,
             payload: Any = None) -> None:
        self._state = state
        self._payload = payload
        self.update(self.render_state(state, palette, payload))

    def render_state(self, state: view_state.ViewState, palette: theme_mod.Theme,
                     payload: Any = None) -> Text:
        raise NotImplementedError

    def repaint(self, palette: theme_mod.Theme) -> None:
        if getattr(self, "_state", None) is not None:
            self.update(self.render_state(self._state, palette, self._payload))


class TwoPart(Horizontal):
    """一条上下栏：左段 + 右段（右对齐）。

    右对齐不能靠一段 `Text` 做到（rich 的文本没有"靠右"），所以它是两个控件 ——
    而这也是 F1/F2/F5 三张图里那三条栏的共同形状。
    """

    def compose(self):
        yield Static("", classes="bar-left")
        yield Static("", classes="bar-right")

    def show(self, state: view_state.ViewState, palette: theme_mod.Theme,
             payload: Any = None) -> None:
        self._state = state
        self._payload = payload
        self._paint(palette)

    def _paint(self, palette: theme_mod.Theme) -> None:
        left, right = self.render_parts(self._state, palette, self._payload)
        self.query_one(".bar-left", Static).update(left)
        self.query_one(".bar-right", Static).update(right)

    def render_parts(self, state: view_state.ViewState, palette: theme_mod.Theme,
                     payload: Any = None) -> tuple[Any, Any]:
        raise NotImplementedError

    def repaint(self, palette: theme_mod.Theme) -> None:
        if getattr(self, "_state", None) is not None:
            self._paint(palette)


class TopBar(TwoPart):
    """顶栏：**这是哪个程序、在哪个工作区**。F1/F2 最高的那一条。

    右边那句 `Ctrl+K 命令面板` 是**提示而不是按钮**：终端里没有"可点的东西"，
    把键位写在它旁边是这块画布上唯一诚实的做法。
    """

    def render_parts(self, state, palette, payload=None):
        name = Text()
        name.append("● ", style=palette.accent)
        name.append("tudouni", style=palette.ink + " bold")
        name.append("  ·  agent_runtime", style=palette.ink3)
        # 窄屏（F5）右边**什么都不放**：那一行只剩名字，而工作区路径和
        # "命令面板怎么开"在窄屏上属于"挤掉别人才显示得出"的东西 ——
        # 工作区在欢迎屏和左栏都有，键位在下面那一行也有。
        if payload is not None and payload < view_state.NARROW_COLUMNS:
            return name, Text("")
        right = Text(state.workspace or "", style=palette.ink4)
        right.append(i18n.t("top.command_palette"), style=palette.ink4)
        return name, right


class SessionBar(TwoPart):
    """会话头：**这一轮谈的是哪个会话、哪个模型、预算多少**。

    右边那枚权限芯片**刻意不做成下拉**：界面按不动任何策略（控制面只有人能写，
    `auto_approve_tools` 也不经过 asker），一个长得像选择器却点不动的东西比直接
    写清楚更坏。

    **窄屏降级**（F5）：窄屏上只剩会话 id 和 `Ctrl+B` —— 权限那枚芯片的内容在
    下面那一行摘要里已经说了（`rail_summary`），而模型名和步数预算都写进过
    欢迎屏和左栏。挤在一起的结果是两边都被裁掉半句（实测：左边裁成
    "·  "，右边顶上来，看起来像坏了一条）。
    """

    def render_parts(self, state, palette, payload=None):
        narrow = payload is not None and payload < view_state.NARROW_COLUMNS
        left = Text()
        left.append("● ", style=palette.accent)
        left.append(i18n.t("session.bar.session",
                           name=state.session_id or i18n.t("session.bar.unnamed")),
                    style=palette.ink2)
        if state.resumed and not narrow:
            left.append(i18n.t("session.bar.resumed"), style=palette.ink4)
        if state.model and not narrow:
            left.append(f"  ·  {state.model}", style=palette.ink3)
        if state.max_steps and not narrow:
            left.append(i18n.t("session.bar.max_steps", n=state.max_steps),
                        style=palette.ink3)

        right = Text()
        if narrow:
            right.append(i18n.t("session.bar.rail_hint"), style=palette.ink4)
            return left, right
        asking = [item.get("risk", "") for item in state.risk_scope
                  if item.get("disposition") == "ask"]
        if not state.risk_scope:
            right.append(i18n.t("session.bar.no_permissions"), style=palette.ink4)
        elif asking:
            right.append(i18n.t("rail.summary.asking",
                                risks=i18n.t("list.separator").join(asking)),
                         style=palette.warn)
        else:
            right.append(i18n.t("rail.summary.all_auto"), style=palette.ok)
        right.append(i18n.t("session.bar.rail_hint_indent"), style=palette.ink4)
        return left, right


class StatusBar(TwoPart):
    """底部那一行：**agent 在干什么**（左）+ **模式指示灯 + 这一轮的成本**（右）。

    它是**投影**（见 `view_state.ViewState.status_left / status_right /
    autopilot_badge / quiet_badge`），不是第二份事实。`payload` 是界面的 wall clock ——
    "本轮 1.4s"要随秒走动，而那个数只能由界面自己数（纯函数里不取时间）；
    **安静模式下同一份时钟还决定转圈转到第几帧**（`view_state.spinner_frame`）。

    右边那一段**以两个空格开头**：左段是 `1fr`，内容长的时候会被裁到边界上，
    于是"第 3 / 30 步"和"自动放行 关"会挤在一起（实测：看不出这两段是两件事）。
    两个空格是这块画布上唯一的"栏间距"。

    **窄屏降级**：审计路径在 80 列上和左边撞车，所以窄屏只留前三个数
    （F5 那张图里状态栏右边就只剩用量）；autopilot 那一格**保留但缩成两个字** ——
    它不是"成本"，而是"接下来还会不会问你"，窄屏也不该把它丢掉。
    """

    def render_parts(self, state, palette, payload=None):
        # 那个记号是**界面自己造的"转圈"**（没有流式，一次往返是秒级）：它的颜色
        # 跟着 phase 走，所以"在跑 / 答完了 / 被砍断"一眼能分开 —— 而文字部分
        # 一律是次级正文色，免得整行都在喊。
        #
        # **安静模式下它会真的转起来**（`spinner_frame`）：那时候工具行和思考行都只
        # 有一行，这一格是屏幕上唯一在动的东西，而"它在想"和"它卡死了"必须分得开。
        # 非安静模式的回合里一个字都不改（`spin` 是空串，记号照旧是那个静态的 `●`）
        # —— **唯一的例外是启动态**（`state.booting`），见下面 `spinner_on`。
        #
        # `payload` 的第三个元素是"现在轮到人了"（审批/提问面板压在最上面）：
        # 那时候**停下**（`app._waiting_for_human`）—— agent 已经停在那儿等你回话了。
        # 第四个元素是"启动态超时了没有"（`app._BOOT_SLOW_SECONDS`）：它只换文案，
        # 转圈照转（见 `status_left` 的 docstring）。
        now, width, *rest = payload if payload else (None, None)
        waiting = bool(rest and rest[0])
        boot_slow = bool(len(rest) > 1 and rest[1])
        narrow = width is not None and width < view_state.NARROW_COLUMNS
        # **启动态也要转**（`state.booting`），不只是安静模式下的回合：那 1.9 秒里
        # 屏幕上如果没有东西在动，这块画布看起来就是卡死的 —— 而它其实正在忙。
        spinner_on = (state.quiet or state.booting) and now is not None and not waiting
        spin = view_state.spinner_frame(now) if spinner_on else ""
        left = Text()
        mark, _, rest = state.status_left(spin, boot_slow).partition(" ")
        left.append(mark, style=_phase_color(palette, state.agent.phase))
        left.append(f" {rest}", style=palette.ink2)

        # autopilot 那一格**自己一档色**（开着是 `warn`），所以它不能并进下面那条
        # 单色的字符串里 —— 走 `paint()` 这个"行 → Text"的唯一出口，别在这里
        # 手写第二份角色到颜色的映射。
        right = Text("  ")
        right.append_text(paint(palette, state.autopilot_badge(narrow)))
        # 安静模式那一枚**只在开着时出现**（`quiet_badge` 返回 None 就是"关着"）：
        # 它和 autopilot 那一格的取舍不同，理由写在 `view_state.quiet_badge` 里 ——
        # 于是关掉它的时候，这一行和加这个开关之前一个字符都不差。
        quiet_badge = state.quiet_badge()
        if quiet_badge is not None:
            right.append("  ·  ", style=palette.ink4)
            right.append_text(paint(palette, quiet_badge))
        # 后台任务那枚徽标**只在真有东西悬着时出现**（`jobs_badge` 返回 None 就是
        # 一格都不占）—— 所以平时这一行和加这个功能之前一模一样。
        #
        # 它接在 autopilot 后面、成本那一段前面：左边三格是"接下来还会不会问你 /
        # 你机器上还挂着什么"，右边那一长串是"这一轮花了多少"。前两者是**状态**，
        # 后者是**账**，混在一起读不出层次。
        badge = state.jobs_badge(narrow)
        if badge is not None:
            right.append("  ·  ", style=palette.ink4)
            right.append_text(paint(palette, badge))
        right.append("  ·  " + state.status_right(now, compact=narrow),
                     style=palette.ink4)
        return (left, right)

    def repaint(self, palette: theme_mod.Theme) -> None:
        # 秒数在变，所以重画之前得重算一次（`render_parts` 会读 payload）。
        super().repaint(palette)


# 键位提示：**这是设计稿 F6「交互键位」那张表里最常用的几条**。它现在画在欢迎屏
# 下面那个「提示」框里（`HintPanel`），而 `/help` 列的是同一份数据 —— 两处各写一遍
# 的话，"改了键位表、忘了改提示"就是必然。
#
# **顺序本身就是优先级**：`Esc` 排在 `Ctrl+S` 前面，因为前一个是"有东西要停下来"，
# 后一个是可以另找入口的（输入 `/skills`）。
# 窄屏那一版用更短的措辞：那几列连"思考过程"四个字都嫌长。
#
# **它们不能在模块级调 `i18n.t()`**：那会把语言冻在 import 那一刻。所以这里存的是
# 键，取值的口子见 `hint_keys()` —— 那个函数才是这两张表的读者。
HINT_KEYS_FULL_KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "hint.enter"), ("/", "hint.slash"),
    ("Ctrl+T", "hint.thinking"), ("Ctrl+B", "hint.rail"),
    ("Esc", "hint.escape"), ("Ctrl+S", "hint.skills"),
    ("Ctrl+K", "hint.palette"),
)
HINT_KEYS_NARROW_KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "hint.enter_short"), ("/", "hint.slash_short"),
    ("Ctrl+T", "hint.thinking_short"), ("Ctrl+B", "hint.rail_short"),
    ("Esc", "hint.escape_short"), ("Ctrl+S", "hint.skills_short"),
    ("Ctrl+K", "hint.palette_short"),
)
HINT_KEYS_EXTRA_KEYS: tuple[tuple[str, str], ...] = (
    ("Shift+Enter", "hint.shift_enter"),
    ("↑ ↓", "hint.arrows"),
)


def hint_keys(narrow: bool = False) -> list[tuple[str, str]]:
    """键位提示（`(键, 这一键干什么)`），按当前语言取好。

    **两张表共用这一个出口**：欢迎屏底下那个框按宽度挑窄/宽两版，`/help` 列的是
    宽版 + 那两条"框里放不下但必须列出来"的（`HINT_KEYS_EXTRA_KEYS`）。
    """
    keys = HINT_KEYS_NARROW_KEYS if narrow else HINT_KEYS_FULL_KEYS
    return [(key, i18n.t(text_key)) for key, text_key in keys]


def extra_hint_keys() -> list[tuple[str, str]]:
    """`/help` 要列、但欢迎屏那个框放不下的那两条。"""
    return [(key, i18n.t(text_key)) for key, text_key in HINT_KEYS_EXTRA_KEYS]


class LineBlock(Static):
    """一组行。**它是重画的单位**：一段连续同类文本一个控件，而不是一行一个。"""

    def __init__(self, lines: list[view_state.Line],
                 palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.lines = list(lines)
        self._palette = palette
        self.update(paint_lines(palette, self.lines))

    def append(self, lines: list[view_state.Line]) -> None:
        self.lines.extend(lines)
        self.refresh_text()

    def append_stream(self, text: str) -> None:
        """流式那一块：把新来的一段**接在上一段后面**（整块就两行：块头 + 这一段）。

        `text` 是**纯文本**（不含引用竖线），这一层只管画。

        为什么不是 `append([Line(text)])`（那样就是一行一块）：provider 吐思考链
        时一块往往只有一个词，于是界面上是**一个词一行**（实测：401 字符的思考过程
        竖着排了 100 多行，完全没法读）。这一块代表"一段还在长的话"。

        **接的是上一次剥掉竖线之后的那段文本**，所以这里不能读自己画出来的那一行
        ——它的开头是 `  │ `，直接接上去会让竖线越堆越多（实测：`  │   │   │ The`）。

        接头处**不补空格**：provider 的分块本身就带着它要的空格
        （"The" + " user" + " says"），自己补一个就会变成 "The  user"。
        块内部的换行由调用方压成空格（`view_state.stream_chunk_text` 是"哪些换行
        该丢"的知识，只有那一处）。
        """
        head = self.lines[:1]
        body = self.lines[1:]
        prefix = view_state.QUOTE_BAR
        merged = "".join(
            str(line)[len(prefix):] if str(line).startswith(prefix) else str(line)
            for line in body
        ) + text
        # **合并之后再压一次换行** —— 这一块是"一段话"，而 provider 每块都带换行
        # （不压的话它就是"接一条长文本"，末尾那个换行会把下一块顶到新的一行，
        # 画出来仍然是"一个词一行"，只是行更多了）。
        self.set_lines([*head,
                        view_state.quote_line(view_state.stream_chunk_text("think",
                                                                          merged))])

    def set_lines(self, lines: list[view_state.Line]) -> None:
        self.lines = list(lines)
        self.refresh_text()

    def set_line(self, index: int, line: view_state.Line) -> None:
        """把**第几行**换成另一个形态（安静模式的原地回填，见 `TurnBlock.replace_anchored`）。

        **只换那一行、整块重画**：这一块是连续的同类行（几个工具行常常并在一块里），
        整块重画的代价是重新拼一遍那几行 —— 而"只把那一行渲染出来再贴回去"要在这里
        维护一份和 `paint_lines` 重复的渲染路径，那才是真的贵（改了一处另一处不跟）。
        """
        self.lines[index] = line
        self.refresh_text()

    def refresh_text(self) -> None:
        self.update(paint_lines(self._palette, self.lines))

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        self.refresh_text()


class AnswerBlock(Markdown):
    """agent 的正文。**它是这一屏上唯一"有语法"的东西**，所以不走 `Line` 那条路。

    `LineBlock` 把每一行交给 `rich.text.Text`，而 `Text` 只认样式不认语法 —— 标题、
    列表、表格、代码块于是全都原样显示（"回复还是原始 MD"就是这么来的）。Textual 的
    `Markdown` 反过来：它把正文解析成**一棵子控件树**（`MarkdownParagraph` /
    `MarkdownH1` / `MarkdownFence` / `MarkdownTable` …），于是配色从"逐行猜角色"
    变成 CSS —— `app.py` 里那几条 `.answer Markdown*` 用的还是同一套 `$td-*` 变量，
    所以 `/theme` 换配色时它跟着变，**不需要** `repaint()` 那一套重画。代码块的语法
    高亮也是它自己做的（`MarkdownFence` 调 `textual.highlight`，并在主题变化时
    重高亮）。

    为什么**可以**这样用而不用担心代价：这一版没有流式（决策 1），一条答案只会解析
    一次；而解析出来的子控件数只和正文的块数有关，和字符数无关。真要做流式时该换的
    是 `Markdown.get_stream()`（Textual 自带），而不是把这里退回去逐行 `Text`。

    `open_links=False`：**模型输出里的链接不许自己拉起浏览器**。那是模型能直接触发的
    一个外部动作，而"点一下就把浏览器打开"这个后果不该由模型写的一行字决定。链接的
    文本在画面里是完整的，要看内容就复制走。
    """

    def __init__(self, text: str, palette: theme_mod.Theme, **kwargs: Any):
        self._palette = palette
        super().__init__(text, open_links=False, **kwargs)

    def repaint(self, palette: theme_mod.Theme) -> None:
        """`ConversationLog.repaint` 会挨个调过来，而这里**只需要记个账**。

        正文的配色全在 CSS 里（`$td-*` 由 Textual 的主题变量算出来），换主题时
        Textual 自己会重算；不像 `LineBlock` 那样要拿新配色再画一遍。它存在的唯一
        理由是"块协议要一致"—— 少了它，`/theme` 会在正文这一块上抛 `AttributeError`。
        """
        self._palette = palette


class StreamAnswerBlock(Markdown):
    """**正在流的那一段正文。** 和 `AnswerBlock` 共用 CSS 和主题，但走另一条路。

    为什么不是"和 `AnswerBlock` 合并成一个类、加一个 stream 方法"：两者的
    **记账方式根本不同**。`AnswerBlock` 是"整篇一次解析"（构造时把全文交给
    Textual，之后不再变），而这里每一块 delta 都要重新解析一次。

    ## 为什么是 `update()` 而不是 `append()`（实测踩过，两次）

    Textual 的 `Markdown.append()` 看起来正是为流式准备的（"接着上次解析到的地方
    往下解析"），但它有两个在这条路上都会命中的问题：

      1. **控件还没进 DOM 时它会抛 `MountError`。** `append()` 立刻在事件循环上排
         一个任务，任务里有 `mount_all`；而 `TurnBlock` 的块是"先记账、挂载排队"
         的（见 `_mount_chunks`），所以第一块内容几乎必然比挂载先到。那个异常发生
         在一个已经排出去的后台任务里，报出来是一组 `ExceptionGroup`，
         栈上完全指不到"是哪一块正文"（实测：pytest 用
         `Multiple exceptions occurred in asynchronous callbacks` 收场）；
      2. **它和 `_on_mount` 会打架。** `Markdown._on_mount` 里有
         `await self.update(initial_markdown or "")`，而 `update()` 会
         `self._markdown = markdown`。于是"挂载前 append 进去的内容"会被这一句
         **清成空串** —— 症状是流式正文只显示最后一块（实测：`_markdown` 里
         只剩后半个片段，前半个连痕迹都没有）。

    `update()` 没有"和 `_on_mount` 打架"这个问题（它自己管 `_markdown` 和旧子控件的
    清理），代价是**每一块都把整段重新解析一遍** —— 那是 O(正文长度)，而它本来就在
    50ms 一条的节奏上（App 的消息泵），对一次几千字的回答是毫秒级。

    ## 三条"必须等挂载"的实测坑

    `Markdown.update()` 也不是随便什么时候都能调，三件事各踩过一次：

      1. **挂载之前调** → 它排出去的那个任务里有一句 `mount_all`，而控件还没进
         DOM：`MountError: Can't mount widget(s) before … is mounted`，
         报出来是一组 `ExceptionGroup`（所以 `_push` 里有 `is_attached` 那道闸）；
      2. **`is_attached` 为真之后立刻调** → `mount()` 之后它马上就是真的，而挂载
         消息还没处理完，`Markdown._on_mount` 会用空文档把内容盖掉 ——
         所以 `TurnBlock._mount_chunks` 用 `call_after_refresh` 补写一次。

    ## `_push` 这个名字是必须的，它原来叫 `_render`

    改名之前那一版叫 `_render`，**正好盖住了 `Widget._render`** —— 而那一个是
    Textual 的钩子："把我 `render()` 的返回值变成 Visual"。盖住它的后果是
    `Widget._render_content` 拿到本方法的 `None` 去 `Visual.to_strips()`，抛
    `'NoneType' object has no attribute 'render_strips'`。

    原作者当时用一个 `BLANK = True` 绕开了那个异常（`render_lines` 在 `BLANK` 时
    压根不走那条路），代价是**连控件自己的边框一起不画** —— 于是 `.answer` 左边那条
    竖线在这个块上从来就没画出来过。而这一块恰好是**默认**那条路（TUI 默认开流式），
    症状是"同一段回答，非流式有竖线、流式没有"，看起来只像"这一版就是没画"。

    所以现在：名字让开（不给 Textual 的钩子添乱），`BLANK` 也去掉 —— 块照旧画
    自己那一趟（背景、边框、然后子控件在上面），左边那条竖线就有了。

    `_text` 是**唯一的累计**（不从控件里读）：它同时是"有没有东西要作废"的判据
    （见 `TurnBlock.discard_stream`）。
    """

    def __init__(self, palette: theme_mod.Theme, **kwargs: Any):
        self._palette = palette
        self._text = ""
        super().__init__(None, open_links=False, **kwargs)

    def feed(self, text: str) -> None:
        """追加一块。**空串直接丢**（白跑一遍解析）。"""
        if not text:
            return
        self._text += text
        self._push()

    def flush(self) -> None:
        """把当前累计写进文档（幂等）。**没挂上时是空操作**（见 `_push`）。"""
        self._push()

    def _push(self) -> None:
        """把累计交给 Markdown。**没挂上就什么都不做**（挂载前调 `update()` 会抛）。"""
        if self._text and self.is_attached:
            self.update(self._text)

    @property
    def streamed(self) -> bool:
        return bool(self._text)

    def repaint(self, palette: theme_mod.Theme) -> None:
        """和 `AnswerBlock.repaint` 同一条规矩：配色在 CSS 里，这里只记个账。"""
        self._palette = palette


class TurnBlock(Vertical):
    """一个回合：**一条头 + 若干块正文**。

    这是 v1 到 v2 最大的一处改动（设计稿的改动 7）：`RichLog` 是"只追加的日志"，
    而这里要的是"可折叠的结构化区块" —— 回合头要能被 `run_finished` 改写成最终
    形态，思考块要能单独展开。只追加的语义支持不了这两件事。

    正文按"块"组织：连续的过程行合成一个 `LineBlock`，而思考正文单独一块
    （它有自己的底色 `sunk`）。块的数量在一次回合里很少（几个），所以"追加时只重画
    最后一块"就够了。

    **子控件在 `__init__` 里造好并直接持有引用，不走 `compose()`** —— 因为 App 会在
    `mount()` 之后**同一次事件里**就往里追加行，而那时候 `compose()` 还没跑
    （Textual 的 mount 是排队的）。这个坑的症状是偶发 `NoMatches`。
    """

    def __init__(self, turn: view_state.Turn, palette: theme_mod.Theme,
                 *args: Any, **kwargs: Any):
        self.head_line = view_state.Line(i18n.t("turn.head", index=turn.index),
                                         view_state.ROLE_TURN_START)
        head = Static("", classes="turn-head")
        body = Vertical(classes="turn-body")
        super().__init__(head, body, *args, **kwargs)
        self.turn = turn
        self._palette = palette
        self._head = head
        self._body = body
        self.chunks: list[dict[str, Any]] = []
        self.refresh_head()

    def refresh_head(self) -> None:
        """画回合头：`回合 1 ─────────────────── 3 步 · 4.2s · 已答`。

        **那条横线是自己算出来的一串 `─`**，不是 `border-bottom`：`border-bottom`
        要在控件底部占掉一整行（`height: 1` 时 Textual 干脆不画它），那样每个回合
        的头会吃掉两行；而设计稿 F1 里横线和文字是**同一行**的。

        宽度取自控件自己的 `size.width`（= 会话流的正文宽），所以重新布局时要重画
        —— `on_resize` 干的就是这件事。算错一格也不会把行撑成两行：头上的
        `text-wrap: nowrap` 会让它裁掉而不是折行。

        **横线用 `accent`**（输入框上下那两条线、欢迎屏三个框的边框也是它）：这一屏上
        "结构线"是同一类东西，同一个颜色才像一套。原先用 `hairline`，它在底色上几乎
        看不出是条线 —— 于是回合之间只剩一个空行，而空行分不出"上一轮结束了"和
        "这里碰巧多了一行"。
        """
        left, right, right_role = view_state.turn_head_parts(self.head_line)
        palette = self._palette
        width = self.size.width or 0
        gap = max(2, width - Text(left).cell_len - Text(right).cell_len - 3)
        text = Text()
        text.append(left, style=style_of(palette, self.head_line.role))
        text.append(" ")
        text.append("─" * gap, style=palette.accent)
        if right:
            text.append(" ")
            text.append(right, style=style_of(palette, right_role))
        self._head.update(text)

    def on_resize(self) -> None:
        # 宽度变了，横线的长度得跟着变（它是算出来的，不是布局给的）。
        self.refresh_head()

    def set_head(self, line: view_state.Line) -> None:
        """`run_finished` 把"进行中 · 第 1 步"改写成"3 步 · 4.2s · 已答"。"""
        self.head_line = line
        self.refresh_head()

    def add(self, lines: list[view_state.Line]) -> None:
        """追加行。思考正文单独成块（`sunk` 底），其余按顺序进同一个流。"""
        pending: list[view_state.Line] = []
        for line in lines:
            if line.role == view_state.ROLE_THINK_BODY:
                if pending:
                    self._append("plain", pending)
                    pending = []
                self._append("think", [line])
            else:
                pending.append(line)
        if pending:
            self._append("plain", pending)

    def add_answer(self, text: str) -> None:
        """agent 正文：**整块交给 `AnswerBlock`**，不拆成行、也不和后一块合并。

        `_append` 那条"同类相邻就并进最后一块"的规则在这里**刻意不适用**：Markdown
        是整篇一次解析的，两条答案塞进同一个控件就会连成一段（前一条的列表会被后一条
        的标题接管，而画面看起来只是"排版有点怪"）。一次 `run_finished` 一条答案，
        所以一块一条正好。
        """
        block = AnswerBlock(text, self._palette, classes="answer")
        self.chunks.append({"kind": "answer", "block": block})
        self._mount_chunks()

    # -- 流式（`t:"delta"` / `t:"delta_reset"`） --------------------------------

    def add_stream(self, kind: str, line: view_state.Line) -> None:
        """往里追加一块流式内容。**带块身份的那一行**由 `view_state.stream_lines` 算好。

        四条约定：

          1. **块头只画一次**（`● ` / `▸ 思考过程`）：判据是"这一回合里已经有这种块了
             吗"。放在这里而不是调用方，是因为调用方每收到一块才调一次 —— 它没有
             "以前有没有过"这份记忆，为它维护一份就是第二份事实；
          2. `kind == "answer"` 走 `StreamAnswerBlock`（Markdown），`think` 走
             `LineBlock`（带 `sunk` 底）。两者在 `chunks` 里的记账方式一样，所以
             `_mount_chunks` / `repaint` / `set_head` 那些都不用分情况；
          3. **只往"紧挨着的同种块"追加**：模型在调用工具之前说的那句话会先流出来，
             工具行随后插进来 —— 工具行之后若又来一段正文（下一步的），那是新的一块，
             不该接在旧的那段后面（否则 Markdown 会把两段当一篇解析，列表和标题
             会互相接管）；
          4. **思考链是"一段"而不是"一堆行"**：它每来一块就**整段重写那一块**
             （累计 + 压平换行，见 `view_state.stream_chunk_text`）。逐块
             `append()` 的写法在这里是错的 —— provider 一块往往只有一个词，
             于是界面上是**一个词一行**（实测：401 字符的思考过程竖着排了 100 多行）。
             重写是 O(这一段的长度)，而它在 50ms 一拍的消息泵里。
        """
        block = self.chunks[-1] if self.chunks else None
        same = block is not None and block["kind"] == kind
        # 正文块还得是**流式**那一版：`AnswerBlock` 是"整篇一次画"的，喂不了。
        same = same and (kind != "answer"
                         or isinstance(block["block"], StreamAnswerBlock))

        if same:
            if kind == "answer":
                block["block"].feed(str(line))
            else:
                block["block"].append_stream(str(line))
            return

        if kind == "answer":
            new_block = StreamAnswerBlock(self._palette, classes="answer")
            # **块头进 Markdown 源文，不进一行 `Line`。** 非流式那边的 `● ` 是
            # `render_event` 画的一行，而这里不能那么做：Markdown 的行内语法是成对的，
            # 流到一半时 `**` 还没闭合是常态 —— 把那一段和 `● ` 拼在同一行，
            # 记号会被当成语法的一部分（`**` 吞掉后面的 ` ` 之类），看起来像"记号
            # 自己会乱跳"。所以记号独占一行，反引号围起来（免得它自己被解析）。
            new_block.feed(f"`{view_state.stream_head('text')[0].strip()}`\n{line}")
        else:
            head_text, head_role = view_state.stream_head("reasoning")
            new_block = LineBlock(
                [view_state.Line(head_text, head_role), view_state.quote_line(str(line))],
                self._palette, classes="think-body",
            )
        self.chunks.append({"kind": kind, "block": new_block})
        self._mount_chunks()

    def close_stream(self, kind: str, text: str) -> bool:
        """一轮结束了：把还在流的那一块**收成折叠形态**。返回"真收了吗"。

        只对思考链有意义：它流的时候是铺开的正文（那是"它正在想"的观感），
        而一轮结束之后该回到和非流式那条路一样的形态 —— 一行
        `▸ 思考过程（N 字符 · Ctrl+T 展开）`（决策 17：默认折叠）。

        **收成折叠形态而不是留着铺开的那一大段**，有两个具体理由：

          * 非流式那一轮的思考过程就是折叠的，两种模式在屏幕上的结果必须一致
            —— 否则"开了流式"就变成"每一轮的思考过程都糊在脸上"；
          * `Ctrl+T` 的判据是那一块**有没有 `ROLE_THINK_HEAD` 那一行**
            （`toggle_thinking` 里按 role 找），而流式那块的行全是 `THINK_BODY`。
            不收的话，展开键会从那一块上滑过去、去动后面那个审计块。

        收完把块的 kind 改成 `plain`：它从此不再是"正在流的东西"，`Ctrl+T` 展开它、
        `discard_stream` 也不再碰它（一次重试不该把已经收好的思考过程抹掉）。
        """
        for chunk in reversed(self.chunks):
            if chunk["kind"] != kind:
                continue
            if not isinstance(chunk["block"], LineBlock):
                return False
            chunk["block"].set_lines([view_state.folded_thinking(text)])
            chunk["kind"] = "plain"
            return True
        return False
    def discard_stream(self, kind: str) -> bool:
        """把**最后一块**这种流式块整块拿掉。返回"真拿掉了没有"。

        重试 / 重发之前的那一声（`t:"delta_reset"`）走这里：界面上那半截是错位的，
        而且**它不会进历史**（半截正文从不落盘），所以留着它就是留一个"屏幕上有、
        恢复会话时查无此物"的东西。

        **只动最后一块、且只动流式那一版。** 两个理由：
          * 前几步已经定下来的内容不在重试范围内，抹掉它是在伪造历史；
          * `AnswerBlock`（整篇一次画的那个）代表"这一轮已经收尾的正文"，
            拿掉它之后 `ui(run_finished)` 不会再补一份，那就是丢内容。
        """
        if not self.chunks:
            return False
        last = self.chunks[-1]
        if last["kind"] != kind:
            return False
        if kind == "answer" and not isinstance(last["block"], StreamAnswerBlock):
            return False
        self.chunks.pop()
        block = last["block"]
        if block.is_attached:
            block.remove()
        return True

    def _append(self, kind: str, lines: list[view_state.Line]) -> None:
        """同一类连续的行并进最后一块，否则开一块新的。

        **块先记账、DOM 挂载可以晚一点**：`mount()` 是排队的，而 App 会在
        `start_turn()` 之后同一次事件里就追加这一轮的头几行 —— 那时候这个回合块
        自己还没挂上（`self._body` 还不是 attached 状态），直接 `mount` 会抛
        `MountError: Can't mount widget(s) before … is mounted`。
        所以挂载统一走 `_mount_chunks()`：挂了就挂，没挂就等 `on_mount` 那一趟。
        """
        if self.chunks and self.chunks[-1]["kind"] == kind:
            self.chunks[-1]["block"].append(lines)
            return
        block = self._make_block(kind, lines)
        self.chunks.append({"kind": kind, "block": block})
        self._mount_chunks()

    def _mount_chunks(self) -> None:
        """把还没挂上的块按顺序挂上去。

        **判据是"这块挂了没有"，不是"挂了几块"**：`_split`（展开思考）会把一块换成
        三块，那时候用计数维护"挂到第几块"会算错，而算错的症状是重复挂载同一个
        控件（Textual 会把它挪走）—— 那是"思考展开之后正文顺序乱了"这种很难看的 bug。
        """
        if not self._body.is_attached:
            return
        for chunk in self.chunks:
            if not chunk["block"].is_attached:
                self._body.mount(chunk["block"])
        # **挂上之后要把流式块的内容补写一次，而且要在挂载真的走完之后。**
        # 两个坑叠在一起：
        #
        #   1. 挂载之前调 `Markdown.update()` 是不行的 —— 它会立刻在事件循环上排
        #      一个 `mount_all` 任务，而那时候控件还没进 DOM，抛
        #      `MountError: Can't mount widget(s) before … is mounted`
        #      （那个异常发生在已经排出去的任务里，报出来是一组
        #      `ExceptionGroup`，栈上完全指不到是哪一块正文）；
        #   2. `is_attached` 在 `mount()` 之后**立刻**就是 True，而挂载消息这时候
        #      还没被处理，`Markdown._on_mount` 也还没把文档初始化成空串 ——
        #      在这里写会被它盖掉。
        #
        # `call_after_refresh` 落在下一次屏幕刷新之后，正好越过这两条。
        for chunk in self.chunks:
            flush = getattr(chunk["block"], "flush", None)
            if flush is not None:
                self.call_after_refresh(flush)

    def on_mount(self) -> None:
        self._mount_chunks()

    def _make_block(self, kind: str, lines: list[view_state.Line]) -> LineBlock:
        return LineBlock(lines, self._palette,
                         classes="think-body" if kind == "think" else "turn-text")

    # -- 思考折叠 --------------------------------------------------------------

    def toggle_thinking(self, text: str) -> bool:
        """把**这一块**里的思考换成展开形态（或反过来）。返回"动了没有"。

        设计稿的第 4 条改动：v1 的 `Ctrl+T` 只认最后一段（`list(state.thinking)[-1]`），
        而回合是分块的 —— 那个写法从第二回合起就会作用到错的那一段上，而画面看起来
        完全正常（这一类 bug 最难查）。现在作用对象由调用方按**光标所在回合**算出来，
        这里只负责画。

        展开时那个折叠行往往**和别的行挤在同一块里**（模型行、工具行都跟着它），
        所以这里把那一块**裂成三块**：之前的、思考正文、之后的 —— 否则思考正文会
        占着"没有底色"的那一块，而它该有自己的 `sunk` 底。
        """
        for position, chunk in enumerate(self.chunks):
            if chunk["kind"] == "answer":
                # 正文块**不是 `LineBlock`**（它没有 `.lines`），也不是折叠的对象 ——
                # 少了这一句，按 `Ctrl+T` 会在这里抛 AttributeError，而界面看起来只是
                # "思考没展开"。
                continue
            block: LineBlock = chunk["block"]
            if chunk["kind"] == "think":
                # 展开着的思考正文：整块收掉，换成一行折叠提示。
                chunk["kind"] = "plain"
                block.set_classes("turn-text")
                block.set_lines([view_state.folded_thinking(text)])
                return True
            index = next((i for i, line in enumerate(block.lines)
                          if line.role == view_state.ROLE_THINK_HEAD), None)
            if index is None:
                continue
            lines = block.lines
            pieces: list[tuple[str, list[view_state.Line]]] = []
            if lines[:index]:
                pieces.append(("plain", lines[:index]))
            pieces.append(("think", [
                view_state.expanded_thinking_head(),
                *view_state.thinking_body(text),
            ]))
            if lines[index + 1:]:
                pieces.append(("plain", lines[index + 1:]))
            self._split(position, pieces)
            return True
        return False

    def _split(self, position: int,
               pieces: list[tuple[str, list[view_state.Line]]]) -> None:
        """把第 `position` 块换成 `pieces` 里那几块（顺序不变）。"""
        old = self.chunks[position]
        new_chunks = [{"kind": kind, "block": self._make_block(kind, lines)}
                      for kind, lines in pieces]
        self.chunks[position:position + 1] = new_chunks
        if self._body.is_attached and old["block"].is_attached:
            self._body.mount(*[chunk["block"] for chunk in new_chunks],
                             before=old["block"])
            old["block"].remove()
        else:  # pragma: no cover - 只有"还没挂上就折叠"才会走到
            self._mount_chunks()

    def has_thinking(self) -> bool:
        """这一回合里有没有思考过程（折叠着的算，展开着的也算）。

        **正文块要跳过**：它没有 `.lines`（见 `toggle_thinking` 里同一条判据）。
        """
        return any(
            line.role == view_state.ROLE_THINK_HEAD or chunk["kind"] == "think"
            for chunk in self.chunks if chunk["kind"] != "answer"
            for line in chunk["block"].lines
        )

    # -- 原地回填（安静模式）---------------------------------------------------

    def replace_anchored(self, line: view_state.Line) -> bool:
        """把新来的这一行**顶掉它认的那一行**。返回"换掉了吗"。

        判据全在 `view_state.same_anchored_line`（纯函数：身份 + 方向），所以这里不认识
        "工具"和"思考" —— 它只负责在一堆行里找那一行、把 `merge_anchored` 拼好的
        结果放回去。**从后往前找**：同一批里两条一样的调用（`read_file a.py` 两次）
        身份不同（`call_id` 不同），但"最近一条还没结果的"才是对的那一条。

        **正文块要跳过**：它没有 `.lines`（和 `has_thinking` 同一条判据）。
        """
        for chunk in reversed(self.chunks):
            if chunk["kind"] == "answer":
                continue
            block: LineBlock = chunk["block"]
            for index, old in enumerate(block.lines):
                if view_state.same_anchored_line(line, old):
                    block.set_line(index, view_state.merge_anchored(old, line))
                    return True
        return False

    def upsert_anchored(self, line: view_state.Line) -> None:
        """有就换掉、没有就追加。

        **安静模式下思考那一行从无到有要走这条**：第一块思考链到达时才画出那一行
        （此前屏幕上没有它 —— 模型可能一步都不吐思考），而它的每一次"换帧"都必须是
        **换**，不是接着追加（追加会一行一行堆起来，那正是安静模式要消灭的东西）。
        """
        if not self.replace_anchored(line):
            self.add([line])

    def finish_thinking(self, anchor: str, text: str) -> bool:
        """一轮收尾：把还在长的那一行思考**定格**成折叠形态（转圈停下）。

        `anchor` 是 `run_id`：还在长的那一行和收尾后的这一行是同一行的两种形态
        （`same_anchored_line` 靠身份 + 方向认出来）。找不到那一行（非安静模式、
        或者用户中途按 `Ctrl+T` 把它展开成了正文块）就返回 False，由调用方走
        `close_stream` 那条老路。
        """
        return self.replace_anchored(
            view_state.folded_thinking(text, anchor=anchor))

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        self.refresh_head()
        for chunk in self.chunks:
            chunk["block"].repaint(palette)


class MessageBlock(Vertical):
    """不带回合的行（开场问候、恢复会话的说明、界面自己的提示）。

    它和 `TurnBlock` 分开是因为**这些行不属于任何一轮**：把它们塞进一个回合块，
    回合头就会说谎（"回合 1 · 已答"里混着"（恢复 12 条历史）"）。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._palette = palette
        self._chunks: list[LineBlock] = []
        # 正文块和行块**分开记**：重画时要区别对待（行要重画、正文不用），而
        # "这一块是什么"只有这里知道 —— 靠类型去 `self.children` 里筛会在挂载
        # 还没排完的时候漏掉刚加的那一块。
        self._answers: list[AnswerBlock] = []

    def add(self, lines: list[view_state.Line]) -> None:
        if not lines:
            return
        if self._chunks:
            self._chunks[-1].append(lines)
            return
        block = LineBlock(lines, self._palette, classes="turn-text")
        self._chunks.append(block)
        self.mount(block)

    def add_answer(self, text: str) -> None:
        """没有回合可归的正文（少见，但不留一条会掉内容的缝）。"""
        block = AnswerBlock(text, self._palette, classes="answer")
        self._answers.append(block)
        self.mount(block)

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        for block in self._chunks:
            block.repaint(palette)
        for answer in self._answers:
            answer.repaint(palette)

    @property
    def empty(self) -> bool:
        return not self._chunks and not self._answers


class BorderedPanel(Static):
    """一个**带标题的方框**：`╭─ 标题 ─────────╮`。

    标题走 Textual 的 `border_title`，不自己在正文里拼一行 `── 标题 ──`。区别是
    具体的：`border_title` 画在**边框那一行**上，所以"这个框叫什么"不占内容行；
    而自己拼一行的话，字和线各画一遍，框里框外两套线宽，早晚对不齐。

    `render_parts()` 回 `(标题, 正文)`，正文里的每一行是一个独立 `Static` ——
    这样每一行可以有自己的对齐方式（方块标居中、最近活动那两列左右分开），而不是
    在同一个 `Text` 里靠空格凑（数空格在宽字符上必错）。

    **它继承 `Static` 而不是 `Panel`**：`Panel.show()` 是把整份内容塞进一次
    `update()`，而这里要的是"若干个子行"。重复用 `update()` 还会把刚挂上去的子控件
    挤掉。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        # 配色是**显式参数**（和这个文件里别的控件一样，见模块 docstring）：
        # 谁都不去 `self.app.palette`。
        super().__init__(*args, **kwargs)
        self._palette = palette
        # 上一次画的时候这个框有多宽（`HintPanel` 要按它决定提示怎么折行）。
        self._width = 0

    def show_children(self, state: view_state.ViewState, palette: theme_mod.Theme,
                      payload: Any = None, *, now: float | None = None,
                      width: int = 0) -> None:
        self._state = state
        self._payload = payload
        self._now = now
        self._width = width
        # 重画（换主题 / 窗口宽度变了）也要用当前这套配色，所以它得留一份。
        self._palette = palette
        title, rows = self.render_parts(state, palette, payload, width)
        self.border_title = title
        self.remove_children()
        self.mount(*rows)

    def render_parts(self, state: view_state.ViewState, palette: theme_mod.Theme,
                     payload: Any = None, width: int = 0) -> tuple[Text, list[Static]]:
        raise NotImplementedError

    def repaint(self, palette: theme_mod.Theme) -> None:
        if getattr(self, "_state", None) is not None:
            self.show_children(self._state, palette, self._payload, now=self._now,
                               width=self._width)

    def on_resize(self) -> None:
        """宽度变了就重画 —— 这几行的省略号位置是按当前列数算的。"""
        self.repaint(self._palette)


# 两个方框的几何。**数字必须和 `app.py` 的 CSS 对上**（`.start-box` / `.recent-box`
# 的 `width`、`.welcome-box` 的 `height`），所以它值得一条测试：对不上的症状只是
# "框里多/少一条空行"，没有任何报错，也没人会去查是哪儿错了。
#
# 两个框的**内容都是 `WelcomeBlock.BOX_LINES` 行**，框高 = 那些行 + 上下 padding 各 1
# + Textual 给边框留的 2 行 = `10 + 2 + 2 = 14`。少一行就会在框底裁掉一行，而画面上
# 看起来只是"框里少了一行字"。
#
# **这里原先写的是 12** —— 那条算式当时把 padding 那两行漏掉了（`10 + 2` 被当成了
# `10 + padding + 边框`），而实测的症状正好是"内容区只有 8 行、装不下 10 行正文"，
# 一条测试因此红着（见 `.welcome-box` 那条 CSS 里的同一个数）。
WELCOME_BOX_HEIGHT = 14
# 右栏那个方框的内容宽（列）：`42(框宽) - 2(边框) - 2(padding)`。`_recent_row` 靠它
# 把"多久以前"和"标题"分成两列，所以它和 CSS 里那个 42 是一对。
WELCOME_RIGHT_WIDTH = 38
# 窄于这么多列就**把两个框叠起来**（并排时每一半只剩二十几列，标题就全被省略号吃掉
# 了）。两个框并排要 `32 + 1 + 42 + 1 + #log 自己的左右 padding` = 78 列，取 86 是留了
# 一点余量。**并排是常态**：叠起来那一版只是"终端实在放不下"时的退路。
WELCOME_STACK_COLUMNS = 86
# 底下那个「提示」框的宽度（列）：**上面两个框加起来**（`32 + 1 间距 + 42`）。它和
# CSS 里 `.hint-box` 的宽度是一对，有一条测试盯着。
WELCOME_HINT_WIDTH = 75
# 提示框里的**文字宽**（`75 - 2(边框) - 2(padding)`）：`HintPanel` 按它决定一行放
# 几条键位。
WELCOME_HINT_TEXT = WELCOME_HINT_WIDTH - 4

# --- 提示框那一行怎么排：**两种语言不一样** --------------------------------
#
# 中文 4 条一行正好两行（每条约 17 列），而英文那几条长得多（`Esc Interrupt this
# turn` 一条就 22 列）—— 4 条一行会**折成四行**，而框只有两行高，多出来的会被
# Textual 静默裁掉（没有报错、没有滚动条，看起来只是"提示框里少了几条"）。那正是
# `.welcome-box` 那条注释里记过的同类事故。
#
# 所以英文：一行 3 条（最宽那三种组合量下来 ≤ 69 列，文字宽 71），三行放完 7 条，
# 框高留到 6（= 4 行正文 + 上下边框）—— 比需要的多一行余量，防的是以后某条文案
# 再长一点。
HINT_PER_ROW = 4
HINT_PER_ROW_EN = 3
HINT_BOX_LINES = 2
HINT_BOX_LINES_EN = 4


def hint_per_row() -> int:
    """提示框一行放几条键位（按当前语言）。"""
    return HINT_PER_ROW_EN if i18n.current() == i18n.EN else HINT_PER_ROW


def hint_box_height() -> int:
    """提示框有多高。**Textual 会从 `height` 里扣掉边框那两行**，所以它是"正文行数
    + 2"（见 `.hint-box` 那条 CSS 里同一个数）。"""
    lines = HINT_BOX_LINES_EN if i18n.current() == i18n.EN else HINT_BOX_LINES
    return lines + 2


class WelcomeBlock(Vertical):
    """空态：**新会话还没说第一句话时那一屏**。

    它不是装饰。一个空白的会话区会让人以为程序没起来，而这里说的三件事
    （这是哪个程序、在哪个工作区/用什么模型、最近动过什么）恰好是"第一次打开它"
    时唯一需要知道的。

    ## 形状：**上面两个方框并排，下面一个通栏的「提示」**

    `╭─ 开始 ──╮ ╭─ 最近 ──╮`：左边是身份（方块标 + 名字与版本 + 模型与工作区），
    右边是"最近动过哪几个会话"和一句箴言；底下那个「提示」横跨上面两个框的宽度，
    里面是键位提示（它原先画在输入行下面那一行，挪进来的理由见 `HintPanel`）。
    **并排是常态**，只有终端窄到 `WELCOME_STACK_COLUMNS` 以下才把上面两个框叠起来。

    **右栏那两块的位置是固定的，不管有没有内容**：没有会话时那里写着"（还没有会话）"。
    这是刻意的 —— 一个"有内容才出现"的区块会让每次启动的版式都不一样，而"今天这里
    为什么少了一块"是没人愿意去查的问题。

    `payload` 是版本号。左边那个方块标是自绘的：终端里没有图片，而一屏空态没有
    任何视觉重量时，"这是哪个程序"这句话就得靠这几行实心块来承担。
    """

    # 三行的小标记。**只用半块和全块字符**（▄▀█）：它们在等宽字体里都是"一个字符
    # 宽"的实心格，不会像有些图形字符那样在 CJK 字体下变成双宽而把版式顶歪。
    # 形状是个上窄下窄、中间鼓的方块（像一坨土豆泥），三行等宽，右边的字才对得齐。
    LOGO = (" ▄▄▄▄▄▄", "██▀▀██ ", " ▄▄▄▄▄▄")
    # 左栏里那个方块标的内容宽（列）：方块标 8 列 + 两侧各留 1 列呼吸 = 10。
    BOX_WIDTH = 10
    # 右栏一次列几条会话。
    RECENT_LIMIT = 4
    # 右栏那个方框的内容宽（列）。**它就是 `WELCOME_RIGHT_WIDTH`**（那里记着它和
    # CSS 里那个 42 的关系）：`_recent_row` 靠它把"多久以前"和"标题"分成两列。
    RIGHT_WIDTH = WELCOME_RIGHT_WIDTH
    # 每个方框正文的行数（**两个框一样多**，这是它们等高、里面不出现空档的全部理由）：
    #   右 = "最近活动" 1 + 4 条会话 + 空行 1 + "箴言" 1 + 箴言 1 = 8
    #   左 = 方块标 3 + 空行 1 + 问候 1 + 空行 1 + `tudouni 版本` 1 + 模型与工作区 1
    #        + 空行 1 + 提示 1 = 10
    # 两边对不齐时**用空行补齐**（那条"框里必须正好这么多行"的规矩见 `StartPanel` 和
    # `RecentPanel` 的 `render_parts`）—— 差一行的症状只是"框里多一条或少一条空行"，
    # 没有任何报错。
    BOX_LINES = 10

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._palette = palette
        self._state: view_state.ViewState | None = None
        self._payload: Any = None
        self._now: float | None = None
        # `None` = 还没排过版；`True`/`False` = 上面那两个框是叠着的还是并排的。
        self._stacked: bool | None = None

    def compose(self):
        """**三个盒子和它们的容器在这一次全部声明出来。**

        为什么要走 `compose()` 而不是在 `show()` 里 `mount()`：`mount()` 是排队的，
        而给一个**还没挂上**的容器挂子控件会当场抛
        `MountError: Can't mount widget(s) before ... is mounted`（实测）。`compose()`
        由 Textual 自己按顺序把整棵子树挂完，没有这个先来后到的问题。

        **配色走关键字**：Textual 的 `Widget.__init__(*children)` 会把位置参数当成子
        控件，于是 `StartPanel(palette, ...)` 里那个 `Theme` 成了它的内容，报的是
        `unable to display 'Theme' type`（看起来完全指不到这儿）。

        版式：外层纵向（上面一行 + 下面那个通栏的提示框）；`#welcome-row` 是横向的 ——
        **写成 `Vertical` 的话两个框就是上下排的**，而 CSS 的 `align` 只管交叉轴，
        "并排"怎么调都调不出来（实测：屏幕上就是"开始框在上、最近框在下"）。窄屏那一版
        把 `#welcome-row` 的 `layout` 改成 `vertical`（见 `_sync_layout`）。
        """
        with Vertical(id="welcome-body"):
            with Horizontal(id="welcome-row"):
                yield StartPanel(palette=self._palette,
                                 classes="welcome-box start-box")
                yield RecentPanel(palette=self._palette,
                                  classes="welcome-box recent-box")
            yield HintPanel(self._palette, classes="welcome-box hint-box")

    @property
    def body(self) -> Vertical:
        return self.query_one("#welcome-body", Vertical)

    @property
    def row(self) -> Horizontal:
        return self.query_one("#welcome-row", Horizontal)

    @property
    def hint(self) -> "HintPanel":
        return self.query_one(HintPanel)

    def show(self, state: view_state.ViewState, palette: theme_mod.Theme,
             payload: Any = None, *, now: float | None = None) -> None:
        self._state = state
        self._palette = palette
        self._payload = payload
        self._now = now
        # **判据是"我自己挂上了没有"，不是 `self.body`。** `mount()` 是排队的，
        # 刚挂上去那一瞬间 `#welcome-body` 还不存在，去 `query_one` 会抛 `NoMatches`。
        # 没挂上就只登记、什么都不画 —— 等 `on_mount` / 之后那次 `on_resize` 再画，
        # 那一趟也才拿得到宽度（见 `_sync_layout` 里"宽度还是 0"那一段）。
        if self.is_mounted:
            self._sync_layout()

    def on_mount(self) -> None:
        """**挂好之后再画。**

        盒子在 `compose()` 里声明（Textual 会把这棵子树挂完），所以"我现在有多宽、
        该并排还是叠起来"只有到这一次回调才算得出来。`ConversationLog` 也刻意**不在**
        挂载前调 `show()`（那时宽度是 0，画出来的是"并排"那一版，窄终端上会溢出屏幕）。
        """
        self._sync_layout()

    def _sync_layout(self) -> None:
        """按当前宽度决定"并排"还是"上下"，然后重画三个方框。

        两件事绑在一起做，是因为**重画要用新的宽度**：先画再换版式的话，那一次画的
        内容是按旧宽度算的（症状是窗口一拖，右栏那两列就错开）。
        """
        if self._state is None:
            return
        if not self.size.width:
            # **宽度还是 0 = Textual 还没排过版**（控件刚挂上去就是这个状态）。这时候
            # 算出来的 `stacked` 一定是"并排"，而窗口其实可能很窄 —— 于是那一版会按
            # 并排画出去、横向溢出屏幕（实测：70 列的终端上两个框各被切掉一截）。
            # 现在什么都不做，等布局定下来那一次 `on_resize` 再画。
            return
        stacked = self.size.width < WELCOME_STACK_COLUMNS
        if stacked != self._stacked:
            # **只在版式真的变了时动一次**：`on_resize` 会被拖动的每一帧调到。
            #
            # 换的是**容器的 `layout`**（横向 ↔ 纵向），不是重建容器、也不是靠 CSS 让
            # 两个定宽的框"折行"：一行里放不下时 Textual 会把子控件压窄（实测：70 列
            # 上两个框各 33 列，而不是各占满整行）。控件一个都不动，所以这里没有
            # "新容器和还没摘掉的旧容器撞在同一个 id 上"那种坑。
            self._stacked = stacked
            self.row.styles.layout = "vertical" if stacked else "horizontal"
            # `stacked` 这个类只给 CSS 用（它管的是"这一版要不要滚动、框宽要不要放开"）。
            # **不能用 `toggle_class`**：它是"翻转"不是"设置"（每次调用都切反）。
            if stacked:
                self.add_class("stacked")
            else:
                self.remove_class("stacked")
        # 提示框按**自己那一行的宽度**决定说几条键位（窄屏另有一套更短的措辞）。叠起来
        # 那一版三个框各占满整行，提示框跟着它们一起变宽。
        hint_width = self.row.size.width if stacked else WELCOME_HINT_WIDTH
        for panel in [*self.row.query(BorderedPanel), self.hint]:
            panel.show_children(self._state, self._palette, self._payload,
                                now=self._now, width=hint_width)

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        self._sync_layout()

    def on_resize(self) -> None:
        self._sync_layout()


class StartPanel(BorderedPanel):
    """左栏：**这是哪个程序、用什么模型、在哪个工作区**。

    方块标和"tudouni + 版本"照旧：它是这一屏上唯一"一眼认出这是 tudouni"的东西。
    """

    # 方块标从 `WelcomeBlock` 那里取（它才是这个标记的定义处，这里只是画它的地方）。
    LOGO = WelcomeBlock.LOGO

    def render_parts(self, state, palette, payload=None, width=0):
        # **这 8 行是定死的**（见 `WELCOME_BOX_HEIGHT`）：两个框一样高、里面不留
        # 会随内容变形的空档，靠的就是这里的条数和 `RecentPanel` 那边一致。
        rows: list[Text] = []
        for line in self.LOGO:
            rows.append(Text(line, style=palette.accent))
        rows.append(Text(""))
        rows.append(Text(i18n.t("welcome.back", name=user_name()),
                         style=palette.ink + " bold"))
        rows.append(Text(""))
        rows.append(Text(f"tudouni {payload or ''}".strip(), style=palette.ink2))
        rows.append(Text(model_and_workspace(state), style=palette.ink3))
        rows.append(Text("", style=palette.ink4))
        rows.append(Text(i18n.t("welcome.palette_hint"), style=palette.ink4))
        return Text(i18n.t("welcome.box.start"), style=palette.ink3), [
            _centered_row(row) for row in rows
        ]


class RecentPanel(BorderedPanel):
    """右栏：**最近动过哪几个会话** + 一句箴言。

    "最近"用的是会话文件的 mtime（`modified_at`，runtime 读盘时算好）—— 它说的是
    "最后一次聊"，而那正是"我上次干到哪儿了"要看的东西。**读不出来或者没有那个
    字段就不显示时间**，不猜一个。

    箴言那一块的形状是从别家 CLI 的"What's new"借来的，内容换成了**一句每天轮换
    的话**：一个每 15 天就有内容过期的新特性区块，在这种天天开的工具里只会变成噪声。
    """

    def render_parts(self, state, palette, payload=None, width=0):
        # **正好 `BOX_LINES` 行**（和 `StartPanel` 那边一样多，这是两个框等高的全部
        # 理由）：标题 1 + 会话 4 + 空行 1 + 箴言标题 1 + 箴言 1 = 8，末尾再补 2 个空行。
        rows: list[Text] = [Text(i18n.t("welcome.recent.title"), style=palette.ink3)]
        items = _most_recent(state.recent_sessions, WelcomeBlock.RECENT_LIMIT)
        for item in items:
            rows.append(_recent_row(item, palette, self._now))
        for index in range(WelcomeBlock.RECENT_LIMIT - len(items)):
            # **空位也要占住**（见 `WelcomeBlock` 的 docstring）：没有会话时第一格
            # 写"（还没有会话）"，其余空格 —— 这样框的高度不随硬盘上有几个会话变。
            blank = i18n.t("welcome.recent.empty") if not items and index == 0 else ""
            rows.append(Text(blank, style=palette.ink4))
        rows.append(Text(""))
        rows.append(Text(i18n.t("welcome.recent.motto"), style=palette.ink3))
        rows.append(Text(view_state.motto_of_day(), style=palette.line))
        rows.extend(Text("") for _ in range(WelcomeBlock.BOX_LINES - len(rows)))
        return Text(i18n.t("welcome.box.recent"), style=palette.ink3), [
            Static(row, classes="welcome-line") for row in rows
        ]


class HintPanel(BorderedPanel):
    """底下那个通栏的「提示」：**键位**。

    它原来是输入行下面那一条常驻的一行字（`#keys`）。挪到欢迎屏里之后有两个变化：

      1. **它有地方了**，所以不再"放不下就从右边少说一条"（那一行原来紧贴输入行，
         多一行就会把输入行顶出屏幕）—— 现在按"每行几条"排，一行放不下就换行；
      2. **它只在欢迎屏上**。说过第一句话之后这一屏就收了，键位提示跟着一起收 ——
         那时候 `/help` 仍然列着完整的键位表，输入框的占位符也还写着最基本的用法。

    窄屏（`width < WELCOME_HINT_WIDTH`）换一套更短的措辞：那几列连"思考过程"四个字
    都嫌长。**宽度由调用方传进来**（`WelcomeBlock` 量的是这一行自己的宽度），因为这个
    框在并排和叠起来两版里的宽度不一样。
    """

    # 一行放几条：**按语言算**（见 `hint_per_row`）。中文 4 条一行两行放完；英文那几条
    # 长得多，4 条一行会折成四行、被框高裁掉。
    def on_mount(self) -> None:
        # 框高也按语言定：CSS 里那个 `height: 4` 是中文那一档（两行正文）。
        self.styles.height = hint_box_height()

    def render_parts(self, state, palette, payload=None, width=0):
        pairs = hint_keys(narrow=width < WELCOME_HINT_WIDTH)
        per_row = hint_per_row()
        rows: list[Static] = []
        for start in range(0, len(pairs), per_row):
            row = Text()
            for key, what in pairs[start:start + per_row]:
                if row.cell_len:
                    row.append("   ")
                row.append(key, style=palette.ink3)
                row.append(f" {what}", style=palette.ink4)
            # `hint-line` 而不是 `welcome-line`：提示那一行的**换行由它自己决定**
            # （`height: auto` + 软换行），而方框里其它行都是"一行就是一行"。
            rows.append(Static(row, classes="hint-line"))
        return Text(i18n.t("welcome.box.hint"), style=palette.ink3), rows


def _most_recent(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """会话清单 → 最近动过的 `limit` 条。

    **清单本身是按创建时间排的**（那是选会话面板要的顺序，见
    `composition.session_summaries`），而这一栏要的是"最后一次聊" —— 两者不是同一个
    顺序：一个昨天建、今天还在聊的会话在创建时间上是旧的，在"我上次干到哪儿了"
    上却是最新的。所以这里按 `modified_at` 重排。

    没有 `modified_at` 的那些（坏文件、老 runtime 给的载荷）排在最后：它们没有
    "最近"可言，而按 0 排也一样。
    """
    def key(item: dict[str, Any]) -> float:
        when = item.get("modified_at")
        if isinstance(when, (int, float)) and not isinstance(when, bool):
            return float(when)
        return 0.0

    return sorted(items, key=key, reverse=True)[:limit]


def _centered_row(text: Text) -> Static:
    """左栏的一行：**整个控件居中**，不是在前面补空格。

    补空格那条路要求调用方知道控件的宽度，而布局是下一帧的事 —— 刚挂上去时宽度还是
    0，于是那一行会贴在左边直到下一次重画（症状：窗口一拖，方块标就歪一下）。
    `text-align: center` 由 CSS 给，宽度由控件自己决定，两边都不需要知道对方的数字。
    """
    return Static(text, classes="welcome-line start-line")


def _recent_row(item: dict[str, Any], palette: theme_mod.Theme,
                now: float | None) -> Text:
    """最近活动的一行：`12分钟前  把欢迎屏改成两个框`。

    右边那一列是**这次对话的标题**，不是会话 id：id 是时间戳（`20260912-101530`），
    人对着它认不出"这是哪一次"。标题直接用 runtime 算好的 `preview` —— 它就是第一条
    用户消息的开头几个字（`composition._first_user_message`），选会话面板用的也是它。
    **界面不自己去读会话文件、也不自己截断**：同一份事实两个来源，早晚会漂。

    左边那一列的宽度按"最长的那个时间说法" `12分钟前` 算（`STAMP_WIDTH`），所以四行
    的时间是右对齐的、标题都在同一条竖线上 —— 那点空白就是这一行的列分隔，不用
    `f"{stamp:<8}"`（一个汉字两列，`len` 数不出来）。

    **标题那一列的宽度取奇数（25 列），这是算出来的。** 截断的形态是"若干个整字 +
    一个 1 列的 `…`"，而全宽字符一次占两列 —— 所以只有**奇数列数**能正好被填满
    （`12 个汉字 + … = 25`）。写成偶数的话，一个全汉字的标题只能到 23 列（11 个汉字
    + `…`），整行就比别的行短一列，右边那条竖线看起来"差一点点" —— 而那正是这一行
    要保证的东西（`test_a_long_title_is_cut_by_columns_not_by_characters` 钉的就是它）。
    """
    when = item.get("modified_at")
    if isinstance(when, (int, float)) and not isinstance(when, bool) and now:
        stamp = view_state.time_ago(now - float(when))
    else:
        # 没有那个字段（老 runtime / 手工构造的载荷）→ 宁可不说时间，也不猜一个。
        stamp = ""
    # 还没说过话的会话（preview 是空串）在面板里也写"（还没说过话）"，这里照抄那个
    # 口径：**一行完全空白看起来像渲染坏了**。
    title = str(item.get("preview") or "") or i18n.t("session.row.untitled")
    row = Text(stamp, style=palette.ink4)
    row.append(" " * max(1, stamp_width() - row.cell_len + 1))
    row.append(_clip_cells(title, title_width()), style=palette.ink2)
    return row


# "12分钟前" 的显示宽（列）：7 个汉字/数字混排 = 3 + 2 + 2 + 2 ≈ 11，留一位余量。
# 它是右栏第一列的固定宽 —— 四行的时间因此右对齐、标题因此对齐成一条竖线。
#
# **两种语言不一样**，所以下面那个 `stamp_width()` 才是读者：英文 `12 minutes ago`
# 是 14 列，按中文的 12 写死会让时间戳**顶着标题**（那个框一共只有 38 列）。
STAMP_WIDTH = 12
STAMP_WIDTH_EN = 14


def stamp_width() -> int:
    """时间那一列在**当前语言**下的宽（列）。"""
    return STAMP_WIDTH_EN if i18n.current() == i18n.EN else STAMP_WIDTH


def title_width() -> int:
    """标题那一列的宽（列）：`38 - 时间戳 - 1(分隔)`。**它必须是奇数**，
    见 `_recent_row` 的 docstring：截断的形态是"若干个整字 + 一个 1 列的 `…`"，
    而全宽字符一次占两列 —— 只有奇数列数能正好铺满。

    两种语言下都是奇数（中文 25、英文 23），所以这条性质不随语言变。
    """
    return WelcomeBlock.RIGHT_WIDTH - stamp_width() - 1


# 中文那一档的值，给"两种语言都要对得上"的测试与文档用（改它们要看上面那段说明）。
TITLE_WIDTH = WelcomeBlock.RIGHT_WIDTH - STAMP_WIDTH - 1


def _clip_cells(text: str, width: int) -> str:
    """按**显示列数**截断，截了就补一个 `…`。

    不用 `view_state.clip`（它按 `len` 数）：标题是"用户说的第一句话"，中英混排是常态，
    而一个汉字占两列 —— 按字符数截出来的标题长短会随内容飘，右边那条竖线就歪了。
    """
    if width <= 1 or cell_len(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        size = cell_len(char)
        if used + size > width - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept) + "…"


def user_name() -> str:
    """当前用户名（问候那一行用）。**认不出来就空着**。

    `getpass.getuser()` 会依次看 `LOGNAME` / `USER` / `LNAME` / `USERNAME`，最后退到
    pwd 数据库 —— 这几样在一个裁剪过的容器里都可能没有，那时候它抛 KeyError。
    问候语少一个名字不是错误，界面因此起不来才是。
    """
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - 见 docstring：这不是错误路径
        return ""


def workspace_name(state: view_state.ViewState) -> str:
    """工作区的**最后一段目录名** —— 完整路径在顶栏那一行写着。

    左栏那个方框只有二十几列，而完整路径的前 30 列在任何机器上都是常量
    （`C:\\Users\\<名字>\\repo\\`）：把常量抄第二遍买不到任何信息。
    """
    path = (state.workspace or "").replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else "—"


def model_and_workspace(state: view_state.ViewState) -> str:
    """左栏那一行身份：`deepseek-chat · agent_runtime`。

    **步数预算不在这里**：它在会话头那一行（`最多 80 步`），而这一格要说的是
    "我在哪儿、用的什么模型"——把三个数挤在一行里，宽屏能看、窄屏全都被省略号吃掉。
    """
    return f"{state.model or '—'}  ·  {workspace_name(state)}"



class RailBlock(Vertical):
    """上下文栏里的一块：**一行小字标题 + 内容**（F1 的 `任务 · 2 / 5`）。

    标题和计数**画在同一行里**，而不是"标题靠左、计数顶到栏的右边缘"：计数被推到
    32 列栏的最右边之后，眼睛得横跨整栏才能把 `2 / 5` 和 `任务` 连起来 —— 贴在一起
    读起来才是"这一块有几个"。计数为空的两块（权限范围 / 本次会话）不画那个 `·`。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        head = Static("", classes="rail-head")
        lines = Static("", classes="rail-lines")
        super().__init__(head, lines, *args, **kwargs)
        self._head = head
        self._lines = lines
        self._data: tuple[str, str, list[view_state.Line]] = ("", "", [])

    def show(self, title: str, count: str, lines: list[view_state.Line],
             palette: theme_mod.Theme) -> None:
        self._data = (title, count, lines)
        text = f"{title} · {count}" if count else title
        # 标题**和正文同一档灰**（`ink4`）、不加粗：它说的是"这一块叫什么"，不是重点
        # —— 每块标题都加粗发亮的话，这一栏里就有五个东西同时在抢眼睛。
        self._head.update(Text(text, style=palette.ink4))
        self._lines.update(paint_lines(palette, lines))

    def repaint(self, palette: theme_mod.Theme) -> None:
        self.show(*self._data, palette)


class ContextRail(VerticalScroll):
    """左栏：**任务 / 已加载技能 / 权限范围 / 本次会话 / 后台任务**（设计稿最值钱的加法）。

    这几块此前只有"另开一个终端"的出口（`--skills` / `--audit` / `--list`；后台任务
    连那个出口都没有 —— 它此前只存在于 `shell_background` 那条工具结果里），
    放进栏里之后"agent 为什么这么做""我现在放行了什么""我机器上还挂着什么"变成常驻可见。

    **它默认收起**（决策 1），而"任务列表从无到有时自动顶开一次、之后听用户的"由
    `app.py` 每次刷新时问 `view_state.should_auto_open` —— 这个控件只负责画。
    后台任务**不参与那个自动顶开**：起一个服务不该把栏从用户手里抢走，它的可见性由
    状态栏那枚常驻徽标保证（`view_state.jobs_badge`）。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._blocks: list[tuple[str, str, list[view_state.Line]]] = []
        self._widgets: list[RailBlock] = []
        self._signature: str | None = None

    def show(self, state: view_state.ViewState, palette: theme_mod.Theme,
             payload: Any = None) -> None:
        self._blocks = view_state.rail_blocks(state)
        signature = repr([(title, count, [str(line) for line in lines])
                          for title, count, lines in self._blocks])
        if signature != self._signature:
            # **只在内容变了才重建控件**：这个函数每次 pump 都会被调（50ms 一次），
            # 重建整栏会在视觉上闪、也会打断滚动位置。
            self._signature = signature
            self.remove_children()
            self._widgets = []
            for title, count, lines in self._blocks:
                block = RailBlock(palette, classes="rail-block")
                block.show(title, count, lines, palette)
                self._widgets.append(block)
                self.mount(block)
        else:
            self.repaint(palette)

    def repaint(self, palette: theme_mod.Theme) -> None:
        for block, data in zip(self._widgets, self._blocks):
            block.show(*data, palette)


class ConversationLog(VerticalScroll):
    """会话正文：**按回合的块容器**（设计稿改动 7）。

    v1 用的是 `RichLog`，理由是"它的语义正好是只追加的日志，而重绘、滚动、裁切
    它都做好了"。那个理由在设计稿面前不成立了：回合头要能被回填、思考块要能单独
    展开、整块要能跟着主题重画 —— 三件事都要求"这一块是谁"是可控的，而只追加的
    日志没有"块"这个概念。

    代价是滚动和裁切要自己管 —— 但那是 `VerticalScroll` 给的，所以真正多出来的
    只有"哪些行属于哪个回合"这一份结构。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._palette = palette
        self._welcome: WelcomeBlock | None = None
        self._current: MessageBlock | None = None
        # **回合块直接按引用记着**，不靠 `query(TurnBlock)`：`mount()` 是排队的，
        # 而 App 会在 `start_turn()` 之后**同一次事件里**就追加这一轮的行 ——
        # 那时候 query 还是空的，行会全部掉进"无回合块"。这个坑的症状是
        # "第一回合的行不见了"，而它只在第一次出现。
        self._turns: list[TurnBlock] = []

    # -- 空态 ------------------------------------------------------------------

    def show_welcome(self, state: view_state.ViewState, palette: theme_mod.Theme,
                     version: str, now: float | None = None) -> None:
        self._palette = palette
        if self._welcome is None:
            # **挂上去，然后立刻把 state 交给它。** 盒子在 `WelcomeBlock.compose()` 里
            # 声明（见那里的说明：给一个还没挂上的容器挂子控件会抛 `MountError`），
            # 所以这一次 `show()` 只登记、不画 —— 画要等挂载走完，由 `_sync_layout`
            # 在拿到宽度之后做（见 `WelcomeBlock.show` 的判据）。
            #
            # 这一句**不能省**：省掉的症状是三个方框只有边框、里面一个字都没有 ——
            # 因为 `_sync_layout` 第一句就是"没有 state 就返回"，而它此后不会再从别处
            # 拿到 state（下一次 `show()` 要等到 `sessions` 那份回包，那可能是几百毫秒
            # 之后，甚至永远不来）。
            block = WelcomeBlock(palette, classes="welcome")
            self.mount(block)
            self._welcome = block
            block.show(state, palette, version, now=now)
        else:
            self._welcome.show(state, palette, version, now=now)
        # **把会话流拉回顶部**：欢迎屏比别的内容高一截，而 `#log` 自己会滚到底
        # （追加行之后的那次 `scroll_end` 留下的位置）—— 不归位的话顶上两个框的边框
        # 会被滚出屏幕，看起来像"这一屏是从中间开始画的"。
        self.scroll_home(animate=False)

    def welcome_visible(self) -> WelcomeBlock | None:
        """空态那一屏**现在**在不在（在就把它还回来）。

        它是 `_on_sessions` 的判据：同一份会话清单，欢迎屏要它和 `/resume` 要它
        是两件事 —— 见 `TuiApp._on_sessions`。
        """
        return self._welcome

    def hide_welcome(self) -> None:
        if self._welcome is not None:
            self._welcome.remove()
            self._welcome = None

    # -- 回合 ------------------------------------------------------------------

    def start_turn(self, turn: view_state.Turn, palette: theme_mod.Theme) -> TurnBlock:
        self._palette = palette
        self.hide_welcome()
        self._current = None
        block = TurnBlock(turn, palette, classes="turn")
        self._turns.append(block)
        self.mount(block)
        self._scroll_end()
        return block

    def add_lines(self, lines: list[view_state.Line], palette: theme_mod.Theme) -> None:
        """往**当前回合**里加行；还没有回合就加到一个无回合块里。

        **空态那一屏还在时不滚到底。** `init` 那条消息是分两步画的：先摆欢迎屏
        （`show_welcome` 会把流拉回顶部），紧接着把这一屏的几行说明追加进去
        （`add_lines` 会滚到底）。后一步在欢迎屏比说明高的时候会把**上面那两行框线
        滚出屏幕** —— 症状是"这一屏从中间开始画"，而它看起来完全像是我把版式算错了。
        欢迎屏还在 = 用户还没说过第一句话，所以这一屏停在顶部才是对的。
        """
        if not lines:
            return
        self._palette = palette
        current = self.current_turn_block
        if current is not None:
            current.add(lines)
        else:
            if self._current is None:
                self._current = MessageBlock(palette, classes="turn")
                self.mount(self._current)
            self._current.add(lines)
        if self._welcome is None:
            self._scroll_end()

    def add_stream(self, kind: str, line: view_state.Line,
                   palette: theme_mod.Theme, *, run_id: str = "") -> None:
        """流式正文/思考链的一块。**和非流式那条路（`add_answer`）分开。**

        分开是为了让"流到一半"和"已经收尾"在控件那一侧是两种块：`delta_reset` 要
        拿掉前者、保留后者（见 `TurnBlock.discard_stream`）。合在一起的话，一次重试
        会把上一轮已经画好的答案也抹掉 —— 那看起来像"答案刚才还在，现在没了"。

        **`run_id` 决定挂到哪个回合上**（缺省才是"最后那一个"）：delta 是异步来的，
        一个迟到的块可能落在 `run_finished` 之后 —— 按 run_id 找，它就不会挂到下一轮
        的头上去（那会让下一轮的正文里凭空多出一句话）。

        没有回合块时（理论上只有第一块 delta 挤在 `run_started` 之前）退回
        `MessageBlock`：宁可少一个回合头，也不要丢内容。
        """
        self._palette = palette
        target = self.turn_block_for(run_id) if run_id else None
        if target is None:
            target = self.current_turn_block
        if target is not None:
            target.add_stream(kind, line)
        elif self._current is not None:
            self._current.add([view_state.Line(str(line), view_state.ROLE_ANSWER)])
        if self._welcome is None:
            self._scroll_end()

    def close_stream(self, kind: str, text: str) -> None:
        """回合收尾：把还在流的那一块收成折叠形态（目前只有思考链需要）。"""
        current = self.current_turn_block
        if current is not None:
            current.close_stream(kind, text)

    def discard_stream(self, kind: str) -> None:
        """把最后一块这种流式内容拿掉（`t:"delta_reset"`）。没有就当没发生。"""
        current = self.current_turn_block
        if current is not None:
            current.discard_stream(kind)

    def add_answer(self, text: str, palette: theme_mod.Theme) -> None:
        """agent 正文 —— **这个前端里唯一走 `AnswerBlock` 的东西**（其余仍是行）。

        滚动那两条理由和 `add_lines` 一样（欢迎屏还挂着就不滚），但这里**还要多排
        一次**：`Markdown` 是分批挂子控件的，挂上去的那一刻它的高度还没算出来，所以
        第一次 `_scroll_end()` 会把底停在"还没长开"的位置 —— 症状是"答完了，但最后
        几行在屏幕外"，而且它只在答案比一屏长的时候出现。刷新之后再排一次才落在真的
        底上。
        """
        self._palette = palette
        current = self.current_turn_block
        if current is not None:
            current.add_answer(text)
        else:
            if self._current is None:
                self._current = MessageBlock(palette, classes="turn")
                self.mount(self._current)
            self._current.add_answer(text)
        if self._welcome is None:
            self._scroll_end()
            self.call_after_refresh(self._scroll_end)

    # -- 原地回填（安静模式）---------------------------------------------------

    def replace_line(self, line: view_state.Line) -> bool:
        """把一条认身份的线**换掉它认的那一行**（安静模式：工具结果回填到调用行上）。

        **只找当前这个回合块**：安静模式下工具行属于正在跑的那一轮，而 `anchor` 是
        `call_id` —— 它不认得回合（那是 `run_id` 的事）。晚到的结果（`tool_result`
        比下一轮的 `run_started` 还晚）在真实时序里不会发生，真发生了也只是"那一行
        没找到"，由调用方退化成单独一行。
        """
        current = self.current_turn_block
        return current is not None and current.replace_anchored(line)

    def upsert_line(self, line: view_state.Line) -> None:
        """同上，但**没有就画出来**（安静模式下思考那一行从无到有）。"""
        current = self.current_turn_block
        if current is not None:
            current.upsert_anchored(line)

    def finish_thinking(self, run_id: str, text: str) -> bool:
        """一轮收尾：把还在长的那一行思考定格成折叠形态（见 `TurnBlock.finish_thinking`）。

        按 `run_id` 找回合块（和 `add_stream` 同一条理由：delta 是异步来的，
        `run_finished` 可能比最后一块先到）。
        """
        block = self.turn_block_for(run_id) or self.current_turn_block
        return block is not None and block.finish_thinking(run_id, text)

    def _scroll_end(self) -> None:
        if self.is_mounted:
            self.scroll_end(animate=False)

    @property
    def current_turn_block(self) -> TurnBlock | None:
        return self._turns[-1] if self._turns else None

    def turn_block_for(self, run_id: str) -> TurnBlock | None:
        """按 `run_id` 找回合块。

        `current_turn_block` 是"最后那一个"，而流式下**消息晚一拍落地**：
        `run_finished` 可能在我们还没处理完上一块的 delta 时就到了（实测：一条
        推理回合的块序列是 `plain, answer, think`，而 `run_finished` 已经在队列里）
        —— 那时候"找刚才那一轮"只能按 run_id，按"最后那一个"会拿到一个还没开始的
        空回合。验收脚本和测试都要它。
        """
        for block in reversed(self._turns):
            if block.turn.run_id == run_id:
                return block
        return None

    def turn_under_viewport(self) -> TurnBlock | None:
        """**光标（视口中心）所在的那个回合** —— `Ctrl+T` 的作用对象。

        v1 取的是"最后一个有思考的回合"（`list(state.thinking)[-1]`），那从第二回合
        起就会作用错，而画面看起来完全正常。这里按视口中心算：用户看到哪一块，
        `Ctrl+T` 就动哪一块 —— 滚动位置是这块画布上唯一能表达"我在看这一段"的东西。

        两条路，因为**中心点不一定落在某个回合上**：内容还没填满一屏时，屏幕中间
        是空的。那时候按"离中心最近的那一块"算（而不是随手取最后一个）——
        这个规则在两种情况下是同一个意思。
        """
        if not self._turns:
            return None
        try:
            widget, _region = self.screen.get_widget_at(
                max(1, self.size.width // 2), max(1, self.size.height // 2))
            for node in (widget, *widget.ancestors):
                if isinstance(node, TurnBlock):
                    return node
        except Exception:  # pragma: no cover - 只在还没有屏幕信息时走到
            pass
        centre = self.region.y + self.size.height // 2

        def distance(block: "TurnBlock") -> int:
            top, bottom = block.region.y, block.region.y + block.region.height - 1
            if top <= centre <= bottom:
                return 0
            return min(abs(centre - top), abs(centre - bottom))

        return min(self._turns, key=distance)

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        if self._welcome is not None:
            self._welcome.repaint(palette)
        if self._current is not None:
            self._current.repaint(palette)
        for block in self._turns:
            block.repaint(palette)

    def clear(self) -> None:
        self.remove_children()
        self._welcome = None
        self._current = None
        self._turns = []


class PromptArea(TextArea):
    """输入框：**两行、软换行、Enter 发送**。

    为什么不是 v1 那个单行 `Input`：长句（尤其往里面贴一段日志或路径）在单行输入框里
    只能横向滚动 —— 你**看不见前面写了什么**，改一个字要靠记忆数位置。两行 + 软换行
    把能同时看见的内容翻了一倍，这才是那个"增加一倍"真正买到的东西。

    顺带把设计稿 F6 键位表里的 `Shift+Enter 换行` 补上了：v1 里它是"明确没做"的一条
    （单行 `Input` 和 `TextArea` 的回车语义冲突），换过来之后它自然就有。

    ## 五个必须覆盖的绑定（都是 TextArea 自带的，不覆盖就会互相抢）

    | 键 | TextArea 默认 | 这里 | 为什么不让它默认 |
    |---|---|---|---|
    | `Enter` | 插入换行 | **发送** | 终端界面里回车就是"说完了"，换行让给 `Shift+Enter` |
    | `Shift+Enter` | 向上选中一行 | 插入换行 | F6 键位表要的就是这个 |
    | `Ctrl+K` | 删到行尾 | 命令面板 | 全界面的 `Ctrl+K` 是命令面板（顶栏那句提示也这么写） |
    | `Ctrl+C` | 复制 | 退出 | 和 `App` 那一层一致；不给它的话输入焦点一进来 `Ctrl+C` 就退不出去了 |
    | `↑` / `↓` | 移动光标 | 面板开着时选候选；光标已在边界时滚会话流；否则移动光标 | 一个键三种用途，但每一种都是**当下唯一说得通的意思**（见 `action_up_or_palette`） |

    其余（`←→`、`Home/End`、`Backspace`、`Ctrl+A/E/W/U`、`Ctrl+Z/Y`、`Ctrl+V`、
    `Tab` 移焦点）**一律继承** —— 那些是"能编辑"的基本盘，自己列一遍只会漏。

    `Enter` **不在** `BINDINGS` 里，因为绑定拦不住它（见 `_on_key`）—— 那一行写在
    下面那张表里是为了说清"谁管它"，而不是让绑定去管。
    """

    BINDINGS = [
        ("shift+enter", "newline", i18n.t("bindings.newline")),
        ("ctrl+k", "palette", i18n.t("bindings.palette")),
        ("ctrl+c", "quit_app", i18n.t("bindings.quit")),
        ("up", "up_or_palette", i18n.t("bindings.up")),
        ("down", "down_or_palette", i18n.t("bindings.down")),
    ]

    class Submitted(Message):
        """按了回车。**带的是全文**（多行也算一句话，交给 runtime 原样入库）。"""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        """`Enter` 只能在这一层拦 —— **绑定拦不住它**。

        `TextArea._on_key` 里硬编了 `insert_values = {"enter": "\\n"}`，而且它当场
        `stop()` + `prevent_default()`：回车在**绑定系统之前**就被它吃掉、变成插入
        换行。实测过一次 —— `BINDINGS` 里明明写着 `enter -> submit`，按下去还是换行
        （而 `_bindings` 里查得到那条绑定，所以光看绑定表是看不出问题的）。

        `Shift+Enter` 不在这里拦：它不是可打印字符、也不在 `insert_values` 里，
        所以它照常走绑定那条路（那条绑定是有效的）。
        """
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        await super()._on_key(event)

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_palette(self) -> None:
        self.app.action_command_palette()  # type: ignore[attr-defined]

    def action_quit_app(self) -> None:
        self.app.action_quit_app()  # type: ignore[attr-defined]

    def action_up_or_palette(self) -> None:
        """`↑`：面板选上一条 / 光标上移 / 滚会话流。**按上下文定，不按模式定。**

        光标已经在第一行的第一个可视行时，"上"唯一说得通的意思是**往回翻会话**；
        否则它就该在输入框里走。面板开着时先在面板里选 —— 那时候用户的注意力在
        候选上，而要翻的会话流本来也被面板盖住了一半。
        """
        if self.app._palette_visible or self.cursor_location[0] == 0:
            self.app.action_palette_up()  # type: ignore[attr-defined]
        else:
            self.action_cursor_up()

    def action_down_or_palette(self) -> None:
        """`↓`：面板选下一条 / 光标下移 / 滚会话流。

        和 `↑` 的判据不同：**"到底了"不能靠行号看**（软换行时一行可能占好几个可视行），
        所以这里让它先试一次光标下移，**没动**就说明已经在最后一个可视行 ——
        那时候把 `↓` 让给会话流。这是唯一一处"先做再看"的地方，而它换来的是
        "在最后一行按 ↓ 就是在往下翻"，不用知道任何布局细节。
        """
        if self.app._palette_visible:
            self.app.action_palette_down()  # type: ignore[attr-defined]
            return
        before = self.cursor_location
        self.action_cursor_down()
        if self.cursor_location == before:
            self.app.action_palette_down()  # type: ignore[attr-defined]


class CommandPalette(Vertical):
    """命令面板浮层（设计稿改动 6）。

    **它替换的是"打一行 `/命令` 然后回车"**：v1 里 `/new` 打错一个字就得到一句
    "没有这个命令"，而面板把候选摆出来、`↑↓` 选、`Enter` 执行 —— 命令集本身
    一条都没变（决策 15 定下来的那六条还在原位，新增的三条排在末尾）。

    候选是**过滤**出来的（前缀匹配，见 `view_state.filter_commands`）：命令一共
    十几条，模糊匹配会让"我打错了"和"它猜对了"长得一样。

    **面板的高度上限必须装得下全部命令**（见 `app._PALETTE_MAX_ROWS`）：它是屏幕纵向
    布局里的一行、不是浮层，所以溢出时不会自己滚 —— 只是最后几条**静默消失**（实测：
    上限写死 12 时，`/tools` `/model` `/thinking` `/effort` 在面板里根本不存在）。
    `tests/test_tui_palette.py` 盯着这一条。
    """

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        title = Static("", classes="palette-title")
        options = Vertical(id="palette-options")
        super().__init__(title, options, *args, **kwargs)
        self._palette = palette
        self._query = "/"
        self._index = 0
        self._title = title
        self._options = options

    @property
    def commands(self) -> list[view_state.Command]:
        return view_state.filter_commands(self._query)

    @property
    def selected(self) -> view_state.Command | None:
        items = self.commands
        if not items:
            return None
        return items[min(self._index, len(items) - 1)]

    def show(self, query: str, palette: theme_mod.Theme) -> None:
        self._palette = palette
        self._query = query
        self._index = min(self._index, max(0, len(self.commands) - 1))
        self.repaint(palette)

    def move(self, delta: int) -> None:
        count = len(self.commands)
        if count:
            self._index = (self._index + delta) % count
            self.repaint(self._palette)

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        title = Text(i18n.t("palette.title"), style=palette.ink3 + " bold")
        title.append(i18n.t("palette.hint"),
                     style=palette.ink4)
        self._title.update(title)
        self._options.remove_children()
        for position, command in enumerate(self.commands):
            # 宽度从 `COMMANDS` 里算出来（见 `view_state.COMMAND_NAME_WIDTH`）——
            # 手写过一次，然后 `/autopilot` 把这个 `<9` 顶穿了。
            text = Text(f"{command.name:<{view_state.COMMAND_NAME_WIDTH}}", style=(
                palette.accent if position == self._index else palette.ink2))
            text.append(command.hint, style=palette.ink4)
            self._options.mount(Static(text, classes=(
                "palette-option selected" if position == self._index
                else "palette-option")))


# --- 弹层 ----------------------------------------------------------------------

class Modal(Vertical):
    """弹层的主体：一条头 + 正文 + 底部按钮/说明。

    它**不是一个 `ModalScreen`**：`ModalScreen` 是"盖住下面一层"和键盘作用域，
    而这里要的只是"这几行怎么排"。分开了之后，四个面板的骨架就只写一遍。
    """

    def __init__(self, body_id: str, *children: Any, **kwargs: Any):
        kwargs.setdefault("id", body_id)
        super().__init__(*children, **kwargs)


class PermissionPanel(ModalScreen):
    """审批面板。**它是一个 `ModalScreen`，不是一个容器。**

    我第一版把它做成 `Vertical`，然后 `push_screen(panel)` —— Textual 8 直接拒了：
    `push_screen requires a Screen instance or str`。做成 Screen 还有第二个好处：
    `Esc` 关闭、焦点锁定、`dismiss(结果)` 这些由它保证，而"盖住下面一层"正是
    审批该有的样子。

    ## 三条约束照搬后端语义，一个字不改（设计稿 F3）

      * `Esc` = **拒绝**（fail-closed），和读不到输入那一支同一个方向；
      * 那句后果说明**原样显示** —— 它出自 `security/asker.py` 的 `_remember_hint`，
        UI 不许自己另写一句（那是同一份事实的第二个来源）；
      * 推不出命令前缀时后端**不提供** `t` 键，于是按钮**条件渲染** ——
        绝不补一个"总是允许整个 shell"，那正是 runtime 刻意堵掉的东西。
    """

    # 字母键。**F6 的键位表把 `[y] [n] [t] [a]` 列成审批的四个答案**，而按钮上
    # 也印着它们 —— 两边说的是同一件事，所以它们必须是同一份绑定。
    # 第三格是**文案键**，实例化时按当前语言取（见 `localize_bindings`）。
    BINDINGS = [
        ("escape", "deny", "bindings.deny"),
        ("y", "allow", "bindings.allow"),
        ("n", "deny", "bindings.deny"),
        ("t", "always", "bindings.always"),
        ("a", "always_group", "bindings.allow_all"),
    ]

    def __init__(self, request: dict[str, Any], tools: dict[str, dict[str, Any]],
                 palette: theme_mod.Theme, **kwargs: Any):
        super().__init__(**kwargs)
        self.request = request
        self.tools = tools
        self.palette = palette
        localize_bindings(self, translated_bindings(self.BINDINGS))

    def compose(self):
        info = self.tools.get(self.request.get("tool", ""), {})
        tool = self.request.get("tool", "?")
        risk = (self.request.get("risk") or info.get("risk") or "?").upper()
        kind = i18n.t("permission_dialog.kind_external") if tool.startswith("mcp__") \
            else i18n.t("permission_dialog.kind_builtin")
        hint = self.request.get("remember_hint")
        trust = self.request.get("trust_all_hint")
        with Modal("permission-body"):
            yield Horizontal(
                Static(Text(i18n.t("permission_dialog.head"),
                            style=self.palette.danger + " bold")),
                Static(Text(i18n.t("permission_dialog.risk_badge", risk=risk),
                            style=self.palette.danger),
                       classes="modal-badge badge-danger"),
                classes="modal-head",
            )
            yield Static(self._title(tool, kind, risk, info), classes="modal-title")
            yield Static(self._arguments(), id="permission-args")
            # **后果那句话原样显示，一个字都不改**（决策 16）。
            if hint:
                yield Static(self._hint("t", hint), classes="modal-hint")
            if trust:
                yield Static(self._hint("a", trust), classes="modal-hint")
            with Horizontal(id="permission-buttons"):
                yield Button(i18n.t("permission_dialog.allow"), id="allow",
                             variant="success")
                yield Button(i18n.t("permission_dialog.deny"), id="deny",
                             variant="error")
                # 只有后端说"这次能记住"时才给按钮。
                if hint:
                    yield Button(i18n.t("permission_dialog.always"), id="always")
                if self.request.get("allow_trust_all") and trust:
                    yield Button(i18n.t("permission_dialog.allow_all"),
                                 id="always_group")
            yield Static(self._footer(), classes="modal-foot")

    def _title(self, tool: str, kind: str, risk: str, info: dict[str, Any]) -> Text:
        """标题行。**从一个空的 `Text()` 开始拼，不用 `Text(x, style=...)`。**

        Rich 里 `Text("x", style=S)` 设的是**整段的基样式**，而后面 `append` 上去的
        片段会**叠加**在它上面 —— 于是"shell"那一格的 bold 会把后面那一整句也变成
        bold（实测：`内置工具 · 风险等级 HIGH` 整句加粗，看起来像强调错了地方）。
        基样式为空、每一段各自带样式，才是这里想要的分工。
        """
        text = Text()
        text.append(tool, style=self.palette.ink + " bold")
        text.append(i18n.t("permission_dialog.title", kind=kind, risk=risk),
                    style=self.palette.ink3)
        if info and not info.get("parallel_safe", True):
            text.append(i18n.t("permission_dialog.no_parallel"),
                        style=self.palette.ink3)
        if info.get("interactive"):
            text.append(i18n.t("permission_dialog.interactive"),
                        style=self.palette.ink3)
        return text

    def _arguments(self) -> Text:
        """参数块。**全文，不截断** —— 它和审计那条 200 字符预览是两回事：

        审计的限制是"jsonl 里不该抄全文"，而这里是**给人做判断的那份参数本身**
        （`permission_request.arguments` 在 schema 里就是这么写的）。shell 命令的
        重点常在后半句，截断等于让人在看不全的情况下签字。
        """
        text = Text()
        arguments = self.request.get("arguments") or {}
        if not arguments:
            return Text(i18n.t("permission_dialog.no_args"), style=self.palette.ink4)
        width = max(len(name) for name in arguments) + 2
        for index, (name, value) in enumerate(arguments.items()):
            if index:
                text.append("\n")
            text.append(f"{name:<{width}}", style=self.palette.ink4)
            text.append(str(value), style=self.palette.ink2)
        return text

    def _hint(self, key: str, text: str) -> Text:
        out = Text()
        out.append(f"{key} = ", style=self.palette.ink4)
        out.append(text, style=self.palette.ink3)
        return out

    def _footer(self) -> Text:
        return Text(i18n.t("permission_dialog.footer"), style=self.palette.ink4)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_deny(self) -> None:
        """`Esc` = **拒绝**，不是"关掉再说"。

        fail-closed：和 `cli_asker` 读不到输入那一支同一个方向（默认拒绝才是安全的
        失败方向）。
        """
        self.dismiss("deny")

    def action_allow(self) -> None:
        self.dismiss("allow")

    def action_always(self) -> None:
        # 后端没提供 `t` 时按 `t` 什么都不做 —— 不是拒绝，而是这个键**不存在**。
        if self.request.get("remember_hint"):
            self.dismiss("always")

    def action_always_group(self) -> None:
        if self.request.get("allow_trust_all"):
            self.dismiss("always_group")


class QuestionPanel(ModalScreen):
    """提问面板。和审批面板是两件事（见 tools/builtin/ask.py 的分工）。

    ## 两条从 F4 抄下来的约束

      * **跳过是一个显式的键**（它有自己的按钮和自己的 `Esc`）：回车在连续交互里
        是最容易做的动作，而"回车即跳过"和"回车即同意"一样糟 —— 前者会把"我没看
        清"变成一个默认答案；
      * **回车 = 选中当前那一条**，回给后端的是**选项原文**（编号只是界面的表示法，
        见 `tools/builtin/ask.py` 的 `_choose`）。
    """

    BINDINGS = [
        ("escape", "skip", "bindings.skip"),
        *[(str(number), f"choose('{number}')", "bindings.choose")
          for number in range(1, 10)],
    ]

    def __init__(self, request: dict[str, Any], palette: theme_mod.Theme,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.request = request
        self._options: list[str] = list(request.get("options") or [])
        self._index = 0
        localize_bindings(self, [
            *translated_bindings([("escape", "skip", "bindings.skip")]),
            *[(str(number), f"choose('{number}')",
               i18n.t("bindings.choose", n=number)) for number in range(1, 10)],
        ])

    def compose(self):
        with Modal("question-body"):
            yield Horizontal(
                Static(Text(i18n.t("question_dialog.head"),
                            style=self.palette.accent + " bold")),
                Static(Text("ask_user", style=self.palette.ink4),
                       classes="modal-badge"),
                classes="modal-head",
            )
            yield Static(Text(self.request.get("question", ""),
                              style=self.palette.ink + " bold"), classes="modal-title")
            header = self.request.get("header")
            if header:
                yield Static(self._header(header), classes="modal-hint")
            if self._options:
                yield Vertical(id="question-options")
            else:
                yield Static(Text(i18n.t("question_dialog.no_options"),
                                  style=self.palette.ink4), classes="modal-hint")
            with Horizontal(id="question-buttons"):
                yield Button(i18n.t("question_dialog.skip"), id="skip")
            # 那两句**各占一行**：它们说的是两件事（跳过为什么必须是显式的键 /
            # 回给后端的是什么），挤在按钮旁边会折成两行半，读起来像一句话。
            for line in self._footer_lines():
                yield Static(line, classes="modal-foot")

    def _header(self, header: str) -> Text:
        text = Text()
        text.append("header: ", style=self.palette.ink4)
        text.append(header, style=self.palette.ink3)
        return text

    def _footer_lines(self) -> list[Text]:
        return [
            Text(i18n.t("question_dialog.footer1"), style=self.palette.ink4),
            Text(i18n.t("question_dialog.footer2"), style=self.palette.ink4),
        ]

    def on_mount(self) -> None:
        self._paint_options()

    def _paint_options(self) -> None:
        # **没有选项时那个容器根本不存在**（compose 里就是条件渲染的），所以这里
        # 必须先看 `self._options`。第一版直接 `query_one`，症状是"一个问题没给选项
        # 时整个界面抛 NoMatches" —— 而那条路径平时不走（模型给的题基本都带选项），
        # 所以它是一颗埋着的雷（实测：一条只塞请求不 pump 的测试把它踩响了）。
        if not self._options:
            return
        options = self.query_one("#question-options", Vertical)
        options.remove_children()
        for index, option in enumerate(self._options):
            options.mount(Static(self._option_text(index, option),
                                 classes=("option selected" if index == self._index
                                          else "option")))

    def _option_text(self, index: int, option: str) -> Text:
        """一条选项。选中那条是 `▌` + **反白**（前景与背景互换）。

        为什么用反白而不是"强调色当底"：反白在**每一套主题**下都自带对比（它就是把
        前景和背景换过来），而一个色块底要和 13 套主题的正文色逐一对一遍 ——
        那是 13 次没必要的校对。`▌` 再给一个**形状**信号：单色终端上光靠颜色分不出
        选中项，而这个块字符在最暗的终端里也看得见。

        **选中那一行刻意不带任何行内样式**：它的颜色和底色由 CSS
        （`.option.selected`）给 —— 那样"反白"铺满的是**控件整行**，而不是文字那么长。
        第一版按内容宽度自己补空格，结果整条反白只有文字长（而且要在布局完成前
        去问容器的宽度，拿到的是 0）。
        """
        if index == self._index:
            return Text(f"▌  {index + 1}  {option}")
        text = Text()
        text.append(f"   {index + 1}  ", style=self.palette.ink4)
        text.append(option, style=self.palette.ink2)
        return text

    def _move(self, delta: int) -> None:
        if not self._options:
            return
        self._index = (self._index + delta) % len(self._options)
        self._paint_options()

    def on_key(self, event: Any) -> None:
        if event.key == "up":
            self._move(-1)
            event.stop()
        elif event.key == "down":
            self._move(1)
            event.stop()
        elif event.key == "enter":
            self.action_choose(str(self._index + 1))
            event.stop()

    def on_click(self, event: Any) -> None:
        widget = getattr(event, "widget", None)
        if widget is None or "option" not in getattr(widget, "classes", ()):
            return
        for index, child in enumerate(self.query(".option")):
            if child is widget:
                self._index = index
                self._paint_options()
                return

    def action_choose(self, number: str) -> None:
        try:
            position = int(number)
        except ValueError:
            return
        if not (1 <= position <= len(self._options)):
            return
        # 回**选项原文**而不是编号：编号是界面的表示法，而后端要的是内容
        # （`tools/builtin/ask.py` 的 `_choose` 做的就是同一件事）。
        self.dismiss(("answered", self._options[position - 1]))

    def action_skip(self) -> None:
        self.dismiss(("skipped", ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "skip":
            self.dismiss(("skipped", ""))


class SkillsPanel(ModalScreen):
    """`Ctrl+S`：**全部技能**（可用的 + 已加载的）。

    它和左栏那块的分工：左栏说"这个会话加载了什么"（`load_skill` 读过的），
    这里说"工作区里有什么" —— 后者此前只有 `main.py --skills` 那一条出口。
    """

    BINDINGS = [("escape", "close", "bindings.close"), ("q", "close", "bindings.close")]

    def __init__(self, state: view_state.ViewState, palette: theme_mod.Theme,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.state = state
        localize_bindings(self, translated_bindings(self.BINDINGS))

    def compose(self):
        loaded = {entry.get("name") for entry in self.state.skills}
        with Modal("skills-body"):
            yield Static(Text(i18n.t("skills.title"),
                              style=self.palette.ink + " bold"),
                         classes="modal-title")
            if not self.state.skill_catalog:
                yield Static(Text(
                    i18n.t("skills.empty"),
                    style=self.palette.ink4), classes="modal-hint")
            for skill in self.state.skill_catalog:
                name = skill.get("name", "?")
                text = Text()
                text.append("✓ " if name in loaded else "  ", style=(
                    self.palette.ok if name in loaded else self.palette.ink4))
                text.append(name, style=self.palette.skill)
                text.append(f"   {skill.get('description', '')}",
                            style=self.palette.ink4)
                yield Static(text, classes="skill-row")
            yield Static(Text(
                i18n.t("skills.footer"),
                style=self.palette.ink4), classes="modal-foot")

    def action_close(self) -> None:
        self.dismiss(None)


class OptionPicker(ModalScreen):
    """**从一份清单里挑一个**：`/model` 和 `/effort` 不带参数时弹这个。

    它替换掉的是"先 `/model` 看一眼清单、再把名字**一个字符不差地**打一遍"——
    两步里第一步只是为了把名字抄出来，而抄错的后果按设计稿那句是"只在账单上体现"
    （Pro 的未命中输入是 Flash 的四倍多）。名字可以又长又带 `provider/` 前缀，
    手抄它不该是唯一的输入方式。

    ## 三条约束，都是从它替代的那条路里学来的

      * **`Enter` = 选了，`Esc` = 什么都不做**（`dismiss(None)`）。`Esc` 刻意没有
        "关掉顺便做点什么"的语义：这一格决定下一次请求花多少钱、想多久，误触的
        代价比"没选"大得多（和 `SessionPicker` 同一条）；
      * **当前那一个带 `●`**：候选里一定有它，而不标出来的话"选了却没反应"看起来
        像坏了；
      * **数据是 runtime 给的**（`state.model_catalog` / `state.effort_levels`），
        界面不自己去读配置、更不写死档位 —— 理由见设计稿 16.2。

    ## 面板**不关**（和 `McpPanel` 同一条，和 `SessionPicker` 相反）

    按 `Enter` 之后面板留着：这一格**只有 runtime 知道成没成**（那条路由上有没有
    密钥、名字对不对），先关掉的话，"没换成"就只表现为一张关掉的浮层 —— 而那和
    "换成了一下子没看出来"分不开。出结果时 `App` 调 `show_result(...)` 把那句话
    原样写在面板底下，**关掉的时机交给用户按 `Esc`**（这一格改错了要花真钱，让人
    看清那句话再走）。想再选一个的话光标还在原处，再按 `Enter` 就换。

    这条路径上**界面不乐观更新**：`●` 跟着 `state`，而 `state` 只由 runtime 的快照改。

    `↑↓` 用 `on_key` 而不是 `BINDINGS`：和 `SessionPicker` / `QuestionPanel` 同一个
    理由 —— 这个面板没有焦点在可编辑控件上，键直接落到 screen 上；走绑定反而要和
    `App` 那一层的 `↑↓`（翻会话流）抢。
    """

    BINDINGS = [("escape", "close", "bindings.cancel")]

    class Chosen(Message):
        """在这个面板里按了 `Enter`。**带的是选中那一项的值**。

        ## 为什么不是 `dismiss(值)`

        `dismiss` 会**立刻把面板收掉**，而这个面板收得比那晚一点：选中的那一刻请求
        才刚发出去，`/model` 的成败要等 runtime 回话（那条路由有没有密钥）。先收掉的
        话，"没换成"就只表现为一张空掉的浮层 —— 和"换成了一下子没看出来"分不开。

        所以这里只**报一声**（消息冒泡到 `App`，由它去发那条协议消息），而面板**由
        用户按 `Esc` 关**（回话由 `App` 写在面板底下，见 `show_result`）。和
        `PromptArea.Submitted` 是同一条分工。
        """

        def __init__(self, value: str, screen: "OptionPicker") -> None:
            self.value = value
            # 面板自己**也随消息带出去**：`/theme` 那条路要在处理完之后把它收掉
            # （消息上没有 `.screen` 这个属性，实测 `AttributeError`）。
            self.picker = screen
            super().__init__()

    def __init__(self, title: str, options: list[view_state.Option],
                 palette: theme_mod.Theme, *, hint: str = "",
                 default_index: int | None = None,
                 items: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.title = title
        self.options = list(options)
        self.hint = hint
        # 一个"重新算一遍候选"的取数函数（`/model` `/effort` 传，`/theme` 不传）。
        #
        # **为什么需要它**：面板选完**不关**（见下面那段），而 `options` 是打开那一刻
        # 的快照 —— 不重算的话，换完之后 `●` 和那一档的高亮还停在**旧**那一项上，
        # 看起来像"刚才那一按没生效"（实测被用户一眼看出来）。所以 `App` 在
        # `ui(state)` 快照到了之后调 `refresh()`，这里拿最新的 state 重新画一遍。
        self._items = items
        self._index = self._initial_index(default_index)
        localize_bindings(self, translated_bindings(self.BINDINGS))

    def _initial_index(self, default_index: int | None) -> int:
        """光标初始停在哪儿。

        **默认停在当前那一个**（`●` 那一条），而调用方可以指定别的：`/model` 传
        "当前的下一个"，因为打开这个面板的人几乎总是想**换一个**，而模型清单长起来
        之后（多 provider）从第二项往下找比从中间往下找少按好几次；`/effort` 不传
        —— 档位只有三四个，"换一个"和"改一档"再按一次键的成本一样，那就干脆停在
        真值上（"我现在是哪一档"是这个面板要回答的第一个问题）。
        """
        if not self.options:
            return -1
        if default_index is not None:
            return default_index % len(self.options)
        for index, option in enumerate(self.options):
            if option.line.role == view_state.ROLE_WAITING:
                return index
        return 0

    def selected(self) -> view_state.Option | None:
        if not self.options or self._index < 0:
            return None
        return self.options[self._index]

    def show_result(self, text: str, *, ok: bool = True) -> None:
        """runtime 回话了：把那句话画到底部（**原样**，界面不另写一句）。"""
        foot = self.query("#option-result")
        if foot:
            foot[0].update(Text(text, style=(
                self.palette.ok if ok else self.palette.warn)))

    def reload_options(self) -> None:
        """拿**最新的** state 重算候选并重画（`●`、高亮和 note 都跟着走）。

        `App` 在每份 `ui(state)` 快照之后调它。**光标停在原来的序号上不动**：换完
        之后人往往还要再看一眼或者再换一个，把光标跳走会让"我刚才选的是哪一个"失去
        锚点 —— 而 `●` 已经把那件事说清楚了。

        没传取数函数的（`/theme`）什么都不做：那一屏选完就关，没有"等回话"这一段。

        **名字里没有 `refresh` 是有意的**：Textual 的 `Widget` 自己有一个
        `refresh(*, repaint=…)`，覆盖它会炸在"框架调 `self.refresh()` 重画"那一步
        （实测：`TypeError: got an unexpected keyword argument 'repaint'`）。
        """
        if self._items is None:
            return
        self.options = list(self._items())
        if self.options:
            self._index = min(max(self._index, 0), len(self.options) - 1)
        else:
            self._index = -1
        self._paint()

    # -- 画 -----------------------------------------------------------------

    def compose(self):
        with Modal("option-body"):
            yield Horizontal(
                Static(Text(self.title, style=self.palette.ink + " bold")),
                Static(Text(i18n.tn("option.count", len(self.options)),
                            style=self.palette.ink4),
                       classes="modal-badge"),
                classes="modal-head",
            )
            if self.options:
                yield Vertical(id="option-options")
                # 选中那一条的 note（"这条路由没有密钥"之类）画在清单**下面**：
                # 挂到那一行里会把行撑到折行，而折行会让"名字那一列"对不齐 ——
                # 那一列正是这个面板唯一要让人一眼扫完的东西。
                yield Static(Text("", style=self.palette.ink4), classes="modal-hint",
                             id="option-note")
            else:
                yield Static(Text(self.hint, style=self.palette.ink4),
                             classes="modal-hint")
            yield Static(Text("", style=self.palette.warn), classes="modal-foot",
                         id="option-result")
            yield Static(Text(
                i18n.t("option.footer"),
                style=self.palette.ink4), classes="modal-foot")

    def on_mount(self) -> None:
        self._paint()

    def _paint(self) -> None:
        note = self.query("#option-note")
        chosen = self.selected()
        if note:
            # note 为空时**整行收起来**（`display = False`）：留着一个空行会让面板
            # 底部多出一道说不清来历的空白，而它大多数时候是空的（只有 runtime
            # 给了 note 的那几条才有字）。
            text = chosen.note if chosen is not None else ""
            note[0].display = bool(text)
            note[0].update(Text(text, style=self.palette.ink4))
        if not self.options:
            return
        options = self.query_one("#option-options", Vertical)
        options.remove_children()
        for index, option in enumerate(self.options):
            selected = index == self._index
            text = Text()
            if selected:
                # 选中那条**不带行内样式**（除了那个块字符）：反白由 CSS 的
                # `.option.selected` 给，那样底色铺满整行而不是只有文字那么长
                # （和 `SessionPicker` 同一个坑）。前面补三个空格是让选中和未选中
                # 两条的**名字那一列对齐**（`▌` 和那三个空格一样宽）。
                text.append("▌  ")
                text.append(str(option.line))
            else:
                text.append("   ")
                # 分段着色走 `Line.segments`（和 `paint()` 同一条规矩）。
                for chunk, role in (option.line.segments
                                    or [(str(option.line), option.line.role)]):
                    text.append(chunk, style=style_of(self.palette, role))
            options.mount(Static(text, classes=(
                "option selected" if selected else "option")))

    # -- 键 -----------------------------------------------------------------

    def _move(self, delta: int) -> None:
        if not self.options:
            return
        self._index = (self._index + delta) % len(self.options)
        self._paint()

    def on_key(self, event: Any) -> None:
        if event.key == "up":
            self._move(-1)
            event.stop()
        elif event.key == "down":
            self._move(1)
            event.stop()
        elif event.key == "enter":
            self.action_choose()
            event.stop()

    def on_click(self, event: Any) -> None:
        widget = getattr(event, "widget", None)
        if widget is None or "option" not in getattr(widget, "classes", ()):
            return
        for index, child in enumerate(self.query(".option")):
            if child is widget:
                self._index = index
                self._paint()
                return

    def action_choose(self) -> None:
        option = self.selected()
        if option is None:
            return
        # **只报"选了哪一个"**：发请求、关面板、画回话都由 `App` 那一层做 ——
        # 和 `/resume` 那条分工一模一样。这里不能 `dismiss`（理由见 `Chosen`）。
        self.post_message(self.Chosen(option.value, self))

    def action_close(self) -> None:
        """`Esc` = **什么都不做**。见类 docstring 第 1 条。"""
        self.dismiss(None)


class SessionPicker(ModalScreen):
    """**选一个会话**（`/resume` 不带参数时弹这个）。

    它替换掉的是"先 `/list` 看一眼 id、再 `/resume <id>` 打一遍"—— 两步里第一步
    只是为了把 id 抄出来，而 id 是时间戳，抄错一位就切到另一个会话上（或者建一个
    新的）。

    ## 三条约束，都是从它替代的那条路里学来的

      * **`Enter` = 切过去，`Esc` = 什么都不做**（`dismiss(None)`）。这里刻意不给
        `Esc` 任何"关掉顺便做点什么"的语义 —— 换会话是会把当前 runtime 收掉的，
        而误触的代价比"没换"大得多；
      * **当前会话那一行带一个 `●`**（`view_state.session_row(conflict=True)`）：
        候选里一定有它，而不标出来的话"选中了却什么都没发生"看起来像坏了；
      * **数据是 runtime 给的**（`sessions` 那条消息），界面不自己去读
        `.tudouni/sessions/` —— 目录布局不是前端该认识的事实（见
        `protocol/messages.py` 里 `IN_SESSION_LIST` 那段）。

    `↑↓` 用 `on_key` 而不是 `BINDINGS`：和 `QuestionPanel` 同一个理由 —— 这个
    面板没有焦点在可编辑控件上，键直接落到 screen 上；走绑定反而要和 `App` 那一层
    的 `↑↓`（翻会话流）抢。
    """

    BINDINGS = [("escape", "close", "bindings.cancel")]

    def __init__(self, sessions: list[dict[str, Any]], current: str,
                 palette: theme_mod.Theme, **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.sessions = list(sessions)
        self.current = current
        self._index = self._default_index()
        localize_bindings(self, translated_bindings(self.BINDINGS))

    def _default_index(self) -> int:
        """默认选中**最新建的那个会话**（清单是按创建时间、最新的在前给的）。

        为什么不是"当前那一个"：打开这个面板的人几乎总是想换一个 —— 默认停在当前
        会话上会让人按一下 `Enter` 之后什么都没发生，而那是这个面板最坏的失败形态
        （看不出自己按成功了没有）。想留在原地的做法是按 `Esc`。
        """
        return 0 if self.sessions else -1

    def compose(self):
        with Modal("session-body"):
            yield Horizontal(
                Static(Text(i18n.t("session_dialog.head"),
                            style=self.palette.ink + " bold")),
                Static(Text(i18n.tn("option.count", len(self.sessions)),
                            style=self.palette.ink4),
                       classes="modal-badge"),
                classes="modal-head",
            )
            if not self.sessions:
                yield Static(Text(i18n.t("session_dialog.empty"),
                                  style=self.palette.ink4), classes="modal-hint")
            else:
                yield Vertical(id="session-options")
            yield Static(Text(
                i18n.t("session_dialog.footer"),
                style=self.palette.ink4), classes="modal-foot")

    def on_mount(self) -> None:
        self._paint()

    def _paint(self) -> None:
        if not self.sessions:
            return
        options = self.query_one("#session-options", Vertical)
        options.remove_children()
        for index, item in enumerate(self.sessions):
            selected = index == self._index
            row = view_state.session_row(item,
                                         conflict=item.get("session_id") == self.current)
            text = Text()
            if selected:
                # 选中那条**不带行内样式**（除了那个块字符）：反白由 CSS 的
                # `.option.selected` 给，那样底色铺满整行而不是只有文字那么长
                # （和 QuestionPanel 里那条同一个坑，见那里的说明）。
                text.append("▌  ")
                text.append(str(row))
            else:
                text.append("   ")
                # 分段着色走 `Line.segments`（和 `paint()` 同一条规矩），而不是
                # `paint().style` —— 后者拿到的是**整行的基样式**，把它套上去会把
                # 分段之间的差别抹平（`Line` 的分段正是为了这个才存在的）。
                for chunk, role in (row.segments or [(str(row), row.role)]):
                    text.append(chunk, style=style_of(self.palette, role))
            options.mount(Static(text, classes=(
                "option selected" if selected else "option")))

    def _move(self, delta: int) -> None:
        if not self.sessions:
            return
        self._index = (self._index + delta) % len(self.sessions)
        self._paint()

    def on_key(self, event: Any) -> None:
        if event.key == "up":
            self._move(-1)
            event.stop()
        elif event.key == "down":
            self._move(1)
            event.stop()
        elif event.key == "enter":
            self.action_choose()
            event.stop()

    def on_click(self, event: Any) -> None:
        widget = getattr(event, "widget", None)
        if widget is None or "option" not in getattr(widget, "classes", ()):
            return
        for index, child in enumerate(self.query(".option")):
            if child is widget:
                self._index = index
                self._paint()
                return

    def selected(self) -> str | None:
        if not self.sessions or self._index < 0:
            return None
        return str(self.sessions[self._index].get("session_id", "")) or None

    def action_choose(self) -> None:
        self.dismiss(self.selected())

    def action_close(self) -> None:
        """`Esc` = **什么都不做**。见类 docstring 第 1 条。"""
        self.dismiss(None)


class McpPanel(ModalScreen):
    """**MCP 服务器开关**（`/mcp` 不带参数时弹这个）。

    它和 `SessionPicker` 是同一族的（`↑↓` 选、`Enter` 执行、`Esc` 关），但有一条
    **刻意的差别：按 `Enter` 之后面板不关。**

    理由是用法不同：换会话是"选中一条 → 切过去"（一次性决定），而 `/mcp` 常见的
    用法是"把这两个都开上"—— 按一下就关掉的面板会逼人重打三次 `/mcp`。所以每次
    `Enter` 只是**发一条请求**，那一行随 runtime 回来的 `ui(kind=mcp)` 就地刷新。

    ## 三条约束

      * **行是从 runtime 的快照画的**（`state.mcp`），界面不做乐观更新。所以我们不
        会把"正在连"画成"已挂上" —— 而 `npx` 起不来时那句话会是假的；
      * **请求发出去到快照回来之间那一段，界面上要有字**（`正在等 runtime…`）。
        起一个 stdio server 要几百毫秒到几秒，而这期间如果屏幕一动不动，人只会以为
        自己没按到；如果正撞上一轮在跑（runtime 会先等那一轮跑完），那可能是几十秒
        —— 那就更必须有字。它在**任何一条快照回来时**清掉（不靠猜是哪一条）；
      * **`Esc` 什么都不做**（`dismiss(None)`）：开关是**立刻生效**的，没有"取消"这个
        概念 —— 关掉面板不会把已经挂上的 server 摘下来。

    面板上那行字里写着"只影响这次运行，不改 mcp.json"：这是这一屏最容易误解的地方
    （用户会以为在这里关掉就是永久关了）。
    """

    BINDINGS = [("escape", "close", "bindings.close"), ("q", "close", "bindings.close")]

    def __init__(self, state: view_state.ViewState, palette: theme_mod.Theme,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.state = state
        self._index = 0
        # "有一件事在等 runtime"：那一条的名字（没有就是 None）。见类 docstring 第 2 条。
        self._pending: str | None = None
        localize_bindings(self, translated_bindings(self.BINDINGS))

    # -- 数据 ---------------------------------------------------------------

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.state.mcp

    def selected(self) -> dict[str, Any] | None:
        rows = self.rows
        if not rows or self._index < 0 or self._index >= len(rows):
            return None
        return rows[self._index]

    def show_pending(self, name: str | None) -> None:
        """记下"有一条在等 runtime"并重画。`None` = 等的事结束了。"""
        self._pending = name
        self._paint()

    # -- 画 -----------------------------------------------------------------

    def compose(self):
        with Modal("mcp-body"):
            yield Horizontal(
                Static(Text(i18n.t("mcp_dialog.head"),
                            style=self.palette.ink + " bold")),
                Static(Text("", style=self.palette.ink4), classes="modal-badge",
                       id="mcp-count"),
                classes="modal-head",
            )
            if not self.rows:
                yield Static(Text(
                    i18n.t("mcp_dialog.empty"),
                    style=self.palette.ink4), classes="modal-hint")
            else:
                yield Vertical(id="mcp-options")
            yield Static(Text(
                i18n.t("mcp_dialog.footer"),
                style=self.palette.ink4), classes="modal-foot",
                id="mcp-foot")

    def on_mount(self) -> None:
        self._paint()

    def _paint(self) -> None:
        count = self.query("#mcp-count")
        if count:
            loaded = sum(1 for item in self.rows if item.get("state") == "loaded")
            count[0].update(Text(i18n.t("mcp_dialog.running", loaded=loaded,
                                        total=len(self.rows)),
                                 style=self.palette.ink4))
        foot = self.query("#mcp-foot")
        if foot:
            # 有件事在等 runtime 时，那行提示**替换**掉键位说明：接一个 stdio server
            # 要几百毫秒到几秒（`npx` 冷启动更久），而正撞上一轮在跑时可能几十秒 ——
            # 这期间屏幕一动不动只会让人以为自己没按到。
            foot[0].update(Text(
                i18n.t("mcp_dialog.pending", name=self._pending)
                if self._pending else
                i18n.t("mcp_dialog.footer"),
                style=self.palette.ink4,
            ))
        if not self.rows:
            return
        options = self.query_one("#mcp-options", Vertical)
        options.remove_children()
        width = max(cell_len(str(item.get("name", ""))) for item in self.rows)
        for index, item in enumerate(self.rows):
            selected = index == self._index
            text = Text()
            if selected:
                # 选中那条**不带行内样式**（除了那个块字符）：反白由 CSS 的
                # `.option.selected` 给（和 SessionPicker 里那条同一个坑）。
                text.append("▌  ")
                self._append_row(text, item, width, styled=False)
            else:
                text.append("   ")
                self._append_row(text, item, width, styled=True)
            options.mount(Static(text, classes=(
                "option selected" if selected else "option")))

    def _append_row(self, text: Text, item: dict[str, Any], width: int,
                    *, styled: bool) -> None:
        row = view_state.mcp_line(item, width)
        if not styled:
            text.append(str(row))
            return
        for chunk, role in (row.segments or [(str(row), row.role)]):
            text.append(chunk, style=style_of(self.palette, role))

    # -- 键 -----------------------------------------------------------------

    def _move(self, delta: int) -> None:
        if not self.rows:
            return
        self._index = (self._index + delta) % len(self.rows)
        self._paint()

    def on_key(self, event: Any) -> None:
        if event.key == "up":
            self._move(-1)
            event.stop()
        elif event.key == "down":
            self._move(1)
            event.stop()
        elif event.key in ("enter", "space"):
            self.action_toggle()
            event.stop()

    def on_click(self, event: Any) -> None:
        widget = getattr(event, "widget", None)
        if widget is None or "option" not in getattr(widget, "classes", ()):
            return
        for index, child in enumerate(self.query(".option")):
            if child is widget:
                self._index = index
                self._paint()
                return

    def action_toggle(self) -> None:
        """把选中的那一个反过来：在跑就卸，没在跑就挂。

        **动作由"它现在是什么状态"决定**，不由用户按了什么键决定 —— 这一屏只有
        一个键（`Enter`），而 `state` 是 runtime 给的。`failed` 那一档落进"没在跑"
        这一支，所以**再按一次就是重试**（这正是用户想要的，而且不需要为它多一个键）。
        """
        item = self.selected()
        if item is None:
            return
        name = str(item.get("name", ""))
        action = ("unload" if item.get("state") == "loaded" else "load")
        emit = getattr(self.app, "mcp_action", None)
        if emit is None:
            return
        emit(action, name)
        self.show_pending(name)

    def action_close(self) -> None:
        """`Esc` = **什么都不做**。开关是立刻生效的，没有"取消"这个概念。"""
        self.dismiss(None)


__all__ = [
    "BorderedPanel", "CommandPalette", "ContextRail", "ConversationLog",
    "HintPanel", "extra_hint_keys", "hint_box_height", "hint_keys",
    "hint_per_row",
    "LineBlock", "McpPanel", "MessageBlock", "OptionPicker", "Panel",
    "PermissionPanel", "QuestionPanel", "RecentPanel", "SessionBar",
    "SessionPicker", "SkillsPanel",
    "StartPanel", "StatusBar", "TopBar", "TurnBlock", "TwoPart",
    "WELCOME_BOX_HEIGHT", "WELCOME_HINT_TEXT", "WELCOME_HINT_WIDTH",
    "WELCOME_RIGHT_WIDTH", "WELCOME_STACK_COLUMNS",
    "WelcomeBlock", "color_of", "paint", "paint_lines", "style_of", "user_name",
    "workspace_name",
]
