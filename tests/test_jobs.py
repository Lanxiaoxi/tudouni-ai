"""后台命令（shell_background / job_output / job_list / job_kill）。

这一组测试盯的不是"后台能不能跑"，而是三件**会静默出错**的事 —— 前两件是用户明确
点名要先保住的：

  1. **不许假装成功。** 在它之前，"一条 tool 消息就是一次事实"，所以模型看到 `退出码 0`
     才敢说成功。后台化把这条掰断了：`shell_background` 返回的是一句"已启动"，
     而它在历史里**和一次成功长得一模一样**。模型据此写下"测试通过"就是一次静默的假
     成功。所以下面有一批断言专门盯措辞：起的时候必须写"结果未知"、没收完之前必须每轮
     提醒、跑着的任务的输出必须被标成"部分"而不是"结果"。
  2. **不许泄漏进程。** 每一个活着的后台任务都是一棵可能活得比会话还久的进程树。
     所以：条数有上限、输出有上限、`close()` 必须收干净、上一个进程留下的痕迹要能看见。
  3. **接线不能漏。** 四个工具的风险等级、都不能并行、不传表就不注册 —— 以及
     `security/commands.py` 那张表里必须有 `shell_background`（漏了它，同一个前缀规则
     对前台命令生效、对后台命令不生效，而症状只是"怎么又问我了"）。

真跑命令的测试只用 `echo` / `exit` 和"睡一会儿"；后者两个 shell 写法不同，用一个按
平台分支的小函数兜住（和 test_shell.py 同一条）。
"""

import os
import platform
import time
from pathlib import Path

import pytest

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import PermissionPolicy
from agent_runtime.security.commands import command_of
from agent_runtime.state import Session
from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin import jobs as jobs_module
from agent_runtime.tools.builtin.jobs import (
    MAX_OUTPUT_CHARS,
    JobBoard,
    job_note,
)
from agent_runtime.tools.tool import RiskLevel

from fakes import ScriptedModel, tool_call, usage


def sleep_command(seconds: int) -> str:
    """「睡一会儿」在两个 shell 里写法完全不同 —— 这种地方只能按平台分支。"""
    if platform.system() == "Windows":
        return f"Start-Sleep -Seconds {seconds}"
    return f"sleep {seconds}"


def noisy_sleep_command() -> str:
    """先吐一段输出、再挂着不动 —— 用来量"输出超限"那条闸。"""
    if platform.system() == "Windows":
        return "'x' * 200; Start-Sleep -Seconds 10"
    return "printf 'x%.0s' $(seq 1 200); sleep 10"


@pytest.fixture
def board(workdir):
    """一张用完一定收掉的表 —— 测试失败时也不该在机器上留下进程。"""
    made = JobBoard(workdir, workdir / ".tudouni" / "jobs" / "s", show_root=".tudouni/jobs/s")
    try:
        yield made
    finally:
        made.close()


def call(board: JobBoard, name: str, **arguments):
    """按模型调用的那条路跑一次：注册表 → 参数校验 → handler。"""
    registry = create_tool_registry(".", jobs=board)
    return registry.get(name).execute(arguments)


