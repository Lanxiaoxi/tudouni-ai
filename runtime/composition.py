"""装配：把内核变成一个能跑的东西。

**这个模块的存在理由，是一段可以被说清的历史。** 在此之前，装配全部住在 `main.py`
里（465 行），而它同时干着两类事：

  1. **真正的运行时职责** —— 起 MCP server 子进程并在退出时收掉、把加载的技能和任务
     列表渲染成载荷尾部那段、把"哪个工具属于哪个 server"接成审批里的 `a`、
     读取策略与密钥；
  2. **呈现** —— 横幅、`[权限]` / `[技能]` / `[任务]` / `[MCP]` 那几行、已注册工具的清单。

第 1 类必须能被**每一个**前端复用（CLI、TUI、将来的 Web），第 2 类必须能被替换。
混在一起的后果是具体的：协议层要复用装配，就只能去 import 整个 `main.py`，
而 `main.py` 又 import 了 CLI —— 环，而且这个环要等到"加一个新前端"时才暴露。

所以这个文件只保留第 1 类。第 2 类变成 `Runtime.notices()` 返回的数据（见 `Notice`）。

**两阶段装配，不是一步。** `boot()` 只做"不需要模型、也不需要人机通道"的那一段
（它服务于 `--list` / `--skills` 这类子命令：没配密钥也要能查历史）；
`open_runtime()` 才造模型、工具、Agent，并且**接受**一对人机通道而不是创建它们 ——
那个顺序问题写在 `runtime/channels.py` 的 docstring 里。

**两条刻意的例外要说明**：会话身份（"新会话 X" / "继续会话 X"）和"审计日志写到哪"
**不在 `notices()` 里** —— 前者来自会话选择（`resolve_session`），后者的路径是前端
自己就能拼的（`logs.directory` + `session_id`）。把它们塞进 `notices()` 会让
"会话是谁"在装配层出现第二份。
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx

from agent_runtime.agents import Agent
from agent_runtime.audit import JsonlSink
from agent_runtime.models import OpenAICompatibleModel
from agent_runtime.runtime.channels import Channels, resolve_memory_factory
from agent_runtime.runtime.config import (
    DEFAULT_AUTO_APPROVE,
    MCP_FILE,
    PERMISSION_FILE,
    ConfigError,
    McpConfig,
    ModelConfig,
    PermissionConfig,
    WebConfig,
    save_approvals,
)
from agent_runtime.security import ApprovalMemory, PermissionPolicy, TrustGroup
from agent_runtime.security.commands import command_parameter, format_rule
from agent_runtime.skills import (
    RUNTIME_DIR_NAME,
    SkillCatalog,
    SkillLoader,
    active_line,
    catalog_part,
    skill_note,
)
from agent_runtime.process import job_object_problem
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state import agents_md
from agent_runtime.state import catalog
from agent_runtime.state import model as model_state
from agent_runtime.state import reasoning
from agent_runtime.state.session import is_valid_session_id
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.grep import host_triple, rg_binary
from agent_runtime.tools.builtin.jobs import JOBS_DIR_NAME, JobBoard, job_note, jobs_dir
from agent_runtime.tools.builtin.todo import TodoBoard, progress_line, todo_note
from agent_runtime.tools.builtin.webfetch import USER_AGENT, WebFetch
from agent_runtime.tools.builtin.websearch import TavilySearch, WebSearch
from agent_runtime.tools.mcp import McpToolset


# --- 呈现：这些是数据，不是打印 -------------------------------------------------

# 一个回合最多几步。默认 **80**：实测把这个项目最典型的长任务（"参考现有实现加一个
# 工具"）跑到收尾需要 19 步，而 20 只剩最后一步的余量 —— 任何一次返工都会撞墙。
#
# 它在这里而不是藏在 `Agent.run` 的签名里，是因为 `init` 要把它发给前端：
# 前端要显示"第 3/80 步"，而它显示一个和实际预算不同的数比不显示更坏。
DEFAULT_MAX_STEPS = 80

class Notice(NamedTuple):
    """一条"关于这次运行的说明"。

    `stream` 记的是**它在老 CLI 里走哪个流**，这**不是**可以忽略的细节：stdout 上
    那条（已注册工具清单）必须继续走 stdout，否则 README 那句"`uv run main.py >
    对话.txt` 拿到的是干净的答案"就让位给了一段纯文本。

    为什么由装配层记这个：哪个流是**历史契约**（README 写着的），不是某个前端的
    审美。新的前端可以无视 `stream` 自己决定（TUI 会渲染成面板），但 ASCII 那一路
    必须照旧 —— 这是第零期"输出逐字节不变"那条验收的落点。

    `code` / `level` / `is_default` 是给**跨进程的**消费者用的（第一期加的）：

      * `code` —— 机器认的标识（`"permissions"` / `"skills"` / `"context"` …）。
        前端要按类别分组、折叠、或只显示一部分时，靠它，**不靠解析 `text`**；
      * `level` —— `info` / `warn`。`warn` 那几条（autopilot、被忽略的 mcp.json）
        必须比其余的更显眼；
      * `is_default` —— **这条是不是"什么都没配"时的样子**。决策 14 要"权限范围只
        显示非默认项"，而判断"什么算非默认"是 `config.py` 的知识、不是界面的，
        所以答案必须由这里给出（见 `notices()` 里那段说明）。

    `stream` 和 `level` 是两个维度，别混：前者是"老 CLI 打到哪个流"（历史契约），
    后者是"这条有多要紧"（给人看的前端该照它排序）。
    """

    stream: str          # "out" | "err" —— 老 CLI 的打法
    text: str
    code: str = ""
    level: str = "info"          # "info" | "warn"
    is_default: bool = False     # True = 没配任何东西时的样子


def _warn(text: str) -> None:
    """警告始终可见，不受 debug 开关控制。

    被吞掉的失败如果不吭声，就变成了静默失败 —— 那比直接崩还难查。这条和
    `Agent._warn` 是同一条规矩，只是这里发生在装配期（还没有 Agent）。
    """
    print(f"[warn] {text}", file=sys.stderr)


# --- 第一段：不需要模型、也不需要通道 -------------------------------------------

def project_dir() -> Path:
    """工作区 = `agent_runtime` 包目录。

    它和"仓库根"是两件事，别共用一个变量：包要能 `import agent_runtime`（所以
    `sys.path` 需要仓库根），而 agent 的文件工作区必须是包目录本身 —— 否则
    `read_file` / `write_file` 会伸到同级的其它项目里去。
    """
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Booted:
    """`boot()` 的产物：在"要不要调模型"之前就能定下来的东西。

    它们服务于四个不需要模型的子命令（`--list` / `--skills` / `--audit` / `--history`），
    所以必须在配置检查**之前**就存在 —— 没配密钥的人照样该能查自己的历史。
    """

    store: JsonSessionStore
    logs: JsonlSink
    skill_loader: SkillLoader
    skill_catalog: SkillCatalog


def boot() -> Booted:
    """造 store / logs / 技能扫描器。不需要密钥、不需要会话。"""
    root = project_dir()

    # 技能是硬盘上的文件，所以扫它不需要模型 —— 和 `--list` 同一档。
    #
    # 扫的是**六个约定目录**（用户级三个、项目级三个，见 skills/loader.py 的
    # default_roots）：用户级那三个在工作区外面，而这条路径是硬编码的 ——
    # SkillLoader 不接受模型给的路径，所以"技能只有人能改"在用户级目录上是
    # 操作系统帮着保证的。
    loader = SkillLoader(root)
    return Booted(
        store=JsonSessionStore(root / RUNTIME_DIR_NAME / "sessions"),
        logs=JsonlSink(root / RUNTIME_DIR_NAME / "logs"),
        skill_loader=loader,
        skill_catalog=loader.reload(),
    )


def check_session_id(session_id: str | None) -> str | None:
    """`--session` 是用户直接敲进来的字符串，写错了要能照着改。

    校验规则本身在 `state/session.py`（它描述的是"什么算合法会话 id"），而这条只
    负责把"不合法"翻译成一句人话。**必须在碰 store 之前**做，否则 ValueError 会从
    `store.load` / `_path` 里冒出来，用户在终端上看到的是一整段 Python 栈 —— 而
    `--session` 写错（带空格、带斜杠、复制进来一个 Windows 路径）是最常见的手滑，
    项目别处（ConfigError、缺密钥）刻意都做到了"报错 + 退出码 2"。

    返回 None 表示没问题；返回字符串就是那棵写好的报错文案。
    """
    if session_id is None or is_valid_session_id(session_id):
        return None
    return (
        f"非法的 --session：{session_id!r}\n"
        f"  会话 id 只能由字母、数字、下划线、连字符组成，长度 1~64 ——\n"
        f"  因为它会被拿去拼文件名（{RUNTIME_DIR_NAME}/sessions/<id>.jsonl 和"
        f" {RUNTIME_DIR_NAME}/logs/<id>.jsonl）。\n"
        f"  用 --list 看一下有哪些现成的 id。"
    )


def resolve_session(
    store: JsonSessionStore, session_id: str | None
) -> tuple[str, Session, bool]:
    """决定这次聊哪个会话，返回 `(id, 会话, 是不是接上了一个已有的)`。

    **第三个返回值是刻意加的。** 调用方需要它来告诉用户"新会话"还是"继续会话 N 条
    消息"，而这件事**推不出来**：一个新建的会话也带一条 system 消息，所以
    `len(messages) > 1` 不是判据（实测踩过 —— 新会话被报成 resumed=True）。
    与其让每个调用方各自去猜，不如在这里一次说清。

    注意这里还不会落盘 —— 第一次写盘发生在你说出第一句话之后（Agent 在把用户消息
    追加进 messages 之后才触发 checkpoint）。所以"开了不用"不会留下空文件。

    **它不打印。** 那两行"继续会话 / 新会话"是呈现，由前端自己发（CLI 打 stdout，
    TUI 显示在界面上）。

    **工作区在这里被交出去**（`project_dir()` 那两处）：新建的会话要拿它去读那份
    AGENT.md。已经存在的会话不重新读 —— 它的 system 消息在创建时就写定了（和
    `prompts/system.zh.md` 同一条规矩，见 `Session.new`）。
    """
    if session_id:
        if store.exists(session_id):
            return session_id, store.load(session_id), True
        return session_id, Session.new(session_id, project_dir()), False

    # 只分配一次 id：`store.new_session_id()` 每次调用都会重新取时间戳，所以
    # `Session.new(store.new_session_id())` 这种写法会让"文件名里的 id"和"会话对象
    # 里的 id"在某次跨秒的调用里分开 —— 那种不一致落盘之后是完全看不出来的。
    new_id = store.new_session_id()
    return new_id, Session.new(new_id, project_dir()), False


# 预览截断到多少字符。**它比 `--list` 那条路多一个数**：终端里 `--list` 打一行就够，
# 而面板上每一行还要跟一段"这个会话是关于什么的" —— 那正是选会话时唯一有用的信息。
# 40 个字符在 76 列的弹层里放得下，再长会把消息条数挤出去。
PREVIEW_CHARS = 40

# 会话清单最多回多少条。**必须有个上限**：`session_list` 是前端在交互中发的，
# 而 `.tudouni/sessions/` 攒到几百个文件时，一次列全部会让面板的渲染和键盘响应都
# 变钝。取**最新**的这些条 —— 要接着聊的几乎总是最近那几个。
SESSION_LIST_LIMIT = 50


def _created_key(session: Session, path: Path | None) -> tuple[float, str]:
    """一份会话的**创建时间**排序键：`(epoch 秒, session_id)`。

    三条来源，优先级从高到低，各自都有具体的理由：

      1. `metadata["created_at"]` —— 真话。新建会话时写进去（`Session.new`），
         跟着会话文件一起落盘，所以它跨进程、跨"说没说过话"都成立；
      2. **会话文件的 mtime** —— 退路，只服务于**老会话文件**（这个键落地之前写的）。
         它其实说的是"最后一次聊"，所以对老会话来说这个排序是近似的；用它而不是
         直接给 0，是因为老文件的 mtime 至少是**同一台机器上真实的先后顺序**，
         而全给 0 会把它们一起丢给 id 那个兜底；
      3. `0.0` —— 连文件都读不到时（`path is None`）。那时候 id 说了算。

    **id 是每一个分支的第二个分量**，不是可选的美化：两个会话的秒级时间戳相同时
    （同一秒里建了两个、或者两个老文件的 mtime 精度只到秒）必须还有一个确定的
    次序，否则列表的顺序会随 `sorted` 的实现细节变。
    """
    created = session.metadata.get("created_at") if session.metadata else None
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        created = None
    if created is None and path is not None:
        try:
            created = path.stat().st_mtime
        except OSError:
            created = None
    return (float(created) if created is not None else 0.0, session.session_id)


def _modified_at(path: Path | None) -> float | None:
    """会话文件最后一次被写的时间（epoch 秒）。**读不到就是 None。**

    它是"最后一次聊这个会话"，和 `created_at` 是**两件事**：选会话面板按创建时间排
    （"这是哪一次对话"），而 TUI 欢迎屏右上那栏按这个排（"我上次干到哪儿了"）。
    后者不能拿 `created_at` 顶替 —— 一个昨天建、今天还在聊的会话会被排到"昨天"。

    **它不是 `metadata` 里的字段**：mtime 是文件系统的属性，不该再抄一份进会话
    文件（抄了就有两份，而 `save` 每次整份重写时它们会对不上）。
    """
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def session_summaries(
    store: JsonSessionStore, *, limit: int = SESSION_LIST_LIMIT
) -> list[dict[str, Any]]:
    """已保存会话的清单（**按创建时间，最新在前**），每条一句话说清"这是哪个会话"。

    它是 `sessions` 那条协议消息的内容，也是 `--list` 与 TUI 选会话面板**共同的**
    事实来源：让两个前端各自去读会话文件、各自决定怎么截预览，就是同一份事实的
    第二、第三个来源，而它们漂掉的症状是"同一个会话在两处看起来不一样"。

    ## 排序为什么不是"id 倒序"（那是它以前的样子）

    自动分配的 id 就是时间戳，所以"按 id 排"和"按创建时间排"在那批会话上恰好一致
    —— 于是这个 bug 一直看不出来。但 `--session demo` 这种自己起的名字不是时间戳，
    按 id 排会把 `demo` 排到 `20250101-…` 后面，而它可能是昨天才建的。
    判据是**数据**（`created_at`），不是"文件名的写法恰好长得像时间戳"。

    一条读失败不拖垮整份清单：会话文件可能被截断、也可能是更新版本写的
    （`store.load` 会为此抛 ValueError）—— 那种文件在列表里显示成"读不出来"比让整个
    面板打不开好得多，而"打不开"的症状是**用户根本不知道有一个坏文件**。

    ## 为什么只对**读得出来**的那些排序

    坏文件没有 `created_at` 可读，把它和最老的会话混在一排会让"哪个才是刚才那个"
    变模糊。所以它们统一排在最后（时间键 0），而它们本来也不该出现在选择面板的
    前几条里。
    """
    loaded: list[tuple[tuple[float, str], dict[str, Any]]] = []
    for session_id in store.list_ids():
        # `store._path` 是"一个 id 对应哪个文件"的**唯一**说法（它同时兜住 id 的
        # 合法性校验）。自己拼 `directory / f"{id}{SUFFIX}"` 就是同一件事的第二个说法，
        # 而它漂掉的那天，症状是"列表里的时间和实际文件对不上"——没人查得出来。
        # （后缀在这里**不写死**有具体的来路：会话文件从 `.json` 改成 `.jsonl` 那次，
        # 凡是自己拼后缀的地方都要跟着改，而走 `_path` 的一处都不用动。）
        path = store._path(session_id)
        try:
            session = store.load(session_id)
        except Exception as exc:  # noqa: BLE001 - 坏文件只影响它自己那一行
            item = {
                "session_id": session_id, "messages": 0, "steps": 0,
                "todos": "", "preview": f"（读不出来：{type(exc).__name__}）",
                "modified_at": _modified_at(path),
            }
            loaded.append(((0.0, session_id), item))
            continue
        item = {
            "session_id": session_id,
            "messages": len(session.messages),
            "steps": session.step_count(),
            "todos": progress_line(session.metadata),
            "preview": _first_user_message(session),
            # "最后一次聊"（文件 mtime）。欢迎屏右上那栏按它排，见 `_modified_at`。
            "modified_at": _modified_at(path),
        }
        loaded.append((_created_key(session, path), item))

    # **先排完再截断**：截断要的是"最新的 N 个"，而那只有排完之后才知道。
    # （`store.list_ids()` 是按 id 升序的，直接切尾巴拿到的只是"id 最大的 N 个"。）
    loaded.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _key, item in loaded[:limit]]


def _first_user_message(session: Session) -> str:
    """会话里第一句用户说的话，截断成一行。**没有就返回空串。**

    为什么是第一句而不是最后一句：选会话的时候人要认的是"这是哪一次对话"，
    而开头那句话是这份对话的标题。最后一句通常是"继续"、"嗯"这种认不出来的东西。

    `preview_line` 里那句 note 说的就是这件事（**它是给人看的，不许当会话正文用**），
    所以这里不再抄一遍详细理由。
    """
    for message in session.messages:
        if message.get("role") == "user":
            text = message.get("content") or ""
            if not isinstance(text, str):
                text = str(text)
            return preview_line(text, PREVIEW_CHARS)
    return ""


def preview_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    """把一段多行文本压成**一行**给列表用。

    **换行必须先换成空格，不能直接截断**：`read_file` 那种正文一进列表就是几十行，
    面板会被撑开，而"每一行一条"是这个面板的版式（和上下栏同样的理由）。
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


# --- 第二段：需要模型和通道 -----------------------------------------------------

@dataclass(frozen=True, slots=True, eq=False)
class Runtime:
    """装配好的一个运行时。

    **它是"持有者"，不是一个纯函数** —— 这个区别有一个具体的后果：它持有两样有
    *进程生命周期*的东西，必须在会话结束时显式收掉（见 `close()`）。

    frozen 是有意的：装配完之后没有任何一个字段该被改。任何"运行中换模型 / 换策略"
    的需求都该再 `open_runtime()` 一个，而不是就地改 —— 那会让审计里前后两段属于同
    一个 run 却按不同策略放行，而那种不一致事后完全看不出来。

    **唯一的例外是模型，而且它是被记账的例外**（见 `select_model`）：换模型在会话历史里
    留下一句"从这里开始用谁"，在审计里留下 model_call 的逐条记录，所以"这一次回答是
    谁生成的"永远答得出来。而"就地换掉权限策略"没有这种痕迹 —— 那才是这条 docstring
    真正防的东西。

    `eq=False`：这个对象持有进程和 socket，它长得不像一个"值"。默认生成的
    `__eq__` 会把两个 Runtime 逐字段比较，包括 httpx client —— 那除了慢没有别的用。
    """

    # 配置。保留下来是为了让 notices() 能说话，而不是给别的代码绕过权限关卡去读它。
    model_cfg: ModelConfig
    web_cfg: WebConfig
    mcp_cfg: McpConfig
    permissions: PermissionConfig
    # 会话与持久化
    session_id: str
    session: Session
    store: JsonSessionStore
    logs: JsonlSink
    skill_loader: SkillLoader
    skill_catalog: SkillCatalog

    # 装配出来的零件
    tools: Any
    policy: PermissionPolicy
    memory: ApprovalMemory
    agent: Agent

    # `/model` 那张清单（`state/catalog.py` 的目录）。它排在装配出来的零件之后，
    # 因为有默认值：加一个模型是改配置文件的事，不该逼每一个构造点都跟着改一行。
    #
    # **它是快照，而"现在用的是哪个"不是** —— 后者读 `agent.model_name`（见
    # `current_model`）。把当前模型也存成字段就有了两份事实，而它们分家的症状是
    # "状态栏写着 flash，请求发给了 pro"。
    #
    # 字段名**不叫 `catalog`**：那会和模块顶部 `from agent_runtime.state import catalog`
    # 撞上，而类体里 `catalog.Registry()` 会解析成 `Registry`（注解求值把类作用域里的
    # 同名属性当成了模块）—— 一个只在这一行报错的谜。
    model_registry: catalog.Registry = catalog.Registry()

    # 一次性开关（用来渲染那条 autopilot 警告）
    autopilot: bool = False
    debug: bool = False
    # 这一次运行开不开流式（模型逐字吐、界面逐字画）。
    #
    # **它是一个"运行期事实"，所以跟着 Runtime 走、并且发给前端**（`init.stream`）：
    # 界面要据此决定"正文从哪儿来"——开了流式之后逐字那份才是正文，而
    # `ui(run_finished).answer` 只是同一个答案的第二次出口（前端只用它兜空）。
    # 界面自己猜（比如"收到过 delta 就算流式"）在**一轮里一个字都没吐**的时候会猜错。
    stream: bool = False
    # 一个回合最多几步。**默认值和 `Agent.run` 的默认值必须是同一个数** ——
    # `init` 要把它发给前端显示（"第 3/80 步"），而前端显示一个和实际预算不同的数
    # 比不显示更坏。所以它有个具名常量，两处都引它。
    max_steps: int = DEFAULT_MAX_STEPS
    # 这次是接上了一个已存在的会话吗。**它不是派生值**：一个新建的会话也带一条
    # system 消息，所以 `len(messages) > 1` 判不出来（实测踩过：新会话被报成
    # resumed=True）。只有 `resolve_session` 知道答案，它返回第三个值，
    # 装配时存进来。
    resumed: bool = False

    # 装配期才知道、而通道那一侧**被调用时**才需要的东西：审批里那个 `a` 的查询口。
    # 它要等 MCP 连上，所以造不出更早；而协议版的 asker 在 `__call__` 里才用它。
    # 为 None 表示"这次没有可信任的组"（没配 MCP，或者 MCP 没连上）。
    mcp_trust_group: Callable[[str], Any] | None = None

    # 需要显式收掉的进程级资源。**排在最后**：前面那些字段有位置参数的调用点，
    # 插在中间会静默地把它们挪位。
    _http: httpx.Client | None = None
    _mcp: McpToolset | None = None
    # 后台任务那张表。它和上面两个是同一类东西（进程级、必须显式收掉），但**只有它会
    # 自己开进程** —— 所以 close() 里收不干净的话，留下的是用户机器上一直在跑的服务。
    # 类型写成 Any 是因为 `Runtime` 不 import tools 那一层（依赖方向：runtime → tools
    # 是允许的，但这里只需要"它有个 close()"这一个事实，写死类型没有收益）。
    _jobs: Any = None

    # -- 模型（`/model`）--------------------------------------------------------

    @property
    def current_model(self) -> str:
        """现在真正在用的模型名。**从 Agent 上读，不留第二个字段。**

        空串 = 适配器没报出模型名（`ChatModel` 的契约里没有这个属性）。调用方按
        "不知道"处理：显示成空、且不报上下文占比。
        """
        return self.agent.model_name

    @property
    def current_provider(self) -> str:
        """现在这条请求发给哪条路由（`deepseek` / `acme` …）。"""
        return str(getattr(self.agent.model, "provider", "") or "")

    @property
    def current_base_url(self) -> str:
        """**适配器上那个端点**，不是配置里那个。

        两者不一样只可能出现在"换过路由但没生效"这种情况里（比如适配器不支持换），
        而那时候该显示的是**真实**的端点 —— 报配置里那个等于把"请求发到哪儿"这件事
        说反了，而它正是这一行存在的理由。
        """
        return str(getattr(self.agent.model, "base_url", "") or "")

    @property
    def current_route(self) -> str:
        """`provider/model` —— 换模型那句说明和 `/status` 都用它。

        两条路由可以有同名模型，而"请求发到哪儿"在账单上、在合规上都是另一件事 ——
        所以这个名字里必须带路由，光一个模型名答不出"它到底在哪跑"。
        """
        provider, model = self.current_provider, self.current_model
        return f"{provider}/{model}" if provider and model else (model or "")

    @property
    def context_tokens(self) -> int | None:
        """**当前那条路线上那个模型**的上下文窗口（分母）。

        它从目录派生，**不是一个存下来的字段** —— `/model` 能中途换模型（甚至换路由），
        存字段就意味着换完之后这里还是旧的那个，而症状是状态栏那个百分比按旧窗口算：
        看起来完全正常，只是数错了。

        目录里没有这个名字时返回 None（自建网关上的模型、或者配置里没写窗口）：
        只报用量、不报占比 —— 错的百分比比没有百分比更坏。
        """
        ref = self.model_ref()
        return ref.window if ref is not None else None

    def model_ref(self) -> catalog.ModelRef | None:
        """当前这个"路由 + 模型"在目录里的那一条（认不出来返回 None）。

        它是**唯一的对账口**：窗口、能力、这条路由上的出厂强度都从这里取。自己再存
        一份的话，`/model` 换了之后就会有两份事实，而它们分家的症状正是最难看的那种
        （界面显示 A 的窗口、请求用的是 B）。
        """
        provider = self.model_registry.provider(self.current_provider)
        if provider is None:
            return None
        return provider.find(self.current_model)

    def select_model(self, name: str, *, provider: str = "") -> tuple[bool, str]:
        """换这个会话用哪条路由上的哪个模型。返回 `(换成了吗, 说给用户听的一句话)`。

        ## 名字的三种写法

          * `provider/model` —— 明确指定。两条路由有同名模型时**必须**这么写；
          * 光写模型名 —— 只在**唯一一条**路由上有它时才认；落在多条路由上时**不猜**，
            而是把候选报出来（随便挑一条是那种"看起来完全正常、账单却在另一个账号上"
            的错误）；
          * 光写 provider 名（`/model acme`）—— 用那条路由上的第一个模型。这个用法很
            自然（"换到 acme 去"），而 acme 上有什么它自己知道。

        ## 三道检查，顺序有意

          1. **名字认不认识** —— 判据是目录（`/model` 那张清单就是从它渲染的）。
             接受一个目录外的名字等于让那张清单变成一句谎话，而"选了一个它根本没
             列出来的模型"这件事没有任何地方会报；
          2. **那条路由有没有密钥** —— 没密钥的路线在目录里照样列着（"这台机器上知道
             它存在"和"现在能用它"是两件事），但选不了；
          3. **适配器认不认** —— 见 `Agent.switch_model`。

        ## 落盘与生效的时序

        这里**立刻落盘**（理由见 `_save_now`）。生效则是"下一次请求" —— 正在跑的那一轮
        已经发出去了，不受影响（见 `Agent.switch_model`）。

        模型与路由都一样时**照样走完**并返回"已经是它了"：那是幂等的，而且用户按了两遍
        `/model flash` 该得到一句"已经是它"，不是一句错误。
        """
        wanted = (name or "").strip()
        if not wanted:
            return False, "没给模型名。/model 不带参数看清单。"

        provider_name = (provider or "").strip()
        if "/" in wanted and not provider_name:
            provider_name, _, wanted = wanted.partition("/")
            provider_name, wanted = provider_name.strip(), wanted.strip()

        # 只写了 provider（`/model acme`）：用那条路由上的第一个模型。
        #
        # **先问"这是不是一条路由名"，再当模型名找。** 顺序不能反：反了的话，一条
        # 名字恰好和一个模型同名的路由（网关叫 `flash`、而模型也叫 `flash`）就会被
        # 当成模型名，于是 `/model flash` 换的是一个模型而不是那条路由 —— 而这两件事
        # 在账单上完全不同。
        if not wanted and provider_name:
            found = self.model_registry.provider(provider_name)
            if found is None:
                return False, f"没有这条路由：{provider_name} —— /model 不带参数看清单。"
            if not found.models:
                return False, f"路由 {provider_name} 一个模型都没声明。"
            wanted = found.models[0].id
        elif not provider_name and self.model_registry.provider(wanted) is not None:
            found = self.model_registry.provider(wanted)
            if not found.models:
                return False, f"路由 {wanted} 一个模型都没声明。"
            provider_name, wanted = wanted, found.models[0].id

        ref = self.model_registry.find(wanted, provider=provider_name or None)
        if ref is None:
            hits = self.model_registry.ambiguous(wanted)
            if hits:
                names = "、".join(item.qualified for item in hits)
                return False, (
                    f"{wanted} 在多条路由上都有（{names}）—— "
                    f"写全一点：/model provider/model"
                )
            known = "、".join(item.id for item in self.model_registry.models()) or "（一条都没有）"
            return False, (
                f"目录里没有这个模型：{wanted} —— /model 不带参数看清单。"
                f"（清单是配置里写死的几个名字，不会把任意名字转给网关："
                f"那样打错一个字母只会在下一次请求时才炸。）现在有：{known}"
            )

        target = self.model_registry.provider(ref.provider)
        if target is None:  # pragma: no cover - find() 就是从 providers 里找出来的
            return False, f"那条路由不见了：{ref.provider}"
        if not target.usable:
            return False, (
                f"路由 {ref.provider} 没有密钥，选不了它下面的模型 —— 在 "
                f"{catalog.MODELS_FILE_NAME} 里给它写一个 api_key 或 api_key_env。"
            )

        if self.current_model == ref.id and self.current_provider == ref.provider:
            return True, f"已经是 {ref.qualified} 了。"

        previous = self.current_route or "（未知）"
        if not self.agent.switch_model(
            ref.id, provider=ref.provider,
            api_key=target.api_key, base_url=target.base_url,
        ):
            return False, (
                f"这个会话的模型适配器不支持中途换模型（{type(self.agent.model).__name__}）"
                f"—— 只能重启时在 {catalog.MODELS_FILE_NAME} 里改默认值。"
            )

        self._save_now("换模型")
        # 回报必须带上"上一个是谁"：那句话里同时有"上面那些轮次是谁写的"和"从这里
        # 开始是谁"，而界面拼不出来（它不知道上一轮用了谁）。
        return True, f"换成 {ref.qualified}（上一个：{previous}）—— 下一次请求生效。"

    def select_thinking(self, on: bool) -> tuple[bool, str]:
        """开关思考模式（`/thinking`）。返回 `(改了吗, 说给用户听的一句话)`。

        **它不动模型，也不动强度**：三样是独立的旋钮，而"顺手把强度也重置了"是那种
        用户没要求、事后也查不出来的行为。关掉之后强度仍然记着（`/thinking on` 回来
        还是原来那个）—— 实测端点在关掉思考时**忽略** effort，所以我们也不发它
        （见 `state/reasoning.request_fields`），但那不代表要把用户的选择删掉。
        """
        if not self.agent.set_reasoning(thinking=on, effort=None):
            return False, "这个会话的模型适配器不支持改思考模式。"
        self._save_now("改思考模式")
        if on:
            return True, (f"思考模式：开（强度 {self.agent.effort}）"
                          f"—— 下一次请求生效。")
        return True, "思考模式：关（强度记着，/thinking on 回来还是它）—— 下一次请求生效。"

    def select_effort(self, effort: str) -> tuple[bool, str]:
        """改思考强度（`/effort`）。返回 `(改了吗, 说给用户听的一句话)`。

        **关着思考时照样接受并记下来**（只是这一次请求不会发出去）。拒绝的话，用户就
        得先记住"要先把思考打开才能设强度"—— 而那是我们发明的顺序，不是任何地方要求的。
        """
        level = reasoning.resolve_effort(effort)
        if level is None:
            if reasoning.is_off(effort):
                # `none` 是端点认的"关掉思考"的写法，而它在这一版里是**另一个旋钮**。
                # 指路而不是照做：把 `/effort none` 当成 `/thinking off` 会让人以为
                # 强度变成了 none（而清单里根本没有那一档）。
                return False, ("`none` 是关掉思考，不是一档强度 —— 用 /thinking off"
                               "（强度会留着），或者 /effort "
                               f"{'、'.join(reasoning.EFFORT_LEVELS)}。")
            return False, (
                f"没有这一档强度：{effort} —— 能写的只有 "
                f"{'、'.join(reasoning.EFFORT_LEVELS)}"
                f"（端点还接受 {'、'.join(sorted(reasoning.ALIASES))} 这些等价写法）。"
            )
        if not self.agent.set_reasoning(thinking=None, effort=level):
            return False, "这个会话的模型适配器不支持改思考强度。"
        self._save_now("改思考强度")
        if self.agent.thinking:
            return True, f"思考强度：{level} —— 下一次请求生效。"
        return True, f"思考强度记成 {level} 了，但思考模式关着（/thinking on 才用得上）。"

    def _save_now(self, what: str) -> None:
        """把这几个会话级设置立刻落盘。**失败不当失败，但必须说。**

        换模型 / 改思考设置都是"用户明确按了一下"，而它们全部只改 `session.metadata`
        —— 那一块平时靠回合里的检查点落盘。等着下一个检查点的话，最自然的用法之一
        （进去、改一下、退出）会丢掉这次改动，而恢复会话时我们报的是旧值 —— 那和刚
        给过的承诺相反。
        """
        try:
            self.store.save(self.session)
        except Exception as exc:  # noqa: BLE001
            _warn(f"{what}之后落盘失败（这一次仍然生效，重开会话会回到配置里那个）："
                  f"{type(exc).__name__}: {exc}")

    def model_rows(self) -> tuple[list[dict], list[dict]]:
        """`/model` 那张清单：目录 + "现在用的是哪个" + "认下的旧名字"。"""
        current = self.model_ref()
        rows = [
            item.as_row(current=bool(
                current is not None and item.provider == current.provider
                and item.id == current.id))
            for item in self.model_registry.models()
        ]
        # 别名（已下线、仍可调用的旧名字）单列：它们是**认下的名字**，不是能选的选项。
        # 放进主清单会摆出两个效果完全一样、价钱也一样的选项，而"我到底选了哪个"就没有
        # 答案了。
        aliases = [
            {"id": alias, "of": target}
            for alias, target in sorted(catalog.ALIASES.items())
        ]
        return rows, aliases

    # -- `/status` -------------------------------------------------------------

    def status(self, *, counts: dict | None = None,
               usage: dict | None = None) -> dict[str, Any]:
        """`/status` 那一屏要的全部事实。**结构化数据，谁来渲染各管各的。**

        分成四组是**刻意的**：这一屏要显示的东西跨了四个层次（会话 / 模型 / 账 /
        这次运行的环境），而平铺成一个大字典之后，字段名会在将来某次合并里悄悄撞车
        （比如"工具清单"和"工具个数"）。

        后两组**由调用方传进来**（`state/status.summarize` 的产物），因为这个函数
        自己数不出来：账在审计日志里，而"读文件"是调用方的事（协议层从它自己的 sink
        读、CLI 从 `runtime.logs` 读）。这样这个函数是纯的、可测的，也不会因为日志
        文件被删掉而失败 —— 而那正是"一次 `/status` 不该让会话出问题"的全部含义。

        `tool_count` 在这里**只给个数**：`/tools` 那条命令要的是完整清单（带权限），
        它有自己的一条消息（`ui(kind="tools")`）。把清单塞进每一份 status 快照等于
        每次刷新都重发那几十行，而状态栏并不显示它们。
        """
        session_model = self.agent.session_model
        return {
            "session": {
                "id": self.session_id,
                "resumed": bool(self.resumed),
                "workspace": str(self.workspace),
                "messages": len(self.session.messages),
                "steps": self.session.step_count(),
            },
            "model": {
                # **现在真正在用的那个**（`Agent` 上的适配器说了算），不是配置里那个。
                "current": self.current_model,
                # 哪条路由，以及**适配器上那个**端点。三分开是有意的：两条路由可以有
                # 同名模型，而"请求发到哪儿"在账单上、在合规上都是另一件事。
                "provider": self.current_provider,
                "base_url": self.current_base_url,
                "route": self.current_route,
                # 这个会话**选**的（`/model` 写的那个）。它和 `current` 在换完模型、
                # 下一次请求之前会暂时不同，而那个差别正是"还没生效"的证据。
                "selected": session_model.selected if session_model is not None else "",
                "last_used": session_model.last_used if session_model is not None else "",
                "since": session_model.selected_since if session_model is not None else 0.0,
                # 当前模型的窗口（分母）。None = 目录里没有这个名字，只报用量。
                "window": self.context_tokens,
                # 思考模式那两个旋钮。**它们和模型一样是"会话级设置"**，所以和
                # provider/model 同住一组 —— `/status` 那一屏的"它在用什么"指的就是
                # 这几样：谁、哪条路由、想不想、想多用力。
                "reasoning": {
                    "thinking": bool(self.agent.thinking),
                    "effort": self.agent.effort,
                },
            },
            "counters": dict(counts or {}),
            "usage": dict(usage or {}),
            "meta": {
                "max_steps": self.max_steps,
                "stream": bool(self.stream),
                "autopilot": bool(self.agent.autopilot),
                "tool_count": len(self.tools.all()),
                "audit_path": str(Path(self.logs.directory) / f"{self.session_id}.jsonl"),
                "permissions": self.non_default_permissions(),
                # 目录是从哪读的。**它是事实，不是装饰**："为什么我改的配置没生效"
                # 这个问题的答案就是这一个字符串（内置 / 哪份文件的绝对路径）。
                "catalog": self.model_registry.source,
            },
        }

    def tool_rows(self) -> list[dict[str, Any]]:
        """`/tools` 那一屏：每个工具 + 它的权限。

        权限那一栏是**策略自己给出的裁定**，不是界面拼的：`decide()` 是唯一知道
        "这个工具会不会问我"的地方（按等级放行、点名拒绝两条路都在里面）。这里不
        假装回答"等级名单 + 点名免问"那两层 —— 那两层要 `ApprovalMemory`，而这一列
        只要"要不要问"这个总答案，另外两个标记（`granted` / `command`）说明"为什么
        不用问"。

        `command` 是这个工具的参数里有没有**命令行**（只有 shell 有）。有它的工具才
        谈得上"按命令前缀放行"那条规则 —— `/tools` 末尾那句提示靠它决定说不说。
        """
        from agent_runtime.security.policy import Decision

        granted = set(self.memory.tools())
        rows: list[dict[str, Any]] = []
        for tool in self.tools.all():
            # 空参数问一次即可：`decide` 只看工具名与风险等级（参数要在更细的策略里
            # 才用得上，见 policy.decide 的说明）。
            decision = self.policy.decide(tool, {})
            rows.append({
                "name": tool.name,
                "risk": tool.risk.value,
                "disposition": {
                    Decision.ALLOW: "auto",
                    Decision.DENY: "deny",
                }.get(decision, "ask"),
                "parallel_safe": bool(tool.parallel_safe),
                "interactive": bool(tool.interactive),
                # 外部工具（MCP）。它和内置工具的差别不只是名字：风险一律 high、
                # 每次都要你按键放行 —— `/tools` 里要看得见这个来源。
                "external": tool.name.startswith("mcp__"),
                "granted": tool.name in granted,
                "command": command_parameter(tool.name),
            })
        return rows

    # -- 呈现 ------------------------------------------------------------------

    def notices(self, *, with_tools: bool = True) -> list[Notice]:
        """关于这次运行的全部说明。**结构化数据，谁来打印/渲染各管各的。**

        它把原来散在 `main.py` 里的 7 处 print 收成一处 —— 而它们的内容和流向
        (stdout/stderr) 一个字都没改，这是第零期的验收标准之一。

        `with_tools=False` 跳过那份已注册工具清单：TUI 要的是 `init.tools` 那样的
        结构化名单，而不是一句排好版的文本。
        """
        out: list[Notice] = []

        # [上下文]：末尾那句统计里 "xx/yy" 需要有 yy，而响应里没有这个字段，
        # 所以它来自 config 里那张按模型名的表。表里没有就**只报用量、不报占比**
        # （错的百分比比没有百分比更坏）。
        if self.context_tokens is None:
            out.append(Notice("err", code="context",
                text=f"[上下文] 模型 {self.current_model!r} 不在目录里（或者配置里没写"
                f"它的 context_window），末尾只报上下文用量、不报占比；"
                f"把它那一行补上即可。"))

        # 缺搜索密钥不是配置错误（不像 DEEPSEEK_API_KEY）：只是不注册那一个工具。
        if not self.web_cfg.tavily_api_key:
            out.append(Notice("err", code="web",
                text="[联网] 没找到 TAVILY_API_KEY，web_search 未注册（fetch_web 不受影响）。"
                "要启用就写进 .env：TAVILY_API_KEY=tvly-..."))

        # [搜索]：**它和上面那条不是一类问题**，所以语气也不同。
        #
        # 缺 TAVILY_API_KEY 是"你没配这个可选能力"，缺引擎是"这份检出缺件" —— 后者按
        # 设计本来就该在仓库里（tools/vendor/rg/ 随包走，见那个目录的 README）。所以
        # 这里要给的是"怎么补"，而不是"怎么配密钥"。
        #
        # 必须说，不能静默：grep 不注册之后，模型搜文本只剩"起一条 shell 命令"那条路，
        # 而那条路每次都弹审批 —— 用户看到的会是"怎么老问我"，而不是"我少了什么"。
        #
        # **两个分支要分开说。** "这个平台没被支持"和"支持了但文件没了"是两件事，补救
        # 办法也完全不同（一个要往代码里加一行，一个跑条命令就行）。合成一句"缺少引擎"
        # 的话，前者会让人反复跑 fetch 脚本，而脚本无论如何也解决不了它 —— 那正是
        # "说反了原因，用户就去查一个不存在的问题"。
        if rg_binary() is None:
            triple = host_triple()
            if triple is None:
                out.append(Notice("err", code="grep",
                    text=f"[搜索] 这个平台（{sys.platform}）不在 grep 引擎的支持列表里"
                    f"（现在只有 x86_64 的 Windows / Linux），grep 未注册"
                    f"（搜文本只能走 shell，每次都要审批）。"
                    f"要支持它是两步，见 tools/vendor/rg/README.md。"))
            else:
                out.append(Notice("err", code="grep",
                    text=f"[搜索] tools/vendor/rg/ 里少了 {triple} 这一份 ripgrep，"
                    f"grep 未注册（搜文本只能走 shell，每次都要审批）。"
                    f"跑 `uv run python scripts/fetch_rg.py` 补上。"))

        # [MCP]：**每次启动都说，而且说清"它们的工具每次都要审批"** —— 外部工具默认
        # 每条都要问人，而这句话是"为什么它又问我了"唯一的解释；不说的话，用户会以为
        # 配置错了。
        if self.mcp_cfg.servers:
            out.append(Notice("err", code="mcp",
                text=f"[MCP] {MCP_FILE} 里配了 {len(self.mcp_cfg.servers)} 个 server："
                f"{'、'.join(server.name for server in self.mcp_cfg.servers)}"))
        if self._mcp is not None:
            for name, count in self._mcp.counts.items():
                out.append(Notice("err", code="mcp",
                    text=f"[MCP] server {name}：连上了，提供 {count} 个工具"
                    f"（风险一律 high，每次调用都要你批准）"))

        # 工作区里那份 mcp.json 是**故意不读**的（理由写在 config.McpConfig 上），
        # 所以它存在就等于"有人按旧位置写了一份"。这和坏技能是同一类症状：
        # 文件明明在那儿却完全不起作用。
        ignored = project_dir() / RUNTIME_DIR_NAME / "mcp.json"
        if ignored.is_file():
            out.append(Notice("err", code="mcp", level="warn",
                text=f"[MCP] 忽略了 {ignored}：server 清单只从用户级 {MCP_FILE} 读。"
                f"理由是这里的 command 是启动时就要执行的代码，而工作区里的文件可能"
                f"随仓库一起被 clone 进来（见 config.McpConfig 上面的说明）。"
                f"要用就把它挪到 {MCP_FILE}"))

        # [后台] 两条，而且必须分开说 —— 它们的补救办法完全不同。
        #
        # 第一条：**上一个进程留下了没收掉的任务**。这意味着那次会话不是正常收场的
        # （关掉了控制台窗口、或者被强杀），于是那几个进程**现在可能还在跑**，而本次
        # 会话既看不到它们、也管不到它们。为什么不管：要管就得照着记下的 pid 去杀，
        # 而 pid 会被复用 —— 认错了就是杀掉一个跟我们毫无关系的进程，那比留几个孤儿
        # 坏得多。所以这里给的是"你自己去看一眼"，而不是一个我们不敢做的动作。
        if self._jobs is not None and self._jobs.leftovers:
            out.append(Notice("err", code="jobs", level="warn",
                text=f"[后台] 上次会话留下了 {self._jobs.leftovers} 个后台任务的输出，"
                f"已经清掉了。这说明那一次没有正常退出（关掉了窗口、或者进程被强杀），"
                f"所以**那几个命令可能还在跑**，而它们不在这次会话的管辖里 —— "
                f"如果端口或 CPU 对不上，自己确认一下。"))

        # 第二条：**收树那层保证没建起来**（Windows 的作业对象）。它是"用户以为自己有"
        # 的一层，所以静默降级等于骗人 —— 和 mcp.py 里"收不掉也要大声说"同一条。
        # 没有它不等于一定会泄漏：正常退出、异常、Ctrl+C 都还能走 close()。
        if (problem := job_object_problem()) is not None:
            out.append(Notice("err", code="jobs", level="warn",
                text=f"[后台] Windows 上那层「关掉窗口也把后台任务一起收掉」的保证没建起来"
                     f"（{problem}）。正常退出仍然会收干净，但**强杀本进程时后台命令可能"
                     f"变成孤儿**。"))

        if with_tools:
            out.append(Notice("out", code="tools", text="已注册工具:"))
            for tool in self.tools.all():
                out.append(Notice("out", code="tools",
                                  text=f"  - {tool.name:16} 风险={tool.risk.value}"))

        # 名单里那些**没有被注册**的工具名：把 shell 写成 shall 的人以为自己放行了。
        # 这是唯一能告诉他的地方 —— 不该拦启动，但绝不能不说。
        unknown = self.permissions.unknown_tools(t.name for t in self.tools.all())
        if unknown:
            out.append(Notice("err", code="permissions", level="warn",
                text=f"[权限] {PERMISSION_FILE.name} 里这些工具没有注册，规则不会生效："
                     f"{', '.join(sorted(unknown))}"))

        # [权限]：**每次启动都说一遍。**「按一次 t 就永久生效」是最容易忘掉的那类
        # 设置，而这份文件攒上几条之后，光盯着它已经答不出"现在到底还有什么会问我"。
        #
        # `is_default` 就是决策 14 要的那个判断（"只显示非默认项"）：默认状态是只有
        # `low` 自动放行、其余三个键都空。**判断在这里做，不在界面里做** ——
        # "什么算默认"是 config 的知识（`DEFAULT_AUTO_APPROVE` 就在那儿），让前端
        # 自己硬编码一份默认值就是第二份事实，而它漂掉的症状是"该显示的没显示"。
        levels = ", ".join(sorted(self.policy.auto_approve)) or "（无）"
        named = ", ".join(sorted(self.memory.tools())) or "（无）"
        out.append(Notice(
            "err", code="permissions",
            text=f"[权限] 按等级自动放行 {levels}；点名免问 {named}",
            is_default=self._permissions_at_default,
        ))
        if self.policy.deny_tools:
            out.append(Notice("err", code="permissions",
                text=f"[权限] 直接拒绝 {', '.join(sorted(self.policy.deny_tools))}"))

        # 命令行规则单列一行：它是"按一次 t 记住哪条前缀"的产物，也最容易被忘掉 ——
        # 印象里只批准过一次 git add，而它此后一直静默生效。
        rules = ", ".join(format_rule(rule) for rule in sorted(self.memory.prefixes())) or "（无）"
        out.append(Notice("err", code="permissions",
                          text=f"[权限] 命令规则（按前缀放行）{rules}"))

        # [模型]：**只在"这个会话选过模型"时说**。目录里那个默认值不是新闻 ——
        # 启动横幅和 `init.model` 都写着它，再说一遍就是噪音。而"恢复一个会话、它用的是
        # 你上次 `/model` 选的那个"必须说出来：不说的话，用户会以为模型跟着配置走，
        # 而账单上会是另一回事。
        session_model = self.agent.session_model
        if session_model is not None and session_model.selection is not None:
            out.append(Notice("err", code="model",
                text=f"[模型] 这个会话选的是 {session_model.route_name()}"
                     f"（{self.current_base_url}）—— /model 可以换，/status 看现在这个。"))

        # [思考]：**同样只在"这个会话改过"时说**。默认是开 + 目录里声明的那个强度 ——
        # 那是常态，为它加一行噪音会把上面那些真警告淹掉。而"这个会话把思考关了"必须
        # 说出来：那是账单和答案质量上都能看出差别的一件事，而它没有别的出口
        # （左栏那一行写的是模型，不是思考设置）。
        if session_model is not None and session_model.selection is not None:
            if (not session_model.thinking
                    or session_model.effort != reasoning.DEFAULT_EFFORT):
                out.append(Notice("err", code="reasoning",
                    text=f"[思考] 这个会话："
                         f"{reasoning.summary(thinking=session_model.thinking, effort=session_model.effort)}"
                         f"（默认是开 · {reasoning.DEFAULT_EFFORT}）—— "
                         f"/thinking 开关、/effort 改强度。"))

        # [目录]：**目录不是内置那份时说一句它从哪来**。这句话是"我改的配置怎么没生效"
        # 唯一的答案 —— 没有它的时候，用户看到的现象只是"模型少了几个"。
        if self.model_registry.source != "内置" and self.model_registry.notes:
            out.extend(Notice("err", code="models", text=line)
                       for line in self.model_registry.notes)
        for problem in self.model_registry.problems:
            out.append(Notice("err", code="models", level="warn", text=problem))

        # [任务] / [技能]：两者都比进程活得久（存在 session.metadata 里），所以恢复
        # 会话时不说的话，用户看到的会是"它怎么突然开始更新一个我从没见过的列表"。
        todo = progress_line(self.session.metadata)
        if todo:
            out.append(Notice("err", code="todos", text=f"[任务] {todo}"))

        if self.skill_catalog.skills:
            out.append(Notice("err", code="skills",
                text=f"[技能] 可用 {len(self.skill_catalog.skills)} 个："
                     f"{'、'.join(skill.name for skill in self.skill_catalog.skills)}"))
        for item in self.skill_catalog.shadowed:
            out.append(Notice("err", code="skills", text=f"[技能] 同名遮蔽：{item}"))
        for problem in self.skill_catalog.problems:
            out.append(Notice("err", code="skills", level="warn",
                              text=f"[技能] {problem}"))
        line = active_line(self.session.metadata)
        if line:
            out.append(Notice("err", code="skills", text=f"[技能] {line}"))

        # [AGENT.md]：工作区里那份项目说明读进来了没有。**和 [技能] 是同一类事实**
        # ——"agent 手里有哪些别人写的说明"—— 所以形状（`[标签] 一句话`）、流向
        # （err，和其余启动说明一起给前端）都一样。
        #
        # **它读的是 session.metadata，不是现场重读一次盘。** 这条说的是"这个会话的
        # system 消息里到底注入了什么"，而那份消息在 Session.new() 时就冻结了：
        # 恢复一个旧会话时重读盘就会报出"加载了 AGENT.md"，而那个会话的提示词里
        # 其实一个字都没有 —— 这种"通知比事实乐观"的偏差没人查得出来。
        for level, code, text in agents_md.notices(
            self.agent_md_report(), relative_to=project_dir(),
        ):
            out.append(Notice("err", code=code, text=text, level=level))

        # autopilot：它意味着接下来所有需要审批的工具都会**直接执行**，而这件事一旦
        # 忘了自己开着，事后看日志只会觉得"这个项目怎么什么都没问"。所以它必须在
        # 启动时大声说一次，而且**不做成配置项** —— 一次性的决定不该悄悄变成永久默认。
        if self.autopilot:
            out.append(Notice("err", code="autopilot", level="warn",
                text="[权限] autopilot：不询问任何审批，需要审批的工具会直接执行；"
                     "也不会向你提问 —— 模型调 ask_user 会拿到「没有人回答」，"
                     "并被告知自己决定、把假设说出来（拒绝名单、工作区边界、控制面写入仍然生效）"))

        return out

    @property
    def _permissions_at_default(self) -> bool:
        """这一份权限配置是不是"什么都没配"时的样子。

        默认 = 只有 `low` 按等级放行，其余三个键都空（`DEFAULT_AUTO_APPROVE`）。
        **这里必须 import 那个常量而不是抄一份字面量** —— 抄一份的下场是有人改了
        config 里的默认值而这里还拿旧值判断，症状是"权限那一行要么一直空着、
        要么一直显示一句废话"。
        """
        return (
            tuple(self.permissions.auto_approve) == DEFAULT_AUTO_APPROVE
            and not self.permissions.auto_approve_tools
            and not self.permissions.deny_tools
            and not self.permissions.shell_allow
        )

    def non_default_permissions(self) -> dict[str, list[str]]:
        """这次启动生效的权限范围里**与默认不同的那些**（决策 14）。

        **只发非默认项**，而不是发全部四个键再加一句"默认是什么"：判断"什么算非默认"
        是 `config.py` 的知识（`DEFAULT_AUTO_APPROVE` 就在那儿），让前端自己硬编码
        一份默认值就是第二份事实 —— 而它漂掉的症状是"该显示的没显示"。

        默认时返回空 dict，于是界面那一行什么都不用显示。这正是决策 14 要的效果：
        "说了但不说废话"。
        """
        if self._permissions_at_default:
            return {}
        out: dict[str, list[str]] = {}
        if tuple(self.permissions.auto_approve) != DEFAULT_AUTO_APPROVE:
            out["auto_approve"] = sorted(self.permissions.auto_approve)
        if self.permissions.auto_approve_tools:
            out["auto_approve_tools"] = sorted(self.permissions.auto_approve_tools)
        if self.permissions.deny_tools:
            out["deny_tools"] = sorted(self.permissions.deny_tools)
        if self.permissions.shell_allow:
            out["shell_allow"] = [format_rule(rule) for rule in self.permissions.shell_allow]
        return out

    @property
    def workspace(self) -> Path:
        """agent 的文件工具能碰的范围。派生值，不存字段 —— 它完全由"包在哪"决定。"""
        return project_dir()

    def agent_md_report(self) -> agents_md.Report:
        """这个会话的 system 消息里那份 AGENT.md 的去向（从 `session.metadata` 还原）。

        **它是"读会话"而不是"读盘"**：这份报告在 `Session.new()` 里和 system 消息
        一起被写下来，所以恢复一个旧会话时它说的就是那份旧提示词里的事。两个消费者
        （`notices()` 和 `ui_state()`）都从这里取，于是"通知说加载了、而提示词里没有"
        这种偏差不可能发生 —— 它们本来就读同一份数据。
        """
        return agents_md.from_block(self.session.metadata.get(agents_md.SESSION_KEY))

    def ui_state(self, *, with_catalog: bool = False) -> dict[str, Any]:
        """**面板数据**：左栏（上下文栏）那几块里，会话状态那一半。

        为什么它在装配层而不在协议层：任务列表和已加载技能住在
        `session.metadata` 里，而"用什么键、结构长什么样"是 `tools/builtin/todo.py`
        和 `skills/` 的知识。协议层只该转发形状，不该认识那两个键。

        **只给指针，不给正文**：技能正文是 L2（模型调 `load_skill` 才拿得到），
        任务正文本来就只有一行。所以这个 dict 很小，可以每次工具返回都发一份。

        `with_catalog=True` 才会带上**可用**技能清单 —— 读它要重扫技能目录
        （见 `SkillBoard.catalog`），而那个目录几乎不变，所以只有开场那一条带它。
        界面上"全部技能"那个弹层要的就是这份清单；它和"已加载"是两件事。
        """
        from agent_runtime.skills.render import load_entries
        from agent_runtime.tools.builtin.todo import load as load_todos
        from agent_runtime.tools.tool import RiskLevel

        md = self.agent_md_report()
        state: dict[str, Any] = {
            "todos": load_todos(self.session.metadata),
            "skills": [dict(entry) for entry in load_entries(self.session.metadata)],
            "messages": len(self.session.messages),
            "steps": self.session.step_count(),
            # 权限范围。**按等级给出"自动放行 / 询问"**，而不是把 auto_approve 集合
            # 原样发给界面：三个等级里哪几个会自动放行是 `PermissionPolicy` 的判断，
            # 界面照着渲染就行（它不该知道"默认只有 low"这件事 —— 那是 config 的
            # 知识，界面自己硬编码一份就是第二份事实）。
            "risk_scope": [
                {
                    "risk": level.value,
                    "disposition": (
                        "auto" if level in self.policy.auto_approve else "ask"
                    ),
                }
                for level in RiskLevel
            ],
            # 「按过一次 t 就永久生效」是这套权限里最容易被忘掉的东西，而左栏的
            # 全部价值就在"我现在放行了什么"常驻可见。所以这三样一并给它 ——
            # 它们此前只有 `--audit` 那一条出口。
            "granted_tools": sorted(self.memory.tools()),
            "granted_prefixes": [
                format_rule(rule) for rule in sorted(self.memory.prefixes())
            ],
            "denied_tools": sorted(self.policy.deny_tools),
            # autopilot 是**活的那个**（`/autopilot` 能中途改它），所以读 Agent 上那份
            # 而不是 `self.autopilot`：后者是字段，而 `Runtime` 是 frozen 的
            # （见类 docstring：装配完就不再改）。界面按这条显示，于是"界面上写着开、
            # 其实没开"不可能发生 —— 那份事实只有这个来源。
            "autopilot": bool(self.agent.autopilot),
            # 这个会话的 system 消息里那份 AGENT.md 的去向。
            #
            # **它读 session.metadata，不重读盘** —— 理由和 notices 里同一条：这一格
            # 说的是"这个会话的提示词里到底有什么"，而恢复旧会话时重读盘会报出一份
            # 提示词里根本没有的说明。
            #
            # **`skipped`（没有这个文件）不进这里**：那是常态，左栏不该为它留一行。
            "agents_md": agents_md.report_for_display(
                md, relative_to=project_dir(),
            ),
            # 后台任务。**这是这一屏里唯一"机器上真的有东西在跑"的一格** ——
            # 任务列表是模型的主张，权限是配置，会话是记账，只有它对应着活着的进程。
            #
            # 快照里的每一行都是 `JobBoard.panel()` 算好的（含"结果还没收"那一档的
            # 判定），界面照着渲染 —— 和上面 `risk_scope.disposition` 同一条规矩。
            #
            # 它**可能随时变**（一条命令自己跑完了），而这条快照本来就在每次
            # `tool_result` 之后发一份；`panel()` 顺手 `poll()` 一遍，所以"跑完了"
            # 这件事最迟在下一次工具返回时出现在界面上。
            "jobs": self._jobs.panel() if self._jobs is not None else [],
        }
        if with_catalog:
            state["skill_catalog"] = [
                {"name": skill.name, "description": skill.description}
                for skill in self.skill_catalog.skills
            ]
        return state

    def audit_log_line(self) -> str:
        """「审计日志写到哪」那一行。

        **它不在 `notices()` 里，但内容仍然由装配层提供。** 分开的理由是顺序：
        老 CLI 的输出是「已注册工具 → 审计日志写到」，而工具清单是 notices 里的一条,
        所以这一行得能单独拎到 notices 之后打；同时"路径怎么拼"是装配的知识
        （`logs.directory` + `session_id`），不该让前端自己拼一遍 —— 那会是第二份事实。
        """
        return f"审计日志写到 {self.logs.directory}\\{self.session_id}.jsonl"

    # -- 生命周期 --------------------------------------------------------------

    def close(self) -> None:
        """收掉那两个**进程级**资源。**必须被调用**（用 `with` 就不会忘）。

        漏掉的后果不是"慢"或"泄漏"，而是具体的：

          * `http`：连接池里那些 keep-alive 的 socket 活到进程退出；
          * `mcp`：**是我们起的子进程** —— 漏了它，`npx` 起的 node 会活过这个进程
            （见 `tools/mcp.py` 的 `_terminate_tree`）；
          * `jobs`：**也是我们起的子进程，而且是用户会立刻注意到的那些** ——
            一个没被收掉的 dev server 还占着端口，下一次启动就会报"端口被占用"，
            而那时候已经没有任何线索指向"是上一次会话留下的"。

        收摊本身失败不该盖住"任务本身"的结果，所以 http 那条吞掉异常并说一声 ——
        和 `McpToolset.close()` 里那条规矩一致。

        **jobs 排在最前面**：它是唯一"不收就会在用户机器上继续跑"的那一个，而上面两个
        最多是占着内存/句柄。真出意外时，先保住那一个。
        """
        if self._jobs is not None:
            try:
                self._jobs.close()
            except Exception as exc:  # noqa: BLE001
                _warn(f"收后台任务时出错：{type(exc).__name__}: {exc}")
        if self._http is not None:
            try:
                self._http.close()
            except Exception as exc:  # noqa: BLE001
                _warn(f"关闭 http client 时出错：{type(exc).__name__}: {exc}")
        if self._mcp is not None:
            self._mcp.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ChosenModel(NamedTuple):
    """**解析好了的一条路线**：请求发给谁、用哪把密钥、哪个模型。

    它和 `catalog.ModelRef` 分开，是因为 ModelRef 说的是"目录里有这么一条"（纯数据、
    可以被列出来但不能被用），而这个是"这一条现在真的能用"（密钥已经在手里）。
    合并的话，每一个 ModelRef 都得带一个可能为空的密钥，而"这台机器上没有它"和
    "有它但没配密钥"就分不出来了 —— 而那两句话给用户的下一步完全不同。
    """

    provider: str
    provider_base_url: str
    provider_key: str
    id: str
    window: int | None
    default_effort: str


def _chosen(ref: catalog.ModelRef, provider: catalog.Provider) -> ChosenModel:
    return ChosenModel(
        provider=provider.name,
        provider_base_url=provider.base_url,
        provider_key=provider.api_key,
        id=ref.id,
        window=ref.window,
        default_effort=ref.default_effort,
    )


def resolve_model(
    session_model: model_state.SessionModel, registry: catalog.Registry,
) -> tuple[model_state.SessionModel, ChosenModel | None]:
    """这个会话该用哪条路由上的哪个模型。**返回 (会用哪个, 解析结果)。**

    ## 四步，顺序有意

      1. **会话选过就用它**（`/model` 之后恢复会话的人期待还是那个）—— 前提是那条
         路由还在、还有密钥、那个模型还认识；
      2. 会话选的那个**解析不出来**（配置被人改了）时退回默认 —— 用户不在键盘前，
         停下来问他没有意义；而"你想的那个没生效"由启动那条 `[模型]` 说明说清楚；
      3. 没有会话级选择时用目录的默认值：**默认路由的第一个模型**。顺序是配置文件里
         的顺序，也就是"哪个是默认"由人自己排出来，不是我们按名字猜的；
      4. 一条能用的都没有 → 返回 `None`，由调用方报一段能照着改的话。

    `fallback` / `fallback_provider` 会写回 `SessionModel`，所以"这个会话用哪条路由"
    在 `/status` 和 `/model` 里答得出来 —— 即使它一次都没 `/model` 过。
    """
    def restored(ref: catalog.ModelRef) -> model_state.SessionModel:
        return model_state.SessionModel.restore(
            session_model.metadata, fallback=ref.id, fallback_provider=ref.provider)

    def pick(provider: catalog.Provider | None) -> ChosenModel | None:
        if provider is None or not provider.usable or not provider.models:
            return None
        return _chosen(provider.models[0], provider)

    # 1/2：会话选过的那个。
    wanted = session_model.selected
    if wanted:
        where = session_model.selected_provider
        ref = registry.find(wanted, provider=where or None)
        if ref is None and not where:
            # 没写 provider 而又落在多条路由上：**不能猜**，但用户不在键盘前，所以
            # 退到默认 —— 差别是那句"你想的那个没生效"必须说出来（`problems` 的活）。
            hits = registry.ambiguous(wanted)
            ref = hits[0] if hits and _usable(registry, hits[0]) else None
        if ref is not None:
            found = registry.provider(ref.provider)
            if found is not None and found.usable:
                return restored(ref), _chosen(ref, found)

    # 3：默认路由。
    provider = registry.default_provider()
    if provider is not None and provider.usable:
        # 配置里写的那个模型名（`DEEPSEEK_MODEL`）在默认路由上时优先 —— 它是"这台机器
        # 上我想用哪个"的老写法，而配置文件的顺序是新的写法；两者优先级不能反：反了
        # 之后，一个写着 `DEEPSEEK_MODEL` 的环境里加一份配置文件会悄悄改掉默认模型。
        preferred = registry.find(wanted, provider=provider.name) if wanted else None
        chosen = _chosen(preferred, provider) if preferred is not None else pick(provider)
        if chosen is not None:
            return model_state.SessionModel.restore(
                session_model.metadata, fallback=chosen.id,
                fallback_provider=chosen.provider), chosen

    # 4：后面还有别的能用吗（默认那条缺密钥 / 没有模型）。
    for other in registry.providers:
        chosen = pick(other)
        if chosen is not None:
            return model_state.SessionModel.restore(
                session_model.metadata, fallback=chosen.id,
                fallback_provider=chosen.provider), chosen
    return session_model, None


def _usable(registry: catalog.Registry, ref: catalog.ModelRef) -> bool:
    found = registry.provider(ref.provider)
    return found is not None and found.usable


def _no_model_message(registry: catalog.Registry) -> str:
    """一条模型都配不出来时的那段报错。

    **它必须说出下一步做什么。** 这一档和"缺密钥"是同一类（用户得先做点事），而
    用户手上唯一的问题是"我该往哪写"。所以它列两种配法（写一份配置文件 / 只给一个
    环境变量），并把这次读到的路由和逐条问题一起带上 —— 那几条正是"为什么每条都
    不行"的答案。
    """
    lines = [
        "一个可用的模型都没有 —— 配不出模型就什么都干不了。",
        "",
        f"两种配法，任选其一（模板见 {catalog.MODELS_EXAMPLE_NAME}）：",
        "",
        f"  1) 写一份 {catalog._PACKAGE_ROOT / catalog.MODELS_FILE_NAME}：",
        "",
        "        {",
        '          "providers": {',
        '            "deepseek": {',
        '              "base_url": "https://api.deepseek.com",',
        '              "api_key_env": "DEEPSEEK_API_KEY",',
        '              "models": [{"id": "deepseek-flash", "context_window": 1000000}]',
        "            }",
        "          }",
        "        }",
        "",
        "  2) 什么都不写，只给一个环境变量（或 .env）：",
        "",
        "        DEEPSEEK_API_KEY=sk-...",
        "",
        "优先级：真实环境变量 > .env > 配置文件里写死的 api_key。",
    ]
    if registry.notes:
        lines += ["", "这次读到的路由：", *[f"  {item}" for item in registry.notes]]
    if registry.problems:
        lines += ["", "逐条问题：", *[f"  {item}" for item in registry.problems]]
    return "\n".join(lines)


def open_runtime(
    *,
    booted: Booted,
    session_id: str,
    session: Session,
    channels: Channels,
    autopilot: bool = False,
    debug: bool = False,
    stream: bool = False,
    resumed: bool = False,
    should_stop: Callable[[], bool] | None = None,
    on_delta: Callable[..., None] | None = None,
    model_config: ModelConfig | None = None,
    permission_config: PermissionConfig | None = None,
    web_config: WebConfig | None = None,
    mcp_config: McpConfig | None = None,
    catalog_config: catalog.Registry | None = None,
) -> Runtime:
    """从 `boot()` 的产物继续装配：配置 → 模型 → 工具 → 策略 → 记忆 → Agent。

    ## 只有 `channels` 必须先造好

    看起来 `memory` 也该由调用方给（协议版的 asker 要读它才能回答"这次能不能按 t"），
    但那样调用方就得自己拼一遍 `ApprovalMemory` —— 而"它落在哪个文件、label 写什么"
    是装配的知识。现在的做法是：

        channels 是**一份工厂**（`runtime/channels.py`），`open_runtime` 造完 memory
        之后把它调出来；而 memory 本身留在 `Runtime.memory` 上，
        协议那一侧从 `attach()` 到的 runtime 上取。

    这样"memory 怎么造"只有一份，而环也解开了 —— 不变量是
    **`ProtocolServer.attach()` 之前不可能收到任何请求**（只有 runtime 会发请求，
    而 runtime 要等本函数返回）。完整来龙去脉见 `runtime/channels.py` 的 docstring。

    ## 六个注入点

    见 README「第 1 条设计原则」：`asker` / `memory` / `on_checkpoint` / `on_event` /
    `session_notes` / `should_stop`。其中 `channels`（asker + questioner）由调用方给，
    其余四个由本函数造或直接引现有对象。

    ## 四个 `*_config` 只在测试里传

    默认全部从它们该在的地方读（环境变量 / `.env` / `.tudouni/permissions.json` /
    用户级 `mcp.json`）。让它们可注入是为了"不改环境变量就能验装配"—— 否则想测一条
    装配路径就得先设一个假 API key 并祈祷用户机器上没配真密钥（那会让测试真的去连
    一次网关）。

    ## 失败时自己收摊

    这个函数会起一个 httpx client 和一个 MCP 子进程，而"注册外部工具时撞名"这类失败
    发生在它们之后 —— 抛出去之前必须把它们收掉，否则一次启动失败会留下一个活着的
    node 进程。这是原来的 `main.py` 就做对了的事，这里保持。
    """
    cfg = model_config or ModelConfig.from_env()
    # 权限策略和密钥一起在这里读：两类配置错误都是「用户得先做点事」，都该在开出
    # 会话之前停下，而不是跑到第一次工具调用才炸。
    permissions = permission_config or PermissionConfig.from_file()
    # 联网工具的密钥**不在这一档**：缺了只是少一个工具，不是"什么都干不了"。
    web = web_config or WebConfig.from_env()
    # 外部 MCP server 的**清单**在这一档：文件里写错一个键名就停下。但"清单是空的"
    # 或"某个 server 起不来"不在这一档 —— 前者是默认状态，后者只是少一批工具。
    # 这一条界线就是"用户得先做点事"和"少一个能力"的界线。
    mcp_cfg = mcp_config or McpConfig.from_file()

    # 这个会话用哪条路由上的哪个模型、想得多用力：**会话级选择优先，目录的默认值兜底**。
    #
    # 判据取 "这个会话选过吗" 而不是 "配置里是什么"：`/model` 之后恢复会话的人期待
    # 还是他选的那个（和任务列表、已加载技能同一条路 —— 它们都住在 session.metadata
    # 里，所以它们一起过期、一起恢复）。
    #
    # 它在这里解析、而不是在 Agent 里：装配期要拿着**解析出来的那条路由**（base_url +
    # 密钥）去造适配器，而 Agent 手上只需要"想用谁 / 上一轮用了谁"那份状态。
    #
    # `fallback` 用配置里那个模型名（`DEEPSEEK_MODEL`，没写就是内置的默认值）：它是
    # **兜底**，不是权威 —— 权威是解析出来的 `chosen`（它可能落在另一条路由上）。
    session_model = model_state.SessionModel.restore(
        session.metadata, fallback=cfg.model,
    )
    model_registry = catalog_config or catalog.load()
    session_model, chosen = resolve_model(session_model, model_registry)
    if chosen is None:
        raise ConfigError(_no_model_message(model_registry))

    model = OpenAICompatibleModel(
        api_key=chosen.provider_key,
        base_url=chosen.provider_base_url,
        model=chosen.id,
        http_client=httpx.Client(),
        provider=chosen.provider,
        # 会话级的思考设置（`/thinking` `/effort` 写的那个）。
        thinking=session_model.thinking,
        effort=session_model.effort,
    )

    # 联网抓取用的 http client：**一个进程一个**，连接复用、TLS 握手只付一次。
    #
    # trust_env=False：环境变量里的 HTTP_PROXY 不该悄悄改掉这个程序的行为 ——
    # 和 config 里"环境变量优先、但方向不能反"是同一个担心的两半。要代理就显式构造
    # 一个 client 传进来。
    http = httpx.Client(trust_env=False, headers={"User-Agent": USER_AGENT})

    mcp: McpToolset | None = None

    # 后台任务那张表。**它只能在会话定下来之后造**（输出目录带会话 id），而且它是
    # 这个装配里唯一**攥着进程**的东西 —— 所以它必须被 close() 收掉（见 Runtime.close）。
    #
    # 它在 try **外面**：`JobBoard.__init__` 会去清上一次留下的输出文件，而那件事
    # 不碰任何进程、也不该因为后面装配失败而回滚（清掉是对的，留着才是垃圾）。
    job_board = JobBoard(
        project_dir(),
        jobs_dir(project_dir() / RUNTIME_DIR_NAME, session_id),
        show_root=f"{RUNTIME_DIR_NAME}/{JOBS_DIR_NAME}/{session_id}",
    )

    try:
        tools = create_tool_registry(
            str(project_dir()),
            # 提问通道和审批通道**分开装配**：审批回答"要不要执行"，它的答案改变权限；
            # 提问回答"你要什么"，它的答案只是内容。两者唯一的共同点是"都需要有人在" ——
            # 而 --autopilot 说的正是这件事本身，所以它同时管住两者。
            questioner=channels.questioner,
            # 任务列表是**按会话的状态**，所以它只能在这里造（会话已经定下来了），而且
            # 拿到的是 session.metadata 这个活字典 —— 写进去的东西跟着会话一起落盘。
            todos=TodoBoard(session.metadata),
            # 联网那一对。**密钥的读取留在装配这一层**（tools/ 不能 import config，
            # 依赖方向是单向的）—— 和 questioner / todos 走的是同一条路。
            web_fetch=WebFetch(http),
            web_search=(
                WebSearch(
                    TavilySearch(http, web.tavily_api_key, web.tavily_base_url),
                    provider="tavily",
                )
                if web.tavily_api_key
                else None
            ),
            # 技能。和 todos 一样是**按会话的状态**（加载了哪个技能存在 session.metadata
            # 里），所以只能在这里造 —— 但造出来的那个 SkillBoard 留在注册表的
            # `tools.skills` 上，载荷尾部那段渲染从那里取回**同一个**对象。
            #
            # 自己再 new 一个的后果很隐蔽：那个副本会带着另一个重扫口，于是"技能加载
            # 成功了、却永远不出现在载荷里"—— 没有异常、没有审计痕迹（tools/tool.py
            # 里那段写了为什么；tests 里那条 test_the_note_never_enters_session_messages
            # 就是盯着它的）。
            #
            # 一个技能都没有时传 None，create_tool_registry 因此**不注册** load_skill
            # —— 和缺 TAVILY_API_KEY 不注册 web_search 同一条路。
            skills=booted.skill_catalog if booted.skill_catalog.skills else None,
            skill_metadata=session.metadata,
            skill_loader=booted.skill_loader,
            # 后台命令那一组。和 todos 一样是按会话的状态，但它攥着进程 ——
            # 所以只有它多一条"会话结束时必须收掉"的义务（见 Runtime.close）。
            jobs=job_board,
        )

        # 外部 MCP server：连上、列工具、注册进同一个注册表。
        #
        # **它为什么不进 create_tool_registry 的参数表**（questioner / todos / web_*
        # 都在那儿）：那边的每一个参数都是"一个可以随注册表一起造出来的协作者"，
        # 而 MCP 带着**进程生命周期**（连上 → 每次工具调用 → 关闭），并且它的失败是
        # **每个 server 各自的**（一个起不来只是少一批工具）。所以它由 Runtime 持有，
        # 这里只是往注册表里放工具。
        #
        # 位置也不能再晚：下面那条 `unknown_tools` 会拿"已注册的工具名"去核对权限
        # 文件，而 mcp__… 这些名字必须已经在里面（否则在 permissions.json 里点名放行
        # 一个外部工具的人会收到一句"这个工具没有注册，规则不会生效"的假警告）。
        mcp = McpToolset.connect(mcp_cfg.servers, on_problem=_warn)
        for tool in mcp.tools:
            tools.register(tool)

        # 只自动放行名单里列出的等级，其余一律弹审批（缺省 low）。
        policy = PermissionPolicy(
            auto_approve=permissions.auto_approve,
            deny_tools=permissions.deny_tools,
        )

        # 人按 t 记下的东西：工具名，以及命令前缀（shell 那种"一条命令一个样"的粒度）。
        # 落盘那一半是注入进来的 —— memory 自己不碰文件，所以它在测试里是纯内存的。
        #
        # 造法来自通道那一包（`Channels.memory_factory`）：协议版和 CLI 版需要的是
        # **同一个** memory（协议版的 asker 要读它），所以"怎么造"只写一份。
        memory = resolve_memory_factory(channels)(permissions)

        # 审批里那个 a（信任一整个 MCP server 的全部工具）的接线。
        #
        # 连接那一层（tools/mcp.py）只报事实 —— "这些工具同属一个 server"；"怎么把它们
        # 一起放行"是审批那一层的事（security/asker.py 的 TrustGroup）。两边都不认识
        # 对方的类型，接起来的地方就在这里。
        def mcp_trust_group(tool_name: str) -> TrustGroup | None:
            pair = mcp.group(tool_name)
            if pair is None:
                return None
            server, names = pair
            # 只有一个工具时不提供 a：那个按键的效果和 t 完全一样，而多一个键只会让人
            # 多读一行提示。
            if len(names) < 2:
                return None
            return TrustGroup(
                label=f"MCP server {server} 的 {len(names)} 个工具", tools=names,
            )

        # asker 由通道那份工厂造 —— 它要 memory 和 trust_group，而那两个刚刚才造出来。
        # 协议那一侧的实现在**被调用时**会去 `runtime.memory` / `runtime.mcp_trust_group`
        # 取（见 protocol/channels.py），所以这里不需要额外接线。
        asker = channels.asker_factory(memory, mcp_trust_group)

        # 六个注入点，同一个原则：判定留在 Agent 内部，执行交给注入的实现。
        # （提问通道不在这个表里，但它在上面装配工具时就注入了 —— 它不属于 Agent：
        # Agent 只看见一次普通的工具调用，ask_user 会不会阻塞在人的输入上，
        # 它不知道也不需要知道。）
        #
        # 流式是第七个：`on_delta` 为 None 就是不流式（模型一次返回完整响应）。
        # **两个条件都要满足**才有流：`stream` 是这次运行的意图（`--stream` /
        # `--no-stream`），`on_delta` 是"往哪儿送"（协议版由 ProtocolServer 提供）。
        # 只看 `stream` 的话，CLI 那个直连的前端会拿到一堆无处可去的回调；
        # 只看 `on_delta` 的话，`--no-stream` 就再也关不掉了。
        agent = Agent(
            model, tools, policy,
            asker=asker,
            memory=memory,
            on_checkpoint=booted.store.save,
            on_event=booted.logs,
            on_delta=on_delta if stream else None,
            # 会话状态每轮都要重新贴在请求末尾（当前状态，不是让模型去翻历史找最近
            # 那一版）。注入的是一段"怎么说"的实现：Agent 自己不知道技能和任务列表
            # 长什么样 —— 它只知道"每次请求末尾要把当前会话状态贴上"。
            session_notes=_session_notes(tools, booted.skill_catalog, job_board),
            debug=debug,
            # autopilot 只管审批那一关：工作区边界、控制面写入、拒绝名单都在它管不着
            # 的地方，所以它不是"关掉权限"，只是"这一轮没人可问"。
            autopilot=autopilot,
            # 第六个注入点：**"要不要停"由外面决定，怎么停在 Agent 里。**
            # 为 None 表示"这一轮没有取消这回事"（CLI、测试）。
            should_stop=should_stop,
            # 第八个注入点：这个会话选的是哪个模型。Agent 用它做两件事 ——
            # 换过模型时在历史里留一句话、每轮记下"实际用了谁"。
            session_model=session_model,
        )
    except Exception:
        # 装配失败时**必须把这些收掉**再往外抛：http client 是我们建的，而 MCP 的
        # server 是**我们起的子进程** —— 它们都不在调用方的清理路径上。
        if mcp is not None:
            mcp.close()
        # 模型适配器现在**可能**已经造过一个 SDK 客户端（它是懒造的：`complete()`
        # 之前不会）。这条路走到的概率很低（装配失败通常发生在它之前），但漏了就是
        # 一个没人认领的连接池 —— 而它正是这一整个 try 块存在的理由。
        model.close()
        http.close()
        raise

    runtime = Runtime(
        model_cfg=cfg,
        web_cfg=web,
        mcp_cfg=mcp_cfg,
        permissions=permissions,
        model_registry=model_registry,
        session_id=session_id,
        session=session,
        store=booted.store,
        logs=booted.logs,
        skill_loader=booted.skill_loader,
        skill_catalog=booted.skill_catalog,
        tools=tools,
        policy=policy,
        memory=memory,
        agent=agent,
        autopilot=autopilot,
        debug=debug,
        stream=stream,
        resumed=resumed,
        # 审批里那个 `a` 的查询口只有装配期才知道（它要 MCP 的连接），而协议版的
        # asker 在**被调用时**才需要它 —— 所以它跟着装配产物一起出去，
        # 由协议那一侧从 `attach()` 到的 runtime 上取（见 protocol/channels.py）。
        mcp_trust_group=mcp_trust_group,
        _http=http,
        _mcp=mcp,
        _jobs=job_board,
    )
    return runtime


def _session_notes(
    tools: Any, skill_catalog: SkillCatalog, jobs: JobBoard | None = None
) -> Callable[[Any], str]:
    """载荷尾部那段会话状态：技能目录 + 已加载技能的正文 + 任务列表 + 后台任务。

    合成**一条**临时消息（Agent 里 `_status_note` 负责合成，这里只负责"这一段说
    什么"）。顺序是刻意的，而且它只在这一个地方定：先目录（有哪些能用），再正文
    （现在该按哪份做），然后任务列表（做到哪了），最后后台任务（有哪些还悬着）。
    倒过来的话，模型会先读到一份"还剩什么活"的清单，再读到"该怎么做" —— 而它做决策
    的瞬间需要的是后者。

    **后台任务排在最后是有理由的**：它是四段里唯一"不看就会出错"的一段（把一条还在跑
    的命令当成已经成功，是这个功能唯一会静默出错的地方）。排在末尾意味着它离模型要
    生成的那个 token 最近 —— 而载荷末尾正是整段对话里单价最贵、也是唯一该变化的位置。

    读的必须是 `tools.skills.catalog`（注册表上那个 board）而不是启动时那份快照：
    board 每次读都会重扫目录 —— 所以中途新加的技能下一轮就会出现在清单里，而且和
    "能不能加载"读到的是同一份事实。重扫在这个函数里**只做一次**（读到局部变量再
    分别渲染两段）：它每次读盘都会把每个技能文件读一遍，而它每一步都会被调一次。

    四段都**不进 session.messages**（逐轮变化的东西不持久化，见 `agent._status_note`），
    所以它必须只读 metadata + 技能目录 + 那张任务表，不做别的事。

    `jobs` 是**唯一一个不是从 metadata 读出来的**：它攥着进程，落不了盘。所以它是从
    装配那一层直接传进来的（和 `tools` 一样），而不是像任务列表那样从 metadata 里取。
    """
    def notes(metadata):
        board = tools.skills
        catalog = board.catalog if board is not None else skill_catalog
        return "\n\n".join(filter(None, (
            catalog_part(metadata, catalog),
            skill_note(metadata, catalog),
            todo_note(metadata),
            job_note(jobs),
        )))

    return notes


__all__ = [
    "Booted", "Channels", "Notice", "Runtime",
    "boot", "check_session_id", "open_runtime", "project_dir", "resolve_session",
    "session_summaries",
]
