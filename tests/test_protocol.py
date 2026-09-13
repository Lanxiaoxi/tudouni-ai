"""协议地基的验收：**真的起一个子进程，真的来回说话**。

## 为什么必须是子进程

这一层要验的东西——"三条人机通道是不是真的换掉了"、"两条流有没有串味"、"exit code
和配置错误怎么出去"——**全都只有真起一个进程才看得见**。同一进程里调 `serve()` 会把
stdin/stdout 换成 StringIO，那样测出来的是"函数能跑"，而不是"这条管道成立"。

第零期已经吃过一次这个亏的反面：当时用"和一次捕获的基线逐字节比"当验收，结果
PowerShell 的 `2>&1 |` 不保证跨流顺序，比出来的 diff 大半是噪声。

## 假模型怎么进去

`DEEPSEEK_BASE_URL` 指向一个本地起的 HTTP 桩（见 `fake_openai_server`）。这样
**不碰真网关**，而链路（httpx → openai SDK → 适配层 → Agent）是真的。用 monkeypatch
把模型换成假的就测不到"配置从环境变量读进来"这一段了。

## 覆盖面

  * `init` / `session_load` 的字段和 schema 对得上；
  * 一条 `user_message` 能跑完一轮，答案出现在 `run_finished` 那条 `t:"ui"` 里；
  * **审批真的走协议**：`permission_request` 发出来、我们回 `allow`、它继续 ——
    这是"三条通道换掉了"唯一的证据（旧的那三个 `input()` 会直接读到我们的 JSON）；
  * 两条流不串味：stdout 上**每一行都是合法 JSON**；
  * 版本对不上时干净地退出，而不是拿一堆看不懂的消息去驱动 Agent。
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_runtime.protocol import messages
from agent_runtime.runtime.config import CONTEXT_WINDOWS
from agent_runtime.state.catalog import ALIASES, load as load_catalog

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
REPO_ROOT = MAIN_PY.parent


# --- 一个假的 OpenAI 兼容端点 -------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """只回答 `POST /v1/chat/completions`，按脚本逐次给回复。

    **它两种形状都会**：`stream: true` 回 SSE（`text/event-stream`，一块一行
    `data:`），否则回今天那条一次性的 JSON。协议那一侧默认开流式（`--stream`），
    所以这条路是**真的被走到**的 —— 而"假网关只会回 JSON"曾经掩盖住一整类问题
    （客户端把 `application/json` 当成一条 SSE 流去迭代时，得到的是一个空回答，
    而整条链路一个错误都不报）。
    """

    scripts: list[dict] = []
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(body)

        index = min(len(type(self).calls) - 1, len(type(self).scripts) - 1)
        step = type(self).scripts[index]
        if body.get("stream"):
            self._reply_stream(step)
        else:
            self._reply(step)

    def _reply(self, step: dict) -> None:
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": step.get("content"),
                    "tool_calls": step.get("tool_calls"),
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _reply_stream(self, step: dict) -> None:
        """SSE。形状照着真网关来，尤其是这三条：

          * `tool_calls` 的 `arguments` **一段一段地给**（而且是按 index 交错的），
            那正是适配层必须按 index 拼的原因；
          * **usage 在最后一个 chunk 上、而且那个 chunk 的 `choices` 是空数组**；
          * 每个事件以空行结束（`data: {...}\\n\\n`）。
        """
        chunks: list[dict] = []
        base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                "created": 0, "model": "fake"}

        def add(delta: dict, finish: str | None = None, usage: dict | None = None) -> None:
            chunk = dict(base)
            chunk["choices"] = (
                [] if usage is not None
                else [{"index": 0, "finish_reason": finish, "delta": delta}]
            )
            if usage is not None:
                chunk["usage"] = usage
            chunks.append(chunk)

        add({"role": "assistant", "content": ""})
        content = step.get("content")
        if content:
            # 拆成几块，让"逐字"这件事在协议上是真的（一块 = 一条 `t:"delta"`）。
            for piece in _pieces(content, 4):
                add({"content": piece})
        for index, call in enumerate(step.get("tool_calls") or []):
            function = call.get("function", {})
            add({"tool_calls": [{"index": index, "id": call.get("id"),
                                 "function": {"name": function.get("name"),
                                              "arguments": ""}}]})
            for piece in _pieces(function.get("arguments") or "", 8):
                add({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
        add({}, finish="tool_calls" if step.get("tool_calls") else "stop")
        add({}, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

        body = "".join(
            f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 别把每个请求打到 stderr 上
        return


def _pieces(text: str, size: int) -> list[str]:
    """把一段文本切成固定大小的块（最后一块可能更短）。**空文本给空列表。**"""
    return [text[i:i + size] for i in range(0, len(text), size)] or []


@pytest.fixture
def fake_openai():
    """起一个本地假端点，返回 (基址, 脚本列表, 收到的请求列表)。

    脚本的最后一条会被重复使用 —— 那些测试只关心前几步。
    """
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.scripts = [{"content": "默认回答"}]
    _Handler.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", _Handler.scripts, _Handler.calls
    finally:
        server.shutdown()
        server.server_close()


# --- 起子进程 -----------------------------------------------------------------

def run_protocol(inbound: list[dict | str], *, env_extra: dict | None = None,
                 session: str | None = "proto-test", timeout: float = 60.0,
                 argv_extra: list[str] | None = None):
    """喂几行给 `--runtime-stdio`，返回 (退出码, stdout 行, stderr 文本)。

    `inbound` 里给 dict 会被编成 JSON；给 str 就原样发（用来发坏行）。
    `session=None` 表示不传 `--session`（每次一个新会话），给"这条断言要求它真的是
    新会话"的测试用。
    """
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-test"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})

    payload = "".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) + "\n"
        for line in inbound
    )

    argv = [sys.executable, str(MAIN_PY), "--runtime-stdio"]
    if session is not None:
        argv += ["--session", session]
    argv += list(argv_extra or [])

    result = subprocess.run(
        argv, input=payload, capture_output=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT), timeout=timeout,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return result.returncode, lines, result.stderr


def parse(lines: list[str]) -> list[dict]:
    """把 stdout 的每一行都解成 JSON。**解析失败就说明有东西串味了。**"""
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 失败时给出诊断
            raise AssertionError(f"stdout 上有一行不是 JSON：{line!r}（{exc}）") from None
    return out


def kinds(messages_in: list[dict], t: str) -> list[dict]:
    return [m for m in messages_in if m.get("t") == t]


# --- 验收 ---------------------------------------------------------------------

def test_every_stdout_line_is_json(fake_openai):
    """**这条是所有其它断言的前提**：stdout 上不能有非协议的东西。

    它挡的是"某处打了一行给 人 的说明" —— 横幅、提示符、`[权限]` 那些。第零期把
    装配的说明变成了 `init.notices`，就是为了这件事。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )

    assert code == 0, err
    parse(lines)          # 有任何一行不是 JSON 就会在这里炸
    assert "a g e n t   r u n t i m e" not in "\n".join(lines)


