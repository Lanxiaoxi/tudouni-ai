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
    assert "agent_runtime.frontends.cli" in MODULES
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
_EXEMPT_FILES = {"main.py", "frontends/cli/__init__.py", "runtime/config.py"}

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

# --- 分层：内核 / runtime / protocol / frontends ---------------------------------
#
# 这三条测试盯的是 doc/TUI-design.md 决策 18 的那张依赖图。它们和上面那些的取向一样：
# **靠"记得"维持的边界一定会烂**，而烂掉的时候什么都不报 —— 只是某天出现一个 import 环，
# 或者 `--list` 莫名其妙地开始加载一个 TUI 框架。
#
# 第零期（抽 Runtime、搬目录）只加这三条；`protocol/` 和 `frontends/tui/` 还不存在，
# 所以第 2、3 条现在会在"目录不存在"时直接跳过 —— 等第一期、第二期把它们建出来，
# 这两条就自动开始生效，不需要再回来改测试。

# 内核：这些包不该认识 runtime / protocol / frontends 里的任何东西。
# 它们只该依赖工具契约（上面那条测试管的是 tools.* 的可见性，这条管的是**分层**）。
_KERNEL_PACKAGES = ("security", "agents", "state", "audit", "skills", "models")

# 分层前缀：内核不许 import 它们。
_LAYER_PREFIXES = ("agent_runtime.runtime", "agent_runtime.protocol", "agent_runtime.frontends")

# 前端不许 import runtime 内部（决策 18 —— 这是"前端只是协议的一个客户端"的全部内容）。
# 例外是 frontends 内部互相 import，那不算。
_FRONTEND_FORBIDDEN = ("agent_runtime.runtime",)

# 决策 18 的唯一例外：CLI 直连 runtime（决策 19）。**只有这一项**，而且有退出条件 ——
# 见 test_the_frontend_exemption_is_only_the_cli。
_FRONTEND_EXEMPT = {"frontends/cli/__init__.py"}

# textual 只准出现在这两个文件里：其余任何模块 import 它，都会让"老 CLI 不加载 TUI
# 框架"这条性质失效，而症状是 `--list` 在没装 textual 的环境里直接崩。
#
# 路径用 **`_relative()` 的形式**（相对包目录、posix 分隔符、**不带 `agent_runtime/`
# 前缀**）—— 和 `_FRONTEND_EXEMPT` 一致。写错的两种下场都很难看：带前缀或写反斜杠
# 时这条断言永远命中不了，也就是**测试静默失效**（实测踩过两次，第一次是反斜杠）。
_TEXTUAL_ALLOWED = {
    "frontends/tui/app.py",
    "frontends/tui/widgets.py",
}

_PKG_ROOT = Path(__file__).resolve().parent.parent


def _source_files(subdir: str) -> list[Path]:
    """某个子树下的全部 .py；目录不存在就返回空（分层还没建出来的期）。"""
    root = _PKG_ROOT / subdir
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _relative(path: Path) -> str:
    return path.relative_to(_PKG_ROOT).as_posix()


def test_protocol_never_imports_a_frontend():
    """`protocol/` 是跨进程契约，**不许认识任何一个前端**。

    反过来（前端 import 协议）是设计要的 —— 前端只讲协议。而协议认识前端就意味着
    "契约里掺进了某个界面的偏好"，那时候它就不再是三个前端能共用的东西了。

    这条比 `frontends` 那两条更严一点，因为方向搞反的代价是隐形的：
    `protocol/channels.py` 里 import 一个 TUI 的 widget 也能跑，只是从此
    Web 那一侧就依赖上了一个终端库。
    """
    for path in _source_files("protocol"):
        for lineno, module in _internal_imports(path):
            assert not module.startswith("agent_runtime.frontends"), (
                f"{_relative(path)}:{lineno} 从 {module} import 了东西 —— "
                f"协议不许认识任何前端（它要能被 Web 那一侧原样复用）"
            )


