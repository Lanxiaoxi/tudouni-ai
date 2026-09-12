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
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

# 信封版本。**每条消息都带**，两端各自检查。
#
# 它和 `init.protocol` 是两件事，别合并：v 是"这一行的信封长什么样"（能不能解析），
# protocol 是"这一整轮会话谈定的语义版本"（能不能对上话）。分开之后，将来在 v 不变
# 的情况下演进语义是可能的 —— 而那是迟早的事。
VERSION = 1

# 入站（前端 → runtime）七种。
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
IN_SHUTDOWN = "shutdown"

INBOUND = (IN_USER_MESSAGE, IN_PERMISSION_RESPONSE, IN_QUESTION_RESPONSE,
           IN_SESSION_SWITCH, IN_SESSION_LIST, IN_INTERRUPT, IN_SET_AUTOPILOT,
           IN_SHUTDOWN)

# 出站（runtime → 前端）九种。
OUT_INIT = "init"
OUT_SESSION_LOAD = "session_load"
OUT_EVENT = "event"
OUT_UI = "ui"
OUT_NOTICE = "notice"
# 已保存会话的清单。**它只回答入站的 `session_list`**，不是"每一次启动都发"的东西。
OUT_SESSIONS = "sessions"
OUT_PERMISSION_REQUEST = "permission_request"
OUT_QUESTION_REQUEST = "question_request"

OUTBOUND = (
    OUT_INIT, OUT_SESSION_LOAD, OUT_EVENT, OUT_UI,
    OUT_NOTICE, OUT_SESSIONS, OUT_PERMISSION_REQUEST, OUT_QUESTION_REQUEST,
)

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

# `t:"ui"` 的两种 `kind`。**它和 `t:"event"` 是两条通道**：event 是审计的原样转发
# （进 jsonl），ui 只给界面、不进审计。
#
#   * `run_finished` —— 这一轮的最终答案（审计里没有正文，所以它是界面唯一的来源）；
#   * `state` —— 面板数据快照（任务列表 / 已加载技能 / 会话规模）。
#     它同样**不进审计**：任务列表的变化在审计里已经有 `tool_call` 那条参数了，
#     再写一份就是同一份事实的第二个来源。
UI_RUN_FINISHED = "run_finished"
UI_STATE = "state"

UI_KINDS = (UI_RUN_FINISHED, UI_STATE)


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
