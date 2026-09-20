from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeDebugStatus = Literal[
    "idle",
    "starting",
    "running",
    "paused",
    "stopping",
    "exited",
    "failed",
    # 任务 3.2/3.5 状态机：停止/重启无法核实旧实例终态时保持该可观察状态，绝不虚报
    # 终态。生产者与结清条件见 node_debug_service 的 claim 恢复路径。
    "reconcile_required",
]
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


class NodeDebugNoActionParams(BaseModel):
    """无需额外参数的 Node Debug 动作参数。"""

    model_config = ConfigDict(extra="forbid")


class NodeDebugSetBreakpointParams(BaseModel):
    """设置源码断点的参数。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    column: int = Field(default=1, ge=1)
    condition: str | None = None
    hit_condition: int | None = Field(default=None, ge=1)
    log_message: str | None = Field(default=None, min_length=1)


class NodeDebugUpdateBreakpointParams(BaseModel):
    """编辑源码断点的参数；未提供的字段保持原值。"""

    model_config = ConfigDict(extra="forbid")

    breakpoint_id: str = Field(min_length=1)
    path: str | None = Field(default=None, min_length=1)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    condition: str | None = None
    hit_condition: int | None = Field(default=None, ge=1)
    log_message: str | None = Field(default=None, min_length=1)


class NodeDebugClearBreakpointParams(BaseModel):
    """清除源码断点的参数。"""

    model_config = ConfigDict(extra="forbid")

    breakpoint_id: str = Field(min_length=1)


class NodeDebugEvaluateParams(BaseModel):
    """暂停上下文求值的参数。"""

    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1)
    call_frame_id: str | None = Field(default=None, min_length=1)


class _NodeDebugActionRequestBase(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


class NodeDebugControlActionRequest(_NodeDebugActionRequestBase):
    action: Literal[
        "continue",
        "pause",
        "step_over",
        "step_into",
        "step_out",
        "stop",
    ]
    params: NodeDebugNoActionParams = Field(default_factory=NodeDebugNoActionParams)


class NodeDebugSetBreakpointActionRequest(_NodeDebugActionRequestBase):
    action: Literal["set_breakpoint"]
    params: NodeDebugSetBreakpointParams


class NodeDebugUpdateBreakpointActionRequest(_NodeDebugActionRequestBase):
    action: Literal["update_breakpoint"]
    params: NodeDebugUpdateBreakpointParams


class NodeDebugClearBreakpointActionRequest(_NodeDebugActionRequestBase):
    action: Literal["clear_breakpoint"]
    params: NodeDebugClearBreakpointParams


class NodeDebugEvaluateActionRequest(_NodeDebugActionRequestBase):
    action: Literal["evaluate"]
    params: NodeDebugEvaluateParams


NodeDebugActionRequest = Annotated[
    NodeDebugControlActionRequest
    | NodeDebugSetBreakpointActionRequest
    | NodeDebugUpdateBreakpointActionRequest
    | NodeDebugClearBreakpointActionRequest
    | NodeDebugEvaluateActionRequest,
    Field(discriminator="action"),
]


class NodeDebugBreakpointRequest(NodeDebugSetBreakpointParams):
    """启动配置中的源码断点参数。"""


class NodeDebugStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    configuration_id: str | None = Field(default=None, min_length=1)
    path: str = Field(min_length=1)
    working_directory: str | None = None
    launch_profile_name: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )


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


class ExtensionCatalogBindingAuditDTO(BaseModel):
    """产生 Agent 调试动作的 sealed 扩展目录 binding 身份。"""

    binding_id: str = Field(min_length=1)
    binding_hash: str = Field(pattern=r"^sha256:")
    catalog_revision: str = Field(pattern=r"^sha256:")
    generation: int = Field(ge=1)
    provider_binding_identity: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_schema_hash: str = Field(pattern=r"^sha256:")


class NodeDebugActionRecordDTO(BaseModel):
    action_id: str
    session_id: str
    #: 动作审计必须显式携带实际 SessionThread 归属，不提供隐式默认值。
    thread_id: str = Field(min_length=1)
    action: str
    message: str
    actor: Literal["human", "ai", "system"] = "human"
    tool_name: str | None = None
    tool_call_id: str | None = None
    extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None
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
    #: 运行状态必须显式携带实际 SessionThread 归属，不提供隐式默认值。
    thread_id: str = Field(min_length=1)
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
    #: manifest 必须显式记录实际 SessionThread owner，不提供隐式默认值。
    thread_id: str = Field(min_length=1)
    active_configuration_id: str | None = None
    # 方案文件由 owner manifest 显式登记。fork source capture 只读取这份清单，
    # 不得通过扫描 configurations/ 目录猜测未登记文件。
    configuration_ids: tuple[str, ...] = Field(default_factory=tuple)
    actions: list[NodeDebugActionRecordDTO] = Field(default_factory=list)
    updated_at: datetime


NodeDebugLaunchPhase = Literal[
    "launch_pending",
    "spawned",
    "running",
    "stopping",
    "reconcile_required",
    "settled",
]


class NodeDebugLaunchClaimDTO(BaseModel):
    """thread 级 durable 启动登记；跨 Turn 保留，用于崩溃后的实例定点恢复。

    门控语义：
    - ``launch_pending``：已在 spawn 前登记 nonce，但尚未 spawn（无 PID），
      崩溃后无法证明进程不存在，必须保持 ``reconcile_required``。
    - ``spawned``：spawn 成功并记录了 OS 起始身份，用于崩溃后定点恢复；此时
      PID/端口还不是权威运行属性。
    - ``running``：OS 起始身份核对 + Inspector 握手都成功后才进入，PID/端口
      作为可验证属性登记。
    - ``stopping`` / ``settled``：停止中/已核实终结并结清。
    - ``reconcile_required``：无法核实旧实例，必须阻断新启动直到人工/自动核实。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    thread_id: str = Field(min_length=1)
    process_instance_id: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    phase: NodeDebugLaunchPhase
    configuration_id: str = Field(min_length=1)
    pid: int | None = Field(default=None, ge=1)
    process_identity_source: str | None = None
    process_start_marker: str | None = None
    inspector_host: str = ""
    inspector_port: int = Field(default=0, ge=0)
    reconcile_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class NodeDebugConfigurationCreateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
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
    thread_id: str = Field(min_length=1)
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
    thread_id: str = Field(min_length=1)


class NodeDebugConfigurationImportRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    configuration: NodeDebugConfigurationDTO
    activate: bool = False


class NodeDebugConfigurationCopyRequest(BaseModel):
    source_session_id: str = Field(min_length=1)
    source_thread_id: str = Field(min_length=1)
    target_session_id: str = Field(min_length=1)
    target_thread_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    activate: bool = False
