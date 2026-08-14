from __future__ import annotations

from typing import cast

# TODO: LangChain 尚未从 middleware 顶层导出 HITL TypedDict；升级后改用公开导出。
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    Decision,
    HITLRequest,
    HITLResponse,
    ReviewConfig,
)
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME

_ALLOWED_DECISIONS = ["approve", "edit", "reject", "respond"]


class CustomToolConfirmationMiddleware(AgentMiddleware):
    """按 invoke_custom_tool 内部目标名触发现有 HITL 确认协议。"""

    def __init__(self, confirmation_tool_names: frozenset[str]) -> None:
        super().__init__()
        self._confirmation_tool_names = confirmation_tool_names

    @staticmethod
    def _target(tool_call: ToolCall) -> tuple[str, dict[str, object]] | None:
        if tool_call["name"] != CUSTOM_TOOL_INVOKER_NAME:
            return None
        tool_name = tool_call["args"].get("tool_name")
        arguments = tool_call["args"].get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return None
        if not all(isinstance(key, str) for key in arguments):
            return None
        return tool_name, cast(dict[str, object], arguments)

    @staticmethod
    def _process_decision(
        decision: Decision,
        tool_call: ToolCall,
        target_name: str,
    ) -> tuple[ToolCall | None, ToolMessage | None]:
        decision_type = decision["type"]
        if decision_type == "approve":
            return tool_call, None
        if decision_type == "edit":
            edited_action = decision["edited_action"]
            return (
                ToolCall(
                    type="tool_call",
                    name=CUSTOM_TOOL_INVOKER_NAME,
                    args={
                        "tool_name": edited_action["name"],
                        "arguments": edited_action["args"],
                    },
                    id=tool_call["id"],
                ),
                None,
            )
        if decision_type == "reject":
            content = decision.get("message") or (
                f"用户拒绝执行调试扩展工具 {target_name}。"
            )
            return (
                tool_call,
                ToolMessage(
                    content=content,
                    name=CUSTOM_TOOL_INVOKER_NAME,
                    tool_call_id=tool_call["id"],
                    status="error",
                ),
            )
        if decision_type == "respond":
            return (
                tool_call,
                ToolMessage(
                    content=decision["message"],
                    name=CUSTOM_TOOL_INVOKER_NAME,
                    tool_call_id=tool_call["id"],
                    status="success",
                ),
            )
        raise ValueError(f"不支持的扩展工具人工确认决定: {decision_type}")

    def after_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        messages = state["messages"]
        if not messages:
            return None
        last_ai_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if last_ai_message is None or not last_ai_message.tool_calls:
            return None

        requested: list[tuple[int, ToolCall, str]] = []
        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        for index, tool_call in enumerate(last_ai_message.tool_calls):
            target = self._target(tool_call)
            if target is None:
                continue
            target_name, target_arguments = target
            if target_name not in self._confirmation_tool_names:
                continue
            requested.append((index, tool_call, target_name))
            action_requests.append(
                ActionRequest(
                    name=target_name,
                    args=target_arguments,
                    description=(
                        "模型请求执行需要人工确认的扩展工具。\n\n"
                        f"Tool: {target_name}\nArgs: {target_arguments}"
                    ),
                )
            )
            review_configs.append(
                ReviewConfig(
                    action_name=target_name,
                    allowed_decisions=list(_ALLOWED_DECISIONS),
                )
            )
        if not requested:
            return None

        response = cast(
            HITLResponse,
            interrupt(
                HITLRequest(
                    action_requests=action_requests,
                    review_configs=review_configs,
                )
            ),
        )
        decisions = response["decisions"]
        if len(decisions) != len(requested):
            raise ValueError(
                "扩展工具人工确认决定数量与待确认工具数量不一致: "
                f"decisions={len(decisions)}, requests={len(requested)}"
            )

        requested_by_index = {
            index: (tool_call, target_name, decisions[decision_index])
            for decision_index, (index, tool_call, target_name) in enumerate(requested)
        }
        revised_tool_calls: list[ToolCall] = []
        artificial_messages: list[ToolMessage] = []
        for index, tool_call in enumerate(last_ai_message.tool_calls):
            pending = requested_by_index.get(index)
            if pending is None:
                revised_tool_calls.append(tool_call)
                continue
            original_call, target_name, decision = pending
            revised_call, tool_message = self._process_decision(
                decision,
                original_call,
                target_name,
            )
            if revised_call is not None:
                revised_tool_calls.append(revised_call)
            if tool_message is not None:
                artificial_messages.append(tool_message)

        last_ai_message.tool_calls = revised_tool_calls
        return {"messages": [last_ai_message, *artificial_messages]}

    async def aafter_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        return self.after_model(state, runtime)
