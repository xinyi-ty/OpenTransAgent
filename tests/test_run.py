from __future__ import annotations

import time
from pathlib import Path

import pytest
from openhands.sdk import Conversation, LLM

from agent.translation_agent import ReActTranslationAgent
from analysis.test_analyzer import CompilationResult, TestAnalysis
from run import (
    _adaptive_steps_per_round,
    _build_layer_test_command,
    _check_translation_completeness,
    _collect_visible_test_files,
    _expected_target_for_source,
    _expected_translations_for_layers,
    _format_completeness_feedback,
    _format_large_layer_guidance,
    _format_required_targets_for_layer,
    _run_conversation_with_timeout,
    _safe_close_conversation,
    _test_result_unit_label,
    format_feedback,
)


class _State:
    execution_status = None


class _Conversation:
    def __init__(self) -> None:
        self.state = _State()
        self.interrupted = False
        self.closed = False

    def run(self) -> None:
        return None

    def interrupt(self) -> None:
        self.interrupted = True

    def close(self) -> None:
        self.closed = True


def test_trace_tool_call_info_supports_dict_and_direct_name() -> None:
    class ToolCall:
        id = "call-1"
        name = "edit_file"
        arguments = "{}"

    assert ReActTranslationAgent._trace_tool_call_info(ToolCall()) == {
        "id": "call-1",
        "name": "edit_file",
        "arguments": "{}",
    }
    assert ReActTranslationAgent._trace_tool_call_info({
        "id": "call-2",
        "function": {"name": "create_file", "arguments": "{x}"},
    }) == {
        "id": "call-2",
        "name": "create_file",
        "arguments": "{x}",
    }


def test_trace_logger_is_passed_during_agent_creation(tmp_path: Path) -> None:
    trace_logger = object()
    agent = ReActTranslationAgent.create(
        llm=LLM(model="dummy/model", api_key="dummy-key", timeout=1),
        workspace_root=str(tmp_path),
        project_name="demo",
        source_language="cpp",
        target_language="python",
        trace_logger=trace_logger,
    )

    assert agent.trace_logger is trace_logger


def test_trace_logger_is_excluded_from_conversation_serialization(tmp_path: Path) -> None:
    trace_logger = object()
    agent = ReActTranslationAgent.create(
        llm=LLM(model="dummy/model", api_key="dummy-key", timeout=1),
        workspace_root=str(tmp_path),
        project_name="demo",
        source_language="cpp",
        target_language="python",
        trace_logger=trace_logger,
    )

    dumped = agent.model_dump(mode="json")
    assert "trace_logger" not in dumped

    conv = Conversation(
        agent=agent,
        workspace=str(tmp_path / "workspace"),
        persistence_dir=None,
        visualizer=None,
    )
    conv.close()


def test_expected_target_for_source_uses_route_mapping() -> None:
    assert _expected_target_for_source("src/foo.cpp", "cpp", "python") == "src/foo.py"
    assert _expected_target_for_source("pkg/foo.py", "python", "cpp") == "pkg/foo.cpp"


def test_expected_target_for_source_uses_generalized_route_mapping() -> None:
    assert _expected_target_for_source("src/Foo.java", "java", "go") == "src/Foo.go"
    assert _expected_target_for_source("src/main.go", "go", "rust") == "src/main.rs"


def test_expected_translations_dedupes_cpp_header_and_source_pair() -> None:
    expected = _expected_translations_for_layers(
        [["src/foo.h", "src/foo.cpp"]],
        None,
        0,
        "cpp",
        "python",
    )

    assert len(expected) == 1
    assert expected[0].expected == "src/foo.py"


def test_check_translation_completeness_uses_paths_not_only_stems(tmp_path: Path) -> None:
    layers = [["src/a/util.cpp", "src/b/util.cpp"]]
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "util.py").write_text("", encoding="utf-8")

    result = _check_translation_completeness(
        str(tmp_path),
        layers,
        None,
        0,
        "cpp",
        "python",
    )

    assert result.passed is False
    assert result.expected_count == 2
    assert result.present_count == 1
    assert [m.expected for m in result.missing] == ["src/b/util.py"]


def test_completeness_requires_python_target_for_cpp_headers(tmp_path: Path) -> None:
    result = _check_translation_completeness(
        str(tmp_path),
        [["include/foo.hpp"]],
        None,
        0,
        "cpp",
        "python",
    )

    assert result.passed is False
    assert [m.expected for m in result.missing] == ["include/foo.py"]


def test_format_completeness_feedback_is_structured() -> None:
    result = _check_translation_completeness(
        ".",
        [["missing.cpp"]],
        None,
        0,
        "cpp",
        "python",
    )

    feedback = _format_completeness_feedback(result, attempt=2, retry_limit=3)

    assert "COMPLETENESS RECOVERY MODE" in feedback
    assert "Source: missing.cpp -> Expected target: missing.py" in feedback
    assert "Do NOT run tests while files are missing" in feedback


def test_format_feedback_treats_compile_failure_as_fixable_failure() -> None:
    analysis = TestAnalysis(
        compilation=CompilationResult(success=False, errors="compile broke"),
        total_tests=0,
        passed_tests=0,
    )

    feedback = format_feedback(analysis, reflection_enabled=False)

    assert "Compilation: FAILED" in feedback
    assert "Compilation output:" in feedback
    assert "compile broke" in feedback
    assert "Some tests are still failing" in feedback
    assert "Fix compilation errors first" in feedback
    assert "All tests pass" not in feedback