def wait_until_over(board: JobBoard, job_id: str, timeout: float = 20.0) -> None:
    """等一条任务自己结束。**靠 list() 推状态**（那是唯一推进状态的地方）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not board._jobs[job_id].running:
            return
        board.list()
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 在 {timeout} 秒里没有结束")


# --- 不许假装成功：起的时候 ---------------------------------------------------

def test_starting_returns_before_the_command_is_over(board):
    """它**立刻返回** —— 这正是这个功能存在的理由（阻塞的那两分钟拿去干别的）。"""
    started = time.perf_counter()
    result = call(board, "shell_background", command=sleep_command(10))
    elapsed = time.perf_counter() - started

    assert elapsed < 5, "起一条要睡 10 秒的命令，不该在那里等它"
    assert "[在跑" in board.list().text


def test_the_start_text_never_reads_like_a_result(board):
    """起的时候必须白纸黑字写"结果未知"。

    这段文本此后会一直躺在会话历史里，而它是模型判断"这条命令成没成"的唯一依据 ——
    写得像一次成功，模型就会当成功用。所以除了断言"结果未知"在，还要断言**没有任何
    只有结果才会有的东西**（退出码）混进去。
    """
    text = call(board, "shell_background", command=sleep_command(10)).text

    assert "结果未知" in text
    assert "不知道它成没成" in text
    assert "退出码" not in text
    # 纪律也要写在返回文本里（系统提示词对老会话已经过期，而这段文本一定在）
    assert "不要说它成功了" in text


def test_the_audit_says_it_is_a_background_start(board):
    """审计要能分辨"这是一次后台启动"，而不是一条普通 shell 调用。"""
    result = call(board, "shell_background", command=sleep_command(10))

    assert result.audit["background"] is True
    assert result.audit["job_status"] == "started"


# --- 不许假装成功：收的时候 ---------------------------------------------------

def test_a_finished_job_gives_the_real_result(board):
    job_id = call(board, "shell_background", command="echo hi").audit["job_id"]
    wait_until_over(board, job_id)

    text = call(board, "job_output", job_id=job_id).text

    assert "退出码 0" in text
    assert "hi" in text
    assert "结果" in text


def test_a_running_job_is_never_presented_as_a_result(board):
    """`wait=false` 取回来的东西必须被标成**部分**输出。

    长测试跑到一半那句 `3 passed` 就是这里最危险的诱饵：它是真的，只是不完整 ——
    而模型分不出"这是结果"和"这是到目前为止"。
    """
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    text = call(board, "job_output", job_id=job_id, wait=False).text

    assert "还在跑" in text
    assert "部分输出" in text
    assert "这不是结果" in text
    assert "不代表它成功了" in text
    assert "退出码 0" not in text


def test_waiting_that_times_out_still_says_it_is_not_a_result(board):
    """等到超时也不能把部分输出当成结论交出去 —— 只说"它还没结束"。"""
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    text = call(board, "job_output", job_id=job_id, wait=True, wait_seconds=1).text

    assert "还在跑" in text
    assert "等了 1 秒它还没结束" in text
    assert "这不是结果" in text


def test_only_a_real_ending_counts_as_collected(board):
    """`wait=false` 收过不算收过 —— 载荷尾部那行提醒必须继续挂着。"""
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    call(board, "job_output", job_id=job_id, wait=False)
    text = call(board, "job_output", job_id=job_id, wait=True, wait_seconds=1).text

    assert "还在跑" in text


def test_a_killed_job_is_not_a_result(board):
    """被我们收掉的任务，它的输出**两个都不是**（既不是结果，也不是失败的结果）。"""
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    killed = call(board, "job_kill", job_id=job_id).text
    assert "已终止" in killed
    assert "不是它自己结束的" in killed

    collected = call(board, "job_output", job_id=job_id).text
    assert "是被终止的，没有结果" in collected


# --- 不许假装成功：载荷尾部 ---------------------------------------------------

def test_the_note_appears_and_keeps_shouting_until_collected(board):
    """「已结束但结果还没收」必须一直说，直到真的收掉。

    那是这个功能唯一会静默出错的地方：模型不知道它跑完了，就会去猜，而猜出来的"成功"
    和真的成功在历史里长得一模一样。
    """
    job_id = call(board, "shell_background", command="echo hi").audit["job_id"]
    wait_until_over(board, job_id)

    note = board.note()
    assert note is not None
    assert "结果还没收" in note
    assert "再下结论" in note

    call(board, "job_output", job_id=job_id)

    assert board.note() is None, "收走之后就不该再为它付载荷的钱"


def test_the_note_lists_a_running_job(board):
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    note = board.note()

    assert job_id in note
    assert "在跑" in note
    assert "在收到结果之前不算成功" in note


def test_no_outstanding_jobs_means_no_note(board):
    """没东西悬着就一个字都不说 —— 载荷尾部是整段对话里单价最贵的位置。"""
    assert board.note() is None
    assert job_note(None) is None


def test_the_note_reaches_the_model_in_every_request(board):
    """一段贴在请求末尾的文本，模型**每一步**都看得到 —— 而不是让它去翻历史。

    和任务列表走的是同一条通道（`agent.session_notes` → `_status_note`），所以这里
    只验"它确实进了载荷、而且和步数提示合成同一条临时消息"。
    """
    session = Session.new("s")
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("shell_background",
                                            {"command": sleep_command(10)})],
                      usage=usage()),
        ModelResponse(content="好", usage=usage()),
    ])
    agent = Agent(
        model,
        create_tool_registry(".", jobs=board),
        PermissionPolicy({RiskLevel.LOW, RiskLevel.HIGH}),
        session_notes=lambda metadata: job_note(board) or "",
    )
    agent.run(session, "起个后台任务")

    injected = [
        str(m.get("content")) for m in model.seen_messages[1]
        if m["role"] == "user" and "## 后台任务" in str(m.get("content"))
    ]
    assert injected, "第二步的请求里必须带着后台任务那一段"
    assert "在跑" in injected[0]
    assert "剩余步数" in injected[0], "和步数提示合成同一条，载荷尾部只有一条临时消息"


def test_the_note_never_enters_the_session_messages(board):
    """逐轮变化的东西不持久化 —— 和任务列表同一条约定。

    判据是那段注入文本的**抬头**，不是"后台任务"这四个字：这四个字本来就会出现在
    `shell_background` 的**工具结果**里，而那个结果当然该进历史（它是那次调用的事实）。
    """
    session = Session.new("s")
    model = ScriptedModel([
        ModelResponse(content=None,
                      tool_calls=[tool_call("shell_background",
                                            {"command": sleep_command(10)})],
                      usage=usage()),
        ModelResponse(content="好", usage=usage()),
    ])
    Agent(
        model,
        create_tool_registry(".", jobs=board),
        PermissionPolicy({RiskLevel.LOW, RiskLevel.HIGH}),
        session_notes=lambda metadata: job_note(board) or "",
    ).run(session, "起个后台任务")

    persisted = [str(m.get("content")) for m in session.messages]
    assert not any("在收到结果之前不算成功" in text for text in persisted)
    # 但那一次调用本身**必须**留下记录（"后台任务 1 已启动"就是那条 tool 结果）
    assert any("结果未知" in text for text in persisted)


# --- 错误路径都是"返回文本"，不是抛异常 ---------------------------------------

def test_an_unknown_job_id_is_returned_not_raised(board):
    """参数错了是模型自己改得对的事 —— 抛出去会被记成工具故障，而它恰恰拿不到这句话。"""
    text = call(board, "job_output", job_id="99").text

    assert "没有 id 为 '99'" in text


def test_an_unknown_job_id_lists_what_there_is(board):
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    text = call(board, "job_kill", job_id="99").text

    assert job_id in text


def test_killing_something_already_over_says_so(board):
    job_id = call(board, "shell_background", command="echo hi").audit["job_id"]
    wait_until_over(board, job_id)

    text = call(board, "job_kill", job_id=job_id).text

    assert "早就结束了" in text


# --- 不许泄漏进程 -------------------------------------------------------------

def test_close_collects_running_jobs(board):
    """`Runtime.close()` 里那一句调的就是它。漏了它，用户机器上就多一个还在跑的服务。"""
    job_id = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]
    job = board._jobs[job_id]
    output = job.output_path

    board.close()

    assert job.proc.poll() is not None, "close() 之后进程必须已经死了"
    assert board.note() is None


def test_close_leaves_nothing_on_disk(board):
    """收尾不留重量：表一空，那些输出文件就再没有读者了（能读到的只剩人手工去开）。

    清的是 `*.out` 而不是整个目录，所以这里同时验"目录空掉"和"别的东西没被碰"。
    """
    call(board, "shell_background", command="echo hi")
    bystander = board.root / "别人的东西.txt"
    bystander.write_text("别删我", encoding="utf-8")

    board.close()

    assert list(board.root.glob("*.out")) == []
    assert bystander.read_text(encoding="utf-8") == "别删我"


def test_close_is_idempotent(board):
    call(board, "shell_background", command=sleep_command(10))

    board.close()
    board.close()          # 不该炸（Runtime.close 可能被调两次）


def test_the_live_job_cap_refuses_instead_of_leaking(board, monkeypatch):
    """活着的任务有上限，而且**拒绝的时候要给出办法**（不是干巴巴一句"不行"）。"""
    monkeypatch.setattr(jobs_module, "MAX_LIVE_JOBS", 1)
    call(board, "shell_background", command=sleep_command(10))

    text = call(board, "shell_background", command=sleep_command(10)).text

    assert "起不了" in text
    assert "job_output" in text
    assert "job_kill" in text


def test_the_record_cap_gives_way_for_collected_jobs(board, monkeypatch):
    """收了结果的记录会让位（它的信息已经在会话历史里了）。"""
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 1)

    first = call(board, "shell_background", command="echo one").audit["job_id"]
    wait_until_over(board, first)
    call(board, "job_output", job_id=first)

    second = call(board, "shell_background", command="echo two").audit["job_id"]

    assert second != first
    assert first not in board._jobs


def test_the_record_cap_refuses_while_something_is_still_outstanding(board, monkeypatch):
    """**没被收走的记录永远不丢** —— 丢掉它等于把"这件事还没完"从模型眼前抹掉。"""
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 1)
    first = call(board, "shell_background", command=sleep_command(10)).audit["job_id"]

    text = call(board, "shell_background", command=sleep_command(10)).text

    assert first in board._jobs
    assert "起不了" not in text          # 这条先撞的是"在跑的上限"，不是记录上限
    monkeypatch.setattr(jobs_module, "MAX_LIVE_JOBS", 99)
    text = call(board, "shell_background", command=sleep_command(10)).text
    assert "都还没收场" in text
    assert first in board._jobs


def test_output_over_the_cap_terminates_the_job(board, monkeypatch):
    """输出有上限 —— 起一个一直打日志的服务，几小时下来就是几个 GB，而没人在看。"""
    monkeypatch.setattr(jobs_module, "MAX_JOB_OUTPUT_BYTES", 10)
    job_id = call(board, "shell_background", command=noisy_sleep_command()).audit["job_id"]

    deadline = time.time() + 20
    while time.time() < deadline and board._jobs[job_id].running:
        board.list()
        time.sleep(0.05)

    job = board._jobs[job_id]
    assert not job.running
    assert "输出超过" in job.reason


def test_leftovers_from_a_previous_process_are_pruned_and_counted(workdir):
    """上一个进程留下的输出文件：清掉，而且**要说出来**。

    它的含义是"那次会话没有正常收场"，所以那几个进程现在可能还在跑 —— 这件事比那几个
    文件本身重要得多（见 composition 里那条 notice）。
    """
    root = workdir / ".tudouni" / "jobs" / "s"
    root.mkdir(parents=True)
    (root / "1.out").write_text("上一次的", encoding="utf-8")
    (root / "2.out").write_text("上一次的", encoding="utf-8")

    made = JobBoard(workdir, root, show_root=".tudouni/jobs/s")

    assert made.leftovers == 2
    assert list(root.glob("*.out")) == []


def test_pruning_survives_an_undeletable_file(workdir, monkeypatch):
    """清不掉也要能起来 —— 那是上一个进程的垃圾，不该拦住这一次会话。"""
    root = workdir / ".tudouni" / "jobs" / "s"
    root.mkdir(parents=True)
    (root / "1.out").write_text("x", encoding="utf-8")

    def explode(self):
        raise OSError("占着")

    monkeypatch.setattr(Path, "unlink", explode, raising=False)

    assert JobBoard(workdir, root).leftovers == 0


# --- 接线 ---------------------------------------------------------------------

def test_the_four_tools_are_not_registered_without_a_board():
    """不传那张表就是不注册这四个工具 —— 和缺密钥不注册 web_search 同一条路。

    默认值绝不偏到"看起来能用"那一边：注册了却没有表，模型只会白花一步去调一次。
    """
    names = {tool.name for tool in create_tool_registry(".").all()}

    assert "shell_background" not in names
    assert "job_output" not in names
    assert "job_list" not in names
    assert "job_kill" not in names


def test_risk_levels(board):
    """**只有起命令那一个是 HIGH**（它执行任意命令，走和 shell 同一套审批）。

    另外三个是 LOW，而 `job_kill` 那个 LOW 是有代价的（模型可能收掉你还想要的任务）。
    换来的是**不造一个没有信息量的仪式**：审批提示只能显示 `job_id=3`，而"3 是哪条命令"
    在参数里根本没有 —— 那正是"看不全就签字等于没审批"。
    """
    registry = create_tool_registry(".", jobs=board)
    risk = {tool.name: tool.risk for tool in registry.all()}

    assert risk["shell_background"] == RiskLevel.HIGH
    assert risk["job_output"] == RiskLevel.LOW
    assert risk["job_list"] == RiskLevel.LOW
    assert risk["job_kill"] == RiskLevel.LOW


def test_none_of_them_can_run_in_a_parallel_batch(board):
    """一个都不能并行：两个有副作用，另两个会阻塞（wait 最长 MAX_WAIT_SECONDS）。

    这里盯的是登记结果而不是理由 —— 将来谁给它标上 `parallel_safe`，这条会红。
    """
    registry = create_tool_registry(".", jobs=board)

    for name in ("shell_background", "job_output", "job_list", "job_kill"):
        assert registry.get(name).parallel_safe is False


def test_background_commands_walk_the_same_prefix_rules():
    """`security/commands.py` 那张表里必须有它。

    漏了的后果是"同一个前缀规则对前台命令生效、对后台命令不生效" —— 用户写过 `pytest`
    放行，换一条后台命令却还是被问，而症状只是"怎么又问我了"。
    """
    assert command_of("shell_background", {"command": "git status"}) == "git status"
    assert command_of("shell", {"command": "git status"}) == "git status"
    assert command_of("job_output", {"job_id": "1"}) is None


def test_the_schema_carries_the_discipline(board):
    """工具描述每一轮都发 —— 所以负面清单必须写在描述里（提示词只对新建会话生效）。"""
    registry = create_tool_registry(".", jobs=board)
    described = registry.get("shell_background").description

    assert "绝不要说它成功了" in described
    # 措辞刻意分成两半：**测试、构建、lint 不许改它读的文件**，而**服务类反过来**
    # （改了才会重载，那正是你要的）。合成一句含糊的"别改文件"会让模型以为起了一个
    # dev server 之后就不能动代码了 —— 那正好把这个功能的主要用法废掉。
    assert "不要改它当作输入读的文件" in described
    assert "服务类任务反过来" in described
    assert "前端 + 后端" in described


def test_shell_points_at_the_background_option_only_when_it_exists(board):
    """两个工具的能力在这里重叠，所以 **`shell` 的描述里也要说清分工** ——
    模型才是做选择的那个人（和 grep 描述里那句"别用 shell 去搜"同一条理由）。

    但没有那张表时不能点名一个 schema 里根本不存在的工具 —— 那正是"缺密钥时提示词
    点名 web_search"的同一个毛病：模型无从判断，只会白花一步去调一次。
    """
    with_board = create_tool_registry(".", jobs=board).get("shell").description
    without = create_tool_registry(".").get("shell").description

    assert "shell_background" in with_board
    assert "shell_background" not in without


def test_output_limit_is_the_shell_one(board):
    """上限是从 shell 插值来的，不是手抄的第二份。"""
    registry = create_tool_registry(".", jobs=board)
    properties = registry.get("job_output").parameters["properties"]

    assert str(MAX_OUTPUT_CHARS) in registry.get("job_output").description
    assert properties["wait_seconds"]["maximum"] == jobs_module.MAX_WAIT_SECONDS
    assert properties["wait_seconds"]["default"] == jobs_module.DOCUMENTED_WAIT_SECONDS


def test_output_wait_must_be_positive(board):
    """下限和上限都由 schema 钉死 —— 撞一次参数错误就得再问一次人。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        call(board, "job_output", job_id="1", wait_seconds=0)
    with pytest.raises(ValidationError):
        call(board, "job_output", job_id="1", wait_seconds=jobs_module.MAX_WAIT_SECONDS + 1)


