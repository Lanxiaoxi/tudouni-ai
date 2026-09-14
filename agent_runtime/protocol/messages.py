"""协议里的字面量，以及"形状的权威在哪"。

## 三件东西，一份事实

  * `schema/*.schema.json` —— **形状的权威**（机器可读：字段名、类型、枚举）；
  * 这个模块 —— 从 schema 读出来的名字和常量，运行时用它编解码；
  * `doc/protocol.md` —— **语义**：什么时候发、收到怎么办、错了怎么办。

**三份东西，但只有一处权威。** 字段名和枚举值只在 schema 里定义一次；这个模块
把要用的那几个读进来变成常量；而 `doc/protocol.md` **不许抄字段表** —— 抄了就是
第三个来源，而它会漂（而且是那种"看着都对"的漂）。

将来加 Web 前端时的分工：TS 那一侧的类型由脚本**从同一份 schema 生成**
（`scripts/gen_ts.py`，第二期之后的事），不是手抄。手抄的话连"字段名比对测试"
都做不了 —— 一个 Python 测试读不到 TS 的类型。

## 为什么用 JSON 而不是在 Python 里定义 dataclass

因为**另一个读者不是 Python**。形状如果只存在于 Python 的 dataclass 里，Web 那一侧
就只能靠人去读源码再手写一遍类型 —— 那正是这一节要避免的事。
"""

import json
from typing import Any

from agent_runtime import paths

# 协议 schema 住在哪。**走 `paths`，不自己算 `__file__`。**
#
# 这两者在源码目录里恰好等价，所以自己算一版本来看不出任何问题 —— 而冻结成可执行文件
# 之后就不等价了：数据文件被搬到 `_MEIPASS` 下面，`__file__` 却指向别处。那时候的症状是
# 协议校验路径没得可校，属于"能起来但悄悄少了一层"。同一件事只有一份算法，是 `paths.py`
# 立在那里唯一的理由。
SCHEMA_DIR = paths.package_dir() / "protocol" / "schema"

# 信封版本。**每条消息都带**，两端各自检查。
#
# 它和 `init.protocol` 是两件事，别合并：v 是"这一行的信封长什么样"（能不能解析），
# protocol 是"这一整轮会话谈定的语义版本"（能不能对上话）。分开之后，将来在 v 不变
# 的情况下演进语义是可能的 —— 而那是迟早的事。
VERSION = 1

# 这一整轮会话谈定的**语义版本**（`init.protocol`）。它和 `VERSION` 是两件事：
# `VERSION` 是"这一行的信封长什么样"（每条消息都带，能不能解析），这个是"我们能
# 对到哪一步话"。分开之后，将来在信封不变的情况下演进语义是可能的 —— 而这件事
# 已经发生过一次了：
#
#   2 = 多两条出站消息 `t:"delta"` / `t:"delta_reset"`，以及 `init.stream`。
#
# **不认识 `t:"delta"` 的老客户端什么都不用改**：按协议约定忽略不认识的 `t` 即可，
# 而 `ui(run_finished).answer` 照旧发一份完整的（见 `protocol/channels.py` 的
# `_run_turn`）—— 所以"答案"这条老路一直是通的，delta 只是让它更早出现。
PROTOCOL = 2

