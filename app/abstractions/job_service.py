from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from app.schemas.public_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
    JobDispatchSnapshotDTO,
    JobDTO,
    StepDTO,
)

T = TypeVar("T")
from app.schemas.public_v2.message import AttachmentRef
from app.schemas.public_v2.pending_request import (
    PendingRequestKind,
    PendingRequestListDTO,
    PendingRequestOrderItem,
)


@runtime_checkable
class JobServiceProtocol(Protocol):
    def assert_accepting_jobs(self) -> None: ...

    async def list(self, session_id: str | None = None) -> list[JobDTO]: ...

    async def get(self, job_id: str) -> JobDTO: ...

    async def run_session_idle_operation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T: ...

    async def list_steps(self, job_id: str) -> list[StepDTO]: ...

    async def control(
        self,
        job_id: str,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO: ...

    async def delete_session_jobs(self, session_id: str) -> int: ...

    async def list_pending(self, session_id: str) -> PendingRequestListDTO: ...

    async def update_pending(
        self,
        session_id: str,
        message_id: str,
        *,
        content: str,
        attachments: list[AttachmentRef],
    ) -> PendingRequestListDTO: ...

    async def remove_pending(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO: ...

    async def clear_pending(self, session_id: str) -> PendingRequestListDTO: ...

    async def reorder_pending(
        self,
        session_id: str,
        requests: list[PendingRequestOrderItem],
    ) -> PendingRequestListDTO: ...

    async def send_pending_immediately(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO: ...

    async def start_job(
        self,
        session_id: str,
        message: str,
        *,
        agent_id: str = "default",
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        pending_kind: PendingRequestKind = "queued",
    ) -> JobDispatchSnapshotDTO: ...
