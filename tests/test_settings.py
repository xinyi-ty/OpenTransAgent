from __future__ import annotations

from types import SimpleNamespace

from config.settings import (
    get_completeness_retry_limit,
    get_invalid_response_limit,
    get_llm_config,
    get_reflection_enabled,
    get_round_timeout,
    get_runtime_error_limit,
    get_search_max_results,
    get_steps_per_round,
    get_test_raw_output_limit,
    get_test_timeout,
    get_tool_command_timeout,
    get_toolchain_paths,
)


def test_translation_runtime_defaults(monkeypatch) -> None:
    for name in [
        "STEPS_PER_ROUND",
        "TOOL_COMMAND_TIMEOUT",
        "SEARCH_MAX_RESULTS",
        "ROUND_TIMEOUT",
        "TEST_TIMEOUT",
        "TEST_RAW_OUTPUT_LIMIT",
        "REFLECTION_ENABLED",
        "INVALID_RESPONSE_LIMIT",
        "RUNTIME_ERROR_LIMIT",
        "COMPLETENESS_RETRY_LIMIT",
    ]:
        monkeypatch.delenv(name, raising=False)

    assert get_steps_per_round() == 50
    assert get_tool_command_timeout() == 60
    assert get_search_max_results() == 10
    assert get_round_timeout() == 1800
    assert get_test_timeout() == 300
    assert get_test_raw_output_limit() == 5000
    assert get_reflection_enabled() is True
    assert get_invalid_response_limit() == 3
    assert get_runtime_error_limit() == 3
    assert get_completeness_retry_limit() == 3


def test_cli_values_override_env(monkeypatch) -> None:
    monkeypatch.setenv("STEPS_PER_ROUND", "50")
    args = SimpleNamespace(steps_per_round=7)

    assert get_steps_per_round(args) == 7


def test_zero_is_allowed_for_raw_output_limit(monkeypatch) -> None:
    monkeypatch.setenv("TEST_RAW_OUTPUT_LIMIT", "5000")
    args = SimpleNamespace(test_raw_output_limit=0)

    assert get_test_raw_output_limit(args) == 0


def test_invalid_or_too_small_int_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("STEPS_PER_ROUND", "-1")
    monkeypatch.setenv("TEST_RAW_OUTPUT_LIMIT", "-1")

    assert get_steps_per_round() == 50
    assert get_test_raw_output_limit() == 5000


def test_bool_invalid_value_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("REFLECTION_ENABLED", "not-a-bool")

    assert get_reflection_enabled() is True


def test_bool_false_values_are_supported(monkeypatch) -> None:
    monkeypatch.setenv("REFLECTION_ENABLED", "off")

    assert get_reflection_enabled() is False


def test_no_reflection_cli_disables_reflection(monkeypatch) -> None:
    monkeypatch.setenv("REFLECTION_ENABLED", "true")
    args = SimpleNamespace(no_reflection=True)

    assert get_reflection_enabled(args) is False


def test_llm_timeout_uses_unified_int_validation(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT", "0")
    args = SimpleNamespace(
        llm_model="",
        llm_api_key="",
        llm_base_url="",
        llm_timeout=None,
    )

    assert get_llm_config(args)[3] == 120


def test_toolchain_paths_supports_legacy_env(monkeypatch) -> None:
    monkeypatch.delenv("TOOLCHAIN_PATHS", raising=False)
    monkeypatch.setenv("OPENHANDS_TOOLCHAIN_PATHS", "legacy-path")

    assert get_toolchain_paths() == "legacy-path"


def test_toolchain_paths_prefers_canonical_env(monkeypatch) -> None:
    monkeypatch.setenv("TOOLCHAIN_PATHS", "new-path")
    monkeypatch.setenv("OPENHANDS_TOOLCHAIN_PATHS", "legacy-path")

    assert get_toolchain_paths() == "new-path"
