"""TUI 的配色层：**14 套主题，纯数据。**

## 这个文件为什么存在（以及为什么它不认识 Textual）

设计稿把配色当成一件可以整体替换的事：F6 的「色彩 Token」定义**角色**
（底 / 栏 / 面板 / 正文三档 / 交互色 / 两个风险色），F8/F9 的九套色卡和 F7 的五套
候选各自给出一组**色值**。角色和色值分开之后，"换配色"就只是换一个表 ——
而不是去 20 处 CSS 里找那个 `#2997FF`。

所以这里只有 dataclass 和一张表，**一个 textual 的 import 都没有**：它要能被单测
（对比度、角色齐全、`/theme` 的匹配规则），而 `tests/test_imports.py` 里那条
"textual 只准出现在 app.py / widgets.py"也就不用为它开口子。

## 14 套是怎么来的

  * `P1`–`P9`：**土豆泥的九套色卡**，按「5 色 → 界面九色」的规则扩展过
    （源文件 `配色Token-你的九套-9色.json`，每个值都带 lineage）。这九套的九个
    色值**原样照搬**，一个都不改 —— 它们是对比度校过的。
  * `A`–`E`：设计稿 F7 的**五套候选方案**（石墨琥珀 / 极地冷 / 墨绿仪器 / 紫夜 /
    纯碳）。它们的 `bg/chrome/ink/accent/warn/danger/ok/ink3` 来自 F7 卡片上印的
    色值，`surface/line/ink2` 三格 F7 没印字，是**从卡片上那三条色块取样的**
    （取样值记在 `source` 里，便于日后核对）。

## 派生角色：为什么不是"每套再手写八个色值"

九套 token 只给了九个角色，而界面要用十几个（左栏底、弹层底、思考块底、细描边、
占位色……）。**手写会引入 14×8 = 112 个没人校对过的色值**，而且每套的观感会开始
漂。所以除 token 之外的角色一律**按固定规则从 token 推导**（见下面 `_derive`），
规则只有三条，而且对 14 套一视同仁：

  1. **抬高明度**（rail / elevated）：往正文色方向插值 —— 深色主题是变亮，
     亮色主题（只有 `P3 粉紫` 一套）是变暗，方向由 `dark` 决定；
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

# 默认主题。**用户拍的板**：`A 石墨琥珀`（展示序号 10）—— 它取代了此前定的 ⑦ 靛夜。
# 靛夜那一套本身没动（它的 accent 仍然是提亮过的 `#7670EF`，原色 `#463DE8` 在
# `#161616` 上只有 2.3:1，肉眼几乎看不见），只是不再是启动默认。
DEFAULT_THEME = "A"


@dataclass(frozen=True)
class Palette:
    """一套主题的九个 token + 两个补充角色。**色值全部原样，不做二次调整。**"""

    key: str
    name: str
    source: str
    dark: bool
    bg: str          # 窗口底 / 对话区
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


# --- 颜色工具（纯函数，便于单测） ---------------------------------------------

def _rgb(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def blend(base: str, toward: str, amount: float) -> str:
    """在 `base` 和 `toward` 之间插值。`amount=0` 给 `base`，`=1` 给 `toward`。

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
        # 深色主题往黑走、亮色主题往白走 —— 方向只有一个来源（`dark`）。
        down = "#000000" if p.dark else "#FFFFFF"
        object.__setattr__(self, "rail", blend(p.bg, p.ink, 0.045))
        object.__setattr__(self, "elevated", blend(p.bg, p.ink, 0.10))
        object.__setattr__(self, "sunk", blend(p.bg, down, 0.35))
        object.__setattr__(self, "hairline", blend(p.bg, p.ink, 0.14))
        object.__setattr__(self, "ink4", blend(p.bg, p.ink, 0.30))
        object.__setattr__(self, "accent_soft", blend(p.bg, p.accent, 0.42))
        object.__setattr__(self, "danger_soft", blend(p.bg, p.danger, 0.42))
        object.__setattr__(self, "skill", blend(p.line, p.ink, 0.20))
        object.__setattr__(self, "rail_bar", blend(p.line, p.bg, 0.30))

    # -- 给 Textual 的那一半 ----------------------------------------------------

    @property
    def key(self) -> str:
        return self.palette.key

    @property
    def name(self) -> str:
        return self.palette.name

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


# --- 14 套 ---------------------------------------------------------------------
#
# 九套色卡（P1–P9）：色值来自 `配色Token-你的九套-9色.json`，**一字不改**。
# 五套候选（A–E）：色值来自设计稿 F7 的卡片（`surface/line/ink2` 三格是取样的）。

