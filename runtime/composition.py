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
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state import agents_md
from agent_runtime.state import model as model_state
from agent_runtime.state.session import is_valid_session_id
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.grep import host_triple, rg_binary
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
        f"  因为它会被拿去拼文件名（{RUNTIME_DIR_NAME}/sessions/<id>.json 和"
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
        # 合法性校验）。自己拼 `directory / f"{id}.json"` 就是同一件事的第二个说法，
        # 而它漂掉的那天，症状是"列表里的时间和实际文件对不上"——没人查得出来。
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

    # `/model` 那张清单（`state/model.py` 的目录）。它排在装配出来的零件之后，
    # 因为有默认值：加一个模型是改那张表，不该逼每一个构造点都跟着改一行。
    #
    # **它是快照，而"现在用的是哪个"不是** —— 后者读 `agent.model_name`（见
    # `current_model`）。把当前模型也存成字段就有了两份事实，而它们分家的症状是
    # "状态栏写着 flash，请求发给了 pro"。
    model_catalog: tuple[Any, ...] = model_state.MODEL_CATALOG

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

    # -- 模型（`/model`）--------------------------------------------------------

    @property
    def current_model(self) -> str:
        """现在真正在用的模型名。**从 Agent 上读，不留第二个字段。**

        空串 = 适配器没报出模型名（`ChatModel` 的契约里没有这个属性）。调用方按
        "不知道"处理：显示成空、且不报上下文占比。
        """
        return self.agent.model_name

    @property
    def context_tokens(self) -> int | None:
        """**当前模型**的上下文窗口（分母）。

        它从 `current_model` 派生，**不是一个存下来的字段** —— `/model` 能中途换模型，
        存字段就意味着换完之后这里还是旧的那个，而症状是状态栏那个百分比按旧窗口算：
        看起来完全正常，只是数错了。

        目录里没有这个名字时返回 None（`--model` 指向自建网关的模型、或者那个名字
        已经下线）：只报用量、不报占比，错的百分比比没有百分比更坏。
        """
        return model_state.context_window(self.current_model)

    def select_model(self, name: str) -> tuple[bool, str]:
        """换这个会话用哪个模型。返回 `(换成了吗, 说给用户听的一句话)`。

        ## 三道检查，顺序有意

          1. **名字认不认识** —— 判据是 `state/model.py` 的目录（`/model` 那个清单就是
             从它渲染的）。不认识就拒绝：接受一个目录外的名字等于让 `/model` 的清单
             变成一句谎话，而"选了一个它根本没列出来的模型"这件事没有任何地方会报；
          2. **网关对不对** —— 目录里的模型**共享同一把密钥与同一个 base_url**（见
             `state/model.py` 那一段）。一个指向自建网关的会话去选 DeepSeek 官方的
             模型名，请求会发到一个那台网关多半没有的模型上（或者更糟：发到官方端点、
             而密钥不是官方那把）。这一档里没有"provider"这个概念，所以只能这样挡：
             配置里的 base_url 和适配器上那个不一致就直接拒绝 —— 那说明装配时就
             已经分家了，是更该先修的问题；
          3. **适配器认不认** —— 见 `Agent.switch_model`。

        ## 落盘与生效的时序

        这里**只改内存 + `session.metadata`**：`/model` 之后用户可能一句话都不说就退出，
        而 `select()` 写的是一个活字典，下一次 checkpoint（或下一个回合开头）自然会带上它。
        写文件不是这一层的事（`store.save` 由 Agent 的 on_checkpoint 调）。

        模型名一样时**照样走完**并返回"已经是它了"：那是幂等的，而且用户按了两遍
        `/model flash` 该得到一句"已经是它"，不是一句错误。
        """
        wanted = (name or "").strip()
        if not wanted:
            return False, "没给模型名。/model 不带参数看清单。"

        known = model_state.get(wanted)
        if known is None:
            return False, (
                f"目录里没有这个模型：{wanted} —— /model 不带参数看清单。"
                f"（目录是写死的几个名字，不会把任意名字转给网关："
                f"那样打错一个字母只会在下一次请求时才炸。）"
            )
        model_id = known.id

        configured = str(getattr(self.agent.model, "base_url", "") or "")
        if configured and configured != self.model_cfg.base_url:
            return False, (
                f"这个会话的模型端点和配置对不上（{configured} ≠ "
                f"{self.model_cfg.base_url}），不换 —— "
                f"否则请求会发到一个没配密钥的地址上。"
            )

        if self.current_model == model_id:
            return True, f"已经是 {model_id} 了。"

        previous = self.current_model or "（未知）"
        if not self.agent.switch_model(model_id):
            return False, (
                f"这个会话的模型适配器不支持中途换模型（{type(self.agent.model).__name__}）"
                f"—— 只能重启时用 DEEPSEEK_MODEL 指定。"
            )

        # **立刻落盘，不等下一个检查点。** 换模型是用户的一次明确操作，而"会话级选择
        # 跟着会话走"这句话必须现在就成立：`/model` 之后一句话都不说就退出，是最自然的
        # 用法之一（我们先告诉过用户"下一次请求生效"，而恢复会话时说"你选的是 flash"
        # 会是同一份承诺的反面）。
        #
        # 此时此刻 messages 是**一致的**（没有人正在跑），所以这个落盘点和 Agent 那些
        # 检查点一样安全。失败不当成失败：选择还在内存里，这一个会话照用；只是它不会
        # 跨进程 —— 而那种情况必须说出来（和 `Agent._checkpoint` 那条规矩一致）。
        try:
            self.store.save(self.session)
        except Exception as exc:  # noqa: BLE001
            _warn(f"换模型之后落盘失败（这一次仍然生效，重开会话会回到配置里那个）："
                  f"{type(exc).__name__}: {exc}")

        # **没有第三步**：`switch_model` 里已经把"这个会话选的是谁"记进
        # `session.metadata` 了（那两件事分给两个方法是一条迟早会漏的约定，见那里的
        # 说明）。这里只剩回报 —— 而回报必须带上"上一个是谁"：那句话里同时有"上面
        # 那些轮次是谁写的"和"从这里开始是谁"，界面拼不出来（它不知道上一轮用了谁）。
        return True, f"换成 {model_id}（上一个：{previous}）—— 下一次请求生效。"

    def model_rows(self) -> list[dict]:
        """`/model` 那张清单：目录 + "现在用的是哪个" + "认下的旧名字"。"""
        current = model_state.canonical(self.current_model)
        rows = [
            {
                "id": item.id,
                "label": item.label,
                "window": item.window,
                "summary": item.summary,
                "note": item.note,
                "current": item.id == current,
            }
            for item in self.model_catalog
        ]
        # 别名（已下线、仍可调用的旧名字）单列：它们是**认下的名字**，不是能选的选项。
        # 放进主清单会摆出两个效果完全一样、价钱也一样的选项（官方明确说过旧名字由
        # V4.1-Flash 提供服务），而"我到底选了哪个"就没有答案了。
        aliases = [
            {"id": alias, "of": target}
            for alias, target in sorted(model_state.ALIASES.items())
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
                # 这个会话**选**的（`/model` 写的那个）。它和 `current` 在换完模型、
                # 下一次请求之前会暂时不同，而那个差别正是"还没生效"的证据。
                "selected": session_model.selected if session_model is not None else "",
                "last_used": session_model.last_used if session_model is not None else "",
                "since": session_model.selected_since if session_model is not None else 0.0,
                # 当前模型的窗口（分母）。None = 目录里没有这个名字，只报用量。
                "window": self.context_tokens,
                "base_url": self.model_cfg.base_url,
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
                text=f"[上下文] 模型 {self.current_model!r} 不在 state/model.py 的目录里，"
                f"末尾只报上下文用量、不报占比；把它的窗口长度加进那张表即可。"))

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

        # [模型]：**只在"这个会话选过模型"时说**。配置里那个（`.env` 的
        # `DEEPSEEK_MODEL`）不是新闻 —— 启动横幅和 `init.model` 都写着它，再说一遍
        # 就是噪音。而"恢复一个会话、它用的是你上次 `/model` 选的那个"必须说出来：
        # 不说的话，用户会以为模型跟着 `.env` 走，而账单上会是另一回事。
        if self.agent.session_model is not None and self.agent.session_model.selection is not None:
            selected = self.agent.session_model.selected
            where = self.model_cfg.base_url
            out.append(Notice("err", code="model",
                text=f"[模型] 这个会话选的是 {selected}（{where}）—— "
                     f"/model 可以换，/status 看现在这个。"))

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
        """**面板数据**：左栏（上下文栏）那四块里，会话状态那一半。

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
            （见 `tools/mcp.py` 的 `_terminate_tree`）。

        收摊本身失败不该盖住"任务本身"的结果，所以 http 那条吞掉异常并说一声 ——
        和 `McpToolset.close()` 里那条规矩一致。
        """
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

    # 这个会话用哪个模型：**会话级选择优先，配置里那个是兜底**。
    #
    # 判据取 "这个会话选过吗" 而不是 "配置里是什么"：`/model` 之后恢复会话的人期待
    # 还是他选的那个（和任务列表、已加载技能同一条路 —— 它们都住在 session.metadata
    # 里，所以它们一起过期、一起恢复）。
    #
    # 它在这里读、而不是在 Agent 里读：装配期的 `SessionModel` 要拿着它去造适配器
    # （模型名是构造参数），而 Agent 手上只需要"想用谁 / 上一轮用了谁"那份状态。
    session_model = model_state.SessionModel.restore(
        session.metadata, fallback=model_state.default_id(cfg.model),
    )

    model = OpenAICompatibleModel(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=session_model.selected,
        http_client=httpx.Client(),
    )

    # 联网抓取用的 http client：**一个进程一个**，连接复用、TLS 握手只付一次。
    #
    # trust_env=False：环境变量里的 HTTP_PROXY 不该悄悄改掉这个程序的行为 ——
    # 和 config 里"环境变量优先、但方向不能反"是同一个担心的两半。要代理就显式构造
    # 一个 client 传进来。
    http = httpx.Client(trust_env=False, headers={"User-Agent": USER_AGENT})

    mcp: McpToolset | None = None
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
            session_notes=_session_notes(tools, booted.skill_catalog),
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
        http.close()
        raise

    runtime = Runtime(
        model_cfg=cfg,
        web_cfg=web,
        mcp_cfg=mcp_cfg,
        permissions=permissions,
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
    )
    return runtime


