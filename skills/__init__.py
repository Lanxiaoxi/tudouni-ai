"""技能：发现（loader）+ 渲染（render）。

**这个包不 import 任何内部模块**，这是它能独立成包的全部依据。所以：

  * 它不认识 `Tool` / `ToolRegistry` / `ToolResult` —— 那是 `tools/skills.py` 的活；
  * 它不 import `state` —— 已加载技能的指针就存在 `session.metadata` 这个普通 dict 里，
    渲染函数收的也是 `Mapping`，所以"技能比进程活得久"这件事由会话文件自然提供；
  * 它不 import `security` —— 技能文本里写的工具限制**只写进说明**，真正的拦截必须在
    关卡里（见 README 的「已知取舍」）。

顺序反过来的话就会出现 `skills → tools`，而 `tools/builtin.py` 又要 import 本包来注册
load_skill —— 环一出现，README 里那句"依赖方向是单向的，无环"就成了假话。所以
`tests/test_imports.py` 里有一条测试盯着这个包的 import 行。
"""

from .loader import (
    AGENTS_DIR_NAME,
    GENERIC_DIR_NAME,
    MAX_ACTIVE_SKILLS,
    MAX_SKILL_BYTES,
    SKILL_FILE_NAME,
    SKILLS_DIR_NAME,
    SKILLS_KEY,
    TUDOUNI_DIR_NAME,
    FrontmatterError,
    Skill,
    SkillCatalog,
    SkillLoader,
    default_roots,
    parse_frontmatter,
    parse_skill,
)
from .render import (
    active_line,
    active_names,
    catalog_entries,
    catalog_part,
    load_entries,
    note_chars,
    skill_note,
    source_lines,
)

__all__ = [
    "AGENTS_DIR_NAME",
    "FrontmatterError",
    "GENERIC_DIR_NAME",
    "MAX_ACTIVE_SKILLS",
    "MAX_SKILL_BYTES",
    "SKILL_FILE_NAME",
    "SKILLS_DIR_NAME",
    "SKILLS_KEY",
    "TUDOUNI_DIR_NAME",
    "Skill",
    "SkillCatalog",
    "SkillLoader",
    "active_line",
    "active_names",
    "catalog_entries",
    "catalog_part",
    "default_roots",
    "load_entries",
    "note_chars",
    "parse_frontmatter",
    "parse_skill",
    "skill_note",
    "source_lines",
]
