"""与 workspace/v2/public.proto 一致的资源 owner JSON 运行时边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.api.canonical_params import CanonicalSessionId, CanonicalThreadId


class ResourceThreadRef(BaseModel):
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId


class WorkspaceResourceDTO(BaseModel):
    resource_id: str
    workspace_id: str
    kind: Literal["terminal", "browser"]
    retention_scope: Literal["thread", "workspace"]
    source_thread: ResourceThreadRef
    thread_associations: list[ResourceThreadRef]
    owner_revision: int
    status: str
    reconciliation_status: str
    unattached_since: str | None
    reclaim_at: str | None
    available_actions: list[str]
    metadata: dict[str, object]
    created_at: str
    updated_at: str
    error: str | None

    @classmethod
    def from_owner(cls, snapshot: dict[str, object]) -> WorkspaceResourceDTO:
        fields = cls.model_fields.keys() - {"metadata"}
        return cls.model_validate({
            **{key: snapshot[key] for key in fields},
            "metadata": {key: value for key, value in snapshot.items() if key not in fields},
        })


class WorkspaceResourceListDTO(BaseModel):
    workspace_id: str
    items: list[WorkspaceResourceDTO]


class TerminalResourceCreateRequest(BaseModel):
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId | None = None
    retention_scope: Literal["thread", "workspace"] = "thread"
    title: str = "用户终端"
    cwd_relative: str | None = None
    cols: int = Field(default=100, gt=0)
    rows: int = Field(default=30, gt=0)


class WorkspaceResourceCommandRequest(BaseModel):
    expected_revision: int = Field(gt=0)
    thread: ResourceThreadRef | None = None


class ResourceTransportGrantDTO(BaseModel):
    resource_id: str
    owner_revision: int
    grant_token: str
    expires_at: str
    resource: WorkspaceResourceDTO


class BrowserResourceCreateRequest(BaseModel):
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId | None = None
    retention_scope: Literal["thread", "workspace"] = "thread"
    title: str = "用户浏览器"
    url: str = "about:blank"
    viewport: dict[str, int] | None = None
    device_profile: str | None = None
    device_orientation: str | None = None


class BrowserResourceOperationRequest(WorkspaceResourceCommandRequest):
    action: Literal[
        "read", "navigate", "click", "hover", "type", "drag", "dialog", "screenshot", "run",
        "resource-policy", "freeze", "wake", "discard", "device-profile", "agent-lock",
    ]
    payload: dict[str, object] = Field(default_factory=dict)
