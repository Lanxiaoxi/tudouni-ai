"""
Agent Runtime 主入口。

这个文件只做一件事：**把各个部件接起来**。怎么跟用户说话在 cli.py，配置从哪来在
config.py，重试策略在 agents/retry.py，权限裁决在 security/gate.py，审计落盘在
audit/。入口保持薄，是因为它的变化原因只有一个 —— 装配方式变了。
"""

import sys
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
from agent_runtime.config import ConfigError, ModelConfig
from agent_runtime.models import OpenAICompatibleModel
from agent_runtime.security import PermissionPolicy, cli_asker
from agent_runtime.state import JsonSessionStore
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.tool import RiskLevel

SESSIONS_DIR = PROJECT_DIR / ".sessions"
LOGS_DIR = PROJECT_DIR / ".logs"


def main() -> int:
    """返回退出码：配置缺失是"用户得先做点事"，脚本调用方应该能看出失败。"""
    args = build_parser().parse_args()
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
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    # 横幅放在配置检查**之后**：密钥没配就退出的那种启动，不需要先看一幅图案。
    # 它也不属于 --list / --history / --audit 那三条路径 —— 那些子命令特意排在配置
    # 检查之前，为的是「没配密钥也能查历史」，跟"要开会话了"是两回事。
    print_banner()

    session_id, session = resolve_session(store, args.session)

    model = OpenAICompatibleModel(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        http_client=httpx.Client(),
    )

    tools = create_tool_registry(str(PROJECT_DIR))
    print("已注册工具:")
    for tool in tools.all():
        print(f"  - {tool.name:12} 风险={tool.risk.value}")

    # 只自动放行低风险工具，其余一律弹审批
    policy = PermissionPolicy(auto_approve={RiskLevel.LOW})

    # 三个注入点，同一个原则：判定留在 Agent 内部，执行交给注入的实现。
    agent = Agent(
        model, tools, policy,
        asker=cli_asker,
        on_checkpoint=store.save,
        on_event=logs,
        debug=args.debug,
    )
    print(f"审计日志写到 {logs.directory}\\{session_id}.jsonl")

    # logs 同时交给 run_repl：末尾那句累计用量是从审计日志里数出来的，传的是
    # **同一个** sink（也就是同一个 on_event）—— 换成别的东西就会报出另一套数字。
    run_repl(agent, session, session_id, logs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
