"""注册、能力与安全结算；领域匹配另由原 owner 证明。"""

import asyncio
import threading
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.schemas.internal_v2.session_navigation import SessionCatalogSearchResultsDTO
from app.services.business.search.service import (
    SearchContractError,
    WorkspaceSearchService,
)


@pytest.fixture
def capability() -> SearchProviderCapabilityDTO:
    return SearchProviderCapabilityDTO(
        provider_id="test-catalog", category="catalog", operations=("name",), contract_version=1,
        supported_options=("match_mode",), position_kind="catalog", max_results=100, cache_policy="none",
    )


@pytest.fixture
def query() -> AsyncMock:
    return AsyncMock(return_value=WorkspaceSearchResultDTO(
        provider_id="test-catalog", workspace_id="workspace-A", category="catalog", operation="name",
        contract_version=1, state="complete", catalog=SessionCatalogSearchResultsDTO(revision="r1", catalog_revision=1),
    ))


@pytest.fixture
def service(capability: SearchProviderCapabilityDTO, query: AsyncMock) -> WorkspaceSearchService:
    service = WorkspaceSearchService("workspace-A")
    service.register(capability, query)
    return service


@pytest.fixture
def search_request() -> WorkspaceSearchRequest:
    return WorkspaceSearchRequest(workspace_id="workspace-A", category="catalog", query="needle")


def test_registration_rejects_conflicts_without_querying(
    service: WorkspaceSearchService, capability: SearchProviderCapabilityDTO, query: AsyncMock,
) -> None:
    assert service.capabilities().providers == [capability]
    with pytest.raises(ValueError, match="类别／操作已注册"):
        service.register(capability.model_copy(update={"provider_id": "other"}), query)
    with pytest.raises(ValueError, match="身份已注册"):
        service.register(capability.model_copy(update={"category": "thread"}), query)
    query.assert_not_called()


async def test_multiple_operations_share_one_registration_and_release(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    release = AsyncMock()
    service.register(capability.model_copy(update={"operations": ("name", "resolve")}), query, release=release)
    assert len(service.capabilities().providers) == 1
    for operation in ("name", "resolve"):
        await service.search(search_request.model_copy(update={"operation": operation}))
        assert query.await_args.args[0].operation == operation
    await service.aclose()
    release.assert_awaited_once()
    assert service.capabilities().providers == []
    with pytest.raises(SearchContractError):
        await service.search(search_request)


@pytest.mark.parametrize("updates,code", [
    ({"workspace_id": "workspace-B"}, "owner_mismatch"),
    ({"category": "message"}, "provider_unavailable"),
    ({"operation": "content"}, "provider_unavailable"),
    ({"contract_version": 2}, "unsupported_version"),
    ({"match_mode": "regex"}, "unsupported_option"),
    ({"limit": 101}, "invalid_query"),
    ({"query": "  ", "limit": 0}, "invalid_query"),
])
async def test_unsupported_contract_fails_before_owner(
    service: WorkspaceSearchService, search_request: WorkspaceSearchRequest, query: AsyncMock,
    updates: dict[str, object], code: str,
) -> None:
    with pytest.raises(SearchContractError) as caught:
        await service.search(search_request.model_copy(update=updates))
    assert caught.value.code == code
    query.assert_not_called()


async def test_zero_quota_is_limited_and_does_not_scan(
    service: WorkspaceSearchService, search_request: WorkspaceSearchRequest, query: AsyncMock,
) -> None:
    result = await service.search(search_request.model_copy(update={"limit": 0}))
    assert result.state == "limited" and result.limited_reason == "output_limit"
    assert result.catalog is result.threads is None
    query.assert_not_called()


@pytest.fixture
def worker() -> Iterator[tuple[threading.Event, threading.Event]]:
    started, release = threading.Event(), threading.Event()
    try:
        yield started, release
    finally:
        release.set()


async def test_unregister_waits_for_worker_and_suppresses_late_result(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
    worker: tuple[threading.Event, threading.Event],
) -> None:
    started, release = worker
    settled = asyncio.Event()
    released = AsyncMock()

    def read() -> None:
        started.set()
        assert release.wait(5), "测试 worker 未获释放"

    async def blocked(search_request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        try:
            await run_sync_shielded(read)
        finally:
            settled.set()
        return await query(search_request, gateway_id)

    service = WorkspaceSearchService("workspace-A")
    registration = service.register(capability, blocked, release=released)
    pending = asyncio.create_task(service.search(search_request))
    assert await asyncio.to_thread(started.wait, 2)
    closing = asyncio.create_task(registration.aclose())
    while registration.active:
        await asyncio.sleep(0)
    assert service.capabilities().providers == []
    assert not closing.done() and not settled.is_set()
    released.assert_not_called()
    with pytest.raises(SearchContractError, match="能力不可用"):
        await service.search(search_request)
    with pytest.raises(ValueError, match="已注册"):
        service.register(capability, query)
    release.set()
    await closing
    assert settled.is_set()
    released.assert_awaited_once()
    with pytest.raises(SearchContractError) as caught:
        await pending
    assert caught.value.code == "provider_unavailable"
    query.assert_not_called()
    service.register(capability, query)
    assert (await service.search(search_request)).state == "complete"
    await service.aclose()


async def test_release_failure_is_visible_and_does_not_allow_replacement(
    capability: SearchProviderCapabilityDTO, query: AsyncMock,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    registration = service.register(capability, query, release=AsyncMock(side_effect=OSError("资源释放失败")))
    with pytest.raises(OSError, match="资源释放失败"):
        await registration.aclose()
    assert not registration.active
    with pytest.raises(ValueError, match="已注册"):
        service.register(capability, query)
    query.assert_not_called()


async def test_unregister_rejects_result_even_if_owner_ignores_cancellation(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
) -> None:
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(search_request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return await query(search_request, gateway_id)

    service = WorkspaceSearchService("workspace-A")
    registration = service.register(capability, slow)
    pending = asyncio.create_task(service.search(search_request))
    await started.wait()
    closing = asyncio.create_task(registration.aclose())
    await cancelled.wait()
    release.set()
    await closing
    with pytest.raises(SearchContractError) as caught:
        await pending
    assert caught.value.code == "provider_unavailable"
