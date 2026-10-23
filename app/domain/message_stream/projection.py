"""消息流确定性投影：提交与 replay 共用，不拥有锁、持久化或缓存。"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.message_stream.errors import MessageStreamError

MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS = 256 * 1024
MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS = 64 * 1024
MESSAGE_STREAM_TEXT_TRUNCATION_MARKER = (
    "\n\n…消息流展示已截断；Turn 完成后可从权威历史详情读取持久化内容…\n\n"
)


def _bounded_stream_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = MESSAGE_STREAM_TEXT_TRUNCATION_MARKER
    retained_chars = max_chars - len(marker)
    head_chars = retained_chars // 2
    tail_chars = retained_chars - head_chars
    return f"{value[:head_chars]}{marker}{value[-tail_chars:]}"


def _clone_json_tree(value: Any) -> Any:
    """复制 JSON 状态树，保留提交与 replay 之间的快照隔离。"""
    if type(value) is dict:
        return {key: _clone_json_tree(item) for key, item in value.items()}
    if type(value) is list:
        return [_clone_json_tree(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return copy.deepcopy(value)


def validate_event_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """拒绝已下线的内部字段，禁止绕过公共协议改写消息流状态。

    auto_closed_blocks 曾在提交 stream.completed 时承载存储层的收口记账；
    现在 running block 必须在同一把 Turn 锁内由规范 block.completed 公共事件
    闭合（见 _close_running_blocks_locked）。手工提交该字段必须响亮失败，
    而不是被静默吸收后又让公共投影报 ParseError。
    """
    if event_type == "stream.completed" and "auto_closed_blocks" in payload:
        raise MessageStreamError(
            "stream.completed 不再接受已下线的 auto_closed_blocks 字段: "
            "running block 必须由规范 block.completed 事件闭合"
        )


def apply_event(
    state: dict[str, Any],
    event: Mapping[str, Any],
) -> dict[str, Any]:
    event_type = str(event["type"])
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise MessageStreamError(
            f"消息流事件 payload 必须是对象: type={event_type}"
        )
    validate_event_payload(event_type, payload)
    snapshot = (
        payload.get("snapshot", payload)
        if event_type == "stream.snapshot"
        else None
    )
    source_state = snapshot if isinstance(snapshot, Mapping) else state
    next_state = _clone_json_tree(dict(source_state))
    if event_type == "stream.opened":
        next_state["stream_status"] = "open"
        next_state["active_state"] = None
    elif event_type == "model.started":
        next_state["current_model_call_id"] = payload.get("model_call_id")
        next_state["current_attempt"] = int(payload.get("attempt") or 0)
        next_state["agent_loop_status"] = "model_running"
        _upsert_model_call(next_state, payload, status="running")
        _set_active_state(
            next_state,
            {
                "kind": "model_output",
                "phase": "reasoning",
                "entity_id": str(payload.get("model_call_id") or ""),
                "status": "running",
            },
        )
    elif event_type in {"model.completed", "model.failed"}:
        next_state["agent_loop_status"] = "validating"
        _upsert_model_call(
            next_state,
            payload,
            status="completed" if event_type == "model.completed" else "failed",
        )
        if event_type == "model.failed":
            next_state["failure"] = dict(payload)
    elif event_type == "model.retrying":
        next_state["agent_loop_status"] = "retrying"
        current_model_call_id = next_state.get("current_model_call_id")
        for block in next_state.get("blocks", []):
            if (
                isinstance(block, dict)
                and block.get("model_call_id") == current_model_call_id
            ):
                block["projection"] = "intermediate"
    elif event_type == "block.started":
        _upsert_block(
            next_state,
            payload,
            status="running",
            model_call_id=event.get("model_call_id"),
        )
        carrier_type = str(payload.get("carrier_type") or "text")
        phase = "reasoning" if carrier_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        } else "text"
        _set_active_state(
            next_state,
            {
                "kind": "model_output",
                "phase": phase,
                "entity_id": str(payload.get("block_id") or ""),
                "block_id": payload.get("block_id"),
                "carrier_type": carrier_type,
                "status": "running",
            },
        )
    elif event_type == "block.delta":
        _apply_block_delta(
            next_state,
            payload,
            model_call_id=event.get("model_call_id"),
        )
    elif event_type == "block.completed":
        block = _find_block(next_state, payload.get("block_id"))
        if block is not None:
            block["status"] = str(payload.get("status") or "completed")
            block["completion_reason"] = str(
                payload.get("completion_reason") or "upstream_completed"
            )
            block["partial"] = bool(payload.get("partial", False))
    elif event_type in {"tool_call", "tool_call.delta"}:
        _upsert_tool_call(next_state, payload)
        _set_active_state(
            next_state,
            {
                "kind": "tool_call",
                "phase": "accumulating",
                "entity_id": str(payload.get("tool_call_id") or ""),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_invocation_id": payload.get("tool_invocation_id"),
                "tool_attempt_id": payload.get("tool_attempt_id"),
                "status": str(payload.get("status") or "running"),
            },
        )
    elif event_type == "tool_call.completed":
        _upsert_tool_call(next_state, payload)
        call = _find_tool_call(next_state, payload.get("tool_call_id"))
        if call is not None:
            call["status"] = str(payload.get("status") or "incomplete")
            call["completion_reason"] = str(
                payload.get("completion_reason") or "execution_lost"
            )
        _set_active_state(
            next_state,
            {
                "kind": "tool_call",
                "phase": "stopping",
                "entity_id": str(payload.get("tool_call_id") or ""),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_invocation_id": payload.get("tool_invocation_id"),
                "tool_attempt_id": payload.get("tool_attempt_id"),
                "status": str(payload.get("status") or "incomplete"),
            },
        )
    elif event_type == "tool.started":
        _upsert_tool(next_state, payload, status="running")
        next_state["agent_loop_status"] = "tool_running"
        _set_active_state(
            next_state,
            {
                "kind": "tool_execution",
                "phase": "running",
                "entity_id": str(payload.get("tool_execution_id") or ""),
                "tool_execution_id": payload.get("tool_execution_id"),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_invocation_id": payload.get("tool_invocation_id"),
                "tool_attempt_id": payload.get("tool_attempt_id"),
                "status": "running",
            },
        )
    elif event_type == "tool.completed":
        tool_status = str(payload.get("status") or "completed")
        if tool_status == "outcome_unknown":
            tool_status = "completed"
        _upsert_tool(
            next_state,
            payload,
            status=tool_status,
        )
        _set_active_state(
            next_state,
            {
                "kind": "tool_execution",
                "phase": "stopping",
                "entity_id": str(payload.get("tool_execution_id") or ""),
                "tool_execution_id": payload.get("tool_execution_id"),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_invocation_id": payload.get("tool_invocation_id"),
                "tool_attempt_id": payload.get("tool_attempt_id"),
                "status": tool_status,
            },
        )
    elif event_type.startswith("activity."):
        _apply_activity_event(next_state, event_type, payload)
    elif event_type == "interrupt.requested":
        next_state["interrupt_state"] = {
            "request_id": payload.get("interrupt_request_id"),
            "status": "requested",
            "reason": payload.get("reason"),
        }
        next_state["stream_status"] = "interrupting"
        previous = next_state.get("active_state")
        next_state["active_state"] = {
            "kind": "interrupting",
            "phase": "stopping",
            "entity_id": str(payload.get("interrupt_request_id") or ""),
            "status": "stopping",
            "last_kind": previous.get("kind") if isinstance(previous, Mapping) else None,
            "last_phase": previous.get("phase") if isinstance(previous, Mapping) else None,
            "reason": payload.get("reason"),
        }
    elif event_type == "interrupt.rejected":
        if next_state.get("stream_status") != "interrupting":
            next_state["interrupt_state"] = {
                "request_id": payload.get("interrupt_request_id"),
                "status": "rejected",
                "reason": payload.get("reason"),
            }
    elif event_type == "stream.completed":
        next_state["stream_status"] = "completed"
        next_state["agent_loop_status"] = "completed"
        next_state["resumable"] = False
        _set_terminal_active_state(next_state, "completed", payload)
    elif event_type == "stream.interrupted":
        next_state["stream_status"] = "interrupted"
        next_state["agent_loop_status"] = "interrupted"
        next_state["resumable"] = False
        _finish_running_model_calls(next_state, outcome="user_interrupt")
        _finish_running_blocks(next_state, status="interrupted")
        _finish_running_tool_calls(next_state, reason="user_interrupt")
        _mark_running_tools_unknown(next_state)
        _finish_running_activities(
            next_state,
            terminal_status="completed",
            outcome="user_interrupt",
            completion_reason="user_interrupt",
        )
        _set_terminal_active_state(next_state, "interrupted", payload)
        if next_state.get("interrupt_state") is None:
            next_state["interrupt_state"] = {
                "request_id": payload.get("interrupt_request_id"),
                "status": "confirmed",
            }
        else:
            next_state["interrupt_state"]["status"] = "confirmed"
    elif event_type == "stream.failed":
        next_state["stream_status"] = "failed"
        next_state["agent_loop_status"] = "failed"
        next_state["failure"] = _clone_json_tree(dict(payload))
        next_state["resumable"] = bool(payload.get("resumable", False))
        _finish_running_model_calls(next_state, outcome="execution_lost")
        _finish_running_blocks(next_state, status="failed")
        _finish_running_tool_calls(next_state, reason="execution_lost")
        _mark_running_tools_unknown(next_state)
        _finish_running_activities(
            next_state,
            terminal_status="failed",
            outcome="execution_lost",
            completion_reason="execution_lost",
        )
        next_state["recovery"] = {
            "status": "execution_lost" if payload.get("code") == "execution_lost" else "failed",
            "code": payload.get("code"),
            "message": payload.get("message"),
            "resumable": bool(payload.get("resumable", False)),
        }
        _set_terminal_active_state(next_state, "failed", payload)
    _mark_event_lifecycle(next_state, event)
    return next_state


def _upsert_tool_call(
    state: dict[str, Any],
    payload: Mapping[str, Any],
) -> None:
    tool_call_id = payload.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise MessageStreamError("tool_call 事件缺少 tool_call_id")
    tool_calls = state.setdefault("tool_calls", [])
    for tool_call in tool_calls:
        if (
            isinstance(tool_call, dict)
            and tool_call.get("tool_call_id") == tool_call_id
        ):
            incoming = dict(payload)
            existing_name = tool_call.get("tool_name")
            if (
                isinstance(existing_name, str)
                and existing_name
                and not incoming.get("tool_name")
            ):
                incoming.pop("tool_name", None)
            existing_arguments = tool_call.get("arguments")
            if (
                isinstance(existing_arguments, dict)
                and existing_arguments
                and incoming.get("arguments") in ({}, "", None)
            ):
                incoming.pop("arguments", None)
            tool_call.update(incoming)
            return
    tool_calls.append(_clone_json_tree(dict(payload)))


def _find_tool_call(
    state: dict[str, Any],
    tool_call_id: object,
) -> dict[str, Any] | None:
    if not isinstance(tool_call_id, str):
        return None
    for tool_call in state.get("tool_calls", []):
        if isinstance(tool_call, dict) and tool_call.get("tool_call_id") == tool_call_id:
            return tool_call
    return None


def _upsert_model_call(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    status: str,
) -> None:
    model_call_id = payload.get("model_call_id")
    if not isinstance(model_call_id, str) or not model_call_id:
        return
    calls = state.setdefault("model_calls", [])
    for call in calls:
        if isinstance(call, dict) and call.get("model_call_id") == model_call_id:
            call.update(dict(payload))
            call["status"] = status
            return
    calls.append({**dict(payload), "status": status})


def _set_active_state(state: dict[str, Any], active_state: Mapping[str, Any]) -> None:
    state["active_state"] = {
        key: value for key, value in dict(active_state).items() if value is not None
    }


def _event_timestamp(event: Mapping[str, Any]) -> str | None:
    emitted_at = event.get("emitted_at")
    if isinstance(emitted_at, str) and emitted_at:
        return emitted_at
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            return updated_at
    return None


def _mark_entity_lifecycle(
    entity: dict[str, Any],
    event: Mapping[str, Any],
    *,
    completed: bool = False,
) -> None:
    event_seq = event.get("event_seq")
    if isinstance(event_seq, bool) or not isinstance(event_seq, int):
        raise MessageStreamError("消息流实体生命周期缺少整数 event_seq")
    event_timestamp = _event_timestamp(event)
    started_seq = entity.get("started_seq")
    if isinstance(started_seq, bool) or not isinstance(started_seq, int) or started_seq <= 0:
        entity["started_seq"] = event_seq
        if event_timestamp is not None:
            entity["started_at"] = event_timestamp
    last_event_seq = entity.get("last_event_seq")
    if (
        isinstance(last_event_seq, bool)
        or not isinstance(last_event_seq, int)
        or event_seq >= last_event_seq
    ):
        entity["last_event_seq"] = event_seq
        if event_timestamp is not None:
            entity["updated_at"] = event_timestamp
    if not completed:
        return
    completed_seq = entity.get("completed_seq")
    if (
        isinstance(completed_seq, bool)
        or not isinstance(completed_seq, int)
        or event_seq >= completed_seq
    ):
        entity["completed_seq"] = event_seq
        if event_timestamp is not None:
            entity["completed_at"] = event_timestamp


def _find_model_call(
    state: dict[str, Any],
    model_call_id: object,
) -> dict[str, Any] | None:
    if not isinstance(model_call_id, str) or not model_call_id:
        return None
    for model_call in state.get("model_calls", []):
        if (
            isinstance(model_call, dict)
            and model_call.get("model_call_id") == model_call_id
        ):
            return model_call
    return None


def _mark_event_lifecycle(
    state: dict[str, Any],
    event: Mapping[str, Any],
) -> None:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return
    completed = event_type in {
        "model.completed",
        "model.failed",
        "block.completed",
        "tool_call.completed",
        "tool.completed",
        "activity.completed",
        "activity.failed",
    }
    if event_type in {"model.started", "model.completed", "model.failed"}:
        model_call = _find_model_call(
            state,
            payload.get("model_call_id") or event.get("model_call_id"),
        )
        if model_call is not None:
            _mark_entity_lifecycle(model_call, event, completed=completed)
    elif event_type in {"block.started", "block.delta", "block.completed"}:
        block = _find_block(state, payload.get("block_id") or event.get("block_id"))
        if block is not None:
            _mark_entity_lifecycle(block, event, completed=completed)
    elif event_type in {"tool_call", "tool_call.delta", "tool_call.completed"}:
        tool_call_id = payload.get("tool_call_id") or event.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            for tool_call in state.get("tool_calls", []):
                if (
                    isinstance(tool_call, dict)
                    and tool_call.get("tool_call_id") == tool_call_id
                ):
                    _mark_entity_lifecycle(tool_call, event, completed=completed)
                    break
    elif event_type in {"tool.started", "tool.completed"}:
        tool_execution_id = payload.get("tool_execution_id") or event.get(
            "tool_execution_id"
        )
        if isinstance(tool_execution_id, str) and tool_execution_id:
            for execution in state.get("tool_executions", []):
                if (
                    isinstance(execution, dict)
                    and execution.get("tool_execution_id") == tool_execution_id
                ):
                    _mark_entity_lifecycle(execution, event, completed=completed)
                    break
    elif event_type.startswith("activity."):
        activity_id = payload.get("activity_id")
        if isinstance(activity_id, str) and activity_id:
            for activity in state.get("activities", []):
                if (
                    isinstance(activity, dict)
                    and activity.get("activity_id") == activity_id
                ):
                    _mark_entity_lifecycle(activity, event, completed=completed)
                    break
    elif event_type in {"stream.interrupted", "stream.failed"}:
        for model_call in state.get("model_calls", []):
            if isinstance(model_call, dict) and "completed_seq" not in model_call:
                _mark_entity_lifecycle(model_call, event, completed=True)
        for block in state.get("blocks", []):
            if isinstance(block, dict) and "completed_seq" not in block:
                _mark_entity_lifecycle(block, event, completed=True)
        for tool_call in state.get("tool_calls", []):
            if isinstance(tool_call, dict) and "completed_seq" not in tool_call:
                _mark_entity_lifecycle(tool_call, event, completed=True)
        for execution in state.get("tool_executions", []):
            if isinstance(execution, dict) and "completed_seq" not in execution:
                _mark_entity_lifecycle(execution, event, completed=True)
        for activity in state.get("activities", []):
            if isinstance(activity, dict) and "completed_seq" not in activity:
                _mark_entity_lifecycle(activity, event, completed=True)


def backfill_lifecycle_metadata(
    state: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
    """从已有事件日志为旧 checkpoint 补齐实体生命周期序号。"""
    for event in events:
        _mark_event_lifecycle(state, event)


def _set_terminal_active_state(
    state: dict[str, Any],
    status: str,
    payload: Mapping[str, Any],
) -> None:
    previous = state.get("active_state")
    _set_active_state(
        state,
        {
            "kind": "terminal",
            "phase": status,
            "entity_id": str(state.get("turn_stream_id") or ""),
            "status": status,
            "last_kind": previous.get("kind") if isinstance(previous, Mapping) else None,
            "last_phase": previous.get("phase") if isinstance(previous, Mapping) else None,
            "reason": payload.get("completion_reason") or payload.get("code") or status,
        },
    )


def _apply_activity_event(
    state: dict[str, Any],
    event_type: str,
    payload: Mapping[str, Any],
) -> None:
    activity_id = payload.get("activity_id")
    if not isinstance(activity_id, str) or not activity_id:
        raise MessageStreamError(f"Activity 事件缺少 activity_id: type={event_type}")
    activities = state.setdefault("activities", [])
    activity = next(
        (
            item
            for item in activities
            if isinstance(item, dict) and item.get("activity_id") == activity_id
        ),
        None,
    )
    if activity is None:
        activity = {"activity_id": activity_id}
        activities.append(activity)
    activity.update(dict(payload))
    if event_type == "activity.started":
        activity["status"] = "running"
    elif event_type == "activity.updated":
        activity["status"] = str(payload.get("status") or activity.get("status") or "running")
    elif event_type == "activity.completed":
        activity["status"] = "completed"
    elif event_type == "activity.failed":
        activity["status"] = str(payload.get("status") or "failed")
    _set_active_state(
        state,
        {
            "kind": "activity",
            "phase": activity["status"],
            "entity_id": activity_id,
            "activity_id": activity_id,
            "activity_kind": activity.get("kind"),
            "status": activity["status"],
            "detail_ref": activity.get("detail_ref"),
        },
    )


def _mark_running_tools_unknown(state: dict[str, Any]) -> None:
    for execution in state.get("tool_executions", []):
        if (
            isinstance(execution, dict)
            and execution.get("status") == "running"
        ):
            execution["status"] = "completed"
            execution["outcome"] = "outcome_unknown"
            execution["completion_reason"] = "execution_lost"


def _finish_running_activities(
    state: dict[str, Any],
    *,
    terminal_status: str,
    outcome: str,
    completion_reason: str,
) -> None:
    """终态收敛 Activity，避免 snapshot 留下不可解释的运行态。"""
    for activity in state.get("activities", []):
        if not isinstance(activity, dict):
            continue
        if activity.get("status") not in {"running", "waiting", "stopping"}:
            continue
        resolved_status = terminal_status
        resolved_outcome = outcome
        if (
            outcome == "user_interrupt"
            and activity.get("side_effect_policy")
            not in {"none", "read_only"}
        ):
            resolved_status = "failed"
            resolved_outcome = "outcome_unknown"
        activity["status"] = resolved_status
        activity["outcome"] = resolved_outcome
        activity["completion_reason"] = completion_reason
        if resolved_status == "failed":
            activity["resumable"] = False


def _finish_running_blocks(state: dict[str, Any], *, status: str) -> None:
    for block in state.get("blocks", []):
        if isinstance(block, dict) and block.get("status") == "running":
            block["status"] = status
            block["completion_reason"] = (
                "user_interrupt" if status == "interrupted" else "execution_lost"
            )
            block["partial"] = True


def _finish_running_model_calls(
    state: dict[str, Any],
    *,
    outcome: str,
) -> None:
    """终态收敛仍未闭合的 model_call，避免快照留下不可解释的运行态。

    与前端 ``finishRunningModelCalls`` 保持一致：终态流里任何仍为 running 的
    model_call 都必须显式改写为 failed，并带上 outcome/retryable/完成原因，
    否则 SSE 事件重放与快照 hydrate 会得到不同的 model_call 状态。
    """
    for model_call in state.get("model_calls", []):
        if not isinstance(model_call, dict):
            continue
        if model_call.get("status") != "running":
            continue
        model_call["status"] = "failed"
        model_call["outcome"] = outcome
        model_call["retryable"] = False
        model_call["completion_reason"] = outcome


def _finish_running_tool_calls(state: dict[str, Any], *, reason: str) -> None:
    for tool_call in state.get("tool_calls", []):
        if not isinstance(tool_call, dict):
            continue
        if tool_call.get("status") not in {"accumulating", "streaming", "running"}:
            continue
        arguments_complete = tool_call.get("arguments_complete") is True
        tool_call["status"] = (
            "cancelled"
            if reason == "user_interrupt" and arguments_complete
            else "incomplete"
        )
        tool_call["completion_reason"] = reason


def _find_block(
    state: dict[str, Any],
    block_id: object,
) -> dict[str, Any] | None:
    if not isinstance(block_id, str):
        return None
    for block in state.get("blocks", []):
        if isinstance(block, dict) and block.get("block_id") == block_id:
            return block
    return None


def _upsert_block(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    status: str,
    model_call_id: object = None,
) -> dict[str, Any]:
    block_id = payload.get("block_id")
    if not isinstance(block_id, str) or not block_id:
        raise MessageStreamError("block 事件缺少 block_id")
    block = _find_block(state, block_id)
    if block is None:
        block = {
            "block_id": block_id,
            "block_index": int(payload.get("block_index") or 0),
            "carrier_type": str(payload.get("carrier_type") or "text"),
            "status": status,
            "text": "",
            "items": [],
            "redacted": bool(payload.get("redacted", False)),
            "projection": str(payload.get("projection") or "streaming"),
        }
        if isinstance(model_call_id, str) and model_call_id:
            block["model_call_id"] = model_call_id
        state.setdefault("blocks", []).append(block)
    else:
        block["status"] = status
    return block


def _apply_block_delta(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    model_call_id: object = None,
) -> None:
    block = _upsert_block(
        state,
        payload,
        status="running",
        model_call_id=model_call_id,
    )
    operation = str(payload.get("operation") or "append")
    text = payload.get("text")
    if operation == "append" and isinstance(text, str):
        block["text"] = _bounded_stream_text(
            str(block.get("text") or "") + text,
            MESSAGE_STREAM_BLOCK_TEXT_MAX_CHARS,
        )
    if operation in {"item_upsert", "item_patch"}:
        item = payload.get("item")
        if not isinstance(item, Mapping):
            raise MessageStreamError("结构化 block.delta 缺少 item")
        items = block.setdefault("items", [])
        item_id = item.get("id")
        found = False
        if isinstance(item_id, str):
            for index, existing in enumerate(items):
                if isinstance(existing, dict) and existing.get("id") == item_id:
                    if operation == "item_patch":
                        items[index] = {**existing, **dict(item)}
                    else:
                        items[index] = _clone_json_tree(dict(item))
                    found = True
                    break
        if not found:
            items.append(_clone_json_tree(dict(item)))
    if bool(payload.get("redacted", False)):
        block["redacted"] = True


def _upsert_tool(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    execution_id = payload.get("tool_execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise MessageStreamError("工具事件缺少 tool_execution_id")
    bounded_payload = dict(payload)
    for field_name in ("result", "error"):
        field_value = bounded_payload.get(field_name)
        if isinstance(field_value, str):
            bounded_payload[field_name] = _bounded_stream_text(
                field_value,
                MESSAGE_STREAM_TOOL_TEXT_MAX_CHARS,
            )
    executions = state.setdefault("tool_executions", [])
    for execution in executions:
        if (
            isinstance(execution, dict)
            and execution.get("tool_execution_id") == execution_id
        ):
            execution.update(bounded_payload)
            execution["status"] = status
            return execution
    execution = {**bounded_payload, "status": status}
    executions.append(execution)
    return execution