def test_collect_visible_test_files_is_cumulative_and_deduped() -> None:
    test_layers = [["tests/a.py", "tests/common.py"], ["tests/b.py", "tests/common.py"]]

    assert _collect_visible_test_files(test_layers, 0) == ["tests/a.py", "tests/common.py"]
    assert _collect_visible_test_files(test_layers, 1) == [
        "tests/a.py",
        "tests/common.py",
        "tests/b.py",
    ]
    assert _collect_visible_test_files(None, 1) == []


def test_test_result_unit_label_distinguishes_cpp_ctest_targets() -> None:
    assert _test_result_unit_label("python", "cpp") == "CTest targets"
    assert _test_result_unit_label("cpp", "python") == "tests"
    assert _test_result_unit_label("py", "cplusplus") == "CTest targets"


def test_adaptive_steps_per_round_scales_for_complex_layers() -> None:
    assert _adaptive_steps_per_round(30, ["a.py"], ["tests/a.cpp"]) == 30
    assert _adaptive_steps_per_round(30, ["a.py", "b.py"], ["t1.cpp", "t2.cpp", "t3.cpp"]) == 40
    assert _adaptive_steps_per_round(30, ["a.py", "b.py", "c.py", "d.py"], []) == 50
    assert _adaptive_steps_per_round(60, ["a.py", "b.py", "c.py", "d.py"], []) == 60
    assert _adaptive_steps_per_round(50, [f"f{i}.py" for i in range(6)], []) == 40
    assert _adaptive_steps_per_round(50, [f"f{i}.py" for i in range(10)], []) == 30


def test_format_large_layer_guidance_only_for_large_layers() -> None:
    assert _format_large_layer_guidance(["a.py", "b.py"]) == ""

    guidance = _format_large_layer_guidance([f"f{i}.py" for i in range(6)])

    assert "Large layer batching rules" in guidance
    assert "at most 4 source files" in guidance
    assert "more than 5 read_file/create_file/edit_file" in guidance
    assert "at most 3 representative test files" in guidance
    assert "do not cd into guessed external project paths" in guidance
    assert "API contract" not in guidance


def test_format_large_layer_guidance_api_contract_first() -> None:
    guidance = _format_large_layer_guidance(
        [f"f{i}.py" for i in range(6)],
        route_strength="api_contract_first",
    )

    assert "Large layer batching rules" in guidance
    assert "API contract" in guidance
    assert "visible C++ test files" in guidance
    assert "Match the test-expected API exactly" in guidance
    assert "never modify tests or CMakeLists.txt" in guidance


def test_format_required_targets_for_layer_lists_mapped_targets() -> None:
    text = _format_required_targets_for_layer(
        ["pythonflow/operations.py", "pythonflow/pfmq/broker.py"],
        "python",
        "cpp",
    )

    assert "Required target files for this layer:" in text
    assert "- pythonflow/operations.cpp" in text
    assert "- pythonflow/pfmq/broker.cpp" in text
    assert "Create all required target files before running build/tests." in text


def test_build_layer_test_command_quotes_python_paths(tmp_path: Path) -> None:
    command = _build_layer_test_command(["tests/my test.py"], str(tmp_path))

    assert command is not None
    assert "python -m pytest" in command
    assert '"tests/my test.py"' in command


def test_cpp_test_command_prefers_ctest_for_cpp_targets(tmp_path: Path) -> None:
    command = _build_layer_test_command(["tests/test_math.cpp"], str(tmp_path))

    assert command is not None
    assert "ctest --test-dir build --output-on-failure -C Release" in command
    assert '-R "^(tests_test_math)$"' in command
    assert "BUILD_FAILED_NO_TEST_EXECUTABLES" not in command


def test_cpp_test_command_uses_correct_target_name_from_precheck(tmp_path: Path) -> None:
    command = _build_layer_test_command(["public_tests/test_public_setup_py.cpp"], str(tmp_path))

    assert command is not None
    assert "public_tests_test_public_setup_py" in command
    assert "test_public_setup_py" not in command.replace("public_tests_test_public_setup_py", "")


def test_build_layer_test_command_combines_pytest_and_ctest(tmp_path: Path) -> None:
    command = _build_layer_test_command(
        ["tests/my test.py", "tests/test_math.cpp"],
        str(tmp_path),
    )

    assert command is not None
    assert "python -m pytest" in command
    assert "ctest --test-dir build" in command
    assert " && " in command


def test_run_conversation_with_timeout_interrupts_slow_run() -> None:
    class SlowConversation(_Conversation):
        def run(self) -> None:
            while not self.interrupted:
                time.sleep(0.01)

    conv = SlowConversation()

    completed, leaked = _run_conversation_with_timeout(
        conv,
        timeout=0.01,
        stop_wait_timeout=0.01,
    )

    assert completed is False
    assert leaked is False
    assert conv.interrupted is True


def test_run_conversation_with_timeout_reports_leaked_thread() -> None:
    class IgnoringConversation(_Conversation):
        def run(self) -> None:
            time.sleep(11)

    conv = IgnoringConversation()

    completed, leaked = _run_conversation_with_timeout(
        conv,
        timeout=0.01,
        stop_wait_timeout=0.01,
    )

    assert completed is False
    assert leaked is True
    assert conv.interrupted is True


def test_run_conversation_with_timeout_propagates_run_errors() -> None:
    class FailingConversation(_Conversation):
        def run(self) -> None:
            raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        _run_conversation_with_timeout(FailingConversation(), timeout=1)


def test_safe_close_conversation_closes_and_suppresses_errors() -> None:
    conv = _Conversation()
    _safe_close_conversation(conv)

    assert conv.closed is True

    class BadCloseConversation(_Conversation):
        def close(self) -> None:
            raise RuntimeError("close failed")

    _safe_close_conversation(BadCloseConversation())