# 入站（前端 → runtime）十四种。
IN_USER_MESSAGE = "user_message"
IN_PERMISSION_RESPONSE = "permission_response"
IN_QUESTION_RESPONSE = "question_response"
# **原地换一个会话**（TUI 的 `/new` 和 `/resume`）。它和"shutdown + 重开进程"是
# 同一件事的两条路，而这条路让**界面进程活着** —— 会话流、左栏、输入框都在原位刷新。
#
# 它不能和 `user_message` 合并：换会话不产生任何用户消息，而它的副作用（收掉当前
# runtime、按新会话重新装配）比"说一句话"大得多。
IN_SESSION_SWITCH = "session_switch"
# 请 runtime 回一份会话清单（出站 `sessions`）。**前端不许自己去读
# `.tudouni/sessions/`** —— 那会让目录布局变成前端也认识的一件事实，而 store 的
# 实现是明确留着"将来换 SQLite"的余地的（state/store.py 的类 docstring）。
IN_SESSION_LIST = "session_list"
# **中断当前这一轮**（Esc）。它和 `shutdown` 是两件事，这一点是实测踩出来的：
# `shutdown` 的语义是"收摊"，而它**不取消**当前回合（否则客户端发完 user_message
# 紧跟一条 shutdown，那一轮会在第一个安全点被砍掉，界面永远拿不到答案）。
# 想停下正在跑的这一轮，只能走这条。
IN_INTERRUPT = "interrupt"
# **运行中开关 autopilot**（TUI 的 `/autopilot`）。它和 `--autopilot` 是同一个模式，
# 区别只在"什么时候决定"：那个在启动时定死，这个让界面在会话中途改。
#
# 为什么值得单独一条消息（而不是让前端自己"假装放行"）：放行这件事只有 runtime 能做
# —— gate 是它调的，审计也是它写的。前端自己把 `permission_request` 回成 allow，审计
# 里记的就是 `approved`（"人按了同意"），而那一刻其实没有人按过任何键 ——
# 那是**审计谎报**，比多一条消息贵得多。
IN_SET_AUTOPILOT = "set_autopilot"
# **换这个会话用哪个模型**（TUI 的 `/model`）。它和 `set_autopilot` 是同一类东西：
# 一条"用户改了运行中的某个选择"的消息，runtime 处理后回一份 state 快照。
#
# 为什么要单开一条（而不是让前端自己挑一个模型名去发请求）：模型名必须过**目录**那道
# 校验（`state/model.py`），而目录是 runtime 的知识。前端拿到什么就发什么的话，
# "选了一个它没列出来的模型"会一路走到下一次请求才炸。
IN_SET_MODEL = "set_model"
# **开关思考模式**（TUI 的 `/thinking`）和**改思考强度**（`/effort`）。
#
# 它们和 `set_model` 是同一类东西（"用户改了运行中的某个会话级选择"，runtime 处理完
# 回一份 state 快照），但**分成了两条消息**而不是给 `set_model` 加参数：三个旋钮
# 互不影响（关掉思考不清强度、换模型不改开关），而合成一条之后"只改其中一个"就得
# 靠"没传的那个字段表示不动它"来表达 —— 那是一种每个客户端都要记住的约定。
IN_SET_THINKING = "set_thinking"
IN_SET_EFFORT = "set_effort"
# **请 runtime 回一份状态**（`/status`）。它的答案走 `t:"ui", kind:"status"` —— 
# 状态是"给界面看的"，和 `ui` 那条通道的性质一致。
#
# 为什么不把它塞进每一次 `ui(state)` 快照里：那一屏要读审计日志（几十~几百行），
# 而快照是每次工具返回都发的。用户按一次 `/status` 读一次文件，那才是对的频率。
IN_STATUS = "status"
# **请 runtime 回一份工具清单**（`/tools`）。和 `IN_STATUS` 同一条路：
# 按需发，答案是 `t:"ui", kind:"tools"`。
IN_TOOLS = "tools"
# **看/改 MCP server 的挂载情况**（TUI 和 CLI 的 `/mcp`）。
#
# 一条消息带三个动作（`MCP_LIST` / `MCP_LOAD` / `MCP_UNLOAD`），而不是三条 —— 它们
# 的**回包完全相同**（一份全量清单），分成三条就等于三个 kind、三段两端各自维护的
# 字段表。`action` 那一格是枚举：认不出来的值当场回一句 notice，不是猜。
#
# ## 一条硬约束：它只由人按键触发
#
# 它是唯一能改"模型看得到什么工具"的入站消息。所以前端**不许**在启动、回合结束、
# 或者收到什么消息时顺手自己发一条 —— 那会让"配置自己变宽"变成可能，而那正是
# `mcp.json` 只读用户级要防的事（见 tools/mcp.py 的模块 docstring）。用户按一次
# 键 = 一次明确的授权决定，和 `/model` 换模型是同一档。
IN_MCP = "mcp"
# 那三个动作。
MCP_LIST = "list"
MCP_LOAD = "load"
MCP_UNLOAD = "unload"

MCP_ACTIONS = (MCP_LIST, MCP_LOAD, MCP_UNLOAD)
# **请 runtime 回一份面板快照**（出站的 `ui`, kind:"state"）。**空消息，没有参数。**
#
# 它和上面两条的"按需"不是一回事，所以值得说清它为什么存在：`ui(state)` 本来只在
# 几条由**交互**触发的时刻发（开场、每次 tool_result、回合收尾、几条命令之后），
# 而**后台任务会在没人在看的时候改变状态** —— 一段安静时间里一条两分钟的命令跑完了，
# 面板却还写着"在跑"，而那句话是假的（和"把已启动当成已成功"是同一类错误，只是方向
# 相反）。所以让前端来问一次，而不是给 runtime 加定时器或监视线程：后台任务那套东西
# 刻意做到"零后台线程"。
IN_REFRESH_STATE = "refresh_state"
IN_SHUTDOWN = "shutdown"

INBOUND = (IN_USER_MESSAGE, IN_PERMISSION_RESPONSE, IN_QUESTION_RESPONSE,
           IN_SESSION_SWITCH, IN_SESSION_LIST, IN_INTERRUPT, IN_SET_AUTOPILOT,
           IN_SET_MODEL, IN_SET_THINKING, IN_SET_EFFORT, IN_STATUS, IN_TOOLS,
           IN_MCP, IN_REFRESH_STATE, IN_SHUTDOWN)

