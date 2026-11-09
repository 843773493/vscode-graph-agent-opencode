from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer

from app.core.resource_uri import parse_resource_uri
from app.schemas.canonical_params import CanonicalThreadId, CanonicalWorkspaceId

NodeDebugStatus = Literal[
    "idle",
    "starting",
    "running",
    "paused",
    "stopping",
    "exited",
    "failed",
    "execution_lost",
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
    thread_id: CanonicalThreadId | None = None

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
    thread_id: CanonicalThreadId | None = None
    configuration_id: str | None = Field(default=None, min_length=1)
    path: str = Field(min_length=1)
    working_directory: str | None = None
    launch_profile_name: str | None = Field(default=None, min_length=1)
    args: list[str] = Field(default_factory=list)
    breakpoints: list[NodeDebugBreakpointRequest] = Field(
        default_factory=list,
        max_length=50,
    )
    debugger_registration_id: str | None = Field(default=None, min_length=1)
    defer_configuration_done: bool = False


def _portable_source_path(value: str) -> str:
    # 可移植方案保留相对源码位置；运行态展示字段不复用此约束。
    if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).root
            or value.lstrip().lower().startswith(("file:/", "file:\\"))):
        raise ValueError("可移植调试方案必须使用相对源码路径")
    return value


_PortableSourcePath = Annotated[
    str, AfterValidator(_portable_source_path), PlainSerializer(_portable_source_path),
]


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
    original_line: int | None = Field(default=None, ge=1)
    source_line: str | None = None
    previous_line: str | None = None
    next_line: str | None = None
    source_digest: str | None = None
    relocation_status: NodeDebugBreakpointRelocationStatus = "current"
    relocation_message: str | None = None
    created_at: datetime


class NodeDebugConfigurationBreakpointInput(BaseModel):
    """待目标 owner 解析的方案断点输入，允许原生路径。"""

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


class NodeDebugConfigurationBreakpointDTO(NodeDebugConfigurationBreakpointInput):
    """可移植方案中的断点，不包含 Inspector 安装和命中状态。"""

    path: _PortableSourcePath


class NodeDebugVariableDTO(BaseModel):
    name: str
    value: str
    type: str | None = None
    object_id: str | None = None
    scope: Literal["local", "global"] = "local"


def _inspector_source_url(value: str) -> str:
    # 导航 URL 的文件目标必须是 URI，不能让展示路径成为第二定位链。
    _portable_source_path(value)
    if value.lstrip().lower().startswith("file:"):
        raise ValueError("Inspector 文件源码必须先转换为资源 URI")
    if value.lstrip().lower().startswith("boxteam:"):
        parsed = parse_resource_uri(value)
        if parsed.kind != "files" or parsed.scope not in {"workspace", "gateway"}:
            raise ValueError("Inspector 源码地址必须使用工作区或 Gateway 文件 URI")
        return parsed.display_uri
    # 内置模块、匿名源码和非文件 URL 沿用 Inspector 标签，不伪造文件资源。
    return value


_InspectorSourceURL = Annotated[
    str, AfterValidator(_inspector_source_url), PlainSerializer(_inspector_source_url),
]


class NodeDebugStackFrameDTO(BaseModel):
    call_frame_id: str
    function_name: str
    url: _InspectorSourceURL
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
    thread_id: CanonicalThreadId
    action: str
    message: str
    actor: Literal["human", "ai", "system"] = "human"
    tool_name: str | None = None
    tool_call_id: str | None = None
    extension_catalog_binding: ExtensionCatalogBindingAuditDTO | None = None
    result: Literal["success", "error"] = "success"
    created_at: datetime


def _launch_profile_uri(value: str) -> str:
    # 空值表示由方案选择；公开能力不承载内部配置的原生路径输入。
    if not value:
        return value
    parsed = parse_resource_uri(value)
    if parsed.scope != "workspace" or parsed.kind != "files":
        raise ValueError("调试启动能力必须引用工作区文件 URI")
    return parsed.display_uri


_LaunchProfileURI = Annotated[
    str, AfterValidator(_launch_profile_uri), PlainSerializer(_launch_profile_uri),
]


class NodeDebugLaunchProfileDTO(BaseModel):
    name: str
    adapter: str
    runtime: str
    supported: bool
    program: _LaunchProfileURI = ""
    working_directory: _LaunchProfileURI = ""
    args: list[str] = Field(default_factory=list)


class NodeDebugCapabilitiesDTO(BaseModel):
    enabled: bool
    default_adapter: str
    supported_adapters: list[str] = Field(default_factory=list)
    launch_profiles: list[NodeDebugLaunchProfileDTO] = Field(default_factory=list)


