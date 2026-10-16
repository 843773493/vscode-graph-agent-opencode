"""Gateway 导航 scope integration 用的 FastAPI/SQLite 下游服务。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api import session_navigation
from app.core.path_utils import build_session_catalog_components, catalog_provider_scope
from app.core.trace_middleware import (
    TraceMiddleware,
    get_current_gateway_id,
    get_current_navigation_actor,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationStatusPageDTO,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    NavigationAuthScope,
    SessionCatalogOperationsService,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_store import TraceEventStore


class _RouteCatalogService(SessionCatalogService):
    """把生产 enqueue/status 实现挂到独立 HTTP 服务并记录收到的认证 scope。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        operations: SessionCatalogOperationsService,
    ) -> None:
        super().__init__(
            session_service=session_service,
            operations_service=operations,
        )
        self.scopes: list[NavigationAuthScope] = []

    async def submit_operation_batch(
        self,
        request: NavigationMutationEnqueueRequest,
        scope: NavigationAuthScope,
    ) -> NavigationMutationEnqueueResultDTO:
        self.scopes.append(scope)
        return await super().submit_operation_batch(request, scope)

    def operation_status(
        self,
        operation_ids: list[str],
        scope: NavigationAuthScope,
    ) -> NavigationMutationStatusPageDTO:
        self.scopes.append(scope)
        return super().operation_status(operation_ids, scope)


def create_app() -> FastAPI:
    """建立独立下游服务；所有队列数据保存在传入工作区的 SQLite catalog。"""

    workspace_root = Path(os.environ["WORKSPACE_ROOT"]).resolve()
    catalog = build_session_catalog_components(
        workspace_root / ".boxteam" / "sessions",
        load_or_create_workspace_id(workspace_root),
    )
    resolver = catalog.resolver
    resolver.initialize()
    store = resolver.catalog_store
    queue = NavigationMutationQueueStore(store)
    executor = NavigationMutationExecutor(
        store=store,
        workspace_id=resolver.workspace_id,
        queue=queue,
        path_resolver=resolver,
    )
    operations = SessionCatalogOperationsService(
        store=store,
        workspace_id=resolver.workspace_id,
        queue=queue,
        executor=executor,
    )
    with catalog_provider_scope(catalog):
        session_service = SessionService(
            config_service=ConfigService(workspace_root=workspace_root),
            trace_event_store=TraceEventStore(catalog.sessions_root),
            workspace_id=resolver.workspace_id,
            path_resolver=resolver,
            creation_service=catalog.creation_service,
            initialize_thread_storage=RolloutCheckpointSaver(catalog.sessions_root).initialize_thread_storage,
        )
    route_service = _RouteCatalogService(
        session_service=session_service, operations=operations
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await operations.start()
            yield
        finally:
            try:
                await operations.stop()
            finally:
                catalog.close()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(TraceMiddleware)
    application.include_router(session_navigation.router, prefix="/api/v1")
    application.dependency_overrides[
        session_navigation.get_session_catalog_service
    ] = lambda: route_service

    @application.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "workspace_id": resolver.workspace_id}

    @application.post("/api/v1/context/read")
    async def read_public_context() -> dict[str, str | None]:
        return {
            "actor": get_current_navigation_actor(),
            "gateway_id": get_current_gateway_id(),
        }

    @application.post("/api/v1/context/search")
    async def search_public_context() -> dict[str, str | None]:
        return {
            "actor": get_current_navigation_actor(),
            "gateway_id": get_current_gateway_id(),
        }

    @application.get("/__test__/navigation-scopes")
    async def navigation_scopes() -> list[dict[str, str]]:
        return [
            {
                "actor": scope.actor,
                "gateway_id": scope.gateway_id,
                "workspace_id": scope.workspace_id,
            }
            for scope in route_service.scopes
        ]

    return application
