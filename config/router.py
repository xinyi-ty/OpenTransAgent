"""翻译语言路由器：统一管理不同语言对的配置和流程。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from config.languages import (
    get_source_extensions,
    get_target_extensions,
    is_known_language,
    normalize_language,
)

# ═══════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TranslationRoute:
    """一个翻译方向（源→目标）的完整配置。"""

    pair: tuple[str, str]            # (source, target)
    prompt_pair_instruction: str     # prompt 中的翻译对指引
    prompt_env_restriction: str      # prompt 中的环境限制
    file_extension_map: Callable[[str], str] | None = None  # 文件名映射规则（源→目标）
    explicit: bool = True            # 是否为显式注册的语言对


# ═══════════════════════════════════════════════════════════════
#  文件名映射函数
# ═══════════════════════════════════════════════════════════════


def _cpp_to_py_ext(f: str) -> str:
    """将 C++ 文件名映射为对应的 Python 文件名。"""
    p = Path(f)
    if p.suffix.lower() in (".h", ".hpp", ".hxx", ".cpp", ".cxx", ".cc"):
        return p.with_suffix(".py").as_posix()
    return f


def _py_to_cpp_ext(f: str) -> str:
    """将 Python 文件名映射为对应的 C++ 文件名。"""
    p = Path(f)
    if p.suffix.lower() == ".py":
        return p.with_suffix(".cpp").as_posix()
    return f


def _default_extension_map(source_language: str, target_language: str) -> Callable[[str], str]:
    """生成通用文件名映射：源语言扩展名 → 目标语言主扩展名。"""
    source_exts = {ext.lower() for ext in get_source_extensions(source_language)}
    target_exts = get_target_extensions(target_language)
    primary_target_ext = target_exts[0] if target_exts else ".txt"

    def mapper(filename: str) -> str:
        p = Path(filename)
        if p.suffix.lower() in source_exts:
            return p.with_suffix(primary_target_ext).as_posix()
        return filename

    return mapper


# ═══════════════════════════════════════════════════════════════
#  路由注册
# ═══════════════════════════════════════════════════════════════

ROUTES: dict[tuple[str, str], TranslationRoute] = {
    ("cpp", "python"): TranslationRoute(
        pair=("cpp", "python"),
        prompt_pair_instruction=(
            "For each .h/.cpp file pair, directly create the equivalent .py file"
        ),
        prompt_env_restriction=(
            "ENVIRONMENT: Windows (cmd.exe shell). "
            "Do NOT use apt-get, make, g++, or other Linux commands."
        ),
        file_extension_map=_cpp_to_py_ext,
    ),
    ("python", "cpp"): TranslationRoute(
        pair=("python", "cpp"),
        prompt_pair_instruction=(
            "For each .py file, create the equivalent .cpp file (and .h if needed)"
        ),
        prompt_env_restriction=(
            "ENVIRONMENT: Windows (cmd.exe shell). "
            "cmake, g++ (MinGW), and make are available via Git Bash.\n\n"
            "BUILD SYSTEM: CMakeLists.txt is pre-configured and auto-discovers "
            ".cpp files via GLOB_RECURSE. Place each translated .cpp at the same "
            "relative path as its source .py; the build system will pick it up "
            "automatically and registers GoogleTest tests with CTest. Google Test "
            "is expected to be available locally via GTest_DIR/GTEST_ROOT/"
            "GOOGLETEST_ROOT or common D:/googletest/mingw-install, "
            "D:/googletest, and D:/gtest paths; "
            "network FetchContent is a last resort only. CMakeLists.txt and tests are "
            "generated/target-project infrastructure; do not create_file, edit_file, or "
            "rewrite CMakeLists.txt, public_tests/*, tests/*, or test/*. Do NOT install dependencies, "
            "create GTest headers manually, or try alternate CMake generators. "
            "Use exactly: cmake -S . -B build -G \"MinGW Makefiles\" -DCMAKE_BUILD_TYPE=Release && "
            "cmake --build build --config Release && "
            "ctest --test-dir build --output-on-failure -C Release"
        ),
        file_extension_map=_py_to_cpp_ext,
    ),
}


# ═══════════════════════════════════════════════════════════════
#  查询接口
# ═══════════════════════════════════════════════════════════════


def get_route(
    source_language: str, target_language: str
) -> TranslationRoute | None:
    """获取显式注册的翻译方向路由配置，未注册时返回 None。"""
    key = (normalize_language(source_language), normalize_language(target_language))
    return ROUTES.get(key)


def get_effective_route(source_language: str, target_language: str) -> TranslationRoute | None:
    """获取有效路由：显式路由优先，已知语言对使用通用路由兜底。"""
    explicit_route = get_route(source_language, target_language)
    if explicit_route is not None:
        return explicit_route

    source = normalize_language(source_language)
    target = normalize_language(target_language)
    if not is_known_language(source) or not is_known_language(target):
        return None

    return TranslationRoute(
        pair=(source, target),
        prompt_pair_instruction=f"For each source file, create the equivalent {target} file",
        prompt_env_restriction=(
            "ENVIRONMENT: Windows (cmd.exe shell). Use only tools and commands "
            "available in the workspace; do not install dependencies unless they are already configured."
        ),
        file_extension_map=_default_extension_map(source, target),
        explicit=False,
    )


def validate_pair(source_language: str, target_language: str) -> bool:
    """检查翻译语言对是否受支持。"""
    return get_effective_route(source_language, target_language) is not None
