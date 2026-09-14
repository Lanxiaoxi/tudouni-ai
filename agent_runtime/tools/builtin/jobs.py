"""后台命令：起一条命令、把它放到一边跑、回头来收。

## 为什么需要它

`shell` 是**阻塞**的：模型发一条命令，整个回合就停在那里等它结束。于是两件事做不到：

  * 一条要跑两分钟的全盘测试，**它跑着的时候模型本来可以同时把提交信息写掉** ——
    省下的正是重叠的那一段；
  * **起一个后端服务然后接着改代码，这件事根本做不到**：dev server 永远不会返回，
    它只会在 `shell.MAX_TIMEOUT_SECONDS` 到点时被掐掉（连带那 300 秒白等）。

第二条才是真正值钱的那一条 —— 它不是慢，是**没有这条路**。

## 这个模块唯一真正难的地方：「已启动」不是「已成功」

在它之前，整个运行时的历史里有一条铁律：**一条 tool 消息就是一次事实**。系统提示词
那句「工具结果返回前不要假设成功」说的正是它。后台化把这条铁律掰断了 ——
`shell_background` 返回的是「命令开跑了，成没成我还不知道」，而**这句话在历史里长得
和一次成功一模一样**。模型据此写下「测试通过」，用户看到的就是一次静默的假成功，而
它伪装成验证通过（README 反对并发写操作时用的正是这个词）。

所以这个模块有一半的代码在防这一件事，而且**三处一起防**（少一处就等于没防）：

  1. `shell_background` 的返回文本**不许出现"成功"能沾上的词**，必须白纸黑字写"结果未知"；
  2. `job_note()` 把"在跑 / 已结束但还没收"每轮贴进**载荷尾部** —— 和任务列表同一条
     通道，因为那里才是模型**要决策的那一刻**（几十步之前的一条 tool 消息不是）；
  3. `job_output` 对还没结束的任务**必须**先说"还在跑"再给输出，而且明确标注那段输出
     是**部分**的、**不是**结果。一个长测试跑到一半打出来的 `3 passed` 就是这里最危险
     的诱饵 —— 它看起来像结论，而且它是真的，只是不完整。

## 三条设计决定

1. **不占用 `parallel_safe` 那条路。** 后台不是并行：并行是"同一批、等齐、按序回灌"
   （见 `agent.py` 的 `_run_batch`），而后台是"发出去、我接着干、回头收"。混进那一套
   会把「整批要么并行要么整批串行」那个偏序保证弄坏，所以它是**独立的一组工具**。
   顺带一句：`shell_background` 是 HIGH，和 `shell` 一样每次都要审批 —— 而审批**天然
   还是同步的**（裁决发生在 `_prepare` 里、执行之前），所以这个功能一点安全性都没松。
2. **状态从 `Popen.poll()` 现算，输出直接落盘。** 于是整个功能**零后台线程**：子进程
   自己的 stdout 就是那个文件（操作系统替你写），"跑完了没有"在每次需要的时候问一次
   进程对象。这和 `_run_parallel` 那条"事件一律由主线程发"是同一种口味 ——
   不留隐藏的机器。
3. **能收就一定要收掉。** 正常退出、异常、Ctrl+C 都走 `close()`，而关掉控制台窗口走的是
   `process.py` 里那个 Job Object。后台任务把"孤儿进程"从例外变成了常态，所以这一层
   不是可选的：见 `close()` 和 `MAX_LIVE_JOBS` / `MAX_JOB_OUTPUT_BYTES` 那两条闸。

## 刻意没做的

  * **跨进程恢复。** 会话文件里活不过来的东西这里也一样（进程句柄没法序列化），所以
    重启之后那些任务既读不到也杀不掉 —— 上一个进程留下的输出文件在 `__init__` 里清掉，
    并且**说出来**（见 `leftovers`）。要真做，得先记 pid、再解决"这个 pid 还是不是当初
    那个进程"（pid 会被复用，认错了就是杀掉一个无辜的进程）。
  * **单条任务的超时。** 起服务本来就不该有超时，而不带超时的命令本来就没法用超时兜住
    （一个死循环的测试和一个 dev server 在这里长得一样）。有的是别的闸：同时活着的条数、
    输出上限、每一步都能看见它、以及 `job_kill`。
"""

