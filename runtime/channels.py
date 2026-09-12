"""人机通道的"一包"。

`Agent` 构造时就需要两个注入的协作方：`asker`（审批）和 `questioner`（提问）。
这个文件把它们**打成一包**，理由是第零期抽 Runtime 时撞上的一个顺序问题：

    Agent 构造时就要 asker / questioner
    而协议版的那两个实现需要 Runtime 才能工作（它们要发消息、等回应）
    Runtime 又要构造 Agent
    → 环

把通道打成一包、由调用方先造好，环就断了。但这里有一个**细节让"先造好"不能是
简单的"两个现成对象"**：`asker` 需要 `memory` 和 `trust_group`，而那两个都是
`open_runtime()` 内部造出来的（memory 要读权限文件、trust_group 要等 MCP 连上）。
所以 `Channels` 拿的不是一个现成的 asker，而是一个**工厂**：

    Channels(
        questioner=...,                        # 现成的：它什么都不需要
        asker_factory=lambda memory, trust: …  # 迟一步：那两样装配期才有
    )

`open_runtime()` 造完 memory / trust_group 之后调这个工厂，把 asker 做出来再交给
Agent。这是"只用不造"和"必须有东西可用"之间最小的那个妥协。

**类型契约不在这个文件里** —— `ApprovalAsker` 住在 `security/asker.py`、`Questioner`
住在 `tools/builtin/ask.py`。这里只是接线，不定义契约，也不 import 具体实现模块
（那些 import 放在函数里：`--list` 这类子命令不该拖进 ask 模块）。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runtime.security.asker import ApprovalAsker
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.tools.builtin.ask import Questioner
from agent_runtime.tools.tool import Tool

# 审批里那个 a（信任一整个 MCP server 的全部工具）的查询口：给工具名，回答一个
# TrustGroup 或 None。返回值标成 Any 而不是 TrustGroup，是为了不在这个文件里
# import security.asker 的类型 —— 少一个 import，多一处不精确，值得。
TrustGroupLookup = Callable[[str], Any]

# 造 asker 的工厂：给它装配期才有的那两样，还一个 asker。
AskerFactory = Callable[[ApprovalMemory, "TrustGroupLookup | None"], ApprovalAsker]

# 造 memory 的工厂：给它权限配置，还一份"人说过别再问"的记忆。
#
# 为什么 memory 也要一份工厂：装配层要**先**有 memory 才能让协议版 asker 回答
# "这次能不能按 t"，而 memory 的构造依赖权限配置（那是装配层读的）。把"怎么造"
# 作为一份可注入的东西交出去，装配层和协议层就都能拿到**同一个**造法，
# 而不必各写一份。
MemoryFactory = Callable[[Any], ApprovalMemory]


@dataclass(frozen=True, slots=True)
class Channels:
    """装好的人机通道：提问是现成的，审批和记忆是迟一步的。"""

    questioner: Questioner
    asker_factory: AskerFactory
    # `None` = 用 `memory_from_permissions`（唯一的默认造法）。测试可以换成纯内存的，
    # 那样按 t 不会碰到真的 permissions.json。
    memory_factory: MemoryFactory | None = None


def resolve_memory_factory(channels: "Channels") -> MemoryFactory:
    """把 `Channels` 上那份可选的工厂变成一定有的。"""
    return channels.memory_factory or memory_from_permissions


def cli_channels(*, unavailable: bool = False) -> Channels:
    """CLI 那一对：审批走终端、提问走终端。

    `unavailable=True`（`--autopilot`）时**两条通道都归它管**：审批直接放行、
    提问拿到"没有人回答"。--autopilot 说的正是"这一轮没有人可问"这件事本身，
    所以它同时管住两条人机通道 —— 而拒绝名单、工作区边界、控制面写入都不归它管
    （那些是"不许做"，不是"要不要问"）。
    """
    if unavailable:
        return Channels(
            questioner=_unavailable_questioner(),
            # autopilot 的审批放行不在 asker 里做：gate 会先看到 autopilot 并直接
            # 放行（audit 里记 outcome=autopilot），asker 根本不会被调用。
            # 但为了"两条通道都归它管"这句话在类型上也成立，这里给一个恒真的实现。
            asker_factory=lambda memory, trust: _always_allow,
        )

    return Channels(
        questioner=_cli_questioner(),
        asker_factory=_cli_asker_factory,
    )


def fixed_channels(asker: ApprovalAsker, questioner: Questioner) -> Channels:
    """两个都现成的一对。测试和将来的嵌入式用法走这里。"""
    return Channels(
        questioner=questioner,
        asker_factory=lambda memory, trust: asker,
        memory_factory=_default_memory,
    )


def memory_from_permissions(permissions) -> ApprovalMemory:
    """按权限配置造一份 `ApprovalMemory`，并把"按 t 记住"落盘到 permissions.json。

    它是**装配的知识**（落在哪个文件、label 写什么），所以住在 `runtime/` 里。

    函数内 import `ApprovalMemory` / `save_approvals` 是有意的：这个模块被
    `protocol/` 和 `frontends/` 引用，而它们大多数路径不需要碰权限文件。
    """
    from agent_runtime.runtime.config import PERMISSION_FILE, save_approvals
    from agent_runtime.security.memory import ApprovalMemory

    return ApprovalMemory(
        permissions.auto_approve_tools,
        on_change=lambda granted, prefixes: save_approvals(
            PERMISSION_FILE, tools=granted, prefixes=prefixes
        ),
        prefixes=permissions.shell_allow,
        label=PERMISSION_FILE.name,
    )


def _default_memory(permissions) -> ApprovalMemory:
    return memory_from_permissions(permissions)


# --- 具体实现（函数内 import：不装配就不付这笔加载费） -------------------------

def _cli_asker_factory(
    memory: ApprovalMemory, trust_group: "TrustGroupLookup | None"
) -> ApprovalAsker:
    from agent_runtime.security.asker import cli_asker

    def asker(tool: Tool, arguments: Mapping[str, Any]) -> bool:
        return cli_asker(tool, arguments, memory=memory, trust_group=trust_group)

    return asker


def _cli_questioner() -> Questioner:
    from agent_runtime.tools.builtin.ask import cli_questioner

    return cli_questioner


def _unavailable_questioner() -> Questioner:
    from agent_runtime.tools.builtin.ask import unavailable_questioner

    return unavailable_questioner


def _always_allow(tool: Tool, arguments: Mapping[str, Any]) -> bool:
    """恒真。**只在 autopilot 那条路上用**，而且正常情况下根本不会被调用
    （gate 先看到 autopilot 就直接放行了，审计里记的是 outcome=autopilot 而不是
    approved —— 这正是它和"有人按了 y"必须分开记的理由）。"""
    return True
