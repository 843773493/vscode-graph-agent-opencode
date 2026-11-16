from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.internal_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)


@runtime_checkable
class SessionResourceProviderProtocol(Protocol):
    """一种会话后台资源的显式生命周期提供者。"""

    kind: SessionResourceKind

    async def list_resources(
        self,
        session_id: str,
        *,
        include_history: bool = True,
    ) -> list[SessionResourceDTO]: ...

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
        operation_id: str,
    ) -> SessionResourceControlResultDTO: ...

    async def cleanup_session(self, session_id: str) -> int: ...