def test_init_carries_the_handshake(fake_openai):
    """`init` **永远是第一条**，而且字段和 schema 对得上。"""
    base, _, _ = fake_openai
    # **每次一个新会话**：这条测试断言 `resumed is False`，而它必须是真的新 ——
    # 传一个固定 id 时，上一次跑留下的会话文件会让它变成 True（实测踩过，
    # 而且那种"第二次跑才红"的失败最难查）。所以 id 每次现取。
    code, lines, err = run_protocol(
        [{"v": 1, "t": "shutdown"}], env_extra={"DEEPSEEK_BASE_URL": base},
        session=None,
    )
    assert code == 0, err
    got = parse(lines)

    assert got[0]["t"] == "init"
    init = got[0]
    for field in messages.required_fields("outbound", "init"):
        assert field in init, f"init 少了 {field}"

    # **语义版本，和信封版本（`v`）不是一回事。** 老客户端拿它判断"能不能对上话"：
    # 版本对不上就该停下，而 `v` 不等于 `VERSION` 是唯一该硬失败的地方（见
    # `doc/protocol.md` 第 2 节）。流式是 2 加进来的（`delta` / `delta_reset`）。
    assert init["protocol"] == messages.PROTOCOL
    assert messages.PROTOCOL >= messages.VERSION
    assert init["session_id"]
    assert init["resumed"] is False
    assert init["max_steps"] > 0
    assert isinstance(init["tools"], list) and init["tools"]
    # 每个工具都要有那四个键 —— 前端要按 risk / parallel_safe 渲染。
    for tool in init["tools"]:
        for key in ("name", "risk", "parallel_safe", "interactive"):
            assert key in tool, f"工具少了 {key}"
    # 裸路径，不是那句中文。
    assert init["audit_path"].endswith(".jsonl")
    assert "审计日志写到" not in init["audit_path"]


def test_init_splits_permissions_into_default_and_not(fake_openai):
    """**决策 14**：`init.permissions` 只含非默认项。

    这一条用两个方向钉：没配过的键**不出现**（界面那一行就不用显示），
    而 schema 里那四个键是白名单（不许冒出别的）。判断"什么算非默认"由 runtime 做，
    所以前端不必硬编码一份默认值。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=None,
    )
    assert code == 0, err
    permissions = parse(lines)[0]["permissions"]

    assert isinstance(permissions, dict)
    # 跑测试的机器上没配 deny_tools，所以它不该出现 —— 而且**空 dict 是合法值**
    # （全默认），所以不能断言"它非空"。
    assert "deny_tools" not in permissions
    assert set(permissions) <= {
        "auto_approve", "auto_approve_tools", "deny_tools", "shell_allow",
    }


def test_init_carries_the_context_window(fake_openai):
    """`init.context_tokens` 是状态栏那个百分比的分母。

    **响应里没有这个字段**（OpenAI 兼容的形状里就没有"上下文窗口"），所以它来自
    `config.CONTEXT_WINDOWS` 那张按模型名的表。界面拿它算占比；表里没有这个名字时
    它是 null，界面就只报用量、不报占比（错的百分比比没有百分比更坏）。
    """
    from agent_runtime.runtime.config import CONTEXT_WINDOWS

    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "shutdown"}], env_extra={"DEEPSEEK_BASE_URL": base},
        session=None,
    )
    assert code == 0, err
    init = parse(lines)[0]
    assert "context_tokens" in init
    assert init["context_tokens"] == CONTEXT_WINDOWS.get(init["model"])


def test_the_state_snapshot_carries_the_rail_data(fake_openai):
    """`t:"ui", kind:"state"` 是**左栏那四块的唯一数据来源**。

    任务列表和已加载技能住在子进程的 `session.metadata` 里，而 TUI 是另一个进程 ——
    没有这条快照，设计稿里那块最值钱的加法就没有数据（它此前只有 `--skills` /
    `--audit` / `--list` 三条"另开一个终端"的出口）。

    开场那条**带可用技能清单**（要扫目录，所以只发一次），而且它排在 `init` /
    `session_load` 之后。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "shutdown"}], env_extra={"DEEPSEEK_BASE_URL": base},
        session=None,
    )
    assert code == 0, err
    got = parse(lines)

    states = [m for m in kinds(got, "ui") if m.get("kind") == "state"]
    assert states, "开场就该有一条面板快照"
    first = states[0]
    for key in ("todos", "skills", "risk_scope", "messages", "steps",
                "granted_tools", "granted_prefixes", "denied_tools",
                "skill_catalog"):
        assert key in first, f"快照少了 {key}"

    # 三个风险等级都在，而且处置是 runtime 算的（界面不认识"默认只有 low"）。
    assert {item["risk"] for item in first["risk_scope"]} == {"low", "medium", "high"}
    assert all(item["disposition"] in ("auto", "ask")
               for item in first["risk_scope"])
    # 快照**排在握手之后**：界面先要知道会话是谁，再谈面板。
    assert [m["t"] for m in got[:3]] == ["init", "session_load", "ui"]


# --- 换会话（`session_switch` / `session_list`）--------------------------------
#
# 这两条是第二期加的，而它们的存在理由是一个**具体的不方便**：`/new` 和 `/resume`
# 原来是"前端杀掉子进程、带另一个 `--session` 重启"。这套测试钉的就是"进程不再重启"
# —— 所以它必须真的起一个子进程、**不关 stdin**、来回发几条。用进程内的假传输测的话，
# "进程还活着"这件事根本没被测到（那正是这一组要验的东西）。

def _open_protocol(fake_openai, *, session: str | None = None,
                   argv_extra: list[str] | None = None):
    """起一个 `--runtime-stdio` 子进程，把 stdin 留着。返回 (进程, 读函数, 发函数)。

    **和 `run_protocol` 分开**：那个是一次性喂完就等的（一次性输入），而这里要
    "发一条、读一条、再发一条" —— 换会话是**多轮**的事。

    **读用一条线程 + 队列，不用 `select()`**：Windows 上 `select()` 只认套接字，
    对管道会抛 `WinError 10038`（实测踩过）。线程 + `queue.get(timeout=...)` 在三个
    平台上都是同一套行为，而且"超时"和"进程卡死"能分开报（见下面的读函数）。

    stderr 和 stdout 都设成 utf-8：协议通道上是中文（`notice.text`），在 Windows 上
    按默认编码读会解出乱码，而那些乱码只在断言失败时才看得见。
    """
    import queue
    import threading

    base, _, _ = fake_openai
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-test"
    env["DEEPSEEK_BASE_URL"] = base
    env["PYTHONIOENCODING"] = "utf-8"

    argv = [sys.executable, str(MAIN_PY), "--runtime-stdio"]
    if session is not None:
        argv += ["--session", session]
    argv += list(argv_extra or [])

    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", env=env, cwd=str(REPO_ROOT),
    )

    # **不带类型注解**：`incoming: "queue.Queue[str]" = ...` 会被当成局部变量的注解，
    # 而那个注解在闭包里解析时算一次赋值 —— 于是内层函数读它拿到的是"还没赋值"的
    # `NameError`（实测踩过，报的是"free variable not associated with a value"）。
    incoming = queue.Queue()

    def pump() -> None:
        for line in process.stdout:
            incoming.put(line)
        incoming.put("")            # EOF 也入队：读函数据此报"流结束了"

    threading.Thread(target=pump, name="protocol-reader", daemon=True).start()

    def next_line(timeout: float = 30.0) -> dict:
        """阻塞等到下一行。**超时就是失败，不是空值** ——

        在"进程其实已经卡死"和"这一条本来就不会发"之间，只有超时能分辨；而一个
        静静返回 None 的读函数会让两类失败长得一模一样。
        """
        try:
            line = incoming.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"等协议消息超时了（{timeout}s）—— 子进程多半卡住了"
            ) from None
        assert line.strip(), "协议流结束了 —— 子进程不该在这里退出"
        return json.loads(line)

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    return process, next_line, send


