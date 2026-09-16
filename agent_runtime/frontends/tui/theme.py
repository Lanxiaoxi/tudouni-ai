"""TUI 的配色层：**13 套主题，纯数据。**

## 这个文件为什么存在（以及为什么它不认识 Textual）

设计稿把配色当成一件可以整体替换的事：F6 的「色彩 Token」定义**角色**
（底 / 栏 / 面板 / 正文三档 / 交互色 / 两个风险色），F8/F9 的九套色卡和 F7 的五套
候选各自给出一组**色值**。角色和色值分开之后，"换配色"就只是换一个表 ——
而不是去 20 处 CSS 里找那个 `#2997FF`。

所以这里只有 dataclass 和一张表，**一个 textual 的 import 都没有**：它要能被单测
（对比度、角色齐全、`/theme` 的匹配规则），而 `tests/test_imports.py` 里那条
"textual 只准出现在 app.py / widgets.py"也就不用为它开口子。

## 名字是两套：中文那两格和英文那两格

每套各有 `name`/`source`（中文）和 `name_en`/`source_en`（英文），取哪一套由
`name_in(lang)` / `source_in(lang)` 决定（`lang` 来自 `i18n.current()`，也就是
`ui.language`）。它们**不走 `i18n` 的两张目录表**：这两格是长在主题上的数据（和色值
同级），而"`/theme` 要同时认中文名和英文名"这条规矩只有在这里说得清（见 `resolve`）。

## 13 套是怎么来的

  * `P3 / P5 / P6 / P7 / P9`：**土豆泥九套色卡里留下来的五套**，按「5 色 → 界面九色」
    的规则扩展过（源文件 `配色Token-你的九套-9色.json`，每个值都带 lineage）。这五套的
    九个色值**原样照搬**，一个都不改 —— 它们是对比度校过的。
    （`P1 暖橄榄 / P2 海蓝橙 / P4 暖光 / P8 藏青陶土` 四套是用户裁掉的，连同它们
    在整个列表里的展示序号一起；留下来的 key 一个没改，所以 `/theme 7` 照旧是 `P7`。）
  * `A`–`E`：设计稿 F7 的**五套候选方案**（石墨琥珀 / 极地冷 / 墨绿仪器 / 紫夜 /
    纯碳）。它们的 `bg/chrome/ink/accent/warn/danger/ok/ink3` 来自 F7 卡片上印的
    色值，`surface/line/ink2` 三格 F7 没印字，是**从卡片上那三条色块取样的**
    （取样值记在 `source` 里，便于日后核对）。
  * `P3-T` / `A-T` / `A-T2`：**三套透明版**（见下一节），其余九格全部从它们各自的原版
    抄来 —— 深浅两档，`A-T2` 是从 `A-T` 再派生一层（只多交 `chrome` / `surface` 两格）。

## 透明版：底色交给终端，深度有两档

很多 CLI 的 TUI 看起来是"透"的 —— 终端底板什么色，对话区就什么色。做法是把那套
配色的 **`bg`（窗口底 / 对话区）换成一个特殊值 `ansi_default`**，也就是"终端自己的
默认底色"（Textual 的 `Color` 里有 `ansi=-1` 这一格，写出去是 SGR 49 而不是一个
真彩色）。Textual 8 的 `Screen.render()` 认得它，所以对话区那一大片**不涂任何颜色**，
露出来的就是终端自己的底板。

**"透"到哪几格由 `Palette.clear_roles` 记着**（`bg` 是每一套透明版都透的那一格，
不重复列）。现在有两档：

  * `P3-T` / `A-T`：**只有 `bg` 那一格透**。顶栏 / 状态栏 / 输入框（`chrome`）、
    开场那三个框（`surface`）、命令面板（`elevated`）、思考块与代码块（`sunk`）、
    左栏（`rail`）都还是实色 —— 层次感全靠这几块底色的差；
  * `A-T2 石墨琥珀 · 深透明`：**横栏和那三个框也不涂**
    （`clear_roles = ("chrome", "surface")`）。这是用户要的"再深一层"：`-T` 那两套上
    最显眼的仍然是顶栏 / 会话头 / 状态栏 / 输入框那四条色带，以及开场那三个方块
    （开始 / 最近 / 提示）——它们和对话区不是一片。这一套把 `chrome` 和 `surface`
    都交给终端，于是横栏、那三个框和对话区连成一整块，只剩文字和框线在上面。
    **别的八格一个字都不动** —— 框线照旧是主题色（输入框上下那两条 accent 线、那三个
    框的圆角边、回合分隔线、左栏色条都还在），文字照旧是琥珀那套。

    **代价（照实写）**：`surface` 不只是那三个框 —— 它同时是 Textual 自己那套变量里的
    `$surface`（见 `app.py` 的 `_register_themes`），而 `Button.-style-default`
    （弹层里的「始终允许」「跳过」那两个）正是拿它当底的。所以深透明那套里，**那两个
    按钮会变成只有上下半格边框的"扁"按钮**（成功 / 拒绝那两个按钮用的是 `$success` /
    `$error`，不受影响）。这是"把这一格交出去"的直接后果，不是漏改 —— 要留住它们的
    底色，就得把 `surface` 从 `clear_roles` 里拿掉。

派生角色（rail / elevated / sunk / hairline / …）**一律从原版那套的 `bg` 算出来**，
不认 `ansi_default`（一个非色值没法拿去插值）：左栏、思考块、命令面板跟着原版走。
这是刻意的 —— 连它们也透了，屏幕上就只剩文字和边框，左栏那种"安静的一块"会消失。

**代价也说清楚**：透出来的是终端底板，那就**不再由我们保证对比度**了。一套深色主题
配一个亮底板（或者反过来，`P3 粉紫` 是亮色主题配深底板）会难读 —— 这是这个功能
本身的性质，不是可以在这里修掉的东西。选它的人自己看得到。

## 派生角色：为什么不是"每套再手写八个色值"

九套 token 只给了九个角色，而界面要用十几个（左栏底、弹层底、思考块底、细描边、
占位色……）。**手写会引入 13×8 = 104 个没人校对过的色值**，而且每套的观感会开始
漂。所以除 token 之外的角色一律**按固定规则从 token 推导**（见下面 `_derive`），
规则只有三条，而且对每一套一视同仁：

  1. **抬高明度**（rail / elevated）：往正文色方向插值 —— 深色主题是变亮，
     亮色主题（`P3 粉紫` 和它的透明版 `P3-T` 两套）是变暗，方向由 `dark` 决定；
  2. **压低明度**（sunk）：往底色之外的那一端插值，用在思考内容块那种"陷进去"的底；
  3. **弱化**（hairline / ink4 / accent_soft / danger_soft / skill）：往底色方向
     插值 —— 保留色相，降对比。

## 终端里的"字号"

F6 的 `11 / 12 / 14` 三档字号在终端里不存在（一个终端只有一种字号），所以那三档
**落到颜色上**：正文 `ink`、过程行 `ink3`、区块标题与键位提示 `ink4`。
这不是翻译损失，是同一个层级意图在另一块画布上的形态 —— 而它恰好也是终端里
唯一能表达层级的手段。
"""

