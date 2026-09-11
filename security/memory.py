"""人说过"这个工具别再问了"的那些工具名。

这是整套权限体系里**唯一可变**的一份状态，所以它值得单独一个模块和一段说明。

为什么可变的东西能进来：因为"t"是人在审批时做的决定，而它必须活得比进程长 ——
关掉再开，不该为了同一件事再问一遍。可变的权限状态本身是危险的，所以边界画得很小：

  * **只记工具名，不记参数。** 一条规则的作用范围必须一眼看得懂 —— "write_file
    任意路径"是一句话能说完的东西，"路径匹配某个前缀的 write_file"不是。
  * **只增不减。** 撤销要人去改 .tudouni.json，不给程序一条"悄悄放宽或收回"的路。
  * **不碰文件。** 落盘交给注入的 on_change，所以测试里它是纯内存的，而"记进哪个
    文件、什么格式"留在配置那一层。
  * **落盘失败不抛。** 记忆已经在内存里生效了，那一次审批也已经批准了；抛出去只会
    让这一轮工具调用被误判成"工具执行失败"（agent 的 except Exception 就在外面）。
    但要大声说出来 —— 和审计写入失败是同一条原则（README 的硬约束：不能变成静默
    失败）。区别在于后果：这里失效只是"重启后要重按一次 t"。
"""

import sys
from collections.abc import Callable, Iterable

# 落盘失败时的提示。写成模块常量是为了让测试能引用它，而不是抄一份字符串。
SAVE_FAILED_NOTE = "[权限] 记住的工具名没能写进配置文件"


class ApprovalMemory:
    """按工具名记住"人说过别再问"。

        memory = ApprovalMemory({"shell"}, on_change=save)
        "shell" in memory          # True
        memory.grant("write_file") # 新记住的，并且已经落盘
    """

    def __init__(
        self,
        initial: Iterable[str] = (),
        on_change: Callable[[frozenset[str]], None] | None = None,
    ):
        self._tools = set(initial)
        self._on_change = on_change

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def tools(self) -> frozenset[str]:
        """当前记住的全部工具名。

        返回不可变快照而不是内部集合：调用方（gate）拿它做前后对比，拿到的必须是
        那一刻的样子，不能是个还会变的视图。
        """
        return frozenset(self._tools)

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

    def _persist(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self.tools())
        except Exception as exc:
            # 内存里的记忆保留 —— 这次会话仍然免问，只是重启后会再问一次。
            print(
                f"{SAVE_FAILED_NOTE}（{type(exc).__name__}: {exc}）；"
                f"这次会话仍然不再询问，但重启后会重新问你。",
                file=sys.stderr,
            )
