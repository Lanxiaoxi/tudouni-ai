"""每个模块都要能被导入。

写这个测试的直接原因：cli.py 里一个引号写错，51 个测试全绿 —— 因为没有一个测试
碰过入口层。语法错误这种东西只有在真的 import 时才暴露，所以"能导入"本身值得
被断言一次。

这也是全套测试的冒烟测试：它花不到一秒，却能在改完任何文件之后立刻告诉你
"至少它还是个合法的 Python 模块"。
"""

import importlib
import pkgutil

import pytest

import agent_runtime


def _all_module_names() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages(agent_runtime.__path__, prefix="agent_runtime.")
    )


MODULES = _all_module_names()


def test_the_walk_actually_found_modules():
    """先证明收集器有效 —— 否则下面那个参数化测试可能一个用例都没跑。"""
    assert "agent_runtime.cli" in MODULES
    assert "agent_runtime.agents.retry" in MODULES
    assert len(MODULES) > 15


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)
