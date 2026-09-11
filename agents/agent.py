import json
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from agent_runtime.agents.retry import Attempt, call_with_retry
from agent_runtime.audit import event
from agent_runtime.models.base import ChatModel
from agent_runtime.models.types import ModelFatalError, TokenUsage
from agent_runtime.security.asker import ApprovalAsker
from agent_runtime.security.gate import check_permission
from agent_runtime.security.memory import ApprovalMemory
from agent_runtime.security.policy import PermissionPolicy
from agent_runtime.state import Session
from agent_runtime.tools.tool import Tool, ToolRegistry

if TYPE_CHECKING:
    from agent_runtime.models.types import ModelResponse


# debug 输出里每条内容的最大预览长度。工具结果（读文件、列目录）可能很长，
# 全打出来会把终端刷掉。
DEBUG_PREVIEW_LIMIT = 200

# 落盘回调：Agent 在「messages 一致」的时刻调用它，通知外部现在可以安全保存了。
# 存到哪、什么格式、要不要存，都由注入进来的实现决定。
Checkpoint = Callable[[Session], None]

# 审计事件回调：Agent 报告「发生了什么」，注入的实现决定记到哪、什么格式。
EventSink = Callable[[dict[str, Any]], None]

# 审计事件里参数预览的最大长度。参数可能很长（write_file 的 content），
# 也可能含敏感内容，所以审计日志只留预览、从不记全文。
AUDIT_PREVIEW_LIMIT = 200

# 工具在正常工作流程里会抛的异常 —— 它们代表"这次调用没成功"，不是 bug。
# 其余异常一律当疑似 bug：给模型的消息照旧，但 stderr 要留下完整 traceback。
_TOOL_LEVEL_ERRORS = (
    FileNotFoundError,
    PermissionError,
    NotADirectoryError,
    IsADirectoryError,
    json.JSONDecodeError,
)


def _is_fatal(exc: BaseException) -> bool:
    """区分"重试也没用"和"重试用完了"。

    两者在审计里必须是不同的 stop_reason：前者要用户改配置，后者可以直接再试。
    """
    return isinstance(exc, ModelFatalError)


class StepLimitExceeded(RuntimeError):
    """步数预算用尽：任务既没失败，也没收尾。

    单独一个类型、而不是返回一句"超过最大步数"的文本，理由是返回值和真正的答案
    在同一条出口上：cli.py 会把它 print 到 **stdout**，于是 `> 对话.txt` 拿到的东西
    里混进一句"已停止"，而用户从输出里分不出"答完了"和"被砍断了"。

    它也不该混进 ModelError：处置方式完全不同 —— 会话是完好的、之前的工作都还在，
    用户接着跑就行，不需要重试、也不需要改配置。

    抛之前 run_finished 和 checkpoint 都已经发生（见 run 末尾），所以日志里能看出
    是哪一轮撞的墙，会话也能原样续上。消息里把"可以接着跑"说出来，是因为那是它和
    其它失败最大的区别。
    """

    def __init__(self, step: int, tools: list[str] | None = None):
        detail = f"，最后一步仍在调用 {', '.join(tools)}" if tools else ""
        super().__init__(
            f"已达最大步数 {step}，任务未收尾{detail}。会话是完好的，可以直接接着跑。"
        )
        self.step = step
        self.tools = list(tools or ())


