import shutil
import sys
import uuid
from pathlib import Path

import pytest

# 让 `import fakes` 稳定可用。pytest 通常已经插入了 tests/，但顺序不保证，
# 显式加一次比赌它更省事。
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fakes import recording_registry  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_workspace(monkeypatch):
    """把 `Session.new()` 的默认工作区挪到一个空目录。

    **这是新加 AGENT.md 注入之后必须有的隔离**：`Session.new()` 会去工作区读那份文件，
    而"工作区"默认是 agent_runtime 包目录本身 —— 于是全套测试的 system 消息取决于
    **跑测试的那台机器上有没有人放了一份 AGENT.md**。那种失败长这样：本机全绿、CI
    全红，而报错只是某条断言里多了一段文本，没人会往"工作区文件"上想。

    ## 为什么自己建目录，不用 `tmp_path`

    和 `workdir` 同一条理由：`tmp_path` 走系统临时目录，在受限环境里直接
    `PermissionError`（实测过）。所以这里在 `tests/_tmp` 下建一个、用完就删。

    **两个默认值都要改**：`agents_md.WORKSPACE` 是模块默认（给没传工作区的
    `Session.new()` 用），`state.session.WORKSPACE` 是同一个名字在 `session` 模块里的
    出口 —— 只改一处的话，另一处仍然指着包目录，隔离就是假的。

    显式传 `workdir` 的那组测试（tests/test_agent_md.py）不受影响：参数优先。
    """
    from agent_runtime.state import agents_md, session as session_module

    path = TESTS_DIR / "_tmp" / f"workspace-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agents_md, "WORKSPACE", path)
    monkeypatch.setattr(session_module, "WORKSPACE", path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def workdir():
    """一个用完就删的干净目录。

    刻意不用 pytest 的 tmp_path：那套机制依赖系统临时目录，在受限环境里会直接
    抛 PermissionError。自己在 tests/_tmp 下建、用完删，行为更可预测，也让这套
    测试在哪都能跑。
    """
    path = TESTS_DIR / "_tmp" / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def registry():
    """只含 list_files 的注册表，handler 不碰磁盘、也不会真的执行。"""
    return recording_registry()[0]


@pytest.fixture
def spy_registry():
    """同上的注册表，外加"handler 被调用了几次"的证据。

    返回 (registry, calls)。权限与参数校验的测试靠 calls 证明"被拦住时 handler
    根本没跑"，而不是只看返回值 —— 只看返回值的话，一个"先执行再报错"的实现
    也能骗过测试。
    """
    return recording_registry()