# **这一行不是风格，是必须的。** `JobBoard` 里有一个方法叫 `list`（它是那四个工具之一），
# 而类体里的注解是**当场求值**的 —— 于是 `def panel(self) -> list[dict[str, object]]`
# 里的 `list` 会先解析成那个方法，报 "function object is not subscriptable"（实测）。
# 推迟注解求值之后，整个类里再写 `list[...]` / `dict[...]` 都不会再踩这个坑。
# pydantic 那三个参数模型不受影响（它们只有 `str` / `bool` / `int` 这种能直接解析的名字）。
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from agent_runtime.process import start as start_process
from agent_runtime.process import terminate_all, terminate_tree

from ..text import truncate
from ..tool import ToolArgs, ToolResult
from .shell import MAX_TIMEOUT_SECONDS, shell_argv, shell_name

# 同时**活着**的后台任务上限。它不是"防手滑"，是"孤儿进程"这条路的闸门：每一个活着的
# 任务都是一棵可能活得比这次会话还久的进程树，所以它必须有个头。
MAX_LIVE_JOBS = 6

# 留着记录的条数上限（含已经结束的）。跑着的和**没被收走的**永远不丢（见 _make_room）。
MAX_JOBS = 12

# 单个任务的输出文件上限。到了就把任务收掉 —— 起一个会一直打日志的服务，几小时下来
# 就是几个 GB，而"这个文件长多大"没有任何人在看着。理由和 MAX_LIVE_JOBS 一样：
# 后台任务把一件本来有界的事（一条命令，跑完就完了）变成了没有界的事。
MAX_JOB_OUTPUT_BYTES = 2 * 1024 * 1024

# 一次交回给模型的字符上限。和 shell 同档（那个数是从"一次 read_file 返回 12524 字符
# 占了整轮成本 86%"的实测里来的，见 shell.py）。
MAX_OUTPUT_CHARS = 8000

# `job_output(wait=true)` 默认等多久。**上限直接复用 shell 的上限** —— 它俩是同一个事实
# （"一次工具调用最多让会话挂住多久"），抄一份过来就会在改一处时漂开。
DOCUMENTED_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = MAX_TIMEOUT_SECONDS

# 后台任务的输出住在 `<运行期目录>/jobs/<会话 id>/` 下面。**它和审计日志、会话文件是
# 同一类东西**（本机数据、随工作区走、.gitignore 里已经整目录排掉），所以它也在
# `.tudouni/` 里 —— 而且那个目录是控制面，`write_file` 写不进去（模型伪造不了一份
# "命令的输出"）。
JOBS_DIR_NAME = "jobs"


@dataclass
class Job:
    """一条后台命令。

    **它不是值对象**：它攥着一个进程句柄，所以只有"活着/死了"是有意义的比对。也没有
    `eq` —— 默认生成的 `__eq__` 会去比 Popen，那是没有意义而且会抛的东西。

    `clock` 跟着任务走而不是去调 `time.perf_counter`：审计里每一个 duration 都出自
    注入的那把尺子，任务耗时也不例外（否则测试只能断言"大于 0"）。
    """

    id: str
    command: str
    proc: subprocess.Popen
    output_path: Path
    shown_path: str          # 给人看的那份路径（相对于工作区）
    started: float
    clock: Callable[[], float]
    ended: float | None = None
    exit_code: int | None = None
    # 为什么结束的 —— **只有被我们收掉的才有**（job_kill / 会话结束 / 输出超限）。
    # 措辞直接进给模型的文本，所以它必须能独立成句（"会话结束了"而不是"closed"）。
    reason: str = ""
    # 最后一次把它当**结果**收走的时刻。它和 `ended` 分开是刻意的：结束了不等于被
    # 收走了，而这两者的差别正是"我到底看没看到结果"—— 见 note()。
    collected: float | None = None

    @property
    def running(self) -> bool:
        return self.ended is None

    @property
    def duration(self) -> float:
        now = self.ended if self.ended is not None else self.clock()
        return now - self.started

    @property
    def uncollected(self) -> bool:
        """结束了、但结果还没被收走。**这一档必须每轮都提醒模型。**"""
        return not self.running and self.collected is None


def _duration(seconds: float) -> str:
    """`45s` / `2m10s` / `1h02m`。给人看的一行，不需要精确到毫秒。"""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"


