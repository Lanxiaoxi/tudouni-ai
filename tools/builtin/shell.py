"""在系统 shell 里执行命令。

**这个工具是唯一不受工作区边界约束的东西。**

`FileSystem.safe_path` 能拦住 `../../evil.txt`，但它拦不住一条 shell 命令 ——
`cd .. && rm -rf x` 走的是操作系统，不是 Python。README 里那句「工作区是唯一能拦住
往外面写的东西」，从这个工具开始就不成立了。所以：

  - 它的风险等级是 HIGH。main.py 的 `auto_approve={RiskLevel.LOW}` 下，**每一次
    调用都要人工审批**，没有例外。
  - 审批提示里命令原文**不许截断**（见 security/asker.py 里那张按风险分级的表）——
    判断依据就是这条命令本身，看不全就签字等于没审批。

**刻意没做参数级判断。** policy.decide() 的签名早就为它留好了位置，但第一版不给它
用：想给「读命令」开自动放行，就必须能保证它读不出工作区，而 `cat /etc/passwd`、
`git log --all` 都是只读的、也都能读到外面。真正的解法是操作系统级沙箱（Codex 的
read-only / workspace-write 是 seatbelt 和 landlock 做的），本项目还没有。在那之前，
「每次都要人看一眼」是唯一诚实的默认值。
"""

import platform
import shutil
import subprocess
from pathlib import Path

from pydantic import Field

from ..text import truncate
from ..tool import ToolArgs


# 命令的默认超时。
#
# 它是**模型可以覆盖的**：装依赖、跑全套测试这类命令几秒里结束不了，不给旋钮模型就
# 没有出路 —— 它会反复重试一条注定超时的命令，而每次重试都得再来一次人工审批，
# 代价比一次长等待大得多。
#
# 真正的约束不在默认值上，在下面那个上限上。
TIMEOUT_SECONDS = 30

# 模型能设的范围。
#
# 上限存在的意义只有一个：**把最坏情况钉死。** 没有它，"把超时交给模型"就等于
# "允许模型让会话无限期挂住"。下限挡的是填成 0 或负数那种手滑 —— 那会让每条命令都
# 立刻超时，而模型多半会把它读成"环境坏了"，然后去查一个不存在的问题。
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 300

# 返回给模型的输出上限。这个项目实测过：一次 read_file 返回 12524 字符占了整轮成本
# 的 86%，而 shell 命令的输出可以轻易到几兆。
MAX_OUTPUT_CHARS = 8000

# PowerShell 往 stdout 写的时候按 [Console]::OutputEncoding 编码。stdout 被重定向时
# 这个值取决于系统区域设置（开发这台机器上实测是 utf-8，但关掉「Unicode UTF-8」那个
# 系统选项的机器上就不是），而子进程这边是**固定按 utf-8 解码**的 —— 不先把它钉死，
# 中文输出换台机器就会变成乱码。
#
# 末尾那个换行是必需的，不是排版问题：用分号接的话，PowerShell 报错时会把出错的整行
# 源码回显出来，把这句前导一并带进模型的上下文，还会把 char: 的列号带偏（实测
# `At line:1 char:59 + ... ]::OutputEncoding = ...; no-such-cmd`）。换成换行之后回显
# 只剩用户那条命令本身，代价是行号整体 +1。
_UTF8_PREAMBLE = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"


def shell_name() -> str:
    """这个平台上实际会用的 shell 名字。

    用处是让工具描述能告诉模型该写哪种语法 —— 同一个 `ls`，PowerShell 和 sh 的输出
    格式、参数名都不一样。
    """
    return "PowerShell" if platform.system() == "Windows" else "sh"


def shell_argv(command: str) -> list[str]:
    """把一条命令包成 argv。

    **不用 `shell=True`。** 那在 Windows 上固定走 cmd.exe，而 cmd 的语法（dir、type、
    %VAR%）和模型默认会写的（PowerShell / POSIX）对不上 —— 模型写 `ls`，cmd 不认。
    这里显式挑一个 shell。

    命令原文是作为**一个 argv 元素**交过去的，由那个 shell 自己去解析。Python 这一层
    没有任何字符串拼接，所以不存在「引号没转义干净」这类注入 —— 把命令交给 shell
    执行本来就是它的用途，注入的边界应该在 shell 那边（也就是审批那边），不该在
    这里假装拦一下。
    """
    if platform.system() == "Windows":
        return [
            _powershell(), "-NoProfile", "-NonInteractive",
            "-Command", _UTF8_PREAMBLE + command,
        ]
    return ["/bin/sh", "-c", command]


def _powershell() -> str:
    """优先 pwsh（PowerShell 7），退回 Windows PowerShell 5.1。

    **找不到就抛，不回退到 cmd.exe。** 静默换一个语法不同的 shell，会让模型写的命令
    莫名其妙地失败，而失败原因从输出里完全看不出来 —— 这比直接报错难查得多。
    """
    for candidate in ("pwsh", "powershell"):
        if found := shutil.which(candidate):
            return found
    raise FileNotFoundError("找不到 pwsh 或 powershell，Windows 上的 shell 工具需要其中之一")


