"""命令行参数的形状。

**这里只有参数的形状，没有版本的语义。** 它从 `cli.py` 提出来，因为现在有两个
消费者：启动器（`main.py`）要先读参数才知道该走哪条路，而 CLI 前端要用同一份形状
去解释它们。放在 `cli.py` 里的话，`main.py` 为了拿到一个 parser 就得 import 整个
CLI 前端（连带横幅、审计渲染、REPL）—— 那正是第零期要拆掉的那种耦合。

四个"不需要模型"的子命令（`--list` / `--skills` / `--audit` / `--history`）的实现
也住在 `frontends/cli/`（决策 20）：它们只读 store / logs / 技能目录，不装配 Runtime。

**参数形状和它们的实现同属这一层** —— 所以这个文件在 `frontends/cli/` 里、不在
`runtime/` 里：参数是前端的契约，不是装配层的知识。第零期那条"前端不许 import
runtime 内部"的测试（`tests/test_imports.py`）第一次跑就把我最初放错的位置抓了出来。
"""

import argparse

from agent_runtime import version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Runtime 命令行入口")
    # **`--version` 排在最前面，而且它由 argparse 自己处理**（打印完就 `sys.exit(0)`）：
    # 于是它和 `--session` 的合法性检查、工作区检查、配置检查全都不相干 —— 一个连配置
    # 都没有的人也应该能问出"我装的是哪一版"。
    #
    # 版本号来自 `agent_runtime/version.py`，**不是这里的字面量**：那个数字的真值在
    # `pyproject.toml`（发布用的那个），而冻结产物里没有那个文件，所以打包时会额外带上
    # 一个戳。见那个模块的 docstring。
    parser.add_argument(
        "--version", action="version", version=version.describe(),
        help="打印版本号然后退出",
    )
    parser.add_argument(
        "--session", default=None,
        help="会话 id。给了就接着那个会话聊（不存在则新建）；不给就自动开一个新的",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="打印 --session 指定会话的对话历史，不调用模型",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="打印 --session 指定会话的审计轨迹（token、权限裁决、耗时），不调用模型",
    )
    parser.add_argument("--list", action="store_true", help="列出已保存的会话")
    parser.add_argument(
        "--skills", action="store_true",
        help="列出工作区里的技能（.tudouni/skills/<名字>/SKILL.md），不调用模型",
    )
    parser.add_argument(
        "--autopilot", action="store_true",
        help="不询问任何审批：需要审批的工具直接执行。拒绝名单、工作区边界、控制面写入"
             "仍然生效；审计里每次放行记为 outcome=autopilot",
    )
    parser.add_argument(
        "--ericai", action="store_true",
        help="启动时检查 EricAI token（providers.ericai.api_key），快过期就用 config "
             "的 scripts.ericai_refresh_token 那行命令刷一把新的并写回 config",
    )
    parser.add_argument("--debug", action="store_true", help="把中间过程打到 stderr")

    # 下面两个开关是 TUI 的一对父子（见 doc/TUI-design.md）。**第零期只是把它们声明
    # 出来，还没有接线** —— 加参数不影响任何现有路径（没人传它们就等于不存在），
    # 放在这里是因为"参数的形状"属于这一层，而实现属于后面的期。
    parser.add_argument(
        "--tui", action="store_true",
        help="用 TUI 界面（它自己去拉起一个 --runtime-stdio 子进程）",
    )
    parser.add_argument(
        "--runtime-stdio", action="store_true",
        help="把 stdout 变成 JSONL 协议通道（由 --tui 拉起，一般不由人直接跑）",
    )
    parser.add_argument(
        "--theme", default=None,
        help="TUI 的配色（14 套：a 石墨琥珀是默认；也能给名字，如 --theme 靛夜）。"
             "运行中还能用 /theme 换",
    )
    # 流式：**两个方向都写成显式开关**，因为它有两个默认值 —— TUI 默认开、老 CLI
    # 默认关（见 main.py 那一支）。用 `store_true` + `store_false` 的一对而不是
    # `BooleanOptionalAction`：后者在 `--help` 里显示成 `--stream | --no-stream`，
    # 看不出"哪个是默认"，而这个参数的默认值**取决于走哪条路**，说不清就是误导。
    parser.add_argument(
        "--stream", dest="stream", action="store_true", default=None,
        help="让模型逐字输出（TUI 默认开；老 CLI 不支持，传了会忽略并说一句）",
    )
    parser.add_argument(
        "--no-stream", dest="stream", action="store_false", default=None,
        help="不要逐字输出（老 CLI 默认如此；TUI 上就是答案整段出现）",
    )
    return parser
