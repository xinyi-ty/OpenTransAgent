from __future__ import annotations

import subprocess

from tools.shell import (
    ExecuteCommandAction,
    ExecuteCommandExecutor,
    _STDOUT_PREVIEW_LIMIT,
    _build_env,
    _normalize_timeout,
    _preview_stream,
)


def test_normalize_timeout_uses_default_for_invalid_values() -> None:
    assert _normalize_timeout(0, 60) == 60
    assert _normalize_timeout(-1, 60) == 60
    assert _normalize_timeout(None, 60) == 60
    assert _normalize_timeout(5, 60) == 5
    assert _normalize_timeout(0, -5) == 1


def test_build_env_sets_utf8_defaults(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    env = _build_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_preview_stream_marks_truncation() -> None:
    preview = _preview_stream("stdout", "x" * (_STDOUT_PREVIEW_LIMIT + 5), _STDOUT_PREVIEW_LIMIT)

    assert "truncated 5 chars" in preview


def test_execute_command_rejects_empty_command(tmp_path) -> None:
    obs = ExecuteCommandExecutor(str(tmp_path))(
        ExecuteCommandAction(command="   ")
    )

    assert obs.is_error is True
    assert "命令不能为空" in obs.text


def test_execute_command_rejects_missing_working_dir(tmp_path) -> None:
    obs = ExecuteCommandExecutor(str(tmp_path / "missing"))(
        ExecuteCommandAction(command="echo hi")
    )

    assert obs.is_error is True
    assert "工作目录不存在" in obs.text


def test_execute_command_success(tmp_path) -> None:
    obs = ExecuteCommandExecutor(str(tmp_path))(
        ExecuteCommandAction(command="python -c \"print('hello')\"", timeout=10)
    )

    assert obs.is_error is False
    assert obs.exit_code == 0
    assert "hello" in obs.stdout
    assert "$ python -c" in obs.text


def test_repeated_successful_command_returns_advisory_without_blocking(tmp_path) -> None:
    executor = ExecuteCommandExecutor(str(tmp_path))
    action = ExecuteCommandAction(command="python -c \"print('ok')\"", timeout=10)

    first = executor(action)
    second = executor(action)

    assert first.is_error is False
    assert first.advisory_code == ""
    assert second.is_error is False
    assert second.exit_code == 0
    assert second.advisory_code == "repeated_successful_command"
    assert second.repeat_count == 2
    assert "Advisory:" in second.text


def test_workspace_change_resets_repeated_command_advisory(tmp_path) -> None:
    executor = ExecuteCommandExecutor(str(tmp_path))
    action = ExecuteCommandAction(command="python -c \"print('ok')\"", timeout=10)
    executor(action)
    (tmp_path / "source.py").write_text("x", encoding="utf-8")

    after_change = executor(action)

    assert after_change.is_error is False
    assert after_change.advisory_code == ""
    assert after_change.repeat_count == 1


def test_command_advisory_state_is_executor_local(tmp_path) -> None:
    action = ExecuteCommandAction(command="python -c \"print('ok')\"", timeout=10)
    first = ExecuteCommandExecutor(str(tmp_path))
    second = ExecuteCommandExecutor(str(tmp_path))
    first(action)
    first(action)

    observation = second(action)

    assert observation.advisory_code == ""
    assert observation.repeat_count == 1


def test_execute_command_timeout_preserves_partial_output(monkeypatch, tmp_path) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args", "cmd"), timeout=1,
            output="partial stdout", stderr="partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    obs = ExecuteCommandExecutor(str(tmp_path))(
        ExecuteCommandAction(command="slow", timeout=1)
    )

    assert obs.is_error is True
    assert "命令超时" in obs.text
    assert "partial stdout" in obs.stdout
    assert "partial stderr" in obs.stderr
