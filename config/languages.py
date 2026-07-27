"""语言配置：每种语言作为源语言时的扫描扩展名，以及作为目标语言时的提取扩展名。"""

LANGUAGE_CONFIG: dict[str, dict[str, list[str]]] = {
    "python": {
        "source_exts": [".py"],
        "target_exts": [".py"],
    },
    "cpp": {
        "source_exts": [".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx"],
        "target_exts": [".cpp", ".h", ".hpp"],
    },
    "c": {
        "source_exts": [".c", ".h"],
        "target_exts": [".c", ".h"],
    },
    "java": {
        "source_exts": [".java"],
        "target_exts": [".java"],
    },
    "csharp": {
        "source_exts": [".cs"],
        "target_exts": [".cs"],
    },
    "go": {
        "source_exts": [".go"],
        "target_exts": [".go"],
    },
    "rust": {
        "source_exts": [".rs"],
        "target_exts": [".rs"],
    },
    "javascript": {
        "source_exts": [".js", ".jsx"],
        "target_exts": [".js", ".jsx"],
    },
    "typescript": {
        "source_exts": [".ts", ".tsx"],
        "target_exts": [".ts", ".tsx"],
    },
}


def get_target_extensions(language: str) -> list[str]:
    """获取目标语言的结果提取扩展名（用于 extract_results 筛选文件）。"""
    cfg = LANGUAGE_CONFIG.get(language.lower())
    return cfg["target_exts"] if cfg else [f".{language}"]
