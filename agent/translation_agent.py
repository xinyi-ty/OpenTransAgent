"""ReActTranslationAgent — 继承 Agent 重写 step() 实现 ReAct 翻译决策循环。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from openhands.sdk import LLM, Agent
from openhands.sdk.agent.response_dispatch import classify_response, LLMResponseType
from openhands.sdk.agent.utils import make_llm_completion, prepare_llm_messages
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent, MessageEvent
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.tool.builtins.finish import FinishAction

from utils.logger import logger
from tools.registry import create_tools
from workspace.manager import LayerController

from .prompts import build_react_prompt


class ReActTranslationAgent(Agent):
    """基于 ReAct 范式的翻译 Agent。

    通过 Thought → Action → Observation 循环自动完成仓库级代码翻译。
    支持分层翻译（layer）、依赖顺序控制、反思纠错。

    可控性：
    - max_iterations（create 参数）控制 ReAct 循环总 step 上限
    - reflection_enabled  控制反思纠错提示和 reflect 工具开关
    - invalid_response_limit 控制连续无效响应的容忍次数
    """

    # ── 运行配置（通过 create() 或构造函数注入） ────────────────
    # CLI 正常路径会传入 max_iter × steps_per_round；500 仅是直接构造兜底。
    max_iterations: int = 500
    reflection_enabled: bool = True
    invalid_response_limit: int = 3
    workspace_root: str = "."
    project_name: str = ""
    source_language: str = ""
    target_language: str = ""
    project_tree: str = ""
    translation_order: list[str] | None = None
    source_files: list[str] | None = None
    trace_logger: Any | None = Field(default=None, exclude=True)

    # ── 内部状态 ────────────────────────────────────────────────
    _step_count: int = 0
    _invalid_response_count: int = 0

    # ═══════════════════════════════════════════════════════════
    #  工厂方法
    # ═══════════════════════════════════════════════════════════

    @classmethod
    def create(
        cls,
        llm: LLM,
        workspace_root: str = ".",
        max_iterations: int = 20,
        project_name: str = "",
        source_language: str = "",
        target_language: str = "",
        project_tree: str = "",
        translation_order: list[str] | None = None,
        layer_ctrl: LayerController | None = None,
        command_timeout: int = 60,
        search_max_results: int = 10,
        reflection_enabled: bool = True,
        invalid_response_limit: int = 3,
        source_files: list[str] | None = None,
        trace_logger: Any | None = None,
        **kwargs: Any,
    ) -> ReActTranslationAgent:
        """创建并返回一个配置好的 ReActTranslationAgent 实例。"""
        cls._setup_tool_environment(layer_ctrl)
        system_prompt = cls._build_system_prompt(
            source_language=source_language,
            target_language=target_language,
            project_name=project_name,
            project_tree=project_tree,
            translation_order=translation_order,
            layer_ctrl=layer_ctrl,
            source_files=source_files,
            reflection_enabled=reflection_enabled,
        )
        exclude_tools = {"reflect"} if not reflection_enabled else None
        tools_list = create_tools(
            exclude=exclude_tools,
            workspace_root=workspace_root,
            command_timeout=command_timeout,
            search_max_results=search_max_results,
        )
        return cls(
            llm=llm,
            tools=tools_list,
            max_iterations=max_iterations,
            reflection_enabled=reflection_enabled,
            invalid_response_limit=invalid_response_limit,
            workspace_root=workspace_root,
            project_name=project_name,
            source_language=source_language,
            target_language=target_language,
            project_tree=project_tree,
            translation_order=translation_order,
            source_files=source_files,
            trace_logger=trace_logger,
            system_prompt=system_prompt,
            condenser=None,
            **kwargs,
        )

    # ═══════════════════════════════════════════════════════════
    #  ReAct 步进
    # ═══════════════════════════════════════════════════════════

    def step(self, conversation, on_event, on_token=None):
        """执行一步 ReAct 循环。

        1. 检查迭代上限 → 达到则 finish
        2. 调用 LLM 获取响应
        3. 按响应类型分发：
           - TOOL_CALLS → 执行工具
           - CONTENT    → 仅明确完成文本才结束，否则提醒使用工具
           - 无效响应   → 提醒 LLM 使用工具
        """
        state = conversation.state

        if self._check_iteration_limit(state, on_event):
            return

        self._step_count += 1
        self._log_step_progress()

        llm_response = self._call_llm(state.view, on_token)
        self._dispatch_llm_response(llm_response, conversation, state, on_event)

    # ═══════════════════════════════════════════════════════════
    #  内部辅助方法
    # ═══════════════════════════════════════════════════════════

    # -- 工具 / 环境 -------------------------------------------

    @classmethod
    def _setup_tool_environment(cls, layer_ctrl: LayerController | None) -> None:
        """注册工具模块，并将层控制器注入 create_file 用于跨层检查。"""
        import tools.registry  # noqa: F401 — 触发工具注册
        from tools.file_ops import set_layer_ctrl

        set_layer_ctrl(layer_ctrl)

    # -- 提示构建 -----------------------------------------------

    @classmethod
    def _build_system_prompt(
        cls,
        source_language: str,
        target_language: str,
        project_name: str,
        project_tree: str = "",
        translation_order: list[str] | None = None,
        layer_ctrl: LayerController | None = None,
        source_files: list[str] | None = None,
        reflection_enabled: bool = True,
    ) -> str:
        """构建 ReAct 系统提示，包含语言对、项目信息、依赖层等上下文。"""
        layers = layer_ctrl.layers if layer_ctrl and layer_ctrl.active else None
        return build_react_prompt(
            source_language=source_language,
            target_language=target_language,
            project_name=project_name,
            project_tree=project_tree,
            translation_order=translation_order,
            layers=layers,
            current_layer=0,
            source_files=source_files,
            reflection_enabled=reflection_enabled,
        )

    # -- 迭代控制 -----------------------------------------------

    def _check_iteration_limit(self, state, on_event) -> bool:
        """达到 max_iterations 时发送 finish 事件并停止。"""
        if self._step_count < self.max_iterations:
            return False
        logger.info(f"  ⏱️ Max iterations ({self.max_iterations}) reached")
        if self.trace_logger:
            self.trace_logger.write("iteration_limit_reached", payload={
                "step": self._step_count, "max_iterations": self.max_iterations,
            })
        on_event(
            ActionEvent(
                action=FinishAction(message="达到最大迭代次数"),
                tool_name="finish",
            )
        )
        state.execution_status = ConversationExecutionStatus.FINISHED
        return True

    def _log_step_progress(self) -> None:
        """每 10 步（及第 1 步）输出进度日志。"""
        if self._step_count % 10 == 0 or self._step_count == 1:
            logger.info(f"  🔄 Step {self._step_count}")

    # -- LLM 调用 -----------------------------------------------

    def _call_llm(self, view, on_token):
        """将对话历史准备为消息列表，调用 LLM 并返回响应。"""
        messages = prepare_llm_messages(view, condenser=self.condenser)
        if self.trace_logger:
            self._trace_llm_request(messages)
        try:
            response = make_llm_completion(
                self.llm,
                messages,
                tools=list(self.tools_map.values()),
                on_token=on_token,
            )
        except Exception as exc:
            if self.trace_logger:
                self.trace_logger.write("llm_error", payload={
                    "exception": str(exc)[:2000],
                })
            raise
        if self.trace_logger:
            self._trace_llm_response(response)
        return response

    def _trace_llm_request(self, messages) -> None:
        t = self.trace_logger
        if t is None:
            return
        msg_summaries: list[dict[str, Any]] = []
        for m in messages:
            role = getattr(m, "role", "unknown")
            tool_calls = getattr(m, "tool_calls", None)
            tool_call_id = getattr(m, "tool_call_id", None)
            content_text = None
            parts = getattr(m, "content", []) or []
            for part in parts:
                if hasattr(part, "text"):
                    content_text = (content_text or "") + part.text
                elif isinstance(part, str):
                    content_text = (content_text or "") + part
                elif isinstance(part, dict) and part.get("text"):
                    content_text = (content_text or "") + part["text"]
            entry: dict[str, Any] = {"role": role}
            if content_text:
                entry["content_len"] = len(content_text)
                entry["content_preview"] = content_text[:1000]
            if tool_calls:
                entry["tool_calls"] = [self._trace_tool_call_info(tc) for tc in tool_calls]
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            msg_summaries.append(entry)
        tools_info = [{"name": t.name} for t in self.tools_map.values()]
        t.write("llm_request", payload={
            "message_count": len(msg_summaries),
            "messages": msg_summaries,
            "tools": tools_info,
            "invalid_response_count": self._invalid_response_count,
        })

    def _trace_llm_response(self, llm_response) -> None:
        t = self.trace_logger
        if t is None:
            return
        msg = llm_response.message
        response_type = classify_response(msg)
        content_text = self._extract_text_content(msg)
        tool_calls_info: list[dict[str, Any]] = []
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_calls_info = [self._trace_tool_call_info(tc) for tc in msg.tool_calls]
        usage = {}
        if hasattr(llm_response, "usage") and llm_response.usage:
            u = llm_response.usage
            usage = {
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
            }
        t.write("llm_response", payload={
            "llm_response_id": getattr(llm_response, "id", None),
            "response_type": str(response_type),
            "content_text": (content_text or "")[:500],
            "tool_calls": tool_calls_info,
            "usage": usage,
        })

    @staticmethod
    def _trace_tool_call_info(tool_call) -> dict[str, Any]:
        """兼容不同 SDK/provider tool call 结构，提取 trace 所需字段。"""
        if isinstance(tool_call, dict):
            func = tool_call.get("function") or {}
            return {
                "id": tool_call.get("id"),
                "name": tool_call.get("name") or tool_call.get("tool_name") or func.get("name"),
                "arguments": tool_call.get("arguments") or func.get("arguments"),
            }
        func = getattr(tool_call, "function", None)
        name = (
            getattr(tool_call, "name", None)
            or getattr(tool_call, "tool_name", None)
            or (getattr(func, "name", None) if func else None)
        )
        arguments = (
            getattr(tool_call, "arguments", None)
            or (getattr(func, "arguments", None) if func else None)
        )
        return {
            "id": getattr(tool_call, "id", None),
            "name": name,
            "arguments": arguments,
        }

    # -- 响应分发 -----------------------------------------------

    def _dispatch_llm_response(self, llm_response, conversation, state, on_event) -> None:
        """根据 LLM 响应类型路由到对应的处理分支。"""
        response_type = classify_response(llm_response.message)

        if response_type == LLMResponseType.TOOL_CALLS:
            self._invalid_response_count = 0
            self._handle_tool_calls(
                llm_response.message,
                llm_response,
                conversation,
                state,
                on_event,
            )
        elif response_type == LLMResponseType.CONTENT:
            content = self._extract_text_content(llm_response.message)
            if self._is_explicit_completion(content):
                self._invalid_response_count = 0
                logger.info(f"  ✅ Translation completed in {self._step_count} steps")
                state.execution_status = ConversationExecutionStatus.FINISHED
            else:
                self._handle_invalid_response(
                    state,
                    on_event,
                    reason="模型返回了普通文本，但当前 ReAct 流程需要调用工具。",
                )
        else:
            self._handle_invalid_response(
                state,
                on_event,
                reason="模型返回了空响应或无法识别的响应。",
            )

    @staticmethod
    def _extract_text_content(message) -> str:
        """尽量从 LLM message 中提取普通文本。"""
        parts = getattr(message, "content", []) or []
        texts: list[str] = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts).strip()

    @staticmethod
    def _is_explicit_completion(content: str) -> bool:
        """判断普通文本是否是明确完成信号。"""
        normalized = content.lower()
        if not normalized:
            return False
        negative_markers = (
            "do not call finish",
            "tests are still failing",
            "some tests are still failing",
            "continue fixing",
            "not complete",
        )
        if any(marker in normalized for marker in negative_markers):
            return False
        completion_markers = (
            "all tests pass",
            "all tests passed",
            "translation complete",
            "translation completed",
            "任务完成",
            "翻译完成",
        )
        return any(marker in normalized for marker in completion_markers)

    def _handle_invalid_response(self, state, on_event, reason: str) -> None:
        """处理无效响应：分级提醒，超过阈值后标记卡住。"""
        self._invalid_response_count += 1
        is_stuck = self._invalid_response_count >= self.invalid_response_limit
        if self.trace_logger:
            self.trace_logger.write("invalid_response", payload={
                "count": self._invalid_response_count,
                "limit": self.invalid_response_limit,
                "reason": reason,
                "will_mark_stuck": is_stuck,
            })
        if is_stuck:
            logger.info(
                f"  ⚠️ Invalid response limit reached "
                f"({self._invalid_response_count}/{self.invalid_response_limit})"
            )
            self._send_tool_reminder(on_event, reason, escalated=True)
            state.execution_status = ConversationExecutionStatus.STUCK
            return
        self._send_tool_reminder(on_event, reason, escalated=self._invalid_response_count > 1)

    def _send_tool_reminder(self, on_event, reason: str, escalated: bool = False) -> None:
        """LLM 返回空/无效响应时，提醒其使用可用工具。"""
        tool_names = sorted(self.tools_map)
        tool_list = ", ".join(tool_names)
        if escalated:
            text = (
                f"{reason}\n"
                f"Your next response MUST call exactly one available tool. "
                f"Valid tools: {tool_list}. Do not output a natural-language plan."
            )
        else:
            text = (
                f"{reason}\n"
                f"请使用工具执行操作，不要只输出自然语言。"
                f"可用工具包括：{tool_list}。"
            )
        on_event(
            MessageEvent(
                source="agent",
                llm_message=Message(
                    role="user",
                    content=[TextContent(text=text)],
                ),
            )
        )
