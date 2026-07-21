from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.public_v2.message import MessageRunAccepted


@runtime_checkable
class SessionOrchestratorProtocol(Protocol):
    async def create_and_run(
        self,
        session_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> MessageRunAccepted: ...
