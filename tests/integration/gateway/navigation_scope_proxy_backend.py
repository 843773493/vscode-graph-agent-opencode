"""Gateway 导航 scope integration 用的 FastAPI/SQLite 下游服务。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api import session_navigation
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import (
    TraceMiddleware,
    get_current_gateway_id,
    get_current_navigation_actor,
)
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationStatusPageDTO,
)
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    NavigationAuthScope,
    SessionCatalogOperationsService,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationQueueStore,
)


class _RouteCatalogService:
    """把生产 enqueue/status 实现挂到独立 HTTP 服务并记录收到的认证 scope。"""

    def __init__(
        self,
        operations: SessionCatalogOperationsService,
        *,
        workspace_id: str,
    ) -> None:
        self._operations = operations
        self.workspace_id = workspace_id
        self.scopes: list[NavigationAuthScope] = []

    async def submit_operation_batch(
        self,
        request: NavigationMutationEnqueueRequest,
        scope: NavigationAuthScope,
    ) -> NavigationMutationEnqueueResultDTO:
        self.scopes.append(scope)
        return await self._operations.enqueue(request, scope)

    def operation_status(
        self,
        operation_ids: list[str],
        scope: NavigationAuthScope,
    ) -> NavigationMutationStatusPageDTO:
        self.scopes.append(scope)
        return self._operations.status(operation_ids, scope)


def create_app() -> FastAPI:
    """建立独立下游服务；所有队列数据保存在传入工作区的 SQLite catalog。"""

    workspace_root = Path(os.environ["WORKSPACE_ROOT"]).resolve()
    resolver = get_session_path_resolver(workspace_root / ".boxteam" / "sessions")
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
    route_service = _RouteCatalogService(
        operations,
        workspace_id=resolver.workspace_id,
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
                store.close()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(TraceMiddleware)
    application.include_router(session_navigation.router, prefix="/api/v1")
    application.dependency_overrides[
        session_navigation.get_session_catalog_service
    ] = lambda: route_service

    @application.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