class NodeDebugDebuggerContributionDTO(BaseModel):
    """一次已就绪 Debug 插件声明的冻结目标快照。"""

    registration_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    label: str = Field(min_length=1)
    configuration_schema: dict[str, object]
    service_id: str = Field(min_length=1)
    dap_version: int = Field(gt=0)
    capabilities: list[str] = Field(default_factory=list)
    gateway_id: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId
    package_id: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    package_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    instance_id: str = Field(min_length=1)
    contract_version: int = Field(gt=0)
    declaration_revision: str = Field(min_length=1)


class NodeDebugDebuggerContributionsDTO(BaseModel):
    """当前明确 workspace 的 ready Debug 声明集合。"""

    gateway_id: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId
    debuggers: list[NodeDebugDebuggerContributionDTO] = Field(default_factory=list)


class NodeDebugBindingSnapshotDTO(BaseModel):
    """一次运行绑定的完整目标身份，不从之后的焦点或配置推断。"""

    registration_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    gateway_id: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId
    package_id: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    package_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    instance_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    contract_version: int = Field(gt=0)
    declaration_revision: str = Field(min_length=1)
    dap_version: int = Field(gt=0)


class NodeDebugConfigurationSummaryDTO(BaseModel):
    configuration_id: str
    name: str
    script_path: str | None = None
    launch_profile_name: str | None = None
    breakpoint_count: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    updated_at: datetime


_ConfigurationBreakpoint = TypeVar("_ConfigurationBreakpoint", bound=NodeDebugConfigurationBreakpointInput)


class _NodeDebugConfigurationFields(BaseModel, Generic[_ConfigurationBreakpoint]):
    """输入与已解析方案共用字段，路径约束由各自业务阶段声明。"""

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
    breakpoints: list[_ConfigurationBreakpoint] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class NodeDebugConfigurationInput(_NodeDebugConfigurationFields[NodeDebugConfigurationBreakpointInput]):
    """目标 owner 解析前的可移植方案输入，保留原生路径语义。"""


class NodeDebugConfigurationDTO(_NodeDebugConfigurationFields[NodeDebugConfigurationBreakpointDTO]):
    """可跨会话复制的源码调试方案，不包含会话和运行时状态。"""

    script_path: _PortableSourcePath | None = None
    working_directory: _PortableSourcePath = ""


class NodeDebugStateDTO(BaseModel):
    session_id: str
    #: 运行状态必须显式携带实际 SessionThread 归属，不提供隐式默认值。
    thread_id: CanonicalThreadId
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
    run_id: str | None = None
    debugger_binding: NodeDebugBindingSnapshotDTO | None = None


class NodeDebugSessionManifestDTO(BaseModel):
    """会话本地状态；该文件不可作为调试方案迁移。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    #: manifest 必须显式记录实际 SessionThread owner，不提供隐式默认值。
    thread_id: CanonicalThreadId
    active_configuration_id: str | None = None
    # 方案文件由 owner manifest 显式登记。fork source capture 只读取这份清单，
    # 不得通过扫描 configurations/ 目录猜测未登记文件。
    configuration_ids: tuple[str, ...] = Field(default_factory=tuple)
    actions: list[NodeDebugActionRecordDTO] = Field(default_factory=list)
    updated_at: datetime


NodeDebugLaunchPhase = Literal[
    "launch_pending",
    "running",
    "stopping",
    "execution_lost",
    "settled",
]


class NodeDebugLaunchClaimDTO(BaseModel):
    """Thread 当前 Node Debug run 的冻结启动记录，由原 Node Debug owner 持有。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    thread_id: CanonicalThreadId
    run_id: str = Field(min_length=1)
    process_instance_id: str = Field(min_length=1)
    phase: NodeDebugLaunchPhase
    configuration_id: str = Field(min_length=1)
    debugger_binding: NodeDebugBindingSnapshotDTO
    script_path: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    node_executable: str = Field(min_length=1)
    working_directory: str = ""
    launch_profile_name: str | None = None
    breakpoints: list[NodeDebugBreakpointDTO] = Field(default_factory=list)
    pid: int | None = Field(default=None, ge=1)
    reconcile_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class NodeDebugConfigurationCreateRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: CanonicalThreadId | None = None
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
    thread_id: CanonicalThreadId | None = None
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
    thread_id: CanonicalThreadId | None = None


class NodeDebugConfigurationImportRequest(BaseModel):
    session_id: str = Field(min_length=1)
    thread_id: CanonicalThreadId | None = None
    configuration: NodeDebugConfigurationInput
    activate: bool = False


class NodeDebugConfigurationCopyRequest(BaseModel):
    source_session_id: str = Field(min_length=1)
    source_thread_id: CanonicalThreadId | None = None
    target_session_id: str = Field(min_length=1)
    target_thread_id: CanonicalThreadId | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    activate: bool = False
