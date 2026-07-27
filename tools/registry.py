"""工具注册与定义中心。

导入此模块会触发所有工具文件的 register_tool() 调用。
TOOL_DEFINITIONS 是 name → LLM 描述的中心映射，registry 和 prompts 共用。
"""

import tools.file_ops  # noqa: F401 — 触发工具注册
import tools.shell  # noqa: F401
import tools.search  # noqa: F401
import tools.context_collector  # noqa: F401
import tools.reflect  # noqa: F401

from openhands.sdk import Tool

__all__ = [
    "TOOL_DEFINITIONS", "BUILTIN_TOOL_DEFINITIONS", "create_tools",
]

# ── 工具定义中心 ──────────────────────────────────────────────
# name → LLM 描述；prompts.py 自动从此生成 AVAILABLE TOOLS 段落。
# 添加/删除工具时：①更新此字典；②在文件顶部 import 触发注册。

TOOL_DEFINITIONS: dict[str, str] = {
    "read_file": "Read file contents",
    "create_file": "Create/overwrite a translated file",
    "list_files": "List files and subdirectories in a workspace directory (non-recursive)",
    "execute_command": "Run shell commands (build, test, etc.)",
    "search_content": "Search keywords in project files",
    "get_source_class_info": "Get class fields/methods from a source file",
    "get_target_class_info": "Get class fields/methods from a target file",
    "find_target_imports": "Get #include/import statements from a file",
    "find_target_class": "Search for a class definition across the workspace",
    "find_target_method": "Search for a method signature across the workspace",
    "reflect": "Analyze translation failure root cause before fixing (does NOT modify files)",
}

# SDK 内置工具（始终可用），prompts.py 用于生成工具列表
BUILTIN_TOOL_DEFINITIONS: dict[str, str] = {
    "finish": "Mark translation task complete",
    "think": "Internal reasoning",
}


def create_tools(**kwargs) -> list[Tool]:
    """创建 Tool 实例列表供 Agent 使用。

    Tool 的 name 来自 TOOL_DEFINITIONS，params 透传公共参数
    （如 workspace_root）给各 ToolDefinition.create()。
    """
    return [Tool(name=n, params=kwargs) for n in TOOL_DEFINITIONS]
