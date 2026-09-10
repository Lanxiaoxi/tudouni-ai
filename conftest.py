"""pytest 的公共准备。

sys.path 只在这里准备一次：项目用 `package = false`，包没有被安装，所以任何入口
都得自己把仓库根塞进 sys.path —— main.py 里也在做同一件事。测试绝不能各写一份，
否则改一处要改 N 处。
"""

import sys
from pathlib import Path

# 仓库根（包目录的上一层）。`import agent_runtime` 需要它在 sys.path 上。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