# 出站（runtime → 前端）十一种。**`/status` 和 `/tools` 不在这里** —— 它们复用
# `t:"ui"` 那条通道（多两种 `kind`），因为它们是"给界面看的东西"，性质和面板快照
# 完全一样。加一条出站消息类型意味着两端的信封都要改，而这件事不值得为一个 kind 做。
OUT_INIT = "init"
OUT_SESSION_LOAD = "session_load"
OUT_EVENT = "event"
OUT_UI = "ui"
OUT_NOTICE = "notice"
# 已保存会话的清单。**它只回答入站的 `session_list`**，不是"每一次启动都发"的东西。
OUT_SESSIONS = "sessions"
OUT_PERMISSION_REQUEST = "permission_request"
OUT_QUESTION_REQUEST = "question_request"
# **流式增量**：模型正在吐的正文/思考链，一块一条。
#
# 为什么不走 `t:"event"`（那样"多一种 kind"就够了）：事件那条流**同时**进审计
# （`.tudouni/logs/<id>.jsonl`），而一次回答是上千块 —— `JsonlSink` 每条事件一次
# open/write/close，抄进去等于把审计日志变成第二个会话文件，"事后能完整回放"这件事
# 也就被稀释了。所以 delta 是一级独立的消息：**只走协议、不进审计**，审计里记的是
# 汇总（`model_call.streamed` / `stream_chunks` / `streamed_chars`）。
OUT_DELTA = "delta"
# **把已经画出来的增量丢掉**：一次重试、或者适配层自己重发（网关拒绝
# `stream_options`）之前发一条。不丢的话，界面上会是两段回答首尾相接 ——
# 而它看起来完全像模型"说了两遍"，不像协议出过问题。
OUT_DELTA_RESET = "delta_reset"

OUTBOUND = (
    OUT_INIT, OUT_SESSION_LOAD, OUT_EVENT, OUT_UI,
    OUT_NOTICE, OUT_SESSIONS, OUT_PERMISSION_REQUEST, OUT_QUESTION_REQUEST,
    OUT_DELTA, OUT_DELTA_RESET,
)

# `t:"delta"` 的两条通道。**按字段名分流**，不然思考链会混进正文里
# （`text` 和 `reasoning` 都是字符串，按位置取值一次就会对调）。
DELTA_TEXT = "text"
DELTA_REASONING = "reasoning"

# 审批的三个答案。`always_group` 是**一次性的**（只对那一条请求有效）。
#
# 为什么前端只能回这个枚举、不能回一份工具名单：让客户端指定"放行哪些工具"就等于
# 让客户端能改策略（写 auto_approve_tools）。诚实的前端和恶意的前端在这一步没有
# 区别 —— 所以名单由 runtime 按 id 查它自己手里那份。和"控制面只有人能写"是同一个
# 担心，只不过这次要写的是权限文件。
ALLOW = "allow"
DENY = "deny"
ALWAYS = "always"
ALWAYS_GROUP = "always_group"

DECISIONS = (ALLOW, DENY, ALWAYS, ALWAYS_GROUP)

# `t:"ui"` 的五种 `kind`。**它和 `t:"event"` 是两条通道**：event 是审计的原样转发
# （进 jsonl），ui 只给界面、不进审计。
#
#   * `run_finished` —— 这一轮的最终答案（审计里没有正文，所以它是界面唯一的来源）；
#   * `state` —— 面板数据快照（任务列表 / 已加载技能 / 会话规模 / 当前模型）。
#     它同样**不进审计**：任务列表的变化在审计里已经有 `tool_call` 那条参数了，
#     再写一份就是同一份事实的第二个来源；
#   * `status` —— `/status` 那一屏（回答入站的 `status`）；
#   * `tools` —— `/tools` 那份清单（回答入站的 `tools`）；
#   * `mcp` —— `/mcp` 那份清单（回答入站的 `mcp`）。
#
# 后三种**只在被问的时候才发**（它们要读审计日志、要遍历工具注册表、要问 MCP 宿主），
# 而 `state` 是"每次工具返回都补一份"的常驻快照。这个区别就是它们为什么不合并成
# 一种 kind。
#
# `tools` 和 `mcp` 看起来像同一件事的两半（都是"有哪些工具"），所以为什么是两个
# kind 要说清：**问的人不同**。`tools` 回答"这个工具会不会问我"（策略与记忆），
# `mcp` 回答"哪个 server 在跑"（生命周期与连接）。合成一个的话，每次 `/mcp load`
# 都要把几十行权限清单重发一遍。
UI_RUN_FINISHED = "run_finished"
UI_STATE = "state"
UI_STATUS = "status"
UI_TOOLS = "tools"
UI_MCP = "mcp"

UI_KINDS = (UI_RUN_FINISHED, UI_STATE, UI_STATUS, UI_TOOLS, UI_MCP)


def load_schema(name: str) -> dict[str, Any]:
    """读一份 schema（`"inbound"` / `"outbound"`）。给测试和生成脚本用。"""
    path = SCHEMA_DIR / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def field_names(schema_name: str, message: str) -> set[str]:
    """某条消息的字段名集合（去掉 `$` 开头的注释键）。

    测试拿它和实际发出去的消息对表 —— 这是"schema 是权威"唯一能自动化的那一半：
    一个字段改了名而 schema 没跟上，测试红。语义那一半没法自动比对（那是散文），
    所以它靠"doc/protocol.md 不抄字段表"这条纪律来防。
    """
    spec = load_schema(schema_name)[message]["fields"]
    return {k for k in spec if not k.startswith("$")}


def required_fields(schema_name: str, message: str) -> set[str]:
    return set(load_schema(schema_name)[message]["required"])
