"""内容搜索工具：search_content"""

from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


class SearchContentAction(Action):
    keyword: str = Field(description="搜索关键词")
    path: str = Field(default=".")


class SearchContentObservation(Observation):
    matches: list[str] = Field(default_factory=list)
    keyword: str = Field(default="")
    count: int = Field(default=0)


class SearchContentExecutor(ToolExecutor):
    def __init__(self, workspace_root: str = ".", max_results: int = 10, extensions: tuple = None):
        self.root = Path(workspace_root).resolve()
        self.max_results = max_results
        self.extensions = extensions or (".py", ".java", ".cpp", ".h", ".cs", ".rs", ".go", ".js", ".ts")

    def __call__(self, action, conversation=None):
        root = self.root
        if action.path and action.path != ".":
            p = Path(action.path)
            root = p if p.is_absolute() else self.root / p
        matches = []
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in self.extensions:
                continue
            try:
                if action.keyword.lower() in f.read_text(encoding="utf-8", errors="ignore"):
                    rel = str(f.relative_to(self.root))
                    matches.append(rel)
                    if len(matches) >= self.max_results:
                        break
            except Exception:
                continue
        text = f"找到 {len(matches)} 个匹配:\n" + "\n".join(matches) if matches else "未找到匹配"
        return SearchContentObservation.from_text(text=text, matches=matches, keyword=action.keyword, count=len(matches))


class SearchContentTool(ToolDefinition):
    description: str = "搜索文件内容"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        ws = kwargs.get("workspace_root", ".")
        mr = kwargs.get("search_max_results", 10)
        return [cls(action_type=SearchContentAction, observation_type=SearchContentObservation,
                     executor=SearchContentExecutor(workspace_root=ws, max_results=mr))]


register_tool("search_content", SearchContentTool)
