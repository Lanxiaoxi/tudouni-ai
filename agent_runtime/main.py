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

# Windows 下强制 UTF-8 编码，避免打包后 --help 因中文编码报错
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# **这里不再动 `sys.path`。**
#
# 以前有三行 bootstrap：算出包目录、再算出它的上一层、把上一层插进 `sys.path`。那是
# **扁平布局**逼出来的（包就是仓库根本身，而 `python main.py` 只会把包目录自己放进
# 搜索路径），而它有一个致命的隐含要求 —— 那个目录必须恰好叫 `agent_runtime`。
# 仓库在 GitHub 上叫 tudouni-ai，clone 下来于是什么都 import 不了。
#
# 现在包住在仓库里的 `agent_runtime/` 子目录、跟着代码一起提交，所以三条入口都自然成立：
#
#   * `tudouni`（装出来的命令）        —— 包在 site-packages 里，本来就在搜索路径上；
#   * `python -m agent_runtime.main`  —— `-m` 会把 cwd 放进 `sys.path`；
#   * `pytest`                        —— 仓库根的 `conftest.py` 管这一条。
#
# 顺带说清：**"包在哪"和"工作区在哪"现在是彻底分开的两件事**（`paths.package_dir()` 和
# `paths.workspace_dir()`）。以前那段注释说"agent 的文件工作区必须是项目自身"，那句话
# 在装成命令之后是错的 —— 工作区是 cwd，理由见 `paths.py`。

