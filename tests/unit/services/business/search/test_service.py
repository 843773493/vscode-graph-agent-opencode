"""注册、能力与安全结算；领域匹配另由原 owner 证明。"""

import asyncio
import sqlite3
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
    SearchProviderReadError,
    WorkspaceSearchService,
)


@pytest.fixture
def capability() -> SearchProviderCapabilityDTO:
    return SearchProviderCapabilityDTO(
        provider_id="test-catalog", category="catalog", operations=("name",), contract_version=1,
        supported_options=("match_mode",), position_kind="catalog", max_results=100, cache_policy="none", match_modes=("fuzzy",),
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
        assert query.await_args is not None
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


@pytest.mark.parametrize("error", [PermissionError(13, "Permission denied", "/target/project/log.json"),
                                    sqlite3.DatabaseError("SQL 内部正文不应作为公开说明")])
async def test_read_failure_keeps_registered_owner_and_original_cause(
    service: WorkspaceSearchService, search_request: WorkspaceSearchRequest, query: AsyncMock, error: Exception,
) -> None:
    query.side_effect = error
    with pytest.raises(SearchProviderReadError) as caught:
        await service.search(search_request)
    assert caught.value.__cause__ is error
    detail = caught.value.detail()
    assert detail["owner"] == "search_provider" and detail["code"] == "provider_read_failed"
    assert (detail["provider_id"], detail["category"], detail["operation"]) == ("test-catalog", "catalog", "name")
    if isinstance(error, OSError):
        assert error.filename is not None
        assert error.filename in detail["message"] and "Permission denied" in detail["message"]
    else:
        assert str(error) not in detail["message"]


@pytest.mark.parametrize("error", [TimeoutError("期限已到"), RuntimeError("内部实现错误")])
async def test_deadline_and_programming_error_keep_original_identity(
    service: WorkspaceSearchService, search_request: WorkspaceSearchRequest, query: AsyncMock, error: BaseException,
) -> None:
    query.side_effect = error
    with pytest.raises(type(error)) as caught:
        await service.search(search_request)
    assert caught.value is error


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


async def test_service_close_waits_for_all_original_owners_after_release_failure(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    failure = OSError("第一个 owner 释放失败")
    first_release, second_release = AsyncMock(side_effect=failure), AsyncMock()
    started, settling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def second_owner(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settling.set()
            await finish.wait()
        return await query(request, gateway_id)

    service.register(capability, query, release=first_release)
    second = service.register(capability.model_copy(update={
        "provider_id": "second-owner", "operations": ("resolve",),
    }), second_owner, release=second_release)
    pending = asyncio.create_task(service.search(search_request.model_copy(update={"operation": "resolve"})))
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(service.aclose())
    try:
        await asyncio.wait_for(settling.wait(), 1)
        assert service.capabilities().providers == []
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.02)
        second_release.assert_not_called()
        finish.set()
        with pytest.raises(OSError) as caught:
            await closing
        assert caught.value is failure
        first_release.assert_awaited_once()
        second_release.assert_awaited_once()
        with pytest.raises(SearchContractError) as unavailable:
            await pending
        assert unavailable.value.code == "provider_unavailable"
        query.assert_not_called()
    finally:
        finish.set()
        await asyncio.gather(pending, closing, second.aclose(), return_exceptions=True)


@pytest.mark.parametrize("failures", [
    (OSError("owner A 释放失败"), ValueError("owner B 释放失败")),
    (asyncio.CancelledError("owner A 原取消"), OSError("owner B 释放失败")),
    (OSError("owner A 释放失败"), asyncio.CancelledError("owner B 原取消")),
])
async def test_service_close_keeps_all_original_failures_without_releasing_twice(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, failures: tuple[BaseException, BaseException],
) -> None:
    service = WorkspaceSearchService("workspace-A")
    first_release, second_release = (AsyncMock(side_effect=error) for error in failures)
    first = service.register(capability, query, release=first_release)
    second = service.register(capability.model_copy(update={
        "provider_id": "second-owner", "operations": ("resolve",),
    }), AsyncMock(), release=second_release)
    for _ in range(2):
        with pytest.raises(BaseExceptionGroup) as caught:
            await service.aclose()
        assert len(caught.value.exceptions) == 2
        assert all(actual is original for actual, original in zip(caught.value.exceptions, failures, strict=True))
    for registration, original in zip((first, second), failures, strict=True):
        with pytest.raises(type(original)) as observed:
            await registration.aclose()
        assert observed.value is original
    first_release.assert_awaited_once()
    second_release.assert_awaited_once()
    assert service.capabilities().providers == []
    with pytest.raises(SearchContractError) as unavailable:
        service.register(capability, query)
    assert unavailable.value.code == "provider_unavailable"
    query.assert_not_called()


@pytest.mark.parametrize("release_fails", [False, True])
async def test_cancelled_service_close_settles_all_owners_and_keeps_original_cancellation(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, release_fails: bool,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    failures = (OSError("owner A 释放失败"), ValueError("owner B 释放失败"))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def release_second() -> None:
        entered.set()
        await finish.wait()
        if release_fails:
            raise failures[1]

    first_release = AsyncMock(side_effect=failures[0] if release_fails else None)
    second_release = AsyncMock(side_effect=release_second)
    service.register(capability, query, release=first_release)
    service.register(capability.model_copy(update={
        "provider_id": "second-owner", "operations": ("resolve",),
    }), AsyncMock(), release=second_release)
    closing = asyncio.create_task(service.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel("原关闭调用方取消")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.02)
        closing.cancel("后续取消不能替换原取消")
        finish.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await closing
        assert caught.value.args == ("原关闭调用方取消",)
        if release_fails:
            cause = caught.value.__cause__
            assert isinstance(cause, BaseExceptionGroup)
            assert all(actual is original for actual, original in zip(cause.exceptions, failures, strict=True))
        else:
            assert caught.value.__cause__ is None
        first_release.assert_awaited_once()
        second_release.assert_awaited_once()
        query.assert_not_called()
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_concurrent_unregister_and_repeated_service_close_release_each_owner_once(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    release_entered, started, cancelled, finish = (asyncio.Event() for _ in range(4))

    async def release_first() -> None:
        release_entered.set()
        await finish.wait()

    async def late_owner(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()
        return await query(request, gateway_id)

    first_release, second_release = AsyncMock(side_effect=release_first), AsyncMock()
    first_query = AsyncMock()
    first = service.register(capability, first_query, release=first_release)
    second = service.register(capability.model_copy(update={
        "provider_id": "second-owner", "operations": ("resolve",),
    }), late_owner, release=second_release)
    pending = asyncio.create_task(service.search(search_request.model_copy(update={"operation": "resolve"})))
    await asyncio.wait_for(started.wait(), 1)
    unregister = asyncio.create_task(first.aclose())
    await asyncio.wait_for(release_entered.wait(), 1)
    closing = [asyncio.create_task(service.aclose()) for _ in range(2)]
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not unregister.done() and all(not task.done() for task in closing)
        assert service.capabilities().providers == []
        with pytest.raises(SearchContractError) as unavailable:
            await service.search(search_request)
        assert unavailable.value.code == "provider_unavailable"
        finish.set()
        await asyncio.gather(unregister, *closing)
        with pytest.raises(SearchContractError) as late:
            await pending
        assert late.value.code == "provider_unavailable"
        await first.aclose()
        await second.aclose()
        await service.aclose()
        first_release.assert_awaited_once()
        second_release.assert_awaited_once()
        first_query.assert_not_called()
        query.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(pending, unregister, *closing, return_exceptions=True)


async def test_query_cancellation_keeps_its_identity_and_does_not_unregister_provider(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
    worker: tuple[threading.Event, threading.Event],
) -> None:
    started, finish = worker

    def read() -> None:
        started.set()
        assert finish.wait(5), "测试 worker 未获释放"

    async def owner(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        await run_sync_shielded(read)
        return await query(request, gateway_id)

    service = WorkspaceSearchService("workspace-A")
    released = AsyncMock()
    registration = service.register(capability, owner, release=released)
    pending = asyncio.create_task(service.search(search_request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel("原查询取消")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.02)
        finish.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await pending
        assert caught.value.args == ("原查询取消",)
        assert registration.active and service.capabilities().providers == [capability]
        query.assert_not_called()
        assert (await service.search(search_request)).state == "complete"
        query.assert_awaited_once()
        await service.aclose()
        released.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(pending, service.aclose(), return_exceptions=True)


@pytest.mark.parametrize("close_provider", [False, True])
@pytest.mark.parametrize("failure_type", [OSError, sqlite3.DatabaseError, RuntimeError])
async def test_query_cancellation_keeps_owner_settlement_failure_as_cause(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, search_request: WorkspaceSearchRequest,
    worker: tuple[threading.Event, threading.Event], failure_type: type[Exception], close_provider: bool,
) -> None:
    started, finish = worker
    failure = failure_type("owner 读取结算失败")
    settled = asyncio.Event()
    released = AsyncMock()

    def read() -> None:
        started.set()
        assert finish.wait(5), "测试 worker 未获释放"
        raise failure

    async def owner(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
        try:
            await run_sync_shielded(read)
        finally:
            settled.set()
        return await query(request, gateway_id)

    service = WorkspaceSearchService("workspace-A")
    registration = service.register(capability, owner, release=released)
    pending = asyncio.create_task(service.search(search_request))
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel("原查询取消")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.02)
        assert not settled.is_set()
        released.assert_not_called()
        pending.cancel("后续取消不能替换原取消")
        if close_provider:
            registration.close_admission()
            closing = asyncio.create_task(registration.aclose())
            with pytest.raises(SearchContractError) as unavailable:
                await service.search(search_request)
            assert unavailable.value.code == "provider_unavailable"
            assert service.capabilities().providers == []
            released.assert_not_called()
        finish.set()
        if close_provider:
            with pytest.raises(SearchContractError) as closed:
                await pending
            assert closed.value.code == "provider_unavailable"
            assert closed.value.__cause__ is failure
            assert closing is not None
            await closing
        else:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await pending
            assert cancelled.value.args == ("原查询取消",)
            assert cancelled.value.__cause__ is failure
            assert registration.active and service.capabilities().providers == [capability]
        assert settled.is_set() and registration.tasks == set()
        query.assert_not_called()
        await service.aclose()
        released.assert_awaited_once()
    finally:
        finish.set()
        tasks = [pending, asyncio.create_task(service.aclose())]
        if closing is not None:
            tasks.append(closing)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("release_fails", [False, True])
async def test_cancelled_registration_observer_keeps_original_result_for_later_close(
    capability: SearchProviderCapabilityDTO, query: AsyncMock, release_fails: bool,
) -> None:
    service = WorkspaceSearchService("workspace-A")
    failure = OSError("原 registration 释放失败")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def release() -> None:
        entered.set()
        await finish.wait()
        if release_fails:
            raise failure

    released = AsyncMock(side_effect=release)
    registration = service.register(capability, query, release=released)
    closing = asyncio.create_task(registration.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel("原注销观察者取消")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.02)
        closing.cancel("后续取消")
        finish.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await closing
        assert caught.value.args == ("原注销观察者取消",)
        assert caught.value.__cause__ is (failure if release_fails else None)
        if release_fails:
            with pytest.raises(OSError) as observed:
                await registration.aclose()
            assert observed.value is failure
        else:
            await registration.aclose()
        released.assert_awaited_once()
        assert not registration.active and service.capabilities().providers == []
        query.assert_not_called()
    finally:
        finish.set()
        await asyncio.gather(closing, service.aclose(), return_exceptions=True)