from dataclasses import dataclass, field

from agent_runtime import i18n

# 默认主题。**用户拍的板**：`A 石墨琥珀`（展示序号 10）—— 它取代了此前定的 ⑦ 靛夜。
# 靛夜那一套本身没动（它的 accent 仍然是提亮过的 `#7670EF`，原色 `#463DE8` 在
# `#161616` 上只有 2.3:1，肉眼几乎看不见），只是不再是启动默认。
DEFAULT_THEME = "A"


@dataclass(frozen=True)
class Palette:
    """一套主题的九个 token（+ 两个透明版的标记）。**色值全部原样，不做二次调整。**"""

    key: str
    name: str
    source: str
    dark: bool
    bg: str          # 窗口底 / 对话区（透明版是 `ansi_default`）
    chrome: str      # 顶杠、状态栏、输入行
    surface: str     # 面板底（比 chrome 再上一档）
    line: str        # 描边与分隔线（带色相，是这套主题性格的一部分）
    ink: str         # 正文
    ink2: str        # 次级正文 / agent 回答
    ink3: str        # 元信息 / 过程行
    accent: str      # **唯一**的交互信号色
    warn: str        # MEDIUM 风险
    danger: str      # HIGH 风险 / 拒绝
    ok: str          # 工具成功返回

    # --- 变体：透明版（目前只有 `A-T` / `P3-T` 两套，见模块 docstring）----------
    #
    # **为什么这两格也在这里、而不是另开一张表**：透明版的另外九格和原版**一个字
    # 都不许差**，而"差没差"这件事只有在同一张表里才看得出来（`_transparent` 就是从
    # 原版那一条算出来的），这两格是它的标记。
    transparent: bool = False
    """这套是不是"透明版"（`bg` 是终端自己的底色而不是一个色值）。"""

    base_bg: str = ""
    """**原版那套的 `bg`**，只用来算派生角色。透明版自己那位是非色值（`ansi_default`），
    拿去插值会抛 —— 而左栏、思考块、命令面板这些该跟着原版走，不该跟着终端底板走。"""

    # 英文名与英文出处。**它们在最后、而且带默认值**：这些表是按位置构造的，插在中间
    # 会把每一个 `Palette(...)` 的位置参数全部错位。
    #
    # 为什么名字不走 `i18n` 的两张目录表：这两格是**长在主题上的数据**（和色值同级），
    # 而 `/theme` 的匹配要**同时**认两套名字 —— 见 `resolve()`。
    name_en: str = ""
    source_en: str = ""

    # 除 `bg` 之外**也交给终端**的那几格（透明版的深浅就靠它区分，见模块 docstring）。
    #
    # **为什么 `bg` 不在里面**：每一套透明版都透它，"是不是透明版"由 `transparent`
    # 一个字段回答就够了；这里记的只是"还多透了哪几格"。`A-T2` 之前它一直是空元组，
    # 而空元组正是"只透最底下那一层"这句话的数据形态。
    clear_roles: tuple[str, ...] = ()
    """`ansi_default` 的那几格（除 `bg`）。现在是 `A-T2` 的 `("chrome", "surface")`。"""

    def name_in(self, lang: str) -> str:
        """这套配色在某种语言下的名字。**英文缺了就回中文**（不显示空白）。"""
        return self.name_en if lang == i18n.EN and self.name_en else self.name

    def source_in(self, lang: str) -> str:
        """出处那一小格（`色卡①` / `F7-A · 默认`）的对应说法。"""
        return self.source_en if lang == i18n.EN and self.source_en else self.source


