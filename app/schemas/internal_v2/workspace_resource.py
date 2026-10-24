"""与 workspace/v2/public.proto 一致的资源 owner JSON 运行时边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId


class ResourceThreadRef(BaseModel):
    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId


class WorkspaceResourceDTO(BaseModel):
    resource_id: str
    workspace_id: str
    kind: Literal["terminal", "browser"]
    retention_scope: Literal["thread", "workspace"]
    # 资源创建时记录的来源 Thread，是起源事实，不随显式关联的增删而改变。
    source_thread: ResourceThreadRef
    # 当前显式关联列表，可变且可空；Thread 级资源为空时，查询仍可按 source_thread 筛选。
    # 该列表只记录关联，不代表操作租约持有者或完整准入结果。
    thread_associations: list[ResourceThreadRef]
    # owner 持久化的传输授权版本，用于版本校验，并与外部 transport_owner_revision 对账。
    owner_revision: int = Field(description="owner 持久化的传输授权版本，用于版本校验，并与外部 transport_owner_revision 对账。")
    # 生命周期状态；观察时来自外部运行快照，创建、停止或删除期间也可处于过渡状态。
    status: str
    # 外部状态及前次操作结果的核实状态，与生命周期状态分开。
    reconciliation_status: str
    unattached_since: str | None
    reclaim_at: str | None
    # 候选管理动作按核实状态、外部观察和持久关联过滤，未知资源可显式停止；仍须核 lease 与版本。
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
