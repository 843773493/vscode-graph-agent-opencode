from __future__ import annotations

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.session_message import (
    SessionMessageDeliveryProtocol,
    SessionMessageTransportProtocol,
)
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_target import SessionTargetResolverProtocol
from app.schemas.public_v2.message import MessageRunAccepted
from app.schemas.public_v2.pending_request import DeliveryPolicy


class SessionMessageDeliveryService(SessionMessageDeliveryProtocol):
    """在本地编排器与 Gateway 远端传输之间选择消息投递路径。"""

    def __init__(
        self,
        *,
        target_resolver: SessionTargetResolverProtocol,
        session_orchestrator: SessionOrchestratorProtocol,
        remote_transport: SessionMessageTransportProtocol,
    ) -> None:
        self._target_resolver = target_resolver
        self._session_orchestrator = session_orchestrator
        self._remote_transport = remote_transport

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
    ) -> MessageRunAccepted:
        target = await self._target_resolver.resolve_session(
            session_id,
            workspace_id=workspace_id,
        )
        if target.workspace_id is not None:
            return await self._remote_transport.dispatch(
                target.session_id,
                workspace_id=target.workspace_id,
                content=content,
                metadata={} if simulate_user else metadata,
                simulate_user=simulate_user,
                delivery_policy=delivery_policy,
                idempotency_key=idempotency_key,
            )
        if simulate_user:
            return await self._session_orchestrator.create_and_run(
                target.session_id,
                content,
                delivery_policy=delivery_policy,
                idempotency_key=idempotency_key,
            )
        if internal_message is None:
            raise RuntimeError("内部会话消息缺少 PreparedInternalMessage")
        return await self._session_orchestrator.create_and_run_internal(
            target.session_id,
            internal_message,
            delivery_policy=delivery_policy,
            idempotency_key=idempotency_key,
        )


__all__ = ["SessionMessageDeliveryService"]
