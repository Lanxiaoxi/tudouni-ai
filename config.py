"""运行配置。

优先级（高 → 低）：

    1. 真实环境变量
    2. 项目根目录的 .env
    3. 内置默认值

.env 只是本地图方便，**绝不能盖掉真实环境变量** —— 否则某天部署时会被一个遗留的
.env 悄悄改到别的网关上，而这种问题极难排查。这个顺序有测试盯着。

密钥一律不进源码：源码会被提交到公开仓库，.env 不会（它在 .gitignore 里）。
`.env.example` 是给人看的那份模板，里面没有真密钥，所以它是被提交的。

**用 `dotenv_values` 而不是 `load_dotenv`**：前者只返回一个 dict，不往 os.environ
里写。没有全局副作用，优先级规则就能在这一个函数里读完，而不是靠库的默认行为。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"

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
    def from_env(cls, env_file: Path | None = None) -> "ModelConfig":
        """读取配置。

        env_file 可指定，默认用项目根的 .env（文件不存在就跳过，不算错误 ——
        环境变量和 .env 任选其一即可）。
        """
        path = ENV_FILE if env_file is None else Path(env_file)
        file_values = dotenv_values(path) if path.is_file() else {}

        def pick(name: str, default: str = "") -> str:
            # 空串一律当作"没设"，这样 .env 里留空的项也能落到下一层默认值上，
            # 而不是变成一个空字符串把后面的判断搞乱。
            from_env_var = os.environ.get(name, "").strip()
            if from_env_var:
                return from_env_var
            from_file = (file_values.get(name) or "").strip()
            return from_file or default

        api_key = pick(_ENV_API_KEY)
        if not api_key:
            raise ConfigError(
                f"没找到 {_ENV_API_KEY}。两种给法，任选其一：\n"
                f"\n"
                f"  1) 写进 {ENV_FILE}（推荐，已被 .gitignore 忽略）\n"
                f"       先从模板复制一份：  Copy-Item {ENV_EXAMPLE_FILE.name} .env\n"
                f"       然后填上：          {_ENV_API_KEY}=sk-...\n"
                f"\n"
                f"  2) 设成环境变量\n"
                f"       当前终端：  $env:{_ENV_API_KEY} = \"sk-...\"\n"
                f"       永久有效：  setx {_ENV_API_KEY} \"sk-...\"   （重开终端生效）\n"
                f"\n"
                f"可选：{_ENV_MODEL}、{_ENV_BASE_URL}\n"
                f"注：环境变量的优先级高于 .env。"
            )

        return cls(
            api_key=api_key,
            base_url=pick(_ENV_BASE_URL, DEFAULT_BASE_URL),
            model=pick(_ENV_MODEL, DEFAULT_MODEL),
        )
