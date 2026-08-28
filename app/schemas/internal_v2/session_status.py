from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class SessionNetworkWaitDTO(BaseModel):
    id: str
    session_id: str
    message: str
    restored: bool
    created_at: datetime
    restored_at: Optional[datetime] = None


class SessionStatusDTO(BaseModel):
    session_id: str
    status: Literal["idle", "busy", "question", "permission", "retry", "offline"]
    message: Optional[str] = None
    active_job_id: Optional[str] = None
    waiting: Optional[SessionNetworkWaitDTO] = None


class SessionObservationStateDTO(BaseModel):
    session_id: str
    active_job_id: Optional[str] = None
    is_streaming: bool = False
    is_idle: bool = True
