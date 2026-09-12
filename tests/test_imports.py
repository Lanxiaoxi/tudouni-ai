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
import subprocess
import sys
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
    顺序反过来的话就会出现 `skills → tools`，而 `tools/builtin/__init__.py` 又要 import skills
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


# --- tools/ 的边界：除入口外，只准 import 契约 ---------------------------------
#
# tools/ 里现在是三类东西：契约（tool.py / text.py）、内置工具（builtin/ 包，一个工具
# 一个模块）、外部来源（mcp.py）。这条测试钉的是**它们的可见性**：一个权限策略、一个
# Agent 循环需要知道的只有"工具长什么样"，不该顺手把 httpx、技能包、某个具体工具拖进来。

# 只有 `tools.tool` 是稳定契约；`tools`（包出口）导出的也全是契约里的名字。
_CONTRACT_MODULES = {"agent_runtime.tools", "agent_runtime.tools.tool"}

# 入口层（装配）和 config 是仅有的例外，各自有明确理由：
#   * main.py / cli.py —— 它们就是装配处，必须能拿到具体工具和 create_tool_registry；
#   * config.py —— 它要解析 mcp.json 的形状，那份形状知识住在 tools/mcp.py（见那里的
#     说明：反向的 tools → config 是禁止的，所以解析只能由 config 这一侧调过去）。
_EXEMPT_FILES = {"main.py", "cli.py", "config.py"}

# 盯住的包：这些是"运行时内核"，它们的 import 面应该只有契约。
_PACKAGES = ("security", "agents", "state", "audit", "skills", "models")


def _internal_imports(path: Path) -> list[tuple[int, str]]:
    """这个文件里所有对 `agent_runtime.*` 的绝对 import（行号 + 模块名）。"""
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.lineno, node.module))
    return found


@pytest.mark.parametrize("package", _PACKAGES)
def test_only_the_tool_contract_is_imported_from_outside(package):
    """`security/` `agents/` 这些包只准通过 `tools.tool` 认识工具。

    为什么值得一条测试：这条边界以前是靠"记得"维持的，而它一旦破掉，症状是
    `security/policy.py` 里多了一个 import 就把 httpx 和整个技能包拖进每一次权限裁决
    —— 没人会注意到，因为**什么都没有坏**，只是加载变重、耦合变宽。
    """
    root = Path(importlib.import_module(f"agent_runtime.{package}").__file__).parent
    for path in sorted(root.rglob("*.py")):
        for lineno, module in _internal_imports(path):
            if not module.startswith("agent_runtime.tools"):
                continue
            assert module in _CONTRACT_MODULES, (
                f"{path.name}:{lineno} 从 {module} import 了东西 —— "
                f"{package}/ 只该依赖工具契约（agent_runtime.tools.tool），"
                f"具体工具与外部来源是装配处（main.py）的事"
            )


def test_the_exemptions_are_few_and_real():
    """例外必须真的存在 —— 一个指向已删除文件的例外会静默放宽这条规则。"""
    for name in _EXEMPT_FILES:
        assert (Path(__file__).resolve().parent.parent / name).is_file(), (
            f"{name} 已经不在了，_EXEMPT_FILES 里这一项该删掉"
        )


def test_the_contract_export_carries_no_baggage():
    """`import agent_runtime.tools.tool` 不该把 httpx / 技能包拖进来。

    这条盯的是"包出口"那个具体代价：`__init__.py` 里多一行 re-export 是看不见的，
    而它会让每一个只想拿一个 Tool 的人付加载费（实测过一次：httpx + skills 三个模块）。
    """
    script = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "import agent_runtime.tools.tool;"
        "print('httpx' in sys.modules, 'agent_runtime.skills' in sys.modules)"
    ) % (Path(__file__).resolve().parent.parent.parent,)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, encoding="utf-8",
    )

    assert result.stdout.strip() == "False False", (
        f"tools 的包出口把东西拖进来了：{result.stdout.strip()}（见 tools/__init__.py）"
    )
