from __future__ import annotations

from typing import Protocol

from app.domain.itemized.execution_queue import ExecutionQueueRecord
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestControl,
    PendingRequestDTO,
    PendingRequestSummaryListDTO,
)


class ExecutionQueueClaimProtocol(Protocol):
    record: ExecutionQueueRecord

    async def start(self) -> None: ...

    async def close(self) -> None: ...


class PendingRequestStoreProtocol(Protocol):
    async def load(self, session_id: str) -> list[PendingRequestDTO]: ...

    async def record(self, session_id: str, thread_id: str, job_id: str) -> ExecutionQueueRecord: ...

    async def records(self, session_id: str | None = None) -> tuple[ExecutionQueueRecord, ...]: ...

    async def snapshot_version(self, session_id: str) -> int: ...

    async def load_summaries(
        self, session_id: str, *, limit: int, thread_id: str | None = None,
    ) -> PendingRequestSummaryListDTO: ...

    async def update_policy(
        self, reference: PendingRequestControl, policy: DeliveryPolicy, *, expected_snapshot_version: int | None,
    ) -> None: ...

    async def before_dispatch(self, record: ExecutionQueueRecord) -> bool: ...

    async def claim(self, record: ExecutionQueueRecord) -> ExecutionQueueClaimProtocol | None: ...

    async def project(self, record: ExecutionQueueRecord, *, position: int, snapshot_version: int) -> PendingRequestDTO: ...
