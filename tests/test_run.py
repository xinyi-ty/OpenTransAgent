from __future__ import annotations

import time
from pathlib import Path

import pytest

from run import (
    _build_layer_test_command,
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
