"""命令执行工具：execute_command"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field

_STDOUT_PREVIEW_LIMIT = 3000
_STDERR_PREVIEW_LIMIT = 1000


class ExecuteCommandAction(Action):
    command: str = Field(description="要执行的命令")
    timeout: int = Field(default=60, description="超时秒数")


class ExecuteCommandObservation(Observation):
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    exit_code: int = Field(default=-1)
    command: str = Field(default="")


def _normalize_timeout(timeout: int | None, default_timeout: int) -> int:
    """规范化 timeout，保证至少 1 秒。"""
    value = timeout if timeout and timeout > 0 else default_timeout
    return max(1, value)


def _build_env() -> dict[str, str]:
    """构建子进程环境，确保 Python 子进程优先使用 UTF-8。"""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _coerce_output(value) -> str:
    """TimeoutExpired 可能返回 bytes/str/None，这里统一为 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _preview_stream(name: str, value: str, limit: int) -> str:
    """生成 stdout/stderr 预览，带截断提示。"""
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... ({name} truncated {len(value) - limit} chars)"


def _format_result(command: str, stdout: str, stderr: str, exit_code: int) -> str:
    output = f"$ {command}\n"
    if stdout:
        output += _preview_stream("stdout", stdout, _STDOUT_PREVIEW_LIMIT)
    if stderr:
        output += f"\n(stderr): {_preview_stream('stderr', stderr, _STDERR_PREVIEW_LIMIT)}"
    output += f"\nExit code: {exit_code}"
    return output


class ExecuteCommandExecutor(ToolExecutor):
    def __init__(self, working_dir: str = ".", default_timeout: int = 60):
        self.working_dir = Path(working_dir).resolve()
        self.default_timeout = max(1, default_timeout)

    def __call__(self, action, conversation=None):
        command = action.command.strip()
        if not command:
            return ExecuteCommandObservation.from_text(
                text="命令不能为空", is_error=True,
                stderr="命令不能为空", exit_code=-1, command=action.command,
            )
        if not self.working_dir.exists() or not self.working_dir.is_dir():
            return ExecuteCommandObservation.from_text(
                text=f"工作目录不存在: {self.working_dir}", is_error=True,
                stderr="工作目录不存在", exit_code=-1, command=command,
            )

        timeout = _normalize_timeout(action.timeout, self.default_timeout)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.working_dir,
                timeout=timeout,
                env=_build_env(),
            )
            output = _format_result(command, result.stdout, result.stderr, result.returncode)
            return ExecuteCommandObservation.from_text(
                text=output, stdout=result.stdout, stderr=result.stderr,
                exit_code=result.returncode, command=command,
            )
        except subprocess.TimeoutExpired as e:
            stdout = _coerce_output(e.stdout)
            stderr = _coerce_output(e.stderr) or "超时"
            text = f"命令超时（{timeout}秒）"
            if stdout or stderr:
                text += "\n" + _format_result(command, stdout, stderr, -1)
            return ExecuteCommandObservation.from_text(
                text=text, is_error=True, stdout=stdout, stderr=stderr,
                exit_code=-1, command=command,
            )
        except Exception as e:
            return ExecuteCommandObservation.from_text(
                text=f"命令执行失败: {e}", is_error=True,
                stderr=str(e), exit_code=-1, command=command,
            )


class ExecuteCommandTool(ToolDefinition):
    description: str = "执行 shell 命令"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        ws = kwargs.get("workspace_root", ".")
        to = kwargs.get("command_timeout", 60)
        return [cls(action_type=ExecuteCommandAction, observation_type=ExecuteCommandObservation,
                    executor=ExecuteCommandExecutor(working_dir=ws, default_timeout=to))]


register_tool("execute_command", ExecuteCommandTool)
