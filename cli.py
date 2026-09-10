"""命令行界面：参数解析、会话选择、交互循环、历史与审计的展示。

和 main.py 分开，是因为**它们的变化原因不同**：main.py 关心"把哪些部件接起来"，
改动来自架构演进；这里关心"怎么跟用户说话"，改动来自使用习惯。放在一个文件里，
两种改动的 diff 会互相淹没。

顺带解决了一个具体的别扭：以前"新会话 X"是在配置检查之前打印的，所以没配
DEEPSEEK_API_KEY 时会先看到"新会话"再看到报错。现在子命令分两段 —— 不需要模型
的（--list / --history / --audit）先处理，需要模型的才读配置。
"""

import argparse
import sys

from agent_runtime.audit import JsonlSink
from agent_runtime.models.types import ModelFatalError, ModelTransientError
from agent_runtime.state import JsonSessionStore, Session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Runtime 命令行入口")
    parser.add_argument(
        "--session", default=None,
        help="会话 id。给了就接着那个会话聊（不存在则新建）；不给就自动开一个新的",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="打印 --session 指定会话的对话历史，不调用模型",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="打印 --session 指定会话的审计轨迹（token、权限裁决、耗时），不调用模型",
    )
    parser.add_argument("--list", action="store_true", help="列出已保存的会话")
    parser.add_argument("--debug", action="store_true", help="把中间过程打到 stderr")
    return parser


# --- 不需要模型的子命令 ---------------------------------------------------

def print_sessions(store: JsonSessionStore) -> None:
    ids = store.list_ids()
    print(f"已保存 {len(ids)} 个会话（{store.directory}）：")
    for session_id in ids:
        session = store.load(session_id)
        print(f"  {session_id:22} {len(session.messages):3} 条消息、{session.step_count()} 步")


def print_history(session: Session) -> None:
    """把对话过程打出来。内容只留预览 —— messages 里可能躺着上万字符的文件正文。"""
    print(f"会话 {session.session_id!r}：{len(session.messages)} 条消息，{session.step_count()} 步")
    print("-" * 72)
    for i, msg in enumerate(session.messages):
        role = msg["role"]
        preview = str(msg.get("content") or "").replace("\n", "\\n")[:76]

        if role == "assistant":
            if preview:
                print(f"{i:3} {role:9} {preview}")
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                print(f"{i:3} {role:9} → 调用 {fn['name']}({fn['arguments'][:64]})")
        elif role == "tool":
            print(f"{i:3} {role:9} ← {preview}")
        else:
            print(f"{i:3} {role:9} {preview}")


def print_audit(sink: JsonlSink, session_id: str) -> None:
    """渲染审计轨迹，末尾附一份汇总。

    **只报 token，不报钱。** 价格会变、还分高峰/空闲时段，把它固化进输出就等于
    把会变的东西写死。想看钱，拿 token 乘以当时的价目表。
    """
    events = list(sink.read(session_id))
    if not events:
        print(f"会话 {session_id!r} 没有审计记录（{sink.directory}）")
        return

    print(f"会话 {session_id!r} 的审计轨迹：{len(events)} 条")
    print("-" * 78)
    for event in events:
        payload = {
            k: v for k, v in event.items()
            if k not in ("ts", "kind", "session_id", "run_id", "step")
        }
        body = " ".join(f"{k}={v}" for k, v in payload.items())
        print(f"{event['ts'][11:]} {event['kind']:<13} step={event['step']:<3} {body[:100]}")

    _print_audit_summary(events)


def _print_audit_summary(events: list[dict]) -> None:
    calls = [e for e in events if e["kind"] == "model_call"]
    ok_calls = [e for e in calls if e.get("status") == "ok"]
    prompt = sum(e.get("prompt_tokens", 0) for e in ok_calls)
    cached = sum(e.get("cached_tokens", 0) for e in ok_calls)
    miss = sum(e.get("miss_tokens", 0) for e in ok_calls)
    completion = sum(e.get("completion_tokens", 0) for e in ok_calls)

    results = [e for e in events if e["kind"] == "tool_result"]
    by_status: dict[str, int] = {}
    for event in results:
        by_status[event.get("status", "?")] = by_status.get(event.get("status", "?"), 0) + 1

    print("-" * 78)
    hit_rate = f"{cached / prompt:.0%}" if prompt else "—"
    print(f"模型调用 {len(calls)} 次（{len(ok_calls)} 次成功）"
          f"  输入 {prompt} token（命中缓存 {cached} / 未命中 {miss}，命中率 {hit_rate}）"
          f"  输出 {completion} token")
    print(f"工具调用 {len(results)} 次  " +
          "  ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    stops = [e.get("stop_reason") for e in events if e["kind"] == "run_finished"]
    if stops:
        print(f"回合结束原因  " + "  ".join(stops))
    print("提示：未命中缓存的输入是成本大头（官方价里它比命中贵约 50 倍），"
          "所以「读了多大的文件」比「聊了多少轮」更决定花费。")


# --- 需要模型的部分 -------------------------------------------------------

def resolve_session(store: JsonSessionStore, session_id: str | None) -> tuple[str, Session]:
    """决定这次聊哪个会话：给了 id 就接着（不存在则新建），没给就自动开一个新的。

    注意这里还不会落盘 —— 第一次写盘发生在你说出第一句话之后（Agent 在把用户
    消息追加进 messages 之后才触发 checkpoint）。所以"开了不用"不会留下空文件。
    """
    if session_id:
        if store.exists(session_id):
            session = store.load(session_id)
            print(f"继续会话 {session_id!r}：{len(session.messages)} 条消息")
        else:
            session = Session.new(session_id)
            print(f"新建会话 {session_id!r}")
        return session_id, session

    session_id = store.new_session_id()
    print(f"新会话 {session_id!r}（说出第一句话之后才会落盘）")
    print(f"  想回来继续它：  --session {session_id}")
    return session_id, Session.new(session_id)


def run_repl(agent, session: Session, session_id: str) -> None:
    """多轮对话循环。

    这个 while 刻意留在 Agent 外面：Agent 的契约是"一个回合"，多轮循环属于驱动层，
    因为它的形态随环境而变（CLI 是循环、Web 是每请求一次、测试是遍历列表）。
    把循环塞进 Agent，它就得知道"从哪读用户输入"。
    """
    print("输入内容回车发送。空行、exit、quit 或 Ctrl+C 退出。\n")
    while True:
        try:
            # 提示符写 stderr，和 cli_asker 保持一致：stdout 只留给 Agent 的产出，
            # 这样 `python main.py > 对话.txt` 拿到的就是干净的答案。
            print("> ", end="", file=sys.stderr, flush=True)
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break

        if not line or line.lower() in {"exit", "quit"}:
            break

        print(f"\n--- 用户输入: {line} ---\n")
        try:
            print(agent.run(session, line))
        except ModelFatalError as exc:
            # 重试没有意义的那类失败（鉴权、模型名、请求格式）—— 告诉用户原因，
            # 但**不退出**：一个回合失败不等于整个会话结束。
            print(f"\n[本轮失败 · 不可重试] {exc}", file=sys.stderr)
        except ModelTransientError as exc:
            # 退避重试都用完了。会话状态是完好的（模型失败的时机总在一致点），
            # 所以可以直接再试一次，或者退出后用 --session 继续。
            print(f"\n[本轮失败 · 重试后仍失败] {exc}", file=sys.stderr)

        print(f"\n（会话 {session_id!r}：{len(session.messages)} 条消息、"
              f"{session.step_count()} 步。退出后可用 --session {session_id} 继续。）\n")
