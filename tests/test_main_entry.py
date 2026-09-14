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

文件末尾另有一组：**`--tui` 的配置预检**（`check_config`）。它和上面是同一个形状的
问题 —— "入口该在哪一刻说那句话"，而那一组还多一层：界面会接管终端，说过就没了。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime import paths, userconfig, version
from agent_runtime.runtime.composition import check_config, check_session_id
from agent_runtime.state.session import is_valid_session_id

# 仓库根 —— 它下面有 `agent_runtime/`。子进程一律用 `-m agent_runtime.main` 起，
# 而不是 `main.py` 的绝对路径：那是生产里真正的起法（见 `protocol/client.py` 的
# `RUNTIME_MODULE`），所以这里照着用就顺带把它钉住了。
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ARGV = [sys.executable, "-m", "agent_runtime.main"]


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

def run_main(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """起真入口。`env_extra` 里给 `None` 表示**删掉**那个变量（不是设成 "None"）。"""
    env = dict(os.environ)
    for name, value in (env_extra or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return subprocess.run(
        [*RUNTIME_ARGV, *args],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
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


# --- `--version` -----------------------------------------------------------
#
# 它存在的理由是具体的：这个程序是**一个压缩包发给别人的**，而人要核对"我装上新版没有"
# 只能靠它。在这之前只能比文件哈希，或者盯着报错文案变没变。


def test_the_version_comes_from_pyproject():
    """版本号的唯一来源是 `pyproject.toml`（发布用的那个），**不抄字面量**。

    抄一份就有两处，而漂掉的那一处没人核对 —— 命令行说是 A、欢迎屏写着 B，比"少一行字"
    坏得多。
    """
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert version.current() == data["project"]["version"]


def test_version_flag_works_even_with_a_config_that_cannot_be_read(workdir):
    """`--version` 跟配置**一点关系都没有**。

    一个连密钥都没有、配置甚至是坏的的人，也应该问得出"我装的是哪一版"。它由 argparse
    自己处理（打印完就 `sys.exit(0)`），所以排在工作区检查和配置检查全都之前。

    这里故意给一份**读不懂的**配置：真去读它就会以退出码 2 收场（`CatalogError` 那一档），
    所以"退出码 0 且说出了版本号"就是"压根没读配置"的证据。
    """
    broken = workdir / "broken.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")

    result = run_main("--version", env_extra={"AGENT_CONFIG_FILE": str(broken)})

    assert result.returncode == 0, result.stderr
    assert version.current() in result.stdout
    assert "Traceback" not in result.stderr


def test_the_version_stamp_wins_over_pyproject(monkeypatch, workdir):
    """产物里那份戳优先于 `pyproject.toml`。

    冻结出来的包里**没有** `pyproject.toml`（构建用的文件不发给用户），所以打包时会额外
    带一个戳。这个分支在源码目录里天然走不到（那边没有戳），于是只能把戳造出来验：
    不验的话，"产物能说出自己版本"这件事就完全落在构建脚本手里，而它一旦漏了，症状是
    `--version` 说"版本号读不出来"——一个看起来像"这个包就是这样"的症状。

    **戳造在一个假包目录里**（`monkeypatch` 掉 `paths.package_dir`），不往真的源码树里
    写东西：那条路一旦中途失败，就会在仓库里留下一个写着假版本号的文件，而它不在
    `.gitignore` 里 —— 之后每次 `--version` 都会一脸确信地报那个假号。
    """
    fake_package = workdir / "agent_runtime"
    fake_package.mkdir()
    (fake_package / version.STAMP_FILE_NAME).write_text("9.9.9-from-stamp", encoding="utf-8")

    monkeypatch.setattr(paths, "package_dir", lambda: fake_package)

    assert version.current() == "9.9.9-from-stamp"
    assert version.describe() == "tudouni 9.9.9-from-stamp"


# --- `--tui` 的配置预检：**必须在进界面之前** -------------------------------
#
# 这一组盯的是一个真实的新用户第一次运行：界面一起来就接管了终端的**备用屏幕缓冲区**
# （Textual 的 `run(inline=False)`，也就是默认），而备用屏**没有回滚缓冲**。子进程那句
# 配置报错于是变成"只有最后一屏看得见、开头永久丢失、在界面里滚不动"的一屏乱码 ——
# 因为它根本不是界面的内容，只是一段被界面盖住的终端输出。而界面对子进程的死一无所知，
# 就停在那儿什么都不干。
#
# 所以那一问挪到了父进程、挪到了进备用屏之前：那时候还是普通终端，话能打完整。


def test_check_config_passes_on_a_custom_route_alone(workdir, monkeypatch):
    """判据是"有没有一条能用的路由"，不是"有没有某把密钥"。

    这条和 `test_providers.py` 里那条端到端是一体两面：那边证明"只配自家网关真能跑起来"，
    这边证明预检也认这同一条判据 —— 否则预检会把一个本来能跑的配置拦在门外。
    """
    cfg = workdir / "custom.json"
    cfg.write_text(json.dumps({"providers": {"my-gw": {
        "base_url": "https://gw.example/v1", "api_key": "sk-mine",
        "models": [{"id": "m"}]}}}), encoding="utf-8")
    monkeypatch.setenv(userconfig.FILE_ENV, str(cfg))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert check_config() is None


def test_tui_reports_a_broken_config_before_taking_over_the_screen(workdir):
    """**配置错时 `--tui` 在进界面之前就停下**，用普通终端把那句话说完。

    判据三样：退出码 2（"用户得先做点事"那一档）、话在 stderr 上、而且**一个字节都没往
    stdout 写** —— 界面一旦真的起来，它会往 stdout 写一屏转义序列。

    用一份 `{}` 的空配置（而不是去动真的 home）：`AGENT_CONFIG_FILE` 指过去之后
    `scaffold()` 会自己让开（那是"我自己管路径"的表示），所以这条测试**不写任何文件**。
    """
    empty = workdir / "no-routes.json"
    empty.write_text("{}", encoding="utf-8")

    result = run_main("--tui", env_extra={"AGENT_CONFIG_FILE": str(empty)})

    assert result.returncode == 2, result.stderr
    assert "模型层是抽象的" in result.stderr
    assert "providers" in result.stderr
    assert result.stdout == "", "界面不该被起来（起来就晚了：备用屏盖掉报错的开头）"
    assert "Traceback" not in result.stderr
