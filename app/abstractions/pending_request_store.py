from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.public_v2.pending_request import (
    PendingRequestDTO,
    PendingRequestSummaryListDTO,
)


@runtime_checkable
class PendingRequestStoreProtocol(Protocol):
    async def migrate_schema(self, session_id: str) -> None: ...

    async def load(self, session_id: str) -> list[PendingRequestDTO]: ...

    async def load_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO: ...

    async def save(
        self,
        session_id: str,
        requests: list[PendingRequestDTO],
    ) -> None: ...

    async def delete(self, session_id: str) -> None: ...
