"""多步上下文收集工具：类信息提取、文件搜索、依赖查询。"""

from __future__ import annotations

import re
from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


# ═══════════════════════════════════════════════════════════════
#  共享辅助函数
# ═══════════════════════════════════════════════════════════════

_CODE_EXTENSIONS = {
    ".py", ".java", ".cpp", ".cxx", ".cc", ".c", ".h", ".hpp", ".hxx",
    ".cs", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx",
}
_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", "build", "dist", "target", "CMakeFiles",
}
_CONTROL_WORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "static_cast",
    "dynamic_cast", "reinterpret_cast", "const_cast",
}


def _resolve(fp: str, root: Path) -> Path:
    """将文件路径解析为工作区内绝对路径，禁止越界访问。"""
    workspace = root.resolve()
    p = Path(fp)
    resolved = p.resolve() if p.is_absolute() else (workspace / p).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"路径超出工作区: {fp}") from exc
    return resolved


def _read_text(fp: Path) -> str:
    """以容错方式读取文本文件。"""
    return fp.read_text(encoding="utf-8", errors="replace")


def _is_code_file(fp: Path) -> bool:
    return fp.suffix.lower() in _CODE_EXTENSIONS


def _should_skip_path(fp: Path, root: Path) -> bool:
    try:
        parts = fp.relative_to(root).parts
    except ValueError:
        parts = fp.parts
    return any(part in _SKIP_DIRS for part in parts)


def _fields(content: str) -> list[str]:
    """从代码块中提取字段名（启发式，去重保序）。"""
    found: list[str] = []
    patterns = [
        # Python: self.name = ...
        r"\bself\.(\w+)\s*=",
        # C/C++/Java/C#: Type name; / Type name = ...
        r"^[ \t]*(?:public|private|protected)?\s*(?:static\s+|const\s+|readonly\s+|final\s+)?[\w:<>,*&\[\] ]+\s+(\w+)\s*(?:[;=])",
    ]
    for pattern in patterns:
        for name in re.findall(pattern, content, re.MULTILINE):
            if name not in _CONTROL_WORDS and name not in found:
                found.append(name)
            if len(found) >= 15:
                return found
    return found


