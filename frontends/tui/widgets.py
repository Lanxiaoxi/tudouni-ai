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

from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from agent_runtime.frontends.tui import theme as theme_mod
from agent_runtime.frontends.tui import view_state

# 行的角色 → 主题里的哪个角色。**这是整份设计稿"色彩 Token"那一节的落地处**：
# 上面（view_state）只回答"这一行是哪一类"，这里才回答"那一类是什么颜色"。
ROLE_ATTR: dict[str, str] = {
    view_state.ROLE_USER: "ink",
    view_state.ROLE_ANSWER: "ink2",
    view_state.ROLE_PROCESS: "ink3",
    view_state.ROLE_TOOL: "accent",
    view_state.ROLE_RESULT: "ok",
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
        right.append("    Ctrl+K 命令面板", style=palette.ink4)
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
        left.append(f"会话 {state.session_id or '（未命名）'}", style=palette.ink2)
        if state.resumed and not narrow:
            left.append("（继续）", style=palette.ink4)
        if state.model and not narrow:
            left.append(f"  ·  {state.model}", style=palette.ink3)
        if state.max_steps and not narrow:
            left.append(f"  ·  最多 {state.max_steps} 步", style=palette.ink3)

        right = Text()
        if narrow:
            right.append("Ctrl+B 上下文栏", style=palette.ink4)
            return left, right
        asking = [item.get("risk", "") for item in state.risk_scope
                  if item.get("disposition") == "ask"]
        if not state.risk_scope:
            right.append("权限 —", style=palette.ink4)
        elif asking:
            right.append("、".join(asking) + " 询问", style=palette.warn)
        else:
            right.append("全部自动放行", style=palette.ok)
        right.append("    Ctrl+B 上下文栏", style=palette.ink4)
        return left, right


class StatusBar(TwoPart):
    """底部那一行：**agent 在干什么**（左）+ **这一轮的成本**（右）。

    它是**投影**（见 `view_state.ViewState.status_left / status_right`），
    不是第二份事实。`payload` 是界面的 wall clock —— "本轮 1.4s"要随秒走动，
    而那个数只能由界面自己数（纯函数里不取时间）。

    右边那一段**以两个空格开头**：左段是 `1fr`，内容长的时候会被裁到边界上，
    于是"第 3 / 30 步"和"上下文"会挤在一起（实测：看不出这两段是两件事）。
    两个空格是这块画布上唯一的"栏间距"。

    **窄屏降级**：审计路径在 80 列上和左边撞车，所以窄屏只留前三个数
    （F5 那张图里状态栏右边就只剩用量）。
    """

    def render_parts(self, state, palette, payload=None):
        # 那个记号是**界面自己造的"转圈"**（没有流式，一次往返是秒级）：它的颜色
        # 跟着 phase 走，所以"在跑 / 答完了 / 被砍断"一眼能分开 —— 而文字部分
        # 一律是次级正文色，免得整行都在喊。
        now, width = payload if payload else (None, None)
        narrow = width is not None and width < view_state.NARROW_COLUMNS
        left = Text()
        mark, _, rest = state.status_left().partition(" ")
        left.append(mark, style=_phase_color(palette, state.agent.phase))
        left.append(f" {rest}", style=palette.ink2)
        return (left, Text("  " + state.status_right(now, compact=narrow),
                           style=palette.ink4))

    def repaint(self, palette: theme_mod.Theme) -> None:
        # 秒数在变，所以重画之前得重算一次（`render_parts` 会读 payload）。
        super().repaint(palette)


class KeyHintBar(Panel):
    """键位提示行：**设计稿 F6「交互键位」那张表的界面形态**。

    它按**当前列数**决定说几条：从左边开始放，放不下就**从右边少说一条**。理由是
    这一行紧贴着输入行，多出来的一行会把输入行顶走 —— 而终端里"少说一条键位"的
    代价远小于"输入行跑到屏幕外"（实测：122 列时最后那条 `Esc 中断本轮` 正好被裁掉，
    看起来像没实现这个键）。

    所以**顺序本身就是优先级**：`Esc` 排在 `Ctrl+S` 前面，因为前一个是"有东西要停下来"，
    后一个是可以另找入口的（输入 `/skills`）。

    窄屏（< 120 列）另外用一套更短的措辞：那几列连"思考过程"四个字都嫌长。
    """

    FULL = [
        ("Enter", "发送"), ("/", "命令面板"),
        ("Ctrl+T", "思考过程"), ("Ctrl+B", "上下文栏"), ("Esc", "中断本轮"),
        ("Ctrl+S", "全部技能"),
    ]
    NARROW = [
        ("Enter", "发送"), ("/", "命令"), ("Ctrl+T", "思考"), ("Esc", "中断"),
    ]
    # 上面两条是**这一行**的候选（放不下就从右边少一条）。这两条不进那一行，
    # 但 `/help` 要列出来：`Shift+Enter` 只在输入框里有意义（而输入框的占位符已经
    # 写着它），`Ctrl+K` 写在顶栏那一句里。**键位表要全，提示行要短。**
    EXTRA = [
        ("Shift+Enter", "输入框里换行（回车是发送）"),
        ("Ctrl+K", "命令面板"),
        ("↑ ↓", "面板选候选 / 光标移动 / 翻会话流"),
    ]

    def render_state(self, state, palette, payload=None):
        narrow = bool(payload) and payload < view_state.NARROW_COLUMNS
        pairs = self.NARROW if narrow else self.FULL
        # 一列的余量留给光标/边框：文本宽度按"中文算两列"估，宁可少说一条。
        budget = (payload or 0) - 2 if payload else None
        text = Text()
        for index, (key, what) in enumerate(pairs):
            piece = Text()
            if index:
                piece.append("  ·  ", style=palette.ink4)
            piece.append(key, style=palette.ink3)
            piece.append(f" {what}", style=palette.ink4)
            if budget is not None and text.cell_len + piece.cell_len > budget:
                break
            text.append_text(piece)
        return text


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

    def set_lines(self, lines: list[view_state.Line]) -> None:
        self.lines = list(lines)
        self.refresh_text()

    def refresh_text(self) -> None:
        self.update(paint_lines(self._palette, self.lines))

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        self.refresh_text()


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
        self.head_line = view_state.Line(f"回合 {turn.index}",
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
        """
        left, right, right_role = view_state.turn_head_parts(self.head_line)
        palette = self._palette
        width = self.size.width or 0
        gap = max(2, width - Text(left).cell_len - Text(right).cell_len - 3)
        text = Text()
        text.append(left, style=style_of(palette, self.head_line.role))
        text.append(" ")
        text.append("─" * gap, style=palette.hairline)
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
            block: LineBlock = chunk["block"]
            if chunk["kind"] == "think":
                # 展开着的思考正文：整块收掉，换成一行折叠提示。
                chunk["kind"] = "plain"
                block.set_classes("turn-text")
                block.set_lines([view_state.seg(
                    ("  ▸ 思考过程", view_state.ROLE_THINK_HEAD),
                    (f"（{len(text)} 字符 · Ctrl+T 展开）", view_state.ROLE_RULE),
                )])
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
                view_state.seg(
                    ("  ▾ 思考过程", view_state.ROLE_THINK_HEAD),
                    ("（展开 · Ctrl+T 收起）", view_state.ROLE_RULE),
                ),
                *[view_state.quote_line(part) for part in text.splitlines()],
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
        """这一回合里有没有思考过程（折叠着的算，展开着的也算）。"""
        return any(
            line.role == view_state.ROLE_THINK_HEAD or chunk["kind"] == "think"
            for chunk in self.chunks for line in chunk["block"].lines
        )

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

    def add(self, lines: list[view_state.Line]) -> None:
        if not lines:
            return
        if self._chunks:
            self._chunks[-1].append(lines)
            return
        block = LineBlock(lines, self._palette, classes="turn-text")
        self._chunks.append(block)
        self.mount(block)

    def repaint(self, palette: theme_mod.Theme) -> None:
        self._palette = palette
        for block in self._chunks:
            block.repaint(palette)

    @property
    def empty(self) -> bool:
        return not self._chunks


class WelcomeBlock(Panel):
    """空态：**新会话还没说第一句话时那一屏**（设计稿 F2 的右半）。

    它不是装饰。一个空白的会话区会让人以为程序没起来，而这里说的三件事
    （这个程序是什么、当前工作区/模型/预算、接下来能做什么）恰好是"第一次打开它"
    时唯一需要知道的。

    `payload` 是版本号。**左边那个方块标是自绘的**：终端里没有图片，而一屏空态
    没有任何视觉重量时，"这是哪个程序"这句话就得靠一个三行的标记来承担 ——
    它也顺带把 `tudouni` 和版本号、标语在左边对齐成一条线。
    """

    # 三行的小标记。**只用半块和全块字符**（▄▀█）：它们在等宽字体里都是"一个字符
    # 宽"的实心格，不会像有些图形字符那样在 CJK 字体下变成双宽而把版式顶歪。
    # 形状是个上窄下窄、中间鼓的方块（像一坨土豆泥），三行等宽，右边的字才对得齐。
    LOGO = (" ▄▄▄▄▄▄", "██▀▀██ ", " ▀▀▀▀▀▀")

    def render_state(self, state, palette, payload=None):
        text = Text()
        for index, row in enumerate(self.LOGO):
            if index:
                text.append("\n")
            text.append(row, style=palette.accent)
            if index == 1:
                text.append("    tudouni", style=palette.ink + " bold")
            elif index == 2:
                text.append(f"    {payload or ''}  ·  agent_runtime",
                            style=palette.ink4)
        text.append("\n\n")
        text.append("极小实现 · 由 DeepSeek 驱动的 agent 运行时", style=palette.ink2)
        text.append("\n")
        text.append(
            f"工作区 {state.workspace or '—'}  ·  模型 {state.model or '—'}"
            f"  ·  最多 {state.max_steps or '—'} 步",
            style=palette.ink3,
        )
        text.append("\n\n")
        text.append("快速上手", style=palette.ink3 + " bold")
        for line in (
            "直接说你要做什么，回车发送",
            "有风险的工具会先问你：shell 是 HIGH，write_file / edit_file 是 MEDIUM",
            "输入 / 打开命令面板；Ctrl+T 展开思考，Ctrl+B 收起上下文栏",
        ):
            text.append("\n  · ", style=palette.ink4)
            text.append(line, style=palette.ink3)
        return text


class RailBlock(Vertical):
    """上下文栏里的一块：标题 + 右侧计数 + 内容。"""

    def __init__(self, palette: theme_mod.Theme, *args: Any, **kwargs: Any):
        title = Static("", classes="rail-title")
        count = Static("", classes="rail-count")
        lines = Static("", classes="rail-lines")
        super().__init__(Horizontal(title, count, classes="rail-head"), lines,
                         *args, **kwargs)
        self._title = title
        self._count = count
        self._lines = lines
        self._data: tuple[str, str, list[view_state.Line]] = ("", "", [])

    def show(self, title: str, count: str, lines: list[view_state.Line],
             palette: theme_mod.Theme) -> None:
        self._data = (title, count, lines)
        self._title.update(Text(title, style=palette.ink3 + " bold"))
        self._count.update(Text(count, style=palette.ink4))
        self._lines.update(paint_lines(palette, lines))

    def repaint(self, palette: theme_mod.Theme) -> None:
        self.show(*self._data, palette)


class ContextRail(VerticalScroll):
    """左栏：**任务 / 已加载技能 / 权限范围 / 本次会话**（设计稿最值钱的加法）。

    这四块此前只有"另开一个终端"的出口（`--skills` / `--audit` / `--list`），
    放进栏里之后"agent 为什么这么做""我现在放行了什么"变成常驻可见。

    **它默认收起**（决策 1），而"检测到有任务/技能时自动展开"由 `app.py` 按窗口
    宽度算（`view_state.should_auto_open`）—— 这个控件只负责画。
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
            # 重建四块会在视觉上闪、也会打断滚动位置。
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
                     version: str) -> None:
        self._palette = palette
        if self._welcome is None:
            block = WelcomeBlock(classes="welcome")
            block.show(state, palette, version)
            self.mount(block)
            self._welcome = block
        else:
            self._welcome.show(state, palette, version)

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
        """往**当前回合**里加行；还没有回合就加到一个无回合块里。"""
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
        self._scroll_end()

    def _scroll_end(self) -> None:
        if self.is_mounted:
            self.scroll_end(animate=False)

    @property
    def current_turn_block(self) -> TurnBlock | None:
        return self._turns[-1] if self._turns else None

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
        ("shift+enter", "newline", "换行"),
        ("ctrl+k", "palette", "命令面板"),
        ("ctrl+c", "quit_app", "退出"),
        ("up", "up_or_palette", "上一条"),
        ("down", "down_or_palette", "下一条"),
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
    八条，模糊匹配会让"我打错了"和"它猜对了"长得一样。
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
        title = Text("命令面板", style=palette.ink3 + " bold")
        title.append("    输入命令名筛选  ·  ↑↓ 选择  ·  Enter 执行  ·  Esc 关闭",
                     style=palette.ink4)
        self._title.update(title)
        self._options.remove_children()
        for position, command in enumerate(self.commands):
            text = Text(f"{command.name:<9}", style=(
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
    BINDINGS = [
        ("escape", "deny", "拒绝"),
        ("y", "allow", "允许"),
        ("n", "deny", "拒绝"),
        ("t", "always", "总是允许"),
        ("a", "always_group", "都允许"),
    ]

    def __init__(self, request: dict[str, Any], tools: dict[str, dict[str, Any]],
                 palette: theme_mod.Theme, **kwargs: Any):
        super().__init__(**kwargs)
        self.request = request
        self.tools = tools
        self.palette = palette

    def compose(self):
        info = self.tools.get(self.request.get("tool", ""), {})
        tool = self.request.get("tool", "?")
        risk = (self.request.get("risk") or info.get("risk") or "?").upper()
        kind = "外部工具（MCP）" if tool.startswith("mcp__") else "内置工具"
        hint = self.request.get("remember_hint")
        trust = self.request.get("trust_all_hint")
        with Modal("permission-body"):
            yield Horizontal(
                Static(Text("⛨ 需要审批", style=self.palette.danger + " bold")),
                Static(Text(f"{risk} 风险", style=self.palette.danger),
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
                yield Button("允许 y", id="allow", variant="success")
                yield Button("拒绝 n", id="deny", variant="error")
                # 只有后端说"这次能记住"时才给按钮。
                if hint:
                    yield Button("总是允许 t", id="always")
                if self.request.get("allow_trust_all") and trust:
                    yield Button("都允许 a", id="always_group")
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
        text.append(f"   {kind}  ·  风险等级 {risk}", style=self.palette.ink3)
        if info and not info.get("parallel_safe", True):
            text.append("  ·  不可与其他工具并发", style=self.palette.ink3)
        if info.get("interactive"):
            text.append("  ·  会占用你的输入", style=self.palette.ink3)
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
            return Text("（没有参数）", style=self.palette.ink4)
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
        return Text(
            "Esc = 拒绝：fail-closed，和读不到输入那一支同一个方向"
            "  ·  裁决写进审计的 permission 事件",
            style=self.palette.ink4,
        )

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
        ("escape", "skip", "跳过"),
        *[(str(number), f"choose('{number}')", f"选 {number}")
          for number in range(1, 10)],
    ]

    def __init__(self, request: dict[str, Any], palette: theme_mod.Theme,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.request = request
        self._options: list[str] = list(request.get("options") or [])
        self._index = 0

    def compose(self):
        with Modal("question-body"):
            yield Horizontal(
                Static(Text("▣ agent 需要你的判断",
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
                yield Static(Text("（这个问题没有给选项，直接在输入行回答）",
                                  style=self.palette.ink4), classes="modal-hint")
            with Horizontal(id="question-buttons"):
                yield Button("跳过 Esc", id="skip")
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
            Text("回车是最容易做的动作，所以跳过必须是显式的一个键",
                 style=self.palette.ink4),
            Text("回给后端的是选项原文，不是编号 —— 编号只是界面的表示法",
                 style=self.palette.ink4),
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
        前景和背景换过来），而一个色块底要和 14 套主题的正文色逐一对一遍 ——
        那是 14 次没必要的校对。`▌` 再给一个**形状**信号：单色终端上光靠颜色分不出
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

    BINDINGS = [("escape", "close", "关闭"), ("q", "close", "关闭")]

    def __init__(self, state: view_state.ViewState, palette: theme_mod.Theme,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.state = state

    def compose(self):
        loaded = {entry.get("name") for entry in self.state.skills}
        with Modal("skills-body"):
            yield Static(Text("技能", style=self.palette.ink + " bold"),
                         classes="modal-title")
            if not self.state.skill_catalog:
                yield Static(Text(
                    "工作区里没有技能（.tudouni/skills/<名字>/SKILL.md）",
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
                "✓ = 已加载  ·  Esc 关闭  ·  完整清单：main.py --skills",
                style=self.palette.ink4), classes="modal-foot")

    def action_close(self) -> None:
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

    BINDINGS = [("escape", "close", "取消")]

    def __init__(self, sessions: list[dict[str, Any]], current: str,
                 palette: theme_mod.Theme, **kwargs: Any):
        super().__init__(**kwargs)
        self.palette = palette
        self.sessions = list(sessions)
        self.current = current
        self._index = self._default_index()

    def _default_index(self) -> int:
        """默认选中**最新那个会话**（清单是从新到旧给的）。

        为什么不是"当前那一个"：打开这个面板的人几乎总是想换一个 —— 默认停在当前
        会话上会让人按一下 `Enter` 之后什么都没发生，而那是这个面板最坏的失败形态
        （看不出自己按成功了没有）。想留在原地的做法是按 `Esc`。
        """
        return 0 if self.sessions else -1

    def compose(self):
        with Modal("session-body"):
            yield Horizontal(
                Static(Text("◱ 换一个会话", style=self.palette.ink + " bold")),
                Static(Text(f"{len(self.sessions)} 个", style=self.palette.ink4),
                       classes="modal-badge"),
                classes="modal-head",
            )
            if not self.sessions:
                yield Static(Text("还没有保存过任何会话 —— 说出第一句话之后才会有。",
                                  style=self.palette.ink4), classes="modal-hint")
            else:
                yield Vertical(id="session-options")
            yield Static(Text(
                "↑↓ 选择  ·  Enter 切过去  ·  Esc 取消   "
                "（● = 你现在所在的会话；切换会收掉当前会话的 runtime）",
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


__all__ = [
    "CommandPalette", "ContextRail", "ConversationLog", "KeyHintBar",
    "LineBlock", "MessageBlock", "Panel", "PermissionPanel", "QuestionPanel",
    "SessionBar", "SessionPicker", "SkillsPanel", "StatusBar", "TopBar",
    "TurnBlock", "TwoPart", "WelcomeBlock", "color_of", "paint", "paint_lines",
    "style_of",
]
