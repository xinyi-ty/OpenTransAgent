"""ReActTranslationAgent — 继承 Agent 重写 step() 实现 ReAct 翻译决策循环。"""

from __future__ import annotations

from openhands.sdk import LLM, Agent
from openhands.sdk.agent.response_dispatch import classify_response, LLMResponseType
from openhands.sdk.agent.utils import make_llm_completion, prepare_llm_messages
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent, MessageEvent
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.tool.builtins.finish import FinishAction

from utils.logger import logger
from .prompts import build_react_prompt
from tools.registry import create_tools


class ReActTranslationAgent(Agent):
    """基于 ReAct 范式的翻译 Agent。

    可控性：
    - max_iterations 控制循环次数
    - reflection_enabled 控制反思纠错
    - prompts 中可切换 prompt 策略
    """

    max_iterations: int = 500
    _step_count: int = 0
    reflection_enabled: bool = True
    workspace_root: str = "."
    project_name: str = ""
    source_language: str = ""
    target_language: str = ""
    project_tree: str = ""
    translation_order: list[str] | None = None

    @classmethod
    def create(cls, llm: LLM, workspace_root: str = ".", max_iterations: int = 20,
               project_name: str = "", source_language: str = "", target_language: str = "",
               project_tree: str = "", translation_order: list[str] | None = None,
               layer_ctrl=None,
               **kwargs) -> "ReActTranslationAgent":
        # 注册所有工具（layer_ctrl 通过模块级变量传递，避免 Pydantic 序列化）
        import tools.registry  # noqa: F401
        from tools.file_ops import set_layer_ctrl
        set_layer_ctrl(layer_ctrl)
        tools_list = create_tools(
            workspace_root=workspace_root, command_timeout=60, search_max_results=10,
        )
        # 构建自定义系统提示，通过 system_prompt 字段传入让父类处理 SystemPromptEvent
        layers = layer_ctrl.layers if layer_ctrl and layer_ctrl.active else None
        system_prompt = build_react_prompt(
            source_language=source_language, target_language=target_language,
            project_name=project_name, project_tree=project_tree,
            translation_order=translation_order,
            layers=layers, current_layer=0)
        return cls(llm=llm, tools=tools_list, max_iterations=max_iterations,
                   workspace_root=workspace_root, project_name=project_name,
                   source_language=source_language, target_language=target_language,
                   project_tree=project_tree, translation_order=translation_order,
                   system_prompt=system_prompt, condenser=None, **kwargs)

    def step(self, conversation, on_event, on_token=None):
        state = conversation.state
        if self._step_count >= self.max_iterations:
            logger.info(f"  ⏱️ Max iterations ({self.max_iterations}) reached")
            on_event(ActionEvent(action=FinishAction(message="达到最大迭代次数"), tool_name="finish"))
            state.execution_status = ConversationExecutionStatus.FINISHED
            return
        self._step_count += 1
        if self._step_count % 10 == 0 or self._step_count == 1:
            logger.info(f"  🔄 Step {self._step_count}")
        _messages = prepare_llm_messages(state.view, condenser=self.condenser)
        llm_response = make_llm_completion(self.llm, _messages, tools=list(self.tools_map.values()), on_token=on_token)
        response_type = classify_response(llm_response.message)
        if response_type == LLMResponseType.TOOL_CALLS:
            self._handle_tool_calls(llm_response.message, llm_response, conversation, state, on_event)
        elif response_type == LLMResponseType.CONTENT:
            logger.info(f"  ✅ Translation completed in {self._step_count} steps")
            state.execution_status = ConversationExecutionStatus.FINISHED
        else:
            on_event(MessageEvent(source="agent", llm_message=Message(role="user", content=[TextContent(text="请使用工具执行操作，不要输出空响应。")])))
