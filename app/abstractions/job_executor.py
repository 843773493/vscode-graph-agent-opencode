from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from app.schemas.public_v2.common import JobStatus, MessageRole
from app.schemas.public_v2.message import AttachmentRef


class JobRuntimeStateProtocol(Protocol):
    job_id: str
    session_id: str
    message: str
    agent_id: str
    message_id: str
    attachments: list[AttachmentRef]
    message_created_at: str
    message_role: MessageRole
    message_metadata: dict[str, object]
    status: JobStatus
    progress: int
    error_message: Optional[str]
    result: Optional[str]
    created_at: datetime
    updated_at: datetime
    ended_at: Optional[datetime]


class JobExecutorProtocol(Protocol):
    async def run(self, job: JobRuntimeStateProtocol) -> str:
        ...

    async def fail(self, job: JobRuntimeStateProtocol, error: Exception) -> None:
        ...
