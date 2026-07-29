"""内容搜索工具：search_content"""

from __future__ import annotations

from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field

from config.languages import COMMON_SKIP_DIRS, get_all_code_extensions

_CODE_EXTENSIONS = get_all_code_extensions()
_SKIP_DIRS = COMMON_SKIP_DIRS
_MAX_FILE_BYTES = 1_000_000
_MAX_SCAN_FILES = 3000
_CONTEXT_LIMIT = 160


class SearchContentAction(Action):
    keyword: str = Field(description="搜索关键词")
    path: str = Field(default=".")


class SearchContentObservation(Observation):
    matches: list[str] = Field(default_factory=list)
    keyword: str = Field(default="")
    count: int = Field(default=0)


def _resolve_path(root: Path, path: str) -> Path:
    """解析搜索目录，允许工作区内绝对路径但禁止越界。"""
    workspace = root.resolve()
    p = Path(path)
    resolved = p.resolve() if p.is_absolute() else (workspace / p).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"路径超出工作区: {path}") from exc
    return resolved


def _should_skip_path(fp: Path, root: Path) -> bool:
    try:
        parts = fp.relative_to(root).parts
    except ValueError:
        parts = fp.parts
    return any(part in _SKIP_DIRS for part in parts)


def _matching_line(content: str, keyword: str) -> tuple[int, str] | None:
    needle = keyword.lower()
    for line_no, line in enumerate(content.splitlines(), start=1):
        if needle in line.lower():
            stripped = line.strip()
            if len(stripped) > _CONTEXT_LIMIT:
                stripped = stripped[:_CONTEXT_LIMIT] + "..."
            return line_no, stripped
    return None


class SearchContentExecutor(ToolExecutor):
    def __init__(
        self,
        workspace_root: str = ".",
        max_results: int = 10,
        extensions: tuple[str, ...] | None = None,
        max_scan_files: int = _MAX_SCAN_FILES,
        max_file_bytes: int = _MAX_FILE_BYTES,
    ):
        self.root = Path(workspace_root).resolve()
        self.max_results = max_results
        self.extensions = tuple(e.lower() for e in (extensions or _CODE_EXTENSIONS))
        self.max_scan_files = max_scan_files
        self.max_file_bytes = max_file_bytes

    def __call__(self, action, conversation=None):
        keyword = action.keyword.strip()
        if not keyword:
            return SearchContentObservation.from_text(
                text="搜索关键词不能为空", is_error=True,
                matches=[], keyword=action.keyword, count=0,
            )
        try:
            root = self.root if not action.path or action.path == "." else _resolve_path(self.root, action.path)
        except ValueError as e:
            return SearchContentObservation.from_text(
                text=str(e), is_error=True,
                matches=[], keyword=keyword, count=0,
            )
        if not root.exists() or not root.is_dir():
            return SearchContentObservation.from_text(
                text=f"目录不存在: {action.path}", is_error=True,
                matches=[], keyword=keyword, count=0,
            )

        matches: list[str] = []
        lines: list[str] = []
        scanned = 0
        truncated_scan = False
        for f in root.rglob("*"):
            if _should_skip_path(f, self.root) or not f.is_file():
                continue
            if f.suffix.lower() not in self.extensions:
                continue
            scanned += 1
            if scanned > self.max_scan_files:
                truncated_scan = True
                break
            try:
                if f.stat().st_size > self.max_file_bytes:
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                hit = _matching_line(content, keyword)
                if hit:
                    line_no, snippet = hit
                    rel = f.relative_to(self.root).as_posix()
                    matches.append(rel)
                    lines.append(f"{rel}:{line_no}: {snippet}")
                    if len(matches) >= self.max_results:
                        break
            except Exception:
                continue

        if matches:
            text = f"找到 {len(matches)} 个匹配:\n" + "\n".join(lines)
            if truncated_scan:
                text += f"\n... 搜索已在扫描 {self.max_scan_files} 个文件后截断"
        else:
            text = "未找到匹配"
            if truncated_scan:
                text += f"（已扫描 {self.max_scan_files} 个文件后截断）"
        return SearchContentObservation.from_text(
            text=text, matches=matches, keyword=keyword, count=len(matches),
        )


class SearchContentTool(ToolDefinition):
    description: str = "搜索文件内容"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        ws = kwargs.get("workspace_root", ".")
        mr = kwargs.get("search_max_results", 10)
        return [cls(action_type=SearchContentAction, observation_type=SearchContentObservation,
                    executor=SearchContentExecutor(workspace_root=ws, max_results=mr))]


register_tool("search_content", SearchContentTool)
