"""第二期的验收脚本：**用 Textual 的测试台跑一次真 TUI**。

它不起真终端（那是人做的事），但**起真子进程、真协议、真 HTTP 桩** ——
所以它验的是"这条链路真的通"，而不是"函数能跑"。

    python scripts/verify_tui.py

三件事，每件都是这一期必须成立的：

  1. **握手**：`init` 到了界面上（会话 id、模型、工具数、步数上限）；
  2. **一轮对话**：一句话发出去 → 假网关回一句 → 答案出现在界面上；
  3. **审批走面板**：Agent 要跑一条 `shell` → 审批面板弹出来 → 点 [允许] →
     这一轮真的走完。

第 3 条是**三条人机通道换掉了**唯一的外部证据。

## 为什么这个脚本不在 `tests/` 里

它要起 HTTP 桩和真子进程，比单测重一个数量级；而 `tests/test_tui.py` 里已经有
轻量的那条（用 `FakeClient` 替掉子进程，验界面逻辑）。重的那条留在这里，
由人按需跑 —— 和"耗时数据要分项看"那类一次性测量是同一种处置。
"""

import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# `scripts/` 在包**外面**，所以要上两级才是"包所在目录"（仓库根）——
# 和 `main.py` 里那句 `sys.path.insert` 同一个道理，而它们算的是同一个目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class _Handler(BaseHTTPRequestHandler):
    """假网关。按脚本逐次回话；最后一条会被重复使用。"""

    scripts: list[dict] = [{"content": "我很好，谢谢。"}]
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).calls.append(json.loads(self.rfile.read(length) or b"{}"))
        index = min(len(type(self).calls) - 1, len(type(self).scripts) - 1)
        step = type(self).scripts[index]
        # `delay` 只有"验中断"那一条用得上：要有一个**正在跑**的回合才按得动 Esc。
        if step.get("delay"):
            time.sleep(step["delay"])
        payload = {
            "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": step.get("content"),
                            "tool_calls": step.get("tool_calls")},
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


async def _drive(app, pilot, predicate, *, tries: int = 100, delay: float = 0.05):
    """推消息泵直到 `predicate()` 成立。

    **必须睡真的时间**：起子进程 + 读 JSONL 是几十到几百毫秒，而
    `pilot.pause()` 只让出一轮事件循环（微秒级）。第一版只 pause，于是循环空转
    100 轮之后子进程还没起来（实测：握手一直是空的）。
    """
    for _ in range(tries):
        app._pump()
        await pilot.pause()
        await asyncio.sleep(delay)
        if predicate():
            return True
    return False


def _log_text(app) -> str:
    """会话流里现在所有的字。

    **回合头也要算进来**：它是 `Static` 而不是 `LineBlock`（它要能被 `run_finished`
    回填成最终形态），所以只读 `LineBlock` 会漏掉"已中断""步数用尽"这些**结局**
    —— 而那正是这里要断言的东西（实测：漏了它，"界面说没说清"这条断言永远失败）。
    """
    from textual.widgets import Static

    from agent_runtime.frontends.tui import widgets

    parts = [str(widget.render()) for widget in app.query(".turn-head")]
    for block in app.query(widgets.LineBlock):
        parts.extend(str(line) for line in block.lines)
    return "\n".join(parts)


