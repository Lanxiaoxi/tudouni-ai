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
    print_skills,
    resolve_session,
    run_repl,
)
from agent_runtime.config import (
    PERMISSION_FILE,
    PERMISSION_FILE_NAME,
    ConfigError,
    ModelConfig,
    PermissionConfig,
    WebConfig,
    save_approvals,
)
from agent_runtime.models import OpenAICompatibleModel
from agent_runtime.security import ApprovalMemory, PermissionPolicy, cli_asker
from agent_runtime.security.commands import format_rule
from agent_runtime.skills import (
    SkillCatalog,
    SkillLoader,
    active_line,
    catalog_entries,
    catalog_part,
    skill_note,
)
from agent_runtime.state import JsonSessionStore
from agent_runtime.state.session import is_valid_session_id
from agent_runtime.tools.ask import cli_questioner, unavailable_questioner
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.todo import TodoBoard, progress_line, todo_note
from agent_runtime.tools.webfetch import USER_AGENT, WebFetch
from agent_runtime.tools.websearch import TavilySearch, WebSearch

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


def report_skills(catalog: SkillCatalog, session=None) -> None:
    """把这次启动扫到的技能说一遍。

    和 `[权限]` / `[任务]` 那几行同一类东西（关于这次运行的既有状态），走 stderr。

    **坏技能和被遮住的同名技能必须逐条报出来，这是这个函数存在的主要理由。** 两者的
    症状一模一样：磁盘上那份文件明明在，却完全不起作用。一份写错 frontmatter 的
    SKILL.md 从启动到会话结束都不会有任何异常；而一份被个人级技能遮住的项目级技能更
    隐蔽 —— 人改它、改了很多遍，改的却是一份不算数的文件。取向和
    `PermissionConfig.unknown_tools` 完全一样：不该拦启动，但绝不能不说。

    已加载的技能也报一遍（恢复会话时 `session.metadata` 里可能就有）：技能正文比进程
    活得久，而"它现在按哪份说明在做"是接着聊之前唯一该先看一眼的事实。
    """
    if catalog.skills:
        print(f"[技能] 可用 {len(catalog.skills)} 个："
              f"{'、'.join(skill.name for skill in catalog.skills)}", file=sys.stderr)
    for item in catalog.shadowed:
        print(f"[技能] 同名遮蔽：{item}", file=sys.stderr)
    for problem in catalog.problems:
        print(f"[技能] {problem}", file=sys.stderr)
    if session is not None and (line := active_line(session.metadata)):
        print(f"[技能] {line}", file=sys.stderr)


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

    # 技能是硬盘上的文件，所以扫它不需要模型 —— 和 --list 同一档，排在配置检查之前。
    # 造在这个位置还有第二个理由：后面装配工具和注 session_notes 都要用到这一份
    # （`--skills` 只需要读它，别的子命令连碰都不碰）。
    #
    # 扫的是**六个约定目录**（用户级三个、项目级三个，见 skills/loader.py 的
    # default_roots）：用户级那三个在工作区外面，而这条路径是硬编码的 —— SkillLoader
    # 不接受模型给的路径，所以"技能只有人能改"在用户级目录上是操作系统帮着保证的。
    skill_loader = SkillLoader(PROJECT_DIR)
    skill_catalog = skill_loader.reload()

    # ---- 不需要模型的子命令：先处理掉，这样没配密钥也能查历史/审计 ----
    if args.list:
        print_sessions(store)
        return 0

    if args.skills:
        print_skills(skill_loader)
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
        # 联网工具的密钥**不在这一档**：缺了只是少一个工具，不是"什么都干不了"。
        web = WebConfig.from_env()
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

    # 联网抓取用的 http client：**一个进程一个**，连接复用、TLS 握手只付一次。
    #
    # trust_env=False：环境变量里的 HTTP_PROXY 不该悄悄改掉这个程序的行为 ——
    # 和 config 里"环境变量优先、但方向不能反"是同一个担心的两半。要代理就显式构造
    # 一个 client 传进来。
    http = httpx.Client(trust_env=False, headers={"User-Agent": USER_AGENT})

    # 缺搜索密钥时把话说在 stderr 上，而不是"注册了再让模型去撞墙"：工具 schema 每一轮
    # 都要发出去，而模型对"没有密钥"这件事无能为力 —— 它只会白花一步去调一次。这句话
    # 让它变成"用户得先做点事"，和 [上下文]/[权限] 那几行是同一种做法。
    if not web.tavily_api_key:
        print("[联网] 没找到 TAVILY_API_KEY，web_search 未注册（fetch_web 不受影响）。"
              "要启用就写进 .env：TAVILY_API_KEY=tvly-...", file=sys.stderr)

    try:
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
            # 联网那一对。**密钥的读取留在入口这一层**（tools/ 不能 import config，
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
            # 成功了、却永远不出现在载荷里"—— 没有异常、没有审计痕迹（tools/tool.py 里
            # 那段写了为什么；tests 里那条 test_the_note_never_enters_session_messages
            # 就是盯着它的）。
            #
            # loader 一起传进去，board 每次读清单都重扫技能目录：会话开着的时候新加的
            # 技能下一轮就能加载（只给一次快照的话，模型会看得见一个加载不了的技能）。
            #
            # 一个技能都没有时传 None，create_tool_registry 因此**不注册** load_skill
            # —— 和缺 TAVILY_API_KEY 不注册 web_search 同一条路。代价说明白：这种情况下
            # 中途新建技能要重开会话才用得上（"连工具都还不在"，和"工具有了、技能换了"
            # 是两件事，后者由重扫兜住）。
            skills=skill_catalog if skill_catalog.skills else None,
            skill_metadata=session.metadata,
            skill_loader=skill_loader,
        )
    except Exception:
        http.close()
        raise
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
    report_skills(skill_catalog, session)

    # autopilot 要在启动时大声说一次：它意味着接下来所有需要审批的工具都会**直接执行**，
    # 而这件事一旦忘了自己开着，事后看日志只会觉得"这个项目怎么什么都没问"。
    # 它也不做成配置项 —— 一次性的决定不该悄悄变成永久默认。
    if args.autopilot:
        print("[权限] autopilot：不询问任何审批，需要审批的工具会直接执行；"
              "也不会向你提问 —— 模型调 ask_user 会拿到「没有人回答」，"
              "并被告知自己决定、把假设说出来（拒绝名单、工作区边界、控制面写入仍然生效）",
              file=sys.stderr)

    # 载荷尾部那段会话状态：技能目录 + 已加载技能的正文 + 任务列表，合成**一条**临时
    # 消息（Agent 里 _status_note 负责合成，这里只负责"这一段说什么"）。
    #
    # 顺序是刻意的，而且它只在这一个地方定：先目录（有哪些能用），再正文（现在该按哪份
    # 做），最后任务列表（做到哪了）。倒过来的话，模型会先读到一份"还剩什么活"的清单，
    # 再读到"该怎么做" —— 而它做决策的瞬间需要的是后者。
    #
    # 读的必须是 `tools.skills.catalog`（注册表上那个 board）而不是上面那份启动快照：
    # board 每次读都会重扫目录 —— 所以中途新加的技能下一轮就会出现在清单里，而且和"能不能
    # 加载"读到的是同一份事实。重扫在这个函数里**只做一次**（读到局部变量再分别渲染两段）：
    # 它每次读盘都会把每个技能文件读一遍，而这个函数每一步都会被调一次。
    #
    # 三段都**不进 session.messages**（逐轮变化的东西不持久化，见 agent._status_note），
    # 所以它必须只读 metadata + 技能目录，不做别的事。
    def session_notes(metadata):
        board = tools.skills
        catalog = board.catalog if board is not None else skill_catalog
        return "\n\n".join(filter(None, (
            catalog_part(metadata, catalog),
            skill_note(metadata, catalog),
            todo_note(metadata),
        )))

    # 四个注入点，同一个原则：判定留在 Agent 内部，执行交给注入的实现。
    # （提问通道是第五个，但它在上面装配工具时就注入了 —— 它不属于 Agent：Agent 只看见
    # 一次普通的工具调用，ask_user 会不会阻塞在人的输入上，它不知道也不需要知道。）
    agent = Agent(
        model, tools, policy,
        asker=partial(cli_asker, memory=memory),
        memory=memory,
        on_checkpoint=store.save,
        on_event=logs,
        # 会话状态每轮都要重新贴在请求末尾（当前状态，不是让模型去翻历史找最近那一版）。
        # 注入的是一段"怎么说"的实现：Agent 自己不知道技能和任务列表长什么样 ——
        # 它只知道"每次请求末尾要把当前会话状态贴上"（见 agent.py 的 SessionNotes）。
        session_notes=session_notes,
        debug=args.debug,
        # autopilot 只管审批那一关：工作区边界、控制面写入、拒绝名单都在它管不着的地方，
        # 所以它不是"关掉权限"，只是"这一轮没人可问"。
        autopilot=args.autopilot,
    )
    print(f"审计日志写到 {logs.directory}\\{session_id}.jsonl")

    # logs 同时交给 run_repl：末尾那句累计用量是从审计日志里数出来的，传的是
    # **同一个** sink（也就是同一个 on_event）—— 换成别的东西就会报出另一套数字。
    # context_tokens 是那句话里 xxx/total 的分母。
    try:
        run_repl(agent, session, session_id, logs, cfg.context_tokens)
    finally:
        # 会话结束就关掉：连接池里那些 keep-alive 的 socket 不该留到进程退出。
        # （模型那个 client 由 OpenAI SDK 自己管，这里是**我们**建的那个。）
        http.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