from agent_runtime import i18n
from agent_runtime.frontends.cli import (
    print_audit,
    print_banner,
    print_history,
    print_sessions,
    print_skills,
    run_repl,
)
from agent_runtime.frontends.cli.args import build_parser
from agent_runtime.runtime import ericai
from agent_runtime.runtime.channels import cli_channels
from agent_runtime.runtime.composition import (
    Notice,
    boot,
    check_config,
    check_session_id,
    check_workspace,
    open_runtime,
    resolve_session,
)
from agent_runtime.userconfig import UserConfigError


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

    # ---- 界面语言：**在所有分支之前定下来** ----
    #
    # 它排在这里，是因为**四类路径都要它**：`--tui` 的父进程（画界面）、它拉起的
    # `--runtime-stdio` 子进程（写通知和回话）、老 CLI（同一进程里打通知）、以及
    # `--list` / `--audit` 这些只读子命令（它们也打人读的字）。
    #
    # 来源是"命令行 > 配置文件 > 默认"（见 `i18n.activate`）。**认不出的值当场报**，
    # 退出码 2 —— 和配置写坏同一个处置：一个静默回退的 `en_US` 让人看到的是"我配的
    # 英文没生效"，而他会去查一个不存在的 bug。
    #
    # 配置**读不动**时这一层不报错（退回默认语言）：那份错误有它自己的那一站
    # （下面的 `check_config` / `open_runtime`），抢着报会让用户看到两句话。
    try:
        i18n.activate(args.lang)
    except i18n.LangError as exc:
        print(exc, file=sys.stderr)
        return 2

    # ---- 工作区能不能用：**在所有分支之前** ----
    #
    # 工作区就是 cwd（见 `paths.workspace_dir()`），而 agent 的文件围栏、会话、审计、
    # 权限文件全都从它长出来。所以这一问必须先于**每一条**路：
    #
    #   * `--tui` 起的子进程继承父进程的 cwd，所以父进程这一关就是它那一关 ——
    #     而且在这里报，那句话还能落在终端上（界面接管屏幕之后就没地方显示了）；
    #   * `--runtime-stdio` 直接开工作区；
    #   * `--list` / `--audit` 这些只读子命令也要 —— 它们读的就是工作区里的会话文件，
    #     在 home 下跑只会得到一句空清单，而那比报错更让人困惑。
    #
    # 它排在参数解析之后、别的一切之前：argparse 自己的报错（`--session` 少个值之类）
    # 该先出来，那是"这条命令写错了"，比"这个目录不能用"更靠前一层。
    if (problem := check_workspace()) is not None:
        print(problem, file=sys.stderr)
        return 2

    # ---- `--tui`：父进程，只起界面 ----
    #
    # **它排在最前面，而且不装配任何东西。** 真正的 runtime 在它拉起的
    # `--runtime-stdio` 子进程里；父进程扫一遍技能、建一遍日志目录、再装配一个
    # Agent 然后什么都不干，是纯浪费。
    #
    # 它**不装配任何东西**（父进程不需要密钥，要密钥的是子进程），但**配置要在这里先问
    # 一遍**。原来的注释写的是"父进程不需要密钥，那种失败让界面去显示" —— 那是错的：
    # 界面一起来就接管了终端的备用屏幕缓冲区，而备用屏没有回滚缓冲，子进程那句配置报错
    # 会变成一屏被截断、滚不动的乱码。详见 `composition.check_config`。
    #
    # import 放在函数里：`frontends/tui/__init__.py` 不许在顶层 import textual
    # （否则 `--list` 那种查询子命令也要加载一个 TUI 框架）。
    if args.tui:
        from agent_runtime.frontends.tui import theme as tui_theme
        from agent_runtime.frontends.tui.app import run_tui

        # **进备用屏之前**把"用户得先做点事"那一档查掉，用普通终端把那句话说完。
        if (problem := check_config()) is not None:
            print(problem, file=sys.stderr)
            return 2

        # `--ericai`：进界面之前把 EricAI token 检查/刷新做掉。放在这里是因为
        # 界面一起来就接管了备用屏，而刷新脚本的输出要落在普通终端上（和上面
        # check_config 同一条理由）。失败不拦启动（见 runtime/ericai.py）。
        # 先打一句进度再干活：刷新脚本可能跑几秒到十几秒，什么都不说就是在空等。
        if args.ericai:
            print("[ericai] 正在检查 EricAI token（需要时会自动登录/刷新）…",
                  file=sys.stderr, flush=True)
            print(ericai.ensure(), file=sys.stderr, flush=True)

        # `--theme` 收的是"人能写出来的一段字"（`p7` / `靛夜` / `7`），而认它的是
        # `theme.resolve` —— 同一个函数也是 `/theme` 用的那个，所以两条入口对
        # "什么算一套配色"的判断不可能分家。认不出就**回默认**，不报错：
        # 配色是个装饰性参数，为它让整个界面起不来是最坏的取舍。
        key = tui_theme.resolve(args.theme) if args.theme else tui_theme.DEFAULT_THEME
        # 流式默认开，`--no-stream` 关掉。`args.stream` 为 None 表示"没说过"——
        # 这正是两个前端默认值不同的表达方式（见 `args.py` 里那一对开关的说明）。
        want_stream = True if args.stream is None else args.stream
        return run_tui(args.session, autopilot=args.autopilot,
                       theme_key=key or tui_theme.DEFAULT_THEME,
                       stream=want_stream, quiet=args.quiet, lang=i18n.current())

    # ---- `--runtime-stdio`：协议子进程 ----
    #
    # **它也不碰 cli 前端**：那一支的 stdout 是协议通道，任何一行人话（横幅、
    # 提示符）打上去都会毒了它。
    #
    # 它也不做配置检查 —— 配置错误由 `protocol.serve` 负责报（打到 stderr 并以
    # 退出码 2 结束），因为那时候才有 stdout 要被保护。
    if args.runtime_stdio:
        from agent_runtime.protocol.serve import main as serve
        return serve(args.session, autopilot=args.autopilot, debug=args.debug,
                     stream=True if args.stream is None else args.stream,
                     lang=i18n.current())

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

    # 老 CLI 这一支不支持流式（它是直连的，走不了协议那条 delta 通道；而"在
    # 行式终端上逐字打"是另一件事，见 README 里那条已实现范围）。
    #
    # **显式传了就说一句**，而不是静默忽略：一个人写下 `--stream` 是想看到逐字，
    # 而"参数被悄悄吃掉"和"这个参数不存在"在他眼里一模一样 —— 下一次他会以为
    # 是模型不支持。**这里不改成流式**：那会让 stdout 从"整段答案"变成"边收边写"，
    # 而 README 把 `> 对话.txt` 拿到一份干净答案写成了契约。
    if args.stream is True:
        print("[流式] 老 CLI 不支持逐字输出（它不走协议那条通道）；"
              "要看逐字请用 --tui。已按 --no-stream 继续。", file=sys.stderr)

    # `--quiet` 同一条规矩：**显式传了就说一句**。它只换 TUI 的画法，而老 CLI 的
    # 输出本来就是一行的（"一次工具调用占几行"在这里无从谈起）—— 静默忽略的话，
    # 用户会以为"安静模式没生效"，而它其实压根不该在这一支上生效。
    if args.quiet:
        print("[安静] --quiet 只作用于 TUI（--tui）：它换的是界面怎么画，"
              "而老 CLI 的输出本来就是一行的。", file=sys.stderr)

    # `--ericai`：老 CLI 直连也需要刷 —— 但必须在 `open_runtime` 之前，因为 catalog
    # 是在 open_runtime 里才读 config（见 composition.py 里那一处 `catalog.load()`）。
    # 刷完写回 config，open_runtime 拿到的就是新 token。失败不拦启动。
    # 先打一句进度再干活：刷新脚本可能跑几秒到十几秒，什么都不说就是在空等。
    if args.ericai:
        print("[ericai] 正在检查 EricAI token（需要时会自动登录/刷新）…",
              file=sys.stderr, flush=True)
        print(ericai.ensure(), file=sys.stderr, flush=True)

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
    except UserConfigError as exc:
        # **捕基类，不是 `ConfigError`。** `CatalogError`（`~/.tudouni/config.json` 的
        # `providers` 段读不懂）也是它的子类，而它以前一处都没被捕 —— 那份文件里多写
        # 一个逗号就会以一整段 Python traceback 收场，而它恰好是新用户最先编辑的文件。
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
