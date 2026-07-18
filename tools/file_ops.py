"""文件读写工具：read_file, create_file"""

from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from openhands.sdk.llm import TextContent
from pydantic import Field

# ── 依赖层访问控制（模块级，避免存入 Pydantic 模型导致序列化失败）──
_LAYER_CTRL = None


def set_layer_ctrl(ctrl):
    global _LAYER_CTRL
    _LAYER_CTRL = ctrl


def _get_layer_ctrl():
    return _LAYER_CTRL


class ReadFileAction(Action):
    filepath: str = Field(description="要读取的文件路径")


class ReadFileObservation(Observation):
    result: str = Field(default="")
    filepath: str = Field(default="")


class ReadFileExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = "."):
        self.root = Path(workspace_root).resolve()

    def __call__(self, action, conversation=None):
        p = Path(action.filepath)
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()

        # 依赖层访问控制（从模块级变量读取，避免序列化问题）
        ctrl = _get_layer_ctrl()
        if ctrl and ctrl.active:
            try:
                rel = str(p.relative_to(self.root).as_posix())
            except ValueError:
                rel = action.filepath
            if not ctrl.is_unlocked(rel):
                return ReadFileObservation.from_text(
                    text=f"⛕ '{rel}' 属于更高依赖层，当前不可读。"
                         f"完成当前层所有文件并通过测试后将自动解锁。",
                    is_error=True, result="", filepath=action.filepath)

        if not p.exists():
            return ReadFileObservation.from_text(text=f"文件不存在: {action.filepath}", is_error=True, result="", filepath=action.filepath)
        try:
            content = p.read_text(encoding="utf-8")
            return ReadFileObservation.from_text(text=f"📄 {action.filepath}\n\n{content[:5000]}", result=content, filepath=action.filepath)
        except Exception as e:
            return ReadFileObservation.from_text(text=f"读取失败: {e}", is_error=True, result="", filepath=action.filepath)


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
        p = Path(action.filepath)
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()

        # 依赖层访问控制（与 read_file 一致）
        ctrl = _get_layer_ctrl()
        if ctrl and ctrl.active:
            try:
                rel = str(p.relative_to(self.root).as_posix())
            except ValueError:
                rel = action.filepath
            if not ctrl.is_unlocked(rel):
                return CreateFileObservation.from_text(
                    text=f"⛕ '{rel}' 属于更高依赖层，当前不可创建。"
                         f"完成当前层所有文件并通过测试后将自动解锁。",
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
