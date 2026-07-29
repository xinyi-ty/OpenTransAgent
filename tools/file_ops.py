"""文件读写工具：read_file, create_file, list_files。"""

from __future__ import annotations

import os
from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field

# ── 依赖层控制（仅用于 create_file，read_file 靠物理隔离）──
_LAYER_CTRL = None
_READ_TEXT_LIMIT = 5000
_LIST_MAX_ENTRIES = 200


def set_layer_ctrl(ctrl):
    global _LAYER_CTRL
    _LAYER_CTRL = ctrl


def _get_layer_ctrl():
    return _LAYER_CTRL


def _resolve_path(root: Path, filepath: str) -> Path:
    """将文件路径解析为工作区内绝对路径，允许工作区内绝对路径但禁止越界。"""
    workspace = root.resolve()
    p = Path(filepath)
    resolved = p.resolve() if p.is_absolute() else (workspace / p).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"路径超出工作区: {filepath}") from exc
    return resolved


def _to_rel(root: Path, path: Path) -> str:
    """返回相对工作区的 POSIX 风格路径。"""
    return path.relative_to(root).as_posix()


def _read_text(path: Path) -> str:
    """容错读取 UTF-8 文本。"""
    return path.read_text(encoding="utf-8", errors="replace")


def _preview_text(content: str, limit: int = _READ_TEXT_LIMIT) -> str:
    """生成给 LLM 的截断预览。"""
    if len(content) <= limit:
        return content
    return (
        content[:limit]
        + f"\n... (truncated {len(content) - limit} chars; full size {len(content)} chars)"
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写入文本文件，减少中断时留下半文件的概率。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class ReadFileAction(Action):
    filepath: str = Field(description="要读取的文件路径")


class ReadFileObservation(Observation):
    result: str = Field(default="")
    filepath: str = Field(default="")


class ReadFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        try:
            p = _resolve_path(self.root, action.filepath)
        except ValueError as e:
            return ReadFileObservation.from_text(
                text=str(e), is_error=True,
                result="", filepath=action.filepath,
            )
        if not p.exists() or not p.is_file():
            return ReadFileObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
                result="", filepath=action.filepath,
            )
        try:
            content = _read_text(p)
            rel = _to_rel(self.root, p)
            return ReadFileObservation.from_text(
                text=f"📄 {rel}\n\n{_preview_text(content)}",
                result=content, filepath=rel,
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
    advisory_code: str = Field(default="")
    advisory_message: str = Field(default="")
    write_count: int = Field(default=0)
    rewrite_count: int = Field(default=0)


class CreateFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()
        self._successful_writes: dict[str, int] = {}

    def __call__(self, action, conversation=None):
        try:
            p = _resolve_path(self.root, action.filepath)
        except ValueError as e:
            return CreateFileObservation.from_text(
                text=str(e), is_error=True, path=action.filepath, size=0,
            )
        rel = _to_rel(self.root, p)

        # 层检查：不能提前创建高层的文件
        ctrl = _get_layer_ctrl()
        if ctrl and ctrl.active:
            if not ctrl.is_unlocked(rel):
                return CreateFileObservation.from_text(
                    text=f"⛕ '{rel}' 属于更高依赖层，当前不可创建。"
                         f"先完成当前层所有文件并通过测试。",
                    is_error=True, path=rel, size=0)

        try:
            existed_before = p.is_file()
            _atomic_write_text(p, action.content)
            previous_writes = self._successful_writes.get(rel, 0)
            write_count = previous_writes + 1
            self._successful_writes[rel] = write_count
            rewrite_count = max(0, write_count - 1) if previous_writes else (1 if existed_before else 0)
            advisory_code = ""
            advisory_message = ""
            text = f"[OK] Created file: {rel} ({len(action.content)} chars)"
            if rewrite_count >= 2:
                advisory_code = "repeated_full_rewrite"
                advisory_message = (
                    f"This file has been fully rewritten {rewrite_count} times. "
                    f"For another small correction, prefer edit_file with a precise non-empty "
                    f"old_string; keep using create_file when a complete replacement is intentional."
                )
                text += f"\nAdvisory: {advisory_message}"
            return CreateFileObservation.from_text(
                text=text,
                path=rel, size=len(action.content),
                advisory_code=advisory_code,
                advisory_message=advisory_message,
                write_count=write_count,
                rewrite_count=rewrite_count,
            )
        except Exception as e:
            return CreateFileObservation.from_text(
                text=f"创建文件失败: {e}", is_error=True, path=rel, size=0,
            )


class CreateFileTool(ToolDefinition):
    description: str = "创建新/空文件，或有意原子替换文件全部内容；小范围修改优先 edit_file"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=CreateFileAction, observation_type=CreateFileObservation,
                    executor=CreateFileExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("create_file", CreateFileTool)


class EditFileAction(Action):
    filepath: str = Field(description="要修改的已有文件路径")
    old_string: str = Field(description="非空的精确原文；必须存在且默认唯一，空文件/全量替换请用 create_file")
    new_string: str = Field(description="替换后的文本")
    replace_all: bool = Field(default=False, description="是否替换所有匹配项")


class EditFileObservation(Observation):
    path: str = Field(default="")
    replacements: int = Field(default=0)


class EditFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        try:
            p = _resolve_path(self.root, action.filepath)
        except ValueError as e:
            return EditFileObservation.from_text(
                text=str(e), is_error=True, path=action.filepath, replacements=0,
            )
        rel = _to_rel(self.root, p)

        ctrl = _get_layer_ctrl()
        if ctrl and ctrl.active and not ctrl.is_unlocked(rel):
            return EditFileObservation.from_text(
                text=f"⛕ '{rel}' 属于更高依赖层，当前不可修改。先完成当前层所有文件并通过测试。",
                is_error=True, path=rel, replacements=0,
            )

        if not p.exists() or not p.is_file():
            return EditFileObservation.from_text(
                text=f"文件不存在: {rel}", is_error=True, path=rel, replacements=0,
            )
        if action.old_string == action.new_string:
            return EditFileObservation.from_text(
                text="old_string 与 new_string 相同，无需修改", is_error=True,
                path=rel, replacements=0,
            )
        if action.old_string == "":
            return EditFileObservation.from_text(
                text="old_string 不能为空", is_error=True, path=rel, replacements=0,
            )

        try:
            content = _read_text(p)
            count = content.count(action.old_string)
            if count == 0:
                return EditFileObservation.from_text(
                    text=f"未找到 old_string: {rel}", is_error=True,
                    path=rel, replacements=0,
                )
            if count > 1 and not action.replace_all:
                return EditFileObservation.from_text(
                    text=f"old_string 在 {rel} 中出现 {count} 次；请提供更精确上下文或设置 replace_all=true",
                    is_error=True, path=rel, replacements=0,
                )
            new_content = content.replace(
                action.old_string,
                action.new_string,
                -1 if action.replace_all else 1,
            )
            _atomic_write_text(p, new_content)
            replacements = count if action.replace_all else 1
            return EditFileObservation.from_text(
                text=f"[OK] Edited file: {rel} ({replacements} replacement(s))",
                path=rel, replacements=replacements,
            )
        except Exception as e:
            return EditFileObservation.from_text(
                text=f"修改文件失败: {e}", is_error=True, path=rel, replacements=0,
            )


class EditFileTool(ToolDefinition):
    description: str = "对已有文件做精确局部替换；old_string 必须非空且默认唯一"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=EditFileAction, observation_type=EditFileObservation,
                    executor=EditFileExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("edit_file", EditFileTool)


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
        try:
            target = self.root if not action.path or action.path == "." else _resolve_path(self.root, action.path)
        except ValueError as e:
            return ListFilesObservation.from_text(
                text=str(e), is_error=True,
            )
        if not target.exists() or not target.is_dir():
            return ListFilesObservation.from_text(
                text=f"目录不存在: {action.path}", is_error=True,
            )
        files, dirs = [], []
        truncated = False
        try:
            for i, entry in enumerate(sorted(target.iterdir(), key=lambda x: x.name)):
                if i >= _LIST_MAX_ENTRIES:
                    truncated = True
                    break
                rel = _to_rel(self.root, entry)
                if entry.is_dir():
                    dirs.append(rel + "/")
                else:
                    files.append(rel)
        except Exception as e:
            return ListFilesObservation.from_text(
                text=f"列出目录失败: {e}", is_error=True,
            )
        rel_target = "." if target == self.root else _to_rel(self.root, target)
        text = f"📁 {rel_target}/  ({len(files)} files, {len(dirs)} subdirs"
        if truncated:
            text += f", truncated at {_LIST_MAX_ENTRIES} entries"
        text += ")\n"
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
