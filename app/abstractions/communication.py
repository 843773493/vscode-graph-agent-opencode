"""工具调用方依赖的唯一跨 Session 通信端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.schemas.internal_v2.communication import CommunicationReceipt
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.business.communication.admission import CommunicationPayload
from app.services.business.communication.wait import WaitSelector, WaitUntil


class SessionCommunicationProtocol(Protocol):
    async def send(
        self, *, session_id: str, thread_id: str, send_operation_id: str,
        target_session_id: str, target_workspace_id: str | None,
        payload: CommunicationPayload,
    ) -> CommunicationReceipt: ...

    async def read(
        self, *, session_id: str, thread_id: str, source_call_id: str, source_call_created_at: datetime,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO: ...

    async def wait(
        self, *, session_id: str, thread_id: str, source_call_id: str, source_call_created_at: datetime,
        target_session_id: str, selector: WaitSelector | None,
        until: WaitUntil, timeout_seconds: int,
    ) -> dict[str, object]: ...
