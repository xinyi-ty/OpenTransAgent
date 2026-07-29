"""从 .env 和环境变量读取配置。"""
from __future__ import annotations

import os
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_LLM_TIMEOUT = 120
DEFAULT_MAX_ITERATIONS = 120
DEFAULT_STEPS_PER_ROUND = 50
DEFAULT_TOOL_COMMAND_TIMEOUT = 60
DEFAULT_SEARCH_MAX_RESULTS = 10
DEFAULT_ROUND_TIMEOUT = 1800
DEFAULT_TEST_TIMEOUT = 300
DEFAULT_TEST_RAW_OUTPUT_LIMIT = 5000
DEFAULT_REFLECTION_ENABLED = True
DEFAULT_INVALID_RESPONSE_LIMIT = 3
DEFAULT_RUNTIME_ERROR_LIMIT = 3
DEFAULT_COMPLETENESS_RETRY_LIMIT = 3
DEFAULT_TRACE_LOG_ENABLED = True
DEFAULT_TRACE_LOG_MAX_FIELD_CHARS = 20000
DEFAULT_TRACE_LOG_REDACT_SECRETS = True

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "n"}


def _clamp_min(value: int, default: int, min_value: int | None) -> int:
    """按最小值约束整数配置，非法范围回退默认值。"""
    if min_value is not None and value < min_value:
        return default
    return value


def _get_env_int(
    name: str,
    default: int = DEFAULT_MAX_ITERATIONS,
    min_value: int | None = None,
) -> int:
    """读取整数型环境变量，解析失败或小于最小值时返回默认值。"""
    try:
        raw = os.environ.get(name, "")
        if raw == "":
            return default
        value = int(raw)
        return _clamp_min(value, default, min_value)
    except (ValueError, TypeError):
        return default


def _get_arg_or_env_int(
    args: Any,
    attr: str,
    env_name: str,
    default: int,
    min_value: int | None = None,
) -> int:
    """读取整数配置，优先级：命令行 > .env/环境变量 > 默认值。"""
    value = getattr(args, attr, None) if args else None
    if value is not None:
        return _clamp_min(value, default, min_value)
    return _get_env_int(env_name, default, min_value)


def _get_arg_or_env_str(args: Any, attr: str, env_name: str, default: str = "") -> str:
    """读取字符串配置，优先级：命令行 > .env/环境变量 > 默认值。"""
    value = getattr(args, attr, None) if args else None
    if value:
        return value
    return os.environ.get(env_name, default)


def _get_env_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量；非法值回退默认值。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def get_llm_config(
    args: Any = None,
) -> tuple[str, str, str | None, int]:
    """获取 LLM 配置。优先级：命令行 > .env > 默认值。"""
    model = _get_arg_or_env_str(args, "llm_model", "LLM_MODEL")
    api_key = _get_arg_or_env_str(args, "llm_api_key", "LLM_API_KEY")
    base_url = _get_arg_or_env_str(args, "llm_base_url", "LLM_BASE_URL") or None
    timeout = _get_arg_or_env_int(
        args, "llm_timeout", "LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT, min_value=1
    )
    return model, api_key, base_url, timeout


def get_max_iterations(args: Any = None) -> int:
    """获取最大外循环次数。"""
    return _get_arg_or_env_int(
        args, "max_iterations", "MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS, min_value=1
    )


def get_steps_per_round(args: Any = None) -> int:
    """获取每轮 Conversation.run 允许的 Agent step 数。"""
    return _get_arg_or_env_int(
        args, "steps_per_round", "STEPS_PER_ROUND", DEFAULT_STEPS_PER_ROUND, min_value=1
    )


def get_tool_command_timeout(args: Any = None) -> int:
    """获取 execute_command 工具默认超时时间（秒）。"""
    return _get_arg_or_env_int(
        args,
        "tool_command_timeout",
        "TOOL_COMMAND_TIMEOUT",
        DEFAULT_TOOL_COMMAND_TIMEOUT,
        min_value=1,
    )


