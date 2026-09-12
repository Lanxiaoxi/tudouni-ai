from pathlib import Path

from agent_runtime.skills import TUDOUNI_DIR_NAME

# 控制面路径：**只有人和程序自己能写**，agent 的 write_file 一律拒绝。
#
#   .tudouni.json   权限策略 —— 能写它就等于能给自己发权限（把 shell 加进免审批名单）
#   .tudouni/       运行时的私有目录，里面住着**技能**（.tudouni/skills/<name>/SKILL.md）
#   .sessions/      会话历史 —— 能写它就能伪造"用户批准过"的记录
#   .logs/          审计日志 —— 能写它就能抹掉"谁批准了什么"的证据
#
# 而 write_file 的边界正好是整个工作区，所以这几样都在它的射程里。以前靠"每次写都
# 要人工审批"挡着；一旦有了"以后别再问这个工具"，那道闸就只剩一次按键了。
#
# **技能目录为什么必须在这里。** 技能正文不是数据，是**指令**：它加载之后会拼进此后
# 每一次请求。能写它就等于能改自己接下来每一轮的指令，而且改一次就永久生效 ——
# 这是控制面里最危险的那一个（比改权限策略更隐蔽：策略改了至少还有一条 permission
# 事件，而"我给自己加了三条规矩"在审计里什么都看不出来）。所以它是"读可以、写一律
# 拒绝，人批准了也不行"，和另外三个一样。
#
# 整个 `.tudouni/` 目录都拒绝写，而不是只拒绝 `skills/` 子目录：那个前缀之下将来放什么
# 都是运行时的私有物，逐个点名会漏（和 auto_approve 不收 "high" 是同一个取向 ——
# 规则的作用范围必须一眼看得懂，而且不能随着将来新增的东西自动变宽）。
#
# 目录名从 skills 包借常量、而不是在这里再写一遍字面量：它是"技能住在哪"这个事实的
# 唯一来源（skills/loader.py），抄第二份的话，哪天目录改名就会变成"技能还在加载，
# agent 却已经能写了"—— 一个不会有任何报错的组合，所以 tests/test_skills.py 里有一条
# 测试盯着这张表和技能目录是同名的。
#
# 只挡写，不挡读：读不出这些文件本身没有破坏性，而 agent 看不懂自己为什么被拒时
# 只会反复重试。
CONTROL_PLANE = (".tudouni.json", TUDOUNI_DIR_NAME, ".sessions", ".logs")

# 比较必须折叠大小写：Windows 的文件系统不区分大小写，`.TUDOUNI.JSON` 写下去就是
# 同一个文件。在 Linux 上那是另一个文件，但"绕过检查"的形态没必要放行任何一种。
_CONTROL_PLANE_FOLDED = frozenset(name.casefold() for name in CONTROL_PLANE)