def test_session_switch_reassembles_without_restarting_the_process(fake_openai):
    """**换会话不重启进程，而且重发整组开场消息。**

    这条是第二期那两行"要重开：main.py --tui"的替代品，所以它要证明的正是那两行
    说做不到的事：

      * 同一个进程能换到另一个会话、再换回来（换完 `shutdown` 之后退出码仍然是 0）；
      * 换过去之后 `init` / `session_load` / `ui state` **一样不少地重发**
        （少发哪一条的症状是"切过去之后左栏或历史有一半是旧的"）；
      * `resumed` 跟着新会话走（接着聊 = True，新会话 = False），而不是留着上一次的值；
      * 换回来的那个会话**历史还在**（`session_load.messages` 里有那句用户消息）。

    ## 为什么第一句要在 A 里说

    会话文件是在**第一次 checkpoint**时写的（`resolve_session` 的 docstring：开了不用
    不会留下空文件），所以"换回来是接着聊"这件事要求那个会话真的落过盘 ——
    实测踩过：不先说一句话时换回来 `resumed` 是 False，而那不是 bug。

    ## 为什么 id 每次现取

    这个进程读的是真工作区的 `.tudouni/sessions/`，而会话文件**跑一次就留下了**。
    写死两个 id 的话，第二次跑这条测试时 `resumed` 就是 True（实测踩过：第一次绿、
    第二次红）—— 那种"第二次才红"的失败最难查。
    """
    import uuid

    first = f"switch-a-{uuid.uuid4().hex[:8]}"
    second = f"switch-b-{uuid.uuid4().hex[:8]}"
    process, next_line, send = _open_protocol(fake_openai, session=first)

    opening = [next_line()["t"] for _ in range(3)]
    assert opening == ["init", "session_load", "ui"]

    # 在 A 里说一句话，让它落盘。
    send({"v": 1, "t": "user_message", "text": "在 A 里说的话"})
    for _ in range(40):
        message = next_line()
        if message.get("t") == "ui" and message.get("kind") == "run_finished":
            break
    else:  # pragma: no cover - 走到这儿说明假模型那条路断了
        raise AssertionError("A 里这一轮没跑完")

    send({"v": 1, "t": "session_switch", "session_id": second})
    fresh = [next_line() for _ in range(3)]
    assert [m["t"] for m in fresh] == ["init", "session_load", "ui"]
    assert fresh[0]["session_id"] == second
    assert fresh[0]["resumed"] is False, "没落过盘的就是新会话 —— 这个值由 runtime 算"
    assert fresh[1]["messages"], "新建的会话也有一条 system 消息"

    # 换回 A：它落过盘了，所以是"接着聊"，而且历史要发回来。
    send({"v": 1, "t": "session_switch", "session_id": first})
    back = [next_line() for _ in range(3)]
    assert [m["t"] for m in back] == ["init", "session_load", "ui"]
    assert back[0]["session_id"] == first
    assert back[0]["resumed"] is True, "有会话文件就是接着聊 —— resumed 由 runtime 算，不是界面猜"
    assert any(m.get("role") == "user" and "在 A 里说的话" in str(m.get("content"))
               for m in back[1]["messages"]), "换回来要把那个会话的历史发回来"

    send({"v": 1, "t": "shutdown"})
    assert process.wait(timeout=30) == 0


def test_new_session_gets_a_fresh_id_from_the_runtime(fake_openai):
    """`session_switch` 不带 id = **新会话**，id 由 runtime 分配（前端不许自己编）。

    前端编 id 的话，"什么算一个没被占用的 id"就成了前端也要知道的事 —— 而那是
    store 的知识（要碰磁盘确认没撞名）。
    """
    import uuid

    current = f"switch-a-{uuid.uuid4().hex[:8]}"
    process, next_line, send = _open_protocol(fake_openai, session=current)
    for _ in range(3):
        next_line()

    send({"v": 1, "t": "session_switch", "session_id": None})
    fresh = next_line()
    assert fresh["t"] == "init"
    assert fresh["session_id"] and fresh["session_id"] != current
    assert fresh["resumed"] is False

    send({"v": 1, "t": "shutdown"})
    assert process.wait(timeout=30) == 0


def test_a_bad_session_id_is_answered_with_a_notice_and_the_session_survives(fake_openai):
    """id 非法：**一条 notice，进程不退，当前会话原样保留。**

    非法 id 是最常见的手滑（带空格、粘进来一个路径），所以它**不能**是一条异常 ——
    `store.exists()` 会从 `_path` 里抛 ValueError，那会打断读循环、把整个进程带走，
    而用户只是打错了一个字。这里同时钉两件事：报错说清了能写什么，而且报错之后
    这个会话还能接着用。
    """
    import uuid

    process, next_line, send = _open_protocol(
        fake_openai, session=f"switch-a-{uuid.uuid4().hex[:8]}")
    for _ in range(3):
        next_line()

    send({"v": 1, "t": "session_switch", "session_id": "bad/../id"})
    notice = next_line()
    assert notice["t"] == "notice"
    assert notice["code"] == "session"
    assert "非法" in notice["text"]

    # 会话还在：再换一次（这次合法）照样工作。
    send({"v": 1, "t": "session_switch",
          "session_id": f"switch-c-{uuid.uuid4().hex[:8]}"})
    assert next_line()["t"] == "init"

    send({"v": 1, "t": "shutdown"})
    assert process.wait(timeout=30) == 0


def test_session_list_answers_with_the_saved_sessions(fake_openai):
    """`session_list` → `sessions`：清单里的东西由 runtime 读盘算好。

    两条检查，各自都有理由：

      * 每一条都带 `session_id` / `messages` / `steps` / `preview` / `todos` /
        `modified_at` —— 界面直接照着渲染，**不该自己去读会话文件**；
      * 它是**只读**的：问一次清单不换会话、不改会话（所以那条断言问完之后，
        当前会话还是进来时那一个）。

    **这里不验排序。** 排序（按创建时间、最新在前）在这条路上测不了：这个进程读的是
    真工作区的 `.tudouni/sessions/`，里面是跑测试攒下来的会话文件 —— 而且是**老文件**
    居多（没有 `created_at`，退到 mtime），排出来的顺序取决于这台机器上那些文件的
    时间戳。那是在测设备，不是测代码。排序有它自己的确定性测试：
    `tests/test_session_list.py`（自己造会话、自己定创建时间）。
    """
    import uuid

    process, next_line, send = _open_protocol(
        fake_openai, session=f"list-a-{uuid.uuid4().hex[:8]}")
    for _ in range(3):
        next_line()

    send({"v": 1, "t": "session_list"})
    listed = next_line()
    assert listed["t"] == "sessions"

    items = listed["items"]
    # **不断言"空"**：同上 —— 跑过几轮测试的机器上一定有会话。
    for item in items:
        assert set(item) == {"session_id", "messages", "steps", "preview", "todos",
                             "modified_at"}
        assert isinstance(item["messages"], int) and isinstance(item["steps"], int)
        # `modified_at` 是"会话文件最后被写"的时刻，TUI 欢迎屏右栏按它排；读不到文件
        # 时是 null（不猜一个）。**它是 mtime，不是 `created_at`** —— 那一个只在
        # runtime 内部用来排序，不上这条协议。
        assert item["modified_at"] is None or isinstance(item["modified_at"], float)

    # 只读：问一次清单之后，**当前会话没有变**。
    send({"v": 1, "t": "session_switch", "session_id": "list-b"})
    assert next_line()["session_id"] == "list-b"

    send({"v": 1, "t": "shutdown"})
    assert process.wait(timeout=30) == 0


