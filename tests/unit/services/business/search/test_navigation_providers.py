"""真实目录和 Session 创建产生名称查询；不加载执行态或维护副本索引。"""

import time

import pytest

from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.search.providers import register_navigation_search
from app.services.business.search.service import WorkspaceSearchService
from app.services.business.session_navigation import SessionCatalogService
from tests.unit.services.business.session_navigation.test_fuzzy_catalog_search import (
    catalog as catalog,  # noqa: PLC0414 - 复用真实 Session／目录 owner 装具。
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def service(catalog: SessionCatalogService) -> WorkspaceSearchService:
    service = WorkspaceSearchService(catalog.workspace_id)
    register_navigation_search(service, catalog, catalog._session_service)
    return service


async def test_registered_owner_preserves_identity_and_original_pagination(
    catalog: SessionCatalogService, service: WorkspaceSearchService,
) -> None:
    sessions = catalog._session_service
    owners = [await sessions.create(SessionCreateRequest(title="Alpha Delta")) for _ in range(2)]
    query = WorkspaceSearchRequest(workspace_id=catalog.workspace_id, category="catalog", query="AD", limit=1)
    first = await service.search(query, gateway_id=GATEWAY)
    assert first.state == "limited" and first.catalog and first.catalog.cursor
    direct = await catalog.search(query="AD", limit=1, cursor=None)
    assert first.catalog == direct
    second = await service.search(query.model_copy(update={"cursor": first.catalog.cursor}), gateway_id=GATEWAY)
    assert second.state == "complete" and second.catalog
    assert {item.node.session_id for page in (first.catalog, second.catalog) for item in page.items} == {owner.session_id for owner in owners}
    threads = await service.search(query.model_copy(update={"category": "thread", "limit": 100}), gateway_id=GATEWAY)
    assert threads.state == "complete" and threads.threads
    assert {(hit.session_id, hit.thread_id) for hit in threads.threads.items} == {
        (owner.session_id, await sessions.resolve_main_thread(owner.session_id)) for owner in owners
    }


async def test_deadline_and_stale_owner_cursor_remain_explicit(
    catalog: SessionCatalogService, service: WorkspaceSearchService,
) -> None:
    sessions = catalog._session_service
    for _ in range(2):
        await sessions.create(SessionCreateRequest(title="Alpha Delta"))
    query = WorkspaceSearchRequest(workspace_id=catalog.workspace_id, category="catalog", query="AD", limit=1)
    first = await service.search(query)
    assert first.catalog and first.catalog.cursor
    await sessions.create(SessionCreateRequest(title="Alpha Later"))
    with pytest.raises(ValueError, match="会话目录已更新"):
        await service.search(query.model_copy(update={"cursor": first.catalog.cursor}))
    with pytest.raises(TimeoutError, match="总执行期限"):
        await service.search(query.model_copy(update={"deadline_at": time.time() - 1}))
