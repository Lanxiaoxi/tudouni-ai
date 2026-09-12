"""人说过"别再问"的东西：工具名，或者命令前缀。

这是整套权限里**唯一可变**的一份状态，所以它值得单独一个模块和一段说明。

两种粒度，对应两种"别再问"：

  * 按**工具名** —— "write_file 别再问了"。粗，但一眼看得懂。
  * 按**命令前缀** —— "git add 开头的命令别再问了"（拆解与匹配在 security/commands.py）。
    shell 这种"一条命令一个样"的工具，只有这个粒度才有意义。

为什么可变的东西能进来：因为"t"是人在审批时做的决定，而它必须活得比进程长 —— 关掉
再开，不该为了同一件事再问一遍。可变的权限状态本身是危险的，所以边界画得很小：

  * **只记工具名或命令前缀，从不记参数。** 一条规则的作用范围必须一眼看得懂 ——
    "write_file 任意路径"是一句话能说完的，"路径匹配某个前缀的 write_file"不是；
    "git add 开头"是，"就是刚才那一条完整命令"不是（那等于每次都问）。
  * **只增不减。** 撤销要人去改 .tudouni.json，不给程序一条"悄悄放宽或收回"的路。
  * **不碰文件。** 落盘交给注入的 on_change，所以测试里它是纯内存的，而"记进哪个
    文件、什么格式"留在配置那一层。`label` 因此只是"写进哪儿"的显示名，给审批提示用，
    不是一条路径 —— 这也让 security/ 不必反过来 import config。
  * **落盘失败不抛。** 记忆已经在内存里生效了，那一次审批也已经批准了；抛出去只会
    让这一轮工具调用被误判成"工具执行失败"（agent 的 except Exception 就在外面）。
    但要大声说出来 —— 和审计写入失败是同一条原则（README 的硬约束：不能变成静默
    失败）。区别在于后果：这里失效只是"重启后要重按一次 t"。
"""

import sys
from collections.abc import Callable, Iterable

from agent_runtime.security.commands import Rule

# 落盘失败时的提示。写成模块常量是为了让测试能引用它，而不是抄一份字符串。
SAVE_FAILED_NOTE = "[权限] 记住的东西没能写进配置文件"

# 落盘回调：参数是"现在记住的全部"（工具名、命令前缀）。注入的实现决定写到哪。
Persist = Callable[[frozenset[str], frozenset[Rule]], None]


class ApprovalMemory:
    """按工具名或命令前缀记住"人说过别再问"。

        memory = ApprovalMemory({"shell"}, on_change=save, label=".tudouni.json")
        "shell" in memory                      # 工具名
        ("git", "add") in memory.prefixes()    # 命令前缀
    """

    def __init__(
        self,
        initial: Iterable[str] = (),
        on_change: Persist | None = None,
        prefixes: Iterable[Rule] = (),
        label: str = "配置文件",
    ):
        self._tools = set(initial)
        self._prefixes = {tuple(rule) for rule in prefixes}
        self._on_change = on_change
        # 只用于审批提示里那句"写进 …"。
        self.label = label

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def tools(self) -> frozenset[str]:
        """当前记住的工具名。

        返回不可变快照而不是内部集合：调用方（gate）拿它做前后对比，拿到的必须是
        那一刻的样子，不能是个还会变的视图。
        """
        return frozenset(self._tools)

    def prefixes(self) -> frozenset[Rule]:
        """当前记住的命令前缀。同样是快照，理由同上。"""
        return frozenset(self._prefixes)

    def grant(self, tool_name: str) -> bool:
        """记住"这个工具以后不用问了"，返回它是不是新记住的。

        返回值有用：重复按 t 不该再说一次"已记住"。落盘只在真的记住新东西时发生，
        所以连按两次不会把文件重写两遍。
        """
        if tool_name in self._tools:
            return False

        self._tools.add(tool_name)
        self._persist()
        return True

    def grant_prefix(self, rule: Rule) -> bool:
        """记住"这个命令前缀以后不用问了"，返回它是不是新记住的。"""
        rule = tuple(rule)
        if rule in self._prefixes:
            return False

        self._prefixes.add(rule)
        self._persist()
        return True

    def grant_all(self, names: Iterable[str]) -> frozenset[str]:
        """一次记住**一组**工具名，返回这次真正新增的那些。

        目前只有一条路走到这里：审批时按 `a` 信任一整个 MCP server 的全部工具
        （见 security/asker.py 的 TrustGroup）。它必须单独一个方法，而不是让调用方
        循环 grant —— 每次 grant 都会落盘一次，一组 12 个工具就是 12 次重写同一个
        文件，而落盘是"人按了一次键"的副作用，本该只发生一次。

        返回值给审计用：gate 会把它并进"这次批准顺带记住了什么"（问前问后的快照差），
        所以 12 个名字会一个不少地落进那条 permission 事件里 —— "谁批的"必须看得见。
        """
        added = {name for name in names if name not in self._tools}
        if not added:
            return frozenset()

        self._tools |= added
        self._persist()
        return frozenset(added)

    def _persist(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self.tools(), self.prefixes())
        except Exception as exc:
            # 内存里的记忆保留 —— 这次会话仍然免问，只是重启后会再问一次。
            print(
                f"{SAVE_FAILED_NOTE}（{type(exc).__name__}: {exc}）；"
                f"这次会话仍然不再询问，但重启后会重新问你。",
                file=sys.stderr,
            )
