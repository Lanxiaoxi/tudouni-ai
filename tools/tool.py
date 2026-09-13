from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


JsonSchema = Mapping[str, Any]
ToolHandler = Callable[..., Any]


class InvalidArgsError(ValueError):
    """参数不合法，但**校验方不是 pydantic**。

    内置工具的参数校验由 args_model 做，失败抛 pydantic.ValidationError；外部工具
    （MCP）的 schema 权威在 server 那一侧 —— 我们不复制一份校验规则（见 Tool.parameters），
    所以它的"参数不合法"只能来自 server 回的一句话，而不是一次 model_validate。

    两者对 Agent 是同一件事：模型自己改得对。所以给它一个名字，让 `_run` 那条分岔
    不必去猜异常是从哪来的（见 agents/agent.py 里紧挨着 ValidationError 的那一支）。
    """


class RiskLevel(str, Enum):
    """工具的风险等级。

    混入 str 是为了让配置文件（JSON/YAML）里读进来的普通字符串 "low" 也能直接
    和枚举互相比对、放进同一个 set：str 在 MRO 里排在 Enum 之前，取到的是
    str.__hash__，所以两个方向的成员判断都成立。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolArgs(BaseModel):
    """所有工具参数模型的基类。

    参数模型一处定义、两用：

      1. 生成发给模型的 schema（**预防**：模型提前看到约束）
      2. 校验模型实际给出的参数（**兜底**：错了就明确告诉它哪个字段不对）

    两者同源，所以不会漂移。这也是**参数模型必须和它的 handler 住在同一个文件里**的
    原因 —— 它们是同一个事实的两面，分居两地就只剩"改了一边忘了另一边"这一种结局。

    extra="forbid" 是这里的关键：Pydantic 默认是 "ignore"，会把模型多传的参数
    静默丢弃 —— 那样「模型读错了 schema」这件事就被藏起来了。统一改成拒绝，
    并且它会在生成的 schema 里写出 additionalProperties: false，让模型提前看到。
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        """剥掉 schema 里的 description。

        Pydantic 会把模型类的 docstring 自动写成 schema 的 description，于是这些
        面向开发者的内部注释会被原样发给模型（而且每次请求都发）。工具对模型的
        说明由 Tool.description 负责，所以这里统一剥掉，避免噪声和内部注释外泄。
        只删顶层；字段级的 description（来自 Field(description=...)）保留。

        **$defs 里也要剥。** 嵌套的 args 模型（`list[TodoItem]` 那种）它的 docstring
        同样会被写成 $defs 条目的 description —— 只剥顶层就等于给"内部注释外泄"留了
        一个只有嵌套模型才走得到的后门，而它照样每次请求都发。
        """
        json_schema = handler(core_schema)
        json_schema.pop("description", None)
        for definition in json_schema.get("$defs", {}).values():
            definition.pop("description", None)
        return json_schema


