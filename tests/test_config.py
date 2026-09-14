"""配置文件里那些**不属于模型目录**的部分：`web` 段。

模型那一层（`providers`：端点、模型清单、密钥）由 `catalog` 解释，它的测试在
`test_catalog.py` 和 `test_providers.py`。

这个文件为什么变短了：`ModelConfig`（`DEEPSEEK_*` 那三格）和那张**三档优先级**
（真实环境变量 > 文件的 `env` 段 > 默认值）都随"配置只有一个来源"退休了。所以这里不再有
"谁压过谁"要测 —— **不看环境变量、也不看 `.env`**，值就在这份文件里。剩下的是联网工具
那一段，以及模板本身的两条断言。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime import userconfig
from agent_runtime.runtime.config import (
    ConfigError,
    DEFAULT_TAVILY_BASE_URL,
    WebConfig,
)


def write_config(workdir, **sections) -> Path:
    """写一份用户级配置：段名 → 内容，例如 `write_config(workdir, web={...})`。"""
    path = workdir / "config.json"
    path.write_text(json.dumps(sections, ensure_ascii=False), encoding="utf-8")
    return path


# --- `web` 段 -------------------------------------------------------------

def test_the_web_key_is_read_from_its_own_section(workdir):
    """`web` 是一节一个主人的那"另一个主人"：联网工具那几个键住在这里。"""
    cfg = WebConfig.from_file(write_config(workdir, web={"tavily_api_key": "tvly-from-file"}))

    assert cfg.tavily_api_key == "tvly-from-file"
    assert cfg.tavily_base_url == DEFAULT_TAVILY_BASE_URL
    assert cfg.enabled is True


def test_the_web_base_url_can_be_switched(workdir):
    """换搜索网关的口子 —— 和"换模型网关"是同一个需求。"""
    cfg = WebConfig.from_file(write_config(
        workdir,
        web={"tavily_api_key": "tvly-x", "tavily_base_url": "https://gateway.example/v1"},
    ))

    assert cfg.tavily_base_url == "https://gateway.example/v1"


def test_an_empty_web_value_does_not_count_as_a_key(workdir):
    """留空的那一格是"还没填"，不是"填了一个空密钥"。

    模板里那一行**就是空的**，所以这条路径是每个新用户都会走一遍的：它必须落到"没配"上，
    否则一个空字符串会被当成有效密钥发出去，然后收到一句鉴权失败 —— 症状离原因太远。
    """
    cfg = WebConfig.from_file(write_config(workdir, web={"tavily_api_key": ""}))

    assert cfg.tavily_api_key == ""
    assert cfg.enabled is False


def test_a_missing_web_section_is_not_an_error(workdir):
    """**缺搜索密钥不拦启动** —— 这和模型那一档是有意相反的。

    模型那边缺了"一条能用的路由"整个程序什么都干不了（`ConfigError` + 退出码 2）；搜索
    密钥缺了只是少一个工具。混成一样会让"只想用文件工具的人"被迫先去注册一个搜索服务。
    """
    cfg = WebConfig.from_file(write_config(workdir, providers={}))

    assert cfg.enabled is False
    assert cfg.tavily_base_url == DEFAULT_TAVILY_BASE_URL


def test_an_unknown_key_in_web_is_refused(workdir):
    """`web` 里写错一个键名而它静默不生效，是最坏的失败形态 —— 所以直接报错。

    `tavily_key`（少了 `api_`）是很自然的手滑，而它的表现会是"我明明配了，`web_search`
    却没出现"。报错还要说清**认识哪些**，否则用户只能去翻源码。
    """
    path = write_config(workdir, web={"tavily_key": "tvly-x"})

    with pytest.raises(ConfigError) as caught:
        WebConfig.from_file(path)

    assert "tavily_key" in str(caught.value)
    assert "tavily_api_key" in str(caught.value)


def test_a_non_string_web_value_is_refused(workdir):
    """数字和布尔**不自动转成字符串**：那几乎总是写错了引号。

    悄悄接受它会让一把"密钥"以 `"12345"` 的形态发出去，然后收到一句鉴权失败 ——
    症状离原因太远。
    """
    path = write_config(workdir, web={"tavily_api_key": 12345})

    with pytest.raises(userconfig.UserConfigError) as caught:
        WebConfig.from_file(path)

    assert "tavily_api_key" in str(caught.value)


# --- 模板本身 -------------------------------------------------------------

def test_the_example_config_exists_and_has_no_secret():
    """模板是**会被提交**的那一份，里面绝不能出现真值。

    而两把密钥应该在**看得见的位置**上等着被填（`"api_key": ""`），不是让人去找 ——
    这条断言正是那次实测的教训：有人把密钥填进了那个"看起来像要密钥"的字段。
    """
    path = userconfig.example_file()
    assert path.is_file()

    text = path.read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "tvly-" not in text
    assert '"api_key": ""' in text


def test_the_example_config_is_a_config_the_program_can_read():
    """模板必须**真的能被这个程序读懂**。

    它是给人照抄的那一份，所以里面一个写错的键名会让每个照着做的人都撞上同一个报错，
    然后以为是自己写错了。两段都走一遍解析。
    """
    from agent_runtime.state import catalog

    sample = userconfig.example_file()
    cfg = userconfig.read(sample)

    assert "deepseek" in cfg.providers
    assert "tavily_api_key" in cfg.web
    # 作为模型目录也读得出来：密钥是空的 ⇒ 选不了，但**不是"文件读不懂"**。
    registry = catalog.load(sample)
    assert registry.provider("deepseek") is not None
    assert registry.usable is False


def test_the_template_has_no_environment_variable_left():
    """模板里**不该再出现环境变量的痕迹**。

    `env` 段、`api_key_env`、`DEEPSEEK_API_KEY` —— 这三个名字随"配置只有一个来源"一起
    退休了。它们若还留在模板里，下一个读它的人（包括未来的我们）会以为环境变量仍然有用，
    而那条路早就不通了。
    """
    text = userconfig.example_file().read_text(encoding="utf-8")

    for gone in ("api_key_env", "DEEPSEEK_API_KEY", "TAVILY_API_KEY", '"env"'):
        assert gone not in text, f"模板里还留着 {gone} 的痕迹"


# --- 读配置不许有副作用 ----------------------------------------------------

def test_reading_the_config_does_not_leak_into_os_environ(workdir):
    """**读配置不许有全局副作用。**

    优先级那三档没了，但这条性质更要紧了 —— "环境变量完全不被读"现在是设计的一部分：
    一旦读配置顺手把值写进 `os.environ`，程序就又长出了一条看不见的配置来源，而那一层
    以后再想拆掉就难了。
    """
    path = write_config(workdir, providers={}, web={"tavily_api_key": "tvly-from-file"})

    userconfig.read(path)
    WebConfig.from_file(path)

    assert "TAVILY_API_KEY" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_importing_config_does_not_read_any_file(workdir):
    """**import 这个模块不许读盘。**

    它以前有一句模块级赋值（`CONTEXT_WINDOWS = catalog.load().windows()`），于是
    `import agent_runtime.runtime.config` 会在那一瞬间去碰 `~/.tudouni/` 和 cwd ——
    一个"读配置"的模块，在被 import 时产生文件系统依赖，而 import 顺序不是任何人打算
    维护的东西。

    判据：把 `AGENT_CONFIG_FILE` 指到一个**不存在**的文件再 import。真去读了就会抛
    （`userconfig.read` 对显式指定的路径是要报错的），没读就什么都不发生 —— 这是
    "import 期读盘"最直接的探针，不依赖任何模块内部的名字。
    """
    env = {**os.environ, userconfig.FILE_ENV: str(workdir / "definitely-not-here.json")}

    result = subprocess.run(
        [sys.executable, "-c", "import agent_runtime.runtime.config; print('ok')"],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
