"""配置的优先级与报错。

优先级（高 → 低）：**真实环境变量 > .env > 默认值**。

这条顺序值得有测试盯着：搞反了会让某天部署时被一个遗留的 .env 悄悄改到别的网关，
而那种问题极难排查 —— 因为源码里什么都看不出来。
"""

from pathlib import Path

import pytest

from agent_runtime.config import (
    CONTEXT_WINDOWS,
    DEFAULT_MODEL,
    ENV_EXAMPLE_FILE,
    ConfigError,
    ModelConfig,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清掉相关的环境变量，免得本机恰好设了而让测试互相干扰。"""
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)


def write_env(workdir, text: str) -> Path:
    path = workdir / ".env"
    path.write_text(text, encoding="utf-8")
    return path


# --- 从 .env 读 ---------------------------------------------------------

def test_reads_key_from_env_file(workdir):
    cfg = ModelConfig.from_env(write_env(workdir, "DEEPSEEK_API_KEY=sk-from-file\n"))
    assert cfg.api_key == "sk-from-file"


def test_env_file_supplies_optional_overrides(workdir):
    path = write_env(workdir, "\n".join([
        "DEEPSEEK_API_KEY=sk-x",
        "DEEPSEEK_BASE_URL=https://gateway.example/v1/",
        "DEEPSEEK_MODEL=my-model",
    ]))
    cfg = ModelConfig.from_env(path)
    assert cfg.base_url == "https://gateway.example/v1/"
    assert cfg.model == "my-model"


def test_defaults_when_env_file_is_minimal(workdir):
    cfg = ModelConfig.from_env(write_env(workdir, "DEEPSEEK_API_KEY=sk-x\n"))
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"


# --- 优先级 -------------------------------------------------------------

def test_real_env_var_wins_over_env_file(workdir, monkeypatch):
    """重点：.env 只是本地方便，不能盖掉真实环境变量。"""
    path = write_env(workdir,
                     "DEEPSEEK_API_KEY=sk-from-file\nDEEPSEEK_MODEL=file-model\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")

    cfg = ModelConfig.from_env(path)
    assert cfg.api_key == "sk-from-env"     # 环境变量赢
    assert cfg.model == "file-model"        # 没设的那个仍然用文件


def test_missing_file_falls_back_to_env_var(monkeypatch):
    """没有 .env 不是错误 —— 环境变量是另一条合法通路。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert ModelConfig.from_env(Path("definitely-does-not-exist.env")).api_key == "sk-from-env"


# --- 缺失与空值 ---------------------------------------------------------

def test_empty_file_value_does_not_count_as_a_key(workdir):
    """`.env` 里留空的那一行不该被当成有效值。"""
    with pytest.raises(ConfigError):
        ModelConfig.from_env(write_env(workdir, "DEEPSEEK_API_KEY=\n"))


def test_empty_file_value_falls_through_to_env_var(workdir, monkeypatch):
    path = write_env(workdir, "DEEPSEEK_API_KEY=\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert ModelConfig.from_env(path).api_key == "sk-from-env"


def test_missing_key_error_names_both_options(workdir):
    """报错要把两条路都给出来 —— 配置错误是"用户得先做点事"，不该让人去翻源码。"""
    with pytest.raises(ConfigError) as exc:
        ModelConfig.from_env(workdir / "nope.env")

    message = str(exc.value)
    assert "DEEPSEEK_API_KEY" in message
    assert ".env" in message        # 可以写文件
    assert "setx" in message        # 也可以设环境变量


# --- 模板文件本身 -------------------------------------------------------

def test_env_example_exists_and_has_no_secret():
    """模板是**会被提交**的那一份，里面绝不能出现真密钥。

    这条断言很小，但它是"密钥不进仓库"这个承诺在测试里的落点。
    """
    assert ENV_EXAMPLE_FILE.is_file()
    text = ENV_EXAMPLE_FILE.read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "DEEPSEEK_API_KEY" in text


def test_dotenv_does_not_leak_into_os_environ(workdir, monkeypatch):
    """用 dotenv_values 而不是 load_dotenv：读配置不该有全局副作用。

    否则一次 from_env 就会把 .env 的内容写进 os.environ，之后任何代码（包括别的
    测试）都会莫名看到这些值，而优先级规则也就不再成立。
    """
    import os
    write_env(workdir, "DEEPSEEK_API_KEY=sk-from-file\n")
    ModelConfig.from_env(workdir / ".env")
    assert "DEEPSEEK_API_KEY" not in os.environ


# --- 上下文窗口那张表 -----------------------------------------------------

def test_the_default_model_has_a_declared_window():
    """默认模型必须在表里 —— 否则每次启动都会打一句"没分母"，而那是默认体验。"""
    cfg = ModelConfig(api_key="sk-x", base_url="x", model=DEFAULT_MODEL)
    assert cfg.context_tokens == CONTEXT_WINDOWS[DEFAULT_MODEL]


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

    cfg = ModelConfig.from_env(Path("definitely-does-not-exist.env"))

    assert cfg.model == "deepseek-v4-pro"
    assert cfg.context_tokens == CONTEXT_WINDOWS["deepseek-v4-pro"]
