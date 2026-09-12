"""一个最小 ANSI 客户端：**把协议画成一行行文本**。

## 它的用处是什么（以及不是什么）

**不是**一个 TUI。它是一个**验收工具**：第一期要证明"这条协议能撑起一个前端"，
而证明的方式就是**真的写一个**。它的全部价值在于：

  * 逼着协议在一个真消费者面前成立（写它的时候抓到过三个真问题：`on_event` 发答案
    太早、`shutdown` 会误取消当前回合、以及"一次把话说完"的客户端根本验不了审批）；
  * 给第二期一个对照物 —— Textual 版出问题时，它可以用来回答"是界面错了还是协议错了"；
  * **允许被扔掉**。第二期之后它的位置可以由 CLI 顶上。

所以它刻意做得很笨：分块打印、不重绘、不做布局。**它不认识协议** —— 所有
`t:"event"` / `permission_request` 的解读都在 `ProtocolClient` 和那几个回调里。
"""

import sys
import threading
from typing import Any

from agent_runtime.protocol import messages
from agent_runtime.protocol.client import ProtocolClient

# 状态那一行的几种样子。**不用颜色** —— Windows 中文控制台在 cp936 下对 ANSI 转义
# 的支持取决于终端，而一个把启动搞崩的装饰比没有装饰糟得多（`frontends/cli` 里那个
# 纯 ASCII 横幅就是为同一件事）。
_MARK = {
    "idle": "·",
    "working": "…",
    "waiting_permission": "?",
    "waiting_human": "?",
    "finished": "✓",
    "limited": "!",
    "failed": "✗",
    "cancelled": "-",
}


