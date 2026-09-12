"""技能的发现与解析：把技能目录里的文件变成数据。

**技能的"领域"住在 skills/ 包，不夹在 tools/ 里。** 切分的依据只有一条：这一层是
纯数据 —— 扫描目录、读 frontmatter、校验、组织成 `Skill` / `SkillCatalog`，它
**不 import 任何内部模块**（连 `tools.tool` 都不）。所以它能独立成包而不会让依赖
方向成环：`tools → skills`，而 skills 谁也不依赖。

一旦这里出现 `Tool` / `ToolRegistry` / `ToolResult`，就会变成 `skills → tools`，
而 `tools/builtin.py` 又要 import 本包来注册 load_skill —— 环一出现，README 里那句
"依赖方向是单向的，无环"就成了假话。工具的接线（参数模型、handler、注册）全部留在
`tools/skills.py`，和 `tools/webfetch.py`（有 WebFetch、注册在 builtin.py）同构。

## 技能住在哪

多个目录，名字一样就是同一个技能（对齐 agentskills.io 的 `SKILL.md` 约定，所以别人
现成的技能包直接放进来就能用）：

    ~/.skills/<name>/SKILL.md                  用户级（通用兜底）
    ~/.agents/skills/<name>/SKILL.md           用户级（通用兜底）
    ~/.tudouni/skills/<name>/SKILL.md          用户级（个人）
    <工作区>/.skills/<name>/SKILL.md           项目级（通用兜底）
    <工作区>/.agents/skills/<name>/SKILL.md    项目级（通用兜底）
    <工作区>/.tudouni/skills/<name>/SKILL.md   项目级

**优先级沿用 Claude Code 的层级：个人 > 项目。** 直觉上反着，但它和 git config 的
"用户配置覆盖仓库配置"是同一条道理：机器是人的，仓库是别人的。同名的低优先级那一份
**不消失、也不静默**——它进 `shadowed`，由入口打到 stderr。静默遮蔽是这里最坏的失败
形态：人改了项目里那份技能、发现"没生效"，而真正生效的那份在另一个盘上。

用户级目录在**工作区外面**，这看起来和"文件工具只碰工作区"冲突，其实不冲突：技能目录
由 `SkillLoader` 自己算（**不接受模型给的路径**），而 `write_file` 的工作区边界本来就够
不着 `~`，所以"技能只有人能改"在用户级目录上是操作系统帮着保证的。

技能目录里的其它文件是 L3 附件（规范推荐的 `references/` / `scripts/` / `assets/` 都在
这一层）：技能文件本身要求人在路径上写清楚，模型自己用 `read_file` / `shell` 去碰。

## 格式与解析

    ---
    name: pdf-extract
    description: 从 PDF 里提取文本并清掉页眉页脚。要读 PDF 时用它。
    allowed-tools: read_file shell        ← 可选，**只是提示**，不做拦截
    license: Apache-2.0                   ← 可选，规范字段，认下但不用
    ---
    ## 步骤
    1. ...

**frontmatter 用手写解析，不引 PyYAML。** 这个项目的立身之本是"零依赖、每个决定
读得懂"，而技能头只有几个键。手写解析的代价是它**只认一部分 YAML**，所以取向必须
和 `security/commands.py` 那条"看不懂就去问人"一致：**看不懂一律进 problems，绝不猜**。
猜错一个引号或缩进，结果是"技能静默地少了一段说明"，而写技能的人以为它生效了。

**坏文件不是错误，是数据。** 解析失败不抛异常、不拦启动，而是进 `SkillCatalog.problems`
由入口打到 stderr —— 和 `PermissionConfig.unknown_tools`（名单里写错工具名）同一个
处置：不该拦启动，但绝不能不说。理由也一样：静默不生效是最坏的失败形态。
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# 技能目录的**目录名**。`.tudouni` 是**本工具的私有位置**（和权限策略 .tudouni.json
# 同一个家）；`.agents` / `.skills` 是**通用兜底**（生态里部分工具用它们）。
#
# 注意两者形状不同：`.agents` 下面还隔一层 `skills/`，而 `.skills` **本身就是技能目录**
# （再拼一层会变成 `.skills/skills/`，那是没人约定过的路径）。
TUDOUNI_DIR_NAME = ".tudouni"
AGENTS_DIR_NAME = ".agents"
GENERIC_DIR_NAME = ".skills"
SKILLS_DIR_NAME = "skills"

SKILL_FILE_NAME = "SKILL.md"

# 会话 metadata 里存"哪些技能已加载"的键。**读写两侧共用这一个常量**（写入在
# tools/skills.py 的 SkillBoard，读出在 skills/render.py）—— 两边各写一份字面量，
# 漂开一个字符就是"技能凭空消失"，而且不会有任何报错（和 todo.TODOS_KEY 同一条）。
SKILLS_KEY = "skills"

# 一个技能正文的字节上限。它和 todo 列表面对的是同一个成本结构：正文会拼进
# **每一次请求的载荷尾部**，此后每一轮都要重发一次。所以超限时**拒绝加载并说清楚**，
# 而不是截断 —— 截断出来的是一份"看起来完整、其实少了后半段步骤"的说明，比读不到更坏
# （tools/todo.py 里"坏数据一条不对就整份丢掉"是这个取向的另一半）。
MAX_SKILL_BYTES = 64_000

# 同时生效的技能数上限。超了拒绝新的，不挤掉旧的：悄悄卸载一个已经生效的技能
# 等于伪造模型的主张（它下一轮会按自己"记得"的技能做，而那份已经不在载荷里了）。
MAX_ACTIVE_SKILLS = 3

# 技能正文在那个临时消息里占的最大比例。载荷尾部是整段对话里单价最贵的位置，
# 所以这个上限要能回答"技能正文最多占掉多少"。
MAX_NOTE_CHARS = 40_000

_NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

# 认识的**全部** frontmatter 键。多一个不认识的键就报错 —— 理由和 ToolArgs 的
# extra="forbid"、PermissionConfig 的 _KNOWN_PERMISSION_KEYS 完全一样：
# 写错一个键名而它静默不生效，是最坏的失败形态。
#
# 后三个是 agentskills.io 规范里的**可选字段**。认下它们不是为了用，而是为了**不吃掉
# 合规的技能包**：别人从生态里拿一份技能进来，光是它写了 license / compatibility，
# 就不该被我们判成"不认识的键"然后整个跳过。
_KNOWN_FRONTMATTER_KEYS = (
    "name",
    "description",
    "allowed-tools",
    "license",
    "compatibility",
    "metadata",
)

# 认下、但本程序用不到的键（不校验、不存、不影响任何行为）。
_IGNORED_KEYS = ("license", "compatibility")

# 值里出现这些字符就拒绝解析。它们只在 YAML 的嵌套结构里有意义，而这个解析器
# 不打算支持嵌套 —— 猜错的后果是某一段说明被静默吃掉。
# （`metadata` 是唯一的例外：规范里它就是个嵌套映射，见 parse_frontmatter。）
_UNSUPPORTED_IN_VALUE = "#{}"


@dataclass(frozen=True, slots=True)
class Skill:
    """一个技能的全部事实。

    `locations` 是**所有**装着这个技能的目录，第一个是生效的那个（优先级最高的）。
    顺序有意义，所以它是个 tuple 而不是 set：`--skills` 和冲突报告都靠这个顺序说清
    "哪一份在生效、哪一份被遮住了"。

    `digest` 只覆盖正文的字节：用它回答"这个技能文件在上次加载之后被人改过吗"。
    存进 metadata 的是**名字 + 摘要**而不是正文 —— 正文每轮从磁盘重渲染，所以
    "会话里存着旧正文、磁盘上是新正文"这两份事实不会同时存在。
    """

    name: str
    description: str
    path: Path
    body: str
    allowed_tools: tuple[str, ...] = ()
    digest: str = ""
    locations: tuple[Path, ...] = ()

    @property
    def body_chars(self) -> int:
        return len(self.body)

    @property
    def shadowed(self) -> tuple[Path, ...]:
        """被优先级遮住的那几份（同名的低优先级技能）。"""
        return self.locations[1:]


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """一次扫描的结果：能用的技能 + 读懂的毛病 + 扫过哪些目录。

    problems 里的每一条都是**给写技能的人看的**，所以它必须带文件名和具体毛病 ——
    只说"有个技能加载失败"等于什么也没说。

    `roots` 是这次真的扫了的目录（按扫描顺序，低优先级在前）。它存在的理由和 problems
    一样：答得出"它是在哪儿找到这个技能的"——用户级目录在工作区外面，不列出来，
    人根本想不到去那儿找。
    """

    skills: tuple[Skill, ...] = ()
    problems: tuple[str, ...] = ()
    roots: tuple[Path, ...] = ()
    shadowed: tuple[str, ...] = ()

    def by_name(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None


class FrontmatterError(ValueError):
    """技能头读不懂。它的消息会原样进 problems（也就是会原样打给人看）。"""


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _is_quoted(value: str) -> bool:
    """整个值被一对引号包着。

    引号是**逃生口**，不是装饰：`#` `{` `}` 这些字符只在裸值里有歧义（在 YAML 里它们
    分别是注释和流式映射的开始），写进引号里就只是一个普通字符。所以引号值要跳过下面
    那条字符检查 —— 否则 `description: "支持 #{x} 这种写法"` 会被拒，而它恰恰是**按
    本解析器的规则**写出来的最明确的形式。
    """
    return (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in "\"'"
    )


def _split_tools(value: str) -> tuple[str, ...]:
    """解析 `allowed-tools`：空格分隔，认 `Tool(specifier)` 的限定写法。

    规范给的是 `Bash(git:*) Bash(jq:*) Read`（**空格**分隔，而且带限定符），所以这里
    不能按逗号切，也不能把整串当成一个工具名 —— 后者会让"技能声明只用了 Bash"这条
    信息变成一句谁也认不出的垃圾。

    限定符 `(git:*)` 只丢给提示用，**不在这里解释**：它是权限规则的语法（"允许 git
    开头的命令"），而那属于 security/ 那一层；本包连工具名都不认识，更不该去懂权限
    规则的语法。逗号也收，因为很多人按中文习惯写成 `a, b` —— 两种都认，不猜。
    """
    text = value.replace(",", " ").replace("[", " ").replace("]", " ")
    tools: list[str] = []
    for part in text.split():
        name = _strip_quotes(part.strip())
        # `Bash(git:*)` → `Bash`；括号不配对时原样保留（那是写错了，让它在提示里显眼）
        if "(" in name:
            name = name.split("(", 1)[0].strip()
        if name and name not in tools:
            tools.append(name)
    return tuple(tools)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """把 SKILL.md 拆成（frontmatter 键值, 正文）。

    **只支持单层 `key: value`**，唯一的例外是规范的 `metadata:` —— 它本来就是个嵌套
    映射（`metadata:\\n  author: me`），而其下的内容我们既不用也不校验，所以整块跳过。
    除此之外不认缩进、不认 `- ` 列表项、不认块标量（`|` / `>`）、不认注释。

    每一条不支持都必须**报错并说清该改成什么**，因为它们全都是"看起来完全正常、实际
    被静默吃掉一段"的形状：

      * 缩进被 `strip()` 抹平 —— 一个嵌套在别的键下面的 description 会被当成顶层键
        收下，而写的人以为自己写了个结构；
      * `- a` 这种列表项连冒号都没有，本该报错，但如果先 strip 再判断，它会变成
        "看不懂这一行"，而真正的原因（本解析器不支持列表块）就说不出来了。

    报错是写给写技能的人的，所以每条都带"该改成什么"。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError(
            "文件开头必须有 frontmatter，第一行是三个连字符（---）"
        )

    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise FrontmatterError("frontmatter 没有闭合：缺少第二行三个连字符（---）")

    entries: dict[str, str] = {}
    open_list: str | None = None
    in_metadata = False

    for raw in lines[1:closing]:
        line = raw.strip()
        if not line:
            continue

        if raw[:1] in (" ", "\t"):
            # metadata 之下的缩进行整块跳过：那是规范里唯一允许的嵌套结构，
            # 而它的内容我们不用（键值都是给别的工具看的元信息）。
            if in_metadata:
                continue
            if open_list is not None:
                # 行内列表写成多行：靠空格接起来（规范就是空格分隔）。
                entries[open_list] = f"{entries[open_list]} {line}".strip()
                if line.endswith("]"):
                    open_list = None
                continue
            raise FrontmatterError(
                f"这一行有缩进：{line!r}。本解析器只认顶格的 `键: 值`，"
                f"不接受嵌套结构或 `- ` 列表项（`metadata` 之下除外）"
            )

        if ":" not in line:
            raise FrontmatterError(
                f"看不懂这一行：{line!r}（只支持顶格的 `键: 值`）"
            )
        key, value = (part.strip() for part in line.split(":", 1))
        if not key:
            raise FrontmatterError(f"这一行缺少键名：{line!r}")
        if key not in _KNOWN_FRONTMATTER_KEYS:
            raise FrontmatterError(
                f"不认识的键 {key!r}；本程序认识的只有 "
                f"{'、'.join(_KNOWN_FRONTMATTER_KEYS)}"
            )

        in_metadata = key == "metadata" and not value
        if key not in entries:
            entries[key] = value
        elif key == "allowed-tools" and open_list is None:
            # allowed-tools 允许分多行写（规范里是一串，写长了自然会折行）。
            entries[key] = f"{entries[key]} {value}".strip()
        else:
            raise FrontmatterError(f"键 {key!r} 写了两遍 —— 哪一遍算数没有答案")

        if value in ("|", ">"):
            raise FrontmatterError(
                f"{key} 用了多行块写法（{value}），本解析器不支持；写成一行"
            )
        # 引号里的值是明确的，所以跳过字符检查（见 _is_quoted）。
        if not _is_quoted(value) and any(c in value for c in _UNSUPPORTED_IN_VALUE):
            raise FrontmatterError(
                f"{key} 的值里有本解析器不支持的字符（{_UNSUPPORTED_IN_VALUE}）；"
                f"值写成简单的一行文本，或者整个用引号包起来，需要说明就写进正文"
            )

        open_list = key if value.startswith("[") and not value.endswith("]") else None
        if key not in _IGNORED_KEYS and key != "metadata":
            entries[key] = _strip_quotes(entries[key])

    body = "\n".join(lines[closing + 1:]).strip()
    return entries, body


