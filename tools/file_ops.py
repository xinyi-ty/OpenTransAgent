"""文件读写工具：read_file, create_file"""

from __future__ import annotations

from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field

# ── 依赖层控制（仅用于 create_file，read_file 靠物理隔离）──
_LAYER_CTRL = None


def set_layer_ctrl(ctrl):
    global _LAYER_CTRL
    _LAYER_CTRL = ctrl


def _get_layer_ctrl():
    return _LAYER_CTRL


def _resolve_path(root: Path, filepath: str) -> Path:
    """将文件路径解析为相对于工作区根的绝对路径。"""
    p = Path(filepath)
    return (root / p).resolve() if not p.is_absolute() else p.resolve()


class ReadFileAction(Action):
    filepath: str = Field(description="要读取的文件路径")


class ReadFileObservation(Observation):
    result: str = Field(default="")
    filepath: str = Field(default="")


class ReadFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        p = _resolve_path(self.root, action.filepath)
        if not p.exists():
            return ReadFileObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
                result="", filepath=action.filepath,
            )
        try:
            content = p.read_text(encoding="utf-8")
            return ReadFileObservation.from_text(
                text=f"📄 {action.filepath}\n\n{content[:5000]}",
                result=content, filepath=action.filepath,
            )
        except Exception as e:
            return ReadFileObservation.from_text(
                text=f"读取失败: {e}", is_error=True,
                result="", filepath=action.filepath,
            )


class ReadFileTool(ToolDefinition):
    description: str = "读取文件内容"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=ReadFileAction, observation_type=ReadFileObservation,
                     executor=ReadFileExecutor(
                         workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("read_file", ReadFileTool)


class CreateFileAction(Action):
    filepath: str = Field(description="文件路径")
    content: str = Field(description="文件内容")


class CreateFileObservation(Observation):
    path: str = Field(default="")
    size: int = Field(default=0)


class CreateFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        p = _resolve_path(self.root, action.filepath)

        # 层检查：不能提前创建高层的文件
        ctrl = _get_layer_ctrl()
        if ctrl and ctrl.active:
            try:
                rel = str(p.relative_to(self.root).as_posix())
            except ValueError:
                rel = action.filepath
            if not ctrl.is_unlocked(rel):
                return CreateFileObservation.from_text(
                    text=f"⛕ '{rel}' 属于更高依赖层，当前不可创建。"
                         f"先完成当前层所有文件并通过测试。",
                    is_error=True, path=action.filepath, size=0)

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(action.content, encoding="utf-8")
            return CreateFileObservation.from_text(text=f"[OK] Created file: {action.filepath} ({len(action.content)} chars)", path=str(p), size=len(action.content))
        except Exception as e:
            return CreateFileObservation.from_text(text=f"创建文件失败: {e}", is_error=True, path=action.filepath, size=0)


class CreateFileTool(ToolDefinition):
    description: str = "创建/写入文件"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=CreateFileAction, observation_type=CreateFileObservation,
                     executor=CreateFileExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("create_file", CreateFileTool)


# ── list_files ──────────────────────────────────────────────────


class ListFilesAction(Action):
    path: str = Field(default=".", description="要列出的目录路径（相对于工作区根）")


class ListFilesObservation(Observation):
    files: list[str] = Field(default_factory=list)
    dirs: list[str] = Field(default_factory=list)
    count: int = Field(default=0)


class ListFilesExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        target = self.root
        if action.path and action.path != ".":
            p = Path(action.path)
            target = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        if not target.exists() or not target.is_dir():
            return ListFilesObservation.from_text(
                text=f"目录不存在: {action.path}", is_error=True,
            )
        files, dirs = [], []
        try:
            for entry in sorted(target.iterdir(), key=lambda x: x.name):
                rel = str(entry.relative_to(self.root).as_posix())
                if entry.is_dir():
                    dirs.append(rel + "/")
                else:
                    files.append(rel)
        except Exception as e:
            return ListFilesObservation.from_text(
                text=f"列出目录失败: {e}", is_error=True,
            )
        text = f"📁 {action.path}/  ({len(files)} files, {len(dirs)} subdirs)\n"
        if dirs:
            text += "\n  📂 " + "\n  📂 ".join(dirs)
        if files:
            text += "\n  📄 " + "\n  📄 ".join(files)
        return ListFilesObservation.from_text(
            text=text, files=files, dirs=dirs,
            count=len(files) + len(dirs),
        )


class ListFilesTool(ToolDefinition):
    description: str = "列出工作区目录下的文件和子目录（不递归）"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=ListFilesAction,
            observation_type=ListFilesObservation,
            executor=ListFilesExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("list_files", ListFilesTool)
