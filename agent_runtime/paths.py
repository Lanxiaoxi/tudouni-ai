"""路径：**这个程序把东西放在哪**，一份事实，一处可查。

## 它为什么必须存在（一段可以被说清的历史）

在这之前，"根目录在哪"有**三份各自独立的算法**：

  1. `runtime/config.py` —— `Path(__file__).parent.parent.parent / "agent_runtime"`，
     也就是"上跳三层再拼一个写死的目录名"；
  2. `state/catalog.py` —— `Path(__file__).parent.parent`（从源码位置推），
     它顶上还留着一句注释解释"路径在这里算，不 import runtime.config"；
  3. `runtime/composition.py` 的 `project_dir()` —— 又是一份 `__file__` 推算。

三份里第 1 份**已经坏了**：仓库目录上传时从 `agent_runtime` 改名成了 `tudouni-ai`，
而那个字面量没跟着改，于是 `ENV_FILE` 指向一个不存在的树 —— `.env` 一次都没被读到，
`permissions.json` 被 `save_approvals` 在一个凭空的目录里创建出来。**没有任何一行输出
会变**，因为"文件不存在"在这条链上每一处都是合法状态（`.env` 缺了不算错误、
`permissions.json` 缺了就用默认）。这正是这个项目一直在防的那种失败：静默、
且看起来完全正常。

一份事实修不掉粗心，但它把"改名"这类事故的影响面从三处收成一处 —— 而这一处**不含
任何写死的目录名**（`package_dir()` 从 `__file__` 推）。

## 为什么是顶层的叶子模块

它**只 import 标准库**，谁都能引它而不成环。这一点是被依赖方向逼出来的：

    runtime.config → state.catalog → paths
    skills.loader  → paths
    tools.builtin.filesystem → skills → paths

`state.catalog` 刻意不 import `runtime.config`（那会成环，它自己的注释写着），所以
"路径"这份知识不能住在 `runtime/` 里。顶层叶子模块是这个项目已有的形状 ——
`process.py` 就是一个（被 `tools/mcp.py` 和 `runtime/composition.py` 同时引用）。

`RUNTIME_DIR_NAME` 从 `skills/loader.py` 搬到这里。它当初落在那儿是**没有更好的地方**
（那份注释原话："skills 谁也不依赖，是这条链上唯一能安全承载布局常量的地方"），
而现在有了。`skills` 那边留着再出口一次，因为它在测试和别的模块里被引用得太多。

## 三个根，回答三个不同的问题

| 函数 | 问题 | 装成命令之后 |
|---|---|---|
| `package_dir()` | **代码在哪**（`prompts/`、随仓库带的 ripgrep、models 模板） | site-packages 里，只读 |
| `user_config_dir()` | **这台机器的配置在哪**（路由、密钥、MCP、个人技能） | `~/.tudouni`，不变 |
| `workspace_dir()` | **这次在操作哪个工作区**（会话、审计、权限、AGENT.md） | 就是 cwd，每次运行都可能不同 |

混用它们中任意两个，**在源码目录里跑的时候都看不出来**（那时三者恰好重合），而装成
命令之后立刻分家。这也是这个模块最主要的用处：让那种重合不再是默认。

## 全部是函数，没有模块级常量

`Path.home()` 和 `Path.cwd()` 都是**进程状态**，在 import 那一刻求值等于把它们冻死：

  * 测试改 `HOME` / `USERPROFILE`、或者 `monkeypatch.chdir()` 之后读不到新值；
  * 更要紧的是 `workspace_dir()` 跟着 cwd 走之后，"哪个模块先被 import"会变成一个
    必须小心维持的顺序 —— 而它错掉的症状是"会话写到了上一个目录里"。

所以凡是从它们派生的路径都不许当模块常量存（`config.permission_file()` 就是为此从
常量改成函数的）。反过来，**`config.MCP_FILE` 照旧是常量**，因为它在用户级、不随 cwd
变 —— 那个区别本身就是一份有用的信息：看一眼是函数还是常量，就知道它属于哪一层。

调用一次的代价是一次 `Path` 拼接，可以忽略。
"""

