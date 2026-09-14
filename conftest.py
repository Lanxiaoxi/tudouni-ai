"""pytest 的公共准备：让 `import agent_runtime` 在没装包的情况下也成立。

`uv run pytest` 走的是**可编辑安装**（项目自己被装进 .venv 并指回源码），所以那条路上
这个文件其实是多余的。它服务的是另一条：直接 `python -m pytest`、或者在一个没同步过的
环境里跑 —— 那时候 `sys.path` 上没有仓库根，收集第一个测试文件就 ImportError。

**它不再是"扁平布局的补丁"。** 以前包就是仓库根本身，所以这里插的是**仓库的上一层**，
而那要求那个目录恰好叫 `agent_runtime`（不叫就全挂 —— 实测踩过：仓库在 GitHub 上叫
tudouni-ai，clone 下来什么都 import 不了）。现在包在 `agent_runtime/` 子目录里，插的是
仓库根本身，和目录叫什么再无关系。
"""

import sys
from pathlib import Path

# 仓库根 —— 它下面有 `agent_runtime/`，所以 `import agent_runtime` 认得。
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
