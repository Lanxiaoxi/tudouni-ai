"""起子进程，以及**把它整棵树收掉**。

这个模块里只有操作系统那一层的知识，不认识工具、也不认识 MCP —— 因为要它的那两处
（`tools/mcp.py` 的 server、`tools/builtin/jobs.py` 的后台命令）问的是同一个问题：

    我起的这个东西，等我不管它的时候，怎么才能保证它真的死了？

## 为什么"杀一个进程"不够

Windows 上 `npx` 会再起一个 node，PowerShell 会再起一个子 shell（`npm run dev`
至少三层）。只杀直接子进程留下的是**孤儿**：它们还占着端口、还占着那个输出文件，
而下一个会话里没有任何人知道它们存在。所以一直用的是 `taskkill /T`（那是 Windows
上收树的办法，`tools/mcp.py` 里原来那份就是它）。

## close() 覆盖不到的那一半

正常退出、异常、Ctrl+C 都会走到 `Runtime.close()`。**但关掉控制台窗口不会** ——
那条路上 Python 的清理代码一行都不执行，进程被操作系统直接终止，于是每一个后台
任务都变成孤儿。`Runtime.close()` 写得再对也够不着那一半。

所以 Windows 上多一层 **Job Object**（见 `_job_object()`）：把子进程塞进一个带
`KILL_ON_JOB_CLOSE` 的作业对象，那个句柄属于**运行时进程自己** —— 进程无论怎么死，
操作系统都会关掉它，而关掉它就会把里面所有进程一起杀掉。这条保证不依赖我们跑过
任何代码，所以它是这个模块里唯一一条"真的不会被绕过"的。

POSIX 那边用 `start_new_session=True` + `killpg`：它同样收得掉整棵树，但**只覆盖
close() 那一条路** —— Linux 上要连硬杀一起覆盖得靠 `prctl(PR_SET_PDEATHSIG)`，
那是每个子进程自己设的、且 macOS 没有。没做，如实说在这里。
"""

import ctypes
import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path

__all__ = ["job_object_problem", "start", "terminate_all", "terminate_tree"]


def start(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    output_path: Path,
) -> subprocess.Popen:
    """起一个**我们必须自己收掉**的子进程，输出直接写进 `output_path`。

    三点和 `shell.py` 里那个一次性调用不同，每一点都是"它会活很久"逼出来的：

      1. **stdout 是文件，不是管道。** 管道要有人一直读，否则写满了就阻塞 —— 那就
         得养一个读线程。交给操作系统写盘之后，这个模块**一个后台线程都不需要**，
         而输出在进程还跑着的时候就能读（`job_output` 靠的就是这个）。
      2. **stderr 并进同一个文件**（`STDOUT`）。和 `shell.py` 的 `_combine` 同一个
         理由：模型对"命令输出"的心智模型就是终端里那一坨，两个流本来就是交织的。
         后台任务更没得选 —— 分成两个文件就等于要两份读游标。
      3. **`stdin` 是 DEVNULL。** 一条命令向 stdin 要输入时立刻读到 EOF，而不是把
         一个后台任务永远挂在"等待输入"上。后台尤其要紧：`shell` 挂住了模型看得见
         （它就阻塞在那儿），而后台挂住了只会表现成"这个任务永远不结束"。

    `start_new_session` 只在 POSIX 上有：它让子进程自成进程组，`terminate_tree`
    因此能一次收掉整棵树。Windows 上树由 Job Object 加 `taskkill /T` 负责。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 句柄在 Popen 返回之后就可以关掉：子进程拿到的是**它自己那份**（由
    # CreateProcess / fork 复制过去），父进程留着它既没用，还会让"这个文件被占了"
    # 变成一件真事。
    with open(output_path, "wb") as sink:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )

    _assign_to_job(proc)
    return proc


def terminate_tree(proc: subprocess.Popen) -> None:
    """强杀，而且**连子进程树一起收**。

    **它不等**（等待由调用方统一做）：走到这里之前调用方通常已经给过一次宽限了，
    所以这里直接上硬的。收不掉也不能让退出流程崩 —— 它多半已经在 finally 里了。

    POSIX 上按**进程组**杀，但只在这个进程确实自成一组时才敢那么干：`start()` 起的
    进程有 `start_new_session`，而 `tools/mcp.py` 那条老路没有 —— 对后者的 pid 调
    `killpg` 会连带把**我们自己这个组**一起杀掉（实测过这种写法的后果就是"点一下
    退出，整个 shell 都没了"）。所以那道判断不是保险，是必须的。

    **两条路走完都要再看一眼进程还在不在。** Windows 上 `taskkill` 是一个外部程序，
    它失败的方式不止"进程已经没了"一种 —— 受限环境（沙箱、被安全软件拦下的机器）里
    它会直接报 Access denied（**实测**），而这时候进程**还活着**。原来那一版无条件
    `return`，于是"收树"恰好会在最需要它的环境里静默变成什么都不做。补一刀的代价是
    对已经在退的进程再 `kill()` 一次，那是无害的。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            group = os.getpgid(proc.pid)
            if group != os.getpgid(0):
                os.killpg(group, signal.SIGKILL)
        except OSError:
            pass

    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