# --- 颜色工具（纯函数，便于单测） ---------------------------------------------

def _rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def blend(base: str, toward: str, amount: float) -> str:
    """在 `base` 和 `toward` 之间插值。`amount=0` 给 `base`，`=1` 给 `toward`。

    **两个入参都必须是真色值（`#RRGGBB`）。** 透明版那套的 `bg` 是 `ansi_default`
    ——"终端自己的底色"不是一个能拿来插值的数，所以透明版算派生角色时用的是它原版的
    `base_bg`（见 `Palette.base_bg` 和 `Theme.__post_init__`）。

    **不用 HSL/HSV**：那会在灰阶附近产生意料之外的色相（两个中性色之间插值应该
    还是中性色，而 HSL 会绕着色环跑一圈）。RGB 线性插值在"往底色压暗"和"往正文
    提亮"这两个用法上就是想要的结果，而且它可预测、可单测。
    """
    a, b = _rgb(base), _rgb(toward)
    return _hex(tuple(x + (y - x) * amount for x, y in zip(a, b)))


@dataclass(frozen=True)
class Theme:
    """一套**落到位**的主题：token + 派生角色 + 供 CSS 用的变量表。

    token 那十二个字段**直接从 `Palette` 复制出来**（而不是让调用方写
    `theme.palette.accent`）：渲染代码里 `palette.accent` 出现几百次，多一层
    `.palette` 除了啰嗦没有任何好处 —— 而 `Palette` 存在的理由是"原始色值"和
    "派生角色"分得开（前者只有一处来源，后者全是算出来的）。
    """

    palette: Palette

    # token（来自色卡 / F7 卡片，原样）
    bg: str = field(init=False)
    chrome: str = field(init=False)
    surface: str = field(init=False)
    line: str = field(init=False)
    ink: str = field(init=False)
    ink2: str = field(init=False)
    ink3: str = field(init=False)
    accent: str = field(init=False)
    warn: str = field(init=False)
    danger: str = field(init=False)
    ok: str = field(init=False)
    dark: bool = field(init=False)

    # 派生角色（规则见模块 docstring）。**都做成字段**，这样 `variables()` 只有一处
    # 拼装，而且测试可以逐个断言"它确实在两色之间"。
    rail: str = field(init=False)
    elevated: str = field(init=False)
    sunk: str = field(init=False)
    hairline: str = field(init=False)
    ink4: str = field(init=False)
    accent_soft: str = field(init=False)
    danger_soft: str = field(init=False)
    skill: str = field(init=False)
    # 左栏每块左边那条色条的底色。**它是 `line` 的弱化版**，不是 `line` 本身：
    # 每块各来一条满血的主题描边色会跟正文抢眼睛，而"锚点"该是安静的那一层。
    # 弱化保留色相（这一套配色的性格还在），只是不再喊。
    rail_bar: str = field(init=False)

    def __post_init__(self) -> None:
        p = self.palette
        for name in ("bg", "chrome", "surface", "line", "ink", "ink2", "ink3",
                     "accent", "warn", "danger", "ok", "dark"):
            object.__setattr__(self, name, getattr(p, name))
        # **插值的基准色**：透明版是 `ansi_default`（不是色值），派生角色全按它原版的
        # `base_bg` 算 —— 于是"透明版"和"原版"的差别只剩最底下那一大片。
        base = p.base_bg or p.bg
        # 深色主题往黑走、亮色主题往白走 —— 方向只有一个来源（`dark`）。
        down = "#000000" if p.dark else "#FFFFFF"
        object.__setattr__(self, "rail", blend(base, p.ink, 0.045))
        object.__setattr__(self, "elevated", blend(base, p.ink, 0.10))
        object.__setattr__(self, "sunk", blend(base, down, 0.35))
        object.__setattr__(self, "hairline", blend(base, p.ink, 0.14))
        object.__setattr__(self, "ink4", blend(base, p.ink, 0.30))
        object.__setattr__(self, "accent_soft", blend(base, p.accent, 0.42))
        object.__setattr__(self, "danger_soft", blend(base, p.danger, 0.42))
        object.__setattr__(self, "skill", blend(p.line, p.ink, 0.20))
        object.__setattr__(self, "rail_bar", blend(p.line, base, 0.30))

    @property
    def transparent(self) -> bool:
        """这套是不是透明版（`bg` 是终端自己的底色）。"""
        return self.palette.transparent

    @property
    def clear_roles(self) -> tuple[str, ...]:
        """除 `bg` 之外**也交给终端**的那几格（转发给 `Palette.clear_roles`）。

        和 `transparent` 一样做成转发属性：`Theme` 那十几个 token 都是复制出来的字段，
        别的代码于是不必为了问一句"还透了哪几格"去 `.palette` 那一层翻。
        """
        return self.palette.clear_roles

    # -- 给 Textual 的那一半 ----------------------------------------------------

    @property
    def key(self) -> str:
        return self.palette.key

    @property
    def name(self) -> str:
        return self.palette.name

    def name_in(self, lang: str) -> str:
        """这套配色在某种语言下的名字（转发给 `Palette.name_in`）。"""
        return self.palette.name_in(lang)

    def source_in(self, lang: str) -> str:
        """出处那一小格（转发给 `Palette.source_in`）。"""
        return self.palette.source_in(lang)

    def variables(self) -> dict[str, str]:
        """CSS 变量表（`$td-bg` 这种）。

        **全部带 `td-` 前缀**：Textual 自己也有 `$surface` / `$panel` / `$accent`
        这些名字，而我们这九个角色和它的语义**不是一回事**（比如我们的 `chrome`
        既当顶杠又当状态栏，它的 `$panel` 只指后者）。同名会让人以为它们等价。
        """
        p = self.palette
        return {
            "td-bg": p.bg,
            "td-chrome": p.chrome,
            "td-surface": p.surface,
            "td-line": p.line,
            "td-ink": p.ink,
            "td-ink2": p.ink2,
            "td-ink3": p.ink3,
            "td-accent": p.accent,
            "td-warn": p.warn,
            "td-danger": p.danger,
            "td-ok": p.ok,
            "td-rail": self.rail,
            "td-elevated": self.elevated,
            "td-sunk": self.sunk,
            "td-hairline": self.hairline,
            "td-ink4": self.ink4,
            "td-accent-soft": self.accent_soft,
            "td-danger-soft": self.danger_soft,
            "td-skill": self.skill,
            "td-rail-bar": self.rail_bar,
        }


