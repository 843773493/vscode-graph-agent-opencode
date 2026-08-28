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
from app.core.model_delta_context import get_current_model_delta_sink
from app.services.orchestration.activity_runtime import ActivityRuntime

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

    @staticmethod
    def _current_activity_runtime() -> ActivityRuntime | None:
        sink = get_current_model_delta_sink()
        activity_runtime = getattr(sink, "activities", None)
        return (
            activity_runtime
            if isinstance(activity_runtime, ActivityRuntime)
            else None
        )

    @staticmethod
    def _approval_activity_id(
        activity_runtime: ActivityRuntime,
        requested: list[tuple[int, ToolCall, str]],
    ) -> str:
        call_ids = ":".join(str(tool_call["id"]) for _, tool_call, _ in requested)
        return f"{activity_runtime.writer.turn_stream_id}:approval:{call_ids}"

    def _requested_approvals(
        self,
        state: AgentState,
    ) -> tuple[
        list[tuple[int, ToolCall, str]],
        list[ActionRequest],
        list[ReviewConfig],
    ] | None:
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
        return requested, action_requests, review_configs

    def _apply_approval_response(
        self,
        state: AgentState,
        requested: list[tuple[int, ToolCall, str]],
        response: HITLResponse,
    ) -> dict[str, object]:
        decisions = response["decisions"]
        if len(decisions) != len(requested):
            raise ValueError(
                "扩展工具人工确认决定数量与待确认工具数量不一致: "
                f"decisions={len(decisions)}, requests={len(requested)}"
            )
        messages = state["messages"]
        last_ai_message = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        if last_ai_message is None:
            raise RuntimeError("审批返回后找不到原始 AIMessage")
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

    def after_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        prepared = self._requested_approvals(state)
        if prepared is None:
            return None
        requested, action_requests, review_configs = prepared
        response = cast(
            HITLResponse,
            interrupt(
                HITLRequest(
                    action_requests=action_requests,
                    review_configs=review_configs,
                )
            ),
        )
        return self._apply_approval_response(state, requested, response)

    async def aafter_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        prepared = self._requested_approvals(state)
        if prepared is None:
            return None
        requested, action_requests, review_configs = prepared
        activity_runtime = self._current_activity_runtime()
        activity_id: str | None = None
        if activity_runtime is not None:
            activity_id = self._approval_activity_id(activity_runtime, requested)
            await activity_runtime.started(
                activity_id=activity_id,
                kind="approval.wait",
                summary="等待用户确认工具操作",
                cancellable=True,
                resumable=True,
                side_effect_policy="none",
                detail={
                    "approval_id": activity_id,
                    "required_action": "approve_or_reject",
                },
            )
            await activity_runtime.updated(
                activity_id=activity_id,
                kind="approval.wait",
                status="waiting",
                detail={
                    "approval_id": activity_id,
                    "required_action": "approve_or_reject",
                },
            )
        response = cast(
            HITLResponse,
            interrupt(
                HITLRequest(
                    action_requests=action_requests,
                    review_configs=review_configs,
                )
            ),
        )
        if activity_runtime is not None and activity_id is not None:
            await activity_runtime.completed(
                activity_id=activity_id,
                kind="approval.wait",
                summary="用户已提交工具审批决定",
            )
        return self._apply_approval_response(state, requested, response)
