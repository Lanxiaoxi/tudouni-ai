"""启动横幅。

这是项目里唯一一块纯粹的装饰代码，所以只盯两件事：**不能把程序搞崩**，以及
**不能污染 stdout**。

前者听着夸张，但非 ASCII 的控制台字符确实会在别人的机器上抛 UnicodeEncodeError ——
而且它只在别人的机器上出现，在开发机上一路正常。
"""

import os
import subprocess
import sys
from pathlib import Path

from agent_runtime.frontends.cli import BANNER, print_banner


MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


def test_banner_is_pure_ascii():
    """一个非 ASCII 字符都不能有。

    Windows 控制台在中文区域设置下是 cp936，框线字符（─│╭╯）和 emoji 要么直接抛
    UnicodeEncodeError、要么显示成乱码。表现出来就是"程序一启动就崩"，而原因在一个
    看起来完全无害的装饰图案里。
    """
    offenders = sorted({c for c in BANNER if ord(c) > 127})
    assert offenders == [], f"横幅里有非 ASCII 字符：{offenders!r}"


def test_banner_fits_an_80_column_terminal():
    """折行的图案只是一堆乱字符，而且折在哪一列取决于终端宽度，没法预测。"""
    assert max(len(line) for line in BANNER.splitlines()) <= 80


def test_banner_has_no_trailing_whitespace():
    """行尾不留空格。

    它不影响观感（看不见），但会让「这一行到底多宽」说不清，diff 里也全是噪声。
    """
    for line in BANNER.splitlines():
        assert line == line.rstrip(), f"行尾有多余空格：{line!r}"


def test_banner_goes_to_stderr_not_stdout(capsys):
    """stdout 只留给 agent 的产出。

    README 承诺 `uv run main.py > 对话.txt` 拿到的是干净的答案，而横幅是装饰 ——
    跑进那个文件里就是污染。
    """
    print_banner()
    captured = capsys.readouterr()

    assert captured.out == ""
    assert BANNER in captured.err


def test_model_free_subcommands_do_not_print_a_banner():
    """--list / --history / --audit 看不到横幅。

    这三条子命令特意排在配置检查之前，为的是「没配密钥也能查历史」；横幅属于"要开
    会话了"那条路径。这里直接跑真入口 —— 那是唯一能证明这个分工的地方。
    """
    result = subprocess.run(
        [sys.executable, str(MAIN_PY), "--list"],
        capture_output=True, encoding="utf-8", errors="replace",
    )

    assert result.returncode == 0
    assert BANNER.splitlines()[0] not in result.stderr


def test_the_two_streams_carry_the_two_kinds_of_text():
    """**第零期的验收测试**：装配搬进 `runtime/` 之后，两条流的分工必须一字不变。

    为什么不能靠"和一次捕获的基线逐字节比"：在 PowerShell 里 `2>&1 |` **不保证**
    跨流的相对顺序（两个流各有各的缓冲），所以那样比出来的 diff 大半是噪声 ——
    我第一版就上过这个当，差点把一次假阳性当成回归去改代码。

    能稳定断言的是**每条流里有什么**，而那正好就是 README 那句承诺的全部内容：

      * stdout = 会话身份 + 已注册工具清单 + 提示语。它是"用户回头要读的东西"，
        所以 `> 对话.txt` 应该拿到一份能读懂的文件；
      * stderr = 横幅、`[权限]` / `[技能]` / `[任务]` / `[MCP]` / `[联网]` /
        `[上下文]` 那些**关于这次运行的说明**，以及审计路径。

    注意"审计日志写到"**故意留在 stderr**：它不是对话，是老 CLI 一直以来的诊断行。
    第零期把它从 `main.py` 的一条 print 变成 `Runtime.audit_log_line()`，但没动它的流。
    """
    env = dict(os.environ)
    # 一个假密钥就够：这条测试问的是"谁打到哪里"，不是"能不能真的调模型"。
    env["DEEPSEEK_API_KEY"] = "sk-not-used"
    result = subprocess.run(
        [sys.executable, str(MAIN_PY), "--session", "stream-check"],
        input="exit\n", capture_output=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(MAIN_PY.parent),
    )

    assert result.returncode == 0, result.stderr

    # --- stdout：会话身份 + 工具清单 ---
    assert "新会话 'stream-check'" in result.stdout
    assert "已注册工具:" in result.stdout
    assert "read_file" in result.stdout and "风险=low" in result.stdout
    # 装配那些说明**不准**跑到 stdout 上。
    for leaked in ("[权限]", "[技能]", "[任务]", "[MCP]", "[联网]", "[上下文]",
                   "审计日志写到", "a g e n t   r u n t i m e"):
        assert leaked not in result.stdout, f"{leaked} 漏进 stdout 了"

    # --- stderr：说明 + 横幅 + 审计路径 ---
    assert "a g e n t   r u n t i m e" in result.stderr      # 横幅
    assert "[权限] 按等级自动放行" in result.stderr
    assert "审计日志写到" in result.stderr
    # 工具清单**不准**跑到 stderr 上（它以前就走 stdout，这是历史契约）。
    assert "已注册工具:" not in result.stderr
