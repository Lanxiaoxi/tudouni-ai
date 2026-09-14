"""配置的优先级与报错。

优先级（高 → 低）：**真实环境变量 > `~/.tudouni/config.json` 的 `env` 段 > 默认值**。

这条顺序值得有测试盯着：搞反了会让某天部署时被一份遗留的配置悄悄改到别的网关，
而那种问题极难排查 —— 因为源码里什么都看不出来。

（`.env` 那一层已经不在了：密钥搬进了用户级那份 `config.json`，理由写在
`agent_runtime/userconfig.py` 的 docstring 里。这个文件里的用例是照着搬过来的 ——
**同一条优先级，只是换了个文件**。）
"""

import json
from pathlib import Path

import pytest

from agent_runtime import userconfig
from agent_runtime.runtime.config import (
    context_windows,
    DEFAULT_MODEL,
    DEFAULT_TAVILY_BASE_URL,
    ModelConfig,
    WebConfig,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清掉相关的环境变量，免得本机恰好设了而让测试互相干扰。

    TAVILY_API_KEY 必须一起清：这台机器上它**真的设着**（开发时就是那么用的），
    不清的话下面"缺密钥"那几条会在本机莫名其妙地绿/红 —— 而 CI 上它们是对的。
    """
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                 "TAVILY_API_KEY", "TAVILY_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def write_config(workdir, env: dict, **rest) -> Path:
    """写一份用户级配置（只有 `env` 段，除非调用方另给）。"""
    path = workdir / "config.json"
    path.write_text(json.dumps({"env": env, **rest}, ensure_ascii=False),
                    encoding="utf-8")
    return path


# --- 从配置文件读 -------------------------------------------------------

def test_reads_key_from_the_config_file(workdir):
    cfg = ModelConfig.from_env(write_config(workdir, {"DEEPSEEK_API_KEY": "sk-from-file"}))
    assert cfg.api_key == "sk-from-file"


def test_the_config_file_supplies_optional_overrides(workdir):
    path = write_config(workdir, {
        "DEEPSEEK_API_KEY": "sk-x",
        "DEEPSEEK_BASE_URL": "https://gateway.example/v1/",
        "DEEPSEEK_MODEL": "my-model",
    })
    cfg = ModelConfig.from_env(path)
    assert cfg.base_url == "https://gateway.example/v1/"
    assert cfg.model == "my-model"


def test_defaults_when_the_config_file_is_minimal(workdir):
    cfg = ModelConfig.from_env(write_config(workdir, {"DEEPSEEK_API_KEY": "sk-x"}))
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"


# --- 优先级 -------------------------------------------------------------

def test_real_env_var_wins_over_the_config_file(workdir, monkeypatch):
    """重点：配置文件只是本地方便，不能盖掉真实环境变量。"""
    path = write_config(workdir, {"DEEPSEEK_API_KEY": "sk-from-file",
                                  "DEEPSEEK_MODEL": "file-model"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")

    cfg = ModelConfig.from_env(path)
    assert cfg.api_key == "sk-from-env"     # 环境变量赢
    assert cfg.model == "file-model"        # 没设的那个仍然用文件


def test_missing_file_falls_back_to_env_var(workdir, monkeypatch):
    """**没有配置文件不是错误** —— 只给环境变量是另一条合法通路（容器里就这么用）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    missing = workdir / "definitely-does-not-exist.json"
    assert ModelConfig.from_env(missing).api_key == "sk-from-env"


# --- 缺失与空值 ---------------------------------------------------------
#
# **"缺密钥"在这个文件里不再是错误。** 它原来是 `ModelConfig.from_env()` 抛的
# `ConfigError`，而那句话正是新用户看到的第一句 —— 后来发现那是把工具说成了一家网关的
# 客户端（模型层是抽象的：端点和密钥都在 `providers` 里）。现在拦住启动的是
# `open_runtime` 里"一条能用的路由都没有"那一问，所以**报错文案和"首次运行写模板"那条
# 接线搬到了 `tests/test_providers.py`** —— 那里起真子进程，验的是用户真正看到的东西。
# 这里只留"值怎么读"。


def test_empty_file_value_does_not_count_as_a_key(workdir):
    """留空的那一格是"还没填"，不是"填了一个空密钥"。

    它必须落到下一层，否则一个空字符串会被当成有效值发出去 —— 而首次运行生成的模板里
    那一格**就是空的**，所以这条路径是每个新用户都会走一遍的。

    这里只钉"空串没被当成有值"。它会不会因此拦住启动，是另一件事（见上面那段）。
    """
    cfg = ModelConfig.from_env(write_config(workdir, {"DEEPSEEK_API_KEY": ""}))
    assert cfg.api_key == ""


def test_empty_file_value_falls_through_to_env_var(workdir, monkeypatch):
    path = write_config(workdir, {"DEEPSEEK_API_KEY": ""})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert ModelConfig.from_env(path).api_key == "sk-from-env"


def test_reading_these_three_cells_never_requires_a_key(workdir):
    """一把密钥都没有时，这三格**照样读得出来**（`api_key` 就是空串）。

    这条守的是"读配置"和"够不够用"是两件事：读永远成功，判断在 `catalog` 那边。
    以前这个方法缺密钥就抛错，于是连"只想看看默认模型名是什么"都得先有一把真密钥 ——
    而那正是"模型层被绑死在一家网关上"的样子。
    """
    cfg = ModelConfig.from_env(write_config(workdir, {"DEEPSEEK_MODEL": "my-model"}))
    assert cfg.api_key == ""
    assert cfg.model == "my-model"
    assert cfg.base_url == "https://api.deepseek.com"


# --- 模板文件本身 -------------------------------------------------------

def test_the_example_config_exists_and_has_no_secret():
    """模板是**会被提交**的那一份，里面绝不能出现真密钥。

    这条断言很小，但它是"密钥不进仓库"这个承诺在测试里的落点。
    """
    path = userconfig.example_file()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "DEEPSEEK_API_KEY" in text


def test_the_example_config_is_a_config_the_program_can_read():
    """模板必须**真的能被这个程序读懂**。

    它是照着抄的那一份，所以里面一个写错的键名会让每个照着做的人都撞上同一个报错，
    然后以为是自己写错了。所以把它当成真配置读一遍 —— 顶层形状、`env` 段、`providers`
    段全走一遍解析。
    """
    cfg = userconfig.read(userconfig.example_file())

    assert "DEEPSEEK_API_KEY" in cfg.env
    assert "deepseek" in cfg.providers


def test_reading_the_config_does_not_leak_into_os_environ(workdir):
    """**读配置不许有全局副作用。**

    这一条以前叫 `test_dotenv_does_not_leak_into_os_environ`，盯的是"用 `dotenv_values`
    而不是 `load_dotenv`"。dotenv 那层依赖已经没了，但要守的性质一字未变，而且现在更
    要紧 —— 因为优先级的第一档就是 `os.environ`：

    一旦读配置顺手把文件里的值写进 `os.environ`，那么**第二次**读的时候它们就变成了
    "真实环境变量"，于是"环境变量优先于文件"这条规则表面上还成立，实际上已经没有意义
    了。而这种自我实现的错误从任何一次断言里都看不出来。
    """
    import os

    path = write_config(workdir, {"DEEPSEEK_API_KEY": "sk-from-file",
                                  "TAVILY_API_KEY": "tvly-from-file"})

    ModelConfig.from_env(path)
    WebConfig.from_env(path)

    assert "DEEPSEEK_API_KEY" not in os.environ
    assert "TAVILY_API_KEY" not in os.environ


# --- 上下文窗口那张表 -----------------------------------------------------

def test_importing_config_does_not_read_the_catalog():
    """**import 这个模块不许读盘。**

    那张窗口表以前是一句模块级赋值（`CONTEXT_WINDOWS = catalog.load().windows()`），
    于是 `import agent_runtime.runtime.config` 会在那一瞬间去碰 `~/.tudouni/` 和 cwd。
    三个后果，一个比一个难查：

      1. import 顺序变成了必须维护的东西，而没人在维护它；
      2. 测试想换一份目录配置（`AGENT_MODELS_FILE`）就得抢在第一次 import 之前设环境
         变量 —— 也就是取决于哪个测试文件先被收集；
      3. 配置搬到用户级之后，它会和"首次运行生成模板"撞上：模板该由入口在明确的时机
         创建，不该被某个 import 顺手触发。

    验法是**在子进程里**数一次：`catalog.load` 被 import 期调用过没有。用子进程是因为
    这个模块在当前进程里早就被 import 过了，`sys.modules` 里那份看不出任何东西。
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(repo_root)!r});"
        "import agent_runtime.state.catalog as cat;"
        "calls = [];"
        "real = cat.load;"
        "cat.load = lambda *a, **k: (calls.append(1), real(*a, **k))[1];"
        "import agent_runtime.runtime.config;"
        "print(len(calls))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", (
        f"import config 时调了 {result.stdout.strip()} 次 catalog.load() —— "
        f"那张表必须惰性算（见 config.context_windows）"
    )


def test_the_default_model_has_a_declared_window():
    """默认模型必须在表里 —— 否则每次启动都会打一句"没分母"，而那是默认体验。"""
    cfg = ModelConfig(api_key="sk-x", base_url="x", model=DEFAULT_MODEL)
    assert cfg.context_tokens == context_windows()[DEFAULT_MODEL]


def test_an_unknown_model_has_no_window_rather_than_a_guess():
    """表里没有就返回 None —— cli 那边据此只报用量、不报占比。

    **错的百分比比没有百分比更坏**：它会被当成真的，而这项目可以指向任意网关。
    """
    cfg = ModelConfig(api_key="sk-x", base_url="x", model="some-gateway-alias")
    assert cfg.context_tokens is None


def test_configured_model_name_decides_the_window(workdir, monkeypatch):
    """窗口跟着配置里的模型名走 —— 它是派生值，不是另一个要维护的字段。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

    cfg = ModelConfig.from_env(workdir / "definitely-does-not-exist.json")

    assert cfg.model == "deepseek-v4-pro"
    assert cfg.context_tokens == context_windows()["deepseek-v4-pro"]


# --- 联网工具的配置（WebConfig） ------------------------------------------
#
# 它和 ModelConfig 共用同一份配置文件、同一套优先级（同一个 `UserConfig.value`），
# 但**缺密钥的处置完全不同**：
# 模型那边缺的是"一条能用的路由"，整个程序什么都干不了（ConfigError + 退出码 2）；
# 搜索密钥缺了只是少一个工具。这两件事混成一样，会让"只想用文件工具的人"被迫先去注册
# 一个搜索服务。
#
# 注意两边的判据形状不同 —— 模型那边**不是**"缺 DEEPSEEK_API_KEY"（密钥归 providers
# 管），所以那件事的测试也不在这个文件里。

def test_web_key_is_read_from_the_same_config_file(workdir):
    cfg = WebConfig.from_env(write_config(workdir, {"TAVILY_API_KEY": "tvly-from-file"}))

    assert cfg.tavily_api_key == "tvly-from-file"
    assert cfg.tavily_base_url == DEFAULT_TAVILY_BASE_URL
    assert cfg.enabled is True


def test_web_env_var_wins_over_the_config_file(workdir, monkeypatch):
    """和 ModelConfig 一字不差的优先级 —— 它们调的就是同一个函数。"""
    path = write_config(workdir, {"TAVILY_API_KEY": "tvly-from-file"})
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")

    assert WebConfig.from_env(path).tavily_api_key == "tvly-from-env"


def test_an_empty_web_key_counts_as_unset(workdir, monkeypatch):
    """留空的那一格是"还没填"，不是"填了一个空密钥"。"""
    path = write_config(workdir, {"TAVILY_API_KEY": ""})

    assert WebConfig.from_env(path).enabled is False
    assert WebConfig.from_env(path).tavily_api_key == ""

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    assert WebConfig.from_env(path).tavily_api_key == "tvly-from-env"


def test_a_missing_web_key_is_not_an_error(workdir):
    """**这条是它和 ModelConfig 的分界。**

    缺搜索密钥不该拦启动：那只会逼着"只想用文件工具的人"先去注册一个搜索服务。
    要说的那句话由 main.py 打到 stderr（"[联网] 没找到 TAVILY_API_KEY…"）。
    """
    cfg = WebConfig.from_env(workdir / "nope.json")

    assert cfg.enabled is False
    assert cfg.tavily_base_url == DEFAULT_TAVILY_BASE_URL


def test_web_base_url_can_be_switched(workdir, monkeypatch):
    """换网关的口子（和 DEEPSEEK_BASE_URL 同一个理由）。"""
    monkeypatch.setenv("TAVILY_BASE_URL", "https://gateway.example/v1")

    assert WebConfig.from_env(workdir / "nonexistent.json").tavily_base_url == "https://gateway.example/v1"


def test_the_example_config_documents_the_web_key_without_a_secret():
    """模板是**会被提交**的那一份。

    键名要出现在里面（否则没人知道该填什么），但它必须**没有一个真的值**。盯的不是
    "文本里不出现 tvly- 这几个字"：模板里"形如 tvly-..." 那句提示是有用的，真该禁的是
    一个真的值。

    判据落在**解析出来的那个 `env` 映射**上，而不是原文的正则：改成 JSON 之后，"赋值
    那一行"这个概念没了（值可以跨行、可以待在注释里），而"程序读出来是什么"才是密钥真正
    泄漏的位置。

    （`test_the_example_config_exists_and_has_no_secret` 那条盯的是 "sk-"，同一个手法。
    这里不能照抄它："sk-" 是 DeepSeek 密钥的固有前缀、不会出现在说明文字里，而 "tvly-"
    会。）
    """
    text = userconfig.example_file().read_text(encoding="utf-8")
    assert "TAVILY_API_KEY" in text, "模板里得说一句这个键存在，否则没人知道该填什么"

    cfg = userconfig.read(userconfig.example_file())
    assert not cfg.value_in_file("TAVILY_API_KEY"), "模板里出现了真的搜索密钥"
    assert not cfg.value_in_file("DEEPSEEK_API_KEY"), "模板里出现了真的模型密钥"


