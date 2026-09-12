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

    ui = kinds(got, "ui")
    assert len(ui) == 1
    assert ui[0]["kind"] == "run_finished"
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
            elif message.get("t") == "ui":
                break          # 拿到答案就收工
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
    ui = [m for m in seen if m.get("t") == "ui"]
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


