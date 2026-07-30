"""System Prompt 构造。

所有静态提示段提取为模块级常量，工具列表从 tools.registry 动态生成，
避免与 registry.py 的 TOOL_NAMES 重复维护。
"""

from __future__ import annotations

import re

from config.languages import normalize_language
from config.router import get_effective_route
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
IMPORTANT: You have exactly the tools listed below. Do NOT call tools that do not exist (e.g. "edit", "sed", "patch"). Use create_file for a new/empty file or an intentional full replacement. Use edit_file only for a targeted change in an existing file: old_string MUST be non-empty, match exact existing text, and be unique unless replace_all=true.

AVAILABLE TOOLS:"""

GUIDELINES = """\
TRANSLATION GUIDELINES:
1. Read source files FIRST, then immediately create target files. Do NOT spend steps just exploring.
2. Start by reading a source file and creating its translation. Repeat for each file.
3. {pair_instruction}
4. Prefer writing each target file once with a complete implementation. Avoid repeatedly overwriting the same file with tiny changes.
5. Use edit_file for small, precise fixes to existing files; use create_file for new files or full rewrites.
6. Run focused checks when useful, but do not rerun the same full test command unless relevant files changed. Once all expected files exist and tests pass, call finish immediately — do NOT spend extra steps verifying, listing, or re-running.
7. execute_command already runs inside the translation workspace. Do NOT cd into guessed external project paths; use workspace-relative commands.
8. Do not call think for layers with 5 or fewer source files unless a failure is ambiguous. If you know which file to inspect, call read_file directly; never respond with only a natural-language plan.{target_guidelines}{route_guidelines}{reflection_guidelines}"""

PYTHON_TARGET_GUIDELINES = """

PYTHON TARGET GUIDELINES:
1. Generate correct import paths from the start.
2. Prefer package-relative imports (e.g. `from .. import enum`) for files in subdirectories.
3. Avoid fragile patterns like `sys.path.insert(0, '..')`; if sys.path is unavoidable,
   use `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` instead.
4. Avoid Python standard-library module name conflicts (for example enum.py, typing.py,
   json.py, dataclasses.py, collections.py). If an expected target file has such a name,
   keep that expected file present for completeness, but put the real implementation in a
   non-conflicting module name and make the expected file a small compatibility shim."""

CPP_TARGET_GUIDELINES = """

