from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import AgentState, PrivateStateAttr
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

MAX_INVALID_TOOL_CALL_RETRIES = 5


class StructuredToolCallState(AgentState):
    _invalid_tool_call_retry_count: Annotated[NotRequired[int], PrivateStateAttr]


@dataclass(frozen=True, slots=True)
class InvalidStructuredToolCall:
    name: str
    call_id: str
    error: str


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    dumped = model_dump()
    return dumped if isinstance(dumped, Mapping) else None


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许常量 {value}")


def _invalid_structured_tool_calls(
    message: AIMessage,
) -> list[InvalidStructuredToolCall]:
    invalid_by_id: dict[str, InvalidStructuredToolCall] = {}
    seen_raw_call_ids: set[str] = set()
    raw_tool_calls = message.additional_kwargs.get("tool_calls")
    if raw_tool_calls is not None:
        if isinstance(raw_tool_calls, (str, bytes)) or not isinstance(
            raw_tool_calls, Sequence
        ):
            raise RuntimeError(
                "结构化工具调用列表格式无效：tool_calls 必须为数组，"
                f"实际类型为 {type(raw_tool_calls).__name__}"
            )
        for index, raw_call in enumerate(raw_tool_calls):
            call = _as_mapping(raw_call)
            if call is None:
                raise RuntimeError(
                    "结构化工具调用格式无效："
                    f"tool_calls[{index}] 必须为 object，"
                    f"实际类型为 {type(raw_call).__name__}"
                )
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError(
                    "结构化工具调用缺少 tool_call_id："
                    f"tool_calls[{index}]"
                )
            if call_id in seen_raw_call_ids:
                raise RuntimeError(
                    "结构化工具调用包含重复 tool_call_id："
                    f"tool_call_id={call_id}"
                )
            seen_raw_call_ids.add(call_id)
            function = _as_mapping(call.get("function"))
            if function is None:
                raise RuntimeError(
                    "结构化工具调用缺少 function object："
                    f"tool_call_id={call_id}"
                )
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    "结构化工具调用缺少工具名称："
                    f"tool_call_id={call_id}"
                )
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                raise RuntimeError(
                    "结构化工具调用参数格式无效："
                    "arguments 应为 JSON object 字符串，"
                    f"实际类型为 {type(arguments).__name__}，"
                    f"tool_call_id={call_id}"
                )
            try:
                parsed_arguments = json.loads(
                    arguments,
                    parse_constant=_reject_nonstandard_json_constant,
                )
            except json.JSONDecodeError as exc:
                error = f"arguments 不是合法 JSON：{exc.msg}（位置 {exc.pos}）"
            except (RecursionError, ValueError) as exc:
                error = f"arguments 不是合法 JSON：{exc}"
            else:
                if isinstance(parsed_arguments, dict):
                    continue
                error = (
                    "arguments 解析结果应为 JSON object，"
                    f"实际类型为 {type(parsed_arguments).__name__}"
                )
            invalid_by_id[call_id] = InvalidStructuredToolCall(name, call_id, error)

    for invalid_call in message.invalid_tool_calls:
        call_id = invalid_call.get("id")
        name = invalid_call.get("name")
        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError("无效结构化工具调用缺少 tool_call_id")
        if call_id in invalid_by_id:
            continue
        if not isinstance(name, str) or not name:
            raise RuntimeError(
                f"无效结构化工具调用缺少工具名称: tool_call_id={call_id}"
            )
        raw_error = invalid_call.get("error")
        error = raw_error if isinstance(raw_error, str) and raw_error else "arguments 不是合法 JSON object"
        invalid_by_id[call_id] = InvalidStructuredToolCall(name, call_id, error)

    return list(invalid_by_id.values())


class StructuredToolCallMiddleware(AgentMiddleware):
    """将损坏的结构化工具参数作为错误结果返回模型重新生成。"""

    state_schema = StructuredToolCallState

    def before_agent(
        self,
        state: StructuredToolCallState,
        runtime: Runtime,
    ) -> dict[str, Any]:
        return {"_invalid_tool_call_retry_count": 0}

    @hook_config(can_jump_to=["model"])
    def after_model(
        self,
        state: StructuredToolCallState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None

        message = messages[-1]
        invalid_calls = _invalid_structured_tool_calls(message)
        retry_count = state.get("_invalid_tool_call_retry_count", 0)
        if not invalid_calls:
            if retry_count:
                return {"_invalid_tool_call_retry_count": 0}
            return None
        if retry_count >= MAX_INVALID_TOOL_CALL_RETRIES:
            names = ", ".join(call.name for call in invalid_calls)
            raise RuntimeError(
                "模型连续生成无效结构化工具参数，已达到重试上限："
                f"tools={names}, retries={MAX_INVALID_TOOL_CALL_RETRIES}"
            )

        invalid_ids = {call.call_id for call in invalid_calls}
        error_messages = [
            ToolMessage(
                content=(
                    f'ERROR: Your input to the tool "{call.name}" was invalid '
                    f"({call.error}).\nPlease check your input and try again."
                ),
                name=call.name,
                tool_call_id=call.call_id,
                status="error",
            )
            for call in invalid_calls
        ]
        update: dict[str, Any] = {
            "messages": error_messages,
            "_invalid_tool_call_retry_count": retry_count + 1,
        }
        has_valid_pending_call = any(
            call.get("id") not in invalid_ids for call in message.tool_calls
        )
        if not has_valid_pending_call:
            update["jump_to"] = "model"
        return update
