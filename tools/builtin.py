"""内置工具的装配。

工具的「参数格式」在这里定义一次、两用：

  1. 生成发给模型的 schema（**预防**：模型提前看到约束）
  2. 校验模型实际给出的参数（**兜底**：错了就明确告诉它哪个字段不对）

两者同源，所以不会漂移 —— 这条是第 4 阶段的核心结论。

为什么放在 tools/ 而不是入口：它描述的是「这个项目自带哪些工具」，属于工具层
的知识。放在入口里会有一个具体代价 —— 测试为了拿到一个工具注册表，不得不
import 整个应用入口（连带把 CLI、httpx、配置全都拖进来）。
"""

from pydantic import Field

from .ask import AskUser, AskUserArgs, Questioner
from .clock import get_current_time
from .filesystem import FileSystem
from .shell import (
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    Shell,
    shell_name,
)
from .todo import TodoArgs, TodoBoard
from .tool import RiskLevel, Tool, ToolArgs, ToolRegistry


class ReadFileArgs(ToolArgs):
    """read_file 的参数。"""

    path: str = Field(min_length=1, description="文件路径")


class WriteFileArgs(ToolArgs):
    """write_file 的参数。"""

    path: str = Field(min_length=1, description="文件路径")
    content: str = Field(description="文件内容")


class EditFileArgs(ToolArgs):
    """edit_file 的参数。

    `old_string` 用 min_length=1：空串的 `str.count` 语义是"每个字符间隙都算一次"，
    放过去会替换出一堆意料之外的东西。真正"找不到/不唯一"的判断在 handler 里 ——
    那些只有读到文件正文之后才知道。

    `replace_all` 带默认值 False：唯一匹配是**绝大多数**调用，而默认 False 意味着
    模型必须显式说"我就是要全改"，才可能误伤多处命中。
    """

    path: str = Field(min_length=1, description="要修改的文件路径")
    old_string: str = Field(
        min_length=1,
        description="要被替换掉的原文片段，必须和文件里的内容逐字符一致（含缩进和换行）",
    )
    new_string: str = Field(description="替换成的新内容；传空串表示删除这段")
    replace_all: bool = Field(
        default=False,
        description="old_string 在文件里出现多次时：true 表示全部替换，false（默认）"
                    "会拒绝执行并要求把 old_string 改得更长、更唯一",
    )


class ListFilesArgs(ToolArgs):
    """list_files 的参数。

    path 带默认值，所以生成的 schema 里它不是必填 —— 模型可以省略它。
    """

    path: str = Field(
        default=".",
        min_length=1,
        description="目录路径（相对于工作区），默认为工作区根目录",
    )


class GetCurrentTimeArgs(ToolArgs):
    """get_current_time 的参数。

    一个字段都没有 —— 拿当前时间不需要任何输入，也就没有"模型填错参数"这条路。
    空模型仍然要存在，是因为 Tool 的契约要求 args_model 必填：schema 和校验都
    从它推导，绕开它就得手写一份 schema，那就回到"两份事实互相漂移"的老问题。
    """


class ShellArgs(ToolArgs):
    """shell 的参数。

    command 写成字符串而不是 argv 数组，是因为模型要用的能力（管道、重定向、多个
    命令串联）本来就得由 shell 解析 —— 拆成数组就等于把这些能力砍掉，而安全边界
    无论如何都落在审批那一道，不在这里（见 tools/shell.py 的模块注释）。

    timeout_seconds 的边界写成 ge/le，而不是让 shell.py 自己去夹：范围必须和 schema
    同源，模型才可能**提前**看到「最多 300 秒」，而不是事后收到一个被悄悄改过的值 ——
    而且工具调用每次都要过人工审批，撞一次参数错误就得再问一次人。
    """

    command: str = Field(
        min_length=1,
        description=f"要执行的命令，按 {shell_name()} 的语法写",
    )
    timeout_seconds: int = Field(
        default=TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="最多等这条命令多少秒。默认值只够 ls / git status 这种秒回的命令，"
                    "装依赖、跑测试这类慢命令要显式调大",
    )