class Agent:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        policy: PermissionPolicy,
        asker: ApprovalAsker | None = None,
        memory: ApprovalMemory | None = None,
        on_checkpoint: Checkpoint | None = None,
        on_event: EventSink | None = None,
        debug: bool = False,
    ):
        # 前三个是【能力】：每个应用构造一次，长期复用、可以跨会话共享。
        self.model = model
        self.tools = tools
        # policy 必填、没有默认值：这样就不可能出现「忘了配策略，于是权限检查静默
        # 消失」的 Agent。默认成「不检查」是最坏的 fail-open。
        self.policy = policy

        # 下面两个是【注入的协作方】，都不属于 Agent 自己。
        #
        # asker 可以为空：若策略把所有等级都放进 auto_approve，ASK 永远不会发生，
        # 就不必塞一个用不到的询问函数。但真要 ASK 而没有 asker 时按拒绝处理。
        self.asker = asker
        # memory 可以为空：不传就是「这一次的批准只管这一次」（测试、无人值守都用
        # 得上）。它和 asker 是一对 —— asker 问出答案，memory 记住人说过「别再问」，
        # 而它是不是存在，决定了审批提示里有没有那个 t。
        self.memory = memory
        # on_checkpoint 可以为空：不传就是不持久化（测试、一次性任务都用得上）。
        # 方向很重要 —— Agent 只知道「什么时候存是安全的」，不知道「存到哪、什么
        # 格式、要不要存」。这和 asker 是同一条原则：判定留在内部，执行交给注入的
        # 实现。万一外面直接改成 run() 之后再存，就会丢掉回合内部的落盘点。
        self.on_checkpoint = on_checkpoint

        # on_event 可以为空：不传就是不记审计日志（测试、一次性任务）。
        # 契约和上面两个完全一样 —— Agent 知道「发生了什么、什么时候发生」，
        # 注入的实现决定「记到哪、什么格式」。所以它也不该自己拼路径、开文件。
        self.on_event = on_event

        self.debug = debug

    def _emit(self, kind: str, session: Session, run_id: str, step: int, **data: Any) -> None:
        """报告一条审计事件。

        每条事件都是纯 JSON 可序列化的 —— 所以这里只传字符串、数字、布尔，
        不传 datetime / Path / 异常对象。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(event(
                kind,
                session_id=session.session_id,
                run_id=run_id,
                step=step,
                **data,
            ))
        except Exception as exc:
            # 观测量出了问题，绝不能影响被观测的过程。而且 on_event 的调用点有些
            # 恰好落在 messages **不一致**的窗口里（tool_call 事件就夹在 assistant
            # 消息和它的 tool 结果之间），抛出去会留下悬空的 tool_calls，会话从此
            # 每轮都 400 —— 这个后果实测过。
            self._warn(f"审计事件写入失败（已忽略）: {type(exc).__name__}: {exc}")

    def _checkpoint(self, session: Session) -> None:
        """在 messages 一致的时刻通知外部保存。

        调用点必须选在 messages **一致**的时刻 —— 见 run() 里标了 ★ 的那处注释。
        不落盘会丢会话；落盘了一个半截状态则更糟：重新加载后直接发不出去。
        """
        if self.on_checkpoint is None:
            return
        try:
            self.on_checkpoint(session)
        except Exception as exc:
            # 和 on_event 不同：落盘点的调用位置**全都在 messages 一致的时刻**，
            # 所以异常抛出去不会损坏会话（实测过）。但它会掩盖「这次没存上」，
            # 而用户会以为存好了 —— 所以吞掉，但必须大声说出来。
            self._warn(f"会话落盘失败（已忽略）: {type(exc).__name__}: {exc}")

    @staticmethod
    def _warn(message: str) -> None:
        """警告始终可见，不受 debug 开关控制。

        被吞掉的失败如果不吭声，就变成了静默失败 —— 那比直接崩还难查。
        """
        print(f"[warn] {message}", file=sys.stderr)

    def _debug(self, message: str) -> None:
        """调试输出走 stderr。

        不混进 stdout，是因为 Agent 的最终结果才是这个程序的"输出"（可以被管道
        接走），中间过程是诊断信息。想关掉就 Agent(..., debug=False)。
        """
        if self.debug:
            print(f"[debug] {message}", file=sys.stderr)

    @staticmethod
    def _preview(text: str, limit: int = DEBUG_PREVIEW_LIMIT) -> str:
        """把多行长文本压成单行预览，过长则截断并标注真实长度。"""
        flat = text.replace("\n", "\\n")
        if len(flat) <= limit:
            return flat
        return f"{flat[:limit]}…(共 {len(flat)} 字符)"

    @staticmethod
    def _usage_fields(usage: TokenUsage | None) -> dict[str, int]:
        """把 token 用量摊成审计字段。

        拿不到 usage 就返回空 dict —— 宁可不写这几个键，也不要往日志里塞一串
        null，那会让后面做成本汇总时处处要判空。
        """
        if usage is None:
            return {}
        return {
            "prompt_tokens": usage.prompt_tokens,
            "cached_tokens": usage.cached_tokens,
            "miss_tokens": usage.miss_tokens,
            "completion_tokens": usage.completion_tokens,
        }

    def _complete_with_retry(
        self,
        session: Session,
        run_id: str,
        step: int,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict],
    ) -> "ModelResponse":
        """调模型，并保证每一次尝试都留下一条审计记录。

        重试策略本身在 agents/retry.py 里 —— 它不碰会话、工具、权限，所以待在
        那边更合适。这里只负责两件本层才知道的事：怎么把尝试记进审计，以及
        失败时补一条 run_finished。

        为什么必须补 run_finished：否则日志里只剩一条悬空的 run_started，事后
        分不清这一轮是"模型调用失败了"还是"进程被杀在半路"，而这两种情况的
        处置完全不同。
        """
        try:
            return call_with_retry(
                self.model, messages, tool_schemas,
                on_attempt=self._attempt_reporter(session, run_id, step),
            )
        except Exception as exc:
            self._emit("run_finished", session, run_id, step,
                       stop_reason="model_fatal" if _is_fatal(exc) else "model_error")
            raise

    def _attempt_reporter(self, session: Session, run_id: str, step: int):
        """把 retry 模块交出来的 Attempt 记成审计事件。"""

        def report(attempt: Attempt) -> None:
            data: dict[str, Any] = {
                "status": attempt.status,       # ok / error / fatal
                "attempt": attempt.number,
                "duration_ms": attempt.duration_ms,
            }
            if attempt.error is not None:
                data["error"] = self._preview(attempt.error, AUDIT_PREVIEW_LIMIT)
            if attempt.response is not None:
                data["tool_calls"] = len(attempt.response.tool_calls)
                data.update(self._usage_fields(attempt.response.usage))
            self._emit("model_call", session, run_id, step, **data)

            if attempt.status == "error":
                self._debug(f"   ↻ 模型调用失败（第 {attempt.number} 次），准备重试")

        return report

        # 循环只会因为 return 或 raise 退出，走不到这里；留着是为了让"函数总有返回
        # 值"这件事在类型上成立，而不是靠读者推理。
        raise ModelError("模型调用重试逻辑异常：未预期的出路")

    @staticmethod
    def _budget_reminder(max_steps: int, step: int) -> dict[str, str]:
        """一条只在本次请求里有效的步数提示。

        **它不进 session.messages。** 三个理由：会话文件会平白多出几十条 user
        消息；step_count() 靠「一条 assistant = 一步」派生，掺进 user 消息之后
        「聊了多少轮」的语义就糊了；而且它逐轮变化，本来就不该被持久化。

        也放不进系统提示词 —— 它逐轮衰减，等于每一轮都把缓存前缀打断一次。所以它
        只能挂在载荷最末尾：那是整段对话里单价最贵的位置，却也正是唯一该变化的位置。
        （一个回合 20 步 ≈ 200 个 token，相对于一次 read_file 动辄上万字符可以忽略。）

        第 1 步它会紧跟真正的用户消息，形成连续两条 user。DeepSeek 的 OpenAI 兼容
        接口接受这个形状，Aider 和 OpenHands 也都是逐轮往尾部追加提醒；但若将来换到
        强制 user/assistant 交替的 provider，这里要改成挂到最后一条 tool 结果上。
        """
        return {"role": "user", "content": f"剩余步数：{max_steps - step}（含本次）"}

    def run(self, session: Session, user_input: str, max_steps: int = 40) -> str:
        # messages 不再每次新建，而是复用传入会话里已有的那份 —— 它让两次 run()
        # 之间、乃至两个进程之间，对话得以延续。会话是参数而不是 Agent 的身份，
        # 所以同一个 Agent 可以服务多个会话。
        # 系统提示词由 Session.new() 在会话创建时写一次，这里不再插入。
        messages = session.messages
        messages.append({"role": "user", "content": user_input})
        self._checkpoint(session)

        # 一次 run 的所有事件共用同一个 run_id。没有它，日志里就分不清哪几条事件
        # 属于同一次提问 —— 而"这一轮为什么失败"恰恰是按轮次问的。
        run_id = uuid4().hex[:8]
        self._emit("run_started", session, run_id, 0,
                   user_input=self._preview(user_input, AUDIT_PREVIEW_LIMIT))

        # 工具定义在一次 run 里不会变，所以取一次就够；下行 debug 也就顺手用同一份。
        tool_schemas = self.tools.schemas()

        # 最后一步调了哪些工具 —— 撞上限时它进异常消息，是"卡在什么上面"唯一的线索。
        last_tools: list[str] = []

        for step in range(max_steps):
            self._debug(
                f"── step {step + 1}/{max_steps}  "
                f"消息数={len(messages)} "
                f"[{', '.join(m['role'] for m in messages)}]  "
                f"工具数={len(tool_schemas)}"
            )

            response = self._complete_with_retry(
                session, run_id, step + 1,
                # 步数提示是按本次载荷临时拼的，不写回 messages —— 见 _budget_reminder
                [*messages, self._budget_reminder(max_steps, step)],
                tool_schemas,
            )

            self._debug(
                f"   ← 模型返回  content={'有' if response.content else '无'}  "
                f"tool_calls={len(response.tool_calls)}"
            )
            if response.content:
                self._debug(f"      content: {self._preview(response.content)}")

            assistant_message = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in response.tool_calls
                ]

            messages.append(assistant_message)
            # 注意：这里**不能**落盘。此刻 messages 处于半截状态 —— assistant 记下了
            # 要调哪些工具，但结果还没跟上。API 会直接 400 拒绝这种历史，
            # 所以半截状态一旦存下来，会话就再也发不出去了。

            if not response.tool_calls:
                self._debug("   无工具调用 → 返回最终结果")
                self._emit("run_finished", session, run_id, step + 1,
                           stop_reason="answered")
                self._checkpoint(session)  # 完整：这条 assistant 消息没有任何 tool_call
                return response.content or ""

            last_tools = [call["name"] for call in response.tool_calls]

            for call in response.tool_calls:
                self._debug(
                    f"   → 工具调用 {call['name']}"
                    f"({self._preview(call['arguments'], 120)})"
                )

                self._emit(
                    "tool_call", session, run_id, step + 1,
                    tool=call["name"],
                    arguments=self._preview(call["arguments"], AUDIT_PREVIEW_LIMIT),
                )

                result = self._execute_tool(call, session, run_id, step + 1)

                self._debug(f"   ← 工具结果 ({len(result)} 字符): {self._preview(result)}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

            # ★ 唯一的常规落盘点：到这里 assistant 的每个 tool_call 都有了对应的
            #   tool 结果，messages 重新一致。崩溃恢复最多退回上一个 ★，不会拿到
            #   一个发不出去的历史。
            self._checkpoint(session)

        # 步数用尽：任务既没失败、也没收尾，所以它既不是返回值（会被 print 进
        # stdout，装成答案），也不是 ModelError（那意味着重试或改配置）。
        #
        # 顺序要紧 —— 审计和落盘必须在 raise 之前完成。否则日志里只剩一条悬空的
        # run_started，事后分不清这一轮是"步数用尽"还是"进程被杀在半路"；而异常
        # 消息里承诺的"会话是完好的"，也得先真的存下去才算数。
        # 撞上限的位置在循环顶部，那里 messages 一致（上一步的结果全 append 完、
        # ★ 也落过盘了），所以这个 raise 不损坏会话。
        self._debug(f"!! 达到最大步数 {max_steps}，停止")
        self._emit("run_finished", session, run_id, max_steps, stop_reason="max_steps")
        self._checkpoint(session)
        raise StepLimitExceeded(max_steps, last_tools)

    def _execute_tool(self, call: dict, session: Session, run_id: str, step: int) -> str:
        """执行一次工具调用并返回给模型的文本。

        四条出口都收敛到末尾那一次 _emit，而不是每处各发一次 —— 状态分类只有这里
        知道（校验失败、被拒、还是运行出错），所以判定留在内部；但"事件长什么样"
        只写一遍，免得四处各写一份、日后改漏。
        """
        started = time.perf_counter()

        try:
            tool = self.tools.get(call["name"])
            arguments = json.loads(call["arguments"])

            # 权限关卡。插在这里是因为 tool.execute() 是全项目唯一让副作用发生的
            # 地方（它内部才调到 handler），所以这是天然的收口点。
            denial = self._check_permission(tool, arguments, session, run_id, step)
            if denial is not None:
                text, status = denial, "denied"
            else:
                result = tool.execute(arguments)
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                status = "ok"

        except ValidationError as exc:
            # 这一支必须放在 except Exception 之前，否则永远不会命中。
            # 参数校验失败是「模型可以自己改对」的错误，所以要明确告诉它
            # 哪个字段、什么毛病；这跟「工具运行出错」是两回事。
            text, status = f"参数校验失败：{self._format_args_error(exc)}", "invalid_args"

        except Exception as exc:
            text, status = f"工具执行失败：{type(exc).__name__}: {exc}", "error"
            if not isinstance(exc, _TOOL_LEVEL_ERRORS):
                # 工具自己抛的（文件不存在、路径越界、JSON 坏）都是预期内的，模型拿到
                # 一句话就够了。其余异常很可能是**我们自己的 bug** —— 把它伪装成
                # 「工具执行失败」喂给模型，模型会老老实实去改参数，于是你在调试一个
                # 根本不存在的问题。所以这里在 stderr 留下完整 traceback。
                self._warn("工具抛出未预期的异常，完整栈如下（给模型的文本已简化）：")
                traceback.print_exc()

        self._emit(
            "tool_result", session, run_id, step,
            tool=call["name"],
            status=status,      # ok / denied / invalid_args / error
            chars=len(text),    # 只记长度，不记全文 —— 全文已经在会话文件里了
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return text

    def _check_permission(
        self,
        tool: Tool,
        arguments: Mapping[str, Any],
        session: Session,
        run_id: str,
        step: int,
    ) -> str | None:
        """走一遍权限关卡，并把裁决记进审计。

        裁决本身在 security/gate.py 里（纯函数、可单测）；这里只负责两件本层才
        知道的事：怎么把结果记进审计，以及怎么在 debug 上说出来。

        permission 事件是审计最要紧的一项 —— "谁批准了什么"除了这里没有别的地方
        知道。无论放行还是拒绝都要发。
        """
        result = check_permission(tool, arguments, self.policy, self.asker, self.memory)

        _DEBUG_BY_OUTCOME = {
            "auto_allowed": "   ✓ 自动放行",
            "rule_allowed": "   ✓ 自动放行（你之前按过 t）",
            "approved": "   ✓ 用户批准",
            "user_denied": "   ✗ 用户拒绝",
            "policy_denied": "   ✗ 权限拒绝（策略禁止）",
            "no_asker": "   ✗ 需要审批但未配置 asker，按拒绝处理",
        }
        self._debug(f"{_DEBUG_BY_OUTCOME[result.outcome]} {tool.name}")

        self._emit(
            "permission", session, run_id, step,
            tool=tool.name,
            risk=tool.risk.value,
            decision=result.decision.value,
            outcome=result.outcome,
            # 参数只留预览：write_file 的 content 可能含敏感内容，审计日志不该抄全文
            arguments=self._preview(json.dumps(arguments, ensure_ascii=False), AUDIT_PREVIEW_LIMIT),
            # 等待审批的时长只在真的问过人才有意义
            **({} if result.waited_ms is None else {"waited_ms": result.waited_ms}),
            # 这一次批准**顺带**改了将来的行为（人按了 t）。省掉它的话，日志里那次
            # 批准看起来就只是"批准了一次"，而后面几十次 shell 无人问津的原因只能靠猜。
            **({} if not result.remembered else {"remembered": sorted(result.remembered)}),
        )
        return result.denial

    @staticmethod
    def _format_args_error(exc: ValidationError) -> str:
        """把 Pydantic 的报错压成一行：「哪个字段: 什么毛病」。

        直接 str(exc) 有 200+ 字符，还带一个 errors.pydantic.dev 文档链接 ——
        那是给人看的。这段文本会进入对话历史，此后每轮请求都重复计费，所以只
        保留模型能用来修正参数的部分。
        """
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<参数>'}: {err['msg']}"
            for err in exc.errors()
        )
