from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef


class JobRuntimeStateProtocol(Protocol):
    job_id: str
    session_id: str
    message: str
    agent_id: str
    message_id: str
    attachments: list[AttachmentRef]
    message_created_at: str
    message_metadata: dict[str, object]
    status: JobStatus
    progress: int
    current_step: str | None
    error_message: str | None
    result: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    progress_reporter: Callable[[str], None] | None


class JobExecutorProtocol(Protocol):
    async def run(self, job: JobRuntimeStateProtocol) -> str:
        ...

    async def fail(self, job: JobRuntimeStateProtocol, error: Exception) -> None:
        ...
