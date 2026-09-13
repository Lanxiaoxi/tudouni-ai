"""模型目录与路由配置（`state/catalog.py`）+ 思考开关与强度（`state/reasoning.py`）。

这两块合在一起测，是因为它们回答同一个问题的两半：**这台机器上能选什么**
（目录）和**选了之后怎么想**（思考那两个旋钮）。它们各自最容易错的地方也是同一类：
**默认值猜错、名字认错，而两种错都不会报错** —— 只会静默地按另一个模型/另一档强度跑。

配置文件那部分的测试全部**自己造文件**（`workdir` 下的临时 json），不碰仓库里真的
`models.local.json` —— 那是个人的东西，而测试要看的是"给一份配置会发生什么"。
"""

import json

import pytest

from agent_runtime.runtime.config import CONTEXT_WINDOWS
from agent_runtime.state import catalog, reasoning


# --- 内置目录（没有配置文件时）--------------------------------------------------

def test_the_builtin_directory_is_derived_into_the_window_table():
    """`CONTEXT_WINDOWS` 和内置目录**必须同源**。

    它们各写一份的后果是静默的：往配置里加一个模型（`/model` 立刻列出它），而窗口表
    没跟上，于是"选了它之后状态栏不报占比" —— 两处都不会报错，只是那个百分比消失了。
    """
    registry = catalog.load()
    assert registry.providers, "至少要有内置那一条路由"
    for item in registry.models():
        assert CONTEXT_WINDOWS[item.id] == item.window


def test_legacy_names_are_recognised_but_not_offered():
    """旧模型名**认，但不列进 `/model` 的清单**。

    官方明确说过那些旧名字对应的模型已下线、请求由新模型提供服务。所以：
      * 认它们 —— 别人的 `DEEPSEEK_MODEL` 里可能就写着它们，而"昨天配的名字今天不能
        用"是我们不该制造的意外；
      * 不列它们 —— 摆出两个效果一样、价钱也一样的选项，是在骗选的人。
    """
    registry = catalog.load()
    ids = {item.id for item in registry.models()}
    for alias in catalog.ALIASES:
        assert alias not in ids
        assert registry.find(alias) is not None
        assert CONTEXT_WINDOWS[alias] == registry.find(alias).window


def test_an_unknown_model_name_has_no_window():
    """认不出来的名字**不给窗口**（不能猜一个）。

    这个项目可以指向自建网关，所以"不认识"是正常状态。错的百分比比没有百分比更坏
    —— 它会被当成真的。
    """
    registry = catalog.load()
    assert registry.find("gpt-9") is None
    assert "gpt-9" not in registry.windows()


# --- 配置文件 -------------------------------------------------------------------

def _write(tmp_path, payload) -> "object":
    path = tmp_path / "models.local.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_provider_needs_a_url_and_a_key(workdir):
    """`base_url` 必填；**没有密钥的路由留在目录里但选不了**。

    "这台机器上知道它存在"和"现在能用它"是两件事：前者要能列出来（好让人知道
    自己配了它），后者要能被拒绝，而且拒绝的那句话要说清怎么补。
    """
    path = _write(workdir, {"providers": {
        "good": {"base_url": "https://a.example/v1", "api_key": "sk-a",
                 "models": [{"id": "m1", "context_window": 1000}]},
        "nokey": {"base_url": "https://b.example/v1",
                  "models": [{"id": "m2"}]},
    }})
    registry = catalog.load(path)

    assert [p.name for p in registry.providers] == ["good", "nokey"]
    assert registry.provider("good").usable is True
    assert registry.provider("nokey").usable is False
    # 两条路由的模型**都在清单里**（选不了那一条下面的是 /model 的活）。
    assert {item.qualified for item in registry.models()} == {"good/m1", "nokey/m2"}
    # 问题必须说出来，而且说清怎么补。
    assert any("nokey" in line and "没有密钥" in line for line in registry.problems)
    assert registry.default_provider().name == "good"


def test_missing_base_url_is_a_shape_error(workdir):
    """`base_url` 少了是**形状错误**（当场停下），不是降级 —— 再猜也没意义。"""
    path = _write(workdir, {"providers": {"x": {"api_key": "sk", "models": []}}})
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.load(path)
    assert "base_url" in str(caught.value)


def test_unknown_keys_are_refused(workdir):
    """写错一个键名而它静默不生效，是最坏的失败形态 —— 所以不认识的键直接报错。

    和 `permissions.json` / `mcp.json` 是同一条规矩，而且报错要说清"认识哪些"。
    """
    path = _write(workdir, {"providers": {
        "x": {"base_url": "https://a", "api_key": "sk", "model": [{"id": "m"}]},
    }})
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.load(path)
    assert "不认识的键" in str(caught.value) and "models" in str(caught.value)


def test_a_bad_effort_is_refused_at_load_time(workdir):
    """模型条目里的 `reasoning_effort` 写错**当场报错并列出能写的**。

    这个字段决定 `/effort` 的出厂值。放过去的话，那个值会在第一次请求时才被端点拒
    （实测：非法值 400），而报错离配置文件很远。
    """
    path = _write(workdir, {"providers": {
        "x": {"base_url": "https://a", "api_key": "sk",
              "models": [{"id": "m", "reasoning_effort": "very-high"}]},
    }})
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.load(path)
    assert "reasoning_effort" in str(caught.value)
    assert "low" in str(caught.value) and "max" in str(caught.value)


