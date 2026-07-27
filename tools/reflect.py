"""反思纠错工具：reflect — 分析编译/测试错误的根因并给出修复策略（不修改文件）。"""

from __future__ import annotations

from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field


class ReflectAction(Action):
    source_function: str = Field(description="待翻译的源函数代码")
    translated_code: str = Field(description="当前的翻译结果")
    error_message: str = Field(description="编译或测试错误信息")
    test_results: str = Field(description="测试执行结果")


class ReflectObservation(Observation):
    root_cause: str = Field(default="")
    fix_strategy: str = Field(default="")


class ReflectExecutor(ToolExecutor):
    def __call__(self, action, conversation=None):
        # TODO: 当前为占位实现，仅截取错误信息前 500 字符。
        # 后续可接入 LLM 调用，对编译/测试错误做结构化根因分析，
        # 返回具体的出错文件、行号和修复建议。
        text = (
            f"[Reflect Analysis]\n"
            f"Root cause (truncated): {action.error_message[:500]}\n"
            f"Fix strategy: pending LLM-based analysis"
        )
        return ReflectObservation.from_text(
            text=text,
            root_cause=action.error_message[:500],
            fix_strategy="",
        )


class ReflectTool(ToolDefinition):
    description: str = "分析翻译错误的根因并制定修复策略（不修改任何文件）"
    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=ReflectAction,
            observation_type=ReflectObservation,
            executor=ReflectExecutor(),
        )]


register_tool("reflect", ReflectTool)
