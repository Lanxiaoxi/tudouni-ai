"""load_skill 工具：把技能的"步骤正文"装进这次会话。

**这一层是薄代理，技能的领域不在这里。** 扫描、解析、渲染全在 `skills/` 包里
（`skills/loader.py` / `skills/render.py`），这里只做三件**只有工具层才知道**的事：

  1. 定义参数模型（schema 和校验同源，见 tools/tool.py 的 ToolArgs）；
  2. 把"已加载哪些技能"写进 `session.metadata`（和 `todo.TodoBoard` 完全同构）；
  3. 返回 `ToolResult`（给模型的文本 + 只有工具自己知道的审计字段）。

拆成两个文件不是洁癖，是**依赖方向**：`skills/` 一旦认识 `Tool`，就会出现
`skills → tools`，而 `tools/builtin/__init__.py` 又要 import `skills` 来注册本工具 —— 成环。
判定留在内部、接线交给工具层，和 `tools/builtin/webfetch.py`（有 WebFetch、
在 builtin/__init__.py 里注册）是同一条原则的又一次应用。

**风险等级 LOW，不触发审批。** 理由和 `todo_write` 完全一样：它只读工作区里的技能
文件、只改会话里属于它自己的那一小块，碰不到工作区、也碰不到控制面。为了读一份说明书
先弹一次审批是本末倒置 —— 那会让人一路按 y，审批在这里就不再是保护，只是打断。

**不声明 parallel_safe。** 它写的是 `session.metadata` 这块共享状态，一批里两条同时跑
就是经典 lost update，而且两边都会报成功（和 `todo_write` / `edit_file` 不能并行是
同一个理由）。不声明就够了：那一条会让整批退回串行。

**它给模型的文本里刻意不带正文。** 正文由载荷尾部那份渲染提供（skills/render.py）
—— 抄进工具结果就是永久把同一份内容在历史里存两份（`todo._ack` 不回显整份列表是
同一条理由），而且恢复会话时那份历史副本会被后续几十步稀释。
"""

from collections.abc import MutableMapping
from typing import Any

from pydantic import Field

from agent_runtime.skills.loader import (
    MAX_ACTIVE_SKILLS,
    SKILLS_KEY,
    SkillCatalog,
    SkillLoader,
)
from agent_runtime.skills.render import active_names, load_entries, note_chars

from ..tool import ToolArgs, ToolResult


class LoadSkillArgs(ToolArgs):
    """load_skill 的参数。

    `name` **有默认值（空）**，所以 schema 里它不是必填：空参就是"列出有哪些技能"。
    这让模型能用同一条调用先看清单再决定读哪个，不必多一个工具。

    这和 `todo_write` 的 `todos` 必填正好相反，方向却是一致的 —— 那边"漏给字段"
    会被解析成清空列表（一次静默的数据丢失），这边"漏给字段"只是列个清单，没有任何
    破坏性。**默认值该不该有，取决于漏掉它的后果，不取决于风格。**
    """

    name: str = Field(
        default="",
        description="要加载的技能名。留空表示只列出有哪些技能，不加载任何东西",
    )
    unload: bool = Field(
        default=False,
        description="true 表示卸载所有已加载的技能（技能正文此后不再出现在对话里）",
    )


