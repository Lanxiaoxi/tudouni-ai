"""Textual 客户端：**跑在父进程里**，通过 stdio 协议驱动一个 runtime 子进程。

## 四件事必须说清楚，否则读这段代码会以为哪里写错了

### 1. 协议回调和 Textual 的界面更新**不在同一个线程**

`ProtocolClient` 有一个读线程在拆 stdout；Textual 的界面在它自己的事件循环里。
第一版想用 `App.call_from_thread` 直接跨过去，实测撞上两个问题：它在 **None 屏幕**
上抛 `ScreenError`（`init` 到达时界面还没挂载），而 Textual 的线程检查又不一定认得出
我们的读线程。

所以改成一个**消息泵**：协议回调只往一个 `queue.Queue` 里放东西（什么线程都能放，
这是 `queue` 的契约），界面用一个 50ms 的定时器去排空它。

代价说明白：**最多 50ms 的排版延迟**。对一个秒级往返的 agent 界面，这换来的确定性
更值钱 —— 而且它是"要么全到、要么晚到 50ms"，不会丢、不会乱序（`queue` 保序）。

### 2. "转圈"必须自己造

没有流式（决策 1），所以模型往返和工具执行期间**界面上不会有任何新东西**。
一次往返是秒级 —— 一个完全静止的界面会被当成卡死。所以状态栏那一行在
`working` 时会显示模型/工具**正在做什么**（`protocol/state.py` 的 `activity`），
而且它还数着这一轮已经跑了多久（"本轮 1.4s"随秒走动）。这是 v1 的验收项
（R6 第 1 条），不是打磨。

### 3. 审批是**非阻塞**的（对读线程而言）

`on_permission` 被读线程调用，它只把请求塞进队列、**立刻返回** —— 否则读线程就
卡在人身上了。真正的回答由界面在用户点按钮之后调 `client.answer_permission(...)`。
子进程那一侧本来就会一直等（`ProtocolServer.wait`），所以"等"发生在**它**那儿，
而不是在我们的读线程上。

### 4. 配色是运行时可换的（`/theme`）

14 套主题在 `theme.py` 里是纯数据，`_register_themes()` 把它们注册成 Textual 主题
（每个 token 变成一个 `$td-*` CSS 变量），而**自定义颜色的那些零件**（会话流、
左栏、状态栏）在换主题时要重画自己 —— `_repaint_all()` 就是那一步。CSS 变量那部分
由 Textual 自己重算，所以只有"用 Rich 手绘颜色的地方"需要这一趟。
"""

import queue
import sys
import time
from typing import Any

from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.theme import Theme as TextualTheme
from textual.widgets import Static

from agent_runtime.frontends.tui import theme as theme_mod
from agent_runtime.frontends.tui import view_state, widgets
from agent_runtime.protocol import messages
from agent_runtime.protocol import state as agent_state
from agent_runtime.protocol.client import ProtocolClient

# 每条上下栏的高度。**它们写在这里而不是 CSS 里**，因为"一共几行"是这个布局的
# 结构事实（顶栏 1 + 会话头 1 + 状态 1 + 输入 1 + 键位 1）—— 加上会话区至少 3 行，
# 这个界面最小要 8 行才不至于把会话区挤没。F5 那张 80×45 的图是它的正常形态。
_BAR = 1

# 命令面板最多几行。**从命令条数算出来，不手写数字。**
#
# 手写过一次（`max-height: 12`），而命令从 8 条长到 13 条之后，面板把**最后四条整个
# 吃掉了** —— 没有滚动条、没有省略号，`/tools` `/model` `/thinking` `/effort` 在面板
# 里根本不存在（实测：用户截图里 `/status` 就是最后一条）。这不是"有点挤"：面板是发
# 现命令的**唯一**入口，看不见的命令等于没有。
#
# 三行是固定开销：标题 1 + 上下边框各 1。最后再留一行余量 —— 面板顶到上限时那最后一条
# 贴着边框，读起来像是"下面还有"。
_PALETTE_MAX_ROWS = len(view_state.COMMANDS) + 3 + 1

# **主动去问面板快照的间隔**（秒）。只在**有后台任务悬着**时才会用到 ——
# 那些是唯一"会在没人在看的时候改变状态"的东西（一条两分钟的命令跑完了，而面板上
# 还写着"在跑"，那句话就是假的）。
#
# 为什么是 2 秒而不是每次 pump（50ms）：那会变成每秒 20 条空消息的**心跳**，而这条
# 消息存在的理由只是"安静的时候也有人问一句"。为什么不是 10 秒：一条跑完的命令要
# 让人盯着假状态看十秒，而它本来就是个真状态。
#
# 平时（没有后台任务）这条消息**一次都不会发**，所以它不占任何常态成本。
_STATE_REFRESH_SECONDS = 2.0


def _version() -> str:
    """`pyproject.toml` 里的版本号，读不到就返回空串。

    **不在这里抄一个 `"0.1.0"`**：一个数字两处写，迟早会漂，而漂掉的那一处
    （欢迎屏）没人会去核对。读文件失败不算错误 —— 它只影响欢迎屏的一行字，
    所以失败时安静地不显示（比让界面起不来好得多）。

    **往上找而不是数层数**：这个仓库的布局是"包目录就是仓库根"（`package = false`，
    见 pyproject 里那段），而 `frontends/tui/app.py` 到 `pyproject.toml` 正好是三层 ——
    但"正好三层"是个会随目录调整而失效的假设（第一版写成 `parents[3]`，实测拿到
    空串，而症状只是欢迎屏少一行字，没人会注意）。所以改成往上找那个文件。
    """
    try:
        import tomllib
        from pathlib import Path

        for parent in Path(__file__).resolve().parents:
            candidate = parent / "pyproject.toml"
            if candidate.is_file():
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                return str(data.get("project", {}).get("version", ""))
        return ""
    except Exception:  # pragma: no cover - 只在打包/裁剪过的环境里走到
        return ""


