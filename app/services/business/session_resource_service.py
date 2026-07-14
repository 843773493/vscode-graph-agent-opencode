from __future__ import annotations

from dataclasses import dataclass, field

from app.abstractions.job_service import JobServiceProtocol
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceKind,
    SessionResourceListDTO,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_service import SessionService


@dataclass(frozen=True, slots=True)
class SessionResourceCleanupResult:
    cleaned_execution_runs: int = 0
    cleaned_by_kind: dict[SessionResourceKind, int] = field(default_factory=dict)

    @property
    def cleaned_background_tasks(self) -> int:
        return self.cleaned_by_kind.get("background_task", 0)

    @property
    def cleaned_terminals(self) -> int:
        return self.cleaned_by_kind.get("terminal", 0)

    @property
    def cleaned_browsers(self) -> int:
        return self.cleaned_by_kind.get("browser", 0)


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

    async def list(self, session_id: str) -> SessionResourceListDTO:
        await self._session_service.get(session_id)
        items = await self._provider_registry.list_resources(session_id)
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

    async def cleanup_session(self, session_id: str) -> SessionResourceCleanupResult:
        await self._session_service.get(session_id)
        cleaned_execution_runs = await self._job_service.delete_session_jobs(session_id)
        cleaned_by_kind = await self._provider_registry.cleanup_session(session_id)
        return SessionResourceCleanupResult(
            cleaned_execution_runs=cleaned_execution_runs,
            cleaned_by_kind=cleaned_by_kind,
        )