# --- 进程那一层 ---------------------------------------------------------------

def test_a_started_process_writes_straight_to_its_output_file(workdir):
    """输出直接落盘（stdout 就是那个文件）—— 所以这个模块一个后台线程都不需要。"""
    from agent_runtime.process import start, terminate_tree

    path = workdir / "out.txt"
    proc = start(["cmd", "/c", "echo hi"] if os.name == "nt" else ["/bin/sh", "-c", "echo hi"],
                 cwd=workdir, output_path=path)
    try:
        proc.wait(timeout=20)
    finally:
        terminate_tree(proc)

    assert "hi" in path.read_bytes().decode("utf-8", "replace")


def test_terminate_tree_kills_the_direct_child(workdir):
    """`close()` 那条路最后落到这里。树那一半由 process.py 的 Job Object 负责
    （它的验收在 Windows 上单独跑，见下面那条）。

    这里是**"taskkill 失败也要补一刀"**那条的唯一防线：受限环境里那个外部程序会被拒
    （实测报 Access denied），而它被拒的时候进程还活着 —— 老那版无条件 return，
    于是"收树"在最需要它的环境里静默变成什么都不做。
    """
    from agent_runtime.process import start, terminate_tree

    argv = (["cmd", "/c", "ping", "-n", "30", "127.0.0.1"] if os.name == "nt"
            else ["/bin/sh", "-c", "sleep 30"])
    proc = start(argv, cwd=workdir, output_path=workdir / "out.txt")
    try:
        time.sleep(0.5)
        assert proc.poll() is None, "它本来就该还在跑，否则这条测试什么都没验到"

        terminate_tree(proc)

        assert proc.wait(timeout=20) is not None
    finally:
        terminate_tree(proc)