class AnsiFrontend:
    """实现 `ClientHooks`：收到什么就打什么。"""

    def __init__(self) -> None:
        self._answering = threading.Event()
        # 审批要在**主线程**问（`input()` 从 stdin 读，而读线程在读 stdout ——
        # 两者不冲突，但让两个线程都调 `input()` 会抢输入）。所以读线程把请求放进
        # 队列，主线程去问。
        self._pending: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    # -- ClientHooks -----------------------------------------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        kind = message.get("t")

        if kind == messages.OUT_INIT:
            self._print_init(message)
        elif kind == messages.OUT_SESSION_LOAD:
            count = len(message.get("messages") or [])
            self._out(f"（会话里有 {count} 条消息）")
        elif kind == messages.OUT_EVENT:
            self._print_event(message)
        elif kind == messages.OUT_UI:
            self._out("")
            self._out(f"agent> {message.get('answer', '')}")
            self._answering.set()
        elif kind == messages.OUT_NOTICE:
            level = message.get("level", "info")
            self._err(f"[{level}] {message.get('text', '')}")

    def on_permission(self, request: dict[str, Any]) -> str:
        """**在终端上问。这是第一期唯一能证明"审批走协议"的地方。**

        值得注意的是它和 `cli_asker` 的**分工**：这里只管"选哪个"，而
        "键代表什么"（t 记什么、a 放行哪一组）全在子进程那一侧 ——
        前端连 `remember` 里装的是什么都不用懂。
        """
        self._out("")
        self._out(f"╭─ 需要审批 ─ {request.get('tool')}  风险 {request.get('risk')}")
        for name, value in (request.get("arguments") or {}).items():
            self._out(f"│ {name} = {value}")
        hint = request.get("remember_hint")
        if hint:
            self._out(f"│ t = {hint}")
        trust = request.get("trust_all_hint")
        if trust:
            self._out(f"│ a = {trust}")
        keys = "[y/N" + ("/t" if hint else "") + ("/a" if trust else "") + "]"
        return self._ask(keys)

    def on_question(self, request: dict[str, Any]) -> tuple[str, str]:
        self._out("")
        self._out(f"╭─ 提问 ─ {request.get('question')}")
        for index, option in enumerate(request.get("options") or [], 1):
            self._out(f"│   {index}) {option}")
        answer = self._ask("直接回车 = 跳过")
        if not answer:
            return "skipped", ""
        return "answered", answer

    # -- 输入 ------------------------------------------------------------------

    def _ask(self, keys: str) -> str:
        self._err(f"│ 是否执行？{keys} ", end="")
        try:
            line = input().strip().lower()
        except (EOFError, OSError):
            self._err("")
            # **读不到输入按拒绝**：默认放行等于"无人值守时静默执行中风险操作"，
            # 默认拒绝才是安全的失败方向（和 `cli_asker` 一字不差）。
            return messages.DENY
        self._answering.clear()
        if line in ("t",) and "t" in keys:
            return messages.ALWAYS
        if line in ("a",) and "a" in keys:
            return messages.ALWAYS_GROUP
        return messages.ALLOW if line in ("y", "yes") else messages.DENY

    # -- 打印 ------------------------------------------------------------------

    def _print_init(self, message: dict[str, Any]) -> None:
        tools = message.get("tools") or []
        self._out(f"会话 {message.get('session_id')}"
                  f"{'（继续）' if message.get('resumed') else '（新的）'}"
                  f"  模型 {message.get('model')}  最多 {message.get('max_steps')} 步")
        self._out(f"工具 {len(tools)} 个："
                  + "、".join(f"{t['name']}({t['risk']})" for t in tools))
        permissions = message.get("permissions") or {}
        if permissions:
            # **只显示非默认项**：默认时它是空的，于是这一行根本不出现。
            # 判断"什么算非默认"在 runtime 那一侧（决策 14）。
            self._out("非默认权限：" + "；".join(
                f"{key}={value}" for key, value in permissions.items()
            ))
        for notice in message.get("notices") or []:
            # **走 stdout**：`init.notices` 是"这次运行的事实"（技能、权限、MCP），
            # 它是界面内容，不是诊断。诊断（子进程的 traceback、`[warn]`）才走
            # stderr —— 那是子进程直接继承过去的。
            self._out(f"[{notice.get('code')}] {notice.get('text')}")

    def _print_event(self, message: dict[str, Any]) -> None:
        kind = message.get("kind")
        step = message.get("step")
        if kind == "model_call":
            status = message.get("status")
            if status == "ok":
                self._out(f"  · 第 {step} 步  模型 {message.get('duration_ms')}ms"
                          f"  {message.get('prompt_tokens', 0)} token"
                          f"（命中 {message.get('cached_tokens', 0)}）")
            else:
                self._out(f"  · 第 {step} 步  模型调用失败（{message.get('error', '')}）")
        elif kind == "tool_call":
            index = message.get("tool_index")
            at = f"[{index + 1}]" if isinstance(index, int) else ""
            self._out(f"  → {at} {message.get('tool')}({message.get('arguments')})")
        elif kind == "tool_result":
            self._out(f"  ← {message.get('tool')}  {message.get('status')}"
                      f"  {message.get('chars')} 字符  {message.get('duration_ms')}ms")
        elif kind == "permission":
            self._out(f"  · 权限 {message.get('tool')} → {message.get('outcome')}")
        elif kind == "tool_batch":
            self._out(f"  · {message.get('calls')} 个只读工具并发"
                      f"（{message.get('wall_ms')}ms）")
        elif kind == "run_finished":
            self._out(f"  · 回合结束：{message.get('stop_reason')}"
                      f"  {message.get('duration_ms')}ms")

    def _out(self, text: str) -> None:
        print(text, file=sys.stdout, flush=True)

    def _err(self, text: str, end: str = "\n") -> None:
        # 提示符走 stderr（和 CLI 一致）：stdout 留给"用户问 + agent 答"。
        print(text, end=end, file=sys.stderr, flush=True)


USAGE = (
    "用法：python -m agent_runtime.frontends.ansi [--session <id>]\n"
    "  一个最小的协议客户端：把 runtime 的协议画成一行行文本。\n"
    "  **它不是 TUI** —— 它的用处是验协议（见那个模块的 docstring）。"
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0

    session = None
    if "--session" in argv:
        index = argv.index("--session")
        try:
            session = argv[index + 1]
        except IndexError:
            print("--session 后面要跟一个会话 id", file=sys.stderr)
            return 2

    frontend = AnsiFrontend()
    with ProtocolClient(frontend, session=session) as client:
        frontend._out("输入内容回车发送。空行或 exit 退出。")
        frontend._out("（每次回车之后，agent 干活期间这里会安静一会儿 —— 没有流式。）")

        def read_stdin() -> None:
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    client.shutdown()
                    return
                if not line.strip() or line.strip().lower() in ("exit", "quit"):
                    client.shutdown()
                    return
                frontend._answering.clear()
                client.user_message(line.strip())
                # **等这一轮结束再读下一句**：两条回合叠着跑会让事件顺序错乱，
                # 而"顺序"是这条协议唯一的同步手段（子进程那边也会 join，
                # 所以这里等只是让界面上的提示符出现在正确的位置）。
                frontend._answering.wait()

        thread = threading.Thread(target=read_stdin, name="stdin", daemon=True)
        thread.start()
        code = client.wait()

    if code != 0:
        frontend._err(f"[协议] 子进程以退出码 {code} 结束")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