def test_the_api_key_priority_is_file_then_env_then_dotenv(workdir, monkeypatch):
    """密钥优先级：**文件里写死的 > `api_key_env` 指的环境变量 > `.env`**。

    文件里写死就是"我要用这个"（它是用户级/工作区级的私有文件，不进版本库）。而
    "密钥不进任何文件"是更好的做法，所以环境变量那条路照样得通 —— 两种都该能选。
    """
    env_file = workdir / ".env"
    env_file.write_text("MY_KEY=sk-from-dotenv\n", encoding="utf-8")
    path = _write(workdir, {"providers": {
        "x": {"base_url": "https://a", "api_key_env": "MY_KEY",
              "models": [{"id": "m"}]},
    }})

    # 1) 只有 .env：用它。
    assert catalog.load(path, env_file=env_file).provider("x").api_key == "sk-from-dotenv"

    # 2) 真实环境变量优先于 .env。
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    assert catalog.load(path, env_file=env_file).provider("x").api_key == "sk-from-env"

    # 3) 文件里写了 api_key：它压过两者（写死了就是写死了）。
    path = _write(workdir, {"providers": {
        "x": {"base_url": "https://a", "api_key": "sk-in-file",
              "api_key_env": "MY_KEY", "models": [{"id": "m"}]},
    }})
    assert catalog.load(path, env_file=env_file).provider("x").api_key == "sk-in-file"


def test_a_sample_file_is_shipped_and_loads(workdir):
    """仓库里那份 `models.example.json` **必须是一份能读的配置**。

    它是给人照抄的模板，而"模板本身是坏的"是所有失败形态里最浪费时间的一种：
    照着写的人会以为是自己写错了。所以把它当成真的配置读一遍。
    """
    from agent_runtime.runtime.config import PROJECT_ROOT

    sample = PROJECT_ROOT / catalog.MODELS_EXAMPLE_NAME
    assert sample.is_file(), "模板不见了 —— 它是 /model 那条路的说明书"
    registry = catalog.load(sample, env_file=workdir / "missing.env")
    assert registry.provider("deepseek") is not None
    assert registry.find("deepseek-flash") is not None
    # 模板里那条示例路由没有密钥：它必须**降级成"选不了"，而不是让整个文件读不出来**。
    assert registry.provider("acme-gateway").usable is False


# --- 思考开关与强度 -------------------------------------------------------------

def test_effort_only_offers_the_three_canonical_levels():
    """只提供三档（`low`/`high`/`max`），因为端点上其余几个名字折算之后没有区别。

    实测：端点接受 `none/minimal/low/medium/high/xhigh/max`，而官方那张映射表把它们
    折成三档。把七个都摆出来会让人以为它们不一样 —— 而选哪一个，账单和效果都一样。
    """
    assert reasoning.EFFORT_LEVELS == ("low", "high", "max")
    assert reasoning.DEFAULT_EFFORT == "high"
    # 别名认，但不列。
    assert reasoning.resolve_effort("minimal") == "low"
    assert reasoning.resolve_effort("medium") == "high"
    assert reasoning.resolve_effort("xhigh") == "high"
    assert reasoning.resolve_effort("ultra") == "max"


def test_an_unknown_effort_is_not_guessed():
    """认不出的强度返回 None，**不就近匹配** —— 猜对猜错都一样（猜对了用户也不知道
    自己少打了一个字母，而那个值会一直生效下去）。"""
    assert reasoning.resolve_effort("hgih") is None
    assert reasoning.resolve_effort("") is None
    # `none` 不是一档强度：它是"关掉思考"，属于另一个旋钮。
    assert reasoning.resolve_effort("none") is None
    assert reasoning.is_off("none") is True


def test_the_thinking_switch_accepts_words_a_human_would_write():
    """`/thinking 开` 和 `/thinking on` 都该懂 —— 只认 `true` 的命令在中文界面里荒谬。"""
    for word in ("on", "开", "true", "yes", "1", "enabled"):
        assert reasoning.resolve_thinking(word) is True, word
    for word in ("off", "关", "false", "no", "0", "disabled", "none"):
        assert reasoning.resolve_thinking(word) is False, word
    assert reasoning.resolve_thinking("maybe") is None


def test_the_request_fields_match_what_the_endpoint_accepts():
    """这两个旋钮 → 请求参数。**三条实测得来的规矩**：

      1. 开着时两个都发：`reasoning_effort` 走顶层（SDK 的原生参数），
         `thinking` 走 `extra_body`（它不在 SDK 的类型里）；
      2. 关着时**只发 `thinking`**：端点会忽略同时发来的 effort，而一个没人读的字段
         只会让抓包的人以为它生效了；
      3. 显式发 `enabled`：那本来就是端点的默认值，但默认值会随端点变，而这个配置是
         用户选的 —— 多一个字段换"这条请求在任何时候都是同样的意思"。
    """
    on = reasoning.request_fields(thinking=True, effort="max")
    assert on == {"reasoning_effort": "max",
                  "extra_body": {"thinking": {"type": "enabled"}}}
    off = reasoning.request_fields(thinking=False, effort="max")
    assert off == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert "reasoning_effort" not in off


def test_the_summary_hides_the_effort_when_thinking_is_off():
    """关着思考时那句摘要**不写强度**（写了会让人以为它还在生效）。

    强度没有被丢掉 —— `/thinking on` 之后还是原来那个，所以摘要里也不该出现
    "已重置"这类说法。
    """
    assert reasoning.summary(thinking=True, effort="high") == "开 · high"
    assert reasoning.summary(thinking=False, effort="high") == "关"
    assert "high" not in reasoning.summary(thinking=False, effort="high")