class TuiApp(App[None]):
    """那个界面。实现 `ClientHooks`（四个回调）。"""

    CSS = """
    Screen { background: $td-bg; color: $td-ink3; }

    /* --- 三条上下栏：顶栏 / 会话头 / 状态栏 ------------------------------- */
    #top, #session, #status {
        height: 1;
        background: $td-chrome;
        padding: 0 1;
    }
    /* **上下栏一律不换行**：它们是"一行一条"的东西，而 `activity` 的长短由事件决定
       （"要调用 edit_file（第 1 个）"就比"模型在想"长一倍）。允许换行的话，行数会
       随时变 —— 多出来的一行会把输入行顶走，而屏幕上看起来只是"状态栏偶尔少半句"
       （实测：`第 1 / 30` 后面的"步"被折到了第二行，而那一行在栏外）。 */
    .bar-left, .bar-right { text-wrap: nowrap; text-overflow: clip; }
    /* **两条栏的宽度都必须显式写。** 只给 `bar-right` 写 `1fr` 是不够的：
       `bar-left` 会按 `Static` 的默认宽度（`1fr`）把整行吃掉，`bar-right` 被挤成
       1 列 —— 于是顶栏右半（工作区 + `Ctrl+K`）**一个字都看不见**，而画面上看起来
       只是"右边空着"，像设计就是这么留白的（实测：这条是用户看截图时发现的，
       我自己的版式探针当时也打印了空行，但我没看出来）。 */
    #top .bar-left { width: 1fr; }
    #top .bar-right { width: auto; text-align: right; }
    #session .bar-left { width: 1fr; }
    #session .bar-right { width: auto; text-align: right; }
    #status .bar-left { width: 1fr; }
    #status .bar-right { width: auto; text-align: right; }

    #rail-summary { height: 1; background: $td-chrome; color: $td-ink4; padding: 0 1; }

    /* --- 主体：左栏 + 会话流 ---------------------------------------------- */
    #body { height: 1fr; }
    /* 滚动条：**设计稿里一条都没有**（沿右边缘取样：x2330→2382 是面板底、2382 就是
       卡片边，再过去是页面底）。左栏直接不出 —— 那几块内容就那么多，真溢出了还有
       `Ctrl+B` 和一行摘要那条路；会话流留 **1 格**中性滑块，因为长回合里"上面还有"
       这个事实需要一个出口，但它不该是原来那条 2 格宽的金色轨道。
       （Textual 默认 `scrollbar-size-vertical: 2`，滑块色由注册的 Textual 主题从
       `accent` 派生 —— 所以默认形态恰好是"最显眼的那种"。） */
    #rail {
        width: 32;
        background: $td-rail;
        border-right: solid $td-hairline;
        padding: 1 1;
        scrollbar-size-vertical: 0;
    }
    #log {
        background: $td-bg;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-background: $td-bg;
        scrollbar-background-hover: $td-rail;
        scrollbar-background-active: $td-bg;
        scrollbar-color: $td-hairline;
        scrollbar-color-hover: $td-ink4;
        scrollbar-color-active: $td-accent;
    }

    /* 上下文栏的一块：**左边一条色条当视觉锚点**。
       它是主题的 `line` 色（那正是这一套配色的"描边"角色）—— 每块共用一条竖线，
       眼睛顺着它就能看出"这一栏有几段"，而不是靠空白去猜。 */
    .rail-block {
        margin-bottom: 1;
        height: auto;
        border-left: solid $td-rail-bar;
        padding-left: 1;
    }
    /* 块头 = **一行**：`任务 · 2 / 5`（计数贴着标题，见 `RailBlock.show`）。
       它**没有自己的宽高分配**：颜色在控件里给（`ink4`），这里只管"它占一行"。 */
    .rail-head { height: 1; }
    .rail-lines { color: $td-ink3; }

    /* --- 回合块 ----------------------------------------------------------- */
    /* **每一个容器都要显式写 `height: auto`。** Textual 的 `Vertical` 默认是
       `height: 1fr`，而它嵌在一个 `height: auto` 的父块里时，`1fr` 会去撑满整个
       视口 —— 于是"一个回合块高 35 行"（里面只有 13 行内容），日志的虚拟高度
       虚高，`scroll_end()` 把回合头整个顶出可视区（实测：屏幕上只剩回合头那一条
       描边，看起来像"分隔线画出来了但标题没画"）。 */
    .turn { height: auto; margin-bottom: 1; }
    .turn-body { height: auto; }
    /* 回合头 = **一行**：标题 + 一串画出来的横线 + 状态（F1 的形态）。
       `nowrap` 是保险：横线长度是按宽度算的，万一算多一格，这里裁掉而不是折成两行。 */
    .turn-head {
        height: 1;
        text-wrap: nowrap;
        text-overflow: clip;
        margin-bottom: 1;
    }
    .turn-text { height: auto; }
    /* 思考正文 = **引用块**：底下压一层 `sunk`，左边那条 `│` 由行内容带
       （`view_state.QUOTE_BAR`）—— 底色划范围、竖线定边界，两根一起才像一个块。 */
    .think-body {
        height: auto;
        background: $td-sunk;
        padding: 0 1;
        margin-bottom: 1;
    }

    /* --- agent 正文：唯一按 Markdown 渲染的一块 ---------------------------- */
    /* 它是 `Markdown` 控件 —— 里面是**一棵子控件树**（`MarkdownParagraph` /
       `MarkdownH1` / `MarkdownFence` / `MarkdownTable` …），所以下面几条挑的是
       "树里的哪一类"，而不是"第几行"。配色仍然只从 `$td-*` 来：换配色时 Textual
       自己重算这些变量，正文不需要 `repaint()`。

       `padding` 必须显式写：`Markdown` 自带 `padding: 0 2 0 2`（那是它给"一整篇
       文档"的版式，左右各留两列）。这里收成一列 —— 那一列是给左边那条竖线的呼吸，
       再多正文就和工具行的缩进对不上了。 */
    .answer {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $td-ink2;
        background: transparent;
        /* **左边一条 accent 竖线 = 正文的视觉锚点。** 正文不再是行，没法像以前那样在
           行首拼一个 `●`（那个标记之所以能存在，是因为整段都被压成了 `str`）——
           而"这一段是它答的"需要一个形状来承担，否则答案和上面那几行工具输出连成
           一片，只能靠读字号去猜分界在哪。
           颜色取 `accent`，和输入框上下线、欢迎屏三个框、回合分隔线同一个 token：
           这一屏上"结构线"是一套颜色。`border` 占控件自己的一格，所以正文会比工具行
           往右挪一格（`padding` 再一格），正好和工具行文字的缩进对齐。 */
        border-left: solid $td-accent;
    }
    /* 标题：**左对齐、用最亮那档 ink**。`Markdown` 自带的 H1 是居中的（同样是
       "整篇文档"的版式），而这里是一段回复 —— 居中的标题看起来像另一块标题栏，
       和上面的回合头抢层级。 */
    .answer MarkdownH1, .answer MarkdownH2, .answer MarkdownH3,
    .answer MarkdownH4, .answer MarkdownH5, .answer MarkdownH6 {
        color: $td-ink;
        background: transparent;
        text-style: bold;
    }
    .answer MarkdownH1 { content-align: left middle; }
    /* 列表的记号压到最暗那档：它是标点，不该和正文抢注意力。 */
    .answer MarkdownBullet { color: $td-ink4; }
    /* 引用块复用思考块的那套语法（`sunk` 底 + 一条竖线），但竖线用主题的描边色：
       它是"别人的话"，和 agent 自己的话要分得开。 */
    .answer MarkdownBlockQuote {
        background: $td-sunk;
        border-left: outer $td-line;
    }
    .answer MarkdownHorizontalRule { border-bottom: solid $td-hairline; }
    /* 代码：**块和行内两种都要上 `sunk` 底**。行内那个是**组件类**（`.code_inline`，
       挂在 `MarkdownBlock` 上），所以选择器得写成"后代 + 类"—— 和 Textual 自带
       DEFAULT_CSS 里的写法一致。Textual 默认给行内代码染的是 `$warning` 底
       （这是它自己的主题语汇），换成 `sunk` 才和这套配色是一家人。 */
    .answer MarkdownFence { background: $td-sunk; color: $td-ink; }
    .answer MarkdownBlock > .code_inline { background: $td-sunk; color: $td-ink; }
    /* 表格：**按内容宽，不铺满整行**。Textual 默认是 `width: 1fr`，一个两列的
       小表也会被拉成整屏宽（两列各占半屏，看起来像排版坏了）；`auto` 在放得下时
       收到内容宽、放不下时照样压缩到可用宽度（实测 60 列下的宽表两种写法一致）。 */
    .answer MarkdownTable { width: auto; background: transparent; }

    /* --- 欢迎屏：上面两个方框并排，下面一个通栏的「提示」 ------------------ */
    /* **框的宽度和高度都写在这里，常数在 `widgets.py` 里**（`WELCOME_BOX_HEIGHT` /
       `WELCOME_RIGHT_WIDTH` / `WELCOME_HINT_WIDTH` / `WELCOME_STACK_COLUMNS` /
       `WelcomeBlock.BOX_LINES`），所以那几处要一起改 —— 有一条测试盯着它们对不对得上。
       分开的理由是它们服务的东西不同：宽度和摆法只受"这一屏有多宽"影响，而高度是
       "框里固定有那么几行内容"。

       三个框加起来 75 列宽（32 + 1 间距 + 42，提示框横跨这三个数），再加 `#log` 自己
       那两列 padding = 77 —— 在 200 列的终端上**不铺满整行**：三条又长又空的横条比
       留白难看得多。 */
    /* `min-height: 0` 是必须的：容器默认带一个按内容算的最小高度，几个框那么高时
       它会把"按内容长"撑成"填满整屏"（实测：不写这一行的症状是顶上的框线被顶出屏幕
       之外，而看起来像"这一屏是从中间开始画的"）。 */
    WelcomeBlock { height: auto; min-height: 0; }
    /* **外层是纵向的**：一行（两个框）+ 提示框。提示框那一行间距用 `margin-bottom`
       放在 `#welcome-row` 上 —— 它是纵向容器里唯一一个"子控件之间要留白"的地方。 */
    #welcome-body {
        layout: vertical;
        height: auto;
        min-height: 0;
        padding: 0 0 1 0;
        align: left top;
    }
    /* **这一行是 `Horizontal`，换版式靠 `layout: vertical`**（见 `_sync_layout`）：
       两个框是同一批控件、只是摆放方向变了，不需要拆了重挂（拆的过程中新容器会和
       还没摘掉的旧容器撞在同一个 id 上）。 */
    #welcome-row {
        layout: horizontal;
        height: auto;
        min-height: 0;
        margin-bottom: 1;
        align: left top;
    }
    /* 窄屏（< `WELCOME_STACK_COLUMNS`）：上面两个框各占满整行、上下摞起来；总高度超了
       就滚动（不然输入行会被顶出屏幕）。 */
    WelcomeBlock.stacked { height: 1fr; }
    WelcomeBlock.stacked #welcome-body { height: 1fr; overflow-y: auto; }
    WelcomeBlock.stacked #welcome-row { layout: vertical; }

    .welcome-box {
        /* **高度要比"内容行数 + 上下 padding"多两行**：Textual 算边框时会从 `height`
           里再扣掉两行，不够的话就在框底裁掉内容 —— 而画面上看起来只是"框里少了两行
           字"。两个方框的内容行数本来就定死（`BOX_LINES` = 10 行），所以这个数也是确定
           的：`10(正文) + 2(padding) + 2(边框) = 14` —— `WELCOME_BOX_HEIGHT` 记的就是
           它，有一条测试盯着两处一致。
           **这里原先写的是 12**（那条算式把 padding 漏了），症状是内容区只有 8 行、
           装不下 10 行正文。 */
        height: 14;
        background: $td-surface;
        /* **边框用交互色，和输入框那两条线同色**（`#input-box` 的 `border-top/bottom`）：
           这一屏上"有边框的东西"是同一类（欢迎屏的三个框、输入框），用同一个颜色才像
           一套。原先用的是 `hairline`（描边的弱化版），那几个框在 `surface` 底上几乎
           看不见边，看着像三块没有形状的色块。 */
        border: round $td-accent;
        padding: 1 1;
        color: $td-ink2;
    }
    /* **两个方框的宽度写在各自类上，不靠 `1fr`**：`1fr` 会让它们各占一半，而右边那个
       要装"多久以前 + 标题"两列，宽一点才不至于把标题全吃掉；左边只要放得下方块标和
       身份那一行。 */
    .start-box { width: 32; margin-right: 1; }
    .recent-box { width: 42; }
    /* 提示框横跨上面两个框（32 + 1 + 42 = 75）。高度 = 2 行键位 —— **这个框没有上下
       `padding`**（不像上面那两个）：那两行键位自己就是全部内容，再垫两行空白会让它比
       里面装的东西高出一截。Textual 还会从 `height` 里扣掉边框占的那两行，所以写 4
       正好画得出 2 行正文（实测）。 */
    .hint-box { width: 75; height: 4; padding: 0 1; }
    /* 叠起来时三个框都占满整行（那时候 `#welcome-row` 的 `layout` 是 vertical）。 */
    WelcomeBlock.stacked .start-box,
    WelcomeBlock.stacked .recent-box,
    WelcomeBlock.stacked .hint-box { width: 1fr; }
    /* 标题画在边框那一行上（`BorderedPanel` 用 `border_title`），所以它和正文不是
       同一档颜色：标题是"这一块叫什么"，正文才是内容。 */
    .welcome-box > .border_title { color: $td-ink4; background: $td-surface; }
    .welcome-line { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .start-line { text-align: center; }
    /* 提示框里的那两行键位：**它们可以折行**（放不下就换到下一行），所以是
       `height: auto` —— 和方框里其它"一行就是一行"的行不一样。 */
    .hint-line { width: 1fr; height: auto; }

    /* --- 命令面板 --------------------------------------------------------- */
    /* 高度**从命令条数算出来**（见 `_PALETTE_MAX_ROWS`）：手写过一次 12，而命令长到
       13 条之后面板把最后四条整个吃掉，且没有任何提示。 */
    #palette {
        height: auto;
        max-height: {_PALETTE_MAX_ROWS};
        background: $td-elevated;
        border: round $td-hairline;
        padding: 0 1;
    }
    /* 兜底：万一哪天命令多到装不下，**滑块是唯一能把它们翻出来的东西**。
       `overflow-y: auto` 只在真的溢出时才占那一格，所以常态下不付代价。 */
    #palette-options {
        height: auto;
        max-height: {_PALETTE_MAX_ROWS - 3};
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-background: $td-elevated;
        scrollbar-color: $td-hairline;
        scrollbar-color-hover: $td-ink4;
    }
    .palette-title { height: 1; }
    .palette-option { height: 1; }
    .palette-option.selected { background: $td-accent-soft; }

    /* --- 输入行（两行 + 上下高亮边框） ------------------------------------ */
    /* **这是这一屏唯一有高亮边框的东西**：上下两条 accent 线夹住两行输入。
       高度写 4 = 上边框 1 + 内容 2 + 下边框 1（Textual 的边框占控件自己的行）。
       `scrollbar-size-vertical: 0`：两行的框里塞一条滚动条会挤掉两个字符，
       而 TextArea 本来就会自动滚动让光标可见 —— 那条条什么也没多告诉他。 */
    #input-box {
        height: 4;
        background: $td-chrome;
        border-top: solid $td-accent;
        border-bottom: solid $td-accent;
    }
    #input-row { height: 2; }
    #prompt { width: 2; color: $td-accent; }
    #input {
        border: none;
        padding: 0 1;
        height: 2;
        width: 1fr;
        background: $td-chrome;
        color: $td-ink;
        scrollbar-size-vertical: 0;
    }

    /* --- 弹层 ------------------------------------------------------------- */
    PermissionPanel, QuestionPanel, SkillsPanel, SessionPicker { align: center middle; }
    #permission-body, #question-body, #skills-body, #session-body {
        width: 76;
        max-width: 96%;
        height: auto;
        background: $td-elevated;
        border: round $td-hairline;
        padding: 1 2;
    }
    .modal-head { height: 1; }
    .modal-head Static { width: 1fr; }
    .modal-badge { width: auto; text-align: right; }
    /* 风险芯片（F3 右上角那一枚）：终端里没有圆角，所以它是**一块底** + 内边距 ——
       这是这块画布上唯一能表达"这是一个标签"的做法。 */
    .badge-danger { background: $td-danger-soft; padding: 0 1; }
    .modal-title { height: auto; margin-bottom: 1; }
    .modal-hint { height: auto; color: $td-ink4; }
    .modal-foot { height: auto; color: $td-ink4; }
    #permission-args, #question-options, #skill-list, #session-options {
        background: $td-sunk;
        padding: 0 1;
        margin: 1 0;
        height: auto;
    }
    #permission-buttons, #question-buttons { height: auto; margin-top: 1; }
    /* 选项和技能行**没有行内样式**（除了未选中那些的灰阶）：选中那一条的反白靠
       这里的 `background` + `color`，而 `width: 1fr` 是让那块底**铺满整行**的关键
       —— 控件不占满宽度的话，反白只有文字那么长。 */
    .option, .skill-row { height: auto; padding: 0 1; width: 1fr; }
    .option.selected {
        background: $td-accent;
        color: $td-bg;
        text-style: bold;
    }
    Button { margin-right: 2; min-width: 10; }
    """

    # 面板那个高度**在这里代入**，不能写成 f-string。
    #
    # CSS 里有几百对花括号（每条规则一对），而 f-string 要求把它们全部写成 `{{` ——
    # 那是一次几百处的改动，而且以后每加一条规则都要记得转义（漏一个就是一次
    # `KeyError`／静默的格式错）。所以只把这一个值替换掉。
    #
    # **替换而不是写死**：写死过一次（`max-height: 12`），命令从 8 条长到 13 条之后
    # 面板把最后四条整个吃掉，而屏幕上没有任何提示（见 `_PALETTE_MAX_ROWS`）。
    CSS = CSS.replace("{_PALETTE_MAX_ROWS}", str(_PALETTE_MAX_ROWS)).replace(
        "{_PALETTE_MAX_ROWS - 3}", str(_PALETTE_MAX_ROWS - 3),
    )

    BINDINGS = [
        ("ctrl+c", "quit_app", "退出"),
        ("ctrl+t", "toggle_thinking", "思考"),
        ("ctrl+b", "toggle_rail", "上下文栏"),
        ("ctrl+k", "command_palette", "命令面板"),
        ("ctrl+s", "skills", "全部技能"),
        ("escape", "escape_key", "中断/关闭"),
        ("up", "palette_up", "上一条"),
        ("down", "palette_down", "下一条"),
    ]

    def __init__(self, session: str | None = None, *, autopilot: bool = False,
                 theme_key: str = theme_mod.DEFAULT_THEME, stream: bool = True):
        super().__init__()
        self._session = session
        self._autopilot = autopilot
        # 要不要让子进程出流。**默认开**（`--tui` 的意义就在这里），`--no-stream`
        # 能关掉。它只是"我们请求什么"，真正生效与否以 `init.stream` 为准 ——
        # 界面不拿这个值当事实（见 `view_state.ViewState.stream_enabled`）。
        self._stream = stream
        self.state = view_state.ViewState()
        # 协议回调往这里放（**任何线程都能放**），界面定时排空它。
        self._inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._client: ProtocolClient | None = None
        # 界面自己的开关。**这里不要出现叫 `_ready` 的属性**：`App` 自己有一个
        # `_ready()` 方法，而 `run_test()` 会去调它 —— 被一个 bool 盖住之后报的是
        # `TypeError: 'bool' object is not callable`，从栈上看完全指不到这里（实测踩过）。
        self._palette_visible = False
        self._version = _version()
        # 启动时向 runtime 要过一次会话清单了吗（欢迎屏右栏要它，只要一次）。
        self._sessions_requested = False
        # 那份还没回来的清单是**欢迎屏**要的吗（回了之后要区分它和 `/resume` 要的）。
        self._welcome_request_pending = False
        # `/autopilot` 请求的那个状态，还没等到 runtime 确认时记在这儿（None = 没在等）。
        # **它记的是"我们要的那个值"而不是"按过一下"**：回合收尾时也会来一条
        # `ui state`（`_run_turn` 的 finally），而它可能排在我们那条回应前面 ——
        # 只认"值对上了"才不会把中间那条当成回应（见 `_report_autopilot`）。
        self._autopilot_wanted: bool | None = None
        # 上一次主动问面板快照的时刻（`_maybe_refresh_state` 的节流）。0 表示还没问过
        # —— 第一次遇到"有东西悬着"时应该**立刻**问一次，而不是等满一个间隔。
        self._last_state_refresh = 0.0
        # 收到过多少块**思考链**。它只有一个用途：验收脚本要"等它开始流"——
        # 而 `state.stream_reasoning` 在回合收尾时会被清空，那时候它就答不出
        # "刚才到底流过没有"了。计数器只增不减，所以没有那个歧义。
        self.thinking_deltas = 0
        self._register_themes()
        self.theme = theme_key if theme_key in theme_mod.THEMES \
            else theme_mod.DEFAULT_THEME

    # -- 主题 ------------------------------------------------------------------

    @property
    def palette(self) -> theme_mod.Theme:
        """当前配色。**取的是 Textual 主题名对应的那一套**（`theme` 是它的键）。"""
        return theme_mod.get(self.theme)

    def _register_themes(self) -> None:
        """14 套设计稿配色 → 14 个 Textual 主题。

        每个 token 同时出现在两个地方，而且**都是必要的**：

          * `Theme.variables`（`$td-*`）—— 给 CSS 用（底、边框、内边距那些静态部分）；
          * `Theme` 自身的 `primary` / `warning` / `error` / `success` / `surface`
            等字段 —— 给 Textual 自带控件（`Button`、`Input` 的内建样式）用。

        漏掉后者会得到一个"自己的行是对的、按钮还是默认蓝"的界面 —— 而那种不一致
        在暗色主题上尤其脏。
        """
        for key in theme_mod.ORDER:
            palette = theme_mod.THEMES[key]
            p = palette.palette
            self.register_theme(TextualTheme(
                name=key,
                primary=p.accent,
                secondary=palette.skill,
                warning=p.warn,
                error=p.danger,
                success=p.ok,
                accent=p.accent,
                foreground=p.ink,
                background=p.bg,
                surface=p.surface,
                panel=p.chrome,
                dark=p.dark,
                variables=palette.variables(),
            ))

    def _set_theme(self, key: str) -> None:
        """换配色：先让 Textual 重算 CSS，再让手绘颜色的零件重画一遍。"""
        self.theme = key
        self._repaint_all()

    def _repaint_all(self) -> None:
        palette = self.palette
        for selector in ("#rail", "#top", "#session", "#status", "#log"):
            found = self.query(selector)
            if found:
                widget = found.first()
                repaint = getattr(widget, "repaint", None)
                if repaint is not None:
                    repaint(palette)

    # -- 组装 ------------------------------------------------------------------

    def compose(self):
        yield widgets.TopBar(id="top")
        yield widgets.SessionBar(id="session")
        yield Static("", id="rail-summary")
        with Horizontal(id="body"):
            yield widgets.ContextRail(self.palette, id="rail")
            yield widgets.ConversationLog(self.palette, id="log")
        yield widgets.CommandPalette(self.palette, id="palette")
        yield widgets.StatusBar(id="status")
        # 输入框 = **一个框**：上面一条 accent 色的线、下面一条，中间两行可输入。
        # 那两条线是这一屏唯一的"高亮边框"，因为它就是我现在要你操作的地方。
        with Vertical(id="input-box"):
            with Horizontal(id="input-row"):
                yield Static(">", id="prompt")
                yield widgets.PromptArea(
                    placeholder="说点什么，回车发送（/ 看命令，/resume 换会话，Shift+Enter 换行）",
                    id="input", highlight_cursor_line=False,
                )
        # 键位提示**不在这里**：它住在欢迎屏底下那个「提示」框里（`widgets.HintPanel`）
        # —— 说过第一句话之后这一屏就收了，而 `/help` 仍然列着完整的键位表。

    def on_mount(self) -> None:
        # `/` 打开的命令面板和输入行是同一个东西的两面：面板默认藏着。
        self.query_one("#palette", widgets.CommandPalette).display = False
        self._client = ProtocolClient(self, session=self._session,
                                      autopilot=self._autopilot,
                                      stream=self._stream)
        self._client.start()
        # 消息泵。见模块 docstring 第 1 条：**不用 call_from_thread**。
        self.set_interval(0.05, self._pump)
        self.query_one("#input", widgets.PromptArea).focus()
        self._refresh_chrome()

    # -- ClientHooks（**在读线程里被调用，只许入队**） -------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        self._inbox.put(("message", message))

    def on_permission(self, request: dict[str, Any]) -> str | None:
        """把请求塞给界面，**返回 None 表示"界面稍后自己回"**。

        **绝不能在这里返回一个兜底答案。** 第一版返回了 `messages.DENY`，想的是
        "稍后用真答案覆盖" —— 而客户端立刻就把那个 DENY 发出去了，子进程据此拒绝、
        继续往下跑；等用户点 [允许] 时那条回应已经没人要（更糟：中间那次拒绝进了
        审计，记成 `user_denied`）。实测的症状是"面板弹出来了、按钮也点了，
        但工具结果是「用户拒绝」"。

        所以这个回调**只负责转交**，回答是 `_ask_permission` 的 `answered` 回调
        通过 `client.answer_permission` 发的。
        """
        self._inbox.put(("permission", request))
        return None

    def on_question(self, request: dict[str, Any]) -> tuple[str, str] | None:
        """同 `on_permission`：转交，返回 `None`，回答由面板发。"""
        self._inbox.put(("question", request))
        return None

    # -- 消息泵 ----------------------------------------------------------------

    def _pump(self) -> None:
        """把队列里的东西画到界面上。**这是唯一改界面的地方。**

        ## 三条防御，都是实测出来的

        `set_interval` 的回调会在**界面还没挂载完**以及**界面已经开始拆**的时候也被
        调用（定时器不跟着 DOM 走）。而 `query_one` 在那两个时刻都抛 `NoMatches` ——
        症状是启动/退出时偶发一个 traceback，看起来像别的地方坏了。

        所以：每次拿控件都用 `_widget`（拿不到就跳过），而且消息**只弹一次** ——
        界面已经在拆了还把队列排空，只会往一个死 DOM 上写。
        """
        if not self.is_running:
            return
        # 后台任务会在**没人在看的时候**改变状态（一条两分钟的命令跑完了），而
        # `ui(state)` 本来只在几条由交互触发的时刻发 —— 所以这里主动去问一次。
        #
        # **只在真的有东西悬着时问，而且要节流**（见 `refresh_state_interval`）：
        # 没有理由的轮询会把"协议上每一条消息都有原因"稀释掉，而平时（没有后台任务）
        # 这条消息一次都不会出现。
        self._maybe_refresh_state()
        while True:
            try:
                kind, payload = self._inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "message":
                self._on_protocol_message(payload)
            elif kind == "permission":
                self._ask_permission(payload)
            elif kind == "question":
                self._ask_question(payload)
        self._refresh_chrome()

    def _widget(self, selector: str, expect: type):
        """拿一个控件；**现在还没有就返回 None**（而不是抛 `NoMatches`）。

        见 `_pump` 的 docstring：定时器会在 DOM 没准备好的时候也被调用。
        """
        found = self.query(selector)
        if not found:
            return None
        widget = found.first()
        return widget if isinstance(widget, expect) else None

    def _log(self) -> widgets.ConversationLog | None:
        return self._widget("#log", widgets.ConversationLog)

    def _maybe_refresh_state(self) -> None:
        """有后台任务悬着时，隔一会儿主动问一次面板快照。

        ## 它补的是哪个洞

        `ui(state)` 只在几条**由交互触发**的时刻发：开场、每条 `tool_result` 之后、
        回合收尾、几条命令之后。于是有一段安静时间里面板是**不会更新**的 —— 而
        **一条后台命令正好会在这段时间里跑完**。面板上那句"在跑"于是变成假话，方向
        和"把已启动当成已成功"相反，但同样是"界面上写着的事实不成立"。

        ## 判据是"有东西悬着"，不是"有后台任务"

        全都收干净了就不用问了：那张表不会再变。所以平时这条消息一次都不发 ——
        它不是一个心跳，而是一个**只在有理由的时候**才出现的询问（`state.jobs` 里
        还有 `running` / `uncollected` 就是那个理由）。

        它**不问"这一轮跑完了没有"**：那是 `run_finished` 那条事件的事，而且回合进行
        中本来就有 `tool_result` 在推快照。这里只补安静那一段。
        """
        if self._client is None:
            return
        outstanding = any(
            str(job.get("state", "")) in ("running", "uncollected")
            for job in self.state.jobs
        )
        if not outstanding:
            return
        now = time.monotonic()
        if now - self._last_state_refresh < _STATE_REFRESH_SECONDS:
            return
        self._last_state_refresh = now
        self._client.refresh_state()

    # -- 上下栏（每次 pump 都刷一遍：状态栏要随秒走动） -----------------------

    def _refresh_chrome(self) -> None:
        palette = self.palette
        # 上下文栏开合：**默认收起**，任务列表从无到有那一刻自动顶开一次
        # （决策 26 —— 是边沿，不是"有任务就一直开着"）。之后听用户的：`Ctrl+B`
        # 收得掉，宽度也不参与判断。判据全在 `should_auto_open` 里，这里只把结果
        # 贴到控件上。
        state = self.state
        want = view_state.should_auto_open(state)
        if want != state.rail_open:
            state.rail_open = want
        rail = self._widget("#rail", widgets.ContextRail)
        if rail is not None:
            rail.display = state.rail_open
            if state.rail_open:
                rail.show(state, palette)
        narrow = self.size.width < view_state.NARROW_COLUMNS
        summary = self._widget("#rail-summary", Static)
        if summary is not None:
            # 摘要**只在窄屏出现**（F5 的形态）：宽屏收起时，会话头右边那句
            # `Ctrl+B 上下文栏` 已经说明了怎么打开它，再来一行就是重复。
            show_summary = narrow and not state.rail_open
            summary.display = show_summary
            if show_summary:
                summary.update(view_state.rail_summary(state))

        # 宽度要传下去：三条栏在窄屏上各有各的降级（F5），而"现在多少列"只有
        # 这里知道（控件不该去问 App）。
        width = self.size.width
        for selector, widget_type in (("#top", widgets.TopBar),
                                      ("#session", widgets.SessionBar)):
            widget = self._widget(selector, widget_type)
            if widget is not None:
                widget.show(state, palette, width)
        status = self._widget("#status", widgets.StatusBar)
        if status is not None:
            status.show(state, palette, (time.monotonic(), width))

    def _say(self, text: str, role: str = view_state.ROLE_RULE) -> None:
        """往会话里说一句界面自己的话（命令回显、提示）。

        **不抛**：拿不到控件就当没说 —— 它只可能在界面还没挂载完或正在拆的时候发生，
        而那两个时刻没有"用户看不到这条提示"之外的后果。
        """
        log = self._log()
        if log is not None:
            log.add_lines([view_state.Line(text, role)], self.palette)

    def _say_lines(self, lines: list[view_state.Line]) -> None:
        log = self._log()
        if log is not None:
            log.add_lines(lines, self.palette)

    def _say_answer(self, text: str) -> None:
        """agent 的正文：**和 `_say_lines` 不是同一条路** —— 它按 Markdown 渲染。

        分开的判据是"这段字有没有自己的语法"，而不是"它重不重要"：正文有标题/列表/
        代码块，得整段交给 `AnswerBlock`（Textual 的 `Markdown`）；而过程行、工具行、
        提示是"一行一个说法"，继续走 `Line`。硬把两者塞进同一个入口的话，早晚会有人
        往正文里掺一行工具行 —— 那一行的 `[` `*` 会被 Markdown 当成语法吃掉。
        """
        log = self._log()
        if log is not None:
            log.add_answer(text, self.palette)

    # -- 协议消息 --------------------------------------------------------------

    def _on_protocol_message(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == messages.OUT_INIT:
            self._on_init(message)
        elif kind == messages.OUT_SESSION_LOAD:
            self._on_session_load(message)
        elif kind == messages.OUT_EVENT:
            self._on_event(message)
        elif kind == messages.OUT_DELTA:
            self._on_delta(message)
        elif kind == messages.OUT_DELTA_RESET:
            self._on_delta_reset(message)
        elif kind == messages.OUT_UI:
            self._on_ui(message)
        elif kind == messages.OUT_SESSIONS:
            self._on_sessions(message)
        elif kind == messages.OUT_NOTICE:
            level = message.get("level", "info")
            self._say(f"[{level}] {message.get('text', '')}",
                      view_state.ROLE_WARN if level == "warn" else view_state.ROLE_NOTICE)

    def _on_delta(self, message: dict[str, Any]) -> None:
        """一块流式内容。**逐块追加，不重画整段。**

        记账（`view_state.stream_delta`）和排版（`widgets.ConversationLog.add_stream`）
        分开，和事件那条路一样：前者是纯函数、可单测，后者要碰 DOM。

        **正文块走 Markdown，思考链走行。** 这一点由 `add_stream` 里那个 `kind` 决定，
        坐标就是 delta 自己的 `channel`（协议按字段名分流，见 `schema/outbound`）。
        """
        view_state.stream_delta(self.state, message)
        channel = message.get("channel")
        text = message.get("text") or ""
        kind = {"text": "answer", "reasoning": "think"}.get(channel or "")
        log = self._log()
        if log is None or kind is None or not text:
            return
        # 那一行怎么排（正文原样、思考链压平换行）是 `view_state.stream_lines` 的
        # 判断，这里只负责把它递给控件 —— 和事件那条路"渲染是纯函数"同一条规矩。
        lines = view_state.stream_lines(message)
        if lines:
            if kind == "think":
                self.thinking_deltas += 1
            log.add_stream(kind, lines[0][1], self.palette, run_id=message.get("run_id", ""))

    def _close_live_thinking(self, run_id: str) -> None:
        """一轮结束了：把**还在流的思考过程**收成折叠的那一行。

        ## 它为什么必须存在

        流式那一轮的思考过程是**铺开**的（"它正在想"的观感），而一轮收尾之后该回到
        和非流式一样的形态：`▸ 思考过程（N 字符 · Ctrl+T 展开）`。不收的话有两个
        具体后果，都不是审美问题：

          * 每一轮的思考过程都糊在屏幕上（非流式那一轮是折叠的，两种模式对不上）；
          * **`Ctrl+T` 会失灵**：它按 `ROLE_THINK_HEAD` 那一行找折叠块
            （`TurnBlock.toggle_thinking`），而流式那块的行全是 `THINK_BODY`
            —— 展开键会从它上面滑过去、去动 `model_call` 画的另一个折叠行。

        ## 为什么判据是 `stream_reasoning`，而正文用 `state.thinking`

        只有**这一轮的思考过程确实是从 delta 画出来的时候**才收：非流式
        （`--no-stream`、或者网关不支持流式）时屏幕上压根没有流式块，收了就是把
        `model_call` 画的那个折叠行换成另一行（或者收出一个重复的）。

        而**收进去的那个字符数要用 `state.thinking` 里那一份**（`model_call` 给的
        完整 reasoning），不用 delta 累计：后者可能被 `streamed_answer` 提前清掉
        （`ui(run_finished)` 到得比 `run_finished` 早的时候），而且它和审计里那份
        口径不一致。折叠行上的"N 字符"要和屏幕上展开时能看到的字对得上，
        所以两边都取同一份。
        """
        state = self.state
        if state.stream_run_id != run_id or not state.stream_reasoning:
            return
        text = state.thinking.get(run_id, ("", False))[0] or state.stream_reasoning
        log = self._log()
        if log is None:
            return
        log.close_stream("think", text)

    def _on_delta_reset(self, message: dict[str, Any]) -> None:
        """重试 / 重发：把**这一步**画出来的那半截丢掉。

        清哪一块是**算出来的**，不是猜的：正文和思考链各清一次（`discard_stream`
        只动最后一块、而且必须是流式那一版）。`view_state.delta_reset` 返回"真的
        清掉了东西没有"，所以一次没吐任何字的重试不会去白动 DOM。

        **不重新画一条提示**：屏幕上本来就是"正文缩回去了"，再补一句"重试中"是
        噪声 —— `model_call` 那条事件（status=error）已经说过一次重试了。
        """
        cleared = view_state.delta_reset(self.state, message)
        if not cleared:
            return
        log = self._log()
        if log is None:
            return
        for kind in ("answer", "think"):
            log.discard_stream(kind)

    def _on_init(self, message: dict[str, Any]) -> None:
        state = self.state
        new_session = message.get("session_id", "")
        # **换会话 = 换一屏。** 判定放在这里、而不是在"用户敲了 `/new`"那一刻，是
        # 有意的：换会话可能失败（权限文件坏了、MCP 起不来），而失败时 runtime 那
        # 一侧**原样保留旧会话**。界面要是抢先清了屏，用户就会看到一个空界面配一条
        # "换不过去"的提示 —— 而他的会话其实还在。
        #
        # 判据是 `init.session_id` 变了（第一条 init 时旧值是空串，天然成立）。
        switched = bool(state.session_id) and new_session != state.session_id
        if switched:
            state.reset_for_session()
            log = self._log()
            if log is not None:
                log.clear()

        state.session_id = new_session
        state.model = message.get("model", "")
        state.provider = message.get("provider", "")
        # 思考模式那两个旋钮：**开场就显示**（它们可能来自这个会话上次的选择 ——
        # `/thinking off` 之后恢复会话，那一格该还写着关）。
        state.thinking_on = message.get("thinking") is not False
        state.effort = str(message.get("effort") or state.effort)
        levels = message.get("effort_levels")
        if levels:
            state.effort_levels = tuple(str(level) for level in levels)
        state.max_steps = message.get("max_steps", 0)
        state.workspace = message.get("workspace", "")
        state.audit_path = message.get("audit_path", "")
        state.context_tokens = message.get("context_tokens")
        # `/model` 那张清单。**它不随会话变**（目录是常量数据），所以 `reset_for_session`
        # 不清它 —— 清掉的话，换一个会话之后 `/model` 会摆出一张空清单。
        catalog = message.get("model_catalog") or {}
        state.model_catalog = [dict(item) for item in catalog.get("models") or []]
        state.model_aliases = [dict(item) for item in catalog.get("aliases") or []]
        # **流式开不开以 runtime 为准**（它是运行期事实，也是 `--stream` 那一侧算出来的）。
        # 界面自己那个 `self._stream` 只是"我们请求了什么" —— 拿它当事实的话，
        # 一个不认识这个开关的老 runtime 会让界面等一堆永远不来的 delta，
        # 而最终那个 `answer` 又因为"以为流过"被丢掉，症状是**答案一片空白**。
        state.stream_enabled = message.get("stream") is True
        state.resumed = bool(message.get("resumed"))
        state.permissions = dict(message.get("permissions") or {})
        tools = message.get("tools") or []
        state.tool_risks = {tool["name"]: tool["risk"] for tool in tools}
        state.tool_info = {tool["name"]: dict(tool) for tool in tools}

        log = self._log()
        if log is None:
            return
        if not state.resumed:
            # 空态：**新会话还没说第一句话时那一屏**。它不是装饰，见
            # `widgets.WelcomeBlock` 的 docstring。
            log.show_welcome(state, self.palette, self._version)
            # 欢迎屏右栏要"最近动过哪几个会话"，而那份清单是异步来的（发一条
            # `session_list`，runtime 回一条 `sessions`）。**只在空态要它**：恢复会话
            # 时那一屏根本不会画，列一次几百个会话文件是白跑。
            self._ask_for_recent_sessions()
        resumed = "（继续）" if state.resumed else "（新的）"
        lines = [view_state.Line(f"（会话 {state.session_id}{resumed}）",
                                 view_state.ROLE_RULE)]
        for notice in message.get("notices") or []:
            code = notice.get("code", "")
            if view_state.notice_is_redundant(code):
                # 左栏常驻显示着同一件事（权限范围 / 已加载技能 / 任务），不再抄一遍。
                continue
            # **文字原样，不自己拼 `[code]` 前缀**：runtime 给的那句话开头已经写着
            # `[权限]` / `[技能]` 了，再加一个 `[permissions]` 就是同一件事说两遍
            # （实测：用户截图里那一行读起来像 debug 输出）。
            role = (view_state.ROLE_WARN if notice.get("level") == "warn"
                    else view_state.ROLE_RULE)
            lines.append(view_state.Line(notice.get("text", ""), role))
        # **把回来的办法说出来**。v1 这里写的是 `main.py --tui --session <id>`
        # （那时换会话只能重开进程）；现在它就是 `/resume <id>`，所以这句话也必须
        # 跟着改 —— 界面里指一条做不到的路，比不说更坏。
        if not state.resumed:
            lines.append(view_state.Line(
                "想回到这个会话：/resume（在列表里挑，● 标着当前这个）",
                view_state.ROLE_RULE))
        log.add_lines(lines, self.palette)
        if not state.resumed:
            # **最后再归位一次。** `show_welcome` 里那次滚动发生在欢迎屏刚摆好、
            # 上面这几行还没加进去的时候，而 Textual 把滚动位置留在了那一刻算出的
            # 最大值上（内容一变高，位置不会自己回到 0）—— 差的那一格正好把顶上那条
            # 框线推出屏幕，看起来像"这一屏从中间开始画的"。
            log.scroll_home(animate=False)

    def _on_session_load(self, message: dict[str, Any]) -> None:
        """恢复会话时重建画面。

        **它只画用户和 agent 说过的话，不画工具卡片**（决策 3：v1 不渲染工具卡片）。
        工具结果在 `messages` 里是全文（`role=="tool"`），想看就 `/history`。
        """
        restored = [
            m for m in (message.get("messages") or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if not restored:
            return
        lines = [view_state.Line(
            f"（恢复 {len(message.get('messages') or [])} 条历史，"
            f"下面是你说过的和 agent 答过的）", view_state.ROLE_RULE)]
        for msg in restored:
            role = view_state.ROLE_USER if msg["role"] == "user" else view_state.ROLE_ANSWER
            lines.append(view_state.Line(str(msg["content"]), role))
        self._say_lines(lines)

    def _on_event(self, message: dict[str, Any]) -> None:
        """一条审计事件 → 回合流里的行。

        **回合的边界在这里判**（`run_started` 开块、`run_finished` 回头改标题），
        而"事件怎么变成字"仍然是 `view_state.render_event` 那个纯函数的事。
        分开的好处很实在：块结构（布局）和行内容（判断）各自能单独测。
        """
        kind = message.get("kind")
        self.state.agent = agent_state.reduce(self.state.agent, message)
        lines = view_state.render_event(self.state, message)
        log = self._log()
        if log is None:
            return

        block: widgets.TurnBlock | None = None
        if kind == "run_started":
            turn = self.state.current_turn
            if turn is not None:
                # 界面自己数秒（"本轮 1.4s"）—— 纯函数里不取时间，所以起点在这儿记。
                turn.started_at = time.monotonic()
                block = log.start_turn(turn, self.palette)
        elif kind == "run_finished":
            # **流式那一轮的思考过程在这里收起来。** 它铺了一整轮，而收尾之后该回到
            # 和非流式一样的折叠形态（见 `_close_live_thinking`）—— 于是屏幕上
            # 仍然只有一行，两种模式看起来一致。
            #
            # 放在这条事件上而不是 `ui(run_finished)`：那条 `ui` 消息的顺序**不保证**
            # （协议文档明写两条靠 run_id 配对、别去补偿顺序），而这条一定先到。
            self._close_live_thinking(message.get("run_id", ""))

        body: list[view_state.Line] = []
        for line in lines:
            if line.role == view_state.ROLE_TURN_START:
                # 回合头由 TurnBlock 自己放（它要能被回填成最终形态），但**内容要用
                # `render_event` 那一条** —— 它写着"进行中 · 第 1 步"。丢掉它的话，
                # 进行中的回合头上只剩一个"回合 2"（实测：一眼看不出它在跑第几步）。
                if block is not None:
                    block.set_head(line)
                continue
            if line.role == view_state.ROLE_TURN_END:
                target = block or log.current_turn_block
                if target is not None:
                    target.set_head(line)
                continue
            body.append(line)
        log.add_lines(body, self.palette)

    def _on_ui(self, message: dict[str, Any]) -> None:
        if message.get("kind") == messages.UI_RUN_FINISHED:
            self.state.agent = agent_state.reduce(self.state.agent, message)
            # **正文走 Markdown，不走行。** 判据（"流过了就不画第二份"）在
            # `view_state.streamed_answer` 里 —— 它是纯函数，而且这一条是流式
            # 接上之后最容易出错的地方（同一个答案画两遍看起来像模型说了两遍）。
            answer = view_state.streamed_answer(self.state, message)
            if answer is not None:
                self._say_answer(answer.text)
            return
        if message.get("kind") == messages.UI_STATE:
            # 面板数据。**它不进对话流**：任务列表每更新一次就在流里插一段，会把
            # "你问的 + 它答的"冲稀。左栏就是它的位置。
            view_state.apply_state(self.state, message)
            self._report_autopilot()
            return
        if message.get("kind") == messages.UI_STATUS:
            # `/status` 的回包。**它进会话流**（不是面板）：那是"看一眼就走"的东西，
            # 而且它常常是在一轮跑着的时候问的 —— 那时候屏幕上正在滚的东西恰恰是
            # 你要看的，弹一个层把它盖住是本末倒置。
            self.state.status = dict(message.get("status") or {})
            self._say_lines(view_state.render_status(self.state, message))
            return
        if message.get("kind") == messages.UI_TOOLS:
            self.state.tools = [dict(row) for row in message.get("tools") or []]
            self._say_lines(view_state.render_tools(self.state, message))
            return

    def _report_autopilot(self) -> None:
        """`/autopilot` 的回声：**说的是 runtime 确认之后的那个值**。

        两条规矩，都是"不许乐观更新"那条的推论：

          * **只有我们在等这个值时才说话。** 开场那条快照也带 `autopilot`（`--autopilot`
            起来时是 true），而那种情况下 runtime 自己已经发过一条 notice 说过这件事，
            界面再复述一遍只是噪声；
          * **说实际值，不说希望值。** 值没对上来就什么都不说（`_autopilot_wanted`
            留着）—— 请求没被受理时（老 runtime 会忽略不认识的 `t`）报一句"已开启"
            是**假话**，而这一格恰恰是"接下来还会不会问你"。
        """
        if self._autopilot_wanted is None or self.state.autopilot != self._autopilot_wanted:
            return
        self._autopilot_wanted = None
        if self.state.autopilot:
            self._say("自动放行：开（需要审批的工具直接执行，审计里记 autopilot；"
                      "再执行一次 /autopilot 关闭）", view_state.ROLE_WARN)
        else:
            self._say("自动放行：关（恢复逐条询问）", view_state.ROLE_RULE)

    def _on_sessions(self, message: dict[str, Any]) -> None:
        """会话清单到了：**欢迎屏要的那一份就喂给它，否则弹选择面板**。

        两条路用的是同一份清单、同一个请求，所以判据只能是"现在是谁在等它"：

          * **空态那一屏要它**（右栏"最近活动"）。启动时就发了一次，而那时候用户
            根本没按过任何键 —— 那份回包要是顺手弹出一个选择面板，界面一起就盖着
            一张没人要的浮层；
          * **`/resume` 要它** → 弹面板。这条**不能只看"欢迎屏在不在"**：欢迎屏会
            一直留到第一句话为止，而"开着欢迎屏就把上次那个会话接回来"是完全正常的
            用法，所以这里认的是"这次请求是谁发的"那个记号。

        清单是**异步**来的（发一条 `session_list`，runtime 回一条 `sessions`），所以
        "请求"和"收到"分在两处。这样即使列清单慢（几百个会话文件），界面也不会卡在
        按键上 —— 菜单盘的开合是界面的操作，读盘是 runtime 的操作。
        """
        items = list(message.get("items") or [])
        self.state.recent_sessions = items
        if self._welcome_request_pending:
            # 只认**第一次**回包：欢迎屏只问一次，那之后发出的都是 `/resume` 要的。
            self._welcome_request_pending = False
            log = self._log()
            welcome = None if log is None else log.welcome_visible()
            if welcome is not None:
                welcome.show(self.state, self.palette, self._version, now=time.time())
                return
        self._show_session_picker(items)

    def _ask_for_recent_sessions(self) -> None:
        """向 runtime 要一次会话清单，**只要一次**。

        第二条 `init`（`/new` 之后）会再画一次欢迎屏，而清单没变 —— 每换一次会话就
        读一遍几百个会话文件是白跑，所以这里记一个"这次运行里已经问过了"。
        """
        if self._client is None or self._sessions_requested:
            return
        self._sessions_requested = True
        self._welcome_request_pending = True
        self._client.list_sessions()

    # -- 人机交互（非阻塞：塞回给子进程，而不是在这里等） ----------------------

    def _ask_permission(self, request: dict[str, Any]) -> None:
        # **先在会话流里留一行**，再弹面板：面板是盖住的，而"这一轮为什么停在这儿"
        # 要看记录的时候只有会话流能回答。
        self._say_lines([view_state.waiting_line(request)])
        panel = widgets.PermissionPanel(request, self.state.tool_info, self.palette,
                                        id="permission")

        def answered(decision: str | None) -> None:
            if self._client is None:
                return
            # `dismiss(None)`（Esc / 关掉）按**拒绝**处理：fail-closed，
            # 和 `cli_asker` 读不到输入那一支同一个方向。
            self._client.answer_permission(
                request.get("id", ""), decision or messages.DENY)

        self.push_screen(panel, answered)

    def _ask_question(self, request: dict[str, Any]) -> None:
        panel = widgets.QuestionPanel(request, self.palette, id="question")

        def answered(result: Any) -> None:
            if self._client is None:
                return
            status, text = result if isinstance(result, tuple) else ("skipped", "")
            self._client.answer_question(request.get("id", ""), status, text)

        self.push_screen(panel, answered)

    # -- 命令面板 --------------------------------------------------------------

    def on_text_area_changed(self, event: Any) -> None:
        """输入以 `/` 开头就打开面板（设计稿改动 6）。

        **不劫持普通输入**：面板只在 `/` 那一支出现，而它出现时输入行仍然是普通的
        输入行（面板只是把候选列出来）—— 所以"我想打一句以 / 开头的话"这件事
        不会因为面板的存在而变得不可能（回车执行的是选中的命令，而 Esc 关掉面板
        之后那句话还能接着打）。
        """
        palette = self._widget("#palette", widgets.CommandPalette)
        if palette is None:
            return
        value = event.text_area.text
        if value.startswith("/"):
            palette.show(value, self.palette)
            palette.display = True
            self._palette_visible = True
        else:
            palette.display = False
            self._palette_visible = False

    def _hide_palette(self) -> None:
        palette = self._widget("#palette", widgets.CommandPalette)
        if palette is not None:
            palette.display = False
        self._palette_visible = False

    def action_palette_up(self) -> None:
        """`↑`：面板开着就移动选择，否则滚会话流。

        **一个键两种用途是刻意的**：面板是浮层、它开着的时候用户的注意力在候选上，
        而面板关着时 `↑↓` 唯一合理的意思是"往回翻"。两个都做，比让 `↑↓` 在面板
        关着时变成死键好 —— 死键会让人以为界面卡了。
        """
        palette = self._widget("#palette", widgets.CommandPalette)
        if self._palette_visible and palette is not None:
            palette.move(-1)
            return
        self.scroll_log(-1)

    def action_palette_down(self) -> None:
        palette = self._widget("#palette", widgets.CommandPalette)
        if self._palette_visible and palette is not None:
            palette.move(1)
            return
        self.scroll_log(1)

    def scroll_log(self, delta: int) -> None:
        """翻会话流。`PromptArea` 在"光标已经到头"时也调它（见那里 `↓` 的说明）。"""
        log = self._log()
        if log is None:
            return
        (log.scroll_up if delta < 0 else log.scroll_down)(animate=False)

    def action_command_palette(self) -> None:
        """`Ctrl+K`：打开命令面板。**它不动你正在写的那句话。**

        输入框空着就插一个 `/`（面板自己就出来了，光标天然落在它后面）；已经有草稿
        就直接把面板盖上去 —— 草稿原样留着，`Esc` 关掉面板接着写。

        第一版是无条件 `field.text = "/"`，两个毛病叠在一起：**吃掉草稿**，而且
        `text` 设完之后光标落在 **0**（`cursor_position = 1` 也救不回来，被那次设置
        的重排冲掉）—— 接着打的字会插到 `/` 前面，面板立刻又关了。实测：`Ctrl+K`
        再按 `t` 得到 `t/`。用 `insert` 就没有这两个毛病（它插在光标处并推进光标）。
        """
        palette = self._widget("#palette", widgets.CommandPalette)
        field = self._widget("#input", widgets.PromptArea)
        if palette is None or field is None:
            return
        if not field.text:
            field.insert("/")
        palette.show(field.text or "/", self.palette)
        palette.display = True
        self._palette_visible = True
        field.focus()

    # -- 输入 ------------------------------------------------------------------

    def on_prompt_area_submitted(self, event: widgets.PromptArea.Submitted) -> None:
        """回车（或命令面板里选中一条）之后走这里。**先把输入框清空再处理。**

        先清空的理由：`submit()` 里可能开面板、也可能抛提示，而那些都要以
        "输入框现在是空的"为前提 —— 反过来（处理完再清）会让 `/theme` 那种
        "执行完还留着一行命令"的状态出现在两个地方。
        """
        field = self._widget("#input", widgets.PromptArea)
        if field is not None:
            field.text = ""
        self.submit(event.text)

    def submit(self, text: str) -> None:
        """处理一句用户输入。**不碰 Textual 的消息对象** —— 所以它能被直接单测。

        第一版把这段写在 `on_input_submitted` 里，于是测试只能去伪造一个
        `Input.Submitted` 再 `post_message` —— 而那条消息在 Textual 8 里怎么派发
        取决于焦点和订阅，测出来的是"消息路由对不对"，不是"我们的逻辑对不对"
        （实测：那条断言一直拿到空列表）。抽出来之后两件事分开测：这一段直接调，
        消息路由交给 Textual 自己。
        """
        text = text.strip()
        if not text:
            return
        if text.startswith("/") and self._palette_visible:
            palette = self._widget("#palette", widgets.CommandPalette)
            selected = palette.selected if palette is not None else None
            if selected is not None:
                # 面板里回车 = "执行选中的那条"（F2 的键位说明）。参数从打进去的
                # 那行里取 —— 面板只是候选，它不该替用户编参数。
                _name, _, rest = text.partition(" ")
                self._hide_palette()
                self._run_command(selected.name, rest.strip())
                return
        if self._handle_slash(text):
            return
        self.state.pending_input = text
        if self._client is not None:
            self._client.user_message(text)

    def _handle_slash(self, text: str) -> bool:
        """`/` 命令。**面板没开着时的兜底路径**（比如整行一次打完）。"""
        if not text.startswith("/"):
            return False
        command, _, rest = text.partition(" ")
        self._run_command(command.lower(), rest.strip())
        return True

    def _run_command(self, command: str, rest: str) -> None:
        """**所有 `/` 命令的唯一执行处**（面板和整行输入都走这里）。

        `/new` 和 `/resume` 从第二期起是**真的换会话**：它们发一条 `session_switch`
        给子进程，由 runtime 收掉当前 runtime、按新会话重新装配，然后重发
        `init` / `session_load` / `ui state`。**界面进程不动** —— 所以不需要退出重敲
        任何命令。

        这件事此前被明确列为"能做，但不该在第一版做"（设计稿 9.2）：它要求运行时
        能重新装配整张工具注册表，因为 `TodoBoard` / `SkillBoard` 绑在
        `session.metadata` 上。现在那份装配只有一处（`protocol/serve.py` 的
        `make_session_opener`），所以两条入口不会各装出一套不一样的运行时。
        """
        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            self._say_lines(self._help_lines())
        elif command == "/audit":
            self._say(f"审计日志：{self.state.audit_path}")
        elif command == "/new":
            self.switch_session(None)
        elif command == "/resume":
            self._command_resume(rest)
        elif command == "/skills":
            self.push_screen(widgets.SkillsPanel(self.state, self.palette,
                                                 id="skills"))
        elif command == "/status":
            self._command_status()
        elif command == "/tools":
            self._command_tools()
        elif command == "/model":
            self._command_model(rest)
        elif command == "/thinking":
            self._command_thinking(rest)
        elif command == "/effort":
            self._command_effort(rest)
        elif command == "/theme":
            self._command_theme(rest)
        elif command == "/autopilot":
            self._command_autopilot()
        else:
            self._say(f"没有这个命令：{command}（/help）")

    def _command_status(self) -> None:
        """`/status`：**请 runtime 说，别自己拼。**

        这一屏里的东西分成两类，而它们恰好都在 runtime 手上：会话规模、当前模型、
        工具个数是它的字段；tokens 和轮次要从**审计日志**里数出来（协议层读一次
        `runtime.logs`）。界面自己拼的话，第二类就得让前端去读
        `.tudouni/logs/` —— 那正是 `list_sessions` 立过的规矩：目录布局不是前端该
        认识的事实（store 明确留着"将来换 SQLite"的余地）。

        所以这里只发一条请求，答案到了由 `_on_ui` 渲染进会话流。
        """
        if self._client is None:
            return
        self._client.ask_status()

    def _command_tools(self) -> None:
        """`/tools`：同上，问 runtime 要那份带权限的清单。

        权限那一列**必须由 runtime 给**：哪几个等级自动放行、点名免问了谁、
        deny_tools 里有什么 —— 三条路都在 `PermissionPolicy` / `ApprovalMemory` 里。
        界面自己算就是第二份事实，而它漂掉的症状是"这里写着会问我，实际没问"
        （或者反过来，那是更坏的那个方向）。
        """
        if self._client is None:
            return
        self._client.ask_tools()

    def _command_thinking(self, rest: str) -> None:
        """`/thinking [on|off]`。**不带参数只报当前值，不做"切一下"。**

        和 `/theme` `/model` 同一条交互：轮换把"现在是什么"变成一个必须靠记忆的状态，
        而列一次的成本是零。

        带参数时**只认字面的 `on` / `off`**（不认 `开` / `关` / `true`）：协议上那
        一格是**布尔**，而"哪些词算开"如果由界面来判，就会有两个地方各自维护一份词表
        —— 漂开之后 `/thinking 开` 在 CLI 里管用、在 TUI 里报错，而那种差别没人查得出。
        所以界面只发协议认的两个字面量，剩下的写法由 CLI 那一支（它直连 runtime，
        可以调 domain 的折算函数）自己处理。认不出来的一律提示，不猜。
        """
        if not rest:
            self._say_lines(view_state.render_thinking(self.state))
            return
        word = rest.strip().lower()
        if word not in ("on", "off"):
            self._say(f"认不出这个写法：{rest}（用 /thinking on 或 /thinking off）")
            return
        if self._client is None:
            return
        self._client.set_thinking(word == "on")

    def _command_effort(self, rest: str) -> None:
        """`/effort [low|high|max]`。同上：不带参数只列档位。

        档位清单**由 runtime 随协议发**（`effort_levels`），界面不写死也不去 import
        内核 —— 这是决策 18 那条"前端只讲协议"的直接体现：它能长出 Web 前端的前提
        就是"前端不认识 runtime 的任何 Python 对象"。
        """
        if not rest:
            self._say_lines(view_state.render_effort(self.state, self.state.effort_levels))
            return
        if self._client is None:
            return
        self._client.set_effort(rest)

    def _command_model(self, rest: str) -> None:
        """`/model [名字]`。**不带参数只列清单，不做"轮换到下一个"。**

        和 `/theme` 完全同一条交互（理由也一样：轮换会把"我现在用的是哪个"变成一个
        必须靠记忆的状态，而列一次清单的成本是零）。

        ## 带参数时不自作聪明

        **认不出来的名字不就近匹配**：`/model flsh` 得到的是"没有这个模型"外加清单。
        配色选错一眼看得出来，而模型选错只会在账单上体现（Pro 的未命中输入是 Flash
        的四倍多），所以这里比 `/theme` 更没有理由去猜。

        改名那一半也**不在这里做**（不检查目录、不比较当前值）：目录是 runtime 的
        知识，请求发出去之后要么回来一条 state 快照（换成/已经是它），要么回来一条
        notice（没换成，含原因）。界面只负责把两种回包显示出来 —— 和 `/autopilot`
        不许乐观更新是同一条规矩。
        """
        if not rest:
            self._say_lines(view_state.render_models(self.state))
            return
        if self._client is None:
            return
        self._client.set_model(rest)

    def _command_autopilot(self) -> None:
        """`/autopilot`：切换"不再逐条问审批"那个模式。**开关在 runtime 手里。**

        ## 它和 `--autopilot` 是同一个模式，但语义变了一半

        设计稿第 15 条决策明确写过"**不给 `/autopilot`**"，理由是它是"这一次没有人
        可问"，而在一个有人看着的界面里按它语义矛盾。现在它回来了，因为那句话的
        前提可以拆开：**人在不在** 和 **要不要每一条都看** 是两件事。`--autopilot`
        说的是前者（无人值守），`/autopilot` 说的是后者（有人看着，但他选择不看）。
        审计里两者仍然分得开 —— 放行记的是 `outcome=autopilot`，而它和 `approved`
        （人按过 y）不是一回事。

        ## 界面只发请求，不先改显示

        真正生效的证据是 runtime 回来的那条 `ui state`（里面带 `autopilot`），
        而回声由 `_report_autopilot` 按那个值说。这和 `/new` 不许乐观清屏是同一条
        规矩 —— 在这一格上尤其要紧："灯亮着、其实还在逐条问你"会让人把真的审批
        面板当成误报点掉。

        发的是**绝对状态**（`not 现在这个值`），那个"现在这个值"来自 runtime 上一次
        的快照：重发同一条是幂等的，所以不存在"两条消息各切一次"的竞态。
        """
        if self._client is None:
            return
        self._autopilot_wanted = not self.state.autopilot
        self._client.set_autopilot(self._autopilot_wanted)

    def _command_resume(self, rest: str) -> None:
        """`/resume [id]`。带 id 直接切，不带就从列表里挑。

        **带 id 时不校验那个会话存不存在**：runtime 那边把它当"新会话"处理（和
        `--session` 同一条语义），而"我以为在接着聊、其实开了一个新的"这件事必须
        在界面上看得见 —— 所以 `init.session_id` 变了就换屏，而 `resumed=False`
        那行字会说明它是新的。在这里先查一次文件反而会多出第二份"什么算存在"的判断。
        """
        if rest:
            self.switch_session(rest)
            return
        if self._client is None:
            return
        # **先撤掉欢迎屏那个记号再发。** 这次要的清单是给选择面板的：不撤的话，
        # 启动时发出的那一条还没回来，它回来的那一份就会被欢迎屏吃掉，而面板永远
        # 不弹 —— 屏幕上一个变化都没有，看起来像 `/resume` 坏了（实测踩过）。
        self._welcome_request_pending = False
        self._say("正在取会话列表…")
        self._client.list_sessions()

    def switch_session(self, session_id: str | None) -> None:
        """请 runtime 换到某个会话（`None` = 新会话）。**界面先不做任何乐观更新。**

        这条规矩值得写下来：换会话可能失败（配置坏了、id 非法），而 runtime 那边
        失败时**保留旧会话**。界面要是在这里就把会话流清掉，失败之后用户看到的是
        一个空界面加一条"换不过去"—— 而他的东西其实还在。所以清屏只发生在
        `_on_init`（那条消息是"已经换好了"的唯一凭据）。
        """
        if self._client is None:
            return
        self._say(f"正在切到{'会话 ' + session_id if session_id else '新会话'}…")
        self._client.switch_session(session_id)

    def _show_session_picker(self, sessions: list[dict[str, Any]]) -> None:
        """弹会话选择面板；选中的那个切过去，`Esc` 什么都不做。"""
        panel = widgets.SessionPicker(sessions, self.state.session_id, self.palette,
                                      id="session-picker")

        def chosen(session_id: Any) -> None:
            if isinstance(session_id, str) and session_id:
                self.switch_session(session_id)

        self.push_screen(panel, chosen)

    def _command_theme(self, rest: str) -> None:
        """`/theme [名字]`。**不带参数就只列清单**（不做"轮换到下一套"）。

        轮换听起来方便，但它把"我现在是哪一套"变成了一个必须靠记忆的状态 ——
        而列一次清单的成本是零。
        """
        if not rest:
            self._say_lines([
                view_state.Line(f"当前配色：{self.theme} {self.palette.name}",
                                view_state.ROLE_WAITING),
                view_state.Line("14 套：", view_state.ROLE_RULE),
                *[view_state.Line("  " + part, view_state.ROLE_PROCESS)
                  for part in theme_mod.listing().split(" · ")],
                view_state.Line("换一套：/theme 石墨琥珀  ·  /theme a  ·  /theme 10",
                                view_state.ROLE_RULE),
            ])
            return
        key = theme_mod.resolve(rest)
        if key is None:
            self._say(f"没有这套配色：{rest}（/theme）")
            return
        self._set_theme(key)
        self._say_lines([view_state.seg(
            ("配色换成 ", view_state.ROLE_RULE),
            (f"{key} {self.palette.name}", view_state.ROLE_WAITING),
            ("（只影响这次运行）", view_state.ROLE_RULE),
        )])

    def _help_lines(self) -> list[view_state.Line]:
        lines = [view_state.Line("命令（输入 / 会打开面板，↑↓ 选、Enter 执行）：",
                                 view_state.ROLE_RULE)]
        for command in view_state.COMMANDS:
            lines.append(view_state.seg(
                # 那一列的宽度和命令面板共用同一个数（`COMMAND_NAME_WIDTH`）：
                # 两处各写一个的话，改了一处、另一处的间距就悄悄变了。
                (f"  {command.name:<{view_state.COMMAND_NAME_WIDTH}}",
                 view_state.ROLE_WAITING),
                (command.hint, view_state.ROLE_PROCESS),
            ))
            # 带参数那条的用法**另起一行、缩进对齐**（而不是塞进上面那一列）：
            # 那一列的宽度是从命令名算出来的，塞一段长文案进去会把整张表的对齐撑坏。
            if command.detail:
                lines.append(view_state.Line(
                    f"  {'':<{view_state.COMMAND_NAME_WIDTH}}{command.detail}",
                    view_state.ROLE_RULE))
        lines.append(view_state.Line("键位：", view_state.ROLE_RULE))
        # **和欢迎屏底下那个「提示」框读的是同一份表**（`widgets.HINT_KEYS_*`）：
        # 两处各写一遍的话，"改了键位、忘了改提示"早晚会发生。
        for key, what in [*widgets.HINT_KEYS_FULL, *widgets.HINT_KEYS_EXTRA]:
            lines.append(view_state.seg(
                (f"  {key:<12}", view_state.ROLE_WAITING),
                (what, view_state.ROLE_PROCESS),
            ))
        return lines

    # -- 动作 ------------------------------------------------------------------

    def action_toggle_rail(self) -> None:
        """`Ctrl+B`：折叠/展开上下文栏。**纯界面操作**，不改变任何 agent 的事实。

        按过一次之后 `rail_pinned` 置位：一次明确的操作不该被下一次**状态更新**推翻
        （否则"有任务就自动展开"会在用户刚收起它之后 50ms 又把它顶开）。所以自动展开
        只认"任务列表从无到有"那一个边沿 —— 见 `should_auto_open`。
        """
        self.state.rail_open = not self.state.rail_open
        self.state.rail_pinned = True
        self._refresh_chrome()

    def action_toggle_thinking(self) -> None:
        """展开/折叠**光标所在回合**的思考过程。

        v1 取的是 `list(state.thinking)[-1]`（最后一段），而那从第二回合起就会作用
        到错的那一段上 —— 画面看起来完全正常，所以属于最难查的那类。现在的对象是
        `turn_under_viewport()`：用户看到哪一块，就动哪一块。
        """
        log = self._log()
        if log is None:
            return
        block = log.turn_under_viewport()
        if block is None:
            self._say("（还没有回合）")
            return
        run_id = block.turn.run_id
        text, _expanded = self.state.thinking.get(run_id, ("", False))
        if not text:
            self._say("（这一轮没有思考过程）")
            return
        self.state.toggle_thinking(run_id)
        if not block.toggle_thinking(text):
            self._say("（这一轮的思考已经不在画面上了）")

    def action_skills(self) -> None:
        """`Ctrl+S`：全部技能（可用的 + 已加载的）。"""
        self.push_screen(widgets.SkillsPanel(self.state, self.palette, id="skills"))

    def action_escape_key(self) -> None:
        """`Esc`：**关闭面板，或者中断这一轮**（F6 的键位表）。

        两个语义共用一键是有意的，而顺序也是：弹层开着时它属于弹层（在审批面板里
        它是**拒绝**，fail-closed），没有弹层时它才是"停下这一轮"。

        **中断不是打断**：runtime 在两步之间停下（`agents/agent.py` 那个检查点），
        所以已经在跑的那一次模型往返或工具调用会跑完。这里只发请求，状态由
        `run_finished(cancelled)` 那条事件改 —— 界面不自己宣布"已停止"（那会是
        第二份事实）。
        """
        if self._palette_visible:
            self._hide_palette()
            field = self._widget("#input", widgets.PromptArea)
            if field is not None:
                field.text = ""
            return
        if self.state.agent.is_busy:
            if self._client is not None:
                self._client.interrupt()
            self._say("已请求停下这一轮（会在当前这一步结束后停）",
                      view_state.ROLE_WARN)
            return
        self._say("（这一轮没在跑 —— Esc 在弹层里是拒绝/跳过）")

    def action_quit_app(self) -> None:
        self.exit()

    # -- 收摊 ------------------------------------------------------------------

    def on_unmount(self) -> None:
        """界面关了就把子进程收掉。

        `ProtocolClient.close()` 是**先请求停止再等**（见那里的 docstring）——
        直接 kill 会让子进程死在半个回合上，留下一个此后发不出去的会话。
        """
        if self._client is not None:
            self._client.close()
            self._client = None


def run_tui(session: str | None = None, *, autopilot: bool = False,
            theme_key: str = theme_mod.DEFAULT_THEME, stream: bool = True) -> int:
    """`main.py --tui` 走这里。

    **配置错时子进程会以退出码 2 结束、并把原因打在 stderr 上。** 那一支由界面
    自然显示（stderr 是继承的，所以那几行会出现在终端上）—— 这条路径刻意不特殊
    处理"还没起来就失败"，因为它的表现是"界面闪一下就退"，而 stderr 上的原因是
    看得见的。
    """
    app = TuiApp(session=session, autopilot=autopilot, theme_key=theme_key,
                 stream=stream)
    app.run()
    client = app._client
    return 0 if client is None or client.exit_code in (None, 0) else client.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_tui(sys.argv[1] if len(sys.argv) > 1 else None))