def create_tool_registry(
    workspace: str,
    questioner: Questioner | None = None,
    todos: TodoBoard | None = None,
) -> ToolRegistry:
    """把内置工具装成一个注册表。

    workspace 既是文件工具的沙箱根，也是唯一能拦住"往工作区外面写"的东西 ——
    所以传进来的应该是项目目录，而不是它的父目录。

    后两个参数是**两个协作方**，形状不同，各自成一条：

      * `questioner` 是一份**能力**（怎么问人），替 ask_user 挡住"怎么问"这件事；
        不传就是"没有人可问"（见 tools/ask.py 的 unavailable_questioner）。
      * `todos` 是一块**会话作用域的状态**（任务列表写在哪），必须在会话定下来之后
        才造得出来（见 tools/todo.py 的 TodoBoard）；不传就是一个没人看得见的列表。

    两个默认值都不能往"看起来能用"的方向偏：一个默认成"能问、问出来算同意"，一个
    默认成"写了但没人读"，都是那种事后完全查不出来的失败。
    """
    fs = FileSystem(workspace)
    shell = Shell(workspace)

    registry = ToolRegistry()

    # 三个 parallel_safe 的工具就是那三个**只读**的：read_file / list_files /
    # get_current_time。判定标准只有一条 —— handler 有没有副作用，而这三个连一个
    # 字节都不写、也不碰任何共享状态，所以同一批里怎么排都不会互相影响。
    #
    # 其余三个（write_file / edit_file / shell）**刻意不标**：
    #   * edit_file 是"读进来、改一段、整份写回去"，两个并发调用会互相盖掉对方
    #     （经典 lost update），而且两边都返回"已替换 1 处" —— 静默丢数据。
    #   * write_file 不是原子写（写了一半的文件会被同批的 read_file 读到）。
    #   * shell 能改工作区里任何东西，"这两条命令彼此独立"运行时**无法验证**。
    registry.register(Tool(
        name="read_file",
        description=(
            "读取指定文件的全部内容（按 UTF-8 解码，不分页）。"
            "文件不存在、路径指向目录、或超出工作区都会报错。"
        ),
        risk=RiskLevel.LOW,
        args_model=ReadFileArgs,
        handler=fs.read_file,
        parallel_safe=True,
    ))

    registry.register(Tool(
        name="write_file",
        description=(
            "把内容写入指定文件。整个文件会被替换 —— 不是追加，也不是局部修改；"
            "缺失的父目录会自动创建。所以要改动一个已存在的文件，必须先 read_file "
            "读出原文，再基于真实内容写出完整的新文本：凭记忆或凭猜测写会丢数据。"
            "只改其中一小段（尤其是文件很长时）应该用 edit_file，不必把整篇正文背出来写回去。"
        ),
        risk=RiskLevel.MEDIUM,
        args_model=WriteFileArgs,
        handler=fs.write_file,
    ))

    # 风险定 MEDIUM，和 write_file 同档：它就是"改文件"，只是改动范围更小。
    # 不能因为"改得少"就降成 LOW —— 它照样能改工作区里任何一个文件（控制面除外），
    # 而 LOW 是自动放行的。比 write_file 更安全的只是它的**形态**（只动一小段、
    # 匹配不唯一就拒绝），不是它的**权限**。
    #
    # 它和 write_file 的分工必须写在描述里，因为**模型才是那个要做选择的人**：
    # 它得知道"改一小段"该用 edit_file（只规定哪里变），而不是回退到整文件覆盖
    # （要为没动过的部分也负责）—— 后者正是丢数据的来路。
    registry.register(Tool(
        name="edit_file",
        description=(
            "把文件里的一段原文替换成新内容，是修改已有文件的首选方式 —— "
            "文件其余部分不经你的手，所以不会因为把没动过的内容背错而丢数据。\n"
            "old_string 必须和文件里的原文**逐字符一致**（含缩进和换行），因此通常"
            "要先 read_file 看清原文；凭记忆拼出来的片段会在匹配失败时被打回。\n"
            "old_string 在文件里出现多次时，默认拒绝执行并要你把它改得唯一，"
            "除非显式设 replace_all=true。文件不存在、或不是 UTF-8 文本，都改不了"
            "（新建文件用 write_file）。"
        ),
        risk=RiskLevel.MEDIUM,
        args_model=EditFileArgs,
        handler=fs.edit_file,
    ))

    registry.register(Tool(
        name="list_files",
        description=(
            "列出目录下的条目名字。只列一层、不递归，也不返回大小、类型或修改时间。"
            "要摸清目录结构就逐层调用；目录不存在或路径不是目录会报错。"
        ),
        risk=RiskLevel.LOW,
        args_model=ListFilesArgs,
        handler=fs.list_files,
        parallel_safe=True,
    ))

    # 风险定 LOW：它只读时钟、没有副作用、不碰工作区，放行不需要问人 ——
    # 和 read_file / list_files 同档，也就落在 main.py 的 auto_approve 里。
    registry.register(Tool(
        name="get_current_time",
        description="获取当前时间（ISO 8601，含时区偏移）",
        risk=RiskLevel.LOW,
        args_model=GetCurrentTimeArgs,
        handler=get_current_time,
        parallel_safe=True,
    ))

    # 风险定 HIGH，而且**刻意不做参数级判断** —— 理由见 tools/shell.py 的模块注释。
    # 后果是明确的：main.py 的 auto_approve 里只有 LOW，所以每一条命令都会被拦下来
    # 问人。这是唯一诚实的默认值 —— 想给"只读命令"开自动放行，得先有 OS 级沙箱。
    #
    # 描述里的输出上限是从 shell.py 的常量插值来的，不手抄第二份 —— 参数在一处改、
    # 说明书跟着变，和 tool.py 里「schema 由 args_model 推导」是同一条原则。
    # 超时那件事不在这里复述：它是 timeout_seconds 这个参数自己的约束，由 schema 的
    # default/minimum/maximum 表达，写在散文里就成了第二份。
    registry.register(Tool(
        name="shell",
        description=(
            f"在工作区目录下执行一条 shell 命令（{shell_name()} 语法），返回输出和退出码。"
            f"命令是非交互的：需要输入时会立刻读到 EOF。"
            f"输出超过 {MAX_OUTPUT_CHARS} 字符会掐掉中间，头和尾都留着。"
            f"它不受文件工具那条路径限制 —— 命令能碰到工作区之外的路径。"
        ),
        risk=RiskLevel.HIGH,
        args_model=ShellArgs,
        handler=shell.run,
    ))

    # 提问工具。三条决定：
    #
    # 1. **风险 LOW。** 提问没有副作用、不碰工作区、不改任何状态，所以放行它不该问人
    #    —— 否则会变成"为了问一个问题，先弹一次审批"，而那个审批本身才是打断。
    #    （若有人把 auto_approve 配成空集，它确实会被先审一次：荒诞但 fail-closed，
    #    不为它在策略里开后门。）
    # 2. **interactive=True。** 它的 handler 阻塞在人的输入上，所以永远不能并行 ——
    #    见 tool.py 里那条注册期校验。
    # 3. **描述里必须带负面清单。** 这是唯一一条"什么时候**不要**用我"比"怎么用我"
    #    更要紧的工具：提示词可能对老会话已经过期（system 消息只在新建会话时写一次），
    #    而工具描述每一轮都发。规则写在这里，恢复的旧会话也一定看得到。
    #
    # 它**不产生任何权限效果**：拿到"用户同意了"不会让下一个 shell 调用免审。这条不是
    # 风格问题 —— 能靠提问换放行的话，模型自己编一句"我已征得同意"就成了绕过审批的路。
    registry.register(Tool(
        name="ask_user",
        description=(
            "向用户提一个问题，并把他的回答作为这次调用的结果拿到。"
            "只在缺了它就没法选对工具或参数时才用：能从工作区里自己查清的一律自己查，"
            "偏好类的问题不要问。更不要拿它去征求执行许可 —— 审批由 runtime 负责，"
            "你照常调用即可；用提问代替审批只会多一次打断。"
            "options 给了就按编号显示给用户，留空表示让他自由作答。"
            "一次只问一个问题，同一个问题不要问第二遍。"
        ),
        risk=RiskLevel.LOW,
        args_model=AskUserArgs,
        handler=AskUser(questioner),
        interactive=True,
    ))

    # 任务列表。三条决定：
    #
    # 1. **风险 LOW。** 它只改会话里属于它自己的那一小块，碰不到工作区、也碰不到控制面
    #    —— 所以放行它不该问人（Claude Code 那套任务管理工具同样是"不触发权限确认"）。
    # 2. **不能并行。** 它写的是会话 metadata 这块共享状态，一批里两条同时跑就是
    #    经典 lost update，而且两边都会报成功（和 edit_file 不能并行是同一个理由）。
    #    不声明 parallel_safe 就够了：那一条会让整批退回串行。
    # 3. **描述里必须写清"什么时候不要建列表"。** 这是最容易变成仪式的工具 —— 单步的
    #    琐碎活也建一张三级列表，除了烧 token 和让模型忙着更新状态之外没有任何作用。
    #    和 ask_user 同理，规则写在这里而不是只写提示词：恢复的旧会话看不到新提示词，
    #    但每一轮都看得见工具描述。
    registry.register(Tool(
        name="todo_write",
        description=(
            "把你当前的多步计划整份写下来，让它在后面几十步里不会走丢。"
            "**只在活儿明显不止一两步、而且你能列出具体步骤时用** —— 单步的琐碎事不要"
            "建列表，也不要为了好看而建。\n"
            "每次都要传**完整的新列表**：它会替换上一次的整份列表，不是增量。"
            "开工前把步骤列出来；开始做哪条就把它标成 in_progress（真的在并行时可以"
            "同时标好几条）；做完一条立刻标 completed，不要攒着一起标。"
            "只要还有没做完的，列表里就该有一条 in_progress。全部完成时传空数组把列表"
            "清掉。\n"
            "它是进度，不是任务本身：别把时间花在反复整理列表上。"
        ),
        risk=RiskLevel.LOW,
        args_model=TodoArgs,
        handler=TodoBoard() if todos is None else todos,
    ))

    return registry
