"""todo_write 工具：模型自己维护的任务列表。

**它不是任务编排。** 这里的列表是模型自己编辑的草稿纸 —— 让它在多步任务里记得住"还剩
什么、现在在做哪条"，顺带让人看得见进度。运行时不按它调度、不按它并行、也不信它的
`completed`：那些是另一件事（README 的「尚未实现」里那条"任务编排、子 Agent"），量级
差着好几个数量级。Claude Code 自己就是两套并存：内存里那套（TodoWrite）给模型看，
文件系统里那套（带 blocks / blockedBy / 认领锁）才是给调度用的。

三条设计决定：

1. **每次传完整列表，直接替换。** 增量更新要求模型对"上一版长什么样"记得准，而计划天生
   在变 —— 对不上就失败或者错配（`edit_file` 的 old_string 是给**稳定**的东西用的）。
   全量覆写是幂等的：丢一次、重一次、乱序一次都不会坏。代价是每次重发整份，所以列表
   必须短。
2. **全部完成时清空。** 一份"全是对勾"的列表此后每轮都要重发一遍，而它已经不含任何信息。
   这不是丢记录：每一次调用的参数（含全完成那次）都在会话历史里，那一份才是证据。
3. **活性约束只提醒，不代填。** "还有活就必须有一条进行中"这条规则写在工具描述里；模型
   违反时这里只在结果里说一句，**不替它把某条改成进行中** —— 那等于伪造模型的主张，
   而它下一轮会读到自己"说过"的状态并当真。
"""

from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

from pydantic import Field

from agent_runtime import i18n

from ..tool import ToolArgs, ToolResult

# 会话 metadata 里存任务列表的那个键。**读写两侧共用这一个常量**（写入在 TodoBoard，
# 读出在 todo_note / progress_line）—— 两边各写一份字面量，漂开一个字符就是"列表凭空
# 消失"，而且不会有任何报错。
TODOS_KEY = "todos"

PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"

# 状态写死成字面量而不是自由字符串：schema 里因此带上 enum，模型不可能打出 inprogress
# 这种拼法然后被当成第三种状态存下来。
Status = Literal["pending", "in_progress", "completed"]

_STATUS_LABEL: dict[str, str] = {
    PENDING: "待办",
    IN_PROGRESS: "进行中",
    COMPLETED: "已完成",
}


class TodoItem(ToolArgs):
    # 刻意不写 docstring：Pydantic 会把它当作 $defs 里的 description 一起发给模型
    # （ToolArgs 那个钩子剥的是顶层那一个）。给维护者看的话写在模块 docstring 里。
    content: str = Field(
        min_length=1,
        description="这一步要做什么，一句话（祈使句，不要写成段落）",
    )
    status: Status = Field(
        default=PENDING,
        description="pending 待办 / in_progress 正在做 / completed 已完成",
    )


class TodoArgs(ToolArgs):
    """todo_write 的参数。

    `todos` **必填、没有默认值**。这是有意的：默认成空列表的话，模型少给一个字段
    （或者字段名打错）就会被解析成"清空任务列表" —— 一次静默的数据丢失，而且看起来
    完全正常。
    """

    todos: list[TodoItem] = Field(
        description="完整的新列表 —— 它**替换**上一次的整份列表，不是增量。"
                    "传空数组表示清空（这个任务不再需要列表时就这么传）",
    )


def load(metadata: Mapping[str, Any]) -> list[dict[str, str]]:
    """从会话 metadata 里取出任务列表；形状不对就当"没有列表"。

    **一条不对就整份丢掉，不是跳过那一条。** 跳过会报出一份"看起来完整、其实缺了几条"
    的列表，而模型会把它当成全部 —— 那比没有列表更坏（它会据此以为某件事还没做，或者
    以为做完了）。这和 `--audit` 那边"缺字段显示成 ?"是同一个取向：宁可少说，不要错说。

    防御性读的三条理由都来自这个项目的既有事实：会话文件会被复制、拼接、手工编辑
    （和审计日志一样）；`JsonSessionStore` 只容忍"多出来的键"，不容忍"同一个键的含义
    变了"；而一个坏字段绝不该让整个会话再也发不出请求。
    """
    raw = metadata.get(TODOS_KEY)
    if not isinstance(raw, list):
        return []

    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            return []
        content, status = entry.get("content"), entry.get("status")
        if not isinstance(content, str) or status not in _STATUS_LABEL:
            return []
        items.append({"content": content, "status": status})
    return items


