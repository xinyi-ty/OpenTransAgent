"""翻译语言路由器：统一管理不同语言对的配置和流程。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ═══════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════


@dataclass
class TranslationRoute:
    """一个翻译方向（源→目标）的完整配置。"""
    pair: tuple[str, str]            # (source, target)
    prompt_pair_instruction: str     # prompt 中的翻译对指引
    prompt_env_restriction: str      # prompt 中的环境限制
    file_extension_map: Callable[[str], str] | None = None  # 文件名映射规则（源→目标）


# ═══════════════════════════════════════════════════════════════
#  文件名映射函数
# ═══════════════════════════════════════════════════════════════


def _cpp_to_py_ext(f: str) -> str:
    """将 C++ 文件名映射为对应的 Python 文件名。"""
    p = Path(f)
    if p.suffix in (".h", ".hpp", ".cpp", ".cxx"):
        return p.with_suffix(".py").as_posix()
    return f


def _py_to_cpp_ext(f: str) -> str:
    """将 Python 文件名映射为对应的 C++ 文件名。"""
    p = Path(f)
    if p.suffix == ".py":
        return p.with_suffix(".cpp").as_posix()
    return f


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
            "cmake, g++ (MinGW), and make are available via Git Bash."
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
    """获取指定翻译方向的路由配置，未注册时返回 None。"""
    key = (source_language.lower(), target_language.lower())
    return ROUTES.get(key)


def validate_pair(source_language: str, target_language: str) -> bool:
    """检查翻译语言对是否受支持。"""
    return get_route(source_language, target_language) is not None
