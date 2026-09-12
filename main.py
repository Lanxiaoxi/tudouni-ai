"""
Agent Runtime 主入口。

这个文件只做一件事：**决定这次运行走哪条路，然后把活交给别人**。

装配在 `runtime/composition.py`（它认识内核的每一个零件），怎么跟用户说话在
`frontends/cli/`，配置从哪来在 `runtime/config.py`。

**第零期之后它有多薄**：以前这里有 465 行，其中一半是真正的运行时职责（起 MCP
子进程、渲染载荷尾部、接 trust group、读策略），另一半是呈现（7 处 print）。
前者搬进了 `runtime/`，后者变成了 `Runtime.notices()` 返回的数据。留下的只有
分派和"用哪个流把它打出来"。

**两条刻意的顺序约定，改的时候要小心**：

  1. 四个"不需要模型"的子命令（`--list` / `--skills` / `--audit` / `--history`）
     排在配置检查**之前** —— 没配密钥的人照样该能查自己的历史；
  2. 横幅排在配置检查**之后** —— 密钥没配就退出的那种启动，不需要先看一幅图案。
"""

import sys
from pathlib import Path

# 项目自身目录与它的父目录是两件事，不要共用一个变量：
#   - agent_runtime 是一个包，要能 `import agent_runtime`，
#     sys.path 必须包含【包所在目录】，也就是项目的父目录；
#   - agent 的文件工作区必须是【项目自身】，不能是父目录，
#     否则 read_file / write_file 会伸到同级的其它项目里去。
PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent

sys.path.insert(0, str(REPO_ROOT))

from agent_runtime.frontends.cli import (
    print_audit,
    print_banner,
    print_history,
    print_sessions,
    print_skills,
    run_repl,
)
from agent_runtime.frontends.cli.args import build_parser
from agent_runtime.runtime.channels import cli_channels
from agent_runtime.runtime.composition import (
    Notice,
    boot,
    check_session_id,
    open_runtime,
    resolve_session,
)
from agent_runtime.runtime.config import ConfigError


def _emit(notices: list[Notice], *, audit_line: str | None = None) -> None:
    """把装配期那些说明打出来，**按它自己记的流**。

    一次遍历、按 `notice.stream` 分派，而不是分两趟（先 stdout 再 stderr）：
    两趟会改掉两者的相对顺序，而 `> 对话.txt` 和终端上看到的都依赖那个顺序。

    `audit_line` 是"审计日志写到哪"，**单独一个参数而且排在最后** —— 它属于装配
    事实（所以它在 `notices()` 里），但老 CLI 的输出顺序是「已注册工具 → 审计日志
    写到」，而那份工具清单是 notices 里的一条。把它单独拎出来打就两全了：位置对得上，
    内容也仍然由装配层提供（前端只是负责打出来）。`--audit` / `--list` 那些子命令
    根本不走这条路，所以它们不会看到这一行。
    """
    for notice in notices:
        stream = sys.stdout if notice.stream == "out" else sys.stderr
        print(notice.text, file=stream)
    if audit_line is not None:
        print(audit_line, file=sys.stderr)


def main() -> int:
    """返回退出码：配置缺失是"用户得先做点事"，脚本调用方应该能看出失败。"""
    args = build_parser().parse_args()

    # ---- `--tui`：父进程，只起界面 ----
    #
    # **它排在最前面，而且不装配任何东西。** 真正的 runtime 在它拉起的
    # `--runtime-stdio` 子进程里；父进程扫一遍技能、建一遍日志目录、再装配一个
    # Agent 然后什么都不干，是纯浪费。
    #
    # 它也必须早于配置检查：父进程自己**不需要密钥**（要密钥的是子进程），
    # 而"没配密钥"那种失败必须能在界面上显示出来，而不是让父进程先崩掉。
    #
    # import 放在函数里：`frontends/tui/__init__.py` 不许在顶层 import textual
    # （否则 `--list` 那种查询子命令也要加载一个 TUI 框架）。
    if args.tui:
        from agent_runtime.frontends.tui.app import run_tui
        return run_tui(args.session, autopilot=args.autopilot)

    # ---- `--runtime-stdio`：协议子进程 ----
    #
    # **它也不碰 cli 前端**：那一支的 stdout 是协议通道，任何一行人话（横幅、
    # 提示符）打上去都会毒了它。
    #
    # 它也不做配置检查 —— 配置错误由 `protocol.serve` 负责报（打到 stderr 并以
    # 退出码 2 结束），因为那时候才有 stdout 要被保护。
    if args.runtime_stdio:
        from agent_runtime.protocol.serve import main as serve
        return serve(args.session, autopilot=args.autopilot, debug=args.debug)

    # 会话 id 的合法性在这里一次查清，早于任何会碰它的东西。--list 不看这个参数，
    # 但传了非法值仍然报错 —— 一个地方查一次，比让三条子命令各自去猜自己会不会
    # 用到它可靠。
    if (problem := check_session_id(args.session)) is not None:
        print(problem, file=sys.stderr)
        return 2

    booted = boot()

    # ---- 不需要模型的子命令：先处理掉，这样没配密钥也能查历史/审计 ----
    if args.list:
        print_sessions(booted.store)
        return 0

    if args.skills:
        print_skills(booted.skill_loader)
        return 0

    if args.audit or args.history:
        if not args.session:
            flag = "--audit" if args.audit else "--history"
            print(f"{flag} 需要配合 --session <id>；先用 --list 看有哪些会话", file=sys.stderr)
            return 2
        if args.audit:
            print_audit(booted.logs, args.session)
        else:
            print_history(booted.store.load(args.session))
        return 0

    # ---- 以下需要模型 ----
    #
    # 配置错误（缺密钥、permissions.json 写坏、mcp.json 写坏）由 open_runtime 抛
    # ConfigError。它发生在**开出一个 Runtime 之前**，所以不需要收摊。
    print_banner()

    session_id, session, resumed = resolve_session(booted.store, args.session)

    try:
        runtime = open_runtime(
            booted=booted,
            session_id=session_id,
            session=session,
            # `--autopilot` 同时管住两条人机通道：审批不问、提问拿到"没有人回答"。
            channels=cli_channels(unavailable=args.autopilot),
            autopilot=args.autopilot,
            debug=args.debug,
            resumed=resumed,
        )
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    with runtime:
        # 顺序是有意的，而且是**照老 CLI 的输出顺序**定下来的（第零期的验收标准
        # 就是"逐字节不变"）：
        #
        #   1. 会话身份那两行排在最前 —— 老 CLI 里 `resolve_session` 就在配置检查
        #      之后、工具清单之前打印它们；
        #   2. 然后是装配那些说明；
        #   3. 「审计日志写到」排在最后，而它同时是**唯一走 stderr 的 stdout 内容**
        #      （见 `_emit` 的 docstring）。
        # 恢复会话时老 CLI 的顺序略有不同（那行在所有说明之前），这里统一成新会话那
        # 一种：那一行是说给"接着聊"的人听的，位置不影响它说的事，而 stdout 的干净
        # 程度是 README 写着的契约 —— 两害相权，保契约。
        if resumed:
            print(f"继续会话 {session_id!r}：{len(session.messages)} 条消息")
        else:
            print(f"新会话 {session_id!r}（说出第一句话之后才会落盘）")
            print(f"  想回来继续它：  --session {session_id}")

        _emit(runtime.notices(), audit_line=runtime.audit_log_line())
        run_repl(runtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