# shell 的参数模型。**和 Shell 住在同一个文件里**（schema 与行为同一个事实的两面），
# 而风险等级不在这里 —— 那是装配处的事（tools/builtin/__init__.py）。
class ShellArgs(ToolArgs):
    """shell 的参数。

    command 写成字符串而不是 argv 数组，是因为模型要用的能力（管道、重定向、多个
    命令串联）本来就得由 shell 解析 —— 拆成数组就等于把这些能力砍掉，而安全边界
    无论如何都落在审批那一道，不在这里（见本模块开头的注释）。

    timeout_seconds 的边界写成 ge/le，而不是让 handler 自己去夹：范围必须和 schema
    同源，模型才可能**提前**看到「最多 300 秒」，而不是事后收到一个被悄悄改过的值 ——
    而且工具调用每次都要过人工审批，撞一次参数错误就得再问一次人。
    """

    command: str = Field(
        min_length=1,
        description=f"要执行的命令，按 {shell_name()} 的语法写",
    )
    timeout_seconds: int = Field(
        default=TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="最多等这条命令多少秒。默认值只够 ls / git status 这种秒回的命令，"
                    "装依赖、跑测试这类慢命令要显式调大",
    )


class Shell:
    """在一个工作目录下执行命令。

    和 FileSystem 一样持有 workspace，但**用法完全不同**：FileSystem 拿它当沙箱边界
    （safe_path 会拒绝越界），这里只拿它当默认工作目录 —— 它拦不住任何东西。
    """

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def run(self, command: str, timeout_seconds: int = TIMEOUT_SECONDS) -> str:
        """执行一条命令，返回给模型的文本。

        **永远返回字符串，不抛异常。** 非零退出码是正常结果，不是工具的故障：grep
        没匹配到、`git diff --exit-code` 发现差异、pytest 有失败，都是模型需要读到
        并据此决策的信息。抛出去的话 agent.py 会把它记成工具故障，而模型恰恰拿不到
        退出码这个最关键的信号。

        timeout_seconds 的范围**不由这里管** —— 边界定义在本模块的 ShellArgs 上
        （ge/le），校验也只有那一道。这里再夹一次就成了第二份事实，早晚和 schema
        对不上，而且模型看不到被夹掉这件事。

        已知的不足：这样一来审计里 `tool_result.status` 永远只可能是 ok，从日志上
        看不出「命令失败了」。要区分得先给工具一个自己上报 status 的口子，那是另
        一件事。
        """
        try:
            argv = shell_argv(command)
        except FileNotFoundError as exc:
            return f"无法启动 shell：{exc}"

        try:
            completed = subprocess.run(
                argv,
                cwd=self.workspace,
                # 命令向 stdin 要输入时会立刻读到 EOF，而不是把会话挂在这里。
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                # 输出里混进非 utf-8 字节时替换掉，而不是让整个工具调用炸掉。
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return (
                f"命令超过了 {timeout_seconds} 秒，已终止。\n"
                f"命令：{command}\n"
                f"确实慢的命令可以把 timeout_seconds 调大（上限 {MAX_TIMEOUT_SECONDS} 秒）"
                f"再试一次；但它本来就不结束的话，调大只是让人多等一会儿，先确认一下。\n"
                f"注意：只杀掉了直接子进程，它自己再拉起来的进程可能还在跑。"
            )
        except OSError as exc:
            # 工作目录不存在、shell 二进制起不来之类。这是环境问题、不是命令写错了，
            # 所以要说清楚 —— 否则模型会去改一条本来没问题的命令。
            return f"无法执行命令（环境问题，不是命令本身）：{type(exc).__name__}: {exc}"

        return _format(completed.returncode, _combine(completed.stdout, completed.stderr))


def _combine(stdout: str, stderr: str) -> str:
    """把两个流合成一份输出。

    **刻意不分开标注。** 模型对「命令输出」的心智模型就是终端里那一坨，终端里两个流
    本来也是交织的；切成「--- stdout ---」「--- stderr ---」两段只会让它变长，而
    「成没成」这个最重要的信号已经由退出码单独说了。
    """
    out, err = stdout or "", stderr or ""
    if out and err and not out.endswith("\n"):
        out += "\n"
    return out + err


def _format(returncode: int, output: str) -> str:
    """把退出码和输出拼成给模型的文本。

    **空输出必须显式说出来。** 一条成功的命令什么都不打印（mkdir、赋值）时，留给
    模型的是一个空字符串，它无从判断是"成功了"还是"工具坏了"。这和 agent.py 里
    「拿不到 usage 就宁可不写这几个键」是同一个意思：宁可多说一句。
    """
    body = _truncate(output.rstrip()) if output.strip() else "(无输出)"
    return f"退出码 {returncode}\n{body}"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超长输出取头尾两段。

    实现搬去了 tools/text.py —— 逐字相同的第三份要出现时（fetch_web），同一份事实就该
    只有一个来源。这里留的是**门面**：默认上限仍然是本工具自己的 MAX_OUTPUT_CHARS
    （每个工具的输出上限不一样，合并不了），名字也没变（test_shell.py 直接 import 它）。
    """
    return truncate(text, limit)
