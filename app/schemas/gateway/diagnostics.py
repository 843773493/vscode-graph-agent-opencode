from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.gateway.workspaces import GatewayConnectionKind, GatewayWorkspaceStatus
from app.schemas.runtime_logging import RuntimeLoggingService

GatewayDiagnosticStatus = Literal["ready", "degraded", "offline"]


GatewayDiagnosticLogStatus = Literal["available", "empty", "unavailable"]


GatewayDiagnosticLogSource = Literal["gateway", "workspace"]


class GatewayHealthDTO(BaseModel):
    gateway_id: str
    status: Literal["ok"] = "ok"
    active_workspace_id: str | None = None
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


class DevelopmentRuntimeRestartDTO(BaseModel):
    status: Literal["scheduled"] = "scheduled"
    previous_process_id: int = Field(gt=0)
    helper_process_id: int = Field(gt=0)
    delay_ms: int = Field(ge=0)