def _methods(content: str) -> list[str]:
    """从代码块中提取方法名（启发式，去重保序）。"""
    found: list[str] = []

    # Python def / async def
    for name in re.findall(r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(", content, re.MULTILINE):
        if name not in found:
            found.append(name)

    # C-like method/function signatures. 排除控制流和普通调用。
    c_like = re.findall(
        r"^[ \t]*(?:template\s*<[^>]+>\s*)?"
        r"(?:[\w:<>,~*&\[\]\s]+\s+)?"
        r"((?:\w+::)*~?\w+)\s*"
        r"\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:final\s*)?(?:\{|;)",
        content,
        re.MULTILINE,
    )
    for raw in c_like:
        name = raw.strip().split("::")[-1].strip()
        if name in _CONTROL_WORDS:
            continue
        if name not in found:
            found.append(name)
        if len(found) >= 15:
            break
    return found[:15]


def _class_info_to_text(class_name: str, fields: list[str], methods: list[str]) -> str:
    """将类信息格式化为可读文本。"""
    parts = [f"类 {class_name}:"]
    if fields:
        parts.append("  字段:\n" + "\n".join(f"    - {f}" for f in fields))
    if methods:
        parts.append("  方法:\n" + "\n".join(f"    - {m}" for m in methods))
    if not fields and not methods:
        parts.append("  （未提取到字段或方法）")
    return "\n".join(parts)


def _extract_class_block(content: str, class_name: str) -> str | None:
    """从代码中提取指定类/结构体的代码块（启发式）。"""
    class_decl = re.compile(
        rf"^[ \t]*(?:template\s*<[^>]+>\s*)?(class|struct)\s+{re.escape(class_name)}\b[^\n]*",
        re.MULTILINE,
    )
    m = class_decl.search(content)
    if not m:
        return None

    next_decl = re.compile(r"^[ \t]*(?:template\s*<[^>]+>\s*)?(?:class|struct)\s+\w+\b", re.MULTILINE)
    n = next_decl.search(content, m.end())
    end = n.start() if n else len(content)
    return content[m.start():end].strip()


def _matching_line(content: str, pattern: str) -> str:
    regex = re.compile(pattern)
    for i, line in enumerate(content.splitlines(), start=1):
        if regex.search(line):
            return f"line {i}: {line.strip()}"
    return ""


def _search_pattern_in_files(
    root: Path,
    pattern: str,
    max_files: int = 2000,
) -> tuple[str | None, str | None, str | None]:
    """在工作目录中搜索匹配指定模式的文件。

    返回 (filepath, content, matched_line)，未找到时返回 (None, None, None)。
    """
    scanned = 0
    for fp in root.rglob("*"):
        if _should_skip_path(fp, root) or not fp.is_file() or not _is_code_file(fp):
            continue
        scanned += 1
        if scanned > max_files:
            break
        try:
            content = _read_text(fp)
            if re.search(pattern, content):
                rel = str(fp.relative_to(root))
                return rel, content, _matching_line(content, pattern)
        except Exception:
            continue
    return None, None, None


def _path_error_observation(observation_cls, error: ValueError):
    return observation_cls.from_text(text=str(error), is_error=True)


# ═══════════════════════════════════════════════════════════════
#  get_source_class_info  — 从源文件提取类的字段和方法
# ═══════════════════════════════════════════════════════════════


class GetSourceClassInfoAction(Action):
    filepath: str = Field(description="文件路径")
    class_name: str = Field(description="类名")


class GetSourceClassInfoObservation(Observation):
    class_name: str = Field(default="")
    fields: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)


class GetSourceClassInfoExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        try:
            fp = _resolve(action.filepath, self.root)
        except ValueError as e:
            return _path_error_observation(GetSourceClassInfoObservation, e)
        if not fp.exists():
            return GetSourceClassInfoObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        content = _read_text(fp)
        block = _extract_class_block(content, action.class_name)
        if not block:
            return GetSourceClassInfoObservation.from_text(
                text=f"未找到类: {action.class_name}", is_error=True,
            )
        fs = _fields(block)
        ms = _methods(block)
        return GetSourceClassInfoObservation.from_text(
            text=_class_info_to_text(action.class_name, fs, ms),
            class_name=action.class_name, fields=fs, methods=ms,
        )


class GetSourceClassInfoTool(ToolDefinition):
    description: str = "获取源文件中指定类的字段和方法签名"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=GetSourceClassInfoAction,
            observation_type=GetSourceClassInfoObservation,
            executor=GetSourceClassInfoExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("get_source_class_info", GetSourceClassInfoTool)


# ═══════════════════════════════════════════════════════════════
#  get_target_class_info  — 从目标文件提取类的字段和方法
# ═══════════════════════════════════════════════════════════════
#  与 source 版本的区别：优先定位类代码块；目标文件语法不完整时降级扫描整个文件。


class GetTargetClassInfoAction(Action):
    filepath: str = Field(description="文件路径")
    class_name: str = Field(description="类名")


class GetTargetClassInfoObservation(Observation):
    class_name: str = Field(default="")
    fields: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)


class GetTargetClassInfoExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        try:
            fp = _resolve(action.filepath, self.root)
        except ValueError as e:
            return _path_error_observation(GetTargetClassInfoObservation, e)
        if not fp.exists():
            return GetTargetClassInfoObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        content = _read_text(fp)
        block = _extract_class_block(content, action.class_name)
        if not block:
            related_pattern = rf"\b{re.escape(action.class_name)}\s*::|class\s+{re.escape(action.class_name)}\b"
            if not re.search(related_pattern, content):
                return GetTargetClassInfoObservation.from_text(
                    text=f"未找到类: {action.class_name}", is_error=True,
                )
            block = content
            prefix = f"未找到完整类声明，已在整个文件中扫描 {action.class_name} 相关实现。\n"
        else:
            prefix = ""
        fs = _fields(block)
        ms = _methods(block)
        return GetTargetClassInfoObservation.from_text(
            text=prefix + _class_info_to_text(action.class_name, fs, ms),
            class_name=action.class_name, fields=fs, methods=ms,
        )