class FileSystem:
    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def safe_path(self, path: str) -> Path:
        target = (self.workspace / path).resolve()

        if target != self.workspace and self.workspace not in target.parents:
            raise PermissionError("Path escapes workspace")

        return target

    def writable_path(self, path: str) -> Path:
        """写操作的落点：先过工作区边界，再过控制面。

        和 safe_path 分开而不是加参数，是因为两件事的失败方向相反 —— 越界是
        "不该出去"，控制面是"不该进来"。合成一个函数，读路径也会被第二道检查挡住，
        而读这些文件是正当的（agent 该看得见自己项目的策略）。

        `.resolve()` 已经把符号链接展开了，所以"先建一个指向 .tudouni.json 的软链再写"
        这条弯路也会落到同一张表上。
        """
        target = self.safe_path(path)
        rel = target.relative_to(self.workspace)

        # 只认工作区根部的第一个路径段：控制面是"根目录那个文件 / 那两个目录"，
        # 不是"叫这个名字的任何东西"。深一层的 sub/.tudouni.json 只是普通文件。
        if rel.parts and rel.parts[0].casefold() in _CONTROL_PLANE_FOLDED:
            raise PermissionError(
                f"Path is control plane, agent must not write it: {rel.parts[0]}"
            )

        return target

    def read_file(self, path: str) -> str:
        target = self.safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = self.writable_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path}"

    def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """把文件里的 old_string 换成 new_string，返回一句给模型的话。

        **预期内的失败返回文本，不抛异常** —— 跟着 grep.py / shell.py 那条规矩走，
        而不是同一个模块里的 read_file / write_file。理由是失败的性质：`old_string`
        对不上，意味着模型**对文件内容的记忆过期了**（没读过，或读过之后文件又变了），
        这是它读着一句话就能自己修正的事。抛出去会被 agent.py 记成工具故障，而它恰恰
        拿不到那句最该看到的话 —— "先 read_file 看清原文"。

        边界仍然抛：工作区越界和控制面由 writable_path 拦下（PermissionError），
        那不是模型调参数能绕开的东西，撞上就该撞上。

        为什么需要它：write_file 是**整文件覆盖**，改一行也要把整篇正文背出来写回去，
        背错一个字就是静默丢数据。edit_file 只规定"哪里变、变成什么"，文件里其余内容
        根本不经模型的手 —— 这才是它防丢数据的地方。

        **换行按文件自己的约定走，而且读写都不翻译**（`newline=""`）。这一条不是洁癖：
        用 Path.read_text/write_text 的话，读的时候 CRLF 被归一成 \\n、写的时候 \\n 又被
        翻成 os.linesep（Windows 上是 CRLF），于是编辑一个 LF 文件里的**一行**，整份
        文件的换行全变 —— "其余内容不经你的手"当场破功。而它偏偏最不容易被发现：
        autocrlf=true 时 git 会把两边的换行都归一，`git diff` 只显示那一行。
        """
        target = self.writable_path(path)

        if not target.exists():
            return f"文件不存在：{path}（edit_file 只改已存在的文件；新建用 write_file）"
        if target.is_dir():
            return f"不是文件：{path}（要列目录里的东西用 list_files）"

        if not old_string:
            # 参数模型用 min_length=1 挡了空串；这里是兜底 —— 空串的 count 语义是
            # "每个字符间隙都算一次"，放过去会替换出一堆意料之外的东西。
            return "old_string 不能为空"

        try:
            with target.open(encoding="utf-8", newline="") as handle:
                original = handle.read()
        except UnicodeDecodeError:
            return f"不是 UTF-8 文本，改不了：{path}"

        # 模型给的片段来自 read_file，那里的换行已经被归一成 \n；而这里的 original 是
        # 逐字节的原文。所以对齐到**文件自己的**换行约定，两个方向都必要：
        #   * 不对齐 —— CRLF 文件匹配不上模型那份 LF 的片段，改不动；
        #   * 反过来在写的时候让 Python 翻译回 os.linesep —— LF 文件被整份改成 CRLF。
        # 混合换行的文件按多数派对齐：没被替换的字节仍然一个都不动，但一段**跨过**
        # 少数派换行的 old_string 可能匹配不上（那种文件本来就该先统一）。
        newline = "\r\n" if "\r\n" in original else "\n"
        old = old_string.replace("\r\n", "\n").replace("\n", newline)
        new = new_string.replace("\r\n", "\n").replace("\n", newline)

        # 比的是对齐之后的两段：old="a\nb"、new="a\r\nb" 在 CRLF 文件里就是同一段内容，
        # 报"没有变化"比报成功更诚实。
        if old == new:
            return f"{path} 没有变化：old_string 和 new_string 是同一段内容"

        occurrences = original.count(old)

        if occurrences == 0:
            return (
                f"在 {path} 里找不到 old_string，文件没有被改动。\n"
                f"先 read_file 读一遍原文 —— 缩进和换行必须逐字符一致，"
                f"凭记忆拼出来的片段通常就差在这里。"
            )

        if occurrences > 1 and not replace_all:
            # 不唯一就什么都不做。这是 edit 相对 write 的核心价值：宁可让模型把
            # old_string 补得更长，也不能替它猜"改哪一处" —— 猜错了是静默改错地方。
            return (
                f"old_string 在 {path} 里出现了 {occurrences} 次，无法确定改哪一处，"
                f"文件没有被改动。\n"
                f"想只改一处：把 old_string 前后多带几行，让它唯一。"
                f"想全部改：设 replace_all=true。"
            )

        # newline="" 关闭写入时的换行翻译：上面已经把 new 对齐到文件的约定，再让
        # Python 翻一次就会把 \r\n 变成 \r\r\n。
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(original.replace(old, new, -1 if replace_all else 1))

        return f"已替换全部 {occurrences} 处" if replace_all else f"已替换 {path} 里 1 处"

    def list_files(self, path: str = ".") -> list[str]:
        """列出目录下的文件和文件夹"""
        target = self.safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        
        result = []
        for item in target.iterdir():
            result.append(item.name)
        return result