def get_search_max_results(args: Any = None) -> int:
    """获取 search_content 工具默认最大结果数。"""
    return _get_arg_or_env_int(
        args,
        "search_max_results",
        "SEARCH_MAX_RESULTS",
        DEFAULT_SEARCH_MAX_RESULTS,
        min_value=1,
    )


def get_round_timeout(args: Any = None) -> int:
    """获取单个外循环 round 的超时时间（秒）。"""
    return _get_arg_or_env_int(
        args, "round_timeout", "ROUND_TIMEOUT", DEFAULT_ROUND_TIMEOUT, min_value=1
    )


def get_test_timeout(args: Any = None) -> int:
    """获取测试分析器超时时间（秒）。"""
    return _get_arg_or_env_int(
        args, "test_timeout", "TEST_TIMEOUT", DEFAULT_TEST_TIMEOUT, min_value=1
    )


def get_test_raw_output_limit(args: Any = None) -> int:
    """获取保存给 LLM 的测试原始输出上限；0 表示不截断。"""
    return _get_arg_or_env_int(
        args,
        "test_raw_output_limit",
        "TEST_RAW_OUTPUT_LIMIT",
        DEFAULT_TEST_RAW_OUTPUT_LIMIT,
        min_value=0,
    )


def get_reflection_enabled(args: Any = None) -> bool:
    """获取是否启用反思纠错。默认开启。"""
    if args and getattr(args, "no_reflection", False):
        return False
    return _get_env_bool("REFLECTION_ENABLED", DEFAULT_REFLECTION_ENABLED)


def get_invalid_response_limit(args: Any = None) -> int:
    """获取连续无效响应上限。"""
    return _get_arg_or_env_int(
        args,
        "invalid_response_limit",
        "INVALID_RESPONSE_LIMIT",
        DEFAULT_INVALID_RESPONSE_LIMIT,
        min_value=1,
    )


def get_runtime_error_limit(args: Any = None) -> int:
    """获取可恢复运行时错误连续重试上限。"""
    return _get_arg_or_env_int(
        args,
        "runtime_error_limit",
        "RUNTIME_ERROR_LIMIT",
        DEFAULT_RUNTIME_ERROR_LIMIT,
        min_value=1,
    )


def get_completeness_retry_limit(args: Any = None) -> int:
    """获取翻译完整性检查失败后的连续补齐重试上限。"""
    return _get_arg_or_env_int(
        args,
        "completeness_retry_limit",
        "COMPLETENESS_RETRY_LIMIT",
        DEFAULT_COMPLETENESS_RETRY_LIMIT,
        min_value=1,
    )


def get_toolchain_paths() -> str:
    """获取编译工具链额外路径；TOOLCHAIN_PATHS 优先，兼容旧 OPENHANDS_TOOLCHAIN_PATHS。"""
    return (
        os.environ.get("TOOLCHAIN_PATHS", "")
        or os.environ.get("OPENHANDS_TOOLCHAIN_PATHS", "")
    )


def get_trace_log_enabled(args: Any = None) -> bool:
    """获取是否启用功能调用追踪日志（translation_trace.jsonl）。"""
    if args and getattr(args, "no_trace_log", False):
        return False
    return _get_env_bool("TRACE_LOG_ENABLED", DEFAULT_TRACE_LOG_ENABLED)


def get_trace_log_max_field_chars(args: Any = None) -> int:
    """获取追踪日志字段截断阈值。"""
    return _get_arg_or_env_int(
        args, "trace_log_max_field_chars", "TRACE_LOG_MAX_FIELD_CHARS",
        DEFAULT_TRACE_LOG_MAX_FIELD_CHARS, min_value=100,
    )


def get_trace_log_redact_secrets(args: Any = None) -> bool:
    """获取追踪日志是否对密钥/敏感模式做 redaction。"""
    if args and getattr(args, "no_trace_redact", False):
        return False
    return _get_env_bool("TRACE_LOG_REDACT_SECRETS", DEFAULT_TRACE_LOG_REDACT_SECRETS)
