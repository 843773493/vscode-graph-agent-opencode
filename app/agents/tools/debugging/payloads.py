from __future__ import annotations

import json

from app.agents.tools.debug_redaction import (
    REDACTION_NOTICE,
    contains_redaction,
    redact_expression_value,
    redact_free_text,
)
from app.schemas.internal_v2.node_debug import NodeDebugStateDTO

_ENDING_DEBUG_TOOLS = frozenset(
    {
        "stop_debugging",
        "continue_execution",
        "step_over",
        "step_into",
        "step_out",
        "restart_debugging",
    }
)
# 结束类调试工具在进程进入 exited/failed 终态时仍需附失效断点清单。
_FINAL_DEBUG_RUNTIME_STATUSES = frozenset({"exited", "failed"})


def _include_invalid_breakpoints(
    tool_name: str,
    state: NodeDebugStateDTO | None,
) -> bool:
    """start_debugging 始终附失效断点清单；其余结束类工具仅在进程已到 exited/failed 时附带。"""
    if tool_name == "start_debugging":
        return True
    return (
        tool_name in _ENDING_DEBUG_TOOLS
        and state is not None
        and state.status in _FINAL_DEBUG_RUNTIME_STATUSES
    )


def _json_result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _state_payload(state: NodeDebugStateDTO | None) -> dict[str, object] | None:
    if state is None:
        return None
    payload = state.model_dump(mode="json")
    # 这些字段只用于后端把协议请求路由到 Inspector；模型既不需要读取，
    # 也不能把它们作为后续源码调试动作的输入。
    payload.pop("session_id", None)
    payload.pop("thread_id", None)
    payload.pop("pid", None)
    call_stack = payload.get("call_stack")
    if isinstance(call_stack, list):
        for frame in call_stack:
            if isinstance(frame, dict):
                frame.pop("call_frame_id", None)
                # 变量值只能通过 get_variables_values 显式读取，避免任何控制类
                # 工具的通用 state 快照绕过最小权限约束。
                frame["variables"] = []
    last_stopped_frame = payload.get("last_stopped_frame")
    if isinstance(last_stopped_frame, dict):
        last_stopped_frame.pop("call_frame_id", None)
        last_stopped_frame["variables"] = []
    breakpoints = payload.get("breakpoints")
    if isinstance(breakpoints, list):
        for breakpoint in breakpoints:
            if isinstance(breakpoint, dict):
                breakpoint.pop("breakpoint_id", None)
                breakpoint.pop("inspector_id", None)
    actions = payload.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                action.pop("action_id", None)
                action.pop("session_id", None)
                action.pop("tool_call_id", None)
    output = payload.get("output")
    if isinstance(output, list):
        payload["output"] = [
            redact_free_text(item)[0] if isinstance(item, str) else item
            for item in output
        ]
    evaluation = payload.get("last_evaluation")
    if isinstance(evaluation, dict):
        expression = evaluation.get("expression")
        if isinstance(expression, str):
            for field_name in ("value", "description"):
                value = evaluation.get(field_name)
                if value is not None:
                    evaluation[field_name] = redact_expression_value(
                        expression,
                        value,
                    )[0]
    evaluations = payload.get("evaluations")
    if isinstance(evaluations, list):
        for history_evaluation in evaluations:
            if not isinstance(history_evaluation, dict):
                continue
            expression = history_evaluation.get("expression")
            if not isinstance(expression, str):
                continue
            for field_name in ("value", "description"):
                value = history_evaluation.get(field_name)
                if value is not None:
                    history_evaluation[field_name] = redact_expression_value(
                        expression,
                        value,
                    )[0]
    return payload


def _invalid_breakpoint_payload(
    state_payload: dict[str, object] | None,
) -> list[dict[str, object]]:
    if state_payload is None:
        return []
    breakpoints = state_payload.get("breakpoints")
    if not isinstance(breakpoints, list):
        return []
    invalid: list[dict[str, object]] = []
    for breakpoint in breakpoints:
        if not isinstance(breakpoint, dict):
            continue
        status = breakpoint.get("relocation_status")
        if status == "current":
            continue
        invalid.append(
            {
                "path": breakpoint.get("path"),
                "line": breakpoint.get("line"),
                "column": breakpoint.get("column"),
                "original_line": breakpoint.get("original_line"),
                "relocation_status": status,
                "relocation_message": breakpoint.get("relocation_message"),
            }
        )
    return invalid


def _success(
    message: str,
    state: NodeDebugStateDTO | None,
    *,
    include_invalid_breakpoints: bool = False,
) -> str:
    state_payload = _state_payload(state)
    payload: dict[str, object] = {
        "ok": True,
        "message": message,
        "state": state_payload,
    }
    if include_invalid_breakpoints:
        payload["invalid_breakpoints"] = _invalid_breakpoint_payload(state_payload)
    if contains_redaction(payload):
        payload["redaction_notice"] = REDACTION_NOTICE
    return _json_result(payload)


def _failure(
    code: str,
    message: str,
    state: NodeDebugStateDTO | None = None,
    *,
    include_invalid_breakpoints: bool = False,
    error_details: dict[str, object] | None = None,
) -> str:
    state_payload = _state_payload(state)
    error_payload: dict[str, object] = {"code": code, "message": message}
    if error_details:
        # 结构化失败必须携带定位字段（如冲突字段、期望值），模型才能自我修正。
        error_payload.update(error_details)
    payload: dict[str, object] = {
        "ok": False,
        "error": error_payload,
        "state": state_payload,
    }
    if include_invalid_breakpoints:
        payload["invalid_breakpoints"] = _invalid_breakpoint_payload(state_payload)
    if contains_redaction(payload):
        payload["redaction_notice"] = REDACTION_NOTICE
    return _json_result(payload)
