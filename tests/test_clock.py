"""时间工具：格式能还原成时刻，且不需要任何参数。

这里**不能**断言"现在具体是几点"—— 那等于把测试绑死在运行的那一刻上。能稳定
断言的是两件事：字符串的**形状**（ISO 8601、带偏移、秒级），以及它的**含义**
（解析回来确实就是"现在"）。后者才是有价值的那个 —— 时区偏移写错时，它会差出
好几个小时，而只对形状的正则完全看不出来。
"""

import re
from datetime import datetime

from agent_runtime.tools.builtin import create_tool_registry
from agent_runtime.tools.builtin.clock import get_current_time
from agent_runtime.tools.tool import RiskLevel


def test_returns_iso_8601_with_offset():
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
        get_current_time(),
    )


def test_parses_back_to_now():
    """偏移必须是**对**的，不只是"存在"。裸的 datetime.now() 会漏掉偏移，
    那样解析出来的时刻和现在能差好几个小时 —— 这条专门盯它。"""
    delta = datetime.fromisoformat(get_current_time()) - datetime.now().astimezone()
    assert abs(delta.total_seconds()) < 5


def test_tool_takes_no_arguments_and_is_low_risk():
    """schema 里没有任何参数 —— 时间不需要模型输入，也就没有填错的可能。"""
    tool = create_tool_registry(".").get("get_current_time")

    assert tool.parameters["properties"] == {}
    assert "required" not in tool.parameters
    assert tool.risk is RiskLevel.LOW


def test_empty_arguments_still_execute():
    """无参工具的 execute 必须能吃下 {} —— 模型发起调用时给的就是空参数对象。"""
    tool = create_tool_registry(".").get("get_current_time")
    assert datetime.fromisoformat(tool.execute({}))
