from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.abstractions.internal_message import PreparedInternalMessage
from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    UserExecutionIdentity,
)
from app.schemas.internal_v2.message import MessageDTO, MessageRunAccepted
from app.schemas.internal_v2.pending_request import DeliveryPolicy


@runtime_checkable
class SessionOrchestratorProtocol(Protocol):
    def reserve_user_execution_identity(self) -> UserExecutionIdentity: ...

    def reserve_internal_job_id(self) -> str: ...

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
        thread_id: str | None = None,
    ) -> MessageRunAccepted: ...

    async def prepare_internal_message(
        self,
        session_id: str,
        message: PreparedInternalMessage,
    ) -> MessageDTO: ...

    async def dispatch_existing_message(
        self,
        session_id: str,
        message: MessageDTO,
        *,
        execution_binding: ExecutionBinding,
        reserved_job_id: str | None = None,
    ) -> MessageRunAccepted: ...