# --- 13 套 ---------------------------------------------------------------------
#
# 五套色卡（P3 / P5 / P6 / P7 / P9）：色值来自 `配色Token-你的九套-9色.json`，
#   **一字不改**（P1 / P2 / P4 / P8 那四套是用户裁掉的，key 里留下的号不重排）。
# 五套候选（A–E）：色值来自设计稿 F7 的卡片（`surface/line/ink2` 三格是取样的）。
# 三套透明版（P3-T / A-T / A-T2）：**由原版（或浅一档的透明版）推出来**，
#   见下面 `_transparent`。

# `bg` 在透明版上的值。**这不是一个颜色，是"别涂底"**：Textual 的 `Color` 用
# `ansi=-1` 表示它，最终写出去的是 SGR 49（终端默认背景），而 `Screen.render()`
# 会把它让给 App —— 于是终端自己的底板透上来。
ANSI_DEFAULT = "ansi_default"

_PALETTES: tuple[Palette, ...] = (
    Palette("P3", "粉紫", "色卡③ · 亮色主题", False,
            bg="#EAF2FE", chrome="#E1E8F4", surface="#D8DEEA", line="#C5ABD3",
            ink="#363044", ink2="#7E7E8E", ink3="#A9ACBB",
            accent="#8E5E6E", warn="#9582A1", danger="#C0362E", ok="#57956D",
            name_en="Pink Violet", source_en="Card ③ · light theme"),
    Palette("P5", "夜紫柔彩", "色卡⑤", True,
            bg="#1E0A26", chrome="#291631", surface="#34223C", line="#F2B28D",
            ink="#F2CEE6", ink2="#9D8099", ink3="#6A516B",
            accent="#9CD9D3", warn="#F2DB66", danger="#D9453C", ok="#6FBF8B",
            name_en="Night Violet Pastel", source_en="Card ⑤"),
    Palette("P6", "莓红奶白", "色卡⑥", True,
            bg="#151320", chrome="#211F2B", surface="#2D2B36", line="#BF5C78",
            ink="#FCF3E7", ink2="#A09997", ink3="#686468",
            accent="#EB6962", warn="#E0A44E", danger="#C93A44", ok="#6FBF8B",
            name_en="Berry & Cream", source_en="Card ⑥"),
    # P7 曾经是默认。`accent` 必须用这个提亮过的值（原色 #463DE8 在 #161616 上 2.3:1）。
    Palette("P7", "靛夜", "色卡⑦", True,
            bg="#161616", chrome="#222222", surface="#2E2E2E", line="#74B5B3",
            ink="#FFFFFF", ink2="#A2A2A2", ink3="#6A6A6A",
            accent="#7670EF", warn="#E8C05A", danger="#D76A7B", ok="#74B5B3",
            name_en="Indigo Night", source_en="Card ⑦"),
    Palette("P9", "森绿石", "色卡⑨", True,
            bg="#2F443A", chrome="#394D44", surface="#43564E", line="#507550",
            ink="#D5D1C7", ink2="#93998F", ink3="#6B776D",
            accent="#A2AF8E", warn="#C9A24E", danger="#E16E66", ok="#769376",
            name_en="Forest Stone", source_en="Card ⑨"),
    # A 是现在的默认（展示序号 10）。
    Palette("A", "石墨琥珀", "F7-A · 默认", True,
            bg="#131210", chrome="#1A1815", surface="#221F18", line="#342B24",
            ink="#EDE6DA", ink2="#AFA395", ink3="#847968",
            accent="#E0A83E", warn="#E0703A", danger="#C64A3E", ok="#8AA63F",
            name_en="Graphite Amber", source_en="F7-A · default"),
    Palette("B", "极地冷", "F7-B · Nord 血统", True,
            bg="#1C2028", chrome="#232830", surface="#29303A", line="#3A404C",
            ink="#E5E9F0", ink2="#AAB3C4", ink3="#7B8598",
            accent="#88C0D0", warn="#EBCB8B", danger="#BF616A", ok="#A3BE8C",
            name_en="Polar Cool", source_en="F7-B · Nord lineage"),
    Palette("C", "墨绿仪器", "F7-C", True,
            bg="#0F1716", chrome="#16201E", surface="#1F2B29", line="#2C3C39",
            ink="#E3EDEA", ink2="#A2B7B2", ink3="#7C908B",
            accent="#45D9C8", warn="#E6B450", danger="#E05252", ok="#86C64B",
            name_en="Instrument Green", source_en="F7-C"),
    Palette("D", "紫夜", "F7-D · Tokyo Night 血统", True,
            bg="#14131C", chrome="#1B1A25", surface="#23212F", line="#322F42",
            ink="#DCDCE8", ink2="#A5A3B9", ink3="#7B7893",
            accent="#BB9AF7", warn="#E0AF68", danger="#F7768E", ok="#9ECE6A",
            name_en="Purple Night", source_en="F7-D · Tokyo Night lineage"),
    Palette("E", "纯碳", "F7-E · accent 就是白", True,
            bg="#0A0A0A", chrome="#141414", surface="#1A1A1A", line="#292929",
            ink="#FAFAFA", ink2="#B8B8B8", ink3="#8A8A8A",
            accent="#FFFFFF", warn="#FACC15", danger="#EF4444", ok="#4ADE80",
            name_en="Pure Carbon", source_en="F7-E · the accent is white"),
)


