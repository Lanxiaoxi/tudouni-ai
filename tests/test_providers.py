"""`/thinking` `/effort` 与**多 provider**：协议那一侧的验收。

分两组，因为它们的证据不同：

  1. **思考那两个旋钮** —— 真凭据是**发出去的请求体**（`reasoning_effort` 在不在、
     `extra_body.thinking` 是 enabled 还是 disabled）。这一组用假网关跑真子进程；
  2. **多 provider** —— 真凭据是"请求落到了**哪一台**假网关上"。所以这一组起**两个**
     网关，各自记自己收到的请求，然后让 `/model` 在它们之间切。
"""

import json
import os
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from agent_runtime.protocol import messages

# 仓库根 —— 它下面有 `agent_runtime/`。子进程一律用 `-m agent_runtime.main` 起，
# 而不是 `main.py` 的绝对路径：那是生产里真正的起法（见 `protocol/client.py` 的
# `RUNTIME_MODULE`），所以这里照着用就顺带把它钉住了。
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ARGV = [sys.executable, "-m", "agent_runtime.main"]


class _Gateway(BaseHTTPRequestHandler):
    """一个最小假网关：记下每一次请求体，回一句固定的回答。

    **它按实例记账**（`self.server.calls`，而不是类属性）：多 provider 那一组要同时起
    两个，而"请求落到哪一台"正是被测的东西 —— 记账混在一起就什么都验不出来。
    """

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.calls.append(body)  # type: ignore[attr-defined]

        payload = {
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "好"}}],
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


class _FakeGateway:
    """一对 `(base_url, calls)`，用完收掉。"""

    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _Gateway)
        self.server.calls = []          # type: ignore[attr-defined]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def calls(self) -> list[dict]:
        return self.server.calls           # type: ignore[attr-defined]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    server = _FakeGateway()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def two_gateways():
    first, second = _FakeGateway(), _FakeGateway()
    try:
        yield first, second
    finally:
        first.close()
        second.close()


def _fresh_session() -> str:
    return f"proto-{uuid.uuid4().hex[:10]}"


