import json
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
from agent_runtime.tools.tool import Tool, ToolRegistry, ToolResult

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

# 计时的时钟。**注入而不是直接调 time.perf_counter**，理由和 retry.py 里 sleep 可注入
# 一样：时间没法断言。测试里换成一个由假模型/假 handler 推进的假时钟，duration_ms 才能
# 被钉成精确值；换成真实时钟，这类断言只能在 CI 上随机红。
#
# 必须是单调时钟：time.time() 会被 NTP 调整，测出来的"耗时"可能是负数。
Clock = Callable[[], float]

# 审计事件里参数预览的最大长度。参数可能很长（write_file 的 content），
# 也可能含敏感内容，所以审计日志只留预览、从不记全文。
AUDIT_PREVIEW_LIMIT = 200

# 一个批次里最多同时跑几个工具。
#
# 它挡的是"模型一口气给出 30 个调用"那种极端形状：read_file 会把整份文件读进内存、
# 结果再整份进入下一轮请求，并发度等于批次大小的话，句柄和内存都会出现一个没有必要的
# 尖峰。实测最常见的批是 2~5 个，所以这个上限在正常形状上永远不会碰到。
MAX_PARALLEL = 8

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


@dataclass(frozen=True, slots=True)
class _Outcome:
    """一次工具调用的结局：给模型的文本 + 审计用的状态和耗时。

    它取代了原来"直接从 `_execute_tool` 返回一个字符串"：状态和耗时只有那一层
    知道，而**事件由主线程发**（见 `_run`），所以判定和上报之间需要一个能带过线程
    边界的载体。
    """

    text: str
    status: str                     # ok / denied / invalid_args / error
    duration_ms: int
    # 工具抛出的**非预期**异常的完整栈（疑似我们自己的 bug）。
    #
    # 它不在工作线程里直接打印，是因为 traceback.print_exc() 是一行一次写 —— 两个
    # 线程同时打会交错成一段读不懂的东西。带回主线程由 _report_tool_bug 打。
    traceback: str | None = None
    # 工具自己带回来的审计字段（`ToolResult.audit`）。**排在最后**：前四个字段有位置
    # 参数的调用点（`_prepare` / `_run` 里那几个 _Outcome(...)），插在中间会静默地把
    # traceback 挪到 audit 上去。
    #
    # 为什么要留这么一条通道：ask_user 的 duration_ms **含等人的时间**（它就阻塞在
    # 人的输入上），而 cli 那边要把那一段减出来单列成"等人回答" —— 否则"我看了 30 秒
    # 才回答"会显示成"这个工具花了 30 秒"。审批没有这个问题，因为裁决和计时都在
    # `_prepare` 里、本来就单独计时；提问发生在 handler 里，只有工具自己知道等了多久。
    #
    # 它**不是**给模型的：回灌进对话历史的只有 text，答案正文也在那里，不记第二遍。
    audit: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Prepared:
    """一次工具调用的前半段：裁决已经做完，执行随时可以开始。

    **"裁决"和"执行"必须能分开**，因为并行的只有后半段：asker 走 stdin，两条审批
    同时问会互相抢输入，所以裁决一律留在主线程、按原顺序发生（见 security/asker.py）。
    """

    call: dict[str, Any]
    tool: Tool | None               # None：准备阶段就已经定局，见 settled
    arguments: dict[str, Any] | None
    parallel_safe: bool
    settled: _Outcome | None        # 有值表示不需要再执行（准备失败、或被拒）


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
        clock: Clock = time.perf_counter,
        autopilot: bool = False,
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

        # 时钟也是注入的：审计里的每个 duration_ms 都出自它，所以测试要能把它换成假的
        # （见上面 Clock 那段）。
        self.clock = clock

        # autopilot：这一轮没有人在键盘前，所以一律不问。它**只管审批** —— 工作区边界、
        # 控制面写入、拒绝名单都不归它管（那些是"不许做"，不是"要不要问"）。审计里每次
        # 放行会记成 outcome=autopilot，好回答"这次会话到底有没有人看着"。
        self.autopilot = autopilot

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

    def _finish_run(
        self,
        session: Session,
        run_id: str,
        step: int,
        run_started: float,
        stop_reason: str,
    ) -> None:
        """收尾：记一条 run_finished，并带上这一回合的墙上时间。

        三个收尾点（答完 / 步数用尽 / 模型失败）都走这里 —— 事件长什么样只写一遍，
        省得三处各写一份、日后漏掉 duration_ms 这种字段。

        duration_ms 是**回合总时长**，和逐条 model_call / tool_result 的口径不重叠，
        所以 CLI 那边可以直接把它们相减去算"未归因"。口径重叠过一次的代价这里记得：
        工具耗时原本是从函数入口起表的，把等人审批也算了进去。
        """
        self._emit(
            "run_finished", session, run_id, step,
            stop_reason=stop_reason,
            duration_ms=int((self.clock() - run_started) * 1000),
        )

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

    def _debug_lazy(self, build: Callable[[], str]) -> None:
        """`_debug` 的惰性版：debug 关着时**连字符串都不去拼**。

        什么时候必须用它：消息里含 `_preview(...)`、或者要把一段可能很长的正文拼进去
        时。两个理由，都不是洁癖：

          1. `_preview` 不是 O(1) —— 它先把换行压平再截断，也就是把整段文本扫一遍；
          2. f-string 的实参**在进 _debug 之前就求值了**，所以 debug 关着的时候，那次
             扫描的产物会被立刻丢掉。

        实测：一个 8MB 的 read_file 结果白扫约 10ms，一批五个就是 50ms —— 而这条路径
        对**默认不开 debug** 的每一次工具调用都成立。也就是说：不开 debug 的会话一直在
        替 debug 买单。

        参数是 `Callable[[], str]` 而不是字符串，是为了让上面那句话在签名上就成立：
        传进来的东西**只有真要打的时候才求值**。短消息（几个字段拼一拼）直接用
        `_debug` 就好，套一层 lambda 只是噪声。
        """
        if self.debug:
            self._debug(build())

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
        run_started: float,
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
                # 同一个时钟传下去：一次回合里所有 duration_ms 必须出自同一把尺子，
                # 否则"模型 1.2s + 工具 0.3s"这种加法没有意义。
                clock=self.clock,
            )
        except Exception as exc:
            self._finish_run(
                session, run_id, step, run_started,
                "model_fatal" if _is_fatal(exc) else "model_error",
            )
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
            # 退避时长（只在这条失败的尝试还会重试时才有）：不记它的话，"这一轮为什么
            # 慢了 3 秒"在日志里是看不出来的 —— 那段等待不属于任何一次请求。
            if attempt.backoff_ms is not None:
                data["backoff_ms"] = attempt.backoff_ms
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
        # 回合的起点：run_finished 里的 duration_ms 从这里算起。放在最前面（而不是从
        # 第一次模型请求算起）是因为"这一轮花了多久"要含上追加消息、落盘这些开销 ——
        # 它们没被单独埋点，交给 CLI 那行汇总里的"未归因"去吸收，比假装它们不存在诚实。
        run_started = self.clock()

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
                run_started,
            )

            self._debug(
                f"   ← 模型返回  content={'有' if response.content else '无'}  "
                f"tool_calls={len(response.tool_calls)}"
            )
            if response.reasoning:
                # 思考过程**整段打，不截断** —— 和下面 content 的预览不一样，理由是
                # "别处能不能看到"：答案在 stdout 上有全文，所以 debug 只给一眼预览；
                # 思维链别处根本看不到，截断它等于不给看。
                #
                # 非流式拿不到"逐字"：这段文字是整块回来的，只能等它回来之后一次打完。
                # 想要 Claude Code 那种实时效果得先把适配层改成流式（另一件事）。
                #
                # 用惰性版：这段正文可能几千字，拼进 f-string 就是一次整段拷贝。
                self._debug_lazy(
                    lambda: f"      thinking（{len(response.reasoning)} 字符）:\n"
                            f"{response.reasoning}"
                )
            if response.content:
                self._debug_lazy(
                    lambda: f"      content: {self._preview(response.content)}"
                )

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
                self._finish_run(session, run_id, step + 1, run_started, "answered")
                self._checkpoint(session)  # 完整：这条 assistant 消息没有任何 tool_call
                return response.content or ""

            last_tools = [call["name"] for call in response.tool_calls]

            # 一批走完（可能并发、也可能逐条，见 _run_batch），结果一一对应地回到
            # messages 里 —— 顺序永远是模型给出的顺序。
            outcomes = self._run_batch(response.tool_calls, session, run_id, step + 1)

            for call, outcome in zip(response.tool_calls, outcomes):
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": outcome.text,
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
        self._finish_run(session, run_id, max_steps, run_started, "max_steps")
        self._checkpoint(session)
        raise StepLimitExceeded(max_steps, last_tools)

    # --- 一批工具调用 -------------------------------------------------------

    def _parallel_safe(self, call: dict) -> bool:
        """这条调用所在的工具声明了"能并行"吗。

        用 `.get` 是为了让"工具名不认识"落到 False（回退串行）而不是抛出来 ——
        真正该报的那个错由 _prepare 在里面按原来的方式报，这里只负责选路径。
        """
        try:
            return self.tools.get(call["name"]).parallel_safe
        except KeyError:
            return False

    def _run_batch(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """跑完一批工具调用，返回与 batch **一一对应**（同序）的结局。

        **串行是默认，并行是例外**，而例外只在一种形状下成立：整批都是
        `parallel_safe`（也就是只读）且不止一条。任何一个不能并行的调用都会把整批
        按老路逐条跑 —— 也就是今天的行为逐字节不变。

        为什么是"整批"而不是"把能并行的挑出来并行"：一个批次里的调用隐含了顺序
        语义。最常见的形状是 `[read_file(a), write_file(a), read_file(a)]`（读、改、
        读回验证 —— 提示词里明确要求写完之后读回来确认）。把两个读挑出来并发、把写
        留在串行，读回验证就可能发生在写之前：模型会读到旧内容，然后报告"已确认改好
        了"。**那是静默错误，而且它伪装成验证通过。** 整批一起退回去，这个偏序问题
        就不存在了。
        """
        if len(batch) >= 2 and all(self._parallel_safe(call) for call in batch):
            return self._run_parallel(batch, session, run_id, step)
        return self._run_serial(batch, session, run_id, step)

    def _report_tool_call(self, call: dict, session: Session, run_id: str, step: int) -> None:
        """两条路径共用的"模型要调什么"那一句（debug + 审计）。"""
        # 惰性：write_file 的 content 可以很长，而 _preview 要把它扫一遍。
        self._debug_lazy(
            lambda: f"   → 工具调用 {call['name']}"
                    f"({self._preview(call['arguments'], 120)})"
        )
        self._emit(
            "tool_call", session, run_id, step,
            tool=call["name"],
            arguments=self._preview(call["arguments"], AUDIT_PREVIEW_LIMIT),
        )

    def _report_tool_result(
        self, call: dict, outcome: _Outcome, session: Session, run_id: str, step: int,
        parallel: bool = False,
    ) -> None:
        """两条路径共用的"这条调用结果如何"那一句（审计 + debug）。"""
        self._emit(
            "tool_result", session, run_id, step,
            tool=call["name"],
            status=outcome.status,      # ok / denied / invalid_args / error
            chars=len(outcome.text),    # 只记长度，不记全文 —— 全文已经在会话文件里了
            duration_ms=outcome.duration_ms,
            # 工具自己知道、而这一层推不出来的字段（ask_user 的 question_status /
            # human_wait_ms）。键名由工具负责不撞上面那几个 —— 它们已经在这里了。
            **outcome.audit,
            # 只有真并发了才写这个键：没有它，事后从日志里分不出这一批是并发还是逐条，
            # 而"这一批为什么快/慢"正是拿着日志要回答的问题。
            **({"parallel": True} if parallel else {}),
        )
        # 惰性 —— 这一行是那笔白工的主项：工具结果动辄几十万字符（read_file 不分页），
        # 而 _preview 会把整段扫一遍。见 _debug_lazy。
        self._debug_lazy(
            lambda: f"   ← 工具结果 ({len(outcome.text)} 字符): {self._preview(outcome.text)}"
        )

    def _run_serial(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """逐条：报调用 → 裁决 → 执行 → 报结果。这就是原来那个 for 循环。

        **裁决和执行在同一条调用上相邻，这一点不能改**：人是在看到上一条的结果之后
        才被问到下一条的，而上一条的结果正是他判断"这条该不该放行"的依据之一。
        """
        outcomes: list[_Outcome] = []
        for call in batch:
            self._report_tool_call(call, session, run_id, step)
            outcome = self._run(self._prepare(call, session, run_id, step))
            self._report_tool_bug(outcome)
            self._report_tool_result(call, outcome, session, run_id, step)
            outcomes.append(outcome)
        return outcomes

    def _run_parallel(
        self, batch: list[dict], session: Session, run_id: str, step: int
    ) -> list[_Outcome]:
        """整批只读工具：先把裁决做完，再并发执行。

        **事件一律由这里（主线程）发**，顺序和串行路径完全一样 —— 先按原顺序报完
        整批 tool_call，再按原顺序报 tool_result。所以 on_event 不需要是线程安全的：
        那是注入进来的实现，"它必须自己加锁"会是一条没人想得到的隐式契约。

        代价是这一批的事件在整批跑完之后才出现。对只读批次来说可以接受（它们本来就
        是秒级以下的活），换来的确定性更值钱 —— 按完成顺序发事件的话，同一个会话
        两次跑出来的审计顺序会不一样。
        """
        for call in batch:
            self._report_tool_call(call, session, run_id, step)

        # 裁决（含问人）仍在主线程、仍按原顺序。这里比串行路径多了一点：整批先问完
        # 再执行。只读工具在默认策略下不会问人（LOW 自动放行），所以这个差别平时看
        # 不见；真问到人时（.tudouni.json 里把 auto_approve 写成了 []），它也是可以
        # 接受的 —— 这一批都是只读的，人的判断不依赖前一条的结果。
        prepared = [self._prepare(call, session, run_id, step) for call in batch]

        started = self.clock()
        with ThreadPoolExecutor(
            max_workers=min(len(prepared), MAX_PARALLEL),
            thread_name_prefix="tool",
        ) as pool:
            # map 按**输入顺序**返回，所以调度不影响 messages 里 tool 结果的顺序，
            # 也不影响事件的顺序：同一个会话跑两次，历史是一样的。
            outcomes = list(pool.map(self._run, prepared))
        wall_ms = int((self.clock() - started) * 1000)

        for call, outcome in zip(batch, outcomes):
            self._report_tool_bug(outcome)
            self._report_tool_result(call, outcome, session, run_id, step, parallel=True)

        # 这一批实际占了多长墙上时间。**必须单独记一笔**：并发时逐条 duration_ms
        # 相加大于墙上时间（两个 5 秒的工具并行，和是 10 秒），而 cli.py 那个
        # "未归因 = 回合总 - 各项之和"依赖的是互不重叠的几段 —— 不记它，那几项一
        # 相加就会超过回合总，差值被 max(0, ...) 悄悄吞掉，报出一行恒为 0 的"未归因"。
        self._emit(
            "tool_batch", session, run_id, step,
            calls=len(outcomes),
            wall_ms=wall_ms,
            tools=",".join(call["name"] for call in batch),
        )
        return outcomes

    def _prepare(self, call: dict, session: Session, run_id: str, step: int) -> _Prepared:
        """一条调用的前半段：认工具、解析参数、过权限关。

        这是原来 `_execute_tool` 的前半段，异常映射一个字都没改（工具名不认识是
        KeyError、参数不是 JSON 是 JSONDecodeError，都变成"工具执行失败"）。

        ValidationError **不在这里**：参数校验发生在 `tool.execute()` 里面，也就是
        `_run` 那一段 —— 见那里的注释。
        """
        started = self.clock()

        try:
            tool = self.tools.get(call["name"])
            arguments = json.loads(call["arguments"])

            # 权限关卡。插在这里是因为 tool.execute() 是全项目唯一让副作用发生的
            # 地方（它内部才调到 handler），所以这是天然的收口点。
            denial = self._check_permission(tool, arguments, session, run_id, step)
            if denial is not None:
                # denied 那一支什么都没执行：记 0 而不是"从函数入口算起的耗时"，否则
                # 这个数字会变成审批开销的同义词（它属于 permission 事件）。
                return _Prepared(call, None, None, False, _Outcome(denial, "denied", 0))

            return _Prepared(call, tool, arguments, tool.parallel_safe, None)

        except Exception as exc:
            return _Prepared(
                call, None, None, False,
                _Outcome(
                    f"工具执行失败：{type(exc).__name__}: {exc}",
                    "error",
                    int((self.clock() - started) * 1000),
                    self._bug_report(exc),
                ),
            )

    def _run(self, prepared: _Prepared) -> _Outcome:
        """一条调用的后半段：真的执行它。**能并发的只有这一段。**

        四条出口都收敛成一个 _Outcome，事件交给调用方（主线程）发 —— 状态分类只有
        这里知道（校验失败、被拒、还是运行出错），但"事件长什么样"只写一遍。
        """
        if prepared.settled is not None:
            return prepared.settled

        tool, arguments = prepared.tool, prepared.arguments
        assert tool is not None and arguments is not None, "settled 为空时 tool/arguments 必然有值"

        started = self.clock()          # ← 从这里才算"执行"
        try:
            result = tool.execute(arguments)

            # ToolResult 是"文本 + 审计字段"的那种返回值（目前只有 ask_user）。放在
            # json.dumps 那一步**之前**：它不是要序列化的数据，而是已经渲染好的文本，
            # 只是多带了几个只有工具自己知道的字段。
            if isinstance(result, ToolResult):
                return _Outcome(
                    result.text, "ok",
                    int((self.clock() - started) * 1000),
                    audit=result.audit,
                )

            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return _Outcome(text, "ok", int((self.clock() - started) * 1000))

        except ValidationError as exc:
            # 这一支必须放在 except Exception 之前，否则永远不会命中。
            # 参数校验失败是「模型可以自己改对」的错误，所以要明确告诉它
            # 哪个字段、什么毛病；这跟「工具运行出错」是两回事。
            return _Outcome(
                f"参数校验失败：{self._format_args_error(exc)}", "invalid_args",
                int((self.clock() - started) * 1000),
            )

        except Exception as exc:
            return _Outcome(
                f"工具执行失败：{type(exc).__name__}: {exc}", "error",
                int((self.clock() - started) * 1000),
                self._bug_report(exc),
            )

    @staticmethod
    def _bug_report(exc: BaseException) -> str | None:
        """非预期异常才要完整栈；工具自己抛的是预期内的，带回来也没人打。

        工具自己抛的（文件不存在、路径越界、JSON 坏）都是预期内的，模型拿到一句话
        就够了。其余异常很可能是**我们自己的 bug** —— 把它伪装成「工具执行失败」喂给
        模型，模型会老老实实去改参数，于是你在调试一个根本不存在的问题。所以这些要
        在 stderr 留下完整 traceback。
        """
        if isinstance(exc, _TOOL_LEVEL_ERRORS):
            return None
        return "".join(traceback.format_exception(exc))

    def _report_tool_bug(self, outcome: _Outcome) -> None:
        """把 _bug_report 攒下的栈打出来。**只在主线程调用。**

        traceback.print_exc() 是一行一次写，而 print 本身只保证"一次调用"是原子的 ——
        两个工作线程同时打，栈就会交错成一段读不懂的东西。所以栈是带回主线程打的。
        """
        if outcome.traceback is None:
            return
        self._warn("工具抛出未预期的异常，完整栈如下（给模型的文本已简化）：")
        print(outcome.traceback, file=sys.stderr, end="")


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
        result = check_permission(tool, arguments, self.policy, self.asker, self.memory,
                                  clock=self.clock, autopilot=self.autopilot)

        _DEBUG_BY_OUTCOME = {
            "auto_allowed": "   ✓ 自动放行",
            "autopilot": "   ✓ 自动放行（autopilot：这一轮没人在看）",
            "rule_allowed": "   ✓ 自动放行（你之前按过 t）",
            "command_allowed": "   ✓ 自动放行（命中命令规则）",
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
            # 凭哪条命令规则放过的。没有它，日志只能看出"这条命令没问过"，看不出凭哪一条
            # —— 而"撤掉哪条规则能把它变回要问"正是事后最想知道的事。
            **({} if result.rule is None else {"rule": " ".join(result.rule)}),
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