def _transparent(base: Palette, *, key: str = "", name: str = "", source: str = "",
                 name_en: str = "", source_en: str = "",
                 clear_roles: tuple[str, ...] = ()) -> Palette:
    """**原版 → 透明版**：把 `bg` 换成终端自己的底色，别的九格一个字节都不动。

    这就是"透明版不是第十套配色，而是同一套配色少涂一层"这句话的实现：色值来自
    `base` 的同一份数据（不是抄一遍），所以原版改了、透明版不可能漂。

    key 是 `<原 key>-T`（`A-T` / `P3-T`）：既看得出它是谁的透明版，也能直接当 key 打。
    名字是 `<原名> · 透明` —— `resolve` 的"名字里的一段"那一路于是能认 `透明` 两个字
    （几套都含它，靠"最精确的那条赢"落到具体某一套，见 `resolve`）。

    ## 四个名字参数与 `clear_roles` 是给"深一层"那一套留的口子

    深浅两档的规则是同一条（"色值从 `base` 来，只是某几格不涂"），差别只有两处：
    透哪几格（`clear_roles`）、叫什么（那四个名字）。所以它们是**带默认值的可选参数**，
    而不是再写一个 `_deep_transparent` —— 后者会把"色值从哪来"这条规矩抄成两份。
    不传的时候就是 `-T` 那两套的字面形态：`<原 key>-T` / `<原名> · 透明`。
    """
    tokens = {
        "chrome": base.chrome, "surface": base.surface, "line": base.line,
        "ink": base.ink, "ink2": base.ink2, "ink3": base.ink3,
        "accent": base.accent, "warn": base.warn, "danger": base.danger, "ok": base.ok,
    }
    # 多透的那几格：**同一个 `ansi_default`，不是另找一个近似的色值**。
    for role in clear_roles:
        tokens[role] = ANSI_DEFAULT
    return Palette(
        key or f"{base.key}-T",
        name or f"{base.name} · 透明",
        source or f"{base.source} · 透明版",
        base.dark,
        bg=ANSI_DEFAULT,
        transparent=True,
        # `base.base_bg or base.bg`：**`base` 自己也可能已经是透明版**（`A-T2` 是从
        # `A-T` 再派生一层），那时候它的 `bg` 是 `ansi_default`、拿不来插值 —— 派生
        # 角色的基准色是"原版那套的 `bg`"，这一格于是要**一路带下去**，不能在这里
        # 被覆盖成 `ansi_default`（实测：写 `base.bg` 的话，`A-T2` 一构造就抛
        # `ValueError: invalid literal for int() with base 16: 'an'`）。
        base_bg=base.base_bg or base.bg,
        name_en=name_en or f"{base.name_en} · Clear",
        source_en=source_en or f"{base.source_en} · clear variant",
        clear_roles=clear_roles,
        **tokens,
    )


