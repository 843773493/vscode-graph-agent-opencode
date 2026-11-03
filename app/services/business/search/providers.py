"""注册现有名称 owner；不复制扫描、分页或缓存。"""

from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)
from app.services.business.session_navigation.read_service import (
    SessionCatalogReadService,
)
from app.services.business.session_navigation.thread_search import (
    search_session_threads,
)
from app.services.business.session_service import SessionService


def register_navigation_search(
    service: WorkspaceSearchService, catalog: SessionCatalogReadService, sessions: SessionService,
) -> None:
    async def catalog_search(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        page = await catalog.search(
            query=request.query, limit=request.limit, cursor=request.cursor, deadline_at=request.deadline_at,
        )
        return WorkspaceSearchResultDTO(
            provider_id="session-catalog", workspace_id=service.workspace_id, category="catalog", operation="name",
            contract_version=1, state="limited" if page.cursor else "complete",
            limited_reason="result_limit" if page.cursor else None, catalog=page,
        )

    async def thread_search(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        if gateway_id is None:
            raise SearchContractError("owner_mismatch", "Thread 搜索缺少已认证 Gateway 身份")
        page = await search_session_threads(
            catalog, sessions, gateway_id=gateway_id, query=request.query,
            limit=request.limit, cursor=request.cursor, deadline_at=request.deadline_at,
        )
        return WorkspaceSearchResultDTO(
            provider_id="session-thread", workspace_id=service.workspace_id, category="thread", operation="name",
            contract_version=1, state="complete" if page.complete else "limited",
            limited_reason=page.limited_reason, threads=page,
        )

    options = ("query", "match_mode", "limit", "cursor", "deadline_at")
    service.register(SearchProviderCapabilityDTO(
        provider_id="session-catalog", category="catalog", operations=("name",), contract_version=1,
        supported_options=options, position_kind="catalog", max_results=500, cache_policy="owner",
    ), catalog_search)
    service.register(SearchProviderCapabilityDTO(
        provider_id="session-thread", category="thread", operations=("name",), contract_version=1,
        supported_options=options, position_kind="thread", max_results=100, cache_policy="none",
    ), thread_search)
