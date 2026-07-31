from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.abstractions.internal_message import PreparedInternalMessage
from app.schemas.public_v2.message import MessageDTO, MessageRunAccepted
from app.schemas.public_v2.pending_request import MessageDispatchMode


@runtime_checkable
class SessionOrchestratorProtocol(Protocol):
    async def create_and_run(
        self,
        session_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        dispatch_mode: MessageDispatchMode = "queued",
    ) -> MessageRunAccepted: ...

    async def create_and_run_internal(
        self,
        session_id: str,
        message: PreparedInternalMessage,
        *,
        dispatch_mode: MessageDispatchMode = "queued",
    ) -> MessageRunAccepted: ...

    async def prepare_internal_message(
        self,
        session_id: str,
        message: PreparedInternalMessage,
    ) -> MessageDTO: ...
