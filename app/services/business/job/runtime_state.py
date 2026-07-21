from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Callable
from typing import Optional

from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.message import AttachmentRef


@dataclass
class JobRuntimeState:
    job_id: str
    session_id: str
    message: str
    agent_id: str
    message_id: str
    message_created_at: str
    message_metadata: dict[str, object] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)
    status: JobStatus = JobStatus.queued
    progress: int = 0
    error_message: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    task: Optional[asyncio.Task] = None
    yield_requested: Callable[[], bool] = field(default=lambda: False)
