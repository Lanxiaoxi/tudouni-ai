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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_runtime.protocol import messages

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"
REPO_ROOT = MAIN_PY.parent


# --- 一个假的 OpenAI 兼容端点 -------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """只回答 `POST /v1/chat/completions`，按脚本逐次给回复。"""

    scripts: list[dict] = []
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(body)

        index = min(len(type(self).calls) - 1, len(type(self).scripts) - 1)
        step = type(self).scripts[index]
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

    def log_message(self, *args):  # 别把每个请求打到 stderr 上
        return


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
                 session: str | None = "proto-test", timeout: float = 60.0):
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

    assert init["protocol"] == messages.VERSION
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

def _open_protocol(fake_openai, *, session: str | None = None):
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

    class _Logs:
        directory = "."

    class _Runtime:
        session = _Session()
        agent = _Agent()
        logs = _Logs()
        context_tokens = None
        max_steps = 1

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
    assert "工具 11 个" in result.stdout
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


