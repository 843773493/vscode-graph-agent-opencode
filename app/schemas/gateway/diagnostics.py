from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.gateway.service_plugin import PluginInstanceView
from app.schemas.gateway.workspaces import GatewayConnectionKind, GatewayWorkspaceStatus
from app.schemas.runtime_logging import RuntimeLoggingService
from app.schemas.runtime_observation import RuntimeObservationStatus

GatewayDiagnosticStatus = Literal["ready", "degraded", "offline"]


GatewayDiagnosticLogStatus = Literal["available", "empty", "unavailable"]


GatewayDiagnosticLogSource = Literal["gateway", "workspace"]


class GatewayHealthDTO(BaseModel):
    gateway_id: str
    status: Literal["ok"] = "ok"
    active_workspace: GatewaySearchWorkspaceOwnerDTO | None = None
    process_id: int = Field(gt=0)
    development_restart_available: bool = False


class GatewayDiagnosticLogDTO(BaseModel):
    log_id: str
    source: GatewayDiagnosticLogSource
    workspace_id: str | None = None
    workspace_name: str | None = None
    service: str
    label: str
    status: GatewayDiagnosticLogStatus
    tail: str = ""
    truncated: bool = False
    line_count: int = Field(default=0, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    updated_at: str | None = None
    error: str | None = None


class GatewayDiagnosticWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    connection_kind: GatewayConnectionKind
    status: GatewayWorkspaceStatus
    managed: bool
    system_default: bool
    connection_error: str | None = None


class GatewayDiagnosticLogSinkDTO(BaseModel):
    log_id: str | None = None
    owner_id: str
    role: str
    source: str
    device: str
    inode: str


class GatewayDiagnosticLoggingStateDTO(BaseModel):
    service: RuntimeLoggingService
    pid: int = Field(gt=0)
    runtime_instance_id: str
    applied_revision: str | None
    level: str
    pretty: bool
    log_sink: GatewayDiagnosticLogSinkDTO | None


class GatewayDiagnosticProcessDTO(BaseModel):
    service: RuntimeLoggingService
    workspace_ids: list[str] = Field(default_factory=list)
    state: GatewayDiagnosticLoggingStateDTO | None = None
    output_mode: Literal["file", "stdout-only", "unknown"]
    error: str | None = None


class PluginRuntimeObservationDTO(BaseModel):
    instance: PluginInstanceView
    observation: RuntimeObservationStatus | None = None
    error: str | None = None


class GatewayDiagnosticsDTO(BaseModel):
    gateway_id: str
    gateway_name: str
    gateway_connection_id: str | None = None
    connection_kind: GatewayConnectionKind
    status: GatewayDiagnosticStatus
    checked_at: str
    selected_workspace_id: str | None = None
    selected_log_id: str | None = None
    workspaces: list[GatewayDiagnosticWorkspaceDTO] = Field(default_factory=list)
    logs: list[GatewayDiagnosticLogDTO] = Field(default_factory=list)
    processes: list[GatewayDiagnosticProcessDTO] = Field(default_factory=list)
    observation: RuntimeObservationStatus | None = None
    plugin_observations: list[PluginRuntimeObservationDTO] = Field(default_factory=list)


class GatewayDiagnosticLogQueryScopeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: GatewayDiagnosticLogSource
    workspace_ids: tuple[CanonicalWorkspaceId, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_source_scope(self) -> Self:
        if (self.source == "workspace") != bool(self.workspace_ids):
            raise ValueError("工作区日志需要明确工作区集合；Gateway 自有日志不接受工作区集合")
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("运行日志范围不能重复包含工作区")
        return self


class GatewayDiagnosticLogQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: GatewayDiagnosticLogQueryScopeDTO
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=50, ge=0, le=100)
    cursor: str | None = Field(default=None, max_length=32768)
    deadline_at: FiniteFloat | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("运行日志查询只接受单行字面量")
        return value


class GatewayDiagnosticLogQuerySourceDTO(BaseModel):
    log_id: str
    source: GatewayDiagnosticLogSource
    service: str
    label: str
    workspace_ids: list[CanonicalWorkspaceId]
    status: GatewayDiagnosticLogStatus
    error: str | None = None


class GatewayDiagnosticLogReferenceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: CanonicalGatewayId
    log_id: str = Field(min_length=1, max_length=512)
    device: str = Field(min_length=1, max_length=64)
    inode: str = Field(min_length=1, max_length=64)
    line_start: int = Field(ge=0)
    line_end: int = Field(gt=0)
    content_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_codepoint: int = Field(ge=0)
    end_codepoint: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.line_end <= self.line_start or self.end_codepoint <= self.start_codepoint:
            raise ValueError("运行日志行及正文位置必须是非空半开区间")
        return self


class GatewayDiagnosticLogMatchDTO(BaseModel):
    source: GatewayDiagnosticLogQuerySourceDTO
    reference: GatewayDiagnosticLogReferenceDTO
    preview: str
    preview_start_codepoint: int = Field(ge=0)


class GatewayDiagnosticLogQueryResultDTO(BaseModel):
    gateway_id: CanonicalGatewayId
    scope: GatewayDiagnosticLogQueryScopeDTO
    query: str
    items: list[GatewayDiagnosticLogMatchDTO]
    sources: list[GatewayDiagnosticLogQuerySourceDTO]
    complete: bool
    cursor: str | None = None
    limited_reason: Literal["result_limit", "scan_limit", "byte_limit", "deadline", "item_size_limit", "source_limit", "source_unavailable"] | None = None
    scanned_sources: int = Field(ge=0)
    scanned_lines: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if self.complete != (self.limited_reason is None) or self.complete and self.cursor is not None:
            raise ValueError("日志查询覆盖状态、原因和续点不一致")
        sources = {source.log_id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("日志查询重复披露同一来源")
        for source in self.sources:
            if source.source != self.scope.source or (
                not source.workspace_ids if self.scope.source == "workspace" else bool(source.workspace_ids)
            ) or (self.scope.source == "workspace" and not set(source.workspace_ids).intersection(self.scope.workspace_ids)):
                raise ValueError("日志查询包含未选择的来源")
        if any(item.reference.gateway_id != self.gateway_id or item.reference.log_id != item.source.log_id
               or sources.get(item.source.log_id) != item.source or item.source.status == "unavailable" for item in self.items):
            raise ValueError("日志命中与公开来源或 Gateway 身份不一致")
        return self

    @property
    def provider_id(self) -> str:
        return "gateway-runtime-logs"

    @property
    def result_count(self) -> int:
        return len(self.items)

    @property
    def state(self) -> Literal["complete", "limited"]:
        return "complete" if self.complete else "limited"


class GatewayDiagnosticLogLocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: GatewayDiagnosticLogQueryScopeDTO
    reference: GatewayDiagnosticLogReferenceDTO


class GatewayDiagnosticLogLocationDTO(BaseModel):
    source: GatewayDiagnosticLogQuerySourceDTO
    reference: GatewayDiagnosticLogReferenceDTO
    text: str = Field(description="原诊断物理行去除行尾后的 UTF-8 替换解码文本；位置采用 Unicode 码点")

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.source.log_id != self.reference.log_id or self.reference.end_codepoint > len(self.text):
            raise ValueError("日志定位正文与原来源或码点范围不符")
        return self


class DevelopmentRuntimeRestartDTO(BaseModel):
    status: Literal["scheduled"] = "scheduled"
    previous_process_id: int = Field(gt=0)
    helper_process_id: int = Field(gt=0)
    delay_ms: int = Field(ge=0)
