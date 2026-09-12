"""技能：发现（skills/ 包）+ 装载（tools/skills.py）。

这一组测试盯的是五件容易被无声破坏的事：

  1. **坏技能不抛异常、也不静默消失** —— 它进 problems，由入口打给人看。一份写错
     frontmatter 的 SKILL.md 从启动到会话结束都没有任何症状：它只是不在清单里。
  2. **正文不进 `session.messages`** —— 和 todo 列表、步数提示同一条约定（逐轮变化
     的东西不持久化，也不该去稀释"一条 assistant = 一步"那个派生规则）。它每轮重新
     拼在载荷**尾部**，所以断言看的是 ScriptedModel 收到的整份载荷。
  3. **技能目录是控制面**：读可以，写一律拒绝（人批准了也不行）。技能正文不是数据、
     是指令 —— 能写它就等于能改自己此后每一轮的指令。
  4. **加载是幂等的、有上限的、不挤掉旧的**：悄悄挤掉一个已经生效的技能等于伪造模型的
     主张（它下一轮会按自己"记得"的技能做）。
  5. **依赖方向**：`skills/` 不 import 任何内部模块（见 tests/test_imports.py 里那条）。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.skills import (
    MAX_ACTIVE_SKILLS,
    MAX_SKILL_BYTES,
    SKILL_FILE_NAME,
    SKILLS_KEY,
    SkillCatalog,
    SkillLoader,
    default_roots,
    parse_frontmatter,
    parse_skill,
)
from agent_runtime.skills.render import (
    active_line,
    active_names,
    catalog_part,
    skill_note,
)
from agent_runtime.state import Session
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.filesystem import CONTROL_PLANE, FileSystem
from agent_runtime.tools.skills import SkillBoard

from fakes import Collector, ScriptedModel, tool_call

GOOD = """---
name: {name}
description: {description}
---
## 步骤
1. {marker}
"""

# 技能正文里的一个标记，**只在技能文件里出现**。
#
# 用它而不是随便一句中文：提示词和工具描述里已经有一大堆正常的句子，拿一句"看起来像
# 步骤"的话去断言，很可能断言的是 **prompts/system.zh.md 里本来就有的那一句** ——
# 那样即使技能正文一个字都没注入，测试也是绿的（这个坑在写这组测试时真的踩到了：
# `先读目标文件` 正好是系统提示词"编辑前先读目标文件"的前半句）。
MARKER = "技能正文标记-DO-NOT-LEAK-42"


def write_skill(root: Path, name: str, text: str | None = None, **fields) -> Path:
    """在工作区里落一个技能文件，返回技能目录。"""
    directory = root / ".tudouni" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE_NAME).write_text(
        text if text is not None else GOOD.format(
            name=name,
            description=fields.get("description", f"{name} 的说明"),
            marker=MARKER,
        ),
        encoding="utf-8",
    )
    return directory


def make_registry(root: Path, metadata: dict | None = None) -> tuple[object, SkillBoard, dict]:
    """按 main.py 的装配方式造一套 (registry, board, metadata)。**就这一条路。**

    **不能自己 new 一个 SkillBoard 再顺手传给注册表。** 技能的"写"（工具调用）和"读"
    （每轮拼在载荷尾部的那段）必须是同一个对象 —— 它带着重扫口，造两个副本的后果是
    "技能加载成功了、却永远不出现在载荷里"，既没有异常也没有审计痕迹。所以构造只发生在
    `create_tool_registry` 里，读的一方从 `registry.skills` 取回同一个对象
    （test_the_note_never_enters_session_messages 盯着的就是这个形态）。

    返回的 metadata 就是 board 真正在写的那块 —— 测试要断言"存下来的只是指针"。
    """
    metadata = {} if metadata is None else metadata
    loader = SkillLoader(root)
    registry = create_tool_registry(
        str(root),
        skills=loader.reload(),
        skill_metadata=metadata,
        skill_loader=loader,
    )
    return registry, registry.skills, metadata


def make_board(root: Path, metadata: dict | None = None) -> tuple[SkillBoard, dict]:
    """只要 board 的测试用它。"""
    _, board, metadata = make_registry(root, metadata)
    return board, metadata


# --- 1b. 多目录与优先级（个人级压项目级，见 README） ----------------------

def test_default_roots_cover_the_six_conventional_dirs():
    """约定俗成的六个位置：工具私有（.tudouni）+ 通用兜底（.agents / .skills），
    各自分用户级和项目级 —— 对齐生态里 `.<工具>/skills/` 的惯例，所以别人现成的技能包
    直接放进来就能用。
    """
    base = Path.cwd()
    roots = default_roots(base / "ws", home=base / "home" / "me")
    paths = [root.path.as_posix().replace(base.as_posix(), "<base>") for root in roots]

    assert paths == [
        "<base>/ws/.skills",
        "<base>/ws/.agents/skills",
        "<base>/ws/.tudouni/skills",
        "<base>/home/me/.skills",
        "<base>/home/me/.agents/skills",
        "<base>/home/me/.tudouni/skills",
    ]
    # 优先级严格递增，且个人级那一组整体高于项目级那一组。
    assert [root.priority for root in roots] == sorted(root.priority for root in roots)
    assert roots[-1].priority > roots[2].priority


def test_personal_skills_beat_project_skills(workdir):
    """**个人级压项目级**，和 Claude Code 的层级一致（也同 git config 的"用户配置覆盖
    仓库配置"）：机器是人的，仓库是别人的。

    本机用户目录里的技能因此可以覆盖团队仓库里的同名技能。
    """
    home = workdir / "home"
    project = workdir / "project"
    write_skill(project, "shared", description="项目里那份")
    write_skill(home, "shared", description="我个人那份")

    catalog = SkillLoader(project, home=home).reload()

    assert len(catalog.skills) == 1
    assert catalog.skills[0].description == "我个人那份"
    # 被遮住的那份**没消失**，只是不算数：它带着路径进 catalog，由入口报给人看。
    assert len(catalog.skills[0].locations) == 2
    assert catalog.shadowed and "被它遮住了" in catalog.shadowed[0]


def test_a_shadowed_skill_is_reported_with_both_paths(workdir):
    """静默遮蔽是这里最坏的失败形态：人反复改项目里那份技能、发现"没生效"，而真正
    生效的那份在另一个盘上。所以两条路径都要报出来。
    """
    home = workdir / "home"
    project = workdir / "project"
    write_skill(project, "dup")
    write_skill(home, "dup")

    catalog = SkillLoader(project, home=home).reload()

    assert len(catalog.shadowed) == 1
    message = catalog.shadowed[0]
    assert str(project) in message and str(home) in message


def test_the_same_skill_in_one_directory_only_counts_once(workdir):
    """同一个目录里不会有重名（目录名就是技能名），所以 locations 只有一条。"""
    write_skill(workdir, "solo")

    catalog = SkillLoader(workdir).reload()

    assert len(catalog.skills) == 1
    assert len(catalog.skills[0].locations) == 1
    assert catalog.skills[0].shadowed == ()
    assert catalog.shadowed == ()


def test_roots_are_reported_so_people_can_find_them(workdir):
    """用户级目录在工作区外面 —— 不列出来，人根本想不到去那儿找技能。"""
    write_skill(workdir, "here")
    catalog = SkillLoader(workdir).reload()

    project_root = (workdir / ".tudouni" / "skills").resolve()
    assert project_root in catalog.roots
    # 报出来的是**真的扫过**的目录：不存在的目录不该混进来当噪声。
    assert all(path.is_dir() for path in catalog.roots)
    # 而且报出来的那个目录就是技能实际所在的地方（否则这一行就是误导）。
    assert catalog.skills[0].path.parent.parent == project_root


def test_a_user_level_skill_is_loadable_end_to_end(workdir):
    """用户级技能要能真的装上用（不只是被扫到）：写到 metadata 的那一条必须来自
    个人级那一份。
    """
    home = workdir / "home"
    project = workdir / "project"
    project.mkdir(parents=True, exist_ok=True)
    write_skill(home, "personal", description="我个人那份")

    metadata: dict = {}
    loader = SkillLoader(project, home=home)
    registry = create_tool_registry(
        str(project), skills=loader.reload(), skill_metadata=metadata, skill_loader=loader
    )

    result = registry.get("load_skill").execute({"name": "personal"})

    assert result.audit["skill_action"] == "load"
    assert metadata[SKILLS_KEY][0]["name"] == "personal"


# --- 1c. 对齐 agentskills.io 规范 ------------------------------------------

def test_allowed_tools_follows_the_spec_syntax():
    """规范给的是 `Bash(git:*) Bash(jq:*) Read`：**空格分隔**，而且带 `Tool(specifier)`
    限定符。按逗号切、或者把整串当成一个工具名，都会让"技能声明用了哪些工具"变成一句
    谁也认不出的垃圾 —— 而这条信息的唯一读者是模型。

    两条 `Bash(...)` 是**同一个工具的两个限定**，收敛成一个 `Bash`：列表里说的还是
    "它要用 Bash"，去重比留着两个一样的名字更诚实。
    """
    skill = parse_skill(
        "x", Path("SKILL.md"),
        "---\nname: x\ndescription: d\nallowed-tools: Bash(git:*) Bash(jq:*) Read\n---\nb\n",
    )

    assert skill.allowed_tools == ("Bash", "Read")

    # 空格和逗号两种写法都认；重复的只留一次。
    for text in (
        "allowed-tools: read_file, shell\n",
        "allowed-tools: read_file shell\n",
        "allowed-tools: [read_file, shell]\n",
        "allowed-tools: read_file, shell, read_file\n",
    ):
        parsed = parse_skill("x", Path("SKILL.md"), f"---\nname: x\ndescription: d\n{text}---\nb\n")
        assert parsed.allowed_tools == ("read_file", "shell"), text


def test_spec_optional_fields_are_recognised():
    """`license` / `compatibility` 认下但不用；`metadata` 是规范里唯一的嵌套映射，
    整块跳过。**认下它们是为了不吃掉合规的技能包** —— 别人从生态里拿一份技能进来，
    光是写了 license 就被判成"不认识的键"然后整个跳过，那就谈不上兼容。
    """
    text = (
        "---\n"
        "name: pdf\n"
        "description: 处理 PDF\n"
        "license: Apache-2.0\n"
        "compatibility: Requires git and python\n"
        "metadata:\n"
        "  author: example-org\n"
        "  version: \"1.0\"\n"
        "---\n"
        "## 步骤\n1. 提取文本\n"
    )

    skill = parse_skill("pdf", Path("SKILL.md"), text)

    assert skill.description == "处理 PDF"
    assert "提取文本" in skill.body
    # metadata 的内容不进正文（它是给别的工具看的元信息，不是步骤）。
    assert "example-org" not in skill.body


def test_metadata_does_not_open_the_door_to_arbitrary_nesting():
    """`metadata` 是唯一被允许的嵌套结构。别的键下面缩进仍然报错 —— 这条是防"顺手放宽"
    的：缩进一旦被静默接受，写错层级的 description 会被当成顶层键收下，而写的人以为
    自己写了个结构。
    """
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\ndescription:\n  nested: y\n---\n")


def test_name_conventions_match_the_spec():
    """规范给了三条硬性约束，逐条盯住（连着两个连字符是最容易写出来、也最容易被
    `[a-z0-9-]+` 这种宽松正则放过去的一条）。
    """
    for bad in ("PDF-Processing", "pdf--processing", "-pdf", "pdf-", "pdf_processing"):
        with pytest.raises(ValueError):
            parse_skill(bad, Path("SKILL.md"), f"---\nname: {bad}\ndescription: d\n---\nb\n")

    for good in ("pdf", "pdf-processing", "data2-analysis"):
        assert parse_skill(
            good, Path("SKILL.md"), f"---\nname: {good}\ndescription: d\n---\nb\n"
        ).name == good


# --- 1. 发现：坏文件进 problems，不抛异常 ---------------------------------

def test_no_skills_directory_is_not_an_error(workdir):
    """目录不存在不是错误（和 .env / .tudouni.json 都不存在同一条）。

    技能是可选能力：没有它就是一个工具都不注册，而不是启动失败。
    """
    catalog = SkillLoader(workdir).reload()
    assert catalog.skills == ()
    assert catalog.problems == ()


def test_a_good_skill_is_discovered(workdir):
    write_skill(workdir, "pdf-extract", description="从 PDF 提取文本")

    catalog = SkillLoader(workdir).reload()

    assert [skill.name for skill in catalog.skills] == ["pdf-extract"]
    assert catalog.skills[0].description == "从 PDF 提取文本"
    assert MARKER in catalog.skills[0].body
    assert catalog.problems == ()
    # digest 必须只覆盖正文：它是"文件在上次加载之后被改过吗"的唯一依据。
    assert catalog.skills[0].digest


def test_missing_description_is_reported_not_raised(workdir):
    """description 是模型判断"什么时候该用这个技能"的唯一依据（正文在加载前看不见），
    所以缺了它这个技能等于没用 —— 但**不能抛异常**，否则一个坏文件会让整个程序起不来。
    """
    write_skill(workdir, "broken", text="---\nname: broken\n---\n## 步骤\n1. x\n")

    catalog = SkillLoader(workdir).reload()

    assert catalog.skills == ()
    assert len(catalog.problems) == 1
    assert "broken" in catalog.problems[0]
    assert "description" in catalog.problems[0]


def test_name_mismatch_with_directory_is_reported(workdir):
    """名字和目录名不一致时模型看到的技能名和文件路径对不上 —— 它按路径去读附件会读错。"""
    write_skill(workdir, "real-name", text=GOOD.format(name="other", description="x", marker=MARKER))

    catalog = SkillLoader(workdir).reload()

    assert catalog.skills == ()
    assert "不一致" in catalog.problems[0]


def test_one_bad_skill_does_not_hide_the_others(workdir):
    """坏文件只影响它自己 —— 这条是这个模块最重要的行为：静默少一个技能是最坏的形态，
    但因为它坏就让别的技能也消失，同样没有道理。"""
    write_skill(workdir, "good")
    write_skill(workdir, "bad", text="---\nname: bad\n---\n正文\n")

    catalog = SkillLoader(workdir).reload()

    assert [skill.name for skill in catalog.skills] == ["good"]
    assert len(catalog.problems) == 1


def test_a_utf8_bom_does_not_break_the_first_line(workdir):
    """带 BOM 的 UTF-8 文件必须照常解析。

    这是**实测踩到**的：Windows 上"另存为 UTF-8"和 PowerShell 的
    `Set-Content -Encoding UTF8` 都会写 BOM，于是文件第一行是 "\\ufeff---"，
    frontmatter 的第一行判断失败 —— 报出来的是"文件开头必须有 frontmatter"，
    而人盯着文件看，那里明明就是三个连字符。一个看不见的字符引起的失败，没人猜得到。

    和 config._read_json_object 用 utf-8-sig 是同一个坑的两次踩中（那次是 JSON 解析
    在第一行报 Expecting value）。
    """
    write_skill(workdir, "bom", text="\ufeff" + GOOD.format(
        name="bom", description="带 BOM", marker=MARKER,
    ))

    catalog = SkillLoader(workdir).reload()

    assert [skill.name for skill in catalog.skills] == ["bom"]
    assert catalog.problems == ()


def test_oversized_skill_is_refused_not_truncated(workdir):
    """超限时拒绝加载，**不截断**：截出来是一份"看起来完整、其实少了后半段步骤"的说明，
    比读不到更坏（模型会照着做一半，然后以为做完了）。
    """
    body = "x" * (MAX_SKILL_BYTES + 1)
    write_skill(workdir, "huge", text=f"---\nname: huge\ndescription: 大\n---\n{body}\n")

    catalog = SkillLoader(workdir).reload()

    assert catalog.skills == ()
    assert str(MAX_SKILL_BYTES) in catalog.problems[0]
    assert "超过上限" in catalog.problems[0]


def test_frontmatter_rejects_what_it_cannot_read():
    """解析器只认单层 `key: value`，看不懂的一律报错 —— 取向和 security/commands.py 的
    "看不懂就问"一致。每一条都是"看起来正常、其实被静默吃掉一段"的形状。
    """
    with pytest.raises(ValueError):
        parse_frontmatter("name: x\n")               # 没有 frontmatter
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\n")          # 没有闭合
    with pytest.raises(ValueError):
        parse_frontmatter("---\n  name: x\n---\n")   # 缩进（嵌套/列表项的形态）
    with pytest.raises(ValueError):
        parse_frontmatter("---\n- a\n---\n")        # 列表项
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\ntypo: y\n---\n")   # 不认识的键
    with pytest.raises(ValueError):
        parse_frontmatter("---\nname: x\nname: y\n---\n")   # 键写两遍
    # 下面这两条不该抛：简单值和带引号的值都是认的（引号里出现 `:` `#` 不算嵌套）
    entries, _ = parse_frontmatter(
        "---\nname: x\ndescription: \"带: 冒号和 # 井号\"\n---\n"
    )
    assert entries["description"] == "带: 冒号和 # 井号"


def test_allowed_tools_accepts_both_notations():
    """`a, b` 和 `[a, b]` 都要能用：现成的技能包两种写法都有，而认不出来的后果是限制
    静默失效 —— 写的人以为自己收窄了范围。"""
    for text in (
        "---\nname: x\ndescription: d\nallowed-tools: read_file, shell\n---\n",
        "---\nname: x\ndescription: d\nallowed-tools: [read_file, shell]\n---\n",
    ):
        entries, _ = parse_frontmatter(text)
        skill = parse_skill("x", Path("SKILL.md"), text)
        assert skill.allowed_tools == ("read_file", "shell"), entries


def test_bad_name_shape_is_rejected(workdir):
    write_skill(workdir, "Bad_Name", text=GOOD.format(name="Bad_Name", description="x", marker=MARKER))

    catalog = SkillLoader(workdir).reload()

    assert catalog.skills == ()
    assert "不合法" in catalog.problems[0]


# --- 2. 装载：三条出口与 metadata -----------------------------------------

def test_empty_arguments_list_the_catalog(workdir):
    """空参列清单，**不写 metadata**：清单是"有哪些"，不是"用了哪些" —— 写进去的话，
    恢复会话时它会被当成"已加载"，然后被渲染成正文块。
    """
    write_skill(workdir, "one")
    write_skill(workdir, "two")
    board, metadata = make_board(workdir)

    result = board.list_skills()

    assert "one" in result.text and "two" in result.text
    assert result.audit["skill_action"] == "catalog"
    assert SKILLS_KEY not in metadata


def test_loading_records_only_a_pointer(workdir):
    """metadata 里存的是**名字 + 摘要**，不是正文：正文每轮从磁盘重渲染，所以
    "会话里存着旧正文、磁盘上是新正文"这两份事实不会同时存在。
    """
    write_skill(workdir, "pdf-extract")
    board, metadata = make_board(workdir)

    board.load("pdf-extract")

    entry = metadata[SKILLS_KEY][0]
    assert entry["name"] == "pdf-extract"
    assert set(entry) == {"name", "digest"}
    assert "步骤" not in str(entry)


def test_loading_an_unknown_skill_returns_text_not_an_exception(workdir):
    """名字打错是模型读着一句话就能自己改对的事，所以返回文本（跟着 edit_file /
    grep / shell 那条规矩走），而不是抛出去被记成"工具故障"。"""
    write_skill(workdir, "known")
    board, metadata = make_board(workdir)

    result = board.load("typo")

    assert result.audit["skill_action"] == "unknown"
    assert "known" in result.text      # 把现有清单给它，它才知道该改成什么
    assert SKILLS_KEY not in metadata


def test_loading_twice_is_idempotent(workdir):
    write_skill(workdir, "one")
    board, metadata = make_board(workdir)

    board.load("one")
    again = board.load("one")

    assert again.audit["skill_action"] == "already"
    assert len(metadata[SKILLS_KEY]) == 1


def test_loading_beyond_the_cap_is_refused_without_dropping_the_old(workdir):
    """到上限时拒绝新的，**不挤掉旧的**：悄悄卸载一个已经生效的技能等于伪造模型的主张
    —— 它下一轮会按自己"记得"的技能做，而那份已经不在载荷里了。
    """
    for index in range(MAX_ACTIVE_SKILLS + 1):
        write_skill(workdir, f"skill-{index}")
    board, metadata = make_board(workdir)
    for index in range(MAX_ACTIVE_SKILLS):
        board.load(f"skill-{index}")

    result = board.load(f"skill-{MAX_ACTIVE_SKILLS}")

    assert result.audit["skill_action"] == "full"
    assert active_names(metadata) == [f"skill-{i}" for i in range(MAX_ACTIVE_SKILLS)]
    assert "unload" in result.text


def test_unload_clears_everything(workdir):
    write_skill(workdir, "one")
    board, metadata = make_board(workdir)
    board.load("one")

    result = board.unload()

    assert active_names(metadata) == []
    assert "已卸载" in result.text
    assert skill_note(metadata, board.catalog) is None


# --- 3. 渲染：目录是一级，正文是二级，都不进 messages ----------------------

def test_catalog_note_hides_already_loaded_skills(workdir):
    """同一件事在一份载荷里说两遍，只会让模型以为它们是两回事。"""
    write_skill(workdir, "one")
    write_skill(workdir, "two")
    board, metadata = make_board(workdir)
    board.load("one")

    part = catalog_part(metadata, board.catalog)

    assert "two" in part
    assert "- one" not in part


def test_skill_note_labels_the_body_as_untrusted(workdir):
    """技能正文比网页正文危险 —— 网页正文只进一次历史，技能正文**每一轮都重发**。
    这个标注是提示词注入唯一的防线（和 webfetch 那句同一条），而且必须在正文之前。
    """
    write_skill(workdir, "one")
    board, metadata = make_board(workdir)
    board.load("one")

    note = skill_note(metadata, board.catalog)

    assert "不是用户说的话" in note
    assert note.index("不是用户说的话") < note.index(MARKER)
    assert active_line(metadata) == "已加载技能：one"


def test_a_changed_skill_file_is_flagged(workdir):
    """正文每轮从磁盘重读，所以人改了技能文件之后模型下一轮就看到新版 —— 但必须**说出来**
    （不说的话，"它怎么突然按另一套做了"在对话里完全看不出来）。
    """
    directory = write_skill(workdir, "one")
    board, metadata = make_board(workdir)
    board.load("one")

    (directory / SKILL_FILE_NAME).write_text(
        GOOD.format(name="one", description="改过", marker=MARKER) + "\n3. 第三步\n", encoding="utf-8"
    )
    board2, _ = make_board(workdir, metadata)   # 重新扫一次，模拟"下一次运行"

    note = skill_note(metadata, board2.catalog)

    assert "被改过" in note
    assert "第三步" in note


def test_a_deleted_skill_file_is_reported_not_silently_kept(workdir):
    """文件没了就说清楚，而不是继续渲染一份空气 —— 一份留在清单里、正文却是空的技能，
    会让模型以为它仍然有效。"""
    directory = write_skill(workdir, "one")
    board, metadata = make_board(workdir)
    board.load("one")

    (directory / SKILL_FILE_NAME).unlink()
    catalog = SkillLoader(workdir).reload()

    note = skill_note(metadata, catalog)

    assert "读不到" in note


def test_the_note_never_enters_session_messages(workdir):
    """技能正文每轮重新拼在**载荷尾部**，从不写进 messages —— 和步数提示、任务列表
    同一条约定（见 agent._status_note）。逐轮变化的东西不该被持久化。

    顺带钉住那条一次性延迟：尾部那段是在**每次请求之前**按当前 metadata 拼的，所以
    刚刚加载的那一步里模型还看不到正文（它先拿到"已加载"的回执），下一次请求才带上。
    这是"每轮重新渲染"这个设计的必然结果，不是 bug —— 但它是模型唯一会感到别扭的地方，
    所以工具回执里明说了"此后每一轮"。
    """
    write_skill(workdir, "one")
    # 会话先定下来，再把 `session.metadata` 这个**活字典**交给 board —— 和 main.py 的
    # 装配顺序一致。反过来（先造 board、再造 Session）的话，`Session.new()` 会给出
    # metadata 那一份默认值，于是 board 写进一个没人读的字典里：技能加载成功了，
    # 载荷尾部却什么都看不见（这条测试第一次跑就是这么红的）。
    session = Session.new("s1")
    registry, board, _ = make_registry(workdir, session.metadata)
    assert registry.get("load_skill").handler is board   # 写和读必须是同一个对象

    # 尾部那段和 main.py 里装配的一模一样：读 `board.catalog`（每次读都重扫），而不是
    # 构造时那份快照 —— 这正是"清单和能不能加载是同一份事实"落地的地方。
    def notes(meta):
        return "\n\n".join(filter(None, (
            catalog_part(meta, board.catalog),
            skill_note(meta, board.catalog),
        )))

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("load_skill", {"name": "one"})]),
        ModelResponse(content=None, tool_calls=[
            tool_call("list_files", {"path": "."}, call_id="c2"),
        ]),
        ModelResponse(content="done"),
    ])
    agent = Agent(
        model, registry, PermissionPolicy(auto_approve=("low",)),
        session_notes=notes,
    )
    agent.run(session, "用技能做件事")

    # 加载那一步的载荷尾部还只有目录（它就是靠这一步知道有技能可加载）……
    assert MARKER not in model.seen_messages[0][-1]["content"]
    # ……下一步的载荷尾部就带上正文了（"此后每一轮"这句话是靠这个成立的）。
    assert MARKER in model.seen_messages[1][-1]["content"]
    # 而历史里一条都没有：正文只活在载荷尾部，不进 messages。
    #
    # 断言看的是**消息正文**（content），不看 assistant 的 tool_calls —— 技能名出现在
    # 那次调用的参数里是正确且必要的（"模型要加载 one"本身就是历史的一部分）。把参数
    # 也算进来的话，这条断言会变成"模型不许提到技能名"，那是另一回事。
    assert all(
        MARKER not in str(m.get("content"))
        for m in session.messages
        if m["role"] != "assistant"
    )
    # 加载只留下一个指针（名字 + 摘要，没有正文），而且它写进了**会话自己**那个字典。
    assert session.metadata[SKILLS_KEY][0]["name"] == "one"
    assert "步骤" not in str(session.metadata[SKILLS_KEY])


def test_a_skill_added_after_startup_can_still_be_loaded(workdir):
    """运行中途新加的技能要能立刻用上。

    这条盯的是一个**自相矛盾**的状态：技能目录每轮都被渲染进载荷，所以"模型看得见清单、
    却加载不了刚加进来的那个技能"是说不通的。构造时那份快照会造出这个状态，所以 board
    每次调用前重扫一次技能目录（见 SkillBoard 的类 docstring）。
    """
    write_skill(workdir, "known")
    registry, board, metadata = make_registry(workdir)

    write_skill(workdir, "later")          # 技能目录在扫描之后才多出这一个

    result = registry.get("load_skill").execute({"name": "later"})

    assert result.audit["skill_action"] == "load"
    # 清单和正文读的是同一份事实：加载之后正文立刻渲染得出来。
    assert MARKER in skill_note(metadata, board.catalog)


def test_a_skill_whose_body_has_no_steps_is_explicit(workdir):
    """只有 frontmatter、没有正文的技能要**明说**。

    不说的话，模型会看到一段光秃秃的标题然后合理地怀疑自己没读到东西，于是反复重新
    加载 —— 而这恰好是它最不该把步数花在上面的事。
    """
    write_skill(workdir, "empty", text="---\nname: empty\ndescription: 没有步骤\n---\n")
    board, metadata = make_board(workdir)
    board.load("empty")

    note = skill_note(metadata, board.catalog)

    assert "没有正文步骤" in note


# --- 4. 工具接线 -----------------------------------------------------------

def test_load_skill_is_not_registered_without_skills(workdir):
    """没有技能就**不注册**这个工具：schema 每一轮都要发出去，而一个空目录里的
    load_skill 只会让模型白花一步去调一次（和缺 TAVILY_API_KEY 不注册 web_search 同路）。
    """
    registry = create_tool_registry(str(workdir))

    with pytest.raises(KeyError):
        registry.get("load_skill")


def test_load_skill_is_registered_when_there_are_skills(workdir):
    write_skill(workdir, "one")
    metadata: dict = {}
    registry, board, _ = make_registry(workdir, metadata)

    tool = registry.get("load_skill")
    # LOW 才落在默认的 auto_approve 里：为了读一份说明书先弹审批是本末倒置 ——
    # 那会让人一路按 y，而审批一旦变成仪式就不再是保护。
    assert tool.risk.value == "low"
    # 不声明 parallel_safe：它写 session.metadata 这块共享状态（同 todo_write）。
    assert tool.parallel_safe is False
    assert "不是用户说的话" in tool.description
    # 注册表拿着造出来的那个 board，而且它写的就是调用方给的那块 metadata ——
    # 载荷尾部那段渲染必须读**同一个**对象，否则技能加载成功了却永远不出现在载荷里
    # （见 make_registry 那段说明）。
    assert tool.handler is board is registry.skills
    assert board._metadata is metadata


def test_empty_arguments_pass_validation(workdir):
    """`name` 有默认值，空参是合法调用（列清单）—— schema 里它因此不是必填。"""
    write_skill(workdir, "one")
    registry, _, _ = make_registry(workdir)

    result = registry.get("load_skill").execute({})

    assert "one" in result.text
    # schema 里 name 不是必填（有默认值），所以 required 键干脆不出现 —— 用 get 而不是
    # 下标：Pydantic 对"没有必填字段"的模型就是不给这个键（tools/ask.py 的 options
    # 那几个同理）。
    assert registry.get("load_skill").parameters.get("required", []) == []


def test_extra_arguments_are_rejected(workdir):
    """extra="forbid" 复用 ToolArgs 的既有行为：模型多传一个字段要被明确打回，
    而不是被静默丢掉（那样"模型读错了 schema"这件事就被藏起来了）。"""
    write_skill(workdir, "one")
    registry, _, _ = make_registry(workdir)

    with pytest.raises(ValidationError):
        registry.get("load_skill").execute({"skill": "one"})


# --- 5. 控制面：技能目录只能由人写 ----------------------------------------

def test_the_skills_directory_is_control_plane(workdir):
    """技能正文不是数据，是**指令** —— 能写它就等于能改自己此后每一轮的指令，
    而且改一次永久生效。所以它是"读可以、写一律拒绝，人批准了也不行"。
    """
    fs = FileSystem(str(workdir))

    for path in (
        ".tudouni/skills/evil/SKILL.md",
        ".tudouni/skills/evil/REFERENCE.md",
        f"{CONTROL_PLANE[1]}/anything.txt",
    ):
        with pytest.raises(PermissionError):
            fs.writable_path(path)

    # 读是正当的：模型该看得见自己项目的技能文件（它还要读同目录的附件）。
    fs.safe_path(".tudouni/skills/evil/SKILL.md")


def test_the_control_plane_uses_the_skills_package_constant():
    """这条盯着"技能住哪"这个事实只有一个来源。

    抄第二份字面量的话，哪天目录改名就会变成"技能还在加载、agent 却已经能写它了" ——
    一个不会有任何报错的组合。
    """
    from agent_runtime.skills import TUDOUNI_DIR_NAME

    assert TUDOUNI_DIR_NAME in CONTROL_PLANE


def test_a_symlinked_skill_directory_cannot_escape(workdir):
    """技能目录项指到外面时必须被拒：否则技能会变成读任意文件的通道，而那条路绕过了
    read_file 的工作区边界（和 webfetch 拒 file:// 是同一条担心）。
    """
    outside = workdir.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / SKILL_FILE_NAME).write_text(
        GOOD.format(name="escape", description="x", marker=MARKER), encoding="utf-8"
    )
    root = workdir / "ws"
    (root / ".tudouni" / "skills").mkdir(parents=True, exist_ok=True)
    link = root / ".tudouni" / "skills" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("这台机器上建不了软链（Windows 需要开发者模式或管理员）")

    catalog = SkillLoader(root).reload()

    assert catalog.skills == ()
    assert "越界" in catalog.problems[0]


# --- 6. 审计 ---------------------------------------------------------------

def test_each_load_leaves_one_tool_result_event(workdir):
    """每次 load_skill 一条 tool_result，带上工具自己才知道的字段（正文多长、声明了
    哪些工具）—— 那是事后回答"这次会话为技能付了多少上下文"所需要的事实。
    """
    write_skill(workdir, "one")
    session = Session.new("s1")
    registry, _, _ = make_registry(workdir, session.metadata)
    sink = Collector()

    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("load_skill", {"name": "one"})]),
        ModelResponse(content="done"),
    ])
    agent = Agent(
        model, registry, PermissionPolicy(auto_approve=("low",)), on_event=sink,
    )
    agent.run(session, "用技能做件事")

    results = sink.of("tool_result")
    assert len(results) == 1
    assert results[0]["tool"] == "load_skill"
    assert results[0]["status"] == "ok"
    assert results[0]["skill_action"] == "load"
    assert results[0]["skill_chars"] > 0
    # LOW ⇒ 落在默认的 auto_approve 里，所以裁决是 auto_allowed，**没问过任何人**
    # （outcome 那一栏就是为回答这个问题存在的，见 security/gate.py 那张来路表）。
    permissions = sink.of("permission")
    assert [event["outcome"] for event in permissions] == ["auto_allowed"]
    assert all("waited_ms" not in event for event in permissions)
