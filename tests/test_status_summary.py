"""`/status` 那几笔账：`state/status.py` 的纯函数。

它和 `frontends/cli` 的 `summarize` 回答同一个问题（"这批事件花了多少"），而口径
必须一致 —— 所以这里钉的是**算错也不会报错**的那几处：失败的尝试算不算、缺字段怎么
办、没有分母时报什么。
"""

import json

from agent_runtime.state import status as status_mod


def _call(**fields) -> dict:
    base = {"kind": "model_call", "status": "ok", "prompt_tokens": 100,
            "cached_tokens": 80, "miss_tokens": 20, "completion_tokens": 7}
    base.update(fields)
    return base


def test_only_successful_calls_contribute_to_the_bill():
    """重试里失败的那几次**没有 usage 字段**（`agents/retry.py`），所以它们对账贡献 0。

    这不是"漏了"，而是"算不出" —— 而"花了多少"这个问题只对成功的调用有答案。
    把它们记进去只会把命中率算低。
    """
    summary = status_mod.summarize([
        _call(status="error", prompt_tokens=0, cached_tokens=0, completion_tokens=0),
        _call(),
    ])
    assert summary["counters"]["model_calls"] == 2
    assert summary["counters"]["model_ok"] == 1
    assert summary["usage"] == {"prompt": 100, "cached": 80, "miss": 20, "completion": 7}


def test_missing_fields_count_as_zero_instead_of_exploding():
    """日志会被复制、拼接、截断 —— 少一个键不该让一次 `/status` 崩掉。

    它读的是**本进程之外**的文件，所以坏行是常态而不是意外。
    """
    summary = status_mod.summarize([
        {"kind": "model_call"},                      # 什么字段都没有
        {"kind": "model_call", "status": "ok", "prompt_tokens": "十"},   # 类型也不对
        {"kind": "run_started"},
        {"kind": "tool_result"},
        {"kind": "permission"},
        {},                                          # 连 kind 都没有
    ])
    assert summary["usage"]["prompt"] == 0
    assert summary["counters"] == {
        "runs": 1, "model_calls": 2, "model_ok": 1, "tool_calls": 1,
        "permission_waits": 1, "asks": 0,
    }


def test_the_context_number_is_the_last_successful_request():
    """`last_prompt_tokens` 是**上一次成功请求**的输入，不是"最后一条 model_call"。

    失败的那条没有这个字段，而它后面还可能有一次成功的 —— 把失败那条当"最后一次"
    会报出一个陈旧的分母，而它看起来完全正常。
    """
    summary = status_mod.summarize([
        _call(prompt_tokens=100),
        {"kind": "model_call", "status": "error"},
        _call(prompt_tokens=250),
    ])
    assert summary["last_prompt_tokens"] == 250
    # 一次成功的都没有 → None（界面据此说"还没成功调用过模型"，不报一个 0）。
    assert status_mod.summarize([])["last_prompt_tokens"] is None


def test_asks_are_counted_by_the_question_status_field():
    """`ask_user` 的那条 `tool_result` 多带一个 `question_status` —— 靠它区分。

    数它是因为"这个会话被人的决定挡住过几次"和"跑了多少次工具调用"是两个问题，
    而后者会把提问淹没在几十次 read_file 里。
    """
    summary = status_mod.summarize([
        {"kind": "tool_result", "tool": "read_file"},
        {"kind": "tool_result", "tool": "ask_user", "question_status": "answered"},
    ])
    assert summary["counters"]["tool_calls"] == 2
    assert summary["counters"]["asks"] == 1


def test_hit_rate_says_dash_when_there_is_nothing_to_divide():
    """没有输入 token 时是 `—`，不是 `0%`。

    0% 会让人以为"缓存白白配错了"，而事实是一次缓存查询都还没发生过。这两种情况
    必须能分辨出来（和 `frontends/cli` 的 `Usage.hit_rate` 同一条理由）。
    """
    assert status_mod.hit_rate(0, 0) == "—"
    assert status_mod.hit_rate(100, 88) == "88%"


def test_token_text_matches_the_status_bar():
    """三个界面（TUI 状态栏 / TUI 的 `/status` / CLI）报同一个数时得长得一样。"""
    assert status_mod.tokens_text(None) == "—"
    assert status_mod.tokens_text(999) == "999"
    assert status_mod.tokens_text(2_035) == "2.0k"
    assert status_mod.tokens_text(1_000_000) == "1.0M"


def test_the_summary_is_json_serializable():
    """它要进协议（`ui(status)` 的 `counters` / `usage`），所以必须是平常数据。"""
    json.dumps(status_mod.summarize([_call()]), ensure_ascii=False)