class GetTargetClassInfoTool(ToolDefinition):
    description: str = "获取目标文件中指定类的字段和方法签名"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=GetTargetClassInfoAction,
            observation_type=GetTargetClassInfoObservation,
            executor=GetTargetClassInfoExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("get_target_class_info", GetTargetClassInfoTool)


# ═══════════════════════════════════════════════════════════════
#  find_target_imports  — 获取文件的 import / #include 语句
# ═══════════════════════════════════════════════════════════════


class FindTargetImportsAction(Action):
    filepath: str = Field(description="文件路径")


class FindTargetImportsObservation(Observation):
    imports: list[str] = Field(default_factory=list)


class FindTargetImportsExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        try:
            fp = _resolve(action.filepath, self.root)
        except ValueError as e:
            return _path_error_observation(FindTargetImportsObservation, e)
        if not fp.exists():
            return FindTargetImportsObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        imports: list[str] = []
        for s in _read_text(fp).splitlines():
            stripped = s.strip()
            if re.match(r"#\s*include\b", stripped) or stripped.startswith(("import ", "from ")):
                if stripped not in imports:
                    imports.append(stripped)
        text = "\n".join(imports[:20]) or "（无 import）"
        return FindTargetImportsObservation.from_text(
            text=text, imports=imports[:20],
        )


class FindTargetImportsTool(ToolDefinition):
    description: str = "获取文件的 #include 或 import 语句"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=FindTargetImportsAction,
            observation_type=FindTargetImportsObservation,
            executor=FindTargetImportsExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("find_target_imports", FindTargetImportsTool)


# ═══════════════════════════════════════════════════════════════
#  find_target_class  — 在工作目录中搜索类定义
# ═══════════════════════════════════════════════════════════════


class FindTargetClassAction(Action):
    class_name: str = Field(description="要搜索的类名")


class FindTargetClassObservation(Observation):
    class_name: str = Field(default="")
    filepath: str | None = Field(default=None)


class FindTargetClassExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        pattern = rf"(?:class|struct)\s+{re.escape(action.class_name)}\b"
        filepath, _, line = _search_pattern_in_files(self.root, pattern)
        if filepath:
            detail = f"\n{line}" if line else ""
            return FindTargetClassObservation.from_text(
                text=f"在 {filepath} 中找到类 {action.class_name}{detail}",
                class_name=action.class_name, filepath=filepath,
            )
        return FindTargetClassObservation.from_text(
            text=f"未找到类: {action.class_name}", is_error=True,
        )


class FindTargetClassTool(ToolDefinition):
    description: str = "在工作目录中搜索指定类名"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=FindTargetClassAction,
            observation_type=FindTargetClassObservation,
            executor=FindTargetClassExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("find_target_class", FindTargetClassTool)


# ═══════════════════════════════════════════════════════════════
#  find_target_method  — 在工作目录中搜索方法定义
# ═══════════════════════════════════════════════════════════════


class FindTargetMethodAction(Action):
    method_name: str = Field(description="要搜索的方法名")


class FindTargetMethodObservation(Observation):
    method_name: str = Field(default="")
    filepath: str | None = Field(default=None)


class FindTargetMethodExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        pattern = rf"\b(?:\w+::)*{re.escape(action.method_name)}\s*\("
        filepath, _, line = _search_pattern_in_files(self.root, pattern)
        if filepath:
            detail = f"\n{line}" if line else ""
            return FindTargetMethodObservation.from_text(
                text=f"在 {filepath} 中找到方法 {action.method_name}{detail}",
                method_name=action.method_name, filepath=filepath,
            )
        return FindTargetMethodObservation.from_text(
            text=f"未找到方法: {action.method_name}", is_error=True,
        )


class FindTargetMethodTool(ToolDefinition):
    description: str = "在工作目录中搜索指定方法名"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=FindTargetMethodAction,
            observation_type=FindTargetMethodObservation,
            executor=FindTargetMethodExecutor(
                workspace_root=kwargs.get("workspace_root", "."),
            ),
        )]


register_tool("find_target_method", FindTargetMethodTool)
