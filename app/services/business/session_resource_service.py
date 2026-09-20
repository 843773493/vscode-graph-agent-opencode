from __future__ import annotations

from app.abstractions.job_service import JobServiceProtocol
from app.schemas.internal_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceKind,
    SessionResourceListDTO,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_service import SessionService


class SessionResourceService:
    def __init__(
        self,
        *,
        session_service: SessionService,
        job_service: JobServiceProtocol,
        provider_registry: SessionResourceProviderRegistry,
    ) -> None:
        self._session_service = session_service
        self._job_service = job_service
        self._provider_registry = provider_registry

    async def list(
        self,
        session_id: str,
        *,
        include_history: bool = True,
    ) -> SessionResourceListDTO:
        await self._session_service.get(session_id)
        items = await self._provider_registry.list_resources(
            session_id,
            include_history=include_history,
        )
        items.sort(key=lambda item: item.created_at, reverse=True)
        return SessionResourceListDTO(session_id=session_id, items=items)

    async def control(
        self,
        *,
        session_id: str,
        kind: SessionResourceKind,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        await self._session_service.get(session_id)
        return await self._provider_registry.control(
            session_id=session_id,
            kind=kind,
            resource_id=resource_id,
            action=action,
        )

    async def cleanup_session(self, session_id: str) -> None:
        await self._session_service.get(session_id)
        await self._job_service.delete_session_jobs(session_id)
        await self._provider_registry.cleanup_session(session_id)
