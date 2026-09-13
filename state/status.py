"""`/status` 要的那几笔账：**从审计事件里数出来，不另记一份计数。**

## 为什么口径只能是审计

这个项目里"这一轮花了多少"此前只有一条出口：`--audit`（读 `.tudouni/logs/<id>.jsonl`）。
`/status` 用的是**同一批事件、同一套算法**（`summarize()`，`frontends/cli` 里那个），
所以它报出来的 token 和 `--audit` 报的一定对得上。

另记一份累加计数当然更快，但那是**同一份事实的第二个来源** —— 它会漂，而且漂的方式
没人查得出来：状态栏说"累计 12 万 token"，审计里逐条加起来是 13 万，两边看起来都正常。
代价写在明处：每次 `/status` 都要把那个 jsonl 读一遍（一次会话的量级是几十~几百行）。

## 轮次计数为什么算 `run_started`

一次回合 = 一条 `run_started`。**不能拿 `run_finished` 数**：被中断的那一轮（Esc）
同样是"跑过的一轮"，而它照样有 `run_finished`（stop_reason=cancelled）—— 两者在
这个数上恰好一样，但用 `run_started` 更直接：它是"这一轮开始了"，而这个数回答的是
"用户按过几次回车"。

`model_calls` 数的是**所有** model_call（含重试里失败的那几次），`tool_calls` 数的是
每一次 tool_result —— 因为 `/status` 回答的是"跑了多少活"，不是"成功了几次"。
成功率在 `--audit` 里逐条看得到，塞进这一行只会让它读不出来。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# 上下文占比的显示口径和 `frontends/cli` 那条统计**必须一致**：同一个数在这个界面
# 报一位小数、在另一个界面报整数，用户会以为其中一个错了。
PERCENT_DECIMALS = 1


def _number(value: Any) -> int:
    """事件里的数字。**取不出来就是 0，绝不抛** —— 见下面 `summarize` 的说明。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def summarize(events: Iterable[dict]) -> dict[str, dict[str, int]]:
    """审计事件 → `{"usage": {...}, "counters": {...}}`。**纯函数。**

    ## 只累加成功的调用

    重试里失败的那几次尝试**没有 usage 字段**（`agents/retry.py` 里 error / fatal
    两种尝试都不带 response），所以它们对 token 各项贡献 0。这不是"漏了"，而是
    "算不出" —— 而"花了多少"这个问题只对成功的调用有答案。

    ## 缺字段一律当 0

    和 `cli.summarize` 同一条规矩（那里写着理由）：日志会被复制、拼接、截断，少一个
    键不该让一次 `/status` 崩掉。而且这里读的是**本进程之外**的文件，坏行是常态。

    ## `asks` / `permission_waits` 记的是次数

    它们不是 token 也不是耗时，而是"这个会话被人的决定挡住过几次"。放在同一份里是因为
    `/status` 那一屏需要它们一起出现（"跑了 12 次工具调用"和"其中 3 次问过你"分开两处
    说，读者就得自己对）。
    """
    runs = 0
    model_calls = 0
    model_ok = 0
    tool_calls = 0
    permission_waits = 0
    asks = 0
    prompt = 0
    cached = 0
    miss = 0
    completion = 0
    last_prompt: int | None = None

    for item in events:
        kind = item.get("kind")
        if kind == "run_started":
            runs += 1
        elif kind == "model_call":
            model_calls += 1
            if item.get("status") == "ok":
                model_ok += 1
                prompt += _number(item.get("prompt_tokens"))
                cached += _number(item.get("cached_tokens"))
                miss += _number(item.get("miss_tokens"))
                completion += _number(item.get("completion_tokens"))
                # "上一次请求实际发出去多少" = **最后一条成功调用的输入**。
                #
                # 按成功算、不按"最后一条"算：失败的那次没有这个字段，而它后面还可能
                # 有成功的一次；把失败的那条当成"最后一次"会把分母报成一个陈旧的值。
                last_prompt = _number(item.get("prompt_tokens"))
        elif kind == "tool_result":
            tool_calls += 1
            if item.get("question_status"):
                asks += 1
        elif kind == "permission":
            permission_waits += 1

    return {
        "usage": {
            "prompt": prompt,
            "cached": cached,
            "miss": miss,
            "completion": completion,
        },
        "counters": {
            "runs": runs,
            "model_calls": model_calls,
            "model_ok": model_ok,
            "tool_calls": tool_calls,
            "permission_waits": permission_waits,
            "asks": asks,
        },
        "last_prompt_tokens": last_prompt,
    }


def hit_rate(prompt: int, cached: int) -> str:
    """命中率的显示形式。**没有输入 token 时是 `—`，不是 `0%`。**

    0% 会让人以为"缓存白白配错了"，而事实是一次缓存查询都还没发生过。这两种情况
    必须能分辨出来 —— 和 `frontends/cli` 的 `Usage.hit_rate` 是同一条理由，所以
    这里的措辞也照它。
    """
    return f"{cached / prompt:.0%}" if prompt else "—"


def tokens_text(count: int | None) -> str:
    """token 数的短写法（`2.0k` / `1.0M`）。

    和 TUI 状态栏 / CLI 那一行**同一套**（`frontends/tui/view_state.tokens_text`）：
    同一个数在三个地方显示成三种样子，用户会以为它们是三个数。刻意不 import 那边的
    实现 —— 那是前端的排版函数，而这里给出的是"账"。
    """
    if count is None:
        return "—"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def context_text(used: int | None, window: int | None) -> str:
    """`上下文 2.0k / 1.0M（0.2%）`。

    两条口径上的事实（README 里也写着）：

      * 它是**上一次请求**实际发出去的大小，不是"现在" —— 下一次请求还要加上这一轮的
        回答和工具结果，所以这个数是个**下界**；
      * `window` 为 None（模型不在目录里）时**只报用量、不报占比**：错的百分比比没有
        百分比更坏，它会被当成真的。超过 100% 也照实报，不夹平。
    """
    if used is None:
        return "—"
    if not window:
        return tokens_text(used)
    return (f"{tokens_text(used)} / {tokens_text(window)}"
            f"（{used / window * 100:.{PERCENT_DECIMALS}f}%）")


__all__ = ["PERCENT_DECIMALS", "context_text", "hit_rate", "summarize", "tokens_text"]