def test_the_whole_loop_through_a_real_turn(board):
    """用户那个场景的缩微版：**起 → 干别的 → 收**，走真实回合。

    这一条和上面那些的区别在于它不直接调 handler：它走的是 `Agent.run` 那条路
    （参数校验、权限裁决、串行批次、tool 消息回灌）。工具级测试全绿而这条路断掉是
    完全可能的 —— 那个形状的失败是"模型起了任务，然后永远收不到结果"。
    """
    session = Session.new("s")
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[
            tool_call("shell_background", {"command": "echo done"}, "c1"),
        ], usage=usage()),
        # 中间那一步就是"测试跑着的时候写提交信息"——这里用一次普通的工具调用来占位。
        ModelResponse(content=None, tool_calls=[
            tool_call("job_output", {"job_id": "1", "wait": True, "wait_seconds": 30}, "c2"),
        ], usage=usage()),
        ModelResponse(content="跑完了", usage=usage()),
    ])
    agent = Agent(
        model,
        create_tool_registry(".", jobs=board),
        PermissionPolicy({RiskLevel.LOW, RiskLevel.HIGH}),
        session_notes=lambda metadata: job_note(board) or "",
    )

    answer = agent.run(session, "跑一下")

    assert answer == "跑完了"
    tool_texts = [m["content"] for m in session.messages if m["role"] == "tool"]
    assert len(tool_texts) == 2
    assert "结果未知" in tool_texts[0], "起的那一步绝不能读起来像成功"
    assert "退出码 0" in tool_texts[1] and "done" in tool_texts[1]
    # 收走之后它的记录就该腾位置了 —— 载荷尾部不该再提它
    assert board.note() is None


