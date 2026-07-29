"""语言配置：扩展名、测试/产物策略，以及共享语言名规范化。"""

from __future__ import annotations

COMMON_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", "build", "dist", "target", "CMakeFiles",
}

ARTIFACT_SKIP_PARTS = {
    "test", "tests", "public_test", "public_tests", "spec", "specs",
    "conftest.py", "run_tests.sh", "run_public_tests.sh",
    "__init__.py",  # precheck 生成的占位，非翻译产物
    "build", "_deps", "CMakeFiles", ".persist",
}

INFRA_FILE_NAMES = {
    "run_tests.sh", "run_public_tests.sh",
    "CMakeLists.txt", "Makefile",
    "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml",
    "pytest.ini", "tox.ini", "conftest.py",
    "Cargo.toml", "go.mod",
    "package.json",
}

PROTECTED_INFRA_FILES = {"CMakeLists.txt", "run_tests.sh", "run_public_tests.sh"}
PROTECTED_TEST_DIRS = ("tests/", "public_tests/", "test/")

LANGUAGE_CONFIG: dict[str, dict[str, object]] = {
    "python": {
        "source_exts": [".py"],
        "target_exts": [".py"],
        "test_exts": [".py"],
        "test_unit_label": "tests",
    },
    "cpp": {
        "source_exts": [".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx"],
        "target_exts": [".cpp", ".h", ".hpp"],
        "test_exts": [".cpp", ".cxx", ".cc"],
        "test_unit_label": "CTest targets",
        "refresh_precheck_after_test_copy": True,
    },
    "c": {
        "source_exts": [".c", ".h"],
        "target_exts": [".c", ".h"],
        "test_exts": [".c"],
    },
    "java": {
        "source_exts": [".java"],
        "target_exts": [".java"],
        "test_exts": [".java"],
    },
    "csharp": {
        "source_exts": [".cs"],
        "target_exts": [".cs"],
        "test_exts": [".cs"],
    },
    "go": {
        "source_exts": [".go"],
        "target_exts": [".go"],
        "test_exts": [".go"],
    },
    "rust": {
        "source_exts": [".rs"],
        "target_exts": [".rs"],
        "test_exts": [".rs"],
    },
    "javascript": {
        "source_exts": [".js", ".jsx"],
        "target_exts": [".js", ".jsx"],
        "test_exts": [".js", ".jsx"],
    },
    "typescript": {
        "source_exts": [".ts", ".tsx"],
        "target_exts": [".ts", ".tsx"],
        "test_exts": [".ts", ".tsx"],
    },
}

_LANGUAGE_ALIASES = {
    "py": "python",
    "c++": "cpp",
    "cplusplus": "cpp",
    "cc": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "golang": "go",
    "js": "javascript",
    "ts": "typescript",
}


def normalize_language(language: str) -> str:
    """规范化语言名，供扩展名和路由查询使用。"""
    key = language.strip().lower()
    return _LANGUAGE_ALIASES.get(key, key)


def is_known_language(language: str) -> bool:
    """检查语言是否在内置语言配置中。"""
    return normalize_language(language) in LANGUAGE_CONFIG


def _fallback_extension(language: str) -> str:
    """未知语言 fallback：保证返回合法扩展名格式。"""
    normalized = normalize_language(language).lstrip(".")
    return f".{normalized}" if normalized else ".txt"


def _get_list(language: str, key: str) -> list[str]:
    cfg = LANGUAGE_CONFIG.get(normalize_language(language))
    value = cfg.get(key, []) if cfg else []
    return list(value) if isinstance(value, list) else []


def get_source_extensions(language: str) -> list[str]:
    """获取源语言扫描扩展名。"""
    exts = _get_list(language, "source_exts")
    return exts if exts else [_fallback_extension(language)]


def get_target_extensions(language: str) -> list[str]:
    """获取目标语言的结果提取扩展名（用于 extract_results 筛选文件）。"""
    exts = _get_list(language, "target_exts")
    return exts if exts else [_fallback_extension(language)]


def get_test_extensions(language: str) -> list[str]:
    """获取测试文件扩展名；未知语言退回目标扩展名。"""
    exts = _get_list(language, "test_exts")
    return exts if exts else get_target_extensions(language)


def get_all_code_extensions() -> tuple[str, ...]:
    """汇总所有已配置语言的源码/目标/测试扩展名，供搜索类工具复用。"""
    exts: list[str] = []
    for cfg in LANGUAGE_CONFIG.values():
        for key in ("source_exts", "target_exts", "test_exts"):
            for ext in cfg.get(key, []):
                if isinstance(ext, str) and ext not in exts:
                    exts.append(ext)
    return tuple(exts)


def get_test_unit_label(language: str) -> str:
    """获取测试统计单位文案。"""
    cfg = LANGUAGE_CONFIG.get(normalize_language(language))
    value = cfg.get("test_unit_label") if cfg else None
    return value if isinstance(value, str) and value else "tests"


def should_refresh_precheck_after_test_copy(language: str) -> bool:
    """复制测试文件后是否需要重新生成/刷新目标语言脚手架。"""
    cfg = LANGUAGE_CONFIG.get(normalize_language(language))
    return bool(cfg.get("refresh_precheck_after_test_copy")) if cfg else False
