"""
Agent Runtime 主入口。

这个文件只做一件事：**把各个部件接起来**。怎么跟用户说话在 cli.py，配置从哪来在
config.py，重试策略在 agents/retry.py，权限裁决在 security/gate.py，审计落盘在
audit/。入口保持薄，是因为它的变化原因只有一个 —— 装配方式变了。
"""

import sys
from functools import partial
from pathlib import Path

import httpx

# 项目自身目录与它的父目录是两件事，不要共用一个变量：
#   - agent_runtime 是一个包，要能 `import agent_runtime`，
#     sys.path 必须包含【包所在目录】，也就是项目的父目录；
#   - agent 的文件工作区必须是【项目自身】，不能是父目录，
#     否则 read_file / write_file 会伸到同级的其它项目里去。
PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent

sys.path.insert(0, str(REPO_ROOT))

from agent_runtime.agents import Agent
from agent_runtime.audit import JsonlSink
from agent_runtime.cli import (
    build_parser,
    print_audit,
    print_banner,
    print_history,
    print_sessions,
    resolve_session,
    run_repl,
)
from agent_runtime.config import (
    PERMISSION_FILE,
    PERMISSION_FILE_NAME,
    ConfigError,
    ModelConfig,
    PermissionConfig,
    save_approvals,
)
from agent_runtime.models import OpenAICompatibleModel
from agent_runtime.security import ApprovalMemory, PermissionPolicy, cli_asker
from agent_runtime.security.commands import format_rule
from agent_runtime.state import JsonSessionStore
from agent_runtime.state.session import is_valid_session_id
from agent_runtime.tools.ask import cli_questioner, unavailable_questioner
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.todo import TodoBoard, progress_line, todo_note

SESSIONS_DIR = PROJECT_DIR / ".sessions"
LOGS_DIR = PROJECT_DIR / ".logs"


def report_permissions(policy: PermissionPolicy, memory: ApprovalMemory) -> None:
    """把这次启动生效的权限范围说出来。

    **每次启动都说一遍。** 「按一次 t 就永久生效」是最容易忘掉的那类设置，而这份
    文件攒上几条之后，光盯着它已经答不出"现在到底还有什么会问我"。

    走 stderr：它和横幅、提示符是一类东西（关于这次运行的说明），不是对话内容。
    """
    levels = ", ".join(sorted(policy.auto_approve)) or "（无）"
    named = ", ".join(sorted(memory.tools())) or "（无）"
    print(f"[权限] 按等级自动放行 {levels}；点名免问 {named}", file=sys.stderr)
    if policy.deny_tools:
        print(f"[权限] 直接拒绝 {', '.join(sorted(policy.deny_tools))}", file=sys.stderr)

    # 命令行规则单列一行：它是"按一次 t 记住哪条前缀"的产物，也是最容易被忘掉的一条 ——
    # 印象里只批准过一次 git add，而它此后一直静默生效。
    rules = ", ".join(format_rule(rule) for rule in sorted(memory.prefixes())) or "（无）"
    print(f"[权限] 命令规则（按前缀放行）{rules}", file=sys.stderr)


def report_todos(session) -> None:
    """恢复会话时，把当前任务列表说一遍。

    **它必须有，因为列表比进程活得久。** 任务列表存在会话文件里（`session.metadata`），
    所以恢复一个会话时，提示词里没有它、而历史里那一版可能已经是几十步之前的 ——
    不说的话，用户看到的会是"它怎么突然开始更新一个我从没见过的列表"。

    和 `report_permissions` 同一类东西：关于这次运行的既有状态，走 stderr。
    """
    line = progress_line(session.metadata)
    if line:
        print(f"[任务] {line}", file=sys.stderr)


