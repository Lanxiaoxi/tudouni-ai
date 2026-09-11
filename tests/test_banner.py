"""启动横幅。

这是项目里唯一一块纯粹的装饰代码，所以只盯两件事：**不能把程序搞崩**，以及
**不能污染 stdout**。

前者听着夸张，但非 ASCII 的控制台字符确实会在别人的机器上抛 UnicodeEncodeError ——
而且它只在别人的机器上出现，在开发机上一路正常。
"""

import subprocess
import sys
from pathlib import Path

from agent_runtime.cli import BANNER, print_banner


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
