from __future__ import annotations

import time
from pathlib import Path

import pytest
from openhands.sdk import Conversation, LLM

from agent.translation_agent import ReActTranslationAgent
from run import (
    _build_layer_test_command,
    _check_translation_completeness,
    _collect_visible_test_files,
    _expected_target_for_source,
    _format_completeness_feedback,
    _run_conversation_with_timeout,
    _safe_close_conversation,
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


def test_collect_visible_test_files_is_cumulative_and_deduped() -> None:
    test_layers = [["tests/a.py", "tests/common.py"], ["tests/b.py", "tests/common.py"]]

    assert _collect_visible_test_files(test_layers, 0) == ["tests/a.py", "tests/common.py"]
    assert _collect_visible_test_files(test_layers, 1) == [
        "tests/a.py",
        "tests/common.py",
        "tests/b.py",
    ]
    assert _collect_visible_test_files(None, 1) == []


def test_build_layer_test_command_quotes_python_paths(tmp_path: Path) -> None:
    command = _build_layer_test_command(["tests/my test.py"], str(tmp_path))

    assert command is not None
    assert "python -m pytest" in command
    assert '"tests/my test.py"' in command


def test_build_layer_test_command_uses_existing_cpp_binary(tmp_path: Path) -> None:
    exe = tmp_path / "build" / "test_math.exe"
    exe.parent.mkdir()
    exe.write_text("", encoding="utf-8")

    command = _build_layer_test_command(["tests/test_math.cpp"], str(tmp_path))

    assert command == "build\\test_math.exe 2>&1"


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
