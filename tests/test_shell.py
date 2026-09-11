"""shell 工具。

它和另外三个工具有一个根本区别：**不受工作区边界约束。** safe_path 拦得住
`../../evil.txt`，拦不住 `cd .. && rm -rf x` —— 后者走的是操作系统，不是 Python。

所以这组测试盯的不是"命令能不能跑"，而是几件更要紧的事：

  - 它是 HIGH 风险，默认策略下必然走审批，不可能被静默执行；
  - 命令是作为**一个 argv 元素**交给 shell 的，Python 这层不拼接 —— 也就不存在
    "引号没转义干净"这类注入；
  - 超时由模型设，但范围由 schema 钉死（上限才是"会不会无限期挂住"的答案）；
  - 失败是**返回**给模型的文本，不是抛异常：退出码是模型要读的信号，不是工具故障；
  - 中文输出不会因为在两个编码之间转一手而变成乱码（乱码不会让测试报错，只会让
    模型看到一堆问号，所以必须显式钉住）。

真跑命令的测试只用 `echo` / `exit` / `ls` 这种两个 shell 都认的东西；写法不一致的
地方（休眠）用一个按平台分支的小函数兜住。
"""

import platform

import pytest
from pydantic import ValidationError

from agent_runtime.agents import Agent
from agent_runtime.models.types import ModelResponse
from agent_runtime.security import Decision, PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.builtin import ShellArgs, create_tool_registry
from agent_runtime.tools.shell import (
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    Shell,
    _format,
    _truncate,
    shell_argv,
)
from agent_runtime.tools.tool import RiskLevel

from fakes import ScriptedModel, tool_call, usage


def sleep_command(seconds: int) -> str:
    """「睡一会儿」在两个 shell 里写法完全不同 —— 这种地方只能按平台分支。"""
    if platform.system() == "Windows":
        return f"Start-Sleep -Seconds {seconds}"
    return f"sleep {seconds}"


# --- 真跑一条命令 -------------------------------------------------------

def test_output_and_exit_code_are_both_reported(workdir):
    result = Shell(str(workdir)).run("echo hi")

    assert "退出码 0" in result
    assert "hi" in result


def test_nonzero_exit_is_returned_not_raised(workdir):
    """非零退出码是正常结果，不是工具故障。

    grep 没匹配到、pytest 有失败、`git diff --exit-code` 发现差异 —— 这些都是模型
    需要读到并据此决策的信息。抛异常的话 agent.py 会把它记成工具故障，而模型恰好
    拿不到退出码这个最关键的信号。
    """
    assert "退出码 3" in Shell(str(workdir)).run("exit 3")


def test_chinese_output_is_not_mangled(workdir):
    """编码在中间转了一手：shell 按 [Console]::OutputEncoding 写，Python 按 utf-8 读。

    不把前者的值钉死，换台机器（或者关掉系统的 UTF-8 选项）就是乱码。乱码不会让任何
    东西报错，所以只能靠这条断言挡着。
    """
    assert "中文测试：成功" in Shell(str(workdir)).run("echo 中文测试：成功")


def test_commands_start_in_the_workspace(workdir):
    """cwd 是工作区。

    注意这**不是**沙箱 —— 命令随时可以 `cd ..`。它只是默认起点，见 tools/shell.py
    的模块注释。
    """
    (workdir / "hello.txt").write_text("x", encoding="utf-8")

    assert "hello.txt" in Shell(str(workdir)).run("ls")


def test_timeout_is_reported_not_raised(workdir):
    """超时是**返回**的文本，不是异常，而且必须把出路说清楚。

    把超时压到 1 秒、让命令睡 5 秒，顺便证明这个值真的被用上了。这样测超时逻辑
    不用真的等 30 秒 —— 和 retry.py 把 sleep 做成可注入是同一条理由。
    """
    result = Shell(str(workdir)).run(sleep_command(5), timeout_seconds=1)

    assert "超过了 1 秒" in result
    assert "已终止" in result
    assert str(MAX_TIMEOUT_SECONDS) in result       # 得告诉模型还有调大的余地


# --- 超时这个旋钮 -------------------------------------------------------
#
# 超时交给模型设。**上限才是"会话会不会无限期挂住"的答案** —— 不是"要不要给旋钮"。

def test_timeout_range_is_bounded_by_the_args_model():
    """把超时交给模型之后，最坏情况必须由别的东西钉死。

    上限挡的是一条卡住的命令占住整个会话；下限挡的是把 timeout_seconds 填成 0 或
    负数 —— 那会让每条命令都立刻超时，而模型多半会把它读成"环境坏了"，然后去查一个
    根本不存在的问题。
    """
    assert ShellArgs(command="echo hi").timeout_seconds == TIMEOUT_SECONDS

    for bad in (0, -1, MAX_TIMEOUT_SECONDS + 1, 10 ** 6):
        with pytest.raises(ValidationError):
            ShellArgs(command="echo hi", timeout_seconds=bad)