# 透明版**追加在末尾**（而不是插在原版旁边）：`ORDER` 就是 `--tui`/`/theme` 列表的
# 展示顺序，而"后面多出来的这几套是透明版"比"每两套里夹一个"好找。序号跟着走：11–13。
# **不放进上面那张表**，因为那张表的每一项都是"从色卡/卡片抄来的原始色值"，而这三套
# 是从表里的某一项算出来的 —— 分开写，"抄来的"和"推出来的"就一眼分得开。
# 索引从 0 起：第 1 项是 `P3`，第 6 项是 `A`。
#
# `A-T2` 是**从 `A-T` 再派生一层**（不是从 `A` 直接推第二遍）：它和 `A-T` 只差
# 交出去的那两格，而那句话只有在"以 `A-T` 为底"的写法里才看得出来。
_PALETTES += (
    _transparent(_PALETTES[0]),
    _transparent(_PALETTES[5]),
    # 深透明：**横栏和欢迎屏的三个框也不涂**。用户点名的那一套 —— 在 `A-T` 上最显眼的
    # 是顶栏 / 会话头 / 状态栏 / 输入框那四条色带，以及开场那三个方块（开始 / 最近 / 提示）。
    _transparent(
        _transparent(_PALETTES[5]),
        key="A-T2",
        name="石墨琥珀 · 深透明",
        source="F7-A · 默认 · 透明版②",
        name_en="Graphite Amber · Deep Clear",
        source_en="F7-A · default · clear variant ②",
        # 两格，两个地方：`chrome` 是四条横栏（顶栏 / 会话头 / 状态栏 / 输入框），
        # `surface` 是开场那三个框（开始 / 最近 / 提示）。**框线都不在里面**
        # —— 那三个框的圆角边框是 `accent`，横栏的线是 `line` / `hairline`。
        clear_roles=("chrome", "surface"),
    ),
)

