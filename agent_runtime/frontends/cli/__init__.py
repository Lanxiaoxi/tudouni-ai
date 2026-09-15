"""多轮对话循环，以及四个"不需要模型"的子命令的展示。

和装配分开，是因为**它们的变化原因不同**：`runtime/composition.py` 关心"把哪些
部件接起来"，改动来自架构演进；这里关心"怎么跟用户说话"，改动来自使用习惯。
放在一起，两种改动的 diff 会互相淹没。

**第零期之后这里少了一样东西**：启动横幅、`[权限]` / `[技能]` / `[任务]` 那几行、
已注册工具的清单全都不在这里了 —— 它们变成了 `Runtime.notices()` 返回的数据，
由启动器按 `notice.stream` 打出来。这里只留下**交互和查看**：REPL、历史、审计、
会话列表、技能列表。

四个"不需要模型"的子命令（`print_sessions` / `print_skills` / `print_history` /
`print_audit`）仍然住在这里（决策 20）：它们只读 store / logs / 技能目录，
不装配 Runtime，而且有意排在配置检查之前 —— 没配密钥的人照样该能查自己的历史。

顺带解决了一个具体的别扭：以前"新会话 X"是在配置检查之前打印的，所以没配
DEEPSEEK_API_KEY 时会先看到"新会话"再看到报错。现在子命令分两段。
"""

import sys
from collections.abc import Iterable
from dataclasses import dataclass

from agent_runtime import i18n
from agent_runtime.agents import RunCancelled, StepLimitExceeded
from agent_runtime.audit import JsonlSink
from agent_runtime.models.types import ModelFatalError, ModelTransientError
from agent_runtime.runtime.composition import Runtime
from agent_runtime.runtime.config import MCP_FILE
from agent_runtime.security.commands import format_rule
from agent_runtime.skills import SkillLoader
from agent_runtime.skills import loader
from agent_runtime.state import JsonSessionStore, Session
from agent_runtime.state import reasoning
from agent_runtime.state import status as status_summary
from agent_runtime.tools.builtin.todo import progress_line

# 参数形状住在 frontends/cli/args.py。这里再导出一次，是因为现有调用点（包括
# `main.py` 和一堆测试）写的是 `from agent_runtime.frontends.cli import build_parser`
# —— 让它们照旧工作，而不是把一次搬动扩散到每一个调用点。
from agent_runtime.frontends.cli.args import build_parser  # noqa: F401


# --- 启动横幅 -------------------------------------------------------------

# 纯 ASCII，一个非 ASCII 字符都没有 —— 这不是审美选择，是兼容性。Windows 控制台在
# 中文区域设置下是 cp936，框线字符（─│╭╯）和 emoji 要么直接抛 UnicodeEncodeError、
# 要么显示成乱码。一个把启动搞崩的装饰图案比没有图案糟糕得多，而且这类问题只在别人
# 的机器上出现。
#
# 最宽 69 列，80 列的终端不会折行。折行的图案只是一堆乱字符，而且折在哪一列取决于
# 终端宽度，没法预测。
#
# 每一行末尾都带 \n：相邻字符串字面量是直接相接的，少一个就是把两行粘成一行。
BANNER = (
    "   .-~~~-.      _____   _   _   ____     ___    _   _   _   _    ___\n"
    "  /  ,-.  \\    |_   _| | | | | |  _ \\   / _ \\  | | | | | \\ | |  |_ _|\n"
    " |  (   )  |     | |   | | | | | | | | | | | | | | | | |  \\| |   | |\n"
    "  \\  `-'  /      | |   | |_| | | |_| | | |_| | | |_| | | |\\  |   | |\n"
    "   `-----'       |_|    \\___/  |____/   \\___/   \\___/  |_| \\_|  |___|\n"
    "               a g e n t   r u n t i m e"
)


def print_banner() -> None:
    """在正式信息之前打一幅启动图案。

    **走 stderr，不走 stdout。** README 承诺 `uv run main.py > 对话.txt` 拿到的是干净
    的答案，而横幅是装饰 —— 跑进那个文件里就是污染。这也正是它不需要 --no-banner
    开关的原因：管道的 stdout 本来就不受影响。

    刻意不收 file 参数：收了，早晚会有一半调用点把装饰又写回 stdout。
    """
    print(file=sys.stderr)
    print(BANNER, file=sys.stderr)
    print(file=sys.stderr)


