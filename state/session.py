"""会话。

第 6 阶段之前，messages 是 Agent.run() 里的局部变量 —— 一次调用结束就消失，
所以 agent 是"金鱼记忆"：两次 run() 之间完全不记得聊过什么。

抽到这里之后，它才可能跨调用、跨进程存活，从而支持多轮对话、查看历史和
中断恢复。

**为什么叫 Session 而不是 AgentState。** 原来那个名字暗示「这是 Agent 的状态」，
也就是暗示它属于某个 Agent。但把会话从 Agent 的构造函数里搬出来之后，事实正好
相反：Agent 是无会话的、可复用的能力组合，而 Session 是独立存在的数据，它比
任何 Agent 对象活得都久（可以跨越进程）。名字里带着 Agent 就成了代码里的一句
假话 —— 名字编码了耦合，解耦了就该改名。
"""

from dataclasses import dataclass, field
import re
from typing import Any


SYSTEM_PROMPT = "你是一个AI助手。需要读取或修改文件时，必须使用工具。"

# session_id 会被拿去拼文件名（会话状态文件、审计日志），所以格式必须受限：
# 只允许字母、数字、下划线、连字符。实测 "../../evil" 能把文件写到目录外面去。
#
# 放在这里而不是各个 store 里，是因为它描述的是「什么算合法会话 id」—— 那是
# 会话自己的属性。两份校验规则早晚会走岔，然后一个组件接受、另一个拒绝。
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_valid_session_id(session_id: str) -> bool:
    """判断一个字符串能不能安全地当 session_id 用（也就能不能拿去拼文件名）。"""
    return bool(_SAFE_SESSION_ID.fullmatch(session_id))


@dataclass
class Session:
    """一次会话的全部「事实」。

    只放三类东西：

      - session_id  它是谁
      - messages    发生过什么（唯一的真相来源）
      - metadata    其它需要跨回合留存的杂项

    **刻意不放的：步数、错误汇总。** 这两样都能从 messages 数出来。存成字段就
    有了两份，早晚不一致 —— 而且步数一旦持久化，语义会从"这一轮走了几步"悄悄
    变成"这个会话一共走了几步"，于是会话跑到一定轮次后会莫名其妙地"超过最大
    执行步数"，一步都不肯走。
    """

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, session_id: str) -> "Session":
        """新建会话。

        系统提示词只在这里写一次。如果把它写在 run() 里，恢复会话时每个回合都会
        再插一条 system 消息，消息条数和角色序列都会变样。
        """
        return cls(
            session_id=session_id,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        )

    def step_count(self) -> int:
        """本会话累计执行了多少步。

        这是派生值，不是存储字段：每一步对应一条 assistant 消息，数一下就有。
        """
        return sum(1 for m in self.messages if m.get("role") == "assistant")