THEMES: dict[str, Theme] = {p.key: Theme(p) for p in _PALETTES}

# 展示顺序就是上面那张表的顺序：五套色卡（1–5）、五套候选（6–10）、
# 三套透明版（11–13：先两套浅的，最后那套深的）。
ORDER: tuple[str, ...] = tuple(p.key for p in _PALETTES)


def get(key: str) -> Theme:
    """按 key 取一套；不认识就**回默认那套**（而不是抛）。

    界面上"换配色"是装饰性操作，一个打错的 key 让整个界面起不来是最坏的取舍。
    真正打错字的那种情况由 `/theme` 的提示负责说清楚（`resolve` 返回 None 时）。
    """
    return THEMES.get(key, THEMES[DEFAULT_THEME])


def resolve(query: str) -> str | None:
    """把用户输入的一小段字认成一套主题的 key。**认不出返回 None。**

    认的方式（按优先级，这是 `/theme` 的全部交互设计）：

      1. key 本身（`p7` / `a`，大小写不敏感；透明版是 `a-t` / `p3-t` / `a-t2`，
         `at` / `p3t` / `at2` 也认 —— 中间的连字符是给人看的，不该变成必须打的字）；
      2. 序号（`7` → `P7`，`12` → `A-T`，`13` → `A-T2`）—— 13 套的展示顺序；
      3. 名字里的一段（`靛` → `P7`，`墨绿` → `C`，`深` → `A-T2`，`透明` → 见下）。

    第 2 条用**展示序号**而不是 key 里的数字：`A`–`E` 那几套没有数字，而"第 13 套"
    在 `/theme` 的列表里是有意义的（列表就是按这个序打的）。

    **第 3 条两套名字都认**（中文名和英文名）：界面语言和用户的肌肉记忆是两件事 ——
    切到英文之后 `/theme 靛夜` 突然失灵，是最容易被当成 bug 的那种回归。英文名那一路
    大小写不敏感（`indigo` / `Indigo` 都行）。

    ## 多个都命中时：**最精确的那一条赢**

    有了透明版之后这条规则才真正被用上：`石墨琥珀 · 透明` 这个名字**含** `石墨琥珀`，
    所以打 `琥珀` 两个字三套都命中 —— 从前那种"命中多于一条就返回 None"会把它变成
    "没有这套配色"，而它明明在列表里。改成两步：**以它开头的赢**，其次**短的那条赢**。
    于是 `/theme 琥珀` 是原版 `A`（`石墨琥珀` 以它开头，另外两套只是含它），
    要透明版就打 `/theme 透明`（三套透明版都含它，取最短的 `P3-T`）、
    `/theme 深` 是 `A-T2`（只有它含这两个字），或者直接 `/theme a-t` / `/theme a-t2`。
    **不猜全名**仍然是规矩 —— `/theme 琥珀色` 照样一个都不命中。

    名字一样长的（`clear` 命中三套透明版的英文名）**再按展示顺序取靠前的那一条**：
    这条规则只为了让结果稳定，不是为了"更对" —— 几套透明版的区分只能靠 key。
    """
    text = query.strip()
    if not text:
        return None
    upper = text.upper()
    if upper in THEMES:
        return upper
    # 去掉连字符再来一次：`at` / `p3t` / `a_t` 都落到 `A-T` / `P3-T` 上。
    squashed = upper.replace("-", "").replace("_", "")
    for key in ORDER:
        if key.replace("-", "").replace("_", "") == squashed:
            return key
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(ORDER):
            return ORDER[index]
    needle = text.lower()
    hits = [(key, name) for key in ORDER
            for name in (THEMES[key].palette.name,
                         THEMES[key].palette.name_en)
            if name and needle in name.lower()]
    if hits:
        # 排序键（越靠前越"精确"）：
        #   1. 名字**以它开头**的赢 —— `琥珀` 开头的是 `石墨琥珀`，`石墨琥珀 · 透明`
        #      不是（它是"含"）；
        #   2. 短的那条赢 —— `Pink Violet` 比 `Pink Violet · Clear` 精确；
        #   3. 还平就按展示顺序 —— 唯一的作用是让"命中一样长"时结果稳定。
        return min(hits, key=lambda hit: (not hit[1].lower().startswith(needle),
                                          len(hit[1]),
                                          ORDER.index(hit[0])))[0]
    return None


def listing(lang: str | None = None) -> str:
    """`/help` 和 `/theme` 用的那句清单：`1 P3 粉紫 · 2 P5 夜紫柔彩 · …`。

    名字按当前语言出（`lang=None` 就是进程定的那套）；**序号和 key 不跟着变** ——
    它们是"打哪一串字能选中它"，跟语言无关。
    """
    lang = lang or i18n.current()
    return " · ".join(
        f"{index} {key} {THEMES[key].palette.name_in(lang)}"
        for index, key in enumerate(ORDER, 1)
    )
