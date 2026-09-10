"""运行配置。

一律从环境变量读，源码里不留任何密钥。

这个文件存在的原因很实际：**源码会被提交到公开仓库，密钥不会。** 把密钥写进
main.py 的那一刻，它就已经在 git 历史里了 —— 删掉那一行也删不掉历史，只能
吊销重发。所以配置和代码从第一天起就该分家。
"""

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

_ENV_API_KEY = "DEEPSEEK_API_KEY"
_ENV_BASE_URL = "DEEPSEEK_BASE_URL"
_ENV_MODEL = "DEEPSEEK_MODEL"


class ConfigError(Exception):
    """配置缺失或非法 —— 属于"用户得先做点事"，不是 bug。"""


@dataclass(frozen=True)
class ModelConfig:
    """模型连接的配置。"""

    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_env(cls) -> "ModelConfig":
        api_key = os.environ.get(_ENV_API_KEY, "").strip()
        if not api_key:
            raise ConfigError(
                f"缺少环境变量 {_ENV_API_KEY}。\n"
                f"  当前会话临时设置：  $env:{_ENV_API_KEY} = \"sk-...\"\n"
                f"  永久设置（重开终端生效）：  setx {_ENV_API_KEY} \"sk-...\"\n"
                f"  换模型/网关可选：  $env:{_ENV_MODEL}、$env:{_ENV_BASE_URL}"
            )

        return cls(
            api_key=api_key,
            base_url=os.environ.get(_ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL,
            model=os.environ.get(_ENV_MODEL, "").strip() or DEFAULT_MODEL,
        )