def _as_text(entries: dict[str, str], key: str) -> str:
    return (entries.get(key) or "").strip()


def parse_skill(directory_name: str, path: Path, text: str) -> Skill:
    """把一个技能文件解析成 `Skill`；任何毛病都抛 FrontmatterError。

    抛而不返回 None，是因为调用方（`SkillLoader.reload`）必须把"为什么"带进 problems
    —— 一个只返回 None 的解析器会让入口只能说"有个技能加载失败"。
    """
    entries, body = parse_frontmatter(text)

    declared = _as_text(entries, "name")
    if not declared:
        raise FrontmatterError("frontmatter 缺少 name")
    if declared != directory_name:
        raise FrontmatterError(
            f"name={declared!r} 和目录名 {directory_name!r} 不一致 —— "
            f"两者必须相同，否则模型看到的技能名和文件路径对不上"
        )
    if not _NAME_RE.fullmatch(declared):
        raise FrontmatterError(
            f"name={declared!r} 不合法：只能用小写字母、数字和连字符，"
            f"且不能以连字符开头/结尾或连着两个（例如 pdf-extract）"
        )
    if len(declared) > 64:
        raise FrontmatterError(f"name 超过 64 个字符（{len(declared)}）")

    description = _as_text(entries, "description")
    if not description:
        raise FrontmatterError(
            "frontmatter 缺少 description —— 它是模型判断「什么时候该用这个技能」的"
            "唯一依据（技能正文在被加载之前是看不见的）"
        )

    size = len(text.encode("utf-8"))
    if size > MAX_SKILL_BYTES:
        raise FrontmatterError(
            f"文件 {size} 字节，超过上限 {MAX_SKILL_BYTES} —— 正文会拼进每一次请求，"
            f"所以超限时拒绝加载而不截断；把细节挪到同目录的另一个文件里，"
            f"让模型需要时自己用 read_file 读"
        )

    return Skill(
        name=declared,
        description=description,
        path=path,
        body=body,
        allowed_tools=_split_tools(_as_text(entries, "allowed-tools")),
        digest=hashlib.sha256(body.encode("utf-8")).hexdigest()[:12],
    )