def _session_notes(tools: Any, skill_catalog: SkillCatalog) -> Callable[[Any], str]:
    """载荷尾部那段会话状态：技能目录 + 已加载技能的正文 + 任务列表。

    合成**一条**临时消息（Agent 里 `_status_note` 负责合成，这里只负责"这一段说
    什么"）。顺序是刻意的，而且它只在这一个地方定：先目录（有哪些能用），再正文
    （现在该按哪份做），最后任务列表（做到哪了）。倒过来的话，模型会先读到一份
    "还剩什么活"的清单，再读到"该怎么做" —— 而它做决策的瞬间需要的是后者。

    读的必须是 `tools.skills.catalog`（注册表上那个 board）而不是启动时那份快照：
    board 每次读都会重扫目录 —— 所以中途新加的技能下一轮就会出现在清单里，而且和
    "能不能加载"读到的是同一份事实。重扫在这个函数里**只做一次**（读到局部变量再
    分别渲染两段）：它每次读盘都会把每个技能文件读一遍，而它每一步都会被调一次。

    三段都**不进 session.messages**（逐轮变化的东西不持久化，见 `agent._status_note`），
    所以它必须只读 metadata + 技能目录，不做别的事。
    """
    def notes(metadata):
        board = tools.skills
        catalog = board.catalog if board is not None else skill_catalog
        return "\n\n".join(filter(None, (
            catalog_part(metadata, catalog),
            skill_note(metadata, catalog),
            todo_note(metadata),
        )))

    return notes


__all__ = [
    "Booted", "Channels", "Notice", "Runtime",
    "boot", "check_session_id", "open_runtime", "project_dir", "resolve_session",
    "session_summaries",
]