@pytest.mark.parametrize("package", _KERNEL_PACKAGES)
def test_kernel_does_not_know_about_layers(package):
    """内核不许 import runtime / protocol / frontends。

    这条是**单向**的：runtime 可以 import 内核（它就是来装配内核的），反过来不行。
    破掉的后果不是"坏了"，而是内核悄悄依赖上某个前端 —— 那时候 `--list`（一个只读
    会话文件的子命令）会连带把整个装配层和界面层拖进来。
    """
    root = Path(importlib.import_module(f"agent_runtime.{package}").__file__).parent
    for path in sorted(root.rglob("*.py")):
        for lineno, module in _internal_imports(path):
            assert not module.startswith(_LAYER_PREFIXES), (
                f"{_relative(path)}:{lineno} 从 {module} import 了东西 —— "
                f"{package}/ 是内核，只该依赖内核自己的东西（决策 18）"
            )


def test_frontends_do_not_import_runtime_internals():
    """前端只准讲协议，不许 import runtime 内部（决策 18）。

    这是"前端 = 协议的一个客户端"这句话的全部内容。它在**同一个仓库、同一个 venv**
    里只能靠这条测试守着 —— 一个 import 就能把它抹掉，而那正是 Web 前端能不能加进来
    的前提。

    **一个带日期的例外：`frontends/cli/__init__.py`。** 决策 18 和决策 19 在这里
    直接冲突：前者要求前端只讲协议，而后者明确让"老 CLI 直连 runtime、v1 不改"。
    两者不能同时成立，所以照决策 19 给 CLI 开一个**写清了理由和退出条件**的口子：

        例外在 `_FRONTEND_EXEMPT` 里，而那个集合只有一项。
        等 CLI 也改成协议客户端（决策 19 说的"以后"）时，那一项就该删掉 ——
        那时这条测试才第一次对**所有**前端生效。

    `frontends/tui/` 和 `frontends/web/` 现在还不存在，所以这条测试眼下护着的正是
    它们的将来：任何新前端从第一天起就必须只讲协议。
    """
    for path in _source_files("frontends"):
        if _relative(path) in _FRONTEND_EXEMPT:
            continue
        for lineno, module in _internal_imports(path):
            assert not module.startswith(_FRONTEND_FORBIDDEN), (
                f"{_relative(path)}:{lineno} 从 {module} import 了东西 —— "
                f"前端只能通过 protocol/ 说话（决策 18）"
            )


def test_the_frontend_exemption_is_only_the_cli():
    """例外只能有一个，而且必须真的是 CLI。

    这条挡的是"例外慢慢变多"：一个集合里多了第二项时没人会注意，而那时决策 18
    已经被削弱到没有意义 —— 而它的全部价值就在"每一个新前端都必须只讲协议"。
    """
    assert _FRONTEND_EXEMPT == {"frontends/cli/__init__.py"}, (
        "决策 18 的例外只允许 CLI 那一个（决策 19）。新增例外之前先读那两个决策 —— "
        "如果新前端也需要直连 runtime，那要改的是决策，不是这张表。"
    )
    for name in _FRONTEND_EXEMPT:
        assert (_PKG_ROOT / name).is_file(), f"{name} 不在了，例外该删掉"


def test_textual_stays_in_the_tui_client():
    """`textual` 只准出现在 TUI 的 app/widgets 里。

    它保证两件事：老 CLI（`uv run main.py`）和子进程（`--runtime-stdio`）都不加载
    一个 TUI 框架。代价是"这个依赖被关在 frontends/tui/ 里"这句话只能靠测试守 ——
    而它破掉时的症状很隐蔽（不是报错，是启动变慢），所以值得一条。

    TUI 还没写出来时这条也是空转。
    """
    for subdir in ("protocol", "frontends", "runtime"):
        for path in _source_files(subdir):
            if _relative(path) in _TEXTUAL_ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] != "textual", (
                        f"{_relative(path)}:{node.lineno} import 了 {name} —— "
                        f"textual 只准出现在 {sorted(_TEXTUAL_ALLOWED)}"
                    )
