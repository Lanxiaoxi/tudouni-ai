from pathlib import Path


class FileSystem:
    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def safe_path(self, path: str) -> Path:
        target = (self.workspace / path).resolve()

        if target != self.workspace and self.workspace not in target.parents:
            raise PermissionError("Path escapes workspace")

        return target

    def read_file(self, path: str) -> str:
        target = self.safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = self.safe_path(path)
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