CPP TARGET GUIDELINES:
1. Do not run build/tests until every expected target file for the current layer exists.
2. While fixing compile errors, run build only (`cmake --build build --config Release`); run ctest only after build succeeds.
3. Do not repeatedly run configure/build/ctest inside the agent loop. After all required target files exist, prefer calling finish so the runtime can run cumulative regression. Only rerun build after changing C++ source/header files.
4. Read only the tests needed for the current failure. For large layers, read at most 3 representative test files before generating target files; read more tests only after a concrete failing test points to them.
5. execute_command runs in the workspace already. Do NOT use `cd /d` into guessed external directories such as E:/Agent_Projects or the dataset source path.
6. Treat generated build/test infrastructure as read-only. Do NOT create_file, edit_file, or rewrite CMakeLists.txt, run_tests.sh, public_tests/*, tests/*, or test/*; translate source files instead."""

REFLECTION_GUIDELINES = """

WHEN TESTS FAIL (Reflection-based Error Correction):
1. First call reflect(source_function, translated_code, error_message, test_results) to analyze root cause
2. Use get_source_class_info / find_target_method etc. to gather needed context
3. Only then call edit_file for a precise fix or create_file for a full rewritten version"""

_TREE_PREFIX_RE = re.compile(r"^[│| ]*(?:[├└]\s*──\s*)?")
_TOOL_LIST_CACHE_KEY: tuple[tuple[str, str], ...] | None = None
_TOOL_LIST_CACHE_VALUE = ""


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _tool_list_fingerprint() -> tuple[tuple[str, str], ...]:
    """返回工具定义内容指纹，保持 registry 动态更新兼容性。"""
    return (
        tuple(TOOL_DEFINITIONS.items())
        + tuple(BUILTIN_TOOL_DEFINITIONS.items())
    )


def _build_tool_list() -> str:
    """从 TOOL_DEFINITIONS 动态生成工具列表行（## name — desc）。"""
    global _TOOL_LIST_CACHE_KEY, _TOOL_LIST_CACHE_VALUE

    fingerprint = _tool_list_fingerprint()
    if fingerprint == _TOOL_LIST_CACHE_KEY:
        return _TOOL_LIST_CACHE_VALUE

    lines = [f"## {name} — {desc}" for name, desc in fingerprint]
    _TOOL_LIST_CACHE_KEY = fingerprint
    _TOOL_LIST_CACHE_VALUE = "\n".join(lines)
    return _TOOL_LIST_CACHE_VALUE


def _parse_project_files(project_tree: str) -> list[str]:
    """将 project_tree 文本解析为尽量干净的文件路径列表。

    project_tree 可能来自 tree 命令，也可能来自 fallback 的逐行路径列表。
    这里保留旧的 fallback 能力，但过滤明显的展示性行，避免 FILES TO CREATE
    混入树形符号、目录统计或省略号。
    """
    files: list[str] = []
    for raw_line in project_tree.splitlines():
        line = raw_line.strip()
        if not line or line == "...":
            continue
        if re.match(r"^\d+ director", line) or re.match(r"^\d+ file", line):
            continue

        cleaned = _TREE_PREFIX_RE.sub("", line).strip()
        if not cleaned or cleaned == "...":
            continue
        if cleaned.endswith(":"):
            continue

        # tree 输出中的目录通常没有后缀；fallback walk 输出通常包含路径分隔符。
        # 这里宁可少推断，也避免把展示用目录行误当成待创建文件。
        if "." not in cleaned and "/" not in cleaned and "\\" not in cleaned:
            continue
        files.append(cleaned.replace("\\", "/"))
    return files


def _select_source_files_for_targets(
    source_files: list[str] | None,
    translation_order: list[str] | None,
    project_tree: str | None,
) -> list[str] | None:
    """选择用于生成 FILES TO CREATE 的源文件列表。"""
    if source_files:
        return source_files
    if translation_order:
        return translation_order
    if project_tree:
        return _parse_project_files(project_tree)
    return None


def _build_files_to_create_section(source_files: list[str], route) -> str:
    """构建待创建文件段落。"""
    target_files = (
        [route.file_extension_map(f) for f in source_files]
        if route and route.file_extension_map
        else source_files
    )
    return "FILES TO CREATE:\n" + "\n".join(f"  - {f}" for f in target_files)


def _build_target_guidelines(target_language: str) -> str:
    """按目标语言追加专属提示，避免 Python/C++ 规则互相污染。"""
    normalized = normalize_language(target_language)
    if normalized == "python":
        return PYTHON_TARGET_GUIDELINES
    if normalized == "cpp":
        return CPP_TARGET_GUIDELINES
    return ""


def _build_route_guidelines(route) -> str:
    """提取语言对专属翻译流程引导，仅出现在显式路由中。"""
    if route and route.prompt_route_guidance:
        return "\n\n" + route.prompt_route_guidance
    return ""


def _build_small_project_fast_path_section(source_files: list[str]) -> str:
    """小项目快速路径提示，减少低价值探索和重复验证。"""
    _ = source_files
    return (
        "SMALL PROJECT FAST PATH:\n"
        "This layer has 5 or fewer source files. Keep the loop tight:\n"
        "  - Read each visible test and each source file at most once unless an error requires rereading.\n"
        "  - Create each expected target file once; prefer edit_file for follow-up fixes.\n"
        "  - Do not inspect or rewrite build/test infrastructure.\n"
        "  - Run at most one configure/build/test cycle after all expected target files exist; rerun only after code changes.\n"
        "  - After tests pass, call finish immediately."
    )


def _build_large_project_batch_section(source_files: list[str]) -> str:
    """大层批处理提示，避免一次性读写过多文件导致上下文和耗时膨胀。"""
    _ = source_files
    return (
        "LARGE LAYER BATCHING:\n"
        "This layer has more than 5 source files. Keep each ReAct step small and productive:\n"
        "  - Do NOT bulk-read every source file before writing. Read at most 4 source files, then create or edit their target files.\n"
        "  - Do NOT issue more than 5 read_file/create_file/edit_file calls in one response.\n"
        "  - Translate in batches, but still create every file listed in FILES TO CREATE before calling finish.\n"
        "  - Read at most 3 representative test files before generating target files; read more only after a concrete failure points to them.\n"
        "  - Run focused checks on one representative file while developing; run full regression only after all expected files exist.\n"
        "  - Avoid repeated full-project/example test loops unless code changed since the last run."
    )


def _build_dependency_layers_section(
    layers: list[list[str]],
    current_layer: int,
) -> str:
    """构建静态依赖层段落，避免 system prompt 中的当前层信息过期。"""
    _ = current_layer  # 保留参数兼容性；当前层由运行时消息宣布。
    total = len(layers)
    layer_lines = "\n".join(
        f"  Layer {i}: {', '.join(layer)}"
        for i, layer in enumerate(layers)
    )
    return (
        f"DEPENDENCY LAYERS ({total} layers):\n"
        f"{layer_lines}\n\n"
        f"The runtime will announce which layer is currently unlocked. "
        f"Only work on the current unlocked layer and earlier layers. "
        f"Higher layers are unlocked by runtime messages after tests pass."
    )


def _build_translation_order_section(translation_order: list[str]) -> str:
    """构建依赖优先的建议翻译顺序段落。"""
    order_lines = "\n".join(
        f"  {i+1}. {f}" for i, f in enumerate(translation_order)
    )
    return (
        f"SUGGESTED TRANSLATION ORDER (dependency-first):\n"
        f"{order_lines}\n\n"
        f"Files with no dependencies are listed first. Following this order\n"
        f"helps avoid missing-dependency errors. Start from the top."
    )


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
    source_files: list[str] | None = None,
    reflection_enabled: bool = True,
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
    route = get_effective_route(source_language, target_language)
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
    reflection_guidelines = REFLECTION_GUIDELINES if reflection_enabled else ""
    sections.append(
        GUIDELINES.format(
            pair_instruction=pair_instruction,
            target_guidelines=_build_target_guidelines(target_language),
            route_guidelines=_build_route_guidelines(route),
            reflection_guidelines=reflection_guidelines,
        )
    )

    # ── 4. 项目文件树 ──────────────────────────────────────────
    if project_tree:
        sections.append(f"PROJECT FILES:\n{project_tree}")

    # ── 5. 待生成的目标文件 ────────────────────────────────────
    # 优先用显式源文件/translation_order，避免展示性 project_tree 混入。
    selected_source_files = _select_source_files_for_targets(
        source_files=source_files,
        translation_order=translation_order,
        project_tree=project_tree,
    )
    if selected_source_files:
        sections.append(_build_files_to_create_section(selected_source_files, route))
        if len(selected_source_files) <= 5:
            sections.append(_build_small_project_fast_path_section(selected_source_files))
        else:
            sections.append(_build_large_project_batch_section(selected_source_files))

    # ── 6. 依赖层 / 翻译顺序 ───────────────────────────────────
    if layers:
        sections.append(_build_dependency_layers_section(layers, current_layer))
    elif translation_order:
        sections.append(_build_translation_order_section(translation_order))

    return "\n\n".join(sections)