@dataclass(frozen=True, slots=True)
class ToolResult:
    """比"一段文本"多一点的返回值：回灌给模型的文本 + 只有工具自己知道的审计字段。

    绝大多数工具直接返回字符串（`_run` 把它当文本用），不需要这个类型。需要它的只有
    那种**耗时里含等人的时间**的工具（ask_user）：它的 duration_ms 会落进 tool_result
    事件，而等人那一段在汇总里必须能减出来（见 cli.summarize_time）—— 否则"我看了 30 秒
    才回答"会显示成"这个工具花了 30 秒"，和当年审批那条一模一样。

    所以 audit 里放的是**工具唯一知道、而 Agent 推不出来**的事实，不是"随便什么元数据"：
    它已经能看见 tool 名、status、chars、duration_ms，别把那些再抄一遍。
    """

    text: str
    audit: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    # 必填且没有默认值：加新工具时无法"忘记"声明风险。若给默认值，漏声明的新工具
    # 会静默落到那一档上 —— 默认成 LOW 等于静默放行，是最坏的 fail-open。
    risk: RiskLevel

    # handler 有两种**调用约定**，取决于 schema 从哪来（见下面 args_model /
    # external_schema）：
    #
    #   内置工具    handler(**kwargs)       —— 参数已经过 pydantic，字段名保证是标识符
    #   外部工具    handler(args: Mapping)  —— **整个参数对象一次传进来**
    #
    # 后者不是风格选择，是 `**` 的硬限制：`handler(**args)` 要求每个参数名都是合法的
    # Python 标识符，而外部工具的 schema 是别人写的（MCP server 给的），`foo-bar`、
    # `foo.bar` 都合法。展开就等于凭空多出一条 JSON Schema 里看不见的约束 ——
    # 模型照着 schema 填，却在 Python 这一层撞墙。
    handler: ToolHandler

    # 参数的**唯一**来源，两者恰好给一个（见 __post_init__）：
    #
    #   args_model      内置工具。schema 和校验都从它推导 —— 手写第二份 schema 等于
    #                   让同一个事实有两个来源，早晚漂移（见 parameters 那段）。
    #   external_schema 外部工具（MCP）。schema 是 server 给的，**原样透传**，
    #                   也不在本地校验内容（权威在 server 那一侧）。
    #
    # 两个都排在 handler 之后：它们有默认值，而 handler 没有 —— dataclass 不允许
    # 无默认值的字段跟在有默认值的字段后面。所有构造点都用关键字（没有位置参数）。
    args_model: type[ToolArgs] | None = None
    external_schema: JsonSchema | None = None

    # 这个工具能不能和其他工具**同时**执行。声明在工具自己身上，而不是在
    # agent.py 里按名字写一张白名单 —— 理由和 risk 一样：知道"它有没有副作用"的
    # 人在注册处，不在循环里。
    #
    # 和 risk 不同，这里**带默认值**，而且那个不对称是有理由的：risk 缺省成 LOW
    # 是 fail-open（静默放行），而 parallel_safe 缺省成 False 是 fail-closed ——
    # 漏声明的后果只是"这条没并行"，慢一点，不会错。所以不需要强制每个工具都写它。
    #
    # 判定标准只有一条：**这个 handler 有没有副作用。** 有副作用（写文件、执行命令、
    # 改任何共享状态）就不能并行，因为同批调用之间没有任何隔离 —— 见 agent.py 里
    # 那段"为什么整批才能并行"。
    #
    # 另有一条注册期硬约束（见 ToolRegistry.register）：parallel_safe 的工具必须
    # 是 LOW。要并行就不能在批内弹审批 —— asker 走的是 stdin，两条审批同时问会
    # 互相抢输入，而"谁批准了哪一条"也没法回答了。
    parallel_safe: bool = False

    # 这个工具的 handler 会**阻塞在人的输入上**（目前的唯一一个：ask_user）。
    #
    # 为什么不能只靠"记得别给它标 parallel_safe"：ask_user 的风险是 LOW，而上面那条
    # 注册期校验只管"标了并行却不是 LOW" —— 于是"会问人"+"标了并行"这个组合**能**
    # 通过校验，跑到真实会话里才变成两条提问互相抢 stdin，而"谁回答了哪一条"也就没
    # 法回答了。这个字段把那句话变成启动时就炸的坏配置（和 risk 不给默认值是同一个
    # 手法：让"忘了"这件事不可能悄悄发生）。
    #
    # 默认 False 是 fail-closed：漏声明的后果只是"少一条启动期校验"，不是"多问了一次人"。
    interactive: bool = False

    def __post_init__(self) -> None:
        """"schema 从哪来"必须有且只有一个答案。

        两份来源会漂移，而漂移的形态最难查：模型看到的是 A，执行时按 B 校验。
        和 args_model 那段"手写第二份 schema"是同一条理由，所以这条约束钉在最里面
        （构造时）而不是注册处 —— 测试里临时造的 Tool 也要受它约束。
        """
        if (self.args_model is None) == (self.external_schema is None):
            given = "两个都给了" if self.args_model is not None else "两个都没给"
            raise ValueError(
                f"工具 {self.name} 必须恰好给出一个 schema 来源："
                f"args_model（内置工具，参数由 pydantic 校验）或 "
                f"external_schema（外部工具，schema 与校验都归 server）——现在是{given}。"
            )

    @property
    def parameters(self) -> JsonSchema:
        """参数 schema。

        内置工具从 args_model 推导，不再手写第二份：手写的 schema 和校验规则是同一个
        事实的两个来源，早晚会漂移。这里让 args_model 成为唯一来源，schema 只是它的
        一种渲染结果。

        **外部工具原样返回 server 给的 schema**，一个字节都不改。把它"翻译"成 pydantic
        模型（create_model 那条路）会静默失真：`$ref` / `anyOf` /
        `additionalProperties` 表达不了，参数名不是合法标识符时更是直接造不出来。
        而 schema 是模型唯一的依据 —— 失真等于让模型照着一份不存在的契约去调用。
        """
        if self.external_schema is not None:
            return dict(self.external_schema)

        assert self.args_model is not None, "__post_init__ 保证了两个来源恰好有一个"
        return self.args_model.model_json_schema()

    def to_openai_schema(self) -> JsonSchema:
        """转换成 OpenAI-compatible API 使用的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }

    def to_schema(self, format: str = "openai") -> JsonSchema:
        """根据格式转换为对应 schema。"""
        if format == "openai":
            return self.to_openai_schema()
        else:
            raise ValueError(f"Unknown format: {format}")

    def execute(self, args: Mapping[str, Any]) -> Any:
        """校验参数后再执行。

        校验失败会抛 pydantic.ValidationError（内置工具），由调用方决定怎么反馈给模型。

        **外部工具不在这里校验内容。** 它的 schema 权威是 server：本地复制一份规则出来
        就是两份会漂移的事实，而且本地过得了、server 那边照样可以拒。所以这里只做一个
        形状检查（参数得是个 JSON 对象），内容原样送进 handler —— server 拒了就是 server
        说的话，由 tools/mcp.py 翻译成 InvalidArgsError。
        """
        if self.args_model is None:
            if not isinstance(args, Mapping):
                raise InvalidArgsError(
                    f"参数必须是一个 JSON 对象，实际是 {type(args).__name__}"
                )
            # 转成普通 dict 再交出去：handler 拿到的不该是一个还带着上游引用的视图，
            # 而 server 那边要的本来就是可序列化的 JSON。
            return self.handler(dict(args))

        validated = self.args_model.model_validate(args)
        return self.handler(**validated.model_dump())


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        # 注册之后**仍然需要被别处读**的协作方（目前只有技能那一块状态）。
        #
        # 它为什么不能像 questioner / web_search 那样只活在 handler 里：技能的"写"和"读"
        # 是两条独立装配的路 —— 写由 SkillBoard（工具调用）负责，读由载荷尾部那段渲染
        # （每轮拼一次）负责，而两边必须看到**同一个** board。让调用方（main.py）自己
        # 再造一个的话，那个副本会带着另一个 loader，于是"技能加载成功了、却永远不出现在
        # 载荷里"——一个既没有异常、也没有审计痕迹的状态（tests/test_skills.py 里那条
        # test_the_note_never_enters_session_messages 就是盯着它的）。
        #
        # 所以谁造的谁留着：注册表拿着它，调用方从注册表上取回同一个对象。
        self.skills = None

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already exists: {tool.name}")

        # 并行的前提之一是"这一批不弹审批"（asker 走 stdin，两条审批同时问会互相抢
        # 输入，而"谁批准了哪一条"也就没法回答了）。LOW 是"默认不用问人"的那一档，
        # 所以把这条约束钉在注册处：坏组合在**启动时**就炸，而不是任务跑到一半才
        # 出怪事 —— 入口本来就会把注册表打印一遍，报错的位置正好是看得见的地方。
        #
        # 它是**必要条件，不是充分条件**：LOW 说的是"不用问人"，不是"没有副作用"。
        # 真正保证安全的是写这个工具的人读过 handler、确认它只读 —— 那件事没有
        # 任何类型能表达，只能靠声明。所以这条校验挡的是"标了并行却要弹审批"，
        # 挡不住"把一个会写的工具标成 LOW 又标成 parallel_safe"。
        #
        # 用 != 而不是 is not：RiskLevel 混了 str，所以配置里读进来的普通字符串
        # "low" 和 RiskLevel.LOW 相等（见上面那个枚举的注释）。
        if tool.parallel_safe and tool.risk != RiskLevel.LOW:
            raise ValueError(
                f"{tool.name} 声明了 parallel_safe，但风险等级是 {tool.risk.value}："
                f"能并行执行的工具必须是 low。\n"
                f"  并行的前提是批内不弹审批（审批走 stdin，两条同时问会互相抢输入）。"
            )

        # 和上面那条是同一个理由的另一半：会问人的工具**永远**不能并行，而且它跟风险
        # 等级无关 —— 上面那条拦不住它（LOW 是自动放行档，正好绕开）。两条合起来才
        # 说完"能并行的前提是批内没有任何人在说话"。
        if tool.interactive and tool.parallel_safe:
            raise ValueError(
                f"{tool.name} 会阻塞在人的输入上（interactive），不能声明 parallel_safe："
                f"两条提问同时问会互相抢输入，而「谁回答了哪一条」也就没法回答了。"
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")

        return self._tools[name]

    def unregister(self, name: str) -> Tool | None:
        """摘掉一个工具。**不在里面不算错**（返回 None），理由见下。

        它存在的唯一理由是 MCP 的 `/mcp unload`：外部工具是**运行中挂上来的**，
        所以必须也能摘掉。内置工具没有任何一条路会调它。

        为什么"不在里面"返回 None 而不是抛：这条路的调用方是卸载流程，而它的
        幂等性是有意义的 —— "把这个 server 的工具摘掉"重复一次不该炸（第一次
        unload 之后又收到一条 unload，或者两个工具名指向同一批）。真正需要报错的
        情况（卸载一个不存在的 server）由上游按 server 名判，那里的报错能说出
        "是哪个 server"，而这里只能说"是哪个工具"。
        """
        return self._tools.pop(name, None)

    def unregister_prefix(self, prefix: str) -> list[Tool]:
        """摘掉所有以 `prefix` 开头的工具，返回被摘掉的那些。

        MCP 的工具名是 `mcp__<server>__<工具>`（见 tools/mcp.py 的 NAME_PREFIX），
        所以按前缀摘正好等于"把一个 server 的工具全摘掉"，**而且不需要另存一份
        名单**：名单会漂（server 中途换了工具），而名字就在这里。

        返回 Tool 对象（不是名字）是有用的：调用方要把它们从 trust group 的映射里
        一起清掉（`runtime.composition.McpHost`），而那一步要的正是这些名字。
        """
        names = [name for name in self._tools if name.startswith(prefix)]
        return [self._tools.pop(name) for name in names]

    def all(self) -> list[Tool]:
        """按注册顺序返回所有工具。

        有这个方法，入口就不必去碰 _tools —— 私有属性被外部读一次，就等于把
        "内部结构"变成了事实上的公开接口。
        """
        return list(self._tools.values())

    def schemas(self, format: str = "openai") -> list[dict]:
        """返回所有工具的 schema 列表。"""
        return [tool.to_schema(format) for tool in self._tools.values()]