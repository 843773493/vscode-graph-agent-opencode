from __future__ import annotations

from typing import Protocol

from app.abstractions.internal_message import PreparedInternalMessage
from app.schemas.internal_v2.message import MessageRunAccepted
from app.schemas.internal_v2.pending_request import DeliveryPolicy


class SessionMessageTransportProtocol(Protocol):
    async def dispatch(
        self,
        session_id: str,
        *,
        workspace_id: str,
        content: str,
        metadata: dict[str, object],
        simulate_user: bool,
        delivery_policy: DeliveryPolicy,
        idempotency_key: str | None,
    ) -> MessageRunAccepted: ...


class SessionMessageDeliveryProtocol(Protocol):
    async def dispatch(
        self,
        session_id: str,
        *,
        workspace_id: str | None,
        content: str,
        metadata: dict[str, object],
        internal_message: PreparedInternalMessage | None,
        simulate_user: bool,
        delivery_policy: DeliveryPolicy,
        idempotency_key: str | None,
    ) -> MessageRunAccepted: ...


__all__ = [
    "SessionMessageDeliveryProtocol",
    "SessionMessageTransportProtocol",
]