class SkillBoard:
    """已加载技能这个会话状态的读写口。**它不碰文件，只碰会话的 metadata。**

    和 `TodoBoard` 一样，它只能在会话定下来之后才造得出来（技能是**按会话的状态**，
    不是装配期就定下来的能力），然后像 `workspace` / `questioner` 那样注进注册表。
    传进来的是 `session.metadata` 这个**活字典**，所以加载过的技能跟着会话一起落盘，
    下个进程恢复会话时还在。

    `metadata=None` 表示"没有接到任何会话上"（测试里裸调 create_tool_registry 的情形）：
    加载照收，只是没人看得见。

    `loader` 是可选的重扫口：给了它，**每次调用前**都重扫一次技能目录。

    为什么要重扫，而不是用构造时那份快照：技能目录每轮都会被渲染进载荷，所以"模型看得见
    清单、却加载不了刚加进来的那个技能"是一个**自相矛盾**的状态。快照会造出这个状态
    —— 人在会话开着的时候新建了一个技能，模型看到它出现在目录里，调 load_skill 却得到
    "没有这个技能"。一次 load_skill 是低频操作，而重扫一个只有几个文件的小目录可以忽略，
    所以这里选**一致性**。它顺带把"改过的技能文件"也带进同一条路：重扫之后 digest 变了，
    渲染那边就会如实标出"这个技能文件被改过"。
    """

    def __init__(
        self,
        metadata: MutableMapping[str, Any] | None = None,
        catalog: SkillCatalog | None = None,
        loader: SkillLoader | None = None,
    ):
        self._metadata: MutableMapping[str, Any] = metadata if metadata is not None else {}
        self._catalog = catalog if catalog is not None else SkillCatalog()
        self._loader = loader

    # --- 组装给元信息 -----------------------------------------------------

    @property
    def catalog(self) -> SkillCatalog:
        """当前的可用清单。

        每次读都**顺手重扫一次**（有 loader 时）—— 见类 docstring：清单和"能不能加载"
        必须是同一份事实，否则模型会看得见一个加载不了的技能。
        """
        if self._loader is not None:
            self._catalog = self._loader.reload()
        return self._catalog

    def stored(self) -> list[dict[str, str]]:
        """已加载技能的**指针**（name + digest，没有正文）。

        单独一个方法是为了让测试和 CLI 能直接断言写入的形状，不必去翻 `_metadata`
        这个私有属性 —— 私有属性被外部读一次，就等于把内部结构变成了事实上的公开
        接口（`ToolRegistry.all()` 的存在是同一个理由）。
        """
        return load_entries(self._metadata)

    def __repr__(self) -> str:  # pragma: no cover - 只为调试时看得清
        names = ", ".join(skill.name for skill in self._catalog.skills) or "（无）"
        return f"SkillBoard(可用={names}, 已加载={active_names(self._metadata)})"

    # --- 工具的那个调用口 --------------------------------------------------

    def __call__(self, name: str = "", unload: bool = False) -> ToolResult:
        if unload:
            return self.unload()
        if name.strip() == "":
            return self.list_skills()
        return self.load(name.strip())

    # --- 三条出口 ---------------------------------------------------------
    #
    # 三个方法都是公开的（不是 _load / _unload 那种私有名）：工具的那个 `__call__` 只是
    # 按参数分派，而"到底发生了什么"是**三条独立的行为**，测试和将来的 Web 面板都该能
    # 单独调用、单独断言，不必绕着参数组合去猜。

    def list_skills(self) -> ToolResult:
        """空参：列清单。

        清单本身**不写进 metadata** —— 它是"有哪些"，不是"用了哪些"。写进去的话，
        恢复会话时会把它当成"已加载"，而那份列表在下一轮就会被渲染成正文块。
        """
        if not self.catalog.skills:
            return ToolResult(
                "现在没有任何可用技能。按你默认的做法完成任务。",
                {"skill_action": "catalog", "skill_count": 0},
            )

        lines = [f"- {skill.name}：{skill.description}" for skill in self.catalog.skills]
        hint = (
            "要用哪个就把它的名字传给 load_skill；它的完整步骤会从下一轮开始生效。"
            f"同时最多生效 {MAX_ACTIVE_SKILLS} 个。"
        )
        return ToolResult(
            "\n".join(["可用技能：", *lines, "", hint]),
            {"skill_action": "catalog", "skill_count": len(self.catalog.skills)},
        )

    def unload(self) -> ToolResult:
        before = active_names(self._metadata)
        self._metadata[SKILLS_KEY] = []
        if not before:
            return ToolResult("本来就没有加载任何技能。", {"skill_action": "unload"})
        return ToolResult(
            f"已卸载技能：{'、'.join(before)}。它们的步骤此后不再出现，"
            f"按你默认的做法继续。",
            {"skill_action": "unload", "skill_unloaded": ",".join(before)},
        )

    def load(self, name: str) -> ToolResult:
        skill = self.catalog.by_name(name)

        if skill is None:
            # 返回文本、不抛异常：跟着 edit_file / grep / shell 那条规矩走 ——
            # 名字打错是模型读着一句话就能自己改对的事，抛出去只会被记成"工具故障"，
            # 而它恰恰拿不到那句最该看到的话（现在的清单）。
            known = "、".join(item.name for item in self.catalog.skills) or "（没有）"
            return ToolResult(
                f"没有这个技能：{name}。当前可用的是：{known}。"
                f"用 load_skill 不带参数可以看到完整的清单和说明。",
                {"skill_action": "unknown", "skill": name},
            )

        active = active_names(self._metadata)
        if name in active:
            # 幂等：已经加载过就说清楚，**不重复写 metadata**。但仍然正常返回 ——
            # 模型可能就是没看清而重调了一次，回一句"已在生效"比报错有用。
            return ToolResult(
                f"技能 {name} 已经在生效了，不必重复加载。按它的步骤继续。",
                {"skill_action": "already", "skill": name},
            )

        if len(active) >= MAX_ACTIVE_SKILLS:
            # 不挤掉旧的：悄悄卸载一个已经生效的技能，等于伪造模型的主张 ——
            # 它下一轮会按自己"记得"的技能做，而那份已经不在载荷里了
            # （tools/builtin/todo.py 里"活性约束只提醒，不代填"是同一条原则）。
            return ToolResult(
                f"现在已经有 {len(active)} 个技能在生效（{'、'.join(active)}），"
                f"达到了上限 {MAX_ACTIVE_SKILLS}。先用 load_skill(unload=true) 卸掉"
                f"不再需要的，再加载 {name}。",
                {"skill_action": "full", "skill": name, "skill_active": len(active)},
            )

        self._metadata[SKILLS_KEY] = [
            *self.stored(),
            {"name": skill.name, "digest": skill.digest},
        ]

        return ToolResult(
            f"已加载技能 {name}。它的完整步骤会出现在此后每一轮对话的末尾，"
            f"按它的步骤做，做完再回到你默认的做法。需要看它同目录下的其它文件时，"
            f"用 read_file 读那个路径。",
            {
                "skill_action": "load",
                "skill": skill.name,
                # 这几项**只给审计**：正文已经在对话里了，长度和工具清单是事后回答
                # "这次会话为技能付了多少上下文""技能声明的限制是不是真的"所需要的。
                "skill_chars": skill.body_chars,
                "skill_allowed_tools": ",".join(skill.allowed_tools),
                "skill_note_chars": note_chars(self._metadata, self.catalog),
            },
        )
