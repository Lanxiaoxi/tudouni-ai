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
