"""System Prompt 构造。

所有静态提示段提取为模块级常量，工具列表从 tools.registry 动态生成，
避免与 registry.py 的 TOOL_NAMES 重复维护。
"""

from __future__ import annotations

from config.router import get_route
from tools.registry import TOOL_DEFINITIONS, BUILTIN_TOOL_DEFINITIONS

# ═══════════════════════════════════════════════════════════════
#  静态提示段落
# ═══════════════════════════════════════════════════════════════

ROLE_DESCRIPTION = """\
You are a repository-level code translation expert.
Your task is to translate a {source_language} project to {target_language}.
Project name: {project_name}

You operate in a ReAct (Reasoning + Acting) loop:
1. Analyze the project structure and code
2. Create translated implementations
3. Run tests to verify correctness
4. Fix issues based on test feedback
5. Mark complete when all tests pass"""

IMPORTANT_NOTICE = """\
IMPORTANT: You have exactly the tools listed below. Do NOT call tools that do not exist (e.g. "edit", "sed", "patch"). To modify a file, use create_file to overwrite it.

AVAILABLE TOOLS:"""

GUIDELINES = """\
TRANSLATION GUIDELINES:
1. Read source files FIRST, then immediately create target files. Do NOT spend steps just exploring.
2. Start by reading a source file and creating its translation. Repeat for each file.
3. {pair_instruction}
4. After creating files, update any build configuration files (e.g. CMakeLists.txt) to reference them
5. Run tests only after creating the translated code

WHEN TESTS FAIL (Reflection-based Error Correction):
1. First call reflect(source, code, error_message) to analyze root cause
2. Use get_source_class_info / find_target_method etc. to gather needed context
3. Only then call create_file to produce the fixed version"""


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _build_tool_list() -> str:
    """从 TOOL_DEFINITIONS 动态生成工具列表行（## name — desc）。"""
    lines: list[str] = []
    for name, desc in {**TOOL_DEFINITIONS, **BUILTIN_TOOL_DEFINITIONS}.items():
        lines.append(f"## {name} — {desc}")
    return "\n".join(lines)


def _parse_project_files(project_tree: str) -> list[str]:
    """将 project_tree 文本按行解析为文件路径列表。"""
    return [f for f in project_tree.strip().split("\n") if f.strip()]


# ═══════════════════════════════════════════════════════════════
#  Prompt 构造入口
# ═══════════════════════════════════════════════════════════════

def build_react_prompt(
    source_language: str,
    target_language: str,
    project_name: str,
    project_tree: str | None = None,
    translation_order: list[str] | None = None,
    layers: list[list[str]] | None = None,
    current_layer: int = 0,
) -> str:
    """构建完整的 ReAct 翻译系统提示。

    段落顺序：
      1. 角色描述与 ReAct 循环说明
      2. 环境限制 + 可用工具列表
      3. 翻译指南与反思纠错流程
      4. 项目文件树（可选）
      5. 待生成的目标文件列表（可选）
      6. 依赖层 / 翻译顺序指引（可选）
    """
    route = get_route(source_language, target_language)
    sections: list[str] = []

    # ── 1. 角色与任务 ──────────────────────────────────────────
    sections.append(
        ROLE_DESCRIPTION.format(
            source_language=source_language,
            target_language=target_language,
            project_name=project_name,
        )
    )

    # ── 2. 环境限制 + 工具列表 ─────────────────────────────────
    env_restriction = route.prompt_env_restriction if route else ""
    tool_list = _build_tool_list()
    sections.append(f"{env_restriction}\n\n{IMPORTANT_NOTICE}\n\n{tool_list}")

    # ── 3. 翻译指南 ────────────────────────────────────────────
    pair_instruction = (
        route.prompt_pair_instruction
        if route
        else f"For each source file, create the equivalent {target_language} file"
    )
    sections.append(GUIDELINES.format(pair_instruction=pair_instruction))

    # ── 4. 项目文件树 ──────────────────────────────────────────
    if project_tree:
        sections.append(f"PROJECT FILES:\n{project_tree}")

    # ── 5. 待生成的目标文件 ────────────────────────────────────
    # 优先用 translation_order（仅源文件），避免基础设施文件混入
    source_files = translation_order or (
        _parse_project_files(project_tree) if project_tree else None
    )
    if source_files:
        target_files = (
            [route.file_extension_map(f) for f in source_files]
            if route and route.file_extension_map
            else source_files
        )
        sections.append(
            "FILES TO CREATE:\n" + "\n".join(f"  - {f}" for f in target_files)
        )

    # ── 6. 依赖层 / 翻译顺序 ───────────────────────────────────
    if layers:
        total = len(layers)
        layer_lines = "\n".join(
            f"  {'→ ' if i == current_layer else '  '}"
            f"Layer {i}: {', '.join(layer)}"
            for i, layer in enumerate(layers)
        )
        sections.append(
            f"DEPENDENCY LAYERS ({total} layers):\n"
            f"{layer_lines}\n\n"
            f"You are currently on Layer {current_layer}. Files in higher layers "
            f"cannot be read yet. Finish the current layer and tests will unlock "
            f"the next layer automatically."
        )
    elif translation_order:
        order_lines = "\n".join(
            f"  {i+1}. {f}" for i, f in enumerate(translation_order)
        )
        sections.append(
            f"SUGGESTED TRANSLATION ORDER (dependency-first):\n"
            f"{order_lines}\n\n"
            f"Files with no dependencies are listed first. Following this order\n"
            f"helps avoid missing-dependency errors. Start from the top."
        )

    return "\n\n".join(sections)
