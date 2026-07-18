"""语言配置：每种语言的源文件扩展名和目标文件扩展名。"""

LANGUAGE_CONFIG = {
    "python": {
        "source": [".py"],
        "target": [".py"],
    },
    "cpp": {
        "source": [".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx"],
        "target": [".cpp", ".h", ".hpp"],
    },
    "c": {
        "source": [".c", ".h"],
        "target": [".c", ".h"],
    },
    "java": {
        "source": [".java"],
        "target": [".java"],
    },
    "csharp": {
        "source": [".cs"],
        "target": [".cs"],
    },
    "go": {
        "source": [".go"],
        "target": [".go"],
    },
    "rust": {
        "source": [".rs"],
        "target": [".rs"],
    },
    "javascript": {
        "source": [".js", ".jsx"],
        "target": [".js", ".jsx"],
    },
    "typescript": {
        "source": [".ts", ".tsx"],
        "target": [".ts", ".tsx"],
    },
}


def get_source_extensions(language: str) -> list[str]:
    """获取源语言的扫描扩展名。"""
    cfg = LANGUAGE_CONFIG.get(language.lower())
    return cfg["source"] if cfg else [f".{language}"]


def get_target_extensions(language: str) -> list[str]:
    """获取目标语言的结果提取扩展名。"""
    cfg = LANGUAGE_CONFIG.get(language.lower())
    return cfg["target"] if cfg else [f".{language}"]
