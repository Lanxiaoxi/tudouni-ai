"""每个模块都要能被导入。

写这个测试的直接原因：cli.py 里一个引号写错，51 个测试全绿 —— 因为没有一个测试
碰过入口层。语法错误这种东西只有在真的 import 时才暴露，所以"能导入"本身值得
被断言一次。

这也是全套测试的冒烟测试：它花不到一秒，却能在改完任何文件之后立刻告诉你
"至少它还是个合法的 Python 模块"。
"""

import importlib
import ast
import pkgutil
from pathlib import Path

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
    assert "agent_runtime.skills.loader" in MODULES
    assert len(MODULES) > 15


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_the_skills_package_imports_no_internal_module():
    """**这条测试盯的是架构，不是语法。**

    `skills/` 能独立成包（而不是夹在 tools/ 里）的全部依据就是它**不 import 任何内部
    模块**：它不认识 Tool / ToolRegistry / ToolResult，不 import state、security、config。
    顺序反过来的话就会出现 `skills → tools`，而 `tools/builtin.py` 又要 import skills
    来注册 load_skill —— 环一出现，README 里那句"依赖方向是单向的，无环"就成了假话，
    而且环不会被任何别的测试发现：它只是让将来某次改动莫名其妙地 import 失败。

    所以这条检查写成对源码的判断（AST），而不是靠"现在能 import 成功"—— 能 import 成功
    恰恰是环出现时的表现（Python 允许部分初始化的模块借这次机会先跑通）。
    """
    package = importlib.import_module("agent_runtime.skills")
    root = Path(package.__file__).parent

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                # 包内的相对 import（`from .loader import ...`）是允许的：那是**包自己
                # 内部**的分层，不是对外的依赖。要拦的是 `from agent_runtime.tools ...`
                # 和 `from agent_runtime import tools`（node.module == "agent_runtime"）。
                if node.level:
                    continue
            else:
                continue
            for name in names:
                assert name != "agent_runtime" and not name.startswith("agent_runtime."), (
                    f"{path.name} 里出现了对内部模块的依赖：{name} —— "
                    f"skills/ 一旦 import 内部模块就可能和 tools/ 成环"
                )