# --- 用量汇总 -------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    """一段事件里模型调用的用量合计。

    下面各项只累加**成功**的调用：重试里失败的尝试没有 usage 字段（agents/retry.py
    里 error / fatal 两种尝试都不带 response），把它们计进去只会把命中率算低 ——
    而"花了多少"是唯一要回答的问题。
    """

    calls: int          # 所有 model_call 事件（含重试里的失败尝试）
    ok_calls: int       # 其中 status=ok 的（"成功了几次"）。网关不回 usage 的调用
                        # 仍算成功、但对下面各项贡献 0 —— 它和"有账可算的次数"不是
                        # 一回事，所以 hit_rate 的分母是 prompt，不是它。
    prompt: int
    cached: int
    miss: int
    completion: int

    @property
    def hit_rate(self) -> str:
        """命中率的展示形式。

        没有输入 token 时返回 "—" 而不是 "0%"：0% 会让人以为缓存白白配错了，
        而事实是一次缓存查询都还没发生过。两者必须能分辨出来。
        """
        return f"{self.cached / self.prompt:.0%}" if self.prompt else "—"


def summarize(events: Iterable[dict]) -> Usage:
    """把 model_call 事件汇总成用量。

    --audit 末尾那份汇总和交互循环末尾那句统计共用这一个函数 —— 两边回答的是
    同一个问题（"这批事件花了多少"），算出两个不同的数字才是 bug。
    """
    # 用 .get 而不是下标：这个函数现在每轮都在交互循环的末尾跑，而 JsonlSink.read
    # 只跳过解析失败的行、不保证每行都有 kind（日志会被复制、拼接、汇总）。少一个键
    # 不该让一轮本来正常的会话崩在收尾那一行上。
    calls = [e for e in events if e.get("kind") == "model_call"]
    ok = [e for e in calls if e.get("status") == "ok"]
    return Usage(
        calls=len(calls),
        ok_calls=len(ok),
        prompt=sum(e.get("prompt_tokens", 0) for e in ok),
        cached=sum(e.get("cached_tokens", 0) for e in ok),
        miss=sum(e.get("miss_tokens", 0) for e in ok),
        completion=sum(e.get("completion_tokens", 0) for e in ok),
    )


# --- 耗时汇总 -------------------------------------------------------------

@dataclass(frozen=True)
class Timing:
    """一段事件里的耗时合计，单位毫秒。

    每一项都是**互不重叠**的一段，所以能直接相加、也能拿总时长减出"未归因"：
    模型往返（每次尝试一条）、工具执行（不含等人审批，也不含等人回答）、等人审批、
    等人回答、重试退避。口径重叠过一次的教训写在这里 —— 工具耗时原本从函数入口起表，
    把等人审批也算了进去，于是它几乎等于人的思考时间，而那两个数一相加还会重复。

    **"等人回答"是唯一减出来的一段**（见下面 human_ms）：审批那一段本来就住在裁决里、
    从没进过工具耗时，而提问发生在 handler 内部，不减它就没有第二个地方能把它分开。

    **工具那一段在并行之后换了口径。** 只读工具整批并发时，逐条 duration_ms 之和已经
    大于它占用的墙上时间（两个 5 秒的工具并行：和是 10 秒，墙上只花了 5 秒），再拿
    这个和去减"未归因"就会减出负数、被 max(0, ...) 吞掉 —— 报出一行恒为 0 的
    "未归因"，而它看起来完全正常。所以并行批次读的是 `tool_batch.wall_ms`（那一批
    实际占了多久），逐条的和挪去回答另一个问题：省下了多少（见 saved_ms）。
    """

    run_ms: int          # 各回合墙上时间之和（run_finished.duration_ms）
    model_ms: int
    tool_ms: int
    waited_ms: int
    backoff_ms: int
    # 等人**回答提问**的时间（ask_user 阻塞在人的输入上的那一段）。
    #
    # 它和 waited_ms（等人审批）分开，因为来路不同、事后要问的问题也不同：审批是
    # runtime 拦住了一次工具调用，提问是模型自己发起的一次交互。而它**必须单独存在**
    # 的理由是另一条：ask_user 的 handler 就阻塞在人的输入上，那段时间**已经**算进了
    # 它的 duration_ms —— 不从 tool_ms 里减出来，"我看了 30 秒才回答"会被报成
    # "这个工具花了 30 秒"，而工具本身只花了几微秒。
    human_ms: int = 0
    # 并行省下来的时间 = 并行批次里"逐条耗时之和 - 实际墙上时间"。
    #
    # 它是**派生值，不是一段**：上面那几项相加等于回合总，而它不参与那个等式（省掉的
    # 时间本来就不在回合总里）。所以它单独显示，且不被 "未归因" 减掉。
    saved_ms: int = 0

    @property
    def explained_ms(self) -> int:
        # human_ms 是**从 tool_ms 里减出来的那一份**，所以必须加回这个等式：不加，
        # 等人的时间就会掉进"未归因"，而那一项的名字是"没被埋点的部分" —— 它是被
        # 埋了点的那一段。
        return (
            self.model_ms + self.tool_ms + self.waited_ms
            + self.backoff_ms + self.human_ms
        )

    @property
    def unattributed_ms(self) -> int:
        """总时长里**没被埋点**的那部分：会话落盘、事件写入、解析与策略判定……

        下界取 0：毫秒取整、以及从别处复制来的日志都可能让它算成负数，而负数在这里
        没有任何解释价值。但它不是"补零"—— 它就是剩下多少，所以宁可显式说出来，
        也不要让那几个分项看起来像是全部。
        """
        return max(0, self.run_ms - self.explained_ms)


def summarize_time(events: Iterable[dict]) -> Timing:
    """把耗时从事件里数出来。

    和 `summarize()` 完全同一个模式：**不另记一份计时状态** —— 另记就有了两份事实，
    早晚不一致。所以这里读的也是审计事件，而 Agent 那边只负责把它们记准。
    """
    # 一份事件要被数好几遍（下面按 kind 分别求和），所以先落成一份。
    events = list(events)

    def total(kind: str, field: str) -> int:
        return sum(e.get(field, 0) for e in events if e.get("kind") == kind)

    def tool_durations(parallel: bool) -> int:
        return sum(
            e.get("duration_ms", 0) for e in events
            if e.get("kind") == "tool_result" and bool(e.get("parallel")) is parallel
        )

    # 并行批次：逐条之和是"各工具自己花了多久"（彼此重叠），批次墙上时间才是
    # "这一批占用了多久"。老日志里根本没有 tool_batch 事件，两项都算 0，
    # 于是 tool_ms 退化回原来的"逐条相加"—— 旧数字一个都不变。
    batch_sum = tool_durations(parallel=True)
    batch_wall = total("tool_batch", "wall_ms")

    # 等人回答提问的那一段：**从工具耗时里减出来**，单列成 human_ms。
    #
    # 工具耗时原来是从 handler 入口起表的，而 ask_user 的 handler 整段时间都阻塞在人的
    # 输入上 —— 不减，"我看了 30 秒才回答"就报成"这个工具要 30 秒"。审批没有这个问题
    # （裁决在 _prepare 里，本来就单独计时），提问发生在 handler 里，只有工具自己能报。
    # 下界取 0 的理由和下面 unattributed_ms 一样。
    human_ms = total("tool_result", "human_wait_ms")
    tool_ms = max(0, tool_durations(parallel=False) + batch_wall - human_ms)

    return Timing(
        run_ms=total("run_finished", "duration_ms"),
        model_ms=total("model_call", "duration_ms"),
        tool_ms=tool_ms,
        waited_ms=total("permission", "waited_ms"),
        backoff_ms=total("model_call", "backoff_ms"),
        human_ms=human_ms,
        saved_ms=max(0, batch_sum - batch_wall),
    )


def _ms_text(ms: int) -> str:
    """毫秒转成人看的字符串。

    秒以下保留整数毫秒：工具耗时常常就是几十毫秒，写成 "0.0s" 等于没说。
    分钟以上换成 min —— 一个会话聊到几十分钟时，"1800.0s" 需要读者自己换算。
    """
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}min"


def _tokens_text(count: int) -> str:
    """token 数的人读形式。

    要用它的地方量级差得很远：一轮的用量可能几千，而窗口是百万级。所以 1k 以下给
    精确值，10k 以下给一位小数，再往上给整数 k，百万以上给 M —— 占比这种东西不需要
    四位有效数字。
    """
    if count < 1_000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.3g}M"


def _print_timing(timing: Timing) -> None:
    """耗时那一行。没有任何耗时数据（旧日志）时什么都不说 —— 硬凑一行 0ms 只是噪声。"""
    if not (timing.model_ms or timing.tool_ms or timing.waited_ms or timing.human_ms):
        return

    parts = [
        f"模型 {_ms_text(timing.model_ms)}",
        f"工具 {_ms_text(timing.tool_ms)}",
    ]
    # 并行省下来的那一份单列。它不参与上面那句"工具"里（那里报的是真实占用的墙上
    # 时间），也不参与末尾的相加 —— 它正是被省掉、没进入回合总的那一段。
    if timing.saved_ms:
        parts.append(f"（并行省 {_ms_text(timing.saved_ms)}）")
    # 没发生过的事不占位置：没人被问过审批、没人被提问过时，那两项只是 0ms 的噪声。
    if timing.waited_ms:
        parts.append(f"等人审批 {_ms_text(timing.waited_ms)}")
    if timing.human_ms:
        parts.append(f"等人回答 {_ms_text(timing.human_ms)}")
    if timing.backoff_ms:
        parts.append(f"重试退避 {_ms_text(timing.backoff_ms)}")
    # 回合总时长只有在记过 run_finished.duration_ms 的日志里才有（旧日志没有），
    # 所以这两项跟着它一起出现或一起消失。
    if timing.run_ms:
        parts.append(f"未归因 {_ms_text(timing.unattributed_ms)}")
        parts.append(f"回合总 {_ms_text(timing.run_ms)}")

    print("耗时  " + "  ".join(parts))


def last_prompt_tokens(events: Iterable[dict]) -> int | None:
    """最近一次**成功**的模型调用实际发过去的输入 token 数；没有就返回 None。

    取最后一条带 prompt_tokens 的 model_call：失败的尝试压根没有用量字段
    （`_usage_fields` 拿不到 usage 就什么都不写），所以"有值"本身就等于"成功"。
    一轮里可能调很多次模型，取**最后**那条 —— 上下文只增不减，最后那条最大，也最接近
    下一次请求会发出去的大小。

    为什么必须是这个实测值：本地估算需要 tokenizer（项目没有这个依赖），而且还得自己把
    tool schemas 也算进去 —— 估出来的数会偏十几个百分点，而**报错的数比不报更坏**。
    """
    for event in reversed(list(events)):
        if event.get("kind") == "model_call" and "prompt_tokens" in event:
            return event["prompt_tokens"]
    return None


def last_turn_ms(events: Iterable[dict]) -> int | None:
    """最近一个回合的墙上时间（毫秒）；配不成对就返回 None。

    **按 run_id 配对**，而不是直接取最后一条 run_finished：一次回合在收尾之前就崩了的
    话（Ctrl+C、进程被杀），日志里最后那条 run_finished 属于**上一轮** —— 把它当"本轮"
    报出来，是在报一个跟这次无关的数字，而它看起来完全合理。

    取的是 run_finished 里那个 duration_ms（Agent 记的），**不在这里重新计时**：那样
    就有两份事实，而且 REPL 报的会和 `--audit` 报的对不上。
    """
    events = list(events)
    started = [e for e in events if e.get("kind") == "run_started"]
    if not started:
        return None

    run_id = started[-1].get("run_id")
    finished = [
        e for e in events
        if e.get("kind") == "run_finished" and e.get("run_id") == run_id
    ]
    if not finished:
        return None
    return finished[-1].get("duration_ms")


# --- 不需要模型的子命令 ---------------------------------------------------

def print_sessions(store: JsonSessionStore) -> None:
    ids = store.list_ids()
    print(f"已保存 {len(ids)} 个会话（{store.directory}）：")
    for session_id in ids:
        session = store.load(session_id)
        # 任务列表跨进程活着（它存在会话文件里），所以"哪个会话还剩着活"是选会话时
        # 真正想知道的那件事之一 —— 它和消息条数一样，都是读一眼会话文件就有的事实。
        todo = progress_line(session.metadata)
        print(f"  {session_id:22} {len(session.messages):3} 条消息、{session.step_count()} 步"
              + (f"；任务 {todo}" if todo else ""))


def print_skills(loader: SkillLoader) -> None:
    """`--skills`：列出工作区里的技能，不调用模型。

    和 `--list` 同档 —— 它读的是硬盘上的文件，不需要密钥，所以也排在配置检查之前：
    没配密钥的人照样该能查自己写了什么技能。

    它打印两样东西，正好对应人在这台机器上能做的两件事：

      * **认出来的技能** —— 名字、正文长度、声明的工具、以及那句 description（它是模型
        判断"什么时候该用"的唯一依据，所以写得好不好在这里一眼能看出来）；
      * **被跳过的技能** —— 每一条都带着文件名和具体毛病。这是 `--skills` 最要紧的
        输出：一份写错 frontmatter 的文件从启动到会话结束都没有任何症状，它只是**不在**
        模型看到的清单里，不在这里报出来就没人知道该去改哪。

    正文长度值得显示，是因为它直接决定此后每一轮请求的成本（正文会拼进每一次请求）——
    "这个技能值不值 3000 字符"是写技能的人真会问的问题。
    """
    catalog = loader.reload()

    # 目录按优先级从低到高列出来，并标出**真的存在的那些**：用户级目录在工作区外面，
    # 不列出来人根本想不到去那儿找技能；而"哪些还不存在"正好是"要新建一个该放哪"的答案。
    print("技能目录（优先级从低到高，个人级压项目级；✓ = 存在）：")
    for path in loader.directories:
        print(f"  {'✓' if path in catalog.roots else ' '} {path}")

    if not catalog.skills:
        print("\n没有技能。要加一个就在上面任一目录下建 <名字>/SKILL.md，"
              "开头写上 name 和 description（格式见 README 的「技能」一节）。")
    else:
        print(f"\n可用技能 {len(catalog.skills)} 个：")
        for skill in catalog.skills:
            tools = "、".join(skill.allowed_tools) or "（未声明）"
            print(f"  {skill.name:20} 正文 {skill.body_chars:>6} 字符  声明的工具：{tools}")
            print(f"  {'':20} {skill.description}")
            print(f"  {'':20} 来自 {skill.path}")

    for item in catalog.shadowed:
        print(f"  [遮蔽] {loader.render(item, i18n.t)}")
    for problem in catalog.problems:
        print(f"  [跳过] {loader.render(problem, i18n.t)}")


def print_history(session: Session) -> None:
    """把对话过程打出来。内容只留预览 —— messages 里可能躺着上万字符的文件正文。"""
    print(f"会话 {session.session_id!r}：{len(session.messages)} 条消息，{session.step_count()} 步")
    print("-" * 72)
    for i, msg in enumerate(session.messages):
        role = msg["role"]
        preview = str(msg.get("content") or "").replace("\n", "\\n")[:76]

        if role == "assistant":
            if preview:
                print(f"{i:3} {role:9} {preview}")
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                print(f"{i:3} {role:9} → 调用 {fn['name']}({fn['arguments'][:64]})")
        elif role == "tool":
            print(f"{i:3} {role:9} ← {preview}")
        else:
            print(f"{i:3} {role:9} {preview}")


def print_audit(sink: JsonlSink, session_id: str) -> None:
    """渲染审计轨迹，末尾附一份汇总。

    **只报 token，不报钱。** 价格会变、还分高峰/空闲时段，把它固化进输出就等于
    把会变的东西写死。想看钱，拿 token 乘以当时的价目表。
    """
    events = list(sink.read(session_id))
    if not events:
        print(f"会话 {session_id!r} 没有审计记录（{sink.directory}）")
        return

    print(f"会话 {session_id!r} 的审计轨迹：{len(events)} 条")
    print("-" * 78)
    for event in events:
        payload = {
            k: v for k, v in event.items()
            if k not in ("ts", "kind", "session_id", "run_id", "step")
        }
        body = " ".join(f"{k}={v}" for k, v in payload.items())
        # 全部用 .get 而不是下标 —— 理由和 `summarize` 里那段一样，而且更要紧：这个
        # 函数读的日志来自硬盘（会被复制、拼接、手工编辑，进程被杀还会留下半截行），
        # 而 JsonlSink.read 只跳过解析失败的行、不保证每行都完整。少一个键就让整条
        # `--audit` 崩掉，等于把"事后排查"的工具毁在最需要它的场景里。缺的字段显示
        # 成 "?"，让那一行照样打得出来。
        print(f"{str(event.get('ts'))[11:]} {event.get('kind', '?'):<13} "
              f"step={event.get('step', '?'):<3} {body[:100]}")

    _print_audit_summary(events)


def _print_audit_summary(events: list[dict]) -> None:
    usage = summarize(events)

    results = [e for e in events if e.get("kind") == "tool_result"]
    by_status: dict[str, int] = {}
    for event in results:
        by_status[event.get("status", "?")] = by_status.get(event.get("status", "?"), 0) + 1

    print("-" * 78)
    print(f"模型调用 {usage.calls} 次（{usage.ok_calls} 次成功）"
          f"  输入 {usage.prompt} token（命中缓存 {usage.cached} / 未命中 {usage.miss}，"
          f"命中率 {usage.hit_rate}）"
          f"  输出 {usage.completion} token")
    print(f"工具调用 {len(results)} 次  " +
          "  ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    _print_timing(summarize_time(events))
    stops = [e.get("stop_reason") for e in events if e.get("kind") == "run_finished"]
    if stops:
        print(f"回合结束原因  " + "  ".join(stops))
    print("提示：未命中缓存的输入是成本大头（官方价里它比命中贵约 50 倍），"
          "所以「读了多大的文件」比「聊了多少轮」更决定花费。")


# --- 需要模型的部分 -------------------------------------------------------

def _usage_note(events: Iterable[dict]) -> str:
    """末尾那句统计里的用量部分。返回空串表示没什么可报的。

    数字来自**审计事件**，而不是另记一份计数：那样就有了两份事实，早晚不一致 ——
    和 `summarize` 是同一个函数，所以这里报的和 `--audit` 里那份对得上。

    刻意按**会话累计**，和它旁边那个 `step_count()` 一致。累计值才是真实花费：
    每一轮都要为整段历史重付一次输入，那是成本的主项。想知道刚结束那一轮花了多少，
    看这句里的"本轮耗时"和 `--audit` 里逐次的 model_call。
    """
    usage = summarize(events)
    if not usage.prompt:
        # 一次成功的模型调用都没有（比如第一轮就鉴权失败）。硬凑一句"命中率 —"
        # 只是噪声 —— 没东西可报的时候就别说。
        return ""
    # 命中率单列：未命中缓存的输入是成本大头（贵约 50 倍），所以"命中多少"比
    # "总共多少"更值得看一眼。
    return (f"；累计输入 {usage.prompt} token"
            f"（命中缓存 {usage.cached}、命中率 {usage.hit_rate}）")


def _turn_note(events: Iterable[dict]) -> str:
    """末尾那句统计里的**本轮耗时**。

    它旁边那几个数都是**会话累计**（消息数、步数、token），只有这个是刚结束的那一轮 ——
    所以标签必须写明"本轮"。累计时间本来也没什么意义（没人关心这个会话一共聊了多久），
    而累计 token 有意义，因为那是钱：两者不能混在一起说。
    """
    ms = last_turn_ms(events)
    return "" if ms is None else f"；本轮 {_ms_text(ms)}"


def _context_note(events: Iterable[dict], context_tokens: int | None) -> str:
    """末尾那句统计里的**上下文用量**：`上下文 2.0k/1M（0.2%）`。

    两个口径上的事实，README 里也写了：

      * 它是**上一次请求**实际发出去的大小，不是"现在" —— 下一次请求还要加上这一轮的
        回答和工具结果，所以这个数是个**下界**。判断"离窗口还有多远"够用。
      * 它**含命中缓存的那部分**（prompt_tokens 就是全部输入）。看窗口够不够要看总数，
        看钱要看未命中 —— 后者已经在同一行的命中率里了。

    context_tokens 为 None（模型不在 CONTEXT_WINDOWS 里）时**只报用量、不报占比**：
    错的百分比比没有百分比更坏。
    """
    used = last_prompt_tokens(events)
    if used is None:
        return ""
    if not context_tokens:
        # 表里没有这个模型（或表里填了个 0 —— 那不是一个窗口）：**只报用量，不猜分母**，
        # 也就没有百分比。错的百分比比没有百分比更坏。
        return f"；上下文 {_tokens_text(used)}"

    # 超过 100% 照原样报（"120.0%"），不夹到 100：那一轮就是发不出去了，把唯一的线索
    # 抹平只会让人以为"刚好卡住"。
    percent = used / context_tokens * 100
    return (
        f"；上下文 {_tokens_text(used)}/{_tokens_text(context_tokens)}"
        f"（{percent:.1f}%）"
    )


def _stats_note(
    sink: JsonlSink | None,
    session: Session,
    context_tokens: int | None = None,
) -> str:
    """末尾那句统计里由审计日志数出来的部分：累计用量 + 本轮耗时 + 上下文用量。

    **一次读取、三样统计共用同一批事件**，所以它们和 `--audit` 报的数字对得上。
    传进来的 sink 必须**就是** Agent 的 on_event —— 它读的就是那份日志。
    """
    if sink is None:
        return ""
    events = list(sink.read(session.session_id))
    return (
        _usage_note(events)
        + _turn_note(events)
        + _context_note(events, context_tokens)
    )


def _report_jobs(runtime: Runtime) -> None:
    """把后台任务打一行到 stderr；一个都不挂着就什么都不说。

    和 `_report_todos` 同一条路（行式终端里没有常驻面板，所以每轮重打一遍），但**比它
    要紧一档**：任务列表忘了更新只是信息旧了，而后台任务忘了收是**用户机器上一个还在
    跑的进程** —— 它还占着端口，而"端口被占用"这句报错里没有任何线索指向"是上一次
    会话留下的"。

    数据从 `runtime._jobs`（那张表）现取，不由这里算。表不存在（老路径、测试）就什么都
    不说 —— 和 `_report_todos` 在没有列表时的行为一致。
    """
    board = getattr(runtime, "_jobs", None)
    if board is None:
        return
    line = board.progress_line()
    if line:
        print(f"[后台] {line}", file=sys.stderr)


def _report_todos(session: Session, prefix: str = "[任务] ") -> None:
    """把当前任务列表打一行到 stderr；没有列表就什么都不说。

    行式终端里没有"常驻面板"这回事，所以进度只能靠每轮重打一遍。这一行是给**人**看的
    —— 模型每轮看到的是载荷尾部那一份完整列表（见 tools/builtin/todo.py），两者刻意
    不是同一份文本：模型要"还剩什么、现在做哪条"，人只要一眼看出做到哪了。

    走 stderr：和提示符、横幅、每轮末尾那句统计同一条线，stdout 只留对话正文。
    """
    line = progress_line(session.metadata)
    if line:
        print(f"{prefix}{line}", file=sys.stderr)


# --- `/status` `/tools` `/model`（老 CLI 这一支）---------------------------------
#
# **这三条命令是行式的，不是面板**：老 CLI 里没有常驻栏、也没有浮层，所以答案直接
# 打到 stderr（和提示符、每轮那句统计同一条线 —— stdout 只留对话正文，README 承诺
# `> 对话.txt` 拿到的是干净的答案）。
#
# 数据全部来自 **runtime**，一行都不自己算：账从审计日志数（`runtime.logs`），
# 权限判定问策略，模型目录读 `state/model.py`。这一支能直连 runtime（决策 19 的
# 例外），所以它不需要走协议 —— 但**口径必须和 TUI 一致**，所以两边读的是同一批
# 接口（`Runtime.status` / `tool_rows` / `model_rows`）。

def _print_status(runtime: Runtime) -> None:
    """`/status`。和 TUI 那一屏同一份数据，只是排成行。"""
    summary = status_summary.summarize(list(runtime.logs.read(runtime.session_id)))
    data = runtime.status(counts=summary["counters"], usage=summary["usage"])
    session, model = data["session"], data["model"]
    counters, usage, meta = data["counters"], data["usage"], data["meta"]

    print("状态", file=sys.stderr)
    # "这次启动：继续/新建"说的是**这次进程怎么开起来的**，不是这个会话有多满 ——
    # 写成"（继续）"会让"继续一个新会话"读起来自相矛盾，而规模和步数就在下一行。
    print(f"  会话        {session['id']}"
          f"（{'这次启动：继续' if session['resumed'] else '这次启动：新建'}）",
          file=sys.stderr)
    print(f"  工作区      {session['workspace']}", file=sys.stderr)
    print(f"  规模        {session['messages']} 条消息 · {session['steps']} 步",
          file=sys.stderr)
    current = model["current"] or "—"
    if model["selected"] and model["selected"] != model["current"]:
        current += f"（换成 {model['selected']} 的，下一次请求生效）"
    if model.get("provider"):
        # 路由名跟在一起：两条路由可以有同名模型，而"它到底在哪跑"决定账单和合规。
        current += f"  @{model['provider']}"
    print(f"  模型        {current}（{model['base_url']}）", file=sys.stderr)

    # 思考模式那两个旋钮。**关着时不写强度**（写了会让人以为它还生效）。
    thought = model.get("reasoning") or {}
    if thought.get("thinking", True):
        print(f"  思考        开 · {thought.get('effort', '?')}"
              f"（/thinking 开关、/effort 改强度）", file=sys.stderr)
    else:
        print(f"  思考        关（强度 {thought.get('effort', '?')} 记着，"
              f"/thinking on 回来）", file=sys.stderr)

    # 分子是**最近一次请求**（`last_prompt_tokens`），分母是**当前模型**的窗口。
    # 分母为 0/None 时只报用量 —— 错的百分比比没有百分比更坏（和状态栏同一条规矩）。
    used = summary["last_prompt_tokens"]
    if used is None:
        context = "—（还没成功调用过模型）"
    elif model["window"]:
        context = (f"{used}/{model['window']} token"
                   f"（{used / model['window'] * 100:.1f}%）")
    else:
        context = f"{used} token（这个模型的窗口不在目录里，不报占比）"
    print(f"  上下文      {context}", file=sys.stderr)

    if usage.get("prompt"):
        rate = f"{usage['cached'] / usage['prompt']:.0%}"
        print(f"  累计输入    {usage['prompt']} token"
              f"（命中缓存 {usage['cached']}、命中率 {rate}）", file=sys.stderr)
        print(f"  累计输出    {usage['completion']} token", file=sys.stderr)
    else:
        print("  累计用量    还没有成功调用过模型", file=sys.stderr)
    print(f"  轮次        {counters.get('runs', 0)} 轮 · "
          f"{counters.get('model_calls', 0)} 次模型调用 · "
          f"{counters.get('tool_calls', 0)} 次工具调用"
          f"（其中审批 {counters.get('permission_waits', 0)} 次、"
          f"提问 {counters.get('asks', 0)} 次）", file=sys.stderr)
    print(f"  工具        {meta['tool_count']} 个（/tools 看清单）", file=sys.stderr)
    print(f"  这次运行    最多 {meta['max_steps']} 步 · "
          f"{'流式' if meta['stream'] else '非流式'} · "
          f"{'自动放行' if meta['autopilot'] else '逐条审批'}", file=sys.stderr)
    print(f"  审计        {meta['audit_path']}", file=sys.stderr)


def _print_tools(runtime: Runtime) -> None:
    """`/tools`。工具名、风险、**会不会问你** —— 三列，和 TUI 那份清单同一个来源。"""
    rows = runtime.tool_rows()
    if not rows:
        print("这次运行一个工具都没注册（缺引擎/密钥时会这样，启动那几行里有原因）",
              file=sys.stderr)
        return
    wording = {"auto": "自动放行", "ask": "需要审批", "deny": "直接拒绝"}
    width = max(len(row["name"]) for row in rows)
    print("可用工具", file=sys.stderr)
    for row in rows:
        marks: list[str] = []
        if row["granted"]:
            marks.append("按过 t")
        if row["external"]:
            marks.append("外部")
        if row["interactive"]:
            marks.append("会问你")
        elif row["parallel_safe"]:
            marks.append("可并发")
        mark = ("  ·  " + "、".join(marks)) if marks else ""
        print(f"  {row['name']:<{width}}  {row['risk']:<7}"
              f"{wording.get(row['disposition'], row['disposition'])}{mark}",
              file=sys.stderr)
    prefixes = [format_rule(rule) for rule in sorted(runtime.memory.prefixes())]
    if prefixes:
        print(f"  命令规则（按前缀放行，只对 shell 这类有命令行的工具生效）："
              f"{'、'.join(prefixes)}", file=sys.stderr)
    print("  改这些去 .tudouni/permissions.json；审批时按 t 会写进去", file=sys.stderr)


def _print_mcp(runtime: Runtime) -> None:
    """`/mcp` 不带参数：挂载情况 + 怎么改。

    **和 TUI 那份面板同一个数据来源**（`runtime.mcp.rows()`）：两边共享的是数据口径，
    不是交互（TUI 弹面板、这里打行）。口径里有一件事这里必须也说清：这一屏里的
    `unload` 那些**可能是从没挂过，也可能是刚才卸掉的** —— 而"为什么没连上"只在
    `failed` 那一档里，跟着 `error` 一起打出来。
    """
    host = runtime.mcp
    if host is None:
        print("这一版没有 MCP 宿主（不是从 open_runtime 起来的？）", file=sys.stderr)
        return
    rows = host.rows()
    if not rows:
        print(f"没有配置任何 MCP server（清单在 {MCP_FILE}）", file=sys.stderr)
        print("配一个 server：本地给 command，远程给 url + headers",
              file=sys.stderr)
        return
    loaded = [row for row in rows if row["state"] == "loaded"]
    width = max(len(row["name"]) for row in rows)
    print(f"MCP server：{len(loaded)} 个在跑 / 共 {len(rows)} 个"
          f"（清单：{MCP_FILE}）", file=sys.stderr)
    for row in rows:
        if row["state"] == "loaded":
            detail = f"{row['tools']} 个工具"
        elif row["state"] == "failed":
            detail = f"没连上：{row['error']}"
        else:
            detail = "未加载"
        print(f"  {MCP_MARK.get(row['state'], '·')} {row['name']:<{width}}  {detail}",
              file=sys.stderr)
        # 来源那一行**只有未加载时才有用**：在跑的那些已经说了工具数，而这一行
        # 回答的是"它到底在哪"（本地命令 / 远程主机）。远程那句不带令牌。
        if row["state"] != "loaded":
            print(f"      {'':<{width}}  {row['where']}", file=sys.stderr)
    print("  挂一个：/mcp load <名字>  ·  卸一个：/mcp unload <名字>"
          "（一次一个，不改配置文件）", file=sys.stderr)


# MCP 三个状态在 CLI 里的记号。**和 TUI 那一套是同一套形状**（● 在跑 / ○ 没跑 /
# ✗ 试过没成），因为它们是同一份数据、同一批含义 —— 两个前端用不同的符号只会让
# "用户在两个界面之间对不上号"。
MCP_MARK = {"loaded": "●", "unload": "○", "failed": "✗"}


def _mcp_action(runtime: Runtime, action: str, name: str) -> None:
    """`/mcp load|unload <名字>`：真改，然后把宿主说的那句话原样打出来。

    那句话由 `McpHost` 拼（`[MCP] server x 挂上了：12 个工具` / `没连上（…）`）——
    **CLI 不自己拼**：它包含"为什么没成"这类只有宿主知道的事实，而两个前端各拼一份
    就会漂（TUI 那边贴的是同一句话）。
    """
    host = runtime.mcp
    if host is None:
        print("这一版没有 MCP 宿主，改不了挂载", file=sys.stderr)
        return
    if action == "load":
        message = host.load(name)
    else:
        message = host.unload(name)
    print(message, file=sys.stderr)
    # 改完再列一遍：`load` 之后"工具数是多少"、`unload` 之后"还剩几个在跑"都是
    # 紧接着会想知道的事，而再打一次 `/mcp` 只是为了看这两行。
    _print_mcp(runtime)


def _print_models(runtime: Runtime) -> None:
    """`/model` 不带参数：清单 + 现在用的是哪个。

    名字写成 `provider/model` —— 同名模型可以在多条路由上，而只写模型名的话那两行
    长得一模一样，可"选了哪一个"决定了请求发到哪个账号上。
    """
    rows, aliases = runtime.model_rows()
    here = runtime.current_route or "—"
    print(f"当前模型：{here}", file=sys.stderr)
    names = [f"{row['provider']}/{row['id']}" if row.get("provider") else row["id"]
             for row in rows]
    width = max(len(name) for name in names) if names else 0
    for row, name in zip(rows, names):
        mark = "●" if row["current"] else " "
        window = f" · 上下文 {row['window']}" if row["window"] else ""
        print(f"  {mark} {name:<{width}}  {row['summary']}"
              f"   （{row['label']}{window}）", file=sys.stderr)
        if row["note"]:
            print(f"      {row['note']}", file=sys.stderr)
    for alias in aliases:
        # 旧名字单列：它们是**认下的名字**，不是能选的选项（官方已把对应模型下线，
        # 请求由新模型提供服务）。列进主清单会摆出两个效果一样的选项。
        print(f"  认下的旧名字：{alias['id']} → {alias['of']}", file=sys.stderr)
    print("换一个：/model <名字>（名字要精确；两条路由同名时写 provider/model）",
          file=sys.stderr)


def _print_thinking(runtime: Runtime) -> None:
    """`/thinking` 不带参数：现在是开还是关（**两个状态都要写出来**）。"""
    thinking = runtime.agent.thinking
    effort = runtime.agent.effort
    print(f"思考模式：{'开' if thinking else '关'}"
          + (f" · 强度 {effort}" if thinking else f"（强度 {effort} 记着，打开才用得上）"),
          file=sys.stderr)
    print("改：/thinking on   ·   /thinking off", file=sys.stderr)


def _print_effort(runtime: Runtime) -> None:
    """`/effort` 不带参数：现在是哪一档、有哪几档。"""
    print(f"思考强度：{runtime.agent.effort}"
          + ("" if runtime.agent.thinking else "（思考关着，打开才用得上）"),
          file=sys.stderr)
    print(f"可选：{'、'.join(reasoning.EFFORT_LEVELS)}", file=sys.stderr)
    print(f"改：/effort {'  ·  /effort '.join(reasoning.EFFORT_LEVELS)}", file=sys.stderr)


def _handle_slash_command(runtime: Runtime, line: str) -> bool:
    """`/` 开头的行：是命令就执行并返回 True，不是就返回 False（当普通输入）。

    **只有这几条**，而且**不做前缀模糊匹配**（`/mod` 不是 `/model`）：这个循环里
    多认一个前缀的代价是真实的 —— 用户打了一句以 `/` 开头的话（路径、正则）会被
    当成命令吃掉，而它看起来只是"我这句话没发出去"。

    所以**认不出来的 `/` 开头的行原样当输入发给模型**，而不是回一句"没有这个命令"
    然后丢掉：丢掉一句用户真的想说的话，比把一次打错字送给模型贵得多（一次往返 vs.
    一句提示）。代价说白：`/stauts` 会被当成话发给模型，而模型多半会回一句"你是不是
    想用 /status"。这一支里没有命令面板去兜这个错，所以这个取舍是刻意的。

    它和 TUI 那套 `/` 命令是**各自实现的**，不是同一个注册表：TUI 那套走协议、
    有面板和浮层，这一支直连 runtime、只有行。两者共享的是**数据口径**
    （`Runtime.status` / `tool_rows` / `model_rows`），不是交互。
    """
    text = line.strip()
    if not text.startswith("/"):
        return False
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command == "/status":
        _print_status(runtime)
    elif command == "/tools":
        _print_tools(runtime)
    elif command == "/mcp":
        # **两种写法，一个出口**：不带参数只列（`/mcp`），带参数就真改一个
        # （`/mcp load github`）。**没有 `all`** —— 和 TUI 那条命令一字不差的规矩：
        # 批量会把"哪几个成了、哪几个没成"揉成一句话，而那句话正是要看的。
        #
        # 名字打错时**不猜**（和 `/model` 同一个取向）：挂上一个别的 server 的后果是
        # "它开始用外面的东西"，比"没挂上"贵得多。所以只认 load / unload 两个字面量。
        if not rest:
            _print_mcp(runtime)
        else:
            action, _, name = rest.partition(" ")
            if action in ("load", "unload") and name.strip():
                _mcp_action(runtime, action, name.strip())
            else:
                print(f"认不出这个写法：{rest}（用 /mcp load <名字> 或 "
                      f"/mcp unload <名字>）", file=sys.stderr)
                _print_mcp(runtime)
    elif command == "/model":
        if rest:
            ok, message = runtime.select_model(rest)
            print(("[模型] " if ok else "[模型] 没换：") + message, file=sys.stderr)
            if not ok:
                _print_models(runtime)
        else:
            _print_models(runtime)
    elif command == "/thinking":
        if rest:
            # 认哪些写法（on/off/开/关/true/false…）由 `state/reasoning.py` 说了算 ——
            # 这一支只管把字符串发过去、把回包原样打出来。少一处会漂的知识。
            if not _apply_thinking(runtime, rest):
                _print_thinking(runtime)
        else:
            _print_thinking(runtime)
    elif command == "/effort":
        if rest:
            ok, message = runtime.select_effort(rest)
            print(("[思考] " if ok else "[思考] 没改：") + message, file=sys.stderr)
            if not ok:
                _print_effort(runtime)
        else:
            _print_effort(runtime)
    else:
        return False
    return True


def _apply_thinking(runtime: Runtime, text: str) -> bool:
    """`/thinking <写法>`：把它折算成 on/off 再改。认不出来返回 False。

    折算走 `reasoning.resolve_thinking`（它认 on/off/开/关/true/false 这些**给人写的**
    词）—— 一个只认 `true` 的命令在中文界面里是荒谬的，而那张词表只该有一份。
    """
    on = reasoning.resolve_thinking(text)
    if on is None:
        print(f"[思考] 认不出这个写法：{text}", file=sys.stderr)
        return False
    ok, message = runtime.select_thinking(on)
    print(("[思考] " if ok else "[思考] 没改：") + message, file=sys.stderr)
    return ok


def run_repl(runtime: Runtime) -> None:
    """多轮对话循环。

    这个 while 刻意留在 Agent 外面：Agent 的契约是"一个回合"，多轮循环属于驱动层，
    因为它的形态随环境而变（CLI 是循环、Web 是每请求一次、测试是遍历列表）。
    把循环塞进 Agent，它就得知道"从哪读用户输入"。

    **它收一个 `Runtime` 而不是五个散参数**（第零期之后）：那些参数全都是 Runtime
    的字段，而"从哪拿会话、从哪读审计"只有一个答案。散着传的代价是真实的 —— 以前
    调用点得记住"logs 必须是同一个 sink，否则数字对不上"，改成 Runtime 之后那件事
    在类型上就成立了。
    """
    agent, session, session_id = runtime.agent, runtime.session, runtime.session_id
    sink, context_tokens = runtime.logs, runtime.context_tokens

    print("输入内容回车发送。空行、exit、quit 或 Ctrl+C 退出。", file=sys.stderr)
    print("（/status 看状态、/tools 看工具与权限、/model 换模型、"
          "/thinking 开关思考、/effort 改强度；其他 / 开头的行会原样发给模型）",
          file=sys.stderr)
    print(file=sys.stderr)
    while True:
        try:
            # 提示符写 stderr，和 cli_asker 保持一致：stdout 只留给 Agent 的产出，
            # 这样 `python main.py > 对话.txt` 拿到的就是干净的答案。
            print("> ", end="", file=sys.stderr, flush=True)
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break

        if not line or line.lower() in {"exit", "quit"}:
            break

        # `/` 命令**在调用模型之前**拦下来：它们是"操作这个会话"，不是"对它说一句话"。
        # 认不出来的 `/` 开头的行**原样当输入发给模型**（见 `_handle_slash_command`
        # 里那段：路径和正则也会以 `/` 开头，吃掉它们是最坏的那种"贴心"）。
        if _handle_slash_command(runtime, line):
            continue

        print(f"\n--- 用户输入: {line} ---\n")
        try:
            print(agent.run(session, line))
        except StepLimitExceeded as exc:
            # 撞上限既不是失败也不是回答，所以两条路都不走：**不走 stdout**（否则
            # `> 对话.txt` 会把它当成答案的一部分），也不结束会话。会话在撞上限那
            # 一刻是一致的，所以接着聊就行 —— 这句"能接着跑"是它和下面两类失败
            # 最大的区别。
            print(f"\n[本轮未收尾 · 步数用尽] {exc}", file=sys.stderr)
            print(f"  接着跑：--session {session_id}", file=sys.stderr)
            # 撞上限时"还剩什么"是最该说的一句：任务列表本来就记着它，而这时候用户
            # 手上唯一的问题正是"还差多少"。它跨进程活着，所以下一次接着跑时模型也
            # 会在请求尾部看到同一份列表。
            _report_todos(session, prefix="  未做完的：")
        except RunCancelled as exc:
            # 取消**必须在这里被接住**：它继承 `BaseException`（理由见那个类），
            # 所以不接的话它会穿透到顶层，把进程干掉 —— 而 CLI 这一支**没有**注入
            # `should_stop`，所以它本来不可能被取消。仍然接住是因为"不可能"会变：
            # 有一天给 CLI 加上停止键，漏了这一支的症状是"按了停止，整个程序退了"。
            print(f"\n[本轮已取消] {exc}", file=sys.stderr)
        except ModelFatalError as exc:
            # 重试没有意义的那类失败（鉴权、模型名、请求格式）—— 告诉用户原因，
            # 但**不退出**：一个回合失败不等于整个会话结束。
            print(f"\n[本轮失败 · 不可重试] {exc}", file=sys.stderr)
        except ModelTransientError as exc:
            # 退避重试都用完了。会话状态是完好的（模型失败的时机总在一致点），
            # 所以可以直接再试一次，或者退出后用 --session 继续。
            print(f"\n[本轮失败 · 重试后仍失败] {exc}", file=sys.stderr)

        # 这行是**统计**，不是对话，所以走 stderr —— 和提示符、横幅、步数用尽那句
        # 同一条线。stdout 只留"用户问 + Agent 答"的对话正文，`> 对话.txt` 拿到的
        # 才真是能回头读的东西；token 数字混进那份文件，就是往答案里掺元数据。
        # 这一行只说"刚才发生了什么"：会话 id、规模、累计用量、本轮耗时。**不再复述怎么
        # 续聊** —— 新会话在启动时已经说过一次，恢复会话时启动那行也带着 id，
        # 每轮再刷一遍只是把同一句话说三十遍。
        #
        # 步数用尽那条路仍然会说"接着跑：--session X"：那里的意思是"这一轮没走完"，
        # 和"你随时可以回来"是两件事，它每次也只在那一种情况下出现。
        _report_todos(session)
        _report_jobs(runtime)
        print(f"\n（会话 {session_id!r}：{len(session.messages)} 条消息、"
              f"{session.step_count()} 步{_stats_note(sink, session, context_tokens)}。）\n",
              file=sys.stderr)
