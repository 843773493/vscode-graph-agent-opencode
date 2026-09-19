from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

NodeDebugStatus = Literal[
    "idle", "starting", "running", "paused", "stopping", "exited", "failed",
    "reconcile_required",
]
NodeDebugAction = Literal[
    "continue",
    "pause",
    "step_over",
    "step_into",
    "step_out",
    "set_breakpoint",
    "clear_breakpoint",
    "evaluate",
    "stop",
]


class NodeDebugBreakpointRequest(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None


class NodeDebugStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(default="main", min_length=1)
    path: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )


class NodeDebugActionRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(default="main", min_length=1)
    action: NodeDebugAction
    params: dict[str, object] = Field(default_factory=dict)


class NodeDebugBreakpointDTO(BaseModel):
    breakpoint_id: str
    path: str
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None
    log_message: str | None = None
    verified: bool = False
    actual_line: int | None = Field(default=None, ge=1)
    inspector_id: str | None = None
    created_at: datetime


class NodeDebugVariableDTO(BaseModel):
    name: str
    value: str
    type: str | None = None
    object_id: str | None = None
    scope: Literal["local", "global"] = "local"


class NodeDebugStackFrameDTO(BaseModel):
    call_frame_id: str
    function_name: str
    url: str
    path: str | None = None
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    scope_names: list[str] = Field(default_factory=list)
    variables: list[NodeDebugVariableDTO] = Field(default_factory=list)


class NodeDebugEvaluationDTO(BaseModel):
    expression: str
    value: str | None = None
    type: str | None = None
    description: str | None = None
    error: str | None = None
    evaluated_at: datetime


class NodeDebugActionRecordDTO(BaseModel):
    action_id: str
    session_id: str
    thread_id: str = Field(default="main", min_length=1)
    action: str
    message: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    result: Literal["success", "error"] = "success"
    created_at: datetime


class NodeDebugStateDTO(BaseModel):
    session_id: str
    thread_id: str = Field(default="main", min_length=1)
    status: NodeDebugStatus
    script_path: str | None = None
    args: list[str] = Field(default_factory=list)
    pid: int | None = None
    inspector_url: str | None = None
    paused_reason: str | None = None
    error_message: str | None = None
    call_stack: list[NodeDebugStackFrameDTO] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointDTO] = Field(default_factory=list)
    output: list[str] = Field(default_factory=list)
    last_evaluation: NodeDebugEvaluationDTO | None = None
    actions: list[NodeDebugActionRecordDTO] = Field(default_factory=list)