class ShellBackgroundArgs(ToolArgs):
    """shell_background 的参数。

    和 `ShellArgs` 长得几乎一样，**少一个东西：没有 timeout_seconds**。那是有意的 ——
    起服务、跑 watch 这类命令本来就不该有超时，而"给一条要跑两小时的命令设个上限"在
    这里也没有意义：它的价值在于**模型可以先去干别的**，而不是"到点自动收"。
    """

    command: str = Field(
        min_length=1,
        description=f"要执行的命令，按 {shell_name()} 的语法写。"
                    "它会在后台一直跑，直到它自己结束或被 job_kill 收掉",
    )


class JobOutputArgs(ToolArgs):
    job_id: str = Field(
        min_length=1,
        description="shell_background 返回的那个 id",
    )
    wait: bool = Field(
        default=True,
        description="true（默认）表示它还没结束就等一会儿；false 表示立刻返回"
                    "**到目前为止**的输出 —— 看一个不会结束的服务（dev server）"
                    "跑到哪儿了就用 false",
    )
    wait_seconds: int = Field(
        default=DOCUMENTED_WAIT_SECONDS,
        ge=1,
        le=MAX_WAIT_SECONDS,
        description=f"wait=true 时最多等多少秒（上限 {MAX_WAIT_SECONDS}）。到点它还没"
                    f"结束，返回的就是「还在跑」加上一段部分输出 —— 那不是结果",
    )


class JobListArgs(ToolArgs):
    """job_list 的参数：一个都没有。

    空模型仍然要有，而不是让 args_model 空着去手写 external_schema —— 理由见
    `clock.py` 里 `GetCurrentTimeArgs` 那段（内置工具的 schema 和校验只能从 args_model
    推导，绕开它就是手写第二份会漂移的事实）。
    """


class JobKillArgs(ToolArgs):
    job_id: str = Field(
        min_length=1,
        description="要终止的后台任务 id。整棵进程树都会被收掉（包括它拉起来的子进程）",
    )


