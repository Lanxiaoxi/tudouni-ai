"""入口层：`--session` 写错时要给人话，而不是一段 Python 栈。

`--session` 是用户直接敲进来的字符串，而它最终会被拿去拼文件名
（`.sessions/<id>.json`、`.logs/<id>.jsonl`）—— 所以它既必须受限（安全），
又必须**报得清楚**（可用）。这两件事分开测：

  * `check_session_id` 的单元断言（纯函数，不用起进程）；
  * 起真入口跑一遍 —— 这是唯一能证明"用户在终端上看到的是那句话、退出码是 2"
    的地方。一段从 store 里冒出来的 traceback 也能让"拒绝"这件事成立，但用户
    照着它改不了任何东西。

校验规则本身在 state/session.py，那里另有测试；这里只测**入口怎么用它**。

这条检查的实现在 `runtime/composition.py`（第零期从 `main.py` 搬过去的，并且
去掉了下划线）：它和 `resolve_session` 是一对，都住在装配层。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime.runtime.composition import check_session_id
from agent_runtime.state.session import is_valid_session_id

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


# --- 单元：翻译层 ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["../../evil", "a b", "a/b", "", "x" * 65])
def test_an_invalid_id_gets_a_readable_message(bad):
    message = check_session_id(bad)

    assert message is not None
    assert "非法的 --session" in message
    assert repr(bad) in message                  # 说清是哪个值
    assert "--list" in message                   # 并给出一条出路


@pytest.mark.parametrize("good", ["demo", "20260911-165811", "a_b-c", None])
def test_valid_ids_and_the_absent_parameter_pass(good):
    """None 是正常的（不传 --session）—— 校验不该把"没给"当成"给错了"。"""
    assert check_session_id(good) is None
    if good is not None:
        assert is_valid_session_id(good)         # 前提：这些确实是合法 id


def test_the_message_is_a_translation_not_a_second_rule():
    """翻译层不自己定义"什么算合法" —— 它必须和 state/session.py 同一份判据。

    这里只钉住方向：翻译层放行的，会话层也得放行。两份规则各写一半，早晚会出现
    "入口说合法、store 说非法"（或反过来），而后者正是这次要消灭的 traceback。
    """
    for candidate in ["demo", "a b", "a/b", "x" * 65]:
        assert (check_session_id(candidate) is None) == is_valid_session_id(candidate)


# --- 端到端：真入口、真退出码 ---------------------------------------------

def run_main(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MAIN_PY), *args],
        capture_output=True, encoding="utf-8", errors="replace",
    )


@pytest.mark.parametrize("bad", ["../../evil", "a b", "a/b"])
def test_bad_session_exits_2_without_a_traceback(bad):
    """用户在终端上看到的必须是那句话，而且退出码是 2（和缺密钥同一档）。

    不能出现 `Traceback` —— 那正是修复前的形态：异常从 store 的 `_path` 里冒出来，
    用户得到一整段栈，照着它改不了任何东西。
    """
    result = run_main("--session", bad)

    assert result.returncode == 2
    assert "非法的 --session" in result.stderr
    assert "Traceback" not in result.stderr
    assert "ValueError" not in result.stderr


def test_the_check_runs_before_any_config_is_read():
    """它排在读配置之前 —— 所以不需要密钥、也不会先打一幅横幅再报错。

    横幅属于"要开会话了"那条路径，而一个非法参数根本开不出会话。
    """
    result = run_main("--session", "a b")

    assert "a g e n t   r u n t i m e" not in result.stderr


def test_a_bad_session_is_caught_even_for_the_model_free_subcommands():
    """`--history` / `--audit` / `--list` 也走同一条检查。

    它们本来就排在配置检查之前（为的是没配密钥也能查），而 `--history` / `--audit`
    会把这个 id 交给 store —— 不先拦，用户拿到的还是那段栈。
    """
    for args in (["--history"], ["--audit"], ["--list"]):
        result = run_main("--session", "a/b", *args)
        assert result.returncode == 2, f"{args} 没拦住"
        assert "非法的 --session" in result.stderr
