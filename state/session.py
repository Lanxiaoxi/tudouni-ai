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

**系统提示词的静态部分住在 prompts/system.zh.md，不再是一个字符串常量。**

  1. 它是这个项目里改动最频繁、也最需要 diff 的文本 —— 混在 Python 里，一次改动
     看不出「哪一句被删了、哪一句被换掉」，而这恰恰是提示词维护最要紧的信息。
  2. 它必须逐字节稳定才能命中 provider 的前缀缓存。放在文件里，就有一份可以直接
     比对的字节序列。

那个文件里**不要加任何注释**：整份内容会原样发给模型、按 token 计费。给维护者
看的说明写在这里。
"""

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 提示词文件的位置：state/session.py → 项目根 → prompts/
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.zh.md"


def load_system_prompt(path: Path | None = None) -> str:
    """读静态系统提示词。

    **刻意不在 import 时读**（比如写成 `SYSTEM_PROMPT = load_system_prompt()`）。
    那会让 `--list` / `--history` / `--audit` 这些根本不碰模型的子命令也依赖这个
    文件存在 —— 而 cli.py 特意把它们排在配置检查之前，为的就是「没配密钥也能查
    历史」。少一个提示词文件不该让查历史也失效。

    也不缓存：一个进程只新建一次会话，重新读盘的代价可以忽略，换来的是改完提示词
    不用重启就能看到效果。
    """
    file = path or SYSTEM_PROMPT_PATH
    if not file.exists():
        # 裸的 FileNotFoundError 只给一个路径；这里多给一句「那是什么、该在哪」。
        raise FileNotFoundError(
            f"系统提示词文件不存在：{file}\n"
            f"它不是一个可选文件 —— agent 每轮都要把它发给模型。"
        )
    return file.read_text(encoding="utf-8").strip()


def build_system_message() -> str:
    """静态提示词 + 这段环境说明，拼成一条 system 消息。

    **切分点是「变不变」，不是语义。** 静态部分逐字节稳定，因而是整段对话里唯一
    能命中 provider 前缀缓存的部分；动态内容一旦混进前缀，它后面的所有 token 每轮
    都按未命中计费 —— 那是官方价里贵约 50 倍的那一档。

    目前动态部分只剩一行。这不是没做完，是刻意留白的结论 —— 见 _env_block。
    """
    return f"{load_system_prompt()}\n\n{_env_block()}"


def _env_block() -> str:
    """这次运行的环境事实：现在只剩操作系统。

    **工作区路径是被特意排除的**，尽管 Claude Code 的 <env> 和 Codex 的
    <environment_context> 都会把 cwd 告诉模型。三条理由：

      1. **模型不需要它。** 四个工具的路径参数都按相对路径解析（绝对路径虽然也能
         通，但模型从不需要写）；list_files(".") 已经把工作区里有什么列全了。
      2. **它是唯一会过期的一行。** .sessions/ 跟着项目走，项目一改路径，旧会话里
         记的就是旧路径；模型据此拼出的绝对路径会被 safe_path 拒掉，而报错是
         "Path escapes workspace" —— 一个看起来像 bug 的正常行为。
      3. **它有更该去的地方。** 等 shell 工具落地，"我在哪"由 pwd 现查：永远新鲜，
         且不占提示词。信息该放在模型第一次需要它的那个工具结果里。

    也不放日期，理由是第 2 条的同一条：写进系统消息的时间在恢复会话时就是错的，
    而 get_current_time 本来就能现取。
    """
    return f"## 运行环境\n- 操作系统：{platform.system()}"


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

        同样地，**恢复已有会话不会重算这条消息**：改 prompts/system.zh.md 只影响
        此后新建的会话，已经落盘的会话保留它创建时的那一份。
        """
        return cls(
            session_id=session_id,
            messages=[{"role": "system", "content": build_system_message()}],
        )

    def step_count(self) -> int:
        """本会话累计执行了多少步。

        这是派生值，不是存储字段：每一步对应一条 assistant 消息，数一下就有。
        """
        return sum(1 for m in self.messages if m.get("role") == "assistant")