from pathlib import Path

# 这个运行时的私有目录名。**用户级和工作区级共用同一个名字**（`~/.tudouni` 与
# `<工作区>/.tudouni`），因为它们是同一个程序的两层配置，不是两个东西 —— 名字不同
# 会让"这份文件该放哪一层"变成需要查文档的问题。
#
# 里面装什么由两边各自决定：用户级放路由与凭据（跨工作区独一份），工作区级放会话、
# 审计、权限、项目技能（每个工作区一份）。
RUNTIME_DIR_NAME = ".tudouni"

# 旧名字（单目录时代留下的）。含义一个字都没变，留着是因为测试和文档引用得太多。
TUDOUNI_DIR_NAME = RUNTIME_DIR_NAME


def package_dir() -> Path:
    """**代码在哪。** 这个包自己的目录。

    只有"随代码走的东西"该用它：`prompts/system.zh.md`、`tools/vendor/rg/` 那几个
    ripgrep 二进制、`config.example.json` 这类模板。它们和代码同版本、同生命周期，
    装成命令之后跟着进 site-packages，**只读**。

    **从 `__file__` 推，不拼任何目录名。** 那个写死的 `"agent_runtime"` 就是上面
    docstring 里说的那次事故 —— 目录改名之后它指向一个不存在的树，而且完全没有症状。
    这个文件在包根上，所以一层 `parent` 就到。
    """
    return Path(__file__).resolve().parent


def user_config_dir() -> Path:
    """**这台机器的配置在哪。** `~/.tudouni`。

    跨工作区独一份的东西住这里：模型路由与密钥、`mcp.json`、个人技能。判据是一句话
    ——"换个目录干活，这件事会不会变"。密钥不会变，所以它在这一层；会话会变，
    所以它不在。

    `mcp.json` 早就在这儿了，理由比"配置该放哪"更硬：那个文件里的 `command` 是
    **启动时就要执行的代码**，而工作区级的位置意味着"clone 一个仓库就自动执行任意
    命令"。见 `runtime/config.py` 里 `MCP_FILE` 上面那一段。

    **不创建它。** 这个函数只回答"在哪"，建目录是写方的责任（`save_approvals` 里
    那句 `mkdir(parents=True)` 就是先例）—— 一个查询函数带着建目录的副作用，会让
    `--list` 这种只读子命令也在别人的 home 里留下东西。
    """
    return Path.home() / RUNTIME_DIR_NAME


def workspace_dir() -> Path:
    """**这次在操作哪个工作区。** 就是**当前工作目录**。

    agent 的文件工具能碰的范围、会话与审计的落点、项目级技能与 `AGENT.md` 的位置，
    全都从这里长出来。

    ## 为什么必须是 cwd，而不是包目录

    它以前返回 `package_dir()`。那在"从源码目录里 `uv run main.py`"这一种用法下恰好
    对（两者重合），而装成命令之后就全错了：包在 site-packages 里，于是
    `read_file` 读不到用户的项目、会话全写进那个跟着 pip 走的目录、几个工作区共用
    同一份历史 —— 而其中最坏的一条是**写得进去**（有权限的话），因为那时症状不是报错，
    是"我的会话怎么串了"。

    cwd 也是同类工具的通行约定（`git`、`npm`、`cargo` 都这样），所以"在哪个目录里
    敲这条命令，就在操作哪个项目"不需要解释。

    ## 它附带一个安全后果，不是可选的

    文件工具的围栏跟着工作区走，所以 cwd 是 `~` 时，一次 `uv run main.py` 就等于把
    整个 home 交出去。判据在 `unsafe_workspace()`，入口必须在装配之前问它一次
    （`composition.check_workspace()` 负责把它翻译成一句人话）。

    ## 不 resolve()

    `Path.cwd()` 已经是绝对路径。刻意不再 `.resolve()`：符号链接指的目录**就是用户
    敲命令时所在的那个**，替他解成真实路径会让 `--audit` 里的路径和他眼里的不一样，
    而 `safe_path` 那道围栏两侧用的是同一个值，所以不解也不会开口子。

    唯一的权威在这里。`state/agents_md.py` 的 `WORKSPACE`、`state/session.py` 的
    `WORKSPACE` 都只是"没人显式传时的默认值"，装配层（`composition.project_dir`）
    会把真值显式传下去 —— 那两处留着是为了可测（见它们各自的注释）。
    """
    return Path.cwd()


