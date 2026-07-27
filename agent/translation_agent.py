"""ReActTranslationAgent — 继承 Agent 重写 step() 实现 ReAct 翻译决策循环。"""

from __future__ import annotations

from typing import Any

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
    - max_iterations（create 参数）控制 ReAct 循环步数上限
    - reflection_enabled  控制反思纠错开关
    """

    # ── 运行配置（通过 create() 或构造函数注入） ────────────────
    max_iterations: int = 500  # class-level 兜底，父类 Agent 不一定从 kwargs 设实例属性
    reflection_enabled: bool = True
    workspace_root: str = "."
    project_name: str = ""
    source_language: str = ""
    target_language: str = ""
    project_tree: str = ""
    translation_order: list[str] | None = None

    # ── 内部状态 ────────────────────────────────────────────────
    _step_count: int = 0

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
        )
        tools_list = create_tools(
            workspace_root=workspace_root,
            command_timeout=60,
            search_max_results=10,
        )
        return cls(
            llm=llm,
            tools=tools_list,
            max_iterations=max_iterations,
            workspace_root=workspace_root,
            project_name=project_name,
            source_language=source_language,
            target_language=target_language,
            project_tree=project_tree,
            translation_order=translation_order,
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
           - CONTENT    → 标记任务完成
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
        )

    # -- 迭代控制 -----------------------------------------------

    def _check_iteration_limit(self, state, on_event) -> bool:
        """达到 max_iterations 时发送 finish 事件并停止。"""
        if self._step_count < self.max_iterations:
            return False
        logger.info(f"  ⏱️ Max iterations ({self.max_iterations}) reached")
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
        return make_llm_completion(
            self.llm,
            messages,
            tools=list(self.tools_map.values()),
            on_token=on_token,
        )

    # -- 响应分发 -----------------------------------------------

    def _dispatch_llm_response(self, llm_response, conversation, state, on_event) -> None:
        """根据 LLM 响应类型路由到对应的处理分支。"""
        response_type = classify_response(llm_response.message)

        if response_type == LLMResponseType.TOOL_CALLS:
            self._handle_tool_calls(
                llm_response.message,
                llm_response,
                conversation,
                state,
                on_event,
            )
        elif response_type == LLMResponseType.CONTENT:
            logger.info(f"  ✅ Translation completed in {self._step_count} steps")
            state.execution_status = ConversationExecutionStatus.FINISHED
        else:
            self._send_tool_reminder(on_event)

    @staticmethod
    def _send_tool_reminder(on_event) -> None:
        """LLM 返回空/无效响应时，提醒其使用可用工具。"""
        on_event(
            MessageEvent(
                source="agent",
                llm_message=Message(
                    role="user",
                    content=[TextContent(text="请使用工具执行操作，不要输出空响应。")],
                ),
            )
        )