_PALETTES: tuple[Palette, ...] = (
    Palette("P1", "暖橄榄", "色卡①", True,
            bg="#242F1A", chrome="#2F3925", surface="#3A4330", line="#556136",
            ink="#FEF9DE", ink2="#A7A890", ink3="#727861",
            accent="#D6975B", warn="#C06E31", danger="#D9453C", ok="#6FBF8B"),
    Palette("P2", "海蓝橙", "色卡②", True,
            bg="#092A3D", chrome="#153547", surface="#214051", line="#2793B1",
            ink="#85C2E0", ink2="#53859F", ink3="#366178",
            accent="#FDAF31", warn="#F77C26", danger="#D9453C", ok="#6FBF8B"),
    Palette("P3", "粉紫", "色卡③ · 亮色主题", False,
            bg="#EAF2FE", chrome="#E1E8F4", surface="#D8DEEA", line="#C5ABD3",
            ink="#363044", ink2="#7E7E8E", ink3="#A9ACBB",
            accent="#8E5E6E", warn="#9582A1", danger="#C0362E", ok="#57956D"),
    Palette("P4", "暖光", "色卡④", True,
            bg="#053F5C", chrome="#124964", surface="#1F536C", line="#428EBD",
            ink="#9FE7F5", ink2="#61A4B8", ink3="#3C7B93",
            accent="#F28B21", warn="#F7AD19", danger="#DF645C", ok="#6FBF8B"),
    Palette("P5", "夜紫柔彩", "色卡⑤", True,
            bg="#1E0A26", chrome="#291631", surface="#34223C", line="#F2B28D",
            ink="#F2CEE6", ink2="#9D8099", ink3="#6A516B",
            accent="#9CD9D3", warn="#F2DB66", danger="#D9453C", ok="#6FBF8B"),
    Palette("P6", "莓红奶白", "色卡⑥", True,
            bg="#151320", chrome="#211F2B", surface="#2D2B36", line="#BF5C78",
            ink="#FCF3E7", ink2="#A09997", ink3="#686468",
            accent="#EB6962", warn="#E0A44E", danger="#C93A44", ok="#6FBF8B"),
    # P7 曾经是默认。`accent` 必须用这个提亮过的值（原色 #463DE8 在 #161616 上 2.3:1）。
    Palette("P7", "靛夜", "色卡⑦", True,
            bg="#161616", chrome="#222222", surface="#2E2E2E", line="#74B5B3",
            ink="#FFFFFF", ink2="#A2A2A2", ink3="#6A6A6A",
            accent="#7670EF", warn="#E8C05A", danger="#D76A7B", ok="#74B5B3"),
    Palette("P8", "藏青陶土", "色卡⑧", True,
            bg="#273F75", chrome="#32497C", surface="#3D5383", line="#797784",
            ink="#EDE6D8", ink2="#A9B2C8", ink3="#7584A7",
            accent="#BEAB73", warn="#C87A60", danger="#D2705E", ok="#6FBF8B"),
    Palette("P9", "森绿石", "色卡⑨", True,
            bg="#2F443A", chrome="#394D44", surface="#43564E", line="#507550",
            ink="#D5D1C7", ink2="#93998F", ink3="#6B776D",
            accent="#A2AF8E", warn="#C9A24E", danger="#E16E66", ok="#769376"),
    # A 是现在的默认（展示序号 10）。
    Palette("A", "石墨琥珀", "F7-A · 默认", True,
            bg="#131210", chrome="#1A1815", surface="#221F18", line="#342B24",
            ink="#EDE6DA", ink2="#AFA395", ink3="#847968",
            accent="#E0A83E", warn="#E0703A", danger="#C64A3E", ok="#8AA63F"),
    Palette("B", "极地冷", "F7-B · Nord 血统", True,
            bg="#1C2028", chrome="#232830", surface="#29303A", line="#3A404C",
            ink="#E5E9F0", ink2="#AAB3C4", ink3="#7B8598",
            accent="#88C0D0", warn="#EBCB8B", danger="#BF616A", ok="#A3BE8C"),
    Palette("C", "墨绿仪器", "F7-C", True,
            bg="#0F1716", chrome="#16201E", surface="#1F2B29", line="#2C3C39",
            ink="#E3EDEA", ink2="#A2B7B2", ink3="#7C908B",
            accent="#45D9C8", warn="#E6B450", danger="#E05252", ok="#86C64B"),
    Palette("D", "紫夜", "F7-D · Tokyo Night 血统", True,
            bg="#14131C", chrome="#1B1A25", surface="#23212F", line="#322F42",
            ink="#DCDCE8", ink2="#A5A3B9", ink3="#7B7893",
            accent="#BB9AF7", warn="#E0AF68", danger="#F7768E", ok="#9ECE6A"),
    Palette("E", "纯碳", "F7-E · accent 就是白", True,
            bg="#0A0A0A", chrome="#141414", surface="#1A1A1A", line="#292929",
            ink="#FAFAFA", ink2="#B8B8B8", ink3="#8A8A8A",
            accent="#FFFFFF", warn="#FACC15", danger="#EF4444", ok="#4ADE80"),
)

THEMES: dict[str, Theme] = {p.key: Theme(p) for p in _PALETTES}

# 展示顺序就是上面这张表的顺序：先九套色卡（1–9），再五套候选（A–E）。
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

      1. key 本身（`p7` / `a`，大小写不敏感）；
      2. 序号（`7` → `P7`，`13` → `D`）—— 14 套的展示顺序；
      3. 名字里的一段（`靛` → `P7`，`墨绿` → `C`）。

    第 2 条用**展示序号**而不是 key 里的数字：`A`–`E` 五套没有数字，而"第 13 套"
    在 `/theme` 的列表里是有意义的（列表就是按这个序打的）。
    """
    text = query.strip()
    if not text:
        return None
    upper = text.upper()
    if upper in THEMES:
        return upper
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(ORDER):
            return ORDER[index]
    hits = [key for key in ORDER if text in THEMES[key].name]
    if len(hits) == 1:
        return hits[0]
    return None


def listing() -> str:
    """`/help` 和 `/theme` 用的那句清单：`1 P1 暖橄榄 · 2 P2 海蓝橙 · …`。"""
    return " · ".join(
        f"{index} {key} {THEMES[key].name}"
        for index, key in enumerate(ORDER, 1)
    )