def workspace_runtime_dir() -> Path:
    """`<工作区>/.tudouni` —— 这个工作区的运行期私有数据。

    会话、审计日志、后台任务的输出、权限策略都在它下面。**整个目录对 agent 是只读的**
    （`tools/builtin/filesystem.py` 的 `CONTROL_PLANE` 点名了它）：能写它就等于能给
    自己加免审批规则，而那件事在审计里什么都看不出来。
    """
    return workspace_dir() / RUNTIME_DIR_NAME


# --- 工作区安不安全 -------------------------------------------------------------
#
# 三个原因码。**它们是机器认的标识，给人看的话在 `composition.check_workspace()`** ——
# 和 `Notice.code` / `Notice.text` 的分工完全一样：判据属于这一层（它认识文件系统的
# 布局），措辞属于入口层（它知道该怎么跟用户说）。

# cwd 就是 home 本身。
UNSAFE_HOME = "home"
# cwd 是某个文件系统的根（`/`、`C:\`）。
UNSAFE_ROOT = "root"
# cwd 是 home 的上层（`/home`、`/Users`、`C:\Users`）—— 那不只是把自己的 home 交出去，
# 是把**这台机器上每个人的**都交出去。
UNSAFE_ABOVE_HOME = "above-home"


def unsafe_workspace(path: Path | None = None) -> str:
    """这个目录能不能当工作区？返回原因码，**能就返回空串**。

    ## 为什么只挡这三种

    工作区的围栏是"不许出去"，所以工作区选得越大，围栏就越没有意义 —— 而这三种是
    "大得没有意义"的全部形态。除此之外**一律放行**：一个空目录、一个没有 `.git` 的
    目录、`/tmp` 下随手建的目录，都是完全正常的用法，为它们加一道"这里看起来不像项目"
    的猜测只会逼人绕过整个机制。

    这也是"不猜"的另一半：拒绝的判据必须是**说得出口的事实**（这就是你的 home），
    不是启发式（这里没有 pyproject.toml）。

    ## 顺序有意

    先问 `home`：一个把 home 当 cwd 的人要的提示是"cd 进一个项目目录"，而如果 home
    恰好是 `/`（容器里 root 用户的常见配置），那句"这是你的 home"比"这是根目录"
    更贴近他刚才做的事。
    """
    here = workspace_dir() if path is None else Path(path)
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        # 算不出 home（没有 HOME、也没有 passwd 条目）。那就只剩根目录那一条可判 ——
        # **不因此放行全部**：`/` 的危险和 home 在不在没有关系。
        home = None

    if home is not None and here == home:
        return UNSAFE_HOME
    if here.parent == here:
        # 根目录的判据是"它的父目录就是它自己"。**比字符串比 `/` 可靠**：Windows 上
        # 根是 `C:\`、UNC 路径是 `\\server\share\`，写死任何一个都会漏。
        return UNSAFE_ROOT
    if home is not None and here in home.parents:
        return UNSAFE_ABOVE_HOME
    return ""


__all__ = [
    "RUNTIME_DIR_NAME",
    "TUDOUNI_DIR_NAME",
    "UNSAFE_ABOVE_HOME",
    "UNSAFE_HOME",
    "UNSAFE_ROOT",
    "package_dir",
    "unsafe_workspace",
    "user_config_dir",
    "workspace_dir",
    "workspace_runtime_dir",
]
