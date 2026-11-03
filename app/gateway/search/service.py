"""浏览器流与授权上下文 relay 共用同一范围、调度及输出准入。"""

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

import httpx

from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.search.output import SearchResponseEncoder
from app.gateway.search.scheduler import plan_search, search_events
from app.gateway.search.scope import resolve_search_scope
from app.gateway.search.transport import SearchTransport
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


@dataclass(frozen=True, slots=True)
class SearchExecution:
    events: AsyncGenerator[FederatedSearchEventDTO, None]
    encoder: SearchResponseEncoder


class GatewaySearchService:
    def __init__(self, registry: GatewayWorkspaceRegistry, navigation: WorkspaceNavigationStore, client: httpx.AsyncClient, *,
                 gateway_id: str, catalog_mirror: GatewaySessionCatalogSearchService | None = None) -> None:
        self.registry = registry
        self.navigation = navigation
        self.client = client
        self.gateway_id = gateway_id
        self.catalog_mirror = catalog_mirror

    def prepare(self, request: FederatedSearchRequest, *, request_id: str,
                encode: Callable[[FederatedSearchEventDTO], bytes], max_bytes: int | None = None) -> SearchExecution:
        budget = NameSearchBudget.create(request.deadline_at)
        scope = resolve_search_scope(self.registry, self.navigation, gateway_id=self.gateway_id, request=request.scope)
        jobs = plan_search(request, scope)
        encoder = SearchResponseEncoder(jobs, scope, request_id, encode=encode, max_bytes=max_bytes)
        transport = SearchTransport(self.registry, self.client, gateway_id=self.gateway_id,
                                    request_id=request_id, catalog_mirror=self.catalog_mirror)
        return SearchExecution(search_events(jobs, scope, request_id=request_id, query=transport.query,
                                            budget=budget, publish=encoder.publish), encoder)