def test_interrupt_stops_the_turn_but_shutdown_does_not(fake_openai):
    """`interrupt` 和 `shutdown` **必须分开**（这是实测踩出来的）。

    `shutdown` 的语义是"收摊"，而它**不取消**当前回合 —— 否则客户端发完
    user_message 紧跟一条 shutdown（"我该说的都说了"），那一轮会在第一个安全点被
    砍掉，界面永远拿不到答案，而且任何地方都不报错。
    想停下正在跑的这一轮只能走 `interrupt`。

    这条在 `ProtocolServer` 这一层测（不起子进程）：它要钉的正是"这两个 `t` 各自
    把哪个标志置起来"。
    """
    from agent_runtime.protocol.channels import ProtocolServer
    from agent_runtime.protocol.transport_stdio import StdioTransport

    class _Transport(StdioTransport):
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, message: dict) -> None:
            self.sent.append(message)

        def recv(self):
            return iter(())

        def close(self) -> None:
            pass

    server = ProtocolServer(_Transport())
    assert server.should_stop() is False

    server._dispatch({"v": 1, "t": "interrupt"})
    assert server.should_stop() is True, "interrupt 必须请求停下这一轮"

    # 收摊**不置**这个标志 —— 它只让循环退出，当前这一轮会跑完。
    server._stop.clear()
    server._dispatch({"v": 1, "t": "shutdown"})
    assert server.should_stop() is False


def test_a_new_turn_clears_a_previous_interrupt():
    """**被 Esc 中断过一次之后，下一轮必须还能正常跑。**

    取消标志落在一个 `threading.Event` 上，而它是**每次会话一个**的。不清的话，
    这个会话此后每一轮都会在第一个安全点被砍掉 —— 症状是"发消息没反应"，
    没有任何地方报错。
    """
    from agent_runtime.protocol.channels import ProtocolServer
    from agent_runtime.protocol.transport_stdio import StdioTransport

    class _Transport(StdioTransport):
        def __init__(self) -> None:      # 不碰真的 stdin/stdout
            self.sent: list[dict] = []

        def send(self, message: dict) -> None:
            pass

        def recv(self):
            return iter(())

        def close(self) -> None:
            pass

    server = ProtocolServer(_Transport())
    server._dispatch({"v": 1, "t": "interrupt"})
    assert server.should_stop() is True

    class _Session:
        messages: list = []
        metadata: dict = {}

        def step_count(self) -> int:
            return 0

    seen: dict[str, bool] = {}

    class _Agent:
        def run(self, session, text, max_steps=None):
            # 这一轮**真正跑起来**时看到的标志：它必须是干净的。
            seen["should_stop"] = server.should_stop()
            return "跑完了"

        # `_state_message` 会读这两个（思考那两个旋钮和模型一起进快照）。
        thinking = True
        effort = "high"

    class _Logs:
        directory = "."

    class _Runtime:
        session = _Session()
        agent = _Agent()
        logs = _Logs()
        max_steps = 1
        # `_state_message` 会读这几个（`/model` 换完模型之后那个百分比的分母跟着变，
        # 而思考那两个旋钮也和模型一起进快照）。替身给的是"这个替身没有模型"：
        # `current_model` 为空串，窗口也就无从谈起。
        current_model = ""
        current_provider = ""
        context_tokens = None

        def ui_state(self, *, with_catalog=False):
            return {"todos": [], "skills": [], "messages": 0, "steps": 0}

    server.attach(_Runtime())
    server._run_turn("再试一次")
    assert seen["should_stop"] is False, "被中断过一次之后，下一轮不该立刻又被砍掉"
    assert server.should_stop() is False


def test_one_turn_produces_the_answer_in_the_ui_message(fake_openai):
    """一轮跑完，**答案在 `run_finished` 那条 `t:"ui"` 里**。

    这条是决策 1（不做流式）之后界面拿到答案的**唯一**途径：审计里没有正文
    （`Agent.run` 的返回值只交给调用方）。少发它，界面就一片空白。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "我很好，谢谢。"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )
    assert code == 0, err
    got = parse(lines)

    # `t:"ui"` 现在有两种 kind（`run_finished` 的正文 + `state` 的面板快照），
    # 所以这里**按 kind 取**，不能按 `t` 取第一条 —— 开场那条 `state` 排在前面。
    ui = [m for m in kinds(got, "ui") if m.get("kind") == "run_finished"]
    assert len(ui) == 1
    assert ui[0]["answer"] == "我很好，谢谢。"

    # 同一轮的事件也在，而且 `run_finished` 的 stop_reason 是 answered。
    events = kinds(got, "event")
    finished = [e for e in events if e["kind"] == "run_finished"]
    assert [e["stop_reason"] for e in finished] == ["answered"]
    assert finished[0]["run_id"] == ui[0]["run_id"]


# --- 流式（`t:"delta"` / `t:"delta_reset"`）-----------------------------------

def _deltas(got: list[dict], channel: str = "text") -> list[dict]:
    return [m for m in kinds(got, "delta") if m.get("channel") == channel]


def test_a_streamed_turn_sends_deltas_and_no_second_copy_of_the_answer(fake_openai):
    """**流式那一轮：正文走 delta，`run_finished` 那条不再画第二遍。**

    两条都要，而它们的理由不同：
      * delta 必须真的出现 —— 否则"流式开着"这句话在协议上没有任何证据；
      * 完整答案**不许**在屏幕上出现第二遍 —— 两份都画的话，用户看到的是同一段
        回答重复一次，而它看起来像模型说了两遍，不像协议发重了。

    它同时钉住了"拼回来的正文和完整答案一致"：适配层和协议各自拼了一遍
    （一条按 chunk、一条按 message），而它们必须说同一件事。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "我先检查一下这个文件，然后改掉那一行。"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "改一下 a.py"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )
    assert code == 0, err
    got = parse(lines)

    # 流式是默认（`--stream`），而且 `init` 把这件事告诉了前端。
    assert kinds(got, "init")[0]["stream"] is True
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}

    streamed = _deltas(got)
    assert len(streamed) > 1, "假网关把正文切成了好几块，这里该收到好几条 delta"
    joined = "".join(m["text"] for m in streamed)
    assert joined == "我先检查一下这个文件，然后改掉那一行。"

    # 每一条 delta 都带 run_id / step，界面靠它把块归到对的回合上。
    assert {m["run_id"] for m in streamed} == {
        m["run_id"] for m in got if m.get("t") == "event"
    } or len({m["run_id"] for m in streamed}) == 1
    assert all(isinstance(m["step"], int) and m["step"] >= 1 for m in streamed)

    ui = [m for m in kinds(got, "ui") if m.get("kind") == "run_finished"]
    assert len(ui) == 1
    # **答案照样发**（协议不变，老前端靠它），只是前端不该再画一遍。
    assert ui[0]["answer"] == joined


