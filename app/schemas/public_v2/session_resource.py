from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SessionResourceKind = Literal["job", "background_task", "terminal"]
SessionResourceAction = Literal["pause", "resume", "cancel", "delete"]


class SessionResourceDTO(BaseModel):
    resource_id: str
    session_id: str
    kind: SessionResourceKind
    name: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    available_actions: list[SessionResourceAction] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class SessionResourceListDTO(BaseModel):
    session_id: str
    items: list[SessionResourceDTO]


class SessionResourceControlRequest(BaseModel):
    action: SessionResourceAction
    params: dict[str, object] = Field(default_factory=dict)


class SessionResourceControlResultDTO(BaseModel):
    session_id: str
    resource_id: str
    kind: SessionResourceKind
    action: SessionResourceAction
    status: str
    resource: SessionResourceDTO | None = None
