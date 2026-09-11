"""命令行界面：参数解析、会话选择、交互循环、历史与审计的展示。

和 main.py 分开，是因为**它们的变化原因不同**：main.py 关心"把哪些部件接起来"，
改动来自架构演进；这里关心"怎么跟用户说话"，改动来自使用习惯。放在一个文件里，
两种改动的 diff 会互相淹没。

顺带解决了一个具体的别扭：以前"新会话 X"是在配置检查之前打印的，所以没配
DEEPSEEK_API_KEY 时会先看到"新会话"再看到报错。现在子命令分两段 —— 不需要模型
的（--list / --history / --audit）先处理，需要模型的才读配置。
"""

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from agent_runtime.agents import StepLimitExceeded
from agent_runtime.audit import JsonlSink
from agent_runtime.models.types import ModelFatalError, ModelTransientError
from agent_runtime.state import JsonSessionStore, Session


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Runtime 命令行入口")
    parser.add_argument(
        "--session", default=None,
        help="会话 id。给了就接着那个会话聊（不存在则新建）；不给就自动开一个新的",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="打印 --session 指定会话的对话历史，不调用模型",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="打印 --session 指定会话的审计轨迹（token、权限裁决、耗时），不调用模型",
    )
    parser.add_argument("--list", action="store_true", help="列出已保存的会话")
    parser.add_argument(
        "--autopilot", action="store_true",
        help="不询问任何审批：需要审批的工具直接执行。拒绝名单、工作区边界、控制面写入"
             "仍然生效；审计里每次放行记为 outcome=autopilot",
    )
    parser.add_argument("--debug", action="store_true", help="把中间过程打到 stderr")
    return parser


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
    模型往返（每次尝试一条）、工具执行（不含等人审批）、等人审批、重试退避。
    口径重叠过一次的教训写在这里 —— 工具耗时原本从函数入口起表，把等人审批也算了
    进去，于是它几乎等于人的思考时间，而那两个数一相加还会重复。
    """

    run_ms: int          # 各回合墙上时间之和（run_finished.duration_ms）
    model_ms: int
    tool_ms: int
    waited_ms: int
    backoff_ms: int

    @property
    def explained_ms(self) -> int:
        return self.model_ms + self.tool_ms + self.waited_ms + self.backoff_ms

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
    def total(kind: str, field: str) -> int:
        return sum(e.get(field, 0) for e in events if e.get("kind") == kind)

    return Timing(
        run_ms=total("run_finished", "duration_ms"),
        model_ms=total("model_call", "duration_ms"),
        tool_ms=total("tool_result", "duration_ms"),
        waited_ms=total("permission", "waited_ms"),
        backoff_ms=total("model_call", "backoff_ms"),
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

    要用它的地方量级差得很远：一轮的用量可能几千，而窗口是百万级。所以 1k 以下给精确值，
    10k 以下给一位小数，再往上给整数 k，百万以上给 M —— 占比这种东西不需要四位有效数字。
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
    if not (timing.model_ms or timing.tool_ms or timing.waited_ms):
        return

    parts = [
        f"模型 {_ms_text(timing.model_ms)}",
        f"工具 {_ms_text(timing.tool_ms)}",
    ]
    # 没发生过的事不占位置：没人被问过审批时，那一项只是 0ms 的噪声。
    if timing.waited_ms:
        parts.append(f"等人审批 {_ms_text(timing.waited_ms)}")
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
        print(f"  {session_id:22} {len(session.messages):3} 条消息、{session.step_count()} 步")


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
        print(f"{event['ts'][11:]} {event['kind']:<13} step={event['step']:<3} {body[:100]}")

    _print_audit_summary(events)


def _print_audit_summary(events: list[dict]) -> None:
    usage = summarize(events)

    results = [e for e in events if e["kind"] == "tool_result"]
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
    stops = [e.get("stop_reason") for e in events if e["kind"] == "run_finished"]
    if stops:
        print(f"回合结束原因  " + "  ".join(stops))
    print("提示：未命中缓存的输入是成本大头（官方价里它比命中贵约 50 倍），"
          "所以「读了多大的文件」比「聊了多少轮」更决定花费。")


# --- 需要模型的部分 -------------------------------------------------------

def resolve_session(store: JsonSessionStore, session_id: str | None) -> tuple[str, Session]:
    """决定这次聊哪个会话：给了 id 就接着（不存在则新建），没给就自动开一个新的。

    注意这里还不会落盘 —— 第一次写盘发生在你说出第一句话之后（Agent 在把用户
    消息追加进 messages 之后才触发 checkpoint）。所以"开了不用"不会留下空文件。
    """
    if session_id:
        if store.exists(session_id):
            session = store.load(session_id)
            print(f"继续会话 {session_id!r}：{len(session.messages)} 条消息")
        else:
            session = Session.new(session_id)
            print(f"新建会话 {session_id!r}")
        return session_id, session

    session_id = store.new_session_id()
    print(f"新会话 {session_id!r}（说出第一句话之后才会落盘）")
    print(f"  想回来继续它：  --session {session_id}")
    return session_id, Session.new(session_id)


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


def run_repl(
    agent,
    session: Session,
    session_id: str,
    sink: JsonlSink | None = None,
    context_tokens: int | None = None,
) -> None:
    """多轮对话循环。

    这个 while 刻意留在 Agent 外面：Agent 的契约是"一个回合"，多轮循环属于驱动层，
    因为它的形态随环境而变（CLI 是循环、Web 是每请求一次、测试是遍历列表）。
    把循环塞进 Agent，它就得知道"从哪读用户输入"。

    sink 和 context_tokens 只影响每轮末尾那行统计（见 `_stats_note`）：模型窗口为 None
    时就只报用量、不报占比。两者都不影响对话本身，缺了只是少几个字。
    """
    print("输入内容回车发送。空行、exit、quit 或 Ctrl+C 退出。\n")
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
        # 续聊** —— 新会话在启动时已经说过一次（resolve_session 里那句"想回来继续它"），
        # 恢复会话时启动那行也带着 id，每轮再刷一遍只是把同一句话说三十遍。
        #
        # 步数用尽那条路仍然会说"接着跑：--session X"：那里的意思是"这一轮没走完"，
        # 和"你随时可以回来"是两件事，它每次也只在那一种情况下出现。
        print(f"\n（会话 {session_id!r}：{len(session.messages)} 条消息、"
              f"{session.step_count()} 步{_stats_note(sink, session, context_tokens)}。）\n",
              file=sys.stderr)