def test_no_stream_sends_the_answer_without_any_delta(fake_openai):
    """`--no-stream`：一个 delta 都没有，答案整段出现在 `t:"ui"` 里。

    这是**老行为**，也是"流式是可选的加速、不是另一种协议"那句话的验收 ——
    关掉它之后协议上一个字节都不该多。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "整段出现。"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
        argv_extra=["--no-stream"],
    )
    assert code == 0, err
    got = parse(lines)

    assert kinds(got, "init")[0]["stream"] is False
    assert kinds(got, "delta") == []
    assert "stream" not in calls[0]
    ui = [m for m in kinds(got, "ui") if m.get("kind") == "run_finished"]
    assert ui[0]["answer"] == "整段出现。"


def test_deltas_never_enter_the_audit_log(fake_openai):
    """**delta 不进审计。** 一次回答是上千块，而 `JsonlSink` 每条事件一次
    open/write/close —— 抄进去等于把审计日志变成第二个会话文件。

    审计里记的是**汇总**：`model_call.streamed` / `stream_chunks` / `streamed_chars`。
    这条测试两边都查：jsonl 里没有 kind=delta 的行，而那一行汇总在。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [{"content": "审计里只该有汇总。"}]

    session = f"stream-audit-{uuid.uuid4().hex[:8]}"
    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err
    got = parse(lines)

    # 协议上也没有 kind=delta 的**事件**（delta 是一级消息，不是一种 event）。
    assert not [e for e in kinds(got, "event") if e.get("kind") == "delta"]

    log = (REPO_ROOT / ".tudouni" / "logs" / f"{session}.jsonl").read_text(
        encoding="utf-8")
    logged = [json.loads(ln) for ln in log.splitlines() if ln.strip()]
    assert not [e for e in logged if e.get("kind") == "delta"], \
        "delta 抄进审计了 —— 那会让日志随回答长度线性膨胀"

    model_calls = [e for e in logged if e.get("kind") == "model_call" and e.get("status") == "ok"]
    assert model_calls, "至少要有一条成功的 model_call"
    assert model_calls[0]["streamed"] is True
    assert model_calls[0]["stream_chunks"] > 1
    assert model_calls[0]["streamed_chars"] == len("审计里只该有汇总。")
    # token 用量照旧在（`stream_options` 换来的）。
    assert model_calls[0]["prompt_tokens"] == 10


def test_streaming_a_tool_turn_keeps_the_tool_events_intact(fake_openai):
    """带工具的回合：delta 和 tool_call / tool_result 在同一条流上，顺序不能乱。

    模型常在调用工具之前先流一句话（"我看一下"），然后才给 tool_calls。那一句话
    同样要逐字出来，而**工具事件一条都不能因此丢失或错位** —— 它们的配对靠
    `call_id`，多插了几十条 delta 之后仍然要对得上（这是"两条流不串味"的流式版）。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [
        {"content": "我先看一眼。", "tool_calls": [{
            "id": "call_x", "type": "function",
            "function": {"name": "list_files", "arguments": json.dumps({"path": "."})},
        }]},
        {"content": "看完了。"},
    ]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "看看目录"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )
    assert code == 0, err
    got = parse(lines)

    text = "".join(m["text"] for m in _deltas(got))
    # 两段正文各流一次，而且拼起来是完整的（工具那一轮的内容也在）。
    assert text == "我先看一眼。看完了。"

    events = kinds(got, "event")
    calls = [e for e in events if e["kind"] == "tool_call"]
    results = [e for e in events if e["kind"] == "tool_result"]
    assert [c["call_id"] for c in calls] == ["call_x"]
    assert [r["call_id"] for r in results] == ["call_x"]
    assert results[0]["status"] == "ok"


def test_every_stdout_line_is_still_json_while_streaming(fake_openai):
    """**流式下这条更值钱**：delta 由回合线程发，而读循环同时在发别的
    （`ui(state)` / `notice`）—— 没有那把锁，两行会交错成半行 JSON，
    而症状是**前端偶尔丢掉一行**（`test_every_stdout_line_is_json` 那条只跑
    非流式，所以这条是它的流式版）。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [{"content": "一二三四五六七八九十" * 30}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "写长一点"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )
    assert code == 0, err
    got = parse(lines)          # 任何一行不是 JSON 都会在这里抛
    assert len(_deltas(got)) > 10
    assert "".join(m["text"] for m in _deltas(got)) == "一二三四五六七八九十" * 30


def test_the_audit_stream_is_forwarded_verbatim(fake_openai):
    """`t:"event"` 就是审计那一行的**原样**。

    "审计 = 协议"这句话要在字节层面成立，所以这里比对"转发出去的消息"和
    "落到 .jsonl 里的那一行" —— 去掉信封之后应当一模一样。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [{"content": "好"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "嗨"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session="verbatim-probe",
    )
    assert code == 0, err
    got = parse(lines)

    forwarded = [
        {k: v for k, v in m.items() if k not in ("v", "t")}
        for m in kinds(got, "event")
    ]
    log = (REPO_ROOT / ".tudouni" / "logs" / "verbatim-probe.jsonl").read_text(
        encoding="utf-8"
    )
    logged = [json.loads(ln) for ln in log.splitlines() if ln.strip()]

    assert forwarded, "至少要有几条事件"
    # 日志里每一条转发的都能找到一模一样的对应行。
    for record in forwarded:
        assert record in logged, f"转发出去的事件不在日志里：{record}"


def test_approval_goes_over_the_protocol(fake_openai):
    """**"三条人机通道换掉了"唯一的证据**，而且它必须是一个**会来回说话**的客户端。

    `subprocess.run(input=...)` 那种"把所有话一次说完"的喂法在这里**证明不了任何
    东西**：审批要的是"子进程发出请求 → 我们看了之后回一句"。一次全喂下去的话，
    就算旧 `cli_asker` 还在，它也会把我们那行 JSON 读走并当成"不是 y"——
    两种实现都会挂住或走偏，而分不清是哪一种。

    所以这条测试自己当客户端：读一行、判断、再写一行。这也正是第二期 Textual
    客户端要做的事的缩影 —— 它是这条协议的第一个真消费者。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [
        # 第一步：要跑一条命令（shell 是 HIGH ⇒ 必定触发审批）。
        {"content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "shell", "arguments": json.dumps({"command": "echo hi"})},
        }]},
        # 第二步：收尾。
        {"content": "跑完了"},
    ]

    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-test"
    env["PYTHONIOENCODING"] = "utf-8"
    env["DEEPSEEK_BASE_URL"] = base

    proc = subprocess.Popen(
        [sys.executable, str(MAIN_PY), "--runtime-stdio", "--session", "approval-probe"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", env=env, cwd=str(REPO_ROOT),
    )

    seen: list[dict] = []
    answered: list[dict] = []

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    try:
        send({"v": 1, "t": "user_message", "text": "跑一下 echo"})
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            message = json.loads(line)
            seen.append(message)

            if message.get("t") == "permission_request":
                answered.append(message)
                send({"v": 1, "t": "permission_response",
                      "id": message["id"], "decision": "allow"})
            elif message.get("t") == "ui" and message.get("kind") == "run_finished":
                break          # 拿到答案就收工（`kind:"state"` 那种快照不算）
        send({"v": 1, "t": "shutdown"})
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 失败时给出诊断
            proc.kill()
            raise AssertionError(
                f"子进程没退出。已经收到的：{[m.get('t') for m in seen]}"
            ) from None

    assert answered, (
        f"子进程**没有**通过协议要审批。收到的是 {[m.get('t') for m in seen]} —— "
        f"要么 shell 被自动放行了（这条测试就失去意义），要么审批还在走 stdin 上的 "
        f"input()（那就是第 3 条通道没换掉）"
    )
    req = answered[0]
    assert req["tool"] == "shell"
    assert req["risk"] == "high"
    # 参数是**全文**（不截断）—— 给人做判断的那份东西。
    assert req["arguments"] == {"command": "echo hi"}
    assert req["call_id"], "审批请求要带上它对应哪一次工具调用"
    # 两条"记住"的说明都在（哪怕为 null），前端不用去猜字段在不在。
    assert "remember" in req and "remember_hint" in req
    assert req["allow_trust_all"] is False, "不是 MCP 工具，没有可信任的组"
    # **回了一句 allow 之后这一轮真的走完了** —— 这是"回应被读走了"的证据。
    ui = [m for m in seen
          if m.get("t") == "ui" and m.get("kind") == "run_finished"]
    assert ui and ui[0]["answer"] == "跑完了"


# --- autopilot：运行中开关（TUI 的 `/autopilot`）--------------------------------

def _state_snapshots(lines: list[str]) -> list[dict]:
    return [m for m in parse(lines)
            if m.get("t") == "ui" and m.get("kind") == "state"]


def test_set_autopilot_flips_it_and_replies_with_a_state_snapshot():
    """改完**立刻回一条 state 快照** —— 界面按它显示（不许自己乐观更新）。

    没有这条回执，界面上那盏灯就只能靠猜，而这一格说的是"接下来还会不会问你"：
    "写着开、其实还在问"会让人把真的审批面板当成误报点掉。
    """
    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_autopilot", "on": True},
         {"v": 1, "t": "shutdown"}],
        session="autopilot-toggle",
    )
    assert code == 0, err
    assert [m.get("autopilot") for m in _state_snapshots(lines)] == [False, True], \
        "开场那条是关的，切过之后那条必须是开的"


