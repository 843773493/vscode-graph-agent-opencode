"""总期限在真实 catalog 扫描和 Session 准入边界生效。"""

import time
from types import SimpleNamespace

import pytest

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_navigation import (
    SessionCatalogService,
    thread_search,
)
from app.services.business.session_navigation import name_search_budget as budgets
from app.services.mapping import session_catalog_search as catalog_matching
from tests.unit.services.business.session_navigation.test_fuzzy_catalog_search import (
    catalog as catalog,  # noqa: PLC0414 - 复用真实 catalog 和 Session owner。
)
from tests.unit.services.business.session_navigation.test_thread_search import GATEWAY


@pytest.fixture
def budget_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 0.0}
    # 只替换本业务时钟，不改变 asyncio 和测试监督器的真实时钟。
    monkeypatch.setattr(budgets, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=lambda: 1000.0))
    return clock


@pytest.mark.asyncio
async def test_expired_query_does_not_enter_snapshot(
    catalog: SessionCatalogService, budget_clock: dict[str, float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_snapshot(*, check=None):
        pytest.fail("已过期的名称查询不应进入 catalog 扫描")

    monkeypatch.setattr(catalog._path_resolver, "catalog_snapshot", reject_snapshot)
    with pytest.raises(TimeoutError, match="总执行期限"):
        await catalog.search(query="needle", limit=1, cursor=None, deadline_at=999.0)
    with pytest.raises(TimeoutError, match="总执行期限"):
        await thread_search.search_session_threads(
            catalog, catalog._session_service, gateway_id=GATEWAY,
            query="needle", limit=1, cursor=None, deadline_at=999.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline_at", [None, 10000.0])
async def test_cold_snapshot_stops_reading_rows_and_does_not_publish_partial_cache(
    catalog: SessionCatalogService, budget_clock: dict[str, float], monkeypatch: pytest.MonkeyPatch,
    deadline_at: float | None,
) -> None:
    resolver = catalog._path_resolver
    for index in range(10):
        resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f"needle {index}")
    # 一致性检查属于原 resolver 准入；在故障注入前完成，不把它伪装为搜索正文。
    resolver._ensure_consistency_verified()
    original = resolver.catalog_store._node_from_row
    consumed = []

    def consume(row):
        node = original(row)
        consumed.append(node.node_id)
        if len(consumed) == 2:
            budget_clock["now"] = 16.0
        return node

    monkeypatch.setattr(resolver.catalog_store, "_node_from_row", consume)
    with pytest.raises(TimeoutError, match="总执行期限"):
        await catalog.search(query="needle", limit=1, cursor=None, deadline_at=deadline_at)
    assert len(consumed) == 2
    assert catalog._cached_snapshot is None
    budget_clock["now"] = 0.0
    monkeypatch.setattr(resolver.catalog_store, "_node_from_row", original)
    assert (await catalog.search(query="needle", limit=1, cursor=None)).total == 10


@pytest.mark.asyncio
async def test_warm_snapshot_stops_matching_when_total_deadline_expires(
    catalog: SessionCatalogService, budget_clock: dict[str, float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = catalog._path_resolver
    for index in range(10):
        resolver.catalog_store.create_folder(create_prefixed_id("ses"), resolver.workspace_id, None, f"needle {index}")
    assert (await catalog.search(query="needle", limit=1, cursor=None)).total == 10
    snapshot = catalog._cached_snapshot
    original = catalog_matching.is_subsequence
    matched = 0

    def matching(query: str, term: str) -> bool:
        nonlocal matched
        matched += 1
        if matched == 2:
            budget_clock["now"] = 16.0
        return original(query, term)

    monkeypatch.setattr(catalog_matching, "is_subsequence", matching)
    with pytest.raises(TimeoutError, match="总执行期限"):
        await catalog.search(query="needle", limit=1, cursor=None)
    assert matched == 2
    assert catalog._cached_snapshot is snapshot
    budget_clock["now"] = 0.0
    monkeypatch.setattr(catalog_matching, "is_subsequence", original)
    assert (await catalog.search(query="needle", limit=1, cursor=None)).total == 10


@pytest.mark.asyncio
async def test_thread_total_deadline_covers_real_admission_wait_and_releases_it(
    catalog: SessionCatalogService,
) -> None:
    sessions = catalog._session_service
    owner = await sessions.create(SessionCreateRequest(title="needle main"))
    await catalog.export_index()
    async with sessions._navigation_topology_gate.exclusive():
        with pytest.raises(TimeoutError):
            await thread_search.search_session_threads(
                catalog, sessions, gateway_id=GATEWAY, query="needle", limit=10,
                cursor=None, deadline_at=time.time() + 0.05,
            )
    result = await thread_search.search_session_threads(
        catalog, sessions, gateway_id=GATEWAY, query="needle", limit=10, cursor=None,
    )
    assert result.complete
    assert [hit.session_id for hit in result.items] == [owner.session_id]


@pytest.mark.parametrize("deadline_at", [float("inf"), float("-inf"), float("nan")])
def test_name_search_rejects_nonfinite_deadline(deadline_at: float) -> None:
    with pytest.raises(ValueError, match="有限"):
        budgets.NameSearchBudget.create(deadline_at)
