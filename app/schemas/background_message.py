from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class BackgroundMessageKind(str, Enum):
    normal = "normal"
    interrupt = "interrupt"


class BackgroundMessageDTO(BaseModel):
    message_id: str
    session_id: str
    agent_id: str
    source_id: str
    kind: BackgroundMessageKind
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class BackgroundMessageBatchDTO(BaseModel):
    session_id: str
    agent_id: str
    source_id: Optional[str] = None
    messages: list[BackgroundMessageDTO] = Field(default_factory=list)
    interrupted: bool = False
    timed_out: bool = False
    last_message_id: Optional[str] = None
    collected_at: datetime