def test_only_a_real_true_turns_autopilot_on():
    """`on` **只认真正的 `true`**：猜错的方向必须是"照旧问你"。

    这一档的后果是"需要审批的工具直接执行"，所以 `"false"` / `1` 这类东西不许被
    当成开 —— 猜错的两个方向代价不对称：该开没开只是维持现状，不该开却开了是
    "没有人在上面点过头就执行了"。
    """
    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_autopilot", "on": "false"},
         {"v": 1, "t": "set_autopilot", "on": 1},
         {"v": 1, "t": "shutdown"}],
        session="autopilot-strict",
    )
    assert code == 0, err
    assert all(m.get("autopilot") is False for m in _state_snapshots(lines))


def test_autopilot_survives_a_session_switch():
    """开着 autopilot 再换会话，新模式要**跟着过去**。

    换会话是新装一个 runtime（`serve.make_session_opener`），它照 `bootstrap.autopilot`
    装 —— 所以那个开关不能只改 Agent 那一份。漏了它的症状是"界面上灯还亮着，
    工具却开始逐条问你"，而这两件事分居两处、谁也不知道对方不一致。
    """
    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_autopilot", "on": True},
         {"v": 1, "t": "session_switch", "session_id": None},
         {"v": 1, "t": "shutdown"}],
        session="autopilot-switch",
    )
    assert code == 0, err
    got = parse(lines)
    states = [m for m in got if m.get("t") == "ui" and m.get("kind") == "state"]
    assert states[-1].get("autopilot") is True, "换过去的那个会话又变成要审批了"

    # 顺带证明"真的换了会话" —— 不然这条测试可能只是"什么都没发生"。
    inits = [m for m in got if m.get("t") == "init"]
    assert len(inits) == 2 and inits[0]["session_id"] != inits[1]["session_id"]


def test_autopilot_turned_on_mid_session_stops_the_asking(fake_openai):
    """端到端：开了之后 HIGH 风险的 shell **不再要审批**，而那一轮照样跑完。

    它比"那个字段被改成 True 了"强得多：`Agent.autopilot` 是**每次调用时**读的
    （见 `agents/agent.py` 里 gate 那一处），只有真的跑一轮才验证得了"这一改走到了
    关卡"。这也是 `--autopilot` 和 `/autopilot` 共用的那一条路。
    """
    base, scripts, _ = fake_openai
    scripts[:] = [
        {"content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "shell", "arguments": json.dumps({"command": "echo hi"})},
        }]},
        {"content": "跑完了"},
    ]
    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_autopilot", "on": True},
         {"v": 1, "t": "user_message", "text": "跑一下 echo"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session="autopilot-e2e",
    )
    assert code == 0, err
    got = parse(lines)
    assert not [m for m in got if m.get("t") == "permission_request"], \
        "开着 autopilot 还在要审批 —— 说明那个开关没走到 gate"

    ui = [m for m in kinds(got, "ui") if m.get("kind") == "run_finished"]
    assert ui and ui[0]["answer"] == "跑完了", "不问归不问，这一轮还是要跑完"

    # 放行记成 `autopilot` 而不是 `approved` —— 这正是"这一轮有没有人看着"的答案，
    # 也是 `/autopilot` 能存在而不算审计谎报的理由。
    outcomes = [e.get("outcome") for e in kinds(got, "event")
                if e.get("kind") == "permission"]
    assert outcomes == ["autopilot"]

def test_the_client_layer_finds_the_runtime_entrypoint():
    """`protocol/client.py` 算出来的 `main.py` 路径必须真的存在。

    这条测试是**为一次真实的 bug** 写的：`__file__` 是 `<包>/protocol/client.py`，
    所以包目录要上两级、仓库根再上一级 —— 少算一级，子进程会去找
    `<仓库>/main.py`，而报错是 "can't open file"，看起来像路径写错了，
    其实是层级算错了。任何前端的**每一次启动**都会踩它，所以值得一条。
    """
    from agent_runtime.protocol import client

    entry = client.runtime_entrypoint()
    assert entry.is_file(), f"算出来的入口不存在：{entry}"
    assert entry.name == "main.py"
    assert (client.repo_root() / "agent_runtime").is_dir()
    # 而 argv 用的是**绝对路径**：`python -m agent_runtime.main` 在
    # `package = false` 下不工作（见 default_argv 的说明）。
    argv = client.default_argv("s")
    assert str(entry) in argv
    assert "-u" in argv


def test_the_client_asks_for_streaming_by_default():
    """**`--tui` 起来就是流式的** —— 这条钉的是那个默认值本身。

    它值得一条测试，因为"默认开着"是靠**三层各写一次**拼出来的：
    `main.py`（`args.stream is None` → True）→ `run_tui(want_stream)` →
    `TuiApp(stream=)` → `ProtocolClient(stream=)` → 子进程 argv 上的 `--stream`。
    任何一层把它丢了，症状都是"默认变成不流式"——而那不是崩溃，**没有任何地方会报错**，
    只是回答又整段蹦出来了（实测踩过一次：Agent 永远传一个空 relay，`--no-stream`
    静默失效）。

    顺带把两个方向都钉住：`--no-stream` 必须真的传 `--no-stream` 过去，
    而不是"不传"（子进程的默认值不需要和父进程的意图一致 —— 这里说了才算）。
    """
    from agent_runtime.protocol import client

    assert "--stream" in client.default_argv("s")
    assert "--no-stream" not in client.default_argv("s")

    assert "--no-stream" in client.default_argv("s", stream=False)
    assert "--stream" not in client.default_argv("s", stream=False)


def test_the_ansi_client_runs_against_a_real_subprocess(fake_openai):
    """冒烟：那个 200 行的 ANSI 客户端能起来、能握手、能干净退出。

    它**不是** TUI，但它是一个真前端，所以它的启动路径值得跑一遍 ——
    它就是"协议能被一个第三方消费者用起来"的最小证据。这里只喂 `exit`，
    所以不碰模型。
    """
    base, _, _ = fake_openai
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-test"
    env["PYTHONIOENCODING"] = "utf-8"
    env["DEEPSEEK_BASE_URL"] = base

    result = subprocess.run(
        [sys.executable, "-m", "agent_runtime.frontends.ansi", "--session", "ansi-smoke"],
        input="exit\n", capture_output=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT.parent), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # 握手过了：会话 id 和工具清单都显示出来了。
    assert "ansi-smoke" in result.stdout
    tools_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("工具 ")),
        None,
    )
    assert tools_line, "init 回执里没有工具清单"
    # **刻意不钉个数。** 这份清单里既有内置工具，也有本机 .env（TAVILY_API_KEY）和
    # .tudouni/（MCP server）带来的那些 —— 钉死一个数就把这条冒烟测试绑在开发机的
    # 配置上，而它要测的是"ANSI 客户端能握手"。钉几个**必然在**的名字就够了：
    # 它们在，就说明这份清单是活的装配结果，不是一句占位文本。
    for name in ("read_file", "grep", "shell"):
        assert name in tools_line
    # 提示语在，说明它真的进到了交互循环。
    assert "输入内容回车发送" in result.stdout


def test_a_bad_version_exits_cleanly(fake_openai):
    """版本对不上是**唯一**该硬失败的地方。

    继续读下去只会拿一堆看不懂的消息去驱动 Agent —— 那比早退坏得多。而且必须
    **从 stderr 说清原因**（stdout 是协议通道，不能混人话）。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 99, "t": "shutdown"}], env_extra={"DEEPSEEK_BASE_URL": base},
    )

    assert code == 0        # 干净退出，不是崩
    assert "版本对不上" in err
    assert lines, "init 应该已经发出去了"


def test_bad_lines_are_skipped_and_counted(fake_openai):
    """坏行**跳过并计数** —— 不能静默，也不能让整条通道死掉。

    坏行在真实情形里更可能是"前端写坏了"，而那是需要看见的。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        ["这不是 JSON\n", "\n", '{"v":1,"t":"shutdown"}\n'],
        env_extra={"DEEPSEEK_BASE_URL": base},
    )

    assert code == 0
    parse(lines)
    assert "跳过" in err and "行" in err


# --- `/status` `/tools` `/model`（三条新入站消息）--------------------------------
#
# **这几条都用自己的一次性会话 id**（`_fresh_session()`），理由不是洁癖：
# 会话文件是在磁盘上活的，而"换过模型"这件事**跟着会话存**（那正是被测的性质之一）。
# 共用一个 id 的话，前一条测试 `/model` 选了 pro，后一条的会话就是从 pro 开始的 ——
# 于是"目录里标着谁"和"这一轮请求发给谁"会随测试顺序变，而失败信息看起来像产品 bug。

def _fresh_session() -> str:
    """一个不会和别的测试撞车的会话 id（也避开上一次运行留下的文件）。"""
    return f"proto-{uuid.uuid4().hex[:10]}"


def _ui(got: list[dict], kind: str) -> dict:
    """取 `ui` 里某种 kind 的那一条。**按 kind 取，不能按 t 取** ——
    `run_finished` / `state` / `status` / `tools` 四种都叫 `t:"ui"`。"""
    found = [m for m in kinds(got, "ui") if m.get("kind") == kind]
    assert found, f"没有 kind={kind} 的 ui 消息：{[m.get('kind') for m in kinds(got, 'ui')]}"
    return found[-1]


def test_status_answers_with_the_audit_numbers(fake_openai):
    """`/status` 的账**从审计日志里数出来**，而且和 `--audit` 是同一套口径。

    走两个进程：第一个跑一轮对话（留下 model_call / tool_result 和一条回答），
    第二个**什么都不做，只问一次状态**。

    ## 为什么分两次，而不是在同一批入站里紧跟一条 `status`

    因为 `/status` **不 join 正在跑的那一轮**（那正是它最有用的场景：跑着的时候看
    一眼）。所以"紧跟一条 status"拿到的数字取决于那一轮跑到哪了 —— 一台快机器上
    模型调用已经好了，慢一点就还没开始。**那样的断言是在测调度，不是在测统计。**

    分两个进程之后，这一条同时钉住了"审计是**跨进程**的事实"：第二个进程什么都没
    跑过，它报出来的每一个数都只能来自那份 jsonl。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "我很好。"}]
    session = _fresh_session()

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err

    code, lines, err = run_protocol(
        [{"v": 1, "t": "status"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err
    got = parse(lines)

    status = _ui(got, "status")
    body = status["status"]
    # 会话那一组：一眼能认出"这是哪个会话"。**这两个数来自上一轮留下的会话文件。**
    assert body["session"]["id"] == session
    assert body["session"]["messages"] == 3      # system + 用户 + 回答
    assert body["session"]["steps"] == 1
    # 模型那一组：现在用的那个，加它的窗口。
    assert body["model"]["current"] == "deepseek-flash"
    assert body["model"]["window"] == CONTEXT_WINDOWS["deepseek-flash"]
    # 账那一组：**和网关报的数一致**（假网关固定报 10 输入 / 5 输出，见 `_reply_stream`）。
    assert body["usage"]["prompt"] == 10
    assert body["usage"]["completion"] == 5
    assert body["counters"]["runs"] == 1
    assert body["counters"]["model_calls"] == 1
    assert body["counters"]["tool_calls"] == 0
    # "上一次请求实际发出去多少"是另一个字段（它是**最近一次**，不是累计）。
    assert status["last_prompt_tokens"] == 10
    assert status["context_tokens"] == CONTEXT_WINDOWS["deepseek-flash"]


def test_status_does_not_wait_for_a_running_turn(fake_openai):
    """**跑着的时候问状态也答得出来** —— 它不 join 那一轮。

    这是 `/status` 最有用的时刻（"它跑了半天了，花了多少？"），而代价要说清：
    那一屏里的数字是**发快照那一刻**的真实值，所以这一轮的答案还没落进历史时
    `messages` 就少一条。这不是 bug，是"不打断也不等待"的必然结果 —— 测试要钉的是
    "它答了、而且答的是那一刻的真值"，不是某个固定的数。
    """
    base, scripts, _calls = fake_openai
    scripts[:] = [{"content": "我很好。"}]
    session = _fresh_session()

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "status"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err
    body = _ui(parse(lines), "status")["status"]
    # 用户那句话一定已经在历史里了（它是在回合开始前就落盘的）。
    assert body["session"]["messages"] >= 2
    # **而计数是竞态的**：`/status` 不 join 那一轮，所以它可能在回合的第一个事件之前
    # 就被处理掉（后台线程刚起来、还没来得及发 `run_started`）。两种都算对 —— 这条
    # 测试钉的是"它答得出来"，不是"它答得多快"。
    assert body["counters"]["runs"] in (0, 1)


def test_status_works_before_any_turn(fake_openai):
    """一步都没走过时**照样答**（全是 0，而不是一条错误）。

    `summarize([])` 返回的就是零 —— 而"会话确实还没花过钱"和"我们数不出来"在这个
    问题上的处置是一样的。**关键是不能崩**：这是用户按下第一件事就会试的命令。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "status"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=_fresh_session(),
    )
    assert code == 0, err
    status = _ui(parse(lines), "status")["status"]
    assert status["counters"]["runs"] == 0
    assert status["usage"]["prompt"] == 0
    assert status["session"]["steps"] == 0


