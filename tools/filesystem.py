from pathlib import Path


# 控制面路径：**只有人和程序自己能写**，agent 的 write_file 一律拒绝。
#
# safe_path 管的是"别出去"，管不了"有些地方别进来"。这三样东西决定的都不是任务
# 内容，而是**这次运行本身可不可信**：
#
#   .tudouni.json   权限策略 —— 能写它就等于能给自己发权限（把 shell 加进免审批名单）
#   .sessions/      会话历史 —— 能写它就能伪造"用户批准过"的记录
#   .logs/          审计日志 —— 能写它就能抹掉"谁批准了什么"的证据
#
# 而 write_file 的边界正好是整个工作区，所以这三样都在它的射程里。以前靠"每次写都
# 要人工审批"挡着；一旦有了"以后别再问这个工具"，那道闸就只剩一次按键了。
#
# 只挡写，不挡读：读不出这些文件本身没有破坏性，而 agent 看不懂自己为什么被拒时
# 只会反复重试。
CONTROL_PLANE = (".tudouni.json", ".sessions", ".logs")

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