def run(inbound, *, env_extra=None, session=None, timeout=60.0, cwd=None):
    """喂几行给 `--runtime-stdio`，返回 (退出码, 已解析的消息, stderr)。

    `env_extra["AGENT_CONFIG_FILE"]` 指定这次用哪份配置 —— **测试一律显式给**
    （不给就会读到开发机上那份 `~/.tudouni/config.json`，而"哪条路由被选中"正是这一组
    要验的东西）。

    `env_extra` 里给 `None` 表示**把这个变量删掉**（不是设成 "None"）。要验"一把
    DeepSeek 密钥都没有也能跑"就得这么做 —— 上面那行默认值是给别的用例垫的底。

    `cwd` 默认是仓库根（`-m agent_runtime.main` 靠它找到包）。只有要跟"假 home"错开时
    才需要传 —— 验"首次运行往 home 里写模板"时，home **不能落在工作区里面**：工作区是
    cwd，而它是 home 的上层时 `check_workspace()` 会（正确地）拒绝启动。那种时候 cwd 换成
    一个和 home 平级的目录，包改由 `PYTHONPATH` 找到。
    """
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-test"
    env["PYTHONIOENCODING"] = "utf-8"
    for name, value in (env_extra or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in inbound)
    argv = [*RUNTIME_ARGV, "--runtime-stdio"]
    if session is not None:
        argv += ["--session", session]
    result = subprocess.run(
        argv, input=payload, capture_output=True, encoding="utf-8",
        errors="replace", env=env, cwd=str(cwd or REPO_ROOT), timeout=timeout,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return result.returncode, [json.loads(line) for line in lines], result.stderr


def kinds(messages_in, t):
    return [item for item in messages_in if item.get("t") == t]


def _ui(got, kind):
    found = [item for item in kinds(got, "ui") if item.get("kind") == kind]
    assert found, f"没有 kind={kind}"
    return found[-1]


def _state_with(got, key):
    """最后一条 `ui(state)` 里某个键的值 —— 快照有好几条，取最后那条是最新状态。"""
    return _ui(got, "state")[key]


# --- 思考开关与强度：真凭据是请求体 ---------------------------------------------

def test_thinking_is_on_by_default_in_the_request(gateway):
    """**默认开着，而且这条请求自己说清了它要什么。**

    端点的默认行为本来就是开，但我们显式发 `thinking: {type: "enabled"}`：默认值会随
    端点变，而这个配置是用户选的 —— 多一个字段换"这条请求在任何时候都是同样的意思"。
    `reasoning_effort` 走顶层（SDK 的原生参数），带的是目录里声明的出厂强度。
    """
    code, got, err = run(
        [{"v": 1, "t": "user_message", "text": "你好"}, {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": gateway.base_url}, session=_fresh_session(),
    )
    assert code == 0, err
    request = gateway.calls[-1]
    assert request["reasoning_effort"] == "high"
    assert request["thinking"] == {"type": "enabled"}
    # 开场就告诉界面这两个旋钮是什么（界面不许自己猜一个默认值）。
    init = kinds(got, "init")[0]
    assert init["thinking"] is True and init["effort"] == "high"
    assert init["effort_levels"] == ["low", "high", "max"]


def test_turning_thinking_off_changes_the_next_request(gateway):
    """`/thinking off` 之后**下一个请求里没有思考**，而且之后每一轮都没有。

    这是这个功能唯一的真凭据：设置改了而请求体没变，界面上说"关"就是假话。
    同时钉住"关着时**不发** `reasoning_effort`"—— 端点会忽略它，但一个没人读的字段
    只会让抓包的人以为它生效了。
    """
    code, got, err = run(
        [{"v": 1, "t": "set_thinking", "on": False},
         {"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": gateway.base_url}, session=_fresh_session(),
    )
    assert code == 0, err
    request = gateway.calls[-1]
    assert request["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in request
    # 回执：一条 state 快照（界面按它显示）+ 一条说明。
    assert _state_with(got, "thinking") is False
    assert _state_with(got, "effort") == "high", "关掉思考不清强度"
    notices = [item for item in kinds(got, "notice") if item.get("code") == "thinking"]
    assert notices and "关" in notices[-1]["text"]


def test_effort_reaches_the_request_and_survives_a_disabled_thinking(gateway):
    """`/effort max` 落到请求里；**关着思考时它只被记下来**（不发、也不丢）。

    ## 两次跑，因为"关着时设强度"和"开着时用它"是两个时刻

    同一个进程里没法验这一条：入站消息是**一条一条读**的，而 `set_effort` 紧跟在
    `set_thinking off` 后面时，第一轮请求甚至还没发出去 —— 那时强度改了、可还没轮到
    它被用上（实测：这种写法下第一轮请求发的已经是新强度）。所以这里分成两个进程，
    用同一个会话：第一个进程关掉思考并设强度，第二个进程只把思考打开、再问一句。

    用户设了强度之后关掉思考，再打开时那个强度必须还在 —— 这是"两个旋钮互不影响"
    那句话的全部内容。
    """
    session = _fresh_session()
    env_extra = {"DEEPSEEK_BASE_URL": gateway.base_url}

    code, _got, err = run(
        [{"v": 1, "t": "set_thinking", "on": False},
         {"v": 1, "t": "set_effort", "effort": "max"},
         {"v": 1, "t": "user_message", "text": "一"},
         {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    # 关着：只发 thinking=disabled，强度不发（端点会忽略它，而一个没人读的字段只会
    # 让抓包的人以为它生效了）。
    assert gateway.calls[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in gateway.calls[0]

    code, got, err = run(
        [{"v": 1, "t": "set_thinking", "on": True},
         {"v": 1, "t": "user_message", "text": "二"},
         {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    # 打开了，而强度**还是 max** —— 关着的时候没有把它清掉。
    assert gateway.calls[1]["thinking"] == {"type": "enabled"}
    assert gateway.calls[1]["reasoning_effort"] == "max"
    assert _state_with(got, "effort") == "max"


def test_a_bad_effort_changes_nothing_and_says_so(gateway):
    """认不出的强度**什么都不改**，而且回一条说清能写什么的说明。

    什么都不改这一半同样重要：改了一半再报错（比如把强度写进会话、但请求没变）会让
    `/status` 和实际发出去的请求对不上。
    """
    code, got, err = run(
        [{"v": 1, "t": "set_effort", "effort": "hgih"},
         {"v": 1, "t": "user_message", "text": "你好"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": gateway.base_url}, session=_fresh_session(),
    )
    assert code == 0, err
    assert gateway.calls[-1]["reasoning_effort"] == "high", "没改成 → 还是出厂值"
    notices = [item for item in kinds(got, "notice") if item.get("code") == "effort"]
    assert notices and notices[-1]["level"] == "warn"
    assert "low" in notices[-1]["text"] and "max" in notices[-1]["text"]


def test_a_non_string_effort_is_refused_at_the_envelope(gateway):
    """信封那一层只拦"它得是个字符串"（一个 dict 会把整段报错打给用户看）。"""
    code, got, err = run(
        [{"v": 1, "t": "set_effort", "effort": {"level": "max"}},
         {"v": 1, "t": "shutdown"}],
        env_extra={"DEEPSEEK_BASE_URL": gateway.base_url}, session=_fresh_session(),
    )
    assert code == 0, err
    notices = [item for item in kinds(got, "notice") if item.get("code") == "effort"]
    assert notices and "字符串" in notices[-1]["text"]


# --- 多 provider：真凭据是"请求落到哪一台" ---------------------------------------

def _write_models(workdir: Path, providers: dict) -> Path:
    path = workdir / "config.json"
    path.write_text(json.dumps({"providers": providers}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def test_a_request_lands_on_the_second_gateway_after_switching(two_gateways, workdir):
    """**多 provider 的端到端验收**：配置两条路由，`/model` 换过去之后请求换了台。

    这一条把四样东西一次串起来：配置文件 → 目录 → `/model` → **真的 HTTP 请求**。
    任何一环断了，"两台各收到过一次"就不会成立，而"界面说换了、请求没动"正是这个功能
    最坏的失败形态。

    **顺序是这个测试的讲究**：入站消息是一条一条读的，而 `user_message` 会**起一个
    后台线程**去跑那一轮（`_dispatch` 不等它）。所以"说一句话 + 紧接着换模型"并不保证
    那一轮先落地 —— 实测过：那种写法下第一轮请求发的已经是新模型。
    想看清"换之前发给谁、换之后发给谁"，就得**先把第一轮跑完再换**（这里用两个进程，
    这正是"会话级设置跨进程有效"那条性质）。
    """
    first, second = two_gateways
    models = _write_models(workdir, {
        "one": {"base_url": first.base_url, "api_key": "sk-one",
                "models": [{"id": "m-one", "context_window": 1111}]},
        "two": {"base_url": second.base_url, "api_key": "sk-two",
                "models": [{"id": "m-two", "context_window": 2222}]},
    })
    session = _fresh_session()
    env_extra = {"AGENT_CONFIG_FILE": str(models)}

    code, got, err = run(
        [{"v": 1, "t": "user_message", "text": "一"}, {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    assert [call["model"] for call in first.calls] == ["m-one"]
    assert second.calls == []
    # 开场那条 init 带着两条路由的清单，而且每条都标着它属于哪条路由。
    catalog_rows = kinds(got, "init")[0]["model_catalog"]["models"]
    assert [row["provider"] for row in catalog_rows] == ["one", "two"]

    code, got, err = run(
        [{"v": 1, "t": "set_model", "model": "two/m-two"},
         {"v": 1, "t": "user_message", "text": "二"},
         {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    assert [call["model"] for call in second.calls] == ["m-two"], \
        "换过路由之后请求必须落到另一台上"
    # 换完之后第一台**不再收到任何请求**（第二轮整个走的是第二台）。
    assert len(first.calls) == 1

    # 换完之后那三条证据：state 快照（界面按它显示）+ 说明。
    assert _state_with(got, "model") == "m-two"
    assert _state_with(got, "model_provider") == "two"
    assert _state_with(got, "model_window") == 2222
    assert _state_with(got, "effort_levels") == ["low", "high", "max"]
    notices = [item for item in kinds(got, "notice") if item.get("code") == "model"]
    assert notices and "two/m-two" in notices[-1]["text"]


def test_the_chosen_route_survives_a_resume(two_gateways, workdir):
    """换过的**路由**跟着会话落盘：恢复之后请求还发给那一台。

    会话级选择要跟着会话走 —— 那条承诺在跨路由之后更要紧：恢复时悄悄回到官方端点，
    而账单上又是另一回事。
    """
    first, second = two_gateways
    models = _write_models(workdir, {
        "one": {"base_url": first.base_url, "api_key": "sk-one",
                "models": [{"id": "m-one", "context_window": 1111}]},
        "two": {"base_url": second.base_url, "api_key": "sk-two",
                "models": [{"id": "m-two", "context_window": 2222}]},
    })
    session = _fresh_session()
    env_extra = {"AGENT_CONFIG_FILE": str(models)}

    code, _got, err = run(
        [{"v": 1, "t": "set_model", "model": "two/m-two"},
         {"v": 1, "t": "user_message", "text": "一"},
         {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    assert len(second.calls) == 1

    code, got, err = run(
        [{"v": 1, "t": "user_message", "text": "二"}, {"v": 1, "t": "shutdown"}],
        env_extra=env_extra, session=session,
    )
    assert code == 0, err
    assert len(second.calls) == 2, "恢复会话要接着用那条路由"
    assert len(first.calls) == 0
    # 恢复时那两行也要说清楚：用的是哪条路由，以及这个会话选过它。
    assert kinds(got, "init")[0]["model"] == "m-two"
    assert kinds(got, "init")[0]["provider"] == "two"
    notices = [item for item in kinds(got, "init")[0]["notices"]
               if item.get("code") == "model"]
    assert notices and "two/m-two" in notices[-1]["text"]


def test_a_route_without_a_key_is_listed_but_not_selectable(two_gateways, workdir):
    """没密钥的路由**在清单里、但选不了**，而那条说明要说清怎么补。

    "这台机器上知道它存在"和"现在能用它"是两件事 —— 前者要列出来（好让人知道
    自己配了它），后者要能被拒绝。
    """
    first, _second = two_gateways
    models = _write_models(workdir, {
        "one": {"base_url": first.base_url, "api_key": "sk-one",
                "models": [{"id": "m-one", "context_window": 1111}]},
        "nokey": {"base_url": "https://nowhere.example/v1",
                  "models": [{"id": "m-two", "context_window": 2222}]},
    })
    code, got, err = run(
        [{"v": 1, "t": "set_model", "model": "nokey/m-two"},
         {"v": 1, "t": "user_message", "text": "一"},
         {"v": 1, "t": "shutdown"}],
        env_extra={"AGENT_CONFIG_FILE": str(models)}, session=_fresh_session(),
    )
    assert code == 0, err
    notices = [item for item in kinds(got, "notice") if item.get("code") == "model"]
    assert notices and "没有密钥" in notices[-1]["text"]
    # 没换成功：请求照旧发给能用的那条。
    assert [call["model"] for call in first.calls] == ["m-one"]
    # 清单里两条都在（选不了不等于看不到）。
    rows = kinds(got, "init")[0]["model_catalog"]["models"]
    assert [row["provider"] for row in rows] == ["one", "nokey"]
    # 启动那几行里也报了这条路由没密钥 —— 它不该只在按下 /model 时才现形。
    assert any("nokey" in item.get("text", "")
               for item in kinds(got, "init")[0]["notices"])


# --- 模型层是抽象的：DeepSeek 只是"最快那条捷径" --------------------------------
#
# 这个工具**不是**一家网关的客户端。端点、模型名、密钥全都是配置，所以"能不能跑"的判据
# 只能是"有没有一条能用的路由"，不能是"有没有 DEEPSEEK_API_KEY"。


def test_a_custom_provider_needs_no_deepseek_key_at_all(gateway, workdir):
    """**只配了自家网关、一把 DeepSeek 密钥都没有，照样能起来。**

    这一条盯的是一个真实存在过的绑定：装配期曾经无条件走 `ModelConfig.from_env()`，
    而它缺 `DEEPSEEK_API_KEY` 就抛错 —— 于是接了自家网关的人被一句 DeepSeek 的密钥挡在
    门外，哪怕他的路由是好的、密钥也是好的。而真正发出去的那把密钥来自 `providers`
    （`chosen.provider_key`），和那个环境变量毫无关系。

    所以这里把 `DEEPSEEK_API_KEY` **删掉**（不是设成空串 —— 那也仍然是一条"能读到但为
    空"的记录），只留 `providers` 里的一条路由，然后要求这一轮真的跑完。
    """
    models = _write_models(workdir, {
        "my-gw": {"base_url": gateway.base_url, "api_key": "sk-mine",
                  "models": [{"id": "my-model", "context_window": 4096}]},
    })
    code, got, err = run(
        [{"v": 1, "t": "user_message", "text": "你好"}, {"v": 1, "t": "shutdown"}],
        env_extra={"AGENT_CONFIG_FILE": str(models), "DEEPSEEK_API_KEY": None},
        session=_fresh_session(),
    )
    assert code == 0, f"只配自家网关就该能跑，却被拦下了：\n{err}"
    assert [call["model"] for call in gateway.calls] == ["my-model"]
    assert "没找到 DEEPSEEK_API_KEY" not in err
    # 而且这条路由真的被当成默认那条（配置里第一条有密钥的）。
    assert kinds(got, "init")[0]["provider"] == "my-gw"


def test_nothing_configured_asks_for_a_route_not_for_a_deepseek_key(workdir):
    """一条路由都配不出来时，那句话**先让人写 `providers`**，而不是"去弄把 DeepSeek 密钥"。

    新用户看到的第一句话就是它（首次运行时模板也是在这一刻被写出来的）。所以它必须让人
    看出"接哪家都行"，而第 2 条那条捷径只是捷径。原来的第一句话是"没找到
    DEEPSEEK_API_KEY，两种给法"（一个已经退休的 `_no_key_message`）—— 换成现在这句的
    全部理由就是把这个工具说成"某家网关的客户端"是错的。
    """
    empty = workdir / "empty-config.json"
    empty.write_text("{}", encoding="utf-8")

    code, _got, err = run(
        [{"v": 1, "t": "shutdown"}],
        env_extra={"AGENT_CONFIG_FILE": str(empty), "DEEPSEEK_API_KEY": None},
        session=_fresh_session(),
    )
    assert code == 2, f"配不出模型就该以 2 收场（用户得先做点事）：\n{err}"
    assert "模型层是抽象的" in err, err
    assert "providers" in err and "任选其一" in err
    # 第 1 条必须是 providers（真正的模型配置面），捷径排第 2。
    assert err.index("providers") < err.index("DEEPSEEK_API_KEY"), err


# --- 首次运行那一步：写模板的接线 ----------------------------------------------
#
# 这两条**原来住在 `tests/test_config.py`**，跟着 `ModelConfig.from_env()` 那个 raise 一起。
# raise 退休之后，"写模板"这件事归了 `open_runtime`（只有它才知道是不是真的一条路由都没
# 有），所以接线也搬到这里来验 —— 而这里能起真子进程，验的是用户真正看到的东西。


def test_a_missing_route_scaffolds_the_config_and_points_at_it(workdir):
    """**首次运行的完整那一步：一条路由都没有 ⇒ 建一份模板，然后指着它报错。**

    这条盯的是"接线"，而 `test_userconfig.py` 盯的是 `scaffold()` 本身。少了这条接线的话，
    `scaffold()` 全绿而用户仍然看到"你得自己建一份" —— 而那正是它要消灭的那句话。

    用假 home（`USERPROFILE`）而不是真 home：这条测试会**真的写一个配置文件**，而它必须
    写在那个被指过去的目录里。

    **工作区和 home 必须是平级的两个目录**：工作区是 cwd，而 cwd 是 home 的上层时
    `check_workspace()` 会拒绝启动（它以为你要把整个 home 交出去 —— 那次拒绝是对的）。
    所以这里 cwd 用 `ws/`，包由 `PYTHONPATH` 找到。
    """
    home = workdir / "home"
    ws = workdir / "ws"
    home.mkdir()
    ws.mkdir()

    code, _got, err = run(
        [{"v": 1, "t": "shutdown"}],
        env_extra={"AGENT_CONFIG_FILE": None, "DEEPSEEK_API_KEY": None,
                   "USERPROFILE": str(home), "HOME": str(home),
                   "PYTHONPATH": str(REPO_ROOT)},
        session=_fresh_session(),
        cwd=ws,
    )

    assert code == 2, err
    created = home / ".tudouni" / "config.json"
    assert created.is_file(), "报错那一刻没有把模板建出来（scaffold 没接上）"
    assert str(created) in err
    assert "已经在这儿给你建好了" in err


def test_a_usable_route_scaffolds_nothing(workdir):
    """一条能用的路由在手 ⇒ **一个文件都不建**。

    容器和 CI 的 home 常常是临时的、甚至只读的，而我们凭什么在那儿留东西。这也是
    `scaffold()` 被放在"报错那一刻"而不是"每次启动"的全部理由。

    这里让 `run()` 垫上的 `DEEPSEEK_API_KEY` 生效（内置那条兜底路由因此可用），于是
    装配成功、压根走不到报错那一刻。
    """
    home = workdir / "home"
    ws = workdir / "ws"
    home.mkdir()
    ws.mkdir()

    code, _got, err = run(
        [{"v": 1, "t": "shutdown"}],
        env_extra={"AGENT_CONFIG_FILE": None,
                   "USERPROFILE": str(home), "HOME": str(home),
                   "PYTHONPATH": str(REPO_ROOT)},
        session=_fresh_session(),
        cwd=ws,
    )

    assert code == 0, err
    assert not (home / ".tudouni").exists(), "有可用路由时不该往别人 home 里写东西"