def test_two_jobs_run_side_by_side_frontend_and_backend(board):
    """**前端一个、后端一个同时跑** —— 用户那个场景，这一条盯着它成立。

    它同时是"后台"这件事和"一批工具调用"那套东西的分界线：两条 `shell_background`
    在**同一条 assistant 消息里**发出来，走的是**串行**那条路（它们有副作用、还都要
    审批，所以 `_run_batch` 不会把它们塞进线程池）—— 而"串行"在这里没有代价，
    因为每条都**立刻返回**。真正同时在跑的是两个**进程**，不是两次工具调用。
    """
    first = call(board, "shell_background", command=sleep_command(20))
    second = call(board, "shell_background", command=sleep_command(20))
    a, b = first.audit["job_id"], second.audit["job_id"]

    assert a != b, "两条任务必须是两个 id —— 否则 job_output 收的是同一条"
    assert len(board._live()) == 2

    # 载荷尾部**两条都在**（模型下一步要能同时看见它们）。
    note = board.note()
    assert note.count("[在跑") == 2
    assert f"{a}:" in note and f"{b}:" in note

    # 两条各自独立收尾：收掉一条不影响另一条。
    board.kill(a)
    assert len(board._live()) == 1
    assert board._jobs[b].running

    # 会话结束时另一条也收掉。
    proc_b = board._jobs[b].proc
    board.close()
    assert proc_b.poll() is not None


