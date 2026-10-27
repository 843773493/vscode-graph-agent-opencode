from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, NotRequired, cast

# TODO: LangChain 尚未从 middleware 顶层导出 HITL TypedDict；升级后改用公开导出。
from langchain.agents.middleware.human_in_the_loop import (
    HITLRequest,
    HITLResponse,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.core.model_delta_context import get_current_model_delta_sink
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)


class ToolApprovalState(AgentState):
    tool_approval_edits: Annotated[NotRequired[dict[str, ToolCall]], PrivateStateAttr]


def approved_tool_call(state: object, original: ToolCall) -> ToolCall:
    """执行与输出归档共用本批审批编辑，身份绑定仍核对原模型调用。"""
    if not isinstance(state, Mapping):
        return original
    edits = cast(dict[str, ToolCall], state.get("tool_approval_edits", {}))
    if not edits:
        return original
    call_id = original["id"]
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("工具审批编辑缺少原调用身份")
    revised = edits.get(call_id, original)
    if revised["id"] != original["id"]:
        raise ValueError("工具审批编辑不能改变原调用身份")
    return revised


async def complete_tool_approval_results(state: Mapping[str, object], messages: Sequence[ToolMessage]) -> None:
    """无 SDK 调用的审批结果沿原 canonical writer 结算，不制造工具回调。"""
    sink = get_current_model_delta_sink()
    if sink is None:
        return
    assistant = next(message for message in reversed(cast(Sequence[BaseMessage], state["messages"])) if isinstance(message, AIMessage))
    model_call_id = LangChainMessageCodec().model_call_id(assistant)
    if model_call_id is None:
        raise RuntimeError("工具审批结果缺少原 emitting ModelCall identity")
    for message in messages:
        await sink.complete_tool_approval(message, model_call_id=model_call_id)


class ToolConfirmationMiddleware(HumanInTheLoopMiddleware):
    """直接工具与扩展目标共用官方审批协议，模型原始调用始终不可变。"""

    state_schema = ToolApprovalState

    def __init__(
        self, confirmation_tool_names: frozenset[str], *,
        interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    ) -> None:
        super().__init__(interrupt_on or {}, description_prefix="模型请求执行需要人工确认的工具。")
        self._confirmation_tool_names = confirmation_tool_names

    @staticmethod
    def _target(tool_call: ToolCall) -> ToolCall:
        if tool_call["name"] != EXTENSION_TOOL_INVOKER_NAME:
            return tool_call
        tool_name = tool_call["args"].get("tool_name")
        arguments = tool_call["args"].get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return tool_call
        return ToolCall(name=tool_name, args=arguments, id=tool_call["id"], type="tool_call")

    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, object]:
        assistant = next((message for message in reversed(state["messages"]) if isinstance(message, AIMessage)), None)
        # 每批模型输出都清空旧编辑，Provider 复用 call ID 时不得借用前批决定。
        result: dict[str, object] = {"tool_approval_edits": {}}
        if assistant is None:
            return result
        requested = []
        actions = []
        reviews = []
        for call in assistant.tool_calls:
            target = self._target(call)
            config = self.interrupt_on.get(call["name"])
            if config is not None:
                target = call
            elif target["name"] in self._confirmation_tool_names:
                config = InterruptOnConfig(allowed_decisions=["approve", "edit", "reject", "respond"])
            else:
                continue
            # TODO: 上游公开请求构造／决定处理端口后，移除这两个私有方法调用。
            action, review = self._create_action_and_config(target, config, state, runtime)
            requested.append((call, target, config))
            actions.append(action)
            reviews.append(review)
        if not requested:
            return result
        response = cast(HITLResponse, interrupt(HITLRequest(action_requests=actions, review_configs=reviews)))
        if len(response["decisions"]) != len(requested):
            raise ValueError("工具审批决定数量与原调用数量不一致")
        edits: dict[str, ToolCall] = {}
        messages: list[ToolMessage] = []
        for (original, target, config), decision in zip(requested, response["decisions"], strict=True):
            revised, message = self._process_decision(decision, target, config)
            call_id = original["id"]
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("工具审批缺少稳定 tool_call_id")
            if decision["type"] == "edit":
                if revised is None:
                    raise RuntimeError("编辑决定缺少工具调用")
                edits[call_id] = (
                    ToolCall(name=EXTENSION_TOOL_INVOKER_NAME,
                             args={"tool_name": revised["name"], "arguments": revised["args"]},
                             id=call_id, type="tool_call")
                    if original["name"] == EXTENSION_TOOL_INVOKER_NAME and target is not original
                    else revised
                )
            if message is not None:
                messages.append(message.model_copy(update={"name": original["name"]}))
        result["tool_approval_edits"] = edits
        if messages:
            result["messages"] = messages
        return result

    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict[str, object]:
        result = self.after_model(state, runtime)
        if "messages" in result:
            await complete_tool_approval_results(state, cast(list[ToolMessage], result["messages"]))
        return result
