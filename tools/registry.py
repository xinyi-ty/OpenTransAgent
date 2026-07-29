"""工具注册与定义中心。

导入此模块会触发所有工具文件的 register_tool() 调用。
TOOL_DEFINITIONS 是 name → LLM 描述的中心映射，registry 和 prompts 共用。
"""

from __future__ import annotations

import re

import tools.file_ops  # noqa: F401 — 触发工具注册
import tools.shell  # noqa: F401
import tools.search  # noqa: F401
import tools.context_collector  # noqa: F401
import tools.reflect  # noqa: F401

from openhands.sdk import Tool

__all__ = [
    "TOOL_DEFINITIONS", "BUILTIN_TOOL_DEFINITIONS", "create_tools",
    "validate_tool_definitions",
]

# ── 工具定义中心 ──────────────────────────────────────────────
# name → LLM 描述；prompts.py 自动从此生成 AVAILABLE TOOLS 段落。
# 添加/删除工具时：①更新此字典；②在文件顶部 import 触发注册。

TOOL_DEFINITIONS: dict[str, str] = {
    "read_file": "Read a workspace file with safe path checks",
    "create_file": "Create a new/empty file or intentionally replace its entire contents atomically",
    "edit_file": "Make a targeted exact replacement in an existing file; old_string must be non-empty",
    "list_files": "List files and subdirectories in a workspace directory (non-recursive)",
    "execute_command": "Run shell commands (build, test, etc.)",
    "search_content": "Search keywords in project files",
    "get_source_class_info": "Get class fields/methods from a source file",
    "get_target_class_info": "Get class fields/methods from a target file",
    "find_target_imports": "Get #include/import statements from a file",
    "find_target_class": "Search for a class definition across the workspace",
    "find_target_method": "Search for a method signature across the workspace",
    "reflect": "Analyze compile/test failure root cause and suggest next fixes",
}

# SDK 内置工具（始终可用），prompts.py 用于生成工具列表；create_tools() 不创建它们。
BUILTIN_TOOL_DEFINITIONS: dict[str, str] = {
    "finish": "Mark translation task complete",
    "think": "Internal reasoning",
}

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_tool_definitions() -> None:
    """校验本地工具定义，避免 prompt 展示与工具创建配置出现明显错误。"""
    duplicate_builtins = set(TOOL_DEFINITIONS) & set(BUILTIN_TOOL_DEFINITIONS)
    if duplicate_builtins:
        names = ", ".join(sorted(duplicate_builtins))
        raise ValueError(f"工具名同时出现在自定义和内置定义中: {names}")

    for mapping_name, mapping in (
        ("TOOL_DEFINITIONS", TOOL_DEFINITIONS),
        ("BUILTIN_TOOL_DEFINITIONS", BUILTIN_TOOL_DEFINITIONS),
    ):
        for name, desc in mapping.items():
            if not name or not _TOOL_NAME_RE.match(name):
                raise ValueError(f"{mapping_name} 中存在非法工具名: {name!r}")
            if not desc or not desc.strip():
                raise ValueError(f"{mapping_name}.{name} 缺少工具描述")


def create_tools(
    exclude: set[str] | frozenset[str] | None = None,
    **kwargs,
) -> list[Tool]:
    """创建 Tool 实例列表供 Agent 使用。

    Tool 的 name 来自 TOOL_DEFINITIONS，params 透传公共参数
    （如 workspace_root）给各 ToolDefinition.create()。exclude 可用于按配置关闭工具。
    """
    validate_tool_definitions()
    excluded = exclude or set()
    return [Tool(name=n, params=kwargs) for n in TOOL_DEFINITIONS if n not in excluded]
