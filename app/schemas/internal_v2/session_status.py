from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SessionNetworkWaitDTO(BaseModel):
    id: str
    session_id: str
    message: str
    restored: bool
    created_at: datetime
    restored_at: datetime | None = None


class SessionStatusDTO(BaseModel):
    session_id: str
    status: Literal["idle", "busy", "question", "permission", "retry", "offline"]
    message: str | None = None
    active_job_id: str | None = None
    waiting: SessionNetworkWaitDTO | None = None