def _clip(text: str, limit: int) -> str:
    """压成单行并截断 —— 给人看的那一行不能因为一条任务的正文就把终端刷掉。"""
    flat = text.replace("\n", " ")
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def todo_note(metadata: Mapping[str, Any]) -> str | None:
    """把当前任务列表渲染成**拼进这次请求末尾**的一段；没有列表时返回 None。

    模型每轮都会看到它，而不是去翻历史找最近那一版：N 次更新在历史里就是 N 个过期副本，
    靠"最近一条看起来对"推断现状是不可靠的；更要紧的是它得出现在模型**要决策的那一刻**
    （载荷尾部），而不是几十步之前。

    它**不进 `session.messages`** —— 理由和 `_budget_reminder` 完全一样（见 agent.py）：
    逐轮变化的东西不该被持久化，也不该去稀释"一条 assistant = 一步"那个派生规则。
    """
    items = load(metadata)
    if not items:
        return None
    lines = [
        f"- [{_STATUS_LABEL[item['status']]}] {item['content']}"
        for item in items
    ]
    return "\n".join(["## 当前任务（你自己维护的列表）", *lines])


def progress_line(metadata: Mapping[str, Any], limit: int = 40) -> str | None:
    """一行进度，**给人看的**：`2/5 完成，当前：写测试`。

    和 `todo_note` 分开，是因为两者的读者不同：模型要的是"还剩什么、现在做哪条"，
    而人只要一眼看得出做到哪了。合成一份的话，两边都得为对方多付 token。
    """
    items = load(metadata)
    if not items:
        return None

    done = sum(1 for item in items if item["status"] == COMPLETED)
    current = next(
        (item["content"] for item in items if item["status"] == IN_PROGRESS), None
    )
    text = i18n.t("todo.progress", done=done, total=len(items))
    if current:
        text += i18n.t("todo.progress.current", what=_clip(current, limit))
    return text


def _all_completed(items: list[TodoItem]) -> bool:
    return bool(items) and all(item.status == COMPLETED for item in items)


def _counts(stored: list[dict[str, str]]) -> dict[str, int]:
    """留给审计的那几个数（**模型的主张，不是事实**）。

    记它们只为了回答一个事后问题："这个功能到底有没有被用起来、有没有用对"。数的是
    **存下来还留着的那份**（全完成时就是 0），因为"还剩几项"是回收站里唯一推不出来的
    东西 —— 模型发了什么，`tool_call` 那条事件的参数里已经有一份完整的。
    """
    return {
        "todos_total": len(stored),
        "todos_in_progress": sum(1 for i in stored if i["status"] == IN_PROGRESS),
        "todos_completed": sum(1 for i in stored if i["status"] == COMPLETED),
    }


def _ack(items: list[TodoItem], stored: list[dict[str, str]], sent: int) -> str:
    """回给模型的一句话。

    **不回显整份列表** —— 它刚刚才在 assistant 消息里发过一遍，而工具结果此后每一轮
    都要重发：抄一遍就是永久地把同一份内容在历史里存两份。
    """
    if sent == 0:
        return "任务列表已清空。"
    if not stored:
        return "任务列表已清空（全部完成）。"

    parts = [
        f"{_STATUS_LABEL[status]} {sum(1 for i in items if i.status == status)} 项"
        for status in (IN_PROGRESS, PENDING, COMPLETED)
        if any(i.status == status for i in items)
    ]
    text = f"任务列表已更新：{len(items)} 项（{'、'.join(parts)}）。"

    # 活性约束在这里只当提醒：见模块 docstring 第 3 条。
    if not any(item.status == IN_PROGRESS for item in items):
        text += (
            "\n提醒：列表里还有未完成的任务，但没有任何一项标为进行中 —— "
            "把当前正在做的那项改成 in_progress。"
        )
    return text


class TodoBoard:
    """任务列表的读写口。**它不碰文件，只碰会话的 metadata。**

    为什么要一个对象：列表是**按会话的状态**，不是装配期就定下来的能力。所以它只能在
    会话定下来之后才造得出来（main.py 里 `resolve_session` 恰好就在注册工具之前），
    然后像 `workspace` / `questioner` 那样注进注册表。工具层的写法因此和其他协作方
    完全一致，`Tool.handler` 的契约一个字都不用动。

    传进来的是 `session.metadata` 这个**活字典**（不是副本）：写进去的东西会跟着会话
    一起落盘，下个进程恢复会话时还在 —— "列表比一次 run 活得久"就靠这个。

    `metadata=None` 表示"没有接到任何会话上"（测试里裸调 `create_tool_registry`、
    只想看看注册表的情形）：更新照收，只是没人看得见。CLI 那条路两者都装配（见
    main.py），所以它不会在生产里静默失效。
    """

    def __init__(self, metadata: MutableMapping[str, Any] | None = None):
        self._metadata: MutableMapping[str, Any] = metadata if metadata is not None else {}

    def __call__(self, todos: list[dict[str, Any]]) -> ToolResult:
        items = [TodoItem(**item) for item in todos]

        # 全完成 → 存空列表（模块 docstring 第 2 条）。
        stored = [] if _all_completed(items) else [item.model_dump() for item in items]
        self._metadata[TODOS_KEY] = stored

        return ToolResult(
            text=_ack(items, stored, sent=len(items)),
            audit=_counts(stored),
        )
