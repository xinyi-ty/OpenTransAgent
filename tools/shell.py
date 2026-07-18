"""命令执行工具：execute_command"""

import subprocess
from pathlib import Path
from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


class ExecuteCommandAction(Action):
    command: str = Field(description="要执行的命令")
    timeout: int = Field(default=60, description="超时秒数")


class ExecuteCommandObservation(Observation):
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    exit_code: int = Field(default=-1)
    command: str = Field(default="")


class ExecuteCommandExecutor(ToolExecutor):
    def __init__(self, working_dir: str = ".", default_timeout: int = 60):
        self.working_dir = Path(working_dir).resolve()
        self.default_timeout = default_timeout

    def __call__(self, action, conversation=None):
        timeout = action.timeout or self.default_timeout
        try:
            result = subprocess.run(action.command, shell=True, capture_output=True, text=True,
                                    cwd=self.working_dir, timeout=timeout)
            output = f"$ {action.command}\n"
            if result.stdout: output += result.stdout[:3000]
            if result.stderr: output += f"\n(stderr): {result.stderr[:1000]}"
            output += f"\nExit code: {result.returncode}"
            return ExecuteCommandObservation.from_text(text=output, stdout=result.stdout, stderr=result.stderr,
                                                        exit_code=result.returncode, command=action.command)
        except subprocess.TimeoutExpired:
            return ExecuteCommandObservation.from_text(text=f"命令超时（{timeout}秒）", is_error=True, stderr="超时", exit_code=-1, command=action.command)
        except Exception as e:
            return ExecuteCommandObservation.from_text(text=f"命令执行失败: {e}", is_error=True, stderr=str(e), exit_code=-1, command=action.command)


class ExecuteCommandTool(ToolDefinition):
    description: str = "执行 shell 命令"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        ws = kwargs.get("workspace_root", ".")
        to = kwargs.get("command_timeout", 60)
        return [cls(action_type=ExecuteCommandAction, observation_type=ExecuteCommandObservation,
                     executor=ExecuteCommandExecutor(working_dir=ws, default_timeout=to))]


register_tool("execute_command", ExecuteCommandTool)