def _check_session_id(session_id: str | None) -> str | None:
    """`--session` 是用户直接敲进来的字符串，写错了要能照着改。

    校验规则本身在 state/session.py（它描述的是"什么算合法会话 id"），而这条只负责
    把"不合法"翻译成一句人话。**必须在碰 store 之前**做，否则 ValueError 会从
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
        f"  因为它会被拿去拼文件名（.sessions/<id>.json 和 .logs/<id>.jsonl）。\n"
        f"  用 --list 看一下有哪些现成的 id。"
    )


def main() -> int:
    """返回退出码：配置缺失是"用户得先做点事"，脚本调用方应该能看出失败。"""
    args = build_parser().parse_args()

    # 会话 id 的合法性在这里一次查清，早于任何会碰它的东西。--list 不看这个参数，
    # 但传了非法值仍然报错 —— 一个地方查一次，比让三条子命令各自去猜自己会不会用
    # 到它可靠。
    if (problem := _check_session_id(args.session)) is not None:
        print(problem, file=sys.stderr)
        return 2

    store = JsonSessionStore(SESSIONS_DIR)
    logs = JsonlSink(LOGS_DIR)

    # ---- 不需要模型的子命令：先处理掉，这样没配密钥也能查历史/审计 ----
    if args.list:
        print_sessions(store)
        return 0

    if args.audit or args.history:
        if not args.session:
            flag = "--audit" if args.audit else "--history"
            print(f"{flag} 需要配合 --session <id>；先用 --list 看有哪些会话", file=sys.stderr)
            return 2
        if args.audit:
            print_audit(logs, args.session)
        else:
            print_history(store.load(args.session))
        return 0

    # ---- 以下需要模型 ----
    try:
        cfg = ModelConfig.from_env()
        # 权限策略和密钥一起在这里读：两类配置错误都是「用户得先做点事」，
        # 都该在开出会话之前停下，而不是跑到第一次工具调用才炸。
        permissions = PermissionConfig.from_file()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    # 横幅放在配置检查**之后**：密钥没配就退出的那种启动，不需要先看一幅图案。
    # 它也不属于 --list / --history / --audit 那三条路径 —— 那些子命令特意排在配置
    # 检查之前，为的是「没配密钥也能查历史」，跟"要开会话了"是两回事。
    print_banner()

    # 末尾那句统计里的"xx/yy"需要有 yy。响应里没有这个字段，所以它来自 config 里那张
    # 按模型名的表；表里没有就**只报用量、不报占比**（错的百分比比没有百分比更坏）。
    # 这句话只在真的缺分母时出现一次，而且它同时就是"该往哪加"的说明。
    if cfg.context_tokens is None:
        print(f"[上下文] 模型 {cfg.model!r} 不在 config.CONTEXT_WINDOWS 里，"
              f"末尾只报上下文用量、不报占比；把它的窗口长度加进那张表即可。",
              file=sys.stderr)

    session_id, session = resolve_session(store, args.session)

    model = OpenAICompatibleModel(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        http_client=httpx.Client(),
    )

    tools = create_tool_registry(
        str(PROJECT_DIR),
        # 提问通道和审批通道**分开装配**：审批回答"要不要执行"，它的答案改变权限；
        # 提问回答"你要什么"，它的答案只是内容。两者唯一的共同点是"都需要有人在" ——
        # 而 --autopilot 说的正是这件事本身，所以它同时管住两者：提问那一支拿到的是
        # unavailable（如实说没有人回答，**不伪造答案、也不记成默许**）。
        questioner=unavailable_questioner if args.autopilot else cli_questioner,
        # 任务列表是**按会话的状态**，所以它只能在这里造（会话上面刚解析出来），而且
        # 拿到的是 session.metadata 这个活字典 —— 写进去的东西跟着会话一起落盘。
        todos=TodoBoard(session.metadata),
    )
    print("已注册工具:")
    for tool in tools.all():
        print(f"  - {tool.name:12} 风险={tool.risk.value}")

    unknown = permissions.unknown_tools(tool.name for tool in tools.all())
    if unknown:
        # 把 shell 写成 shall 的人以为自己放行了。这是唯一能告诉他的地方 ——
        # 它不该拦启动，但绝不能不说。
        print(f"[权限] {PERMISSION_FILE.name} 里这些工具没有注册，规则不会生效："
              f"{', '.join(sorted(unknown))}", file=sys.stderr)

    # 只自动放行名单里列出的等级，其余一律弹审批。名单来自 .tudouni.json（缺省 low）。
    policy = PermissionPolicy(
        auto_approve=permissions.auto_approve,
        deny_tools=permissions.deny_tools,
    )

    # 人按 t 记下的东西：工具名，以及命令前缀（shell 那种"一条命令一个样"的粒度）。
    # 落盘那一半是注入进来的 —— memory 自己不碰文件，所以它在测试里是纯内存的。
    memory = ApprovalMemory(
        permissions.auto_approve_tools,
        on_change=lambda tools, prefixes: save_approvals(
            PERMISSION_FILE, tools=tools, prefixes=prefixes
        ),
        prefixes=permissions.shell_allow,
        label=PERMISSION_FILE_NAME,
    )
    report_permissions(policy, memory)
    report_todos(session)

    # autopilot 要在启动时大声说一次：它意味着接下来所有需要审批的工具都会**直接执行**，
    # 而这件事一旦忘了自己开着，事后看日志只会觉得"这个项目怎么什么都没问"。
    # 它也不做成配置项 —— 一次性的决定不该悄悄变成永久默认。
    if args.autopilot:
        print("[权限] autopilot：不询问任何审批，需要审批的工具会直接执行；"
              "也不会向你提问 —— 模型调 ask_user 会拿到「没有人回答」，"
              "并被告知自己决定、把假设说出来（拒绝名单、工作区边界、控制面写入仍然生效）",
              file=sys.stderr)

    # 四个注入点，同一个原则：判定留在 Agent 内部，执行交给注入的实现。
    # （提问通道是第五个，但它在上面装配工具时就注入了 —— 它不属于 Agent：Agent 只看见
    # 一次普通的工具调用，ask_user 会不会阻塞在人的输入上，它不知道也不需要知道。）
    agent = Agent(
        model, tools, policy,
        asker=partial(cli_asker, memory=memory),
        memory=memory,
        on_checkpoint=store.save,
        on_event=logs,
        # 任务列表每轮都要重新贴在请求末尾（当前状态，不是让模型去翻历史找最近那一版）。
        # 注入的是一段"怎么说"的实现：Agent 自己不知道任务列表长什么样。
        session_notes=todo_note,
        debug=args.debug,
        # autopilot 只管审批那一关：工作区边界、控制面写入、拒绝名单都在它管不着的地方，
        # 所以它不是"关掉权限"，只是"这一轮没人可问"。
        autopilot=args.autopilot,
    )
    print(f"审计日志写到 {logs.directory}\\{session_id}.jsonl")

    # logs 同时交给 run_repl：末尾那句累计用量是从审计日志里数出来的，传的是
    # **同一个** sink（也就是同一个 on_event）—— 换成别的东西就会报出另一套数字。
    # context_tokens 是那句话里 xxx/total 的分母。
    run_repl(agent, session, session_id, logs, cfg.context_tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