class JobBoard:
    """后台任务的表，以及它周围的那些闸。

    为什么要一个对象、而不是几个模块级函数：这张表是**按会话的状态**，而且它攥着进程。
    所以它只能在会话定下来之后造出来，然后像 `TodoBoard` 那样注进注册表 —— 工具层的
    写法和其他协作方完全一致，`Tool.handler` 的契约一个字都不用动。

    和 `TodoBoard` 有一个根本区别，值得写在明处：任务列表只是 `session.metadata` 里的
    几行数据（落盘、跨进程、随便复制），而**这张表里的每一条都是一个活着的进程**。
    所以它不能被序列化、不能被恢复，而且必须在会话结束时**主动收掉** —— 见 close()。
    """

    def __init__(
        self,
        workspace: Path | str,
        root: Path,
        *,
        clock: Callable[[], float] = time.perf_counter,
        show_root: str | None = None,
    ):
        """`root` 是这个会话放输出文件的目录（`.tudouni/jobs/<会话 id>/`）。

        `show_root` 是它在给模型看的文本里的样子（相对工作区）。分开传是因为"文件实际
        写到哪"和"怎么跟模型说它在哪"是两件事：前者是绝对路径（跟 cwd 无关），后者要短。
        """
        self.workspace = Path(workspace).resolve()
        self.root = Path(root)
        self.clock = clock
        self.show_root = show_root or str(root)
        self._jobs: dict[str, Job] = {}
        self._next_id = 1
        self._closed = False
        # 上一个进程留下的输出文件数。**它不是错误、也不是噪声**：它意味着那次会话没有
        # 正常收场（关掉了窗口、或者被强杀），于是那几个进程可能还在跑，而本次会话既
        # 看不到它们也管不到它们。这件事必须说出来 —— 见 composition 里那条 notice。
        self.leftovers = self._prune_leftovers()

    # -- 内部 ---------------------------------------------------------------

    def _prune_leftovers(self) -> int:
        """清掉这个会话上一次留下的输出文件，返回清掉了几个。

        **为什么是清掉而不是留着**：这一版不支持跨进程恢复（那种任务既读不到也杀不掉），
        所以留下来的文件是纯粹的死重量 —— 每一份还可以到 2 MiB。
        """
        if not self.root.is_dir():
            return 0
        count = 0
        for entry in self.root.glob("*.out"):
            try:
                entry.unlink()
                count += 1
            except OSError:
                pass
        return count

    def _output_size(self, job: Job) -> int:
        try:
            return job.output_path.stat().st_size
        except OSError:
            return 0

    def _snapshot(self) -> list[Job]:
        """这一瞬的任务列表（一份**副本**，不是那个 dict 的视图）。

        **必须这样取，不能直接 `for job in self._jobs.values()`。** 这张表会被两个线程
        碰：回合线程在工具调用里 `start()` / `_make_room()` 往里插删，而**读循环那个线程**
        会构造面板快照（`ui(kind:"state")` 在每次 `tool_result` 之后发一份，而
        `set_model` / `/autopilot` 这些命令也各发一份 —— 见 protocol/channels.py）。
        边迭代边被改的话，CPython 抛的是 `RuntimeError: dictionary changed size during
        iteration`，而它的落点是**读循环**：那一条异常会把整个前端的消息泵带走。

        `list(dict.values())` 本身在 CPython 里是一次 C 层调用，不会被字节码切进去，
        所以它是这里唯一需要的那道保护 —— 不需要一把锁（协议层那两根锁管的也不是这件事，
        见 `ProtocolServer.__init__` 里那段）。
        """
        return list(self._jobs.values())

    def _refresh(self, job: Job) -> None:
        """把这条任务的状态推进到"现在"，顺手执行那两条闸。

        **它是唯一推进状态的地方**，而且只在这个模块需要看状态的时候被调用（每一步的
        载荷尾部、每一次 job_output / job_list / job_kill）。没有后台监视器 —— 见模块
        docstring 第 2 条。
        """
        if not job.running:
            return

        code = job.proc.poll()
        if code is not None:
            job.exit_code = code
            job.ended = self.clock()
        elif self._output_size(job) > MAX_JOB_OUTPUT_BYTES:
            job.reason = (
                f"它的输出超过了 {MAX_JOB_OUTPUT_BYTES // 1024 // 1024} MiB，已终止"
            )
            terminate_tree(job.proc)
            job.exit_code = job.proc.poll()
            job.ended = self.clock()

    def _refresh_all(self) -> None:
        for job in self._snapshot():
            self._refresh(job)

    def _live(self) -> list[Job]:
        return [job for job in self._snapshot() if job.running]

    def _make_room(self) -> str | None:
        """腾出一条记录的位置；腾不出来就返回一句给模型的拒绝话。

        **先丢已经收走结果的那些**（它们的信息已经进了会话历史），跑着的和没被收走的
        永远不丢 —— 丢一条"还没收结果"的记录等于把"这件事还没完"从模型眼前抹掉，
        而它下一轮就会以为没有这回事了。
        """
        if len(self._jobs) < MAX_JOBS:
            return None

        for job in self._snapshot():
            if job.collected is not None:
                del self._jobs[job.id]
                if len(self._jobs) < MAX_JOBS:
                    return None

        outstanding = "、".join(
            f"{job.id}（{'在跑' if job.running else '结果还没收'}）"
            for job in self._snapshot()
        )
        return (
            f"开不了新任务：同时留着的任务最多 {MAX_JOBS} 个，而现在这 "
            f"{len(self._jobs)} 个都还没收场 —— {outstanding}。\n"
            f"先用 job_output 把它们的结果收掉（收完的记录会让位），"
            f"不再需要的用 job_kill 收掉。"
        )

    def _find(self, job_id: str) -> Job | None:
        self._refresh_all()
        return self._jobs.get(job_id)

    def _unknown(self, job_id: str) -> str:
        if not self._jobs:
            return f"没有 id 为 {job_id!r} 的后台任务 —— 现在一个后台任务都没有。"
        known = "、".join(f"{job.id}（{job.command}）" for job in self._snapshot())
        return f"没有 id 为 {job_id!r} 的后台任务。现有的：{known}。"

    def _read_output(self, job: Job) -> str:
        """读输出文件。

        **整个读进来再截断**，而不是只读尾巴：上限是 2 MiB（见常量），而那点 I/O 相对
        一次模型往返可以忽略 —— 换来的是一段头尾都在的文本（`text.truncate` 的取向：
        关键信息常常压在最后，测试的汇总行就是）。
        """
        try:
            raw = job.output_path.read_bytes()
        except OSError as exc:
            return f"（读不到它的输出文件：{type(exc).__name__}: {exc}）"
        text = raw.decode("utf-8", errors="replace").rstrip()
        return truncate(text, MAX_OUTPUT_CHARS) if text.strip() else "(无输出)"

    def _body(self, job: Job) -> str:
        """任务输出的正文，**带着一段说明它是什么的抬头**。

        这个抬头是这个模块存在的理由本身：同一段字节，在任务正常结束之后是"结果"，
        在它还跑着的时候只是"到目前为止"，而在它被我们收掉之后**两个都不是**。
        三者绝不能长得一样 —— 见模块 docstring 第 2 段。
        """
        if job.running:
            head = "--- 部分输出（**任务还没结束，这不是结果**）---"
        elif job.reason:
            head = "--- 被终止时的输出（不完整，不是结果）---"
        else:
            head = "--- 输出 ---"
        return f"{head}\n{self._read_output(job)}"

    def _verdict(self, job: Job) -> str:
        """一句话说清"这条命令是怎么结束的"。**被我们收掉的绝不说成它自己退出了。**"""
        if job.running:
            return f"**还在跑**（已跑 {_duration(job.duration)}）"
        if job.reason:
            return (
                f"**不是它自己结束的**：{job.reason}"
                f"（已跑 {_duration(job.duration)}，退出码 {job.exit_code}）。"
                f"它被终止了，所以它的输出不能当结论用"
            )
        return f"已结束（退出码 {job.exit_code}，跑了 {_duration(job.duration)}）"

    def _audit(self, job: Job, status: str) -> dict[str, object]:
        """`tool_result` 上那几个只有这一层知道的字段。

        刻意**不新开事件类型**：job 的来龙去脉全都能挂在它那两次工具调用上（起的时候
        一次、收的时候一次），而新事件得一路穿到 `on_event` 去，那条路上没有 run_id。
        代价如实说在这里：**一条从没被收过的任务，它的退出码进不了审计** —— 那句话在
        载荷尾部对模型说过，也在会话文件的 tool_result 文本里，但审计里查不到。
        """
        return {
            "job_id": job.id,
            "job_status": status,
            "job_running": job.running,
            "job_ms": int(job.duration * 1000),
            **({} if job.exit_code is None else {"job_exit_code": job.exit_code}),
            **({} if not job.reason else {"job_reason": job.reason}),
        }

    # -- 四个工具 -------------------------------------------------------------

    def start(self, command: str) -> ToolResult:
        """起一条命令。**立刻返回**，不等它。

        这里唯一不能省的一句话是"结果未知"：返回的那段文本此后会一直躺在会话历史里，
        而它是模型判断"这条命令成没成"的唯一依据。
        """
        if self._closed:
            return ToolResult(
                "这次会话已经在收尾了，起不了新的后台任务。",
                {"job_status": "closed"},
            )

        self._refresh_all()

        if len(self._live()) >= MAX_LIVE_JOBS:
            running = "、".join(f"{job.id}（{job.command}）" for job in self._live())
            return ToolResult(
                f"起不了：同时最多 {MAX_LIVE_JOBS} 个后台任务在跑，现在跑着的是 {running}。\n"
                f"先 job_output 收掉一个、或者 job_kill 收掉不再需要的，再起新的。",
                {"job_status": "refused", "job_live": len(self._live())},
            )

        if (refusal := self._make_room()) is not None:
            return ToolResult(
                refusal, {"job_status": "refused", "job_live": len(self._live())}
            )

        try:
            argv = shell_argv(command)
        except FileNotFoundError as exc:
            return ToolResult(f"无法启动 shell：{exc}", {"job_status": "error"})

        job_id = str(self._next_id)
        self._next_id += 1
        output_path = self.root / f"{job_id}.out"

        try:
            proc = start_process(argv, cwd=self.workspace, output_path=output_path)
        except OSError as exc:
            return ToolResult(
                f"无法执行命令（环境问题，不是命令本身）：{type(exc).__name__}: {exc}",
                {"job_status": "error"},
            )

        job = Job(
            id=job_id,
            command=command,
            proc=proc,
            output_path=output_path,
            shown_path=f"{self.show_root}/{job_id}.out",
            started=self.clock(),
            clock=self.clock,
        )
        self._jobs[job_id] = job

        return ToolResult(
            text=(
                f"后台任务 {job_id} 已启动 —— **结果未知**。\n"
                f"命令：{command}\n"
                f"它正在独立地跑，你**现在不知道它成没成**。输出写到 {job.shown_path}。\n"
                f"- 收结果：job_output(job_id={job_id!r})"
                f"（默认最多等 {DOCUMENTED_WAIT_SECONDS} 秒；它慢就自己给 wait_seconds，"
                f"上限 {MAX_WAIT_SECONDS}）\n"
                f"- 先干别的：它是后台任务，本来就不用等它 —— "
                f"但**在收到结果之前不要说它成功了**，也别改它正在读的文件\n"
                f"- 收掉它：job_kill(job_id={job_id!r})"
            ),
            audit={"job_id": job_id, "job_status": "started", "background": True},
        )

    def output(
        self,
        job_id: str,
        wait: bool = True,
        wait_seconds: int = DOCUMENTED_WAIT_SECONDS,
    ) -> ToolResult:
        """收一条任务的结果；还没结束就等一会儿，到点返回部分输出。

        **"部分"和"结果"的措辞是这里唯一的讲究**：模型拿到一段输出之后，唯一能阻止它
        把中途那句 `3 passed` 当成结论的，就是这段文字里明明白白写着"这不是结果"。
        """
        job = self._find(job_id)
        if job is None:
            return ToolResult(self._unknown(job_id), {"job_status": "unknown"})

        if wait and job.running:
            # 这是整个工具唯一会阻塞的地方，而且阻塞的是**模型主动要求**的那一段
            # （`wait=false`、或者已经结束，都走不到这里）。
            try:
                job.proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass
            self._refresh(job)

        final = not job.running
        if final:
            # **只有真的拿到了结局才算"收走了"** —— 部分输出不算（见 Job.collected）。
            job.collected = self.clock()
            if job.reason:
                text = (
                    f"后台任务 {job_id} 是被终止的，没有结果。\n"
                    f"命令：{job.command}\n{self._verdict(job)}。\n{self._body(job)}"
                )
            else:
                text = (
                    f"后台任务 {job_id} 的结果：\n"
                    f"命令：{job.command}\n{self._verdict(job)}。\n{self._body(job)}"
                )
        else:
            waited = f"等了 {wait_seconds} 秒它还没结束。" if wait else "（没有等它。）"
            text = (
                f"后台任务 {job_id} {self._verdict(job)}。{waited}\n"
                f"命令：{job.command}\n{self._body(job)}\n"
                f"**上面只是它到这一刻为止打出来的东西 —— 不代表它成功了、"
                f"也不代表它失败了。**\n"
                f"要结果就再等（把 wait_seconds 调大，上限 {MAX_WAIT_SECONDS}），"
                f"或者先去干别的，回头再收。"
            )

        return ToolResult(text, self._audit(job, "collected" if final else "running"))

    def list(self) -> ToolResult:
        """列出留着的后台任务。**先把"没收到结果"的挑出来说**，那才是要看的东西。"""
        self._refresh_all()
        if not self._jobs:
            return ToolResult("没有后台任务。", {"job_live": 0, "job_uncollected": 0})

        live = self._live()
        uncollected = [job for job in self._snapshot() if job.uncollected]

        lines = []
        for job in self._snapshot():
            if job.running:
                mark = f"在跑 {_duration(job.duration)}"
            elif job.reason:
                mark = f"已被终止（{job.reason}）"
            elif job.uncollected:
                mark = f"已结束·退出码 {job.exit_code}·**结果还没收**"
            else:
                mark = f"已结束·退出码 {job.exit_code}·已收"
            lines.append(f"- [{mark}] {job.id}: {job.command}")

        header = f"后台任务 {len(self._jobs)} 个"
        if live:
            header += f"，其中 {len(live)} 个在跑"
        if uncollected:
            header += f"，{len(uncollected)} 个的结果还没收"

        text = "\n".join([header + "：", *lines])
        if uncollected:
            text += (
                f"\n**{'、'.join(job.id for job in uncollected)} 已经结束了但结果还没收** —— "
                f"先用 job_output 把它们收掉再下结论。"
            )
        return ToolResult(
            text, {"job_live": len(live), "job_uncollected": len(uncollected)}
        )

    def kill(self, job_id: str) -> ToolResult:
        """收掉一条任务（整棵进程树）。"""
        job = self._find(job_id)
        if job is None:
            return ToolResult(self._unknown(job_id), {"job_status": "unknown"})

        if not job.running:
            return ToolResult(
                f"后台任务 {job_id} 早就结束了（{self._verdict(job)}），不用收。\n"
                f"它的结果：job_output(job_id={job_id!r})",
                self._audit(job, "already_over"),
            )

        job.reason = "你用 job_kill 收掉了它"
        terminate_tree(job.proc)
        try:
            job.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        job.exit_code = job.proc.poll()
        job.ended = self.clock()
        job.collected = self.clock()

        return ToolResult(
            (
                f"后台任务 {job_id} 已终止（整棵进程树）。\n"
                f"命令：{job.command}\n"
                f"{self._verdict(job)}。\n"
                f"**它的输出不是结果** —— 要那个结果就重新起一条。"
            ),
            self._audit(job, "killed"),
        )

    # -- 会话状态 -------------------------------------------------------------

    def note(self) -> str | None:
        """渲染拼进载荷尾部的那一段；**没有"在跑"或"没被收走"的就返回 None**。

        已经收走结果的任务不进这里：它们的事已经了了，而载荷尾部是整段对话里单价最贵的
        位置（每一轮都要重发一遍）。

        反过来，**"已结束但结果还没收"必须一直说到被收掉为止** —— 那是这个功能唯一
        会静默出错的地方：模型不知道它跑完了，就会去猜，而猜出来的"成功"和真的成功
        在历史里长得一模一样。
        """
        interesting = [
            job for job in self._snapshot() if job.running or job.uncollected
        ]
        if not interesting:
            return None

        lines = []
        for job in interesting:
            if job.running:
                mark = f"在跑 {_duration(job.duration)}"
            else:
                mark = f"已结束·退出码 {job.exit_code}·**结果还没收**"
            lines.append(f"- [{mark}] {job.id}: {job.command}")

        text = "\n".join(
            ["## 后台任务（你起的；在收到结果之前不算成功）", *lines]
        )
        if any(job.uncollected for job in interesting):
            text += (
                "\n先用 job_output 把上面**结果还没收**的那些收掉再下结论 —— "
                "现在你还不知道它们成没成。"
            )
        return text

    def progress_line(self) -> str | None:
        """一行进度，**给人看的**：`2 个在跑、1 个结果还没收`；没什么可说时返回 None。

        和 `note()` 分开是照 `todo.progress_line` / `todo_note` 那条分工来的：模型要的是
        "有哪几条、分别是什么命令、我该去收哪条"，而人只要一眼看出"机器上还挂着东西吗"。
        合成一份的话，两边都得为对方多付 token。

        **它比任务列表那一行要紧一档**：列表忘了更新只是信息旧了，而后台任务忘了收是
        一台机器上一直在跑的进程。所以行式终端的每一轮末尾都要重打一遍。
        """
        self._refresh_all()
        live = len(self._live())
        uncollected = sum(1 for job in self._snapshot() if job.uncollected)
        if not live and not uncollected:
            return None
        parts = []
        if live:
            parts.append(f"{live} 个在跑")
        if uncollected:
            parts.append(f"{uncollected} 个结果还没收")
        ids = "、".join(job.id for job in self._snapshot() if job.running or job.uncollected)
        return f"{'、'.join(parts)}（{ids}）—— 明细 job_list"

    def panel(self) -> list[dict[str, object]]:
        """面板快照：给界面看的那几条（`ui(kind:"state")` 里的 `jobs`）。

        **判定在这里做完，界面照着渲染** —— 和 `risk_scope.disposition` 是同一条规矩
        （"三档里哪几档自动放行"是 policy 的判断，界面不该知道）。所以给的是一个已经
        算好的 `state`，而不是三个布尔让界面自己去推：界面推的话，"什么样的组合算
        『结果还没收』"就有了第二份定义，而它漂掉的样子是**面板上少了一个警告**。

        四档：
          * `running`     —— 还在跑
          * `uncollected` —— 结束了、结果还没被收走（**要人/模型做点什么的那一档**）
          * `done`        —— 结束了、结果收走了
          * `killed`      —— 被我们收掉的（job_kill / 会话结束 / 输出超限）

        **顺序：还没收场的在前**（在跑、结果没收到），已经收走的在后；**组内按起的先后**
        （和 `job_list` 同一个次序）。这是一个判定，所以它在这一层做 —— 界面按顺序渲染
        就够了（"哪些要先看"不是排版知识）。
        """
        self._refresh_all()
        outstanding = [job for job in self._snapshot() if job.running or job.uncollected]
        settled = [
            job for job in self._snapshot() if not (job.running or job.uncollected)
        ]
        return [self._panel_row(job) for job in (*outstanding, *settled)]

    @staticmethod
    def _panel_row(job: Job) -> dict[str, object]:
        if job.running:
            state = "running"
        elif job.reason:
            state = "killed"
        else:
            state = "uncollected" if job.uncollected else "done"
        return {
            "id": job.id,
            "command": job.command,
            "state": state,
            "seconds": int(job.duration),
            "exit_code": job.exit_code,
        }

    def close(self) -> None:
        """收掉所有还活着的任务，然后把这个会话的输出目录清干净。**`Runtime.close()` 里调它。**

        两条路都走，因为它们是两个不同的保证：

          * 逐条 `terminate_tree`：好让每条任务各记各的账（谁被收掉了、为什么），
            也覆盖任务没进作业对象的情况（分配失败、或者不在 Windows 上）；
          * `terminate_all`：内核级的兜底。Windows 上收树靠外部程序 `taskkill`，
            而受限环境里它会被拒（实测：沙箱里报 Access denied），被拒之后
            `terminate_tree` 会**静默退化**成"只杀直接子进程"。

        **顺手把输出文件清掉，是因为它们到这里已经没有读者了**：表一空，`job_output`
        就再也不可能打开它们（它只认表里的 id），能读到的只剩"人手工去开那个文件"。
        而一个后台功能最不该做的事就是留下重量 —— 这正是 `MAX_LIVE_JOBS` 和输出上限
        存在的理由，收尾这一刀是同一个道理的最后一环。真想要某一条的输出，就在会话里
        用 `job_output` 把它收掉 —— 那份文本此后一直在会话文件里。

        清的是 `*.out`（复用 `_prune_leftovers`），不是 `rmtree` 整个目录：**能删掉的东西
        必须是"我们自己写的那几种文件"**，而不是"一个我们传进来的路径"。两者在装配正确
        时是同一件事，在装配错了的时候差着一次删库。目录空掉之后再 `rmdir`，失败无所谓
        （可能还有别人放的东西）。
        """
        if self._closed:
            return
        self._closed = True

        self._refresh_all()
        for job in self._live():
            job.reason = "会话结束了"
            terminate_tree(job.proc)
            try:
                job.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            job.exit_code = job.proc.poll()
            job.ended = self.clock()

        # 兜底扫一遍：上面那一步漏掉的（分进作业对象之前就已经 fork 出子进程之类）在这里
        # 一起收。**放在最后**，因为它是"全都杀掉"，没有逐条记账的能力。
        terminate_all()

        self._prune_leftovers()
        try:
            self.root.rmdir()
        except OSError:
            pass

        self._jobs.clear()