@dataclass(frozen=True, slots=True)
class _Root:
    """一个技能目录 + 它的优先级。数字大的赢。"""

    path: Path
    priority: int


def default_roots(workspace: str | Path, home: str | Path | None = None) -> tuple[_Root, ...]:
    """约定的技能目录，**低优先级在前**（扫描按这个顺序，后来的覆盖先前的）。

    个人级压项目级，理由见模块 docstring（和 git config 的层级一致）。同一层里
    `.tudouni`（本工具的私有位置）压通用兜底 —— 那是给「这个运行时的技能」留的位置，
    而通用目录是"别人的技能碰巧也在这儿"。
    """
    workspace = Path(workspace).resolve()
    home = (Path.home() if home is None else Path(home)).resolve()

    def group(base: Path) -> list[Path]:
        # 低优先级 → 高优先级
        return [
            base / GENERIC_DIR_NAME,
            base / AGENTS_DIR_NAME / SKILLS_DIR_NAME,
            base / TUDOUNI_DIR_NAME / SKILLS_DIR_NAME,
        ]

    # 先项目后个人：扫描时后扫的优先级更高，所以个人级那份会成为生效的那一份。
    return tuple(
        _Root(path, priority=index)
        for index, path in enumerate([*group(workspace), *group(home)])
    )