def test_the_panel_snapshot_puts_outstanding_first(board):
    """给界面的那份快照：**还没收场的排在前面**，而且四档的判定由 runtime 算好。

    "什么样的组合算『结果还没收』"不该有第二份定义 —— 界面自己去拼三个布尔的话，
    它漂掉的样子是**面板上少了一个警告**，而那个警告正是这个功能唯一会静默出错的
    地方。
    """
    done = call(board, "shell_background", command="echo hi").audit["job_id"]
    wait_until_over(board, done)
    call(board, "job_output", job_id=done)

    running = call(board, "shell_background", command=sleep_command(20)).audit["job_id"]
    uncollected = call(board, "shell_background", command="echo two").audit["job_id"]
    wait_until_over(board, uncollected)

    panel = board.panel()

    # 悬着的在前、收过的在后；**组内按起的先后**（和 `job_list` 同一个次序 ——
    # 换一套排序就等于多一条"哪个更该先看"的规则，而那条规则没有第二个消费者）。
    assert [row["state"] for row in panel] == ["running", "uncollected", "done"], \
        "悬着的在前，收过的在后"
    assert [row["id"] for row in panel] == [running, uncollected, done]
    assert panel[1]["exit_code"] == 0 and panel[1]["command"] == "echo two"
    assert isinstance(panel[0]["seconds"], int)


def test_a_job_started_mid_read_does_not_break_the_snapshot(board):
    """读循环那个线程构造面板快照时，回合线程可能**正好在往表里插一条**。

    这不是假想的交错：`ui(kind:"state")` 在每次 `tool_result` 之后发一份，而
    `set_model` / `/autopilot` 那些命令也各发一份 —— 前者在回合线程、后者在读循环
    那个线程（见 protocol/channels.py）。所以"边遍历边被改"是必然会遇上的。

    老写法（直接 `for job in self._jobs.values()`）在这种交错下抛
    `RuntimeError: dictionary changed size during iteration`，而落点是**读循环** ——
    那一条异常会把前端的消息泵整个带走。`_snapshot()` 取副本，所以这条在它下面是绿的。
    """
    call(board, "shell_background", command="echo one")
    original = board._refresh

    def refresh_and_insert(job):
        # 只劫持第一次：在遍历那张表的**中间**插一条新的。
        board._refresh = original
        call(board, "shell_background", command="echo two")
        original(job)

    board._refresh = refresh_and_insert

    rows = board.panel()          # 不该抛

    assert len(rows) == 2
    assert len(board._snapshot()) == 2, "_snapshot 给的是一份副本"


def test_a_killed_job_is_its_own_panel_state(board):
    """被我们收掉的和"自己跑完"的必须分得开 —— 界面上前者是 `—`、后者是 `·`。"""
    job_id = call(board, "shell_background", command=sleep_command(20)).audit["job_id"]

    call(board, "job_kill", job_id=job_id)

    assert [row["state"] for row in board.panel()] == ["killed"]


def test_ui_state_carries_the_jobs_panel():
    """**装配那一层**：`Runtime.ui_state()` 里那格 `jobs` 真的接上了。

    界面上能看见后台任务的整条链是"表 → `ui_state` → 协议 → TUI"，这里钉的是第一跳。
    它断掉的样子很隐蔽：界面永远显示"当前没有后台任务"，而没有任何报错。
    """
    runtime, _session_id = open_test_runtime()
    try:
        assert runtime.ui_state()["jobs"] == [], "没起过就是空列表，不是缺字段"

        job_id = runtime.tools.get("shell_background").execute(
            {"command": sleep_command(20)}
        ).audit["job_id"]

        rows = runtime.ui_state()["jobs"]
        assert [row["id"] for row in rows] == [job_id]
        assert rows[0]["state"] == "running"
        assert rows[0]["command"] == sleep_command(20)
    finally:
        runtime.close()

    # 会话收掉之后那一格跟着空 —— 界面不该再显示一个已经不在的服务。
    assert runtime.ui_state()["jobs"] == []


def test_the_job_reaches_the_tui_through_the_real_panel_data():
    """**整条链走一遍**：真起一条后台命令 → `ui_state()` → `apply_state` → 左栏那块说得出它。

    中间任何一跳断了，症状都是"有任务在跑而界面上一个字都没有"，而且**不会报错**
    （`apply_state` 认不出一个键时是安静地跳过，`ui_state` 少发一个键也没人管）。
    所以这里不逐跳断言，直接从"起了一条命令"走到"界面上看得见"。
    """
    from agent_runtime.frontends.tui import view_state

    runtime, _session_id = open_test_runtime()
    try:
        runtime.tools.get("shell_background").execute({"command": sleep_command(20)})

        state = view_state.ViewState(session_id=runtime.session_id)
        view_state.apply_state(state, {"kind": "state", **runtime.ui_state()})

        title, count, lines = view_state.rail_blocks(state)[4]
        assert title == "后台任务"
        assert count == "1 / 1"
        assert "在跑" in "\n".join(str(line) for line in lines)
        # 以及收起左栏时那一枚 —— 它是宽屏收起时唯一的出口。
        assert str(state.jobs_badge()).startswith("后台 1")
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "nt", reason="作业对象是 Windows 上的东西")
def test_the_job_object_was_created():
    """它的失败是**静默降级**（只剩 close() 那条路），所以要有地方能看出来。

    这条在正常机器上永远是绿的；它红了意味着这台机器上"关掉窗口也把后台任务一起收掉"
    那层保证没建起来，而 `Runtime.notices()` 会照着同一个答案说一句。
    """
    from agent_runtime.process import job_object_problem

    assert job_object_problem() is None