def job_note(board: JobBoard | None) -> str | None:
    """载荷尾部那一段；没有 board（没装配）就什么都不说。

    它是一个模块级函数而不是只有 `JobBoard.note()`，是为了和 `todo_note(metadata)` /
    `catalog_part(...)` 排在一起读 —— `composition._session_notes` 那里三段是并列的，
    三段的取法长得不一样会更难读。
    """
    return board.note() if board is not None else None


def jobs_dir(runtime_dir: Path, session_id: str) -> Path:
    """这个会话放输出文件的目录。**只有这一处拼这个路径**（composition 也调它）
    —— 两处各拼一份，漂一个字符就是"任务起来了，但收不到输出"。"""
    return runtime_dir / JOBS_DIR_NAME / session_id


__all__ = [
    "DOCUMENTED_WAIT_SECONDS",
    "JOBS_DIR_NAME",
    "Job",
    "JobBoard",
    "JobKillArgs",
    "JobListArgs",
    "JobOutputArgs",
    "MAX_JOBS",
    "MAX_JOB_OUTPUT_BYTES",
    "MAX_LIVE_JOBS",
    "MAX_OUTPUT_CHARS",
    "MAX_WAIT_SECONDS",
    "ShellBackgroundArgs",
    "job_note",
    "jobs_dir",
]
