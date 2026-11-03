"""工具和脚本消费同一 Gateway 搜索流，不承担跨目标调度。"""

from collections.abc import AsyncIterator
from typing import Protocol

from app.schemas.gateway.search import GatewayWorkspaceDiscoveryResultsDTO
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)


class GatewayResourceSearchProtocol(Protocol):
    async def discover_workspaces(
        self, *, query: str, limit: int, cursor: str | None, include_folders: bool, request_id: str,
    ) -> GatewayWorkspaceDiscoveryResultsDTO: ...

    def search(
        self, request: FederatedSearchRequest, *, request_id: str,
    ) -> AsyncIterator[FederatedSearchEventDTO]: ...
