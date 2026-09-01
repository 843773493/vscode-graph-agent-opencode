from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef


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
    current_step: str | None = None
    error_message: str | None = None
    result: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    task: asyncio.Task | None = None
    progress_reporter: Callable[[str], None] | None = None
