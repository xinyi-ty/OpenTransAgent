"""从 .env 和环境变量读取配置。"""
from __future__ import annotations

import os
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _get_env_int(name: str, default: int = 120) -> int:
    """读取整数型环境变量，解析失败时静默返回默认值。"""
    try:
        raw = os.environ.get(name, "")
        return int(raw) if raw else default
    except (ValueError, TypeError):
        return default


def get_llm_config(
    args: Any = None,
) -> tuple[str, str, str | None, int]:
    """获取 LLM 配置。优先级：命令行 > .env > 默认值。"""
    model = (args.llm_model if args else None) or os.environ.get("LLM_MODEL", "")
    api_key = (args.llm_api_key if args else None) or os.environ.get("LLM_API_KEY", "")
    base_url = (args.llm_base_url if args else None) or os.environ.get("LLM_BASE_URL") or None
    timeout = (args.llm_timeout if args else None) or _get_env_int("LLM_TIMEOUT", 120)
    return model, api_key, base_url, timeout


def get_max_iterations(args: Any = None) -> int:
    """获取最大迭代次数。"""
    if args and getattr(args, "max_iterations", None):
        return args.max_iterations
    return _get_env_int("MAX_ITERATIONS", 120)


def get_toolchain_paths() -> str:
    """获取编译工具链额外路径（分号分隔），用于 test_analyzer 的子进程 PATH。"""
    return os.environ.get("TOOLCHAIN_PATHS", "")
