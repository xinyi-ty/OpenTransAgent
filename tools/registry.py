"""工具注册。导入此模块会触发所有工具文件的 register_tool() 调用。"""

# 导入所有工具文件（每个文件在模块级别调用 register_tool）
import tools.file_ops  # noqa: F401
import tools.shell  # noqa: F401
import tools.search  # noqa: F401
import tools.context_collector  # noqa: F401
import tools.reflect  # noqa: F401

from openhands.sdk import Tool


def create_tools(**kwargs) -> list[Tool]:
    TOOL_NAMES = [
        "read_file", "create_file", "execute_command", "search_content",
        "get_source_class_info", "get_target_class_info",
        "find_target_imports", "find_target_class", "find_target_method",
        "reflect",
    ]
    return [Tool(name=n, params=kwargs) for n in TOOL_NAMES]
