from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.public_v2.job import JobControlRequest, JobControlResponseDTO, JobDTO, StepDTO
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.message import AttachmentRef


@runtime_checkable
class JobServiceProtocol(Protocol):
    async def list(self, session_id: str | None = None) -> list[JobDTO]: ...

    async def get(self, job_id: str) -> JobDTO: ...

    async def list_steps(self, job_id: str) -> list[StepDTO]: ...

    async def control(
        self,
        job_id: str,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO: ...

    async def delete_session_jobs(self, session_id: str) -> int: ...

    async def start_job(
        self,
        session_id: str,
        message: str,
        *,
        agent_id: str = "default",
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_role: MessageRole = MessageRole.user,
        message_metadata: dict[str, object] | None = None,
    ) -> str: ...
