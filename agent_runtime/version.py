"""这个程序是哪个版本 —— **一个事实，三处要用**（`--version`、欢迎屏、测试）。

## 为什么不写字面量

`"0.1.0"` 抄进代码就有两份（`pyproject.toml` 里那份才是发布用的那个），而漂掉的那一处
没人会去核对 —— 欢迎屏上少一行字，谁会发现？所以这里从 `pyproject.toml` 读。

（这段规矩原来只写在 TUI 的 `_version()` 里。`--version` 是第二个消费者，而"第二个
消费者"正是把一件事从某个前端里拎出来的时机 —— 否则第二个就会自己抄一份。）

## 冻结成可执行文件之后怎么办

源码目录里往上找 `pyproject.toml` 就行，但**产物里没有那个文件**：它是构建用的，
不该跟着可执行文件一起发给用户。所以打包时把版本号单独写成一个数据文件随包带上
（`_version.txt`，落点和那四类随码文件一样，都在 `paths.package_dir()` 下），
这里**优先读它**。

优先而不是并列：产物里万一两份都在，那个戳才是构建时钉下的真值。源码目录里没有戳，
自然落到 `pyproject.toml`。

## 这个模块是一个叶子

只 import 标准库和 `paths`，所以谁都能引它而不成环（和 `paths.py` 同一个理由）。
"""

from pathlib import Path

from agent_runtime import paths

# 打包时写出来的那份版本戳。**源码目录里通常没有它** —— 读不到不算错，见 `current()`。
STAMP_FILE_NAME = "_version.txt"


def current() -> str:
    """版本号。**读不到就返回空串，绝不抛。**

    一个连自己版本号都读不出来的安装（裁剪过的产物、只拷了部分文件的部署）不该因此
    崩掉 —— 它只影响一行字。这个方向是刻意的：显示版本是装饰，起不来是事故。
    """
    stamped = _stamped()
    if stamped:
        return stamped
    return _from_pyproject()


def describe() -> str:
    """给 `--version` 和欢迎屏用的那一行。**读不到版本号时也说得出话。**"""
    found = current()
    return f"tudouni {found}" if found else "tudouni（版本号读不出来）"


def _stamped() -> str:
    """打包时钉下的那个版本号（没有就是空串）。"""
    try:
        return (paths.package_dir() / STAMP_FILE_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _from_pyproject() -> str:
    """往上找 `pyproject.toml`，读 `project.version`。

    **往上找而不是数层数**：`frontends/tui/app.py` 到仓库根是几层，取决于文件放在
    哪一级 —— 而"正好三层"这种假设失效时，症状只是欢迎屏少一行字，没人会注意。
    （这段注释和它上面那条规矩都是从 TUI 的 `_version()` 搬过来的，那边踩过一次：
    第一版写成 `parents[3]`，实测拿到空串。）

    `tomllib` 在函数里 import：它是 3.11 才有的，而 `pyproject.toml` 声明支持 3.10
    （下界由 `@dataclass(slots=True)` 和运行时求值的 `X | None` 注解定的）。放在模块
    顶层会让 3.10 上**连 `import agent_runtime.version` 都失败**，那就不是"少一行字"
    了。整个函数包在 try 里，理由和 `current()` 一样。
    """
    try:
        import tomllib

        for parent in Path(__file__).resolve().parents:
            candidate = parent / "pyproject.toml"
            if candidate.is_file():
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                return str(data.get("project", {}).get("version", ""))
    except Exception:  # pragma: no cover - 只在裁剪过或异常的环境里走到
        return ""
    return ""


__all__ = ["STAMP_FILE_NAME", "current", "describe"]
