"""Textual 客户端（`main.py --tui`）。

**这个包是唯一准 import textual 的地方。** 而且连它内部也不是处处可以：

| 模块 | 能不能 import textual | 为什么 |
|---|---|---|
| `app.py` / `widgets.py` | **能** | 它们就是界面 |
| `view_state.py` | **不能** | 它是纯函数（事件 → 给人看的行），**可单测**，而且不该拖一个 UI 框架进测试 |
| `theme.py` | **不能** | 14 套配色是**纯数据**（对比度、角色齐全、`/theme` 的匹配规则都要能单测）。它不 import textual，所以也不需要进 `_TEXTUAL_ALLOWED` 那张白名单 |
| `__init__.py` | **不能** | 在包顶层 import 会把 textual 拖进**所有**路径 —— 老 CLI、协议子进程、`--list` 那些查询子命令，全都白付这笔加载费（在没有 textual 的环境里则是直接崩） |

最后一行是最容易破的：有人在 `__init__.py` 里加一句"顺便"的 re-export 就破了。
`tests/test_imports.py` 里有一条测试盯着它，所以不要在这上面加东西 ——
`main.py` 那一侧用的是**函数内 import**（见 `main.py` 的 `--tui` 分支）。
"""