async def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    from textual.widgets import Button

    from agent_runtime.frontends.tui.app import TuiApp

    app = TuiApp(session="acceptance")
    ok = True
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            # --- 1. 握手 ---
            await _drive(app, pilot, lambda: bool(app.state.session_id))
            print("=== 握手 ===")
            print(f"  会话 {app.state.session_id}  模型 {app.state.model}  "
                  f"最多 {app.state.max_steps} 步  工具 {len(app.state.tool_risks)} 个")
            assert app.state.session_id, "init 没到界面上"
            assert app.state.tool_risks, "init 里没有工具清单"

            # --- 2. 一轮对话 ---
            app.submit("你好")
            await _drive(app, pilot, lambda: bool(app.state.answers))
            print("=== 一轮对话 ===")
            print(f"  phase={app.state.agent.phase}  answers={app.state.answers}")
            assert "我很好，谢谢。" in app.state.answers.values(), \
                f"答案没到界面上：{app.state.answers}"
            assert app.state.agent.phase == "finished", app.state.agent.phase

            # --- 3. 审批 ---
            _Handler.scripts.append({
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "shell",
                                 "arguments": json.dumps({"command": "echo hi"})},
                }],
            })
            _Handler.scripts.append({"content": "跑完了"})
            before = len(app.state.answers)

            app.submit("跑一下 echo hi")
            panel = await _drive(app, pilot,
                                 lambda: type(app.screen).__name__ == "PermissionPanel")
            if panel:
                # **面板挂上去和它里面的按钮挂上去不是同一刻**：`push_screen` 先把
                # Screen 放好，里面的 Button 要等下一次布局才在 DOM 里。只等前者
                # 就会偶发 `NoMatches: #allow`（实测：这条验收脚本因此红过一次，
                # 而重跑又绿 —— 典型的时序依赖）。所以再等一次，等的是那个按钮。
                await _drive(app, pilot,
                             lambda: bool(app.screen.query("#allow")), tries=40)
            print("=== 审批 ===")
            print(f"  面板 {type(app.screen).__name__}  "
                  f"phase={app.state.agent.phase}  网关收到 {len(_Handler.calls)} 次请求")

            if type(app.screen).__name__ != "PermissionPanel":
                # 那条命令被权限配置自动放行了（本机 permissions.json 里可能有
                # shell_allow 命中）。那不是失败 —— 但这条验收就失去意义了。
                print("  ！没弹面板：这条命令被自动放行了，这条验收跳过")
            else:
                ids = sorted(b.id for b in app.screen.query(Button))
                print(f"  按钮 {ids}")
                app.screen.query_one("#allow", Button).press()
                await _drive(app, pilot, lambda: len(app.state.answers) > before)
                print(f"  answers={app.state.answers}")
                assert "跑完了" in app.state.answers.values(), \
                    f"点了允许之后这一轮没走完：{app.state.answers}"

                # 那次 shell 真的执行了 —— 从网关收到的最后一条请求里能看到结果。
                last = _Handler.calls[-1]["messages"]
                tool_msgs = [m for m in last if m.get("role") == "tool"]
                assert tool_msgs, "最后那次请求里没有 tool 结果"
                print(f"  工具结果：{str(tool_msgs[-1].get('content'))[:60]!r}")
                assert "退出码 0" in str(tool_msgs[-1].get("content")), \
                    "审批放行之后命令应该真的执行了"

            # --- 4. Esc 中断这一轮 ---
            #
            # **这是"协议里那条 interrupt 真的通到 Agent 的检查点"唯一的端到端证据。**
            # 单测只能钉住"标志置了没有"，而这条链有四段：TUI 的 Esc → 协议一行 →
            # `ProtocolServer.request_stop` → Agent 在**两步之间**停下并发出
            # `run_finished(stop_reason=cancelled)` → 界面把它显示成"已中断"。
            #
            # 两件事是这条验收能成立的前提，都不是随手写的：
            #
            #   * 网关这一步要**慢 2 秒**，否则回合在按 Esc 之前就跑完了；
            #   * 让模型**调一个工具**（而不是直接回答）。检查点在两步之间，而
            #     "只有一步、模型直接回答"的回合里根本没有下一个检查点 —— 那时候
            #     Esc 的正确行为就是**什么都不发生**（不打断正在进行的那一步，
            #     也不丢掉已经拿到的答案）。
            _Handler.scripts.append({
                "content": None,
                "delay": 2.0,
                "tool_calls": [{
                    "id": "call_2", "type": "function",
                    "function": {"name": "read_file",
                                 "arguments": json.dumps({"path": "main.py"})},
                }],
            })
            _Handler.scripts.append({"content": "不该看到我"})
            app.submit("这一轮请慢一点")
            running = await _drive(app, pilot, lambda: app.state.agent.is_busy,
                                   tries=60)
            print("=== 中断 ===")
            assert running, "这一轮没跑起来，Esc 就无从测起"
            app.action_escape_key()
            stopped = await _drive(
                app, pilot, lambda: app.state.agent.phase == "cancelled", tries=200)
            print(f"  phase={app.state.agent.phase}")
            assert stopped, f"Esc 之后这一轮没有停下：{app.state.agent.phase}"
            assert "已中断" in _log_text(app), "界面上没说清这一轮是被中断的"
            print("=== 通过 ===")
    except AssertionError as exc:
        print(f"=== 失败：{exc} ===")
        ok = False
    finally:
        server.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