class SkillLoader:
    """扫描技能目录。**它只在被调用时读盘**，不在构造时（同 load_system_prompt 那条）。

    目录由 `default_roots` 算出来（或者由调用方显式给一组）。**不接受模型给的路径**：
    这是"技能只有人能改"的另一半 —— 用户级目录在工作区外面，而这条路是硬编码的。
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        directory: str | Path | None = None,
        roots: tuple[_Root, ...] | list[_Root] | None = None,
        home: str | Path | None = None,
    ):
        if roots is not None:
            self._roots = tuple(roots)
        elif directory is not None:
            # 显式指定单个目录（测试用；也是"只要这个目录"的逃生口）。
            self._roots = (_Root(Path(directory), 0),)
        elif workspace is not None:
            self._roots = default_roots(workspace, home)
        else:
            raise ValueError("SkillLoader 需要 workspace 或 directory 之一")

        # 目录列表本身也要能被看见（`--skills` 会打出来）。按扫描顺序保留。
        self.directories = tuple(root.path for root in self._roots)
        # problems 只在 reload 期间攒；类属性给了个空默认值，免得 reload 之前被碰到。
        self._problems: list[str] = []
        self.workspace = Path(workspace) if workspace is not None else None
        # 生效的那个目录（优先级最高的）—— 单目录时代 `directory` 就是这个值。
        self.directory = self.directories[-1] if self.directories else Path(".")

    def _skill_file(self, root: Path, entry: Path) -> Path:
        """一个技能目录里 SKILL.md 的落点，必须仍在**它所属于的那个技能目录**里面。

        `resolve()` 把软链展开，所以"在技能目录下建一个指向 C:/Users/x/.ssh 的软链"
        会在这里被拒。这条检查是**独立写的一份**，没有复用 tools/filesystem.py 的
        safe_path —— 复用会让依赖方向多一条 `skills → tools` 的箭头，而那条箭头正是
        这个包能独立存在的前提。两者管的事也不重叠：safe_path 管的是"工具的参数别
        出去"，这里管的是"技能目录项别指出去"。
        """
        target = (entry / SKILL_FILE_NAME).resolve()
        if target.parent.parent != root.resolve():
            raise ValueError(f"技能目录越界（软链指到了技能目录外面）：{entry}")
        return target

    def _scan(self, root: Path, problems: list[str]) -> list[Skill]:
        """扫一个目录，把读懂的毛病 append 进 problems，返回认出来的技能。

        problems 是**参数**而不是实例状态：扫一个目录这件事没有"记忆"，攒问题的是
        reload 那一层（它才是那次扫描的代表）。
        """
        if not root.is_dir():
            # 目录不存在不是错误（和 .env、.tudouni.json 都不存在同一条）。
            return []

        found: list[Skill] = []
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            try:
                path = self._skill_file(root, entry)
            except ValueError as exc:
                problems.append(f"{entry.name} 被跳过：{exc}")
                continue
            if not path.is_file():
                # 目录建了但还没写 SKILL.md：跳过就好。它是"正在写"的中间状态，
                # 报成 problem 会让每次启动都刷一行噪声。
                continue
            try:
                # utf-8-sig 而不是 utf-8：Windows 上"另存为 UTF-8"（以及 PowerShell 的
                # `Set-Content -Encoding UTF8`）常常带 BOM，而带 BOM 的文件第一行是
                # "\ufeff---"，于是 frontmatter 的第一行判断失败 —— 一个看不见的字符
                # 引起的失败，没人猜得到。没有 BOM 时它和 utf-8 完全一样。
                #
                # 这条和 config._read_json_object 里那条是同一个坑的两次踩中。
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(
                    f"{entry.name} 被跳过：读不了 {SKILL_FILE_NAME}（{exc}）"
                )
                continue

            try:
                found.append(parse_skill(entry.name, path, text))
            except FrontmatterError as exc:
                problems.append(f"{entry.name} 被跳过：{exc}")
        return found

    def reload(self) -> SkillCatalog:
        """重新扫一遍所有目录，按优先级合并。坏文件进 problems，**绝不抛异常**。

        每次调用都重扫，是为了让"会话中途新加一个技能"这件事在下次 load_skill 时
        就能生效：目录表每轮都会重渲染，但**可用清单**是扫描结果，缓存住的话，
        新建的技能要重启才看得见 —— 一个"加了文件就能用"的功能卡在重启上很荒唐。
        `load_skill` 是低频操作，重扫几个只有几个文件的小目录可以忽略代价。

        合并规则：同名的按优先级留最高的那份（个人 > 项目 > 兜底），被遮住的那几份
        进 `shadowed` 报给人看 —— 静默遮蔽会让人改了项目里那份技能却发现"没生效"，
        而真正生效的在另一个盘上。
        """
        problems: list[str] = []
        # 低优先级在前地扫，所以后面的同名会把前面的顶掉。
        collected: dict[str, list[tuple[int, Skill]]] = {}
        scanned: list[Path] = []

        for root in sorted(self._roots, key=lambda item: item.priority):
            if root.path.is_dir():
                scanned.append(root.path)
            for skill in self._scan(root.path, problems):
                collected.setdefault(skill.name, []).append((root.priority, skill))

        skills: list[Skill] = []
        shadowed: list[str] = []
        for name, candidates in sorted(collected.items()):
            # 优先级高的在前：第一个是生效的，其余是被它遮住的。
            candidates.sort(key=lambda item: item[0], reverse=True)
            winner = candidates[0][1]
            locations = tuple(item[1].path for item in candidates)
            if len(locations) > 1:
                shadowed.append(
                    f"{name} 取 {locations[0]}，"
                    + "、".join(str(path) for path in locations[1:])
                    + " 被它遮住了"
                )
            skills.append(
                Skill(
                    name=winner.name,
                    description=winner.description,
                    path=winner.path,
                    body=winner.body,
                    allowed_tools=winner.allowed_tools,
                    digest=winner.digest,
                    locations=locations,
                )
            )

        self._problems = problems
        return SkillCatalog(
            skills=tuple(skills),
            problems=tuple(problems),
            roots=tuple(scanned),
            shadowed=tuple(shadowed),
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SkillLoader({', '.join(str(path) for path in self.directories)})"