# --- Windows 上的 Job Object -------------------------------------------------
#
# 为什么用 ctypes 而不是第三方库：这个项目**零依赖**（见 pyproject.toml），而
# `pywin32` 只为这一件事进来太重了。代价是下面这段结构体必须和 Win32 头文件逐字段
# 对齐 —— 对不齐的后果不是报错，是 `SetInformationJobObject` 静默设了个错的东西，
# 所以 `_job_object()` 里对返回值一律检查。
#
# **它在启动时就被建起来，而不是等第一个后台命令。** 理由是这一层必须能回答
# "这台机器上那层保证到底在不在"，而 `Runtime.notices()` 是在启动时问的 —— 真等到
# 第一个后台任务再建，用户就只能在**已经起了任务之后**才知道它没生效。建一个作业
# 对象就是一个内核句柄，不用它的会话为此付的代价可以忽略。

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

# 创建失败的**唯一**一次报告。降级本身可以接受（close() 那条路还在），但静默降级
# 不行 —— 用户会以为自己有那层保证，而实际上没有。和 mcp.py 里"收不掉也要大声说"
# 是同一条。
_job_handle: int | None = None
_job_problem: str | None = None
_job_resolved = False


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        # 后面两个是 SIZE_T：32 位进程里它们是 4 字节，而 ctypes 按平台取宽度 ——
        # 写死 c_uint64 在 32 位 Python 上会让整个结构体的偏移全错。
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _job_object() -> int | None:
    """拿到那个进程级的作业对象句柄；拿不到返回 None（并且已经说过一次为什么）。

    句柄**故意泄漏到进程结束**：那正是它的用法 —— 操作系统在进程消失时关掉它，
    而关掉它就是"把里面所有进程全杀掉"这个保证的来路。主动关掉它反而会让仍然在跑的
    后台任务立刻死光，那是 `close()` 该做的事，不是这里。
    """
    global _job_handle, _job_problem, _job_resolved

    if _job_resolved:
        return _job_handle
    _job_resolved = True

    if os.name != "nt":
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")

        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject 失败")
    except (OSError, AttributeError) as exc:
        _job_problem = f"{type(exc).__name__}: {exc}"
        return None

    _job_handle = handle
    return _job_handle


def _assign_to_job(proc: subprocess.Popen) -> None:
    """把刚起的进程放进作业对象。放不进去只是少一层保证，不该让启动失败。"""
    handle = _job_object()
    if handle is None:
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    # 自己 OpenProcess 拿句柄，而不是读 `proc._handle`：那是 CPython 的私有属性，
    # 而 AssignProcessToJobObject 要的权限（SET_QUOTA | TERMINATE）它也不保证带着。
    opened = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid
    )
    if not opened:
        return
    try:
        kernel32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(opened))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(opened))


def terminate_all() -> bool:
    """把作业对象里的进程**一次全收掉**。返回"真收成了吗"。

    它是 `close()` 那条路的主力，而这不是锦上添花：Windows 上收树靠的是
    `taskkill /T`，而那是一个**外部程序** —— 受限环境（沙箱、精简过的镜像、
    被安全软件拦下的机器）里它会被拒，而拒绝的后果是 `terminate_tree` 静默退化成
    "只杀直接子进程"。实测过：在那种环境里 `taskkill` 报 Access denied，
    于是后台命令拉起来的子 shell 全留下来。

    `TerminateJobObject` 没有这个问题 —— 它是内核里的一个调用，不经过任何外部程序。
    所以 Windows 上 close() 先逐条 `terminate_tree`（好让每个任务各记各的账），
    再用这个扫一遍底。

    返回 False 分两种情况，调用方不需要区分：不在 Windows 上、或者作业对象没建起来
    （那两种情况下 `terminate_tree` 是唯一的手段，而它已经被调过了）。
    """
    handle = _job_object()
    if handle is None:
        return False

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        return bool(kernel32.TerminateJobObject(ctypes.c_void_p(handle), 1))
    except (OSError, AttributeError):
        return False


def job_object_problem() -> str | None:
    """作业对象没建起来的原因；建起来了（或不在 Windows 上）返回 None。

    给 `Runtime.notices()` 用：这是一层**用户以为自己有**的保证，静默降级等于骗人。
    所以它会**把那个对象建出来**（第一次调用时），而不是只报告一个还没发生过的失败
    —— 启动时问"它在不在"，答案必须在启动时就拿得到。
    """
    if os.name == "nt":
        _job_object()
    return _job_problem
