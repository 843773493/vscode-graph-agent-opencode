from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeDebugStatus = Literal["idle", "starting", "running", "paused", "exited", "failed"]
NodeDebugBreakpointRelocationStatus = Literal[
    "current",
    "relocated",
    "pending_update",
    "source_deleted",
]
NodeDebugAction = Literal[
    "continue",
    "pause",
    "step_over",
    "step_into",
    "step_out",
    "set_breakpoint",
    "update_breakpoint",
    "clear_breakpoint",
    "evaluate",
    "stop",
]


class NodeDebugBreakpointRequest(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None
    hit_condition: int | None = Field(default=None, ge=1)
    log_message: str | None = Field(default=None, min_length=1)


class NodeDebugStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    configuration_id: str | None = Field(default=None, min_length=1)
    path: str = Field(min_length=1)
    working_directory: str | None = None
    launch_profile_name: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )


class NodeDebugActionRequest(BaseModel):
    session_id: str = Field(min_length=1)
    action: NodeDebugAction
    params: dict[str, object] = Field(default_factory=dict)


class NodeDebugBreakpointDTO(BaseModel):
    breakpoint_id: str
    path: str
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None
    hit_condition: int | None = Field(default=None, ge=1)
    log_message: str | None = None
    verified: bool = False
    actual_line: int | None = Field(default=None, ge=1)
    inspector_id: str | None = None
    original_line: int | None = Field(default=None, ge=1)
    source_line: str | None = None
    previous_line: str | None = None
    next_line: str | None = None
    source_digest: str | None = None
    relocation_status: NodeDebugBreakpointRelocationStatus = "current"
    relocation_message: str | None = None
    created_at: datetime


class NodeDebugConfigurationBreakpointDTO(BaseModel):
    """可移植方案中的断点，不包含 Inspector 安装和命中状态。"""

    model_config = ConfigDict(extra="forbid")

    breakpoint_id: str
    path: str
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None
    hit_condition: int | None = Field(default=None, ge=1)
    log_message: str | None = None
    original_line: int | None = Field(default=None, ge=1)
    source_line: str | None = None
    previous_line: str | None = None
    next_line: str | None = None
    source_digest: str | None = None
    relocation_status: NodeDebugBreakpointRelocationStatus = "current"
    relocation_message: str | None = None
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
    action: str
    message: str
    actor: Literal["human", "ai", "system"] = "human"
    tool_name: str | None = None
    tool_call_id: str | None = None
    result: Literal["success", "error"] = "success"
    created_at: datetime


class NodeDebugLaunchProfileDTO(BaseModel):
    name: str
    adapter: str
    runtime: str
    supported: bool
    program: str = ""
    working_directory: str = ""
    args: list[str] = Field(default_factory=list)


class NodeDebugCapabilitiesDTO(BaseModel):
    enabled: bool
    default_adapter: str
    supported_adapters: list[str] = Field(default_factory=list)
    launch_profiles: list[NodeDebugLaunchProfileDTO] = Field(default_factory=list)


class NodeDebugConfigurationSummaryDTO(BaseModel):
    configuration_id: str
    name: str
    script_path: str | None = None
    launch_profile_name: str | None = None
    breakpoint_count: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    updated_at: datetime


class NodeDebugConfigurationDTO(BaseModel):
    """可跨会话复制的源码调试方案，不包含会话和运行时状态。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    configuration_id: str = Field(
        min_length=1,
        pattern=r"^dbgcfg_[0-9a-f]{32}$",
    )
    name: str = Field(min_length=1, max_length=80)
    revision: int = Field(default=1, ge=1)
    script_path: str | None = None
    working_directory: str = ""
    launch_profile_name: str | None = None
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugConfigurationBreakpointDTO] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class NodeDebugStateDTO(BaseModel):
    session_id: str
    status: NodeDebugStatus
    active_configuration_id: str | None = None
    active_configuration_name: str | None = None
    configurations: list[NodeDebugConfigurationSummaryDTO] = Field(default_factory=list)
    script_path: str | None = None
    working_directory: str | None = None
    launch_profile_name: str | None = None
    args: list[str] = Field(default_factory=list)
    pid: int | None = None
    paused_reason: str | None = None
    error_message: str | None = None
    call_stack: list[NodeDebugStackFrameDTO] = Field(default_factory=list)
    last_stopped_frame: NodeDebugStackFrameDTO | None = None
    breakpoints: list[NodeDebugBreakpointDTO] = Field(default_factory=list)
    output: list[str] = Field(default_factory=list)
    last_evaluation: NodeDebugEvaluationDTO | None = None
    evaluations: list[NodeDebugEvaluationDTO] = Field(default_factory=list)
    actions: list[NodeDebugActionRecordDTO] = Field(default_factory=list)
    configuration_revision: int = Field(default=0, ge=0)
    requires_restart: bool = False
    source_changed_paths: list[str] = Field(default_factory=list)


class NodeDebugSessionManifestDTO(BaseModel):
    """会话本地状态；该文件不可作为调试方案迁移。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    active_configuration_id: str | None = None
    actions: list[NodeDebugActionRecordDTO] = Field(default_factory=list)
    updated_at: datetime


class NodeDebugConfigurationCreateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=80)
    script_path: str | None = None
    working_directory: str = ""
    launch_profile_name: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )
    activate: bool = True


class NodeDebugConfigurationUpdateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=80)
    script_path: str | None = None
    working_directory: str = ""
    launch_profile_name: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )


class NodeDebugConfigurationActivateRequest(BaseModel):
    session_id: str = Field(min_length=1)


class NodeDebugConfigurationImportRequest(BaseModel):
    session_id: str = Field(min_length=1)
    configuration: NodeDebugConfigurationDTO
    activate: bool = False


class NodeDebugConfigurationCopyRequest(BaseModel):
    source_session_id: str = Field(min_length=1)
    target_session_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    activate: bool = False
