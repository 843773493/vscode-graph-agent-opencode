"""注册现有名称与历史 owner；不复制扫描、分页或缓存。"""

from pydantic import ValidationError

from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.schemas.internal_v2.turn_search import TurnMessageSearchRequest
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
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.business.session_turn_history.workspace_search import (
    search_workspace_messages,
)


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
        supported_options=options, position_kind="catalog", max_results=500, cache_policy="owner", match_modes=("fuzzy",),
    ), catalog_search)
    service.register(SearchProviderCapabilityDTO(
        provider_id="session-thread", category="thread", operations=("name",), contract_version=1,
        supported_options=options, position_kind="thread", max_results=100, cache_policy="none", match_modes=("fuzzy",),
    ), thread_search)


def register_history_search(
    service: WorkspaceSearchService, catalog: SessionCatalogReadService, sessions: SessionService,
    history: SessionTurnHistoryService,
) -> None:
    async def content_search(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        if gateway_id is None:
            raise SearchContractError("owner_mismatch", "历史搜索缺少已认证 Gateway 身份")
        try:
            payload = TurnMessageSearchRequest.model_validate({
                "query": request.query, "limit": request.limit, "cursor": request.cursor, "category": request.category,
            })
        except ValidationError as error:
            raise SearchContractError("invalid_query", str(error)) from error
        page = await search_workspace_messages(
            catalog, sessions, history, payload, gateway_id=gateway_id, deadline_at=request.deadline_at,
        )
        return WorkspaceSearchResultDTO(
            provider_id=f"canonical-{payload.category}", workspace_id=service.workspace_id,
            category=payload.category, operation="content", contract_version=1,
            state="complete" if page.complete else "limited", limited_reason=page.limited_reason, messages=page,
        )

    for category in ("message", "tool_result"):
        service.register(SearchProviderCapabilityDTO(
            provider_id=f"canonical-{category}", category=category, operations=("content",), contract_version=1,
            supported_options=("query", "match_mode", "limit", "cursor", "deadline_at"),
            position_kind="canonical_codepoint", max_results=100, cache_policy="none", match_modes=("literal",),
        ), content_search)
