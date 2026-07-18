"""多步上下文收集工具。"""

import re
from pathlib import Path
from typing import Optional
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


def _resolve(fp: str, root: Path) -> Path:
    p = Path(fp)
    return p if p.is_absolute() else root / p


def _fields(content: str) -> list[str]:
    return re.findall(r"\w[\w<>, ]*\s+(\w+)\s*[;=]", content, re.MULTILINE)[:15]


def _methods(content: str) -> list[str]:
    ms = re.findall(r"(\w+)\s*\([^)]*\)\s*(?:const)?\s*(?:{|;)", content, re.MULTILINE)
    return list(dict.fromkeys(ms))[:15]


# ── get_source_class_info ──

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
            return GetSourceClassInfoObservation.from_text(text=f"文件不存在: {action.filepath}", is_error=True)
        content = fp.read_text(encoding="utf-8")
        pattern = rf"(class\s+{re.escape(action.class_name)}[\s\S]*?)(?:^class\s|\Z)"
        m = re.search(pattern, content, re.MULTILINE)
        if not m:
            return GetSourceClassInfoObservation.from_text(text=f"未找到类: {action.class_name}", is_error=True)
        block = m.group(1).strip()
        fs = _fields(block)
        ms = _methods(block)
        text = f"类 {action.class_name}:\n" + ("  字段:\n" + "\n".join(f"    - {f}" for f in fs) if fs else "") + \
               ("\n  方法:\n" + "\n".join(f"    - {m}" for m in ms) if ms else "")
        return GetSourceClassInfoObservation.from_text(text=text, class_name=action.class_name, fields=fs, methods=ms)


class GetSourceClassInfoTool(ToolDefinition):
    description: str = "获取源文件中指定类的字段和方法签名"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=GetSourceClassInfoAction, observation_type=GetSourceClassInfoObservation,
                     executor=GetSourceClassInfoExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("get_source_class_info", GetSourceClassInfoTool)


# ── get_target_class_info ──

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
            return GetTargetClassInfoObservation.from_text(text=f"文件不存在: {action.filepath}", is_error=True)
        content = fp.read_text(encoding="utf-8")
        if f"class {action.class_name}" not in content:
            return GetTargetClassInfoObservation.from_text(text=f"未找到类: {action.class_name}", is_error=True)
        fs = _fields(content)
        ms = _methods(content)
        text = f"类 {action.class_name}:\n" + ("  字段:\n" + "\n".join(f"    - {f}" for f in fs) if fs else "") + \
               ("\n  方法:\n" + "\n".join(f"    - {m}" for m in ms) if ms else "")
        return GetTargetClassInfoObservation.from_text(text=text, class_name=action.class_name, fields=fs, methods=ms)


class GetTargetClassInfoTool(ToolDefinition):
    description: str = "获取目标文件中指定类的字段和方法签名"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=GetTargetClassInfoAction, observation_type=GetTargetClassInfoObservation,
                     executor=GetTargetClassInfoExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("get_target_class_info", GetTargetClassInfoTool)


# ── find_target_imports ──

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
            return FindTargetImportsObservation.from_text(text=f"文件不存在: {action.filepath}", is_error=True)
        imports = [s for s in fp.read_text(encoding="utf-8").split("\n")
                   if s.strip().startswith(("#include", "import ", "from "))]
        text = "\n".join(imports[:20]) or "（无 import）"
        return FindTargetImportsObservation.from_text(text=text, imports=imports[:20])


class FindTargetImportsTool(ToolDefinition):
    description: str = "获取文件的 #include 或 import 语句"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=FindTargetImportsAction, observation_type=FindTargetImportsObservation,
                     executor=FindTargetImportsExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("find_target_imports", FindTargetImportsTool)


# ── find_target_class ──

class FindTargetClassAction(Action):
    class_name: str = Field(description="要搜索的类名")


class FindTargetClassObservation(Observation):
    class_name: str = Field(default="")
    filepath: Optional[str] = Field(default=None)


class FindTargetClassExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        for fp in self.root.rglob("*"):
            if not fp.is_file() or fp.suffix not in (".py", ".java", ".cpp", ".h", ".hpp"):
                continue
            try:
                if re.search(rf"class\s+{re.escape(action.class_name)}", fp.read_text(encoding="utf-8", errors="ignore")):
                    rel = str(fp.relative_to(self.root))
                    return FindTargetClassObservation.from_text(text=f"在 {rel} 中找到类 {action.class_name}", class_name=action.class_name, filepath=rel)
            except Exception:
                continue
        return FindTargetClassObservation.from_text(text=f"未找到类: {action.class_name}", is_error=True)


class FindTargetClassTool(ToolDefinition):
    description: str = "在工作目录中搜索指定类名"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=FindTargetClassAction, observation_type=FindTargetClassObservation,
                     executor=FindTargetClassExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("find_target_class", FindTargetClassTool)


# ── find_target_method ──

class FindTargetMethodAction(Action):
    method_name: str = Field(description="要搜索的方法名")


class FindTargetMethodObservation(Observation):
    method_name: str = Field(default="")
    filepath: Optional[str] = Field(default=None)


class FindTargetMethodExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        for fp in self.root.rglob("*"):
            if not fp.is_file() or fp.suffix not in (".py", ".java", ".cpp", ".h", ".hpp"):
                continue
            try:
                if re.search(rf"{re.escape(action.method_name)}\s*\(", fp.read_text(encoding="utf-8", errors="ignore")):
                    rel = str(fp.relative_to(self.root))
                    return FindTargetMethodObservation.from_text(text=f"在 {rel} 中找到方法 {action.method_name}", method_name=action.method_name, filepath=rel)
            except Exception:
                continue
        return FindTargetMethodObservation.from_text(text=f"未找到方法: {action.method_name}", is_error=True)


class FindTargetMethodTool(ToolDefinition):
    description: str = "在工作目录中搜索指定方法名"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(action_type=FindTargetMethodAction, observation_type=FindTargetMethodObservation,
                     executor=FindTargetMethodExecutor(workspace_root=kwargs.get("workspace_root", ".")))]


register_tool("find_target_method", FindTargetMethodTool)
