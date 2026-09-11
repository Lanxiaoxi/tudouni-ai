"""工具调用的权限策略。

这一层只做「决定」，不做「询问」。

decide() 是纯函数：不读 stdin、不打印、不碰网络、不改自身状态。把 ASK 的处理
留给调用方，换来两件事：

  1. 可以直接单测 —— 不需要 mock 掉 input()，策略与交互不再纠缠；
  2. 同一份策略能同时服务 CLI 和将来的 Web 服务 —— 后者没有 stdin，
     一个内联 input() 的策略在那里根本无法使用。
"""

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from agent_runtime.tools.tool import RiskLevel, Tool


class Decision(str, Enum):
    """策略对一次工具调用的裁定。

    三个出口的分工：ALLOW 是"不必问"，DENY 是"问都不用问，答案已经知道了"，
    ASK 是"要问人"。
    """

    ALLOW = "allow"  # 直接执行，不必询问
    DENY = "deny"  # 直接拒绝，不必询问
    ASK = "ask"  # 需要征求用户


class PermissionPolicy:
    """按风险等级决定每次工具调用是否需要审批。

    auto_approve 里列出的等级直接放行，deny_tools 里点名的工具直接拒绝，
    其余一律返回 ASK。

    **两个名单先问拒绝。** 同一个工具同时出现在两边时，拒绝赢 —— 配置矛盾时唯一
    安全的读法是 fail-closed。这种矛盾在加载 .tudouni.json 时就会被拦下（见
    config.PermissionConfig），但策略本身不能因为别人少写了一道检查就变成放行。

    参数类型写成 Iterable[RiskLevel | str]，是因为等级常常来自配置文件
    （JSON/YAML 读出来是普通字符串 "low"）。RiskLevel 混入了 str，且
    __hash__ 取自 str.__hash__，所以字符串和枚举混在同一个 set 里也能正确命中，
    不需要在调用处做转换。
    """

    def __init__(
        self,
        auto_approve: Iterable[RiskLevel | str] = (),
        deny_tools: Iterable[str] = (),
    ):
        # 存成新的 set：一是查找 O(1)，二是避免外部集合后续被改动而悄悄影响策略。
        self.auto_approve: set[RiskLevel | str] = set(auto_approve)
        self.deny_tools: set[str] = set(deny_tools)

    def decide(self, tool: Tool, arguments: Mapping[str, Any]) -> Decision:
        """裁定一次工具调用，返回 Decision。

        arguments 现在没有被读取 —— 它是有意留在签名里的。风险等级是「每个工具
        一个」，粒度很粗：write_file 写 doc/notes.md 和写 tools/filesystem.py
        同为 MEDIUM，实际风险却天差地别。将来要做参数级判断（比如拒绝对 runtime
        自身源码的写入）时，这个签名不需要改动，所有调用处也不用动。
        """
        if tool.name in self.deny_tools:
            return Decision.DENY
        if tool.risk in self.auto_approve:
            return Decision.ALLOW
        return Decision.ASK