def test_schema_shows_the_bounds_so_the_model_can_fill_them_in():
    """范围必须**提前**出现在 schema 里，而不是只在事后报错。

    每次工具调用都要过一次人工审批。模型填个 3600、撞一次参数错误、再填一次，就是
    白白多问用户一次 —— 所以 default / minimum / maximum 得让它一眼看到。
    """
    params = create_tool_registry(".").get("shell").parameters
    timeout = params["properties"]["timeout_seconds"]

    assert timeout["default"] == TIMEOUT_SECONDS
    assert timeout["minimum"] == MIN_TIMEOUT_SECONDS
    assert timeout["maximum"] == MAX_TIMEOUT_SECONDS


# --- 纯函数 -------------------------------------------------------------

def test_truncation_keeps_both_ends():
    """超长输出取头尾，**不是**只留开头。

    doc/guide.md 里那版 truncate() 是只留开头的。这里刻意反着来：命令输出的关键信息
    通常压在最后（报错、traceback、测试失败的汇总），只留开头会让模型看到一整屏正常
    日志，而真正的失败正好被切掉 —— 那比截断本身更危险。
    """
    text = "头" * 100 + "中" * (MAX_OUTPUT_CHARS * 2) + "尾" * 100
    result = _truncate(text)

    assert result.startswith("头" * 100)
    assert result.endswith("尾" * 100)
    assert "中间省略" in result
    assert len(result) < len(text)


def test_short_output_is_untouched():
    assert _truncate("短") == "短"


def test_empty_output_says_so_explicitly():
    """一条什么都不打印的成功命令（mkdir、赋值）不能只返回空字符串。

    模型拿到空串时无从判断是"成功了"还是"工具坏了" —— 和 agent.py 里「拿不到 usage
    就宁可不写那几个键」是同一条规矩：宁可多说一句。
    """
    assert _format(0, "") == "退出码 0\n(无输出)"
    assert _format(0, "   \n") == "退出码 0\n(无输出)"


def test_command_is_passed_to_the_shell_as_one_element():
    """命令整体交给 shell 解析，Python 这层不切开。

    这就是"没有注入"这句话的具体形状：`; rm -rf /` 不会变成一个独立的 argv 元素，
    也就不存在"某一段没转义干净"。它整体进到 shell 按 shell 的语义执行 —— 那正是
    我们要的，安全边界在审批那一道，不在这里假装拦一下。
    """
    argv = shell_argv("echo a; rm -rf /")

    assert argv[-1].endswith("echo a; rm -rf /")
    assert sum(1 for part in argv if "rm -rf" in part) == 1


# --- 安全属性 -----------------------------------------------------------

def test_shell_is_high_risk_so_the_default_policy_always_asks():
    """它不受工作区边界约束，所以绝不能进 auto_approve。

    main.py 装的是 `PermissionPolicy({RiskLevel.LOW})`。这条断言把两者钉在一起：
    只要 shell 还是 HIGH，"每一条命令都得问人"就自动成立，不需要额外的配置。
    """
    tool = create_tool_registry(".").get("shell")

    assert tool.risk is RiskLevel.HIGH
    assert PermissionPolicy({RiskLevel.LOW}).decide(
        tool, {"command": "rm -rf /"}
    ) is Decision.ASK


def test_denied_command_never_runs(workdir):
    """被拒绝时命令**一次都没执行**。

    证明方式用的是命令本身的输出特征：`exit 3` 真跑过的话，返回值里必然出现
    「退出码 3」。只看"返回值是不是拒绝文案"是不够的 —— 一个"先执行再报错"的实现
    也能让它看起来正确，而这里那句话根本不可能凭空出现。

    （test_permissions.py 里用的是"handler 被调用了几次"这种更强的证据；shell 的
    副作用不好跨平台复现，所以退回用输出特征。）
    """
    model = ScriptedModel([
        ModelResponse(content=None, tool_calls=[tool_call("shell", {"command": "exit 3"})],
                      usage=usage()),
        ModelResponse(content="完成", usage=usage()),
    ])
    agent = Agent(model, create_tool_registry(str(workdir)),
                  PermissionPolicy({RiskLevel.LOW}), asker=lambda t, a: False)
    session = Session.new("s")
    agent.run(session, "跑个命令")

    tool_result = [m for m in session.messages if m["role"] == "tool"][-1]["content"]
    assert "权限拒绝" in tool_result
    assert "退出码" not in tool_result
