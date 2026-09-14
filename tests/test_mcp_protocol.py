"""`/mcp` 走协议那条路：**真起一个 runtime 子进程，真起一个 MCP 子进程**。

它和另外三个文件的分工：

  * `tests/test_mcp.py` / `tests/test_mcp_http.py` —— 一个 server 自己（协议、两种传输）；
  * `tests/test_mcp_host.py` —— 宿主（加载/卸载/状态），用假通道；
  * **这个文件** —— 端到端：界面发一条 `t:"mcp"`，runtime 真的把 server 挂上、把它
    的工具注册进那一轮的注册表，并把结果发回来。

为什么非要真起：`/mcp` 那件事的效果是"**模型下一次能看到哪些工具**"，而那条链是
读配置 → 起子进程 → 握手 → 列工具 → 注册表 → `init`/`ui(tools)`。中间任何一环
写错，用假通道测都会是绿的（假通道把"起进程"那一整段替换掉了）。

`mcp.json` 的位置也一起验了：它只从**用户级**目录读（`Path.home()/".tudouni"`），
所以这里把 `HOME`/`USERPROFILE` 指到一个临时目录 —— 那既是不污染跑测试那台机器的
办法，也顺带钉住"它读的是用户级那一个"。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

# 仓库根 —— 它下面有 `agent_runtime/`。子进程一律用 `-m agent_runtime.main` 起，
# 而不是 `main.py` 的绝对路径：那是生产里真正的起法（见 `protocol/client.py` 的
# `RUNTIME_MODULE`），所以这里照着用就顺带把它钉住了。
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ARGV = [sys.executable, "-m", "agent_runtime.main"]
FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"
KERNEL = "mcp__fake__echo"


def write_mcp_config(workdir: Path, *names: str) -> Path:
    """在 `workdir`（当 HOME 用）里写一份用户级 `mcp.json`。

    每个 server 都指向 `tests/fake_mcp_server.py`（真子进程，5 个工具）。
    """
    home = workdir / "home"
    (home / ".tudouni").mkdir(parents=True, exist_ok=True)
    payload = {"servers": {
        name: {"command": sys.executable, "args": [str(FAKE_SERVER)],
               "timeout_seconds": 30}
        for name in names
    }}
    (home / ".tudouni" / "mcp.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    return home


class Session:
    """一个活着的 `--runtime-stdio` 子进程 + 一队列已经收到的消息。"""

    def __init__(self, home: Path):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # **模型那一层不再往这里塞环境变量**：`AGENT_CONFIG_FILE` 已经由 `fake_openai`
        # fixture 指向一份写着假网关的配置（配置只有一个来源，`DEEPSEEK_BASE_URL` 那条
        # 老路退休了）。这个文件要的是"子进程能起来、能挂 MCP server"，不碰模型。
        # **用户级目录在这里**（`~/.tudouni/mcp.json`）。两个变量都设：`Path.home()`
        # 在 Windows 上看 USERPROFILE、在 POSIX 上看 HOME。
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        # `-m` 能不能找到包：装过的环境里本来就行，`PYTHONPATH` 是给没同步过的环境兜底。
        env["PYTHONPATH"] = str(REPO_ROOT)

        # **cwd 是一个独立的工作区，不是仓库根。** 两个理由，第二个是踩出来的：
        #
        #   1. 工作区就是 cwd（`paths.workspace_dir()`），所以这条测试跑起来会在 cwd 下
        #      建 `.tudouni/`（会话、审计）—— 那不该落在仓库里；
        #   2. 更要紧：假 HOME 在 `workdir/home` 里，**而 workdir 在仓库下面**。用仓库根
        #      当 cwd 的话，工作区就成了那个 home 的上层，于是 `check_workspace()` 直接
        #      拒绝启动（"它下面是所有人的 home"）—— 那条检查是对的（工作区真的会包住那个
        #      home），错的是把仓库根当 cwd。
        workspace = home.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        self.process = subprocess.Popen(
            [*RUNTIME_ARGV, "--runtime-stdio", "--session", "mcp-proto"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", env=env, cwd=str(workspace),
        )
        self.incoming: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, name="mcp-proto-reader",
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="mcp-proto-stderr",
                         daemon=True).start()
        self.stderr = ""

    def _drain_stderr(self) -> None:
        """把子进程的 stderr 收着。

        **不是可选的**：子进程的 stderr 是一条管道，没人读的话写满之后它会卡住 ——
        而那时症状是"等协议消息超时"，看起来像我们的逻辑有问题。而且断言失败时
        那段文本正是唯一能说明"它为什么没起来"的东西。
        """
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr += line

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.incoming.put(line)
        self.incoming.put("")

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next_message(self, timeout: float = 60.0) -> dict[str, Any]:
        try:
            line = self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"等协议消息超时了（{timeout}s）—— 子进程卡住了。"
                f"它的 stderr：\n{self.stderr[-2000:]}"
            ) from None
        assert line.strip(), f"协议流结束了 —— 子进程不该在这里退出。stderr：\n{self.stderr[-2000:]}"
        return json.loads(line)

    def wait_for(self, kind: str, *, ui_kind: str | None = None,
                 timeout: float = 60.0) -> dict[str, Any]:
        """等到某一条消息。**超时是失败，不是空值**（两种失败不该长得一样）。"""
        deadline = timeout
        while True:
            message = self.next_message(deadline)
            if message.get("t") != kind:
                continue
            if ui_kind is not None and message.get("kind") != ui_kind:
                continue
            return message

    def close(self) -> int:
        try:
            self.send({"v": 1, "t": "shutdown"})
            if self.process.stdin is not None:
                self.process.stdin.close()
            return self.process.wait(timeout=30)
        except Exception:  # noqa: BLE001 - 收摊失败不该盖住断言
            self.process.kill()
            return -1


@pytest.fixture
def session_factory(fake_openai, workdir):
    """造一个会话。

    `fake_openai`（住在 `tests/conftest.py`）在这里**要的是它的副作用**：它会写一份
    指向本地假网关的配置并把 `AGENT_CONFIG_FILE` 指过去 —— 子进程没有别的地方能收到
    这个参数。本文件不碰模型，但子进程必须有一份能读的配置才起得来。
    """
    made: list[Session] = []

    def start(*server_names: str) -> Session:
        home = write_mcp_config(workdir, *server_names)
        session = Session(home)
        made.append(session)
        return session

    try:
        yield start
    finally:
        for session in made:
            session.close()


def test_mcp_list_says_what_is_configured_but_not_running(session_factory):
    """开场不动手：配了不等于挂上（`mcp.json` 只说明"有这几个可以挂"）。

    这条钉的是 `/mcp` 那条命令最容易错的地方 —— 它**只由人按键触发**。启动时自动
    把配好的全挂上会让"配置自己变宽"成为可能，而那正是 `mcp.json` 只读用户级要防的
    事（见 tools/mcp.py 的模块 docstring）。
    """
    session = session_factory("fake")
    try:
        assert session.wait_for("init")["protocol"]

        # **先发再等**：`wait_for` 会把中间的消息吃掉（它等的是"下一条符合条件的"），
        # 所以"等一下看看它会不会自己发"这种写法会把后面的消息一起丢掉。
        session.send({"v": 1, "t": "mcp", "action": "list"})
        listing = session.wait_for("ui", ui_kind="mcp")

        rows = {row["name"]: row for row in listing["mcp_servers"]}
        assert rows["fake"]["state"] == "unload"
        assert rows["fake"]["tools"] == 0
        assert "fake_mcp_server.py" in rows["fake"]["where"]
        assert listing["mcp_notes"], "list 也要说一句（它是一次动作的回包）"

        # 紧接着那一份 `ui(state)` 里也带着同一份清单（左栏那块读的是它）。
        state = session.wait_for("ui", ui_kind="state")
        assert state["mcp"][0]["state"] == "unload"
    finally:
        assert session.close() == 0


def test_mcp_load_really_starts_the_server_and_registers_its_tools(session_factory):
    """**验收**：`t:"mcp" load` 之后，那个 server 的工具真的进了这一轮的注册表。

    三条一起看才算数：回包里那一格变成 `loaded` + 工具数、`ui(state)` 里那一格跟着变
    （左栏）、以及 `/tools` 那份清单里出现了 `mcp__fake__*`（**模型下一轮就会看到
    它们**）。少任何一条，"挂上了"都可能是自说自话。
    """
    session = session_factory("fake")
    try:
        session.wait_for("init")

        session.send({"v": 1, "t": "mcp", "action": "load", "servers": ["fake"]})
        result = session.wait_for("ui", ui_kind="mcp")

        row = result["mcp_servers"][0]
        assert row["state"] == "loaded" and row["tools"] == 5
        assert any("挂上了" in text for text in result["mcp_notes"])

        state = session.wait_for("ui", ui_kind="state")
        assert state["mcp"][0]["state"] == "loaded"

        session.send({"v": 1, "t": "tools"})
        rows = {item["name"]: item for item in session.wait_for("ui", ui_kind="tools")["tools"]}
        assert KERNEL in rows
        assert rows[KERNEL]["external"] is True
        assert rows[KERNEL]["risk"] == "high"
        assert rows[KERNEL]["disposition"] == "ask", "外部工具默认每条都要问"
    finally:
        assert session.close() == 0


def test_mcp_unload_takes_the_tools_back_out(session_factory):
    """卸载之后 `/tools` 里不能再有它 —— 那才叫"模型看不到它了"。"""
    session = session_factory("fake")
    try:
        session.wait_for("init")

        session.send({"v": 1, "t": "mcp", "action": "load", "servers": ["fake"]})
        session.wait_for("ui", ui_kind="mcp")
        session.wait_for("ui", ui_kind="state")

        session.send({"v": 1, "t": "mcp", "action": "unload", "servers": ["fake"]})
        result = session.wait_for("ui", ui_kind="mcp")
        assert result["mcp_servers"][0]["state"] == "unload"
        assert any("卸下了" in text for text in result["mcp_notes"])

        session.send({"v": 1, "t": "tools"})
        names = {item["name"] for item in session.wait_for("ui", ui_kind="tools")["tools"]}
        assert KERNEL not in names
    finally:
        assert session.close() == 0


def test_a_bad_action_or_name_is_refused_with_a_reason(session_factory):
    """认不出来的动作、清单里没有的名字：**回一句人话，什么都不改**。

    认不出来的动作被当成 `list` 是最坏的失败形态（一次打错的 `load` 看起来像成功）。
    """
    session = session_factory("fake")
    try:
        session.wait_for("init")

        session.send({"v": 1, "t": "mcp", "action": "reload", "servers": ["fake"]})
        notice = session.wait_for("notice")
        assert notice["code"] == "mcp" and "认不出这个动作" in notice["text"]

        session.send({"v": 1, "t": "mcp", "action": "load", "servers": ["nope"]})
        result = session.wait_for("ui", ui_kind="mcp")
        assert any("清单里没有 server" in text for text in result["mcp_notes"])
        assert result["mcp_servers"][0]["state"] == "unload"
    finally:
        assert session.close() == 0


def test_a_server_that_cannot_start_shows_up_as_failed(fake_openai, workdir):
    """起不来的 server：那一格是 `failed` + 原因，而且**不拦启动**。

    用"命令根本不存在"这一种（比 `--exit-now` 更接近真实的手滑）。失败必须落在
    **两处**：会话流里那一条通知，以及 `/mcp` 清单里那一格带着原因 —— 只说一句
    "没连上"而清单里那一格还是 `unload` 的话，用户会以为是自己没按到。
    """
    home = workdir / "home"
    (home / ".tudouni").mkdir(parents=True, exist_ok=True)
    (home / ".tudouni" / "mcp.json").write_text(json.dumps({"servers": {
        "broken": {"command": "definitely-not-a-real-command-xyz", "timeout_seconds": 5},
    }}), encoding="utf-8")

    session = Session(home)
    try:
        session.wait_for("init")

        session.send({"v": 1, "t": "mcp", "action": "load", "servers": ["broken"]})
        result = session.wait_for("ui", ui_kind="mcp")

        row = result["mcp_servers"][0]
        assert row["state"] == "failed"
        assert row["error"], "失败那一格必须带上原因"
        assert any("没连上" in text for text in result["mcp_notes"])
        # 再按一次就是重试（这里还是同一条不存在的命令，所以还是失败）。
        session.send({"v": 1, "t": "mcp", "action": "load", "servers": ["broken"]})
        again = session.wait_for("ui", ui_kind="mcp")
        assert again["mcp_servers"][0]["state"] == "failed"
    finally:
        assert session.close() == 0
