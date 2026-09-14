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

**这个会话所在的工作区还贡献第三段：它的 AGENT.md。** 那一段由
`state/agents_md.py` 负责读（连同"读没读成、有没有被截断"那份事实），这里只负责
把它拼到**最末尾**并记进 `metadata`。为什么不放进 `prompts/`：那两份文件的主人
不是同一个 —— `prompts/system.zh.md` 是这个项目的作者写的、描述 agent 该怎么做事，
AGENT.md 是**被操作的那个工作区**的维护者写的、描述那个工作区是什么样。混在一起
之后，"改哪一份能让所有工作区都受益"就没有答案了。
"""

import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime import paths
from agent_runtime.state import agents_md


# 提示词文件的位置。**它跟着代码走，不跟着工作区走** —— 所以取的是 `package_dir()`
# 而不是 `workspace_dir()`：这份提示词是这个项目的作者写的、和代码同版本，装成命令
# 之后它在 site-packages 里。工作区那边贡献的是另一份文本（AGENT.md），两者的主人
# 不是同一个人，见本模块 docstring 末尾那一段。
PROMPTS_DIR = paths.package_dir() / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.zh.md"

# 工作区的默认位置。**权威在 `paths.workspace_dir()`** —— 装配会把它显式传给
# `Session.new()`，这里留着只是为了"不传工作区的新会话"有一个说得通的地方可查。
# 名字在这里再出口一次，是为了让测试能一处 monkeypatch 掉它（见 tests/conftest.py）。
WORKSPACE = agents_md.WORKSPACE


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


def build_system_message(
    *, text: str = "", footer: str = ""
) -> str:
    """静态提示词 + 环境说明 + 工作区那份 AGENT.md，拼成一条 system 消息。

    **切分点是「变不变」，不是语义。** 静态部分逐字节稳定，因而是整段对话里唯一
    能命中 provider 前缀缓存的部分；动态内容一旦混进前缀，它后面的所有 token 每轮
    都按未命中计费 —— 那是官方价里贵约 50 倍的那一档。

    所以顺序是**从最不变到最易变**：

      1. `prompts/system.zh.md` —— 只随这个项目的发版变；
      2. `## 运行环境` —— 一个进程内不变（见 `_env_block`）；
      3. `## 项目说明（AGENT.md）` —— **本工作区唯一会被人手改的那一段**，所以它
         排在最后：它一动，前面那些字节照旧命中，被作废的只有尾巴。

    两个参数是**已经读好的正文**，不是工作区路径 —— 读盘（连带"没读成/被截断"
    那份事实）是 `agents_md.load_agent_md` 的事，而那份事实还要同时交给
    `Runtime.notices()`。让这个函数自己再去读一遍，就是同一份事实的第二个来源。
    """
    return f"{load_system_prompt()}\n\n{_env_block()}{agents_md.text_block(text, footer=footer)}"


def _env_block() -> str:
    """这次运行的环境事实：现在只剩操作系统。

    **工作区路径是被特意排除的**，尽管 Claude Code 的 <env> 和 Codex 的
    <environment_context> 都会把 cwd 告诉模型。三条理由：

      1. **模型不需要它。** 四个工具的路径参数都按相对路径解析（绝对路径虽然也能
         通，但模型从不需要写）；list_files(".") 已经把工作区里有什么列全了。
      2. **它是唯一会过期的一行。** .tudouni/ 跟着项目走，项目一改路径，旧会话里
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
    def new(cls, session_id: str, workspace: str | Path | None = None) -> "Session":
        """新建会话。

        系统提示词只在这里写一次。如果把它写在 run() 里，恢复会话时每个回合都会
        再插一条 system 消息，消息条数和角色序列都会变样。

        同样地，**恢复已有会话不会重算这条消息**：改 prompts/system.zh.md 只影响
        此后新建的会话，已经落盘的会话保留它创建时的那一份。**工作区的 AGENT.md
        走完全同一条规矩** —— 改它也一样只对新会话生效，理由不是"实现简单"，而是
        两件事都是"这次会话的说明书"，而说明书中途换版会让同一段对话里前后两半
        依据不同的说明办事，事后完全看不出来。

        ## 工作区为什么是参数

        `workspace` 不给默认值就查不到 AGENT.md（那是它的容身之处），而给一个
        "模块级默认值"是有代价的：那份默认值只能来自包目录，于是**测试会看开发机
        的脸色** —— 谁的包里恰好躺着一份 AGENT.md，谁的断言就红。调用方
        （`composition.resolve_session`）显式传 `project_dir()`，测试传临时目录。

        ## 那份读盘报告为什么进 metadata

        见 `agents_md.Report`：它是"这个会话的 system 消息里到底注入了什么"唯一的
        说法，而这件事在**恢复会话、甚至换一个前端**之后仍然要被说得对。

        ## 为什么记一个 `created_at`

        `metadata` 里那一条是**建这个会话的时刻**（epoch 秒），它服务的唯一一件事是
        "把会话按创建时间排出来"（`--list` 和 TUI 的选会话面板）。

        为什么不靠别的东西推：

          * **`session_id` 不总是一个时间戳。** 自动分配的 id 是，但 `--session demo`
            这种自己起的名字不是 —— 按 id 排序会把 `demo` 排到 `20250101-…` 后面，
            而它可能是昨天建的；
          * **会话文件的 mtime 不是创建时间。** 它每次 checkpoint 都会变，所以那个
            时间说的是"最后一次聊"，不是"什么时候建的"。用 mtime 排的话，切回一个
            老会话说一句话，它就会跳到列表最上面。

        它**不进 session.messages**（那是发给模型的东西）—— 这一条只是本地的书签。
        老会话文件里没有这两个键，`composition._created_key` 对它们有另一条退路。
        """
        # `workspace or WORKSPACE` 而不是直接把 None 传下去：这一层的默认值就是
        # `WORKSPACE`（模块级的那个名字），让 `agents_md` 自己再兜一次会让"默认工作区
        # 到底是哪个"出现第二种解释 —— 而测试只 patch 这里那一个名字。
        text, report = agents_md.load_agent_md(workspace or WORKSPACE)
        footer = ""
        if report.loaded and report.loaded[0].truncated:
            footer = agents_md.truncation_footer(report.loaded[0])
        return cls(
            session_id=session_id,
            messages=[{"role": "system",
                       "content": build_system_message(text=text, footer=footer)}],
            metadata={
                "created_at": time.time(),
                agents_md.SESSION_KEY: agents_md.to_block(report),
            },
        )

    def step_count(self) -> int:
        """本会话累计执行了多少步。

        这是派生值，不是存储字段：每一步对应一条 assistant 消息，数一下就有。
        """
        return sum(1 for m in self.messages if m.get("role") == "assistant")