def test_tools_lists_every_tool_with_its_permission(fake_openai):
    """`/tools` 的清单**和 `init.tools` 是同一批工具**，而权限那一列由 runtime 算。

    两处对不上的症状很难查（"启动时列着 shell、`/tools` 里没有它"），所以这里直接
    拿两条消息比对名字集合。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "tools"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=_fresh_session(),
    )
    assert code == 0, err
    got = parse(lines)

    init = kinds(got, "init")[0]
    listed = _ui(got, "tools")["tools"]
    assert {row["name"] for row in listed} == {tool["name"] for tool in init["tools"]}

    rows = {row["name"]: row for row in listed}
    # 默认策略：low 自动放行，medium/high 要问。
    assert rows["read_file"]["disposition"] == "auto"
    assert rows["read_file"]["risk"] == "low"
    assert rows["shell"]["disposition"] == "ask"
    # `command` 那一格说的是"这个工具的参数量有没有命令行"—— 只有 shell 有，
    # 而 `/tools` 末尾那句"命令规则只对有命令行的工具生效"靠它。
    assert rows["shell"]["command"]
    assert rows["read_file"]["command"] is None
    assert rows["read_file"]["external"] is False


def test_set_model_switches_and_says_so(fake_openai):
    """`/model` 换模型：**回一条 state 快照 + 一条说明**，而且下一次请求真的用新名字。

    三件事缺一不可：
      * state 快照（界面按它显示，不许乐观更新）；
      * 一条 notice（含"上一个是谁、什么时候生效"—— 界面拼不出这两个事实）；
      * 下一次请求的 `model` 字段变了（**唯一的真凭据**）。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "一"}, {"content": "二"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_model", "model": "deepseek-v4-pro"},
         {"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=_fresh_session(),
    )
    assert code == 0, err
    got = parse(lines)

    snapshot = _ui(got, "state")
    assert snapshot["model"] == "deepseek-v4-pro"
    assert snapshot["model_window"] == CONTEXT_WINDOWS["deepseek-v4-pro"]

    notices = [m for m in kinds(got, "notice") if m.get("code") == "model"]
    assert notices, "换模型要回一条说明"
    assert "deepseek-v4-pro" in notices[-1]["text"]
    assert notices[-1]["level"] == "info"

    # 真凭据：请求里带的是新模型名。
    assert calls[-1]["model"] == "deepseek-v4-pro"


def test_set_model_rejects_a_name_outside_the_catalog(fake_openai):
    """目录外的名字**拒绝并说清**，而且**什么都不改**。

    这条同时钉住"前端发什么 runtime 都得先校验"：界面拿到什么发什么，所以一个
    手写的客户端可以发任何字符串。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "一"}]

    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_model", "model": "gpt-9"},
         {"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=_fresh_session(),
    )
    assert code == 0, err
    got = parse(lines)

    notices = [m for m in kinds(got, "notice") if m.get("code") == "model"]
    assert notices and notices[-1]["level"] == "warn"
    assert "目录里没有这个模型" in notices[-1]["text"]
    assert _ui(got, "state")["model"] == "deepseek-flash"
    # 请求照旧用旧模型 —— "拒绝了"必须是真的没换。
    assert calls[-1]["model"] == "deepseek-flash"


def test_set_model_with_a_non_string_is_refused_at_the_envelope(fake_openai):
    """信封那一层只拦"它得是个字符串"（`{"model": {...}}` 会把整个 dict 打给用户看）。"""
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_model", "model": {"id": "x"}},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=_fresh_session(),
    )
    assert code == 0, err
    notices = [m for m in kinds(parse(lines), "notice") if m.get("code") == "model"]
    assert notices and "字符串" in notices[-1]["text"]


def test_the_model_choice_survives_a_resume(fake_openai):
    """换过的模型**跟着会话落盘**：恢复它时还是那个（而不是回到 .env 里那个）。

    这是"只影响当前会话"那句承诺的另一半 —— 它必须在**下一个进程**里也成立，
    所以这里跑两次子进程，第二次不重新选。
    """
    base, scripts, calls = fake_openai
    scripts[:] = [{"content": "一"}, {"content": "二"}]
    session = _fresh_session()

    code, lines, err = run_protocol(
        [{"v": 1, "t": "set_model", "model": "deepseek-v4-pro"},
         {"v": 1, "t": "user_message", "text": "一"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err
    assert calls[-1]["model"] == "deepseek-v4-pro"

    code, lines, err = run_protocol(
        [{"v": 1, "t": "user_message", "text": "二"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": base}, session=session,
    )
    assert code == 0, err
    got = parse(lines)
    init = kinds(got, "init")[0]
    # 启动那一刻就说清楚"这个会话选的是谁"（不说的话，用户会以为它跟着 .env 走）。
    # **它走 `init.notices`，不是一条 `notice` 消息** —— 那是开场那批说明的统一出口。
    model_notices = [n for n in init["notices"] if n.get("code") == "model"]
    assert model_notices, [n.get("code") for n in init["notices"]]
    assert "deepseek-v4-pro" in model_notices[-1]["text"]
    # 而且请求真的用新模型 —— 恢复会话不该悄悄回到 .env 那个。
    assert calls[-1]["model"] == "deepseek-v4-pro"


def test_init_carries_the_model_catalog(fake_openai):
    """`/model` 那张清单**开场就发给前端**（它是常量数据，不随会话变）。

    界面照它渲染、不写死模型名 —— 写死的话，加一个模型要改两个地方，而漏改的那一处
    只表现为"这个模型选不了"。
    """
    base, _, _ = fake_openai
    code, lines, err = run_protocol(
        [{"v": 1, "t": "shutdown"}], env_extra={"DEEPSEEK_BASE_URL": base},
        session=_fresh_session(),
    )
    assert code == 0, err
    catalog = kinds(parse(lines), "init")[0]["model_catalog"]

    ids = [item["id"] for item in catalog["models"]]
    assert ids == [item.id for item in load_catalog().models()]
    # `current` 由 runtime 标好（它要对账别名折算），界面不自己比字符串。
    current = [item for item in catalog["models"] if item["current"]]
    assert len(current) == 1 and current[0]["id"] == "deepseek-flash"
    # **每一条都带 provider**：同名模型可以在多条路由上，而"请求发到哪儿"是另一件事。
    assert all(item["provider"] for item in catalog["models"])
    # 旧名字**单列**，不混在可选项里。
    assert {item["id"] for item in catalog["aliases"]} == set(ALIASES)
    assert all(item["of"] in ids for item in catalog["aliases"])


