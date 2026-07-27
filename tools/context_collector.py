"""多步上下文收集工具：类信息提取、文件搜索、依赖查询。"""

from __future__ import annotations

import re
from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


# ═══════════════════════════════════════════════════════════════
#  共享辅助函数
# ═══════════════════════════════════════════════════════════════


def _resolve(fp: str, root: Path) -> Path:
    """将文件路径解析为绝对路径。"""
    p = Path(fp)
    return p if p.is_absolute() else root / p


def _fields(content: str) -> list[str]:
    """从代码块中提取字段名。"""
    return re.findall(r"\w[\w<>, ]*\s+(\w+)\s*[;=]", content, re.MULTILINE)[:15]


def _methods(content: str) -> list[str]:
    """从代码块中提取方法名（去重保序）。"""
    ms = re.findall(r"(\w+)\s*\([^)]*\)\s*(?:const)?\s*(?:{|;)", content, re.MULTILINE)
    return list(dict.fromkeys(ms))[:15]


def _class_info_to_text(class_name: str, fields: list[str], methods: list[str]) -> str:
    """将类信息格式化为可读文本。"""
    parts = [f"类 {class_name}:"]
    if fields:
        parts.append("  字段:\n" + "\n".join(f"    - {f}" for f in fields))
    if methods:
        parts.append("  方法:\n" + "\n".join(f"    - {m}" for m in methods))
    return "\n".join(parts)


def _extract_class_block(content: str, class_name: str) -> str | None:
    """从代码中提取指定类的代码块（含继承/模板等上下文）。

    返回类的代码块文本，如果未找到则返回 None。
    """
    pattern = rf"(class\s+{re.escape(class_name)}[\s\S]*?)(?:^class\s|\Z)"
    m = re.search(pattern, content, re.MULTILINE)
    return m.group(1).strip() if m else None


def _search_pattern_in_files(
    root: Path,
    pattern: str,
) -> tuple[str | None, str | None]:
    """在工作目录中搜索匹配指定模式的文件。

    返回 (filepath, matched_text)，未找到时返回 (None, None)。
    """
    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix not in (".py", ".java", ".cpp", ".h", ".hpp"):
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
            if re.search(pattern, content):
                rel = str(fp.relative_to(root))
                return rel, content
        except Exception:
            continue
    return None, None


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
        fp = _resolve(action.filepath, self.root)
        if not fp.exists():
            return GetSourceClassInfoObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        content = fp.read_text(encoding="utf-8")
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
#  与 source 版本的区别：不做类代码块定位，在整个文件中搜索字段/方法。
#  这是因为目标文件可能尚未完成、语法不完整，class 关键字未必出现。


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
        fp = _resolve(action.filepath, self.root)
        if not fp.exists():
            return GetTargetClassInfoObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        content = fp.read_text(encoding="utf-8")
        if f"class {action.class_name}" not in content:
            return GetTargetClassInfoObservation.from_text(
                text=f"未找到类: {action.class_name}", is_error=True,
            )
        fs = _fields(content)
        ms = _methods(content)
        return GetTargetClassInfoObservation.from_text(
            text=_class_info_to_text(action.class_name, fs, ms),
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
        fp = _resolve(action.filepath, self.root)
        if not fp.exists():
            return FindTargetImportsObservation.from_text(
                text=f"文件不存在: {action.filepath}", is_error=True,
            )
        imports = [
            s for s in fp.read_text(encoding="utf-8").split("\n")
            if s.strip().startswith(("#include", "import ", "from "))
        ]
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
        pattern = rf"class\s+{re.escape(action.class_name)}\b"
        filepath, _ = _search_pattern_in_files(self.root, pattern)
        if filepath:
            return FindTargetClassObservation.from_text(
                text=f"在 {filepath} 中找到类 {action.class_name}",
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
        pattern = rf"\b{re.escape(action.method_name)}\s*\("
        filepath, _ = _search_pattern_in_files(self.root, pattern)
        if filepath:
            return FindTargetMethodObservation.from_text(
                text=f"在 {filepath} 中找到方法 {action.method_name}",
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
