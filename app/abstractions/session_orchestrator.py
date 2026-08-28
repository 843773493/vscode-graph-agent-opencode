from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.abstractions.internal_message import PreparedInternalMessage
from app.schemas.internal_v2.message import MessageDTO, MessageRunAccepted
from app.schemas.internal_v2.pending_request import DeliveryPolicy


@runtime_checkable
class SessionOrchestratorProtocol(Protocol):
    async def create_and_run(
        self,
        session_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        delivery_policy: DeliveryPolicy = "after_turn",
        idempotency_key: str | None = None,
    ) -> MessageRunAccepted: ...

    async def create_and_run_internal(
        self,
        session_id: str,
        message: PreparedInternalMessage,
        *,
        delivery_policy: DeliveryPolicy = "after_turn",
        idempotency_key: str | None = None,
    ) -> MessageRunAccepted: ...

    async def prepare_internal_message(
        self,
        session_id: str,
        message: PreparedInternalMessage,
    ) -> MessageDTO: ...
