"""有效上下文沿原 owner 查询；不读取第二份消息历史。"""

import asyncio

from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceContextSearchOptionsDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.schemas.internal_v2.session_context import SessionContextSearchRequest
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


def register_context_search(service: WorkspaceSearchService, context: SessionContextQueryService) -> None:
    async def content_search(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        if gateway_id is None:
            raise SearchContractError("owner_mismatch", "上下文搜索缺少已认证 Gateway 身份")
        options = request.context or WorkspaceContextSearchOptionsDTO()
        original = SessionContextSearchRequest.model_validate({
            "resource_identity": workspace_session_inventory_identity(service.workspace_id),
            "uri": workspace_session_inventory_uri(service.workspace_id, gateway_authority=gateway_id),
            "scope": "workspace", "query": request.query, "sources": options.sources,
            "match_mode": request.match_mode, "case_sensitive": options.case_sensitive,
            "max_results": request.limit, "max_chars": options.max_chars, "cursor": request.cursor, "revision": options.revision,
        })
        budget = NameSearchBudget.create(request.deadline_at)
        # 原 owner 的同步投影仍按安全检查点结算；不能宣称强行打断本机阻塞读取。
        async with asyncio.timeout(budget.remaining()):
            page = await context.search_context(original)
        budget.check()
        return WorkspaceSearchResultDTO(
            provider_id="session-context", workspace_id=service.workspace_id, category="context", operation="content",
            contract_version=1, state="limited" if page.truncated else "complete",
            limited_reason=("result_limit" if page.has_more else "byte_limit") if page.truncated else None,
            context=page,
        )

    service.register(SearchProviderCapabilityDTO(
        provider_id="session-context", category="context", operations=("content",), contract_version=1,
        supported_options=("query", "match_mode", "limit", "cursor", "deadline_at", "context"),
        position_kind="context_codepoint", max_results=200, cache_policy="none", match_modes=("literal", "regex"),
    ), content_search)