def open_test_runtime():
    """开一个**真装配**（和 test_mcp.py 那条同一条路）。

    不碰网络：`base_url` 指向一个没人听的端口，而这里一次模型调用都不发。四个
    `*_config` 参数本来就是为这件事加的 —— 想测一条装配路径不必先设一个假 API key。
    """
    from agent_runtime.runtime.channels import cli_channels
    from agent_runtime.runtime.composition import boot, open_runtime, resolve_session
    from agent_runtime.runtime.config import (
        McpConfig,
        PermissionConfig,
        WebConfig,
    )
    from fakes import model_registry

    booted = boot()
    session_id, session, _resumed = resolve_session(booted.store, None)
    runtime = open_runtime(
        booted=booted,
        session_id=session_id,
        session=session,
        channels=cli_channels(),
        # 模型那一层现在只认目录（`ModelConfig` 随"配置只有一个来源"退休了）——
        # 给一份现造的，免得依赖跑测试的机器上恰好配了什么。
        catalog_config=model_registry(base_url="http://127.0.0.1:1",
                                      model="fake-model"),
        # 权限文件是真的从磁盘读的；给一份明确的，免得依赖跑测试的机器上恰好有配置。
        permission_config=PermissionConfig(),
        web_config=WebConfig(),
        mcp_config=McpConfig(),
    )
    return runtime, session_id


def test_open_runtime_wires_the_board_and_closes_it():
    """装配那一层：造出那张表 → 注册四个工具 → 会话结束时把它们收掉。

    **这是唯一能证明"谁来收掉那些进程"的地方**：工具、表、Agent 各自都有单测，但它们
    之间的接线 —— 尤其是 `Runtime.close()` 真的调到了 `JobBoard.close()` —— 只有真的
    走一遍装配才看得见。而接线漏掉的表现是最坏的一种：**测试全绿，用户机器上多一个
    一直在跑的服务**（它还会占着端口，让下一次启动报一句和上次会话毫无关系的错）。
    """
    runtime, session_id = open_test_runtime()

    try:
        names = {tool.name for tool in runtime.tools.all()}
        assert {"shell_background", "job_output", "job_list", "job_kill"} <= names

        board = runtime._jobs
        assert board is not None
        # 输出落在**这个会话自己的目录**里 —— 两个会话各写各的，谁也不覆盖谁。
        assert board.root.name == session_id

        job_id = runtime.tools.get("shell_background").execute(
            {"command": sleep_command(10)}
        ).audit["job_id"]
        proc = board._jobs[job_id].proc
        assert proc.poll() is None
    finally:
        runtime.close()

    assert proc.poll() is not None, "Runtime.close() 必须真的把后台命令收掉"


def test_a_broken_job_object_is_reported_at_startup(monkeypatch):
    """那层保证没建起来时**必须说一声** —— 静默降级等于让用户以为自己有。

    这条盯的是 `notices()` 里那一句。它很容易被"反正是降级、不影响功能"说服着删掉，
    而它防的正是那种情况：用户以为关掉窗口也会收干净，于是从来没检查过。
    """
    from agent_runtime import process

    monkeypatch.setattr(process, "_job_problem", "OSError: 建不起来")
    monkeypatch.setattr(process, "_job_resolved", True)

    runtime, _ = open_test_runtime()
    try:
        err = "\n".join(n.text for n in runtime.notices() if n.stream == "err")
        assert "[后台]" in err
        assert "建不起来" in err
        assert "孤儿" in err
    finally:
        runtime.close()


def test_a_healthy_startup_says_nothing_about_jobs():
    """没出事就别出声 —— 启动那一屏每一行都该是有事要说的。"""
    runtime, _ = open_test_runtime()
    try:
        err = "\n".join(n.text for n in runtime.notices() if n.stream == "err")
        assert "[后台]" not in err
    finally:
        runtime.close()
