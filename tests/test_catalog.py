"""模型目录与路由配置（`state/catalog.py`）+ 思考开关与强度（`state/reasoning.py`）。

这两块合在一起测，是因为它们回答同一个问题的两半：**这台机器上能选什么**
（目录）和**选了之后怎么想**（思考那两个旋钮）。它们各自最容易错的地方也是同一类：
**默认值猜错、名字认错，而两种错都不会报错** —— 只会静默地按另一个模型/另一档强度跑。

配置文件那部分的测试全部**自己造文件**（`workdir` 下的临时 json），不碰这台机器上真的
`~/.tudouni/config.json` —— 那是个人的东西，而测试要看的是"给一份配置会发生什么"。
"""

import json

import pytest

from fakes import context_windows
from agent_runtime.state import catalog, reasoning


# --- 窗口表：和目录同源 ---------------------------------------------------------

def test_the_window_table_is_derived_from_the_catalog():
    """窗口表（`{模型名: 窗口}`）**从目录现算**，不是第二份数据。

    各写一份的后果是静默的：往配置里加一个模型（`/model` 立刻列出它），而窗口表没跟上，
    于是"选了它之后状态栏不报占比" —— 两处都不会报错，只是那个百分比消失了。

    （这条以前叫"内置目录同源"，因为不带配置文件时有一条内置路由。内置目录随"配置只有
    一个来源"退休了，现在这句话说的是目录本身。）
    """
    registry = catalog.load()
    assert registry.providers, "隔离用的那份配置里应该有路由"
    for item in registry.models():
        assert context_windows()[item.id] == item.window


def test_an_unknown_model_name_has_no_window():
    """认不出来的名字**不给窗口**（不能猜一个）。

    这个项目可以指向任意网关，所以"不认识"是正常状态。错的百分比比没有百分比更坏
    —— 它会被当成真的。
    """
    registry = catalog.load()
    assert registry.find("gpt-9") is None
    assert "gpt-9" not in registry.windows()


# --- 配置文件 -------------------------------------------------------------------

def _write(tmp_path, payload) -> "object":
    path = tmp_path / "config.json"
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


def test_api_key_env_is_an_unknown_key_now(workdir):
    """`api_key_env` 现在是**不认识的键** —— 这一条是一次实测换来的。

    它以前是"填一个环境变量的名字"，而那个字段和 `api_key` 长得几乎一样：一个装名字、
    一个装值。实测有人把**密钥本身**填了进去，然后只拿到一句"没有密钥"（而他手里明明有
    一把填进去的密钥），于是只能来问"为什么"。

    配置收口到"只有这份文件"之后，那个字段没有立足之地了；而它落进"不认识的键"意味着
    **同样的手滑会当场被指出来**，并且报错会说清这条路由认识哪些键。
    """
    path = _write(workdir, {"providers": {
        "x": {"base_url": "https://a", "api_key_env": "sk-433215e4",
              "models": [{"id": "m"}]}}})

    with pytest.raises(catalog.CatalogError) as caught:
        catalog.load(path)

    assert "api_key_env" in str(caught.value)
    assert "api_key" in str(caught.value)      # 报错要说清"认识的只有哪些"


def test_no_providers_means_no_routes(workdir):
    """**没有 `providers` 就是一条路由也没有** —— 不再退到某条内置路由上。

    以前这里有一条兜底路由（密钥读 `DEEPSEEK_API_KEY`）。环境和 `.env` 一起退休之后它
    没有密钥来源了，于是那份"不配置也能跑"的形态也跟着消失：现在唯一的配法就是在这份
    文件里写一条路由。
    """
    path = _write(workdir, {"web": {"tavily_api_key": "tvly-x"}})

    registry = catalog.load(path)

    assert registry.providers == ()
    assert registry.usable is False
    assert registry.default_model() is None
    # 那份文件仍然是"这份目录从哪来"的答案 —— `/status` 要拿它回答"为什么我改的没生效"。
    assert str(path) == registry.source


def test_an_unknown_top_level_key_is_refused(workdir):
    """顶层写错一个键名也直接报错，不忽略。

    和 providers 里面那条同一个理由，但它更容易犯：`"provider"`（少个 s）、
    `"environment"`（不是 `web`）都是很自然的手滑，而静默忽略的表现是"我明明配了，
    它却完全没生效"。

    **抛的是 `UserConfigError` 而不是 `CatalogError`**，这一点是分工的直接体现：顶层长
    什么样是 `userconfig` 的知识（它读文件），`providers` 里面长什么样才是这个模块的。
    对入口来说没有区别 —— 它捕的是基类，两者都会变成"打到 stderr + 退出码 2"。
    """
    from agent_runtime import userconfig

    path = _write(workdir, {"provider": {}})

    with pytest.raises(userconfig.UserConfigError) as caught:
        catalog.load(path)

    assert "不认识的顶层键" in str(caught.value)
    assert "providers" in str(caught.value) and "web" in str(caught.value)
    # 而 `CatalogError` 是它的子类，所以入口那条 `except` 一样能兜住这一类。
    assert issubclass(catalog.CatalogError, userconfig.UserConfigError)


def test_a_sample_file_is_shipped_and_loads():
    """仓库里那份 `config.example.json` **必须是一份能读的配置**。

    它是给人照抄的模板，而"模板本身是坏的"是所有失败形态里最浪费时间的一种：
    照着写的人会以为是自己写错了。所以把它当成真的配置读一遍。

    模板里那条路由的 `api_key` 是空的，那必须**降级成"选不了"，而不是让整个文件读不出
    来** —— 新用户第一次跑看到的应该是"打开它填一格"，而不是一个形状错误。
    """
    from agent_runtime import userconfig

    sample = userconfig.example_file()
    assert sample.is_file(), "模板不见了 —— 它是这条路的说明书"

    registry = catalog.load(sample)

    assert registry.provider("deepseek") is not None
    assert registry.find("deepseek-flash") is not None
    assert registry.usable is False, "模板里密钥是空的：它该是'选不了'，不是'能用'"
    assert any("没有密钥" in line for line in registry.problems)


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
