"""连接操作的真实 HTTP 期限与发布前 fencing，不用 helper 伪造授权状态。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI

from app.api import mcp as mcp_api
from app.api.deps import get_mcp_connection_service, verify_local_token
from app.core.lifecycle import LifetimeScope
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.schemas.internal_v2.mcp import McpConnectionRequestDTO
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import connection_service
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.mcp.operation_store import McpConnectionOperationStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 唯一真实 OAuth 资产
)
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def connection_case(request: pytest.FixtureRequest):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = prepare_test_workspace(
        workspace_root=context.workspace_root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=(),
    )

    @asynccontextmanager
    async def create(endpoint):
        async with AsyncExitStack() as cleanup:
            store = WorkspaceStateStore(workspace_root=root)
            cleanup.callback(store.close)
            owner = McpCatalogOwner(
                raw_config={"servers": {"secure": endpoint.configuration()}}, workspace_root=root,
                event_service=EventChannelService(), leases=ExternalResourceLeaseLedger(),
            )
            service = McpConnectionService(owner=owner, store=store, workspace_id="test-workspace")
            await owner.start()
            cleanup.push_async_callback(owner.shutdown)
            service.start()
            cleanup.push_async_callback(service.close)
            yield service, owner, store
    return create


async def observe(service, operation_id: str, status: str):
    async with asyncio.timeout(8):
        while True:
            operation = await service.status(operation_id, wait_timeout_ms=100)
            if operation.status == status:
                return operation
            assert not operation.terminal, operation


async def test_real_authorization_deadline_closes_candidate_without_publishing(
    oauth_endpoint_factory, connection_case, monkeypatch: pytest.MonkeyPatch,
):
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        monkeypatch.setattr("app.services.infrastructure.mcp.connection_service._OPERATION_TIMEOUT_SECONDS", 0.3)
        operation = await service.submit(McpConnectionRequestDTO(operation_id="expires", server_id="secure", action="connect"))
        await observe(service, "expires", "waiting_user")
        authorization = service.authorization("expires")
        expired = await observe(service, "expires", "expired")
        assert expired.error_code == "deadline_exceeded" and expired.stage == "authorization"
        assert await service.submit(McpConnectionRequestDTO(operation_id="expires", server_id="secure", action="connect")) == expired
        assert not owner.get_tool_ids() and endpoint.exchanges == 0
        state = parse_qs(urlparse(authorization.authorization_url).query)["state"][0]
        with pytest.raises(PermissionError):
            service.complete_authorization(operation.operation_id, code="late-code", state=state)


async def test_observer_deadline_and_cancellation_preserve_shared_authorization(
    oauth_endpoint_factory, connection_case,
):
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        operation = await service.submit(McpConnectionRequestDTO(operation_id="observed", server_id="secure", action="connect"))
        waiting = await observe(service, operation.operation_id, "waiting_user")
        async with asyncio.timeout(1):
            short = await service.wait_terminal(operation.operation_id, deadline=time.time() + 0.02)
        assert short == waiting and not short.terminal
        first = asyncio.create_task(service.wait_terminal(operation.operation_id, deadline=operation.deadline))
        second = asyncio.create_task(service.wait_terminal(operation.operation_id, deadline=operation.deadline))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done() and endpoint.exchanges == 0
        async with httpx.AsyncClient() as client:
            redirect = await client.get(service.authorization(operation.operation_id).authorization_url)
        callback = {name: values[0] for name, values in parse_qs(urlparse(redirect.headers["location"]).query).items()}
        service.complete_authorization(operation.operation_id, **callback)
        async with asyncio.timeout(8):
            succeeded = await second
        assert succeeded.status == "succeeded" and owner.get_tool_ids() == {"mcp__secure__probe"}
        # 终态先于新观察者登记，不能等待下一条事件，也不能续期。
        async with asyncio.timeout(0.1):
            assert await service.wait_terminal(operation.operation_id, deadline=time.time() - 1) == succeeded
        assert endpoint.exchanges == endpoint.registrations == 1


async def test_changed_configuration_fences_late_authorization_before_exchange(
    oauth_endpoint_factory, connection_case,
):
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        operation = await service.submit(McpConnectionRequestDTO(operation_id="changed", server_id="secure", action="connect"))
        await observe(service, operation.operation_id, "waiting_user")
        authorization = service.authorization(operation.operation_id)
        scope = LifetimeScope("mcp-config-change")
        try:
            publish, _ = await owner.prepare_config({"servers": {}}, scope, lambda _effect: None)
            publish()
            state = parse_qs(urlparse(authorization.authorization_url).query)["state"][0]
            with pytest.raises(LookupError):
                service.complete_authorization(operation.operation_id, code="late-code", state=state)
            assert (await service.cancel(operation.operation_id)).status == "cancelled"
            assert endpoint.exchanges == 0 and not owner.get_tool_ids()
        finally:
            await scope.close()


async def test_disconnect_cleanup_failure_preserves_unavailable_catalog_and_stage(
    oauth_endpoint_factory, connection_case,
):
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        await service.submit(McpConnectionRequestDTO(operation_id="connect", server_id="secure", action="connect"))
        await observe(service, "connect", "waiting_user")
        async with httpx.AsyncClient() as client:
            redirect = await client.get(service.authorization("connect").authorization_url)
        callback = {name: items[0] for name, items in parse_qs(urlparse(redirect.headers["location"]).query).items()}
        service.complete_authorization("connect", **callback)
        await observe(service, "connect", "succeeded")
        fail = True

        def cleanup() -> None:
            if fail:
                raise RuntimeError("private-path-and-token-must-not-leak")

        owner._runtimes["secure"].scope.register(cleanup, label="injected-cleanup-failure")
        await service.submit(McpConnectionRequestDTO(operation_id="disconnect", server_id="secure", action="disconnect"))
        operation = await observe(service, "disconnect", "failed")
        assert operation.stage == "disconnect" and operation.error_code == "ExceptionGroup"
        assert "private-path" not in json.dumps(operation.model_dump())
        assert not owner.get_tool_ids() and owner.list_servers()[0].status != "ready"
        fail = False


@pytest.fixture
def held_connection_cleanup(monkeypatch: pytest.MonkeyPatch):
    """只暂停真实 SDK 的清理交付，不替产品创建或结束连接操作。"""
    entered, release = asyncio.Event(), asyncio.Event()
    original = connection_service.production_mcp_session_factory

    @asynccontextmanager
    async def held(*args, **kwargs):
        try:
            async with original(*args, **kwargs) as session:
                yield session
        finally:
            entered.set()
            await release.wait()

    monkeypatch.setattr(connection_service, "production_mcp_session_factory", held)
    return entered, release


@pytest.fixture
def connection_api():
    @asynccontextmanager
    async def create(service):
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.exception_handler(mcp_api.HTTPException)(workspace_http_exception_handler)
        app.include_router(mcp_api.router, prefix="/api/v1")
        app.dependency_overrides[get_mcp_connection_service] = lambda: service
        app.dependency_overrides[verify_local_token] = lambda: "local-test"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    return create


async def test_repeated_cancel_does_not_interrupt_original_cleanup(
    oauth_endpoint_factory, connection_case, held_connection_cleanup,
):
    entered, release = held_connection_cleanup
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        await service.submit(McpConnectionRequestDTO(operation_id="cancel-once", server_id="secure", action="connect"))
        await observe(service, "cancel-once", "waiting_user")
        first = asyncio.create_task(service.cancel("cancel-once"))
        second = None
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            second = asyncio.create_task(service.cancel("cancel-once"))
            done, _ = await asyncio.wait((first, second), timeout=0.05)
            assert not done, "重复取消不得让原操作在清理交付前结束"
            assert not owner.get_tool_ids() and endpoint.exchanges == 0
        finally:
            release.set()
            async with asyncio.timeout(5):
                results = await asyncio.gather(first, *(() if second is None else (second,)))
        assert all(result.status == "cancelled" for result in results)


async def test_cancel_request_interruption_preserves_original_cleanup(
    oauth_endpoint_factory, connection_case, held_connection_cleanup,
):
    entered, release = held_connection_cleanup
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        await service.submit(McpConnectionRequestDTO(operation_id="observer-cancelled", server_id="secure", action="connect"))
        await observe(service, "observer-cancelled", "waiting_user")
        cancellation = asyncio.create_task(service.cancel("observer-cancelled"))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            original_task = service._tasks["observer-cancelled"]
            cancellation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancellation
            assert not original_task.done() and service._tasks["observer-cancelled"] is original_task
            with pytest.raises(PermissionError, match="清理"):
                await service.submit(McpConnectionRequestDTO(operation_id="observer-new", server_id="secure", action="connect"))
        finally:
            release.set()
            async with asyncio.timeout(5):
                await service.cancel("observer-cancelled")
        assert not owner.get_tool_ids() and endpoint.exchanges == 0


@pytest.mark.parametrize("outcome", ["cancelled", "expired"])
async def test_failed_cleanup_retains_handle_and_blocks_new_connection(
    oauth_endpoint_factory, connection_case, monkeypatch: pytest.MonkeyPatch, outcome: str,
):
    original = connection_service.production_mcp_session_factory

    @asynccontextmanager
    async def failing(*args, **kwargs):
        try:
            async with original(*args, **kwargs) as session:
                yield session
        finally:
            raise RuntimeError("private-cleanup-marker")

    async with oauth_endpoint_factory() as endpoint:
        case = connection_case(endpoint)
        service, owner, store = await case.__aenter__()
        try:
            monkeypatch.setattr(connection_service, "production_mcp_session_factory", failing)
            if outcome == "expired":
                monkeypatch.setattr(connection_service, "_OPERATION_TIMEOUT_SECONDS", 1.0)
            await service.submit(McpConnectionRequestDTO(operation_id="failed-cleanup", server_id="secure", action="connect"))
            await observe(service, "failed-cleanup", "waiting_user")
            if outcome == "expired":
                await observe(service, "failed-cleanup", "expired")
            with pytest.raises(RuntimeError, match="清理未完成"):
                await service.cancel("failed-cleanup")
            await asyncio.sleep(0)  # 已完成回调不能删除失败句柄。
            assert service._tasks["failed-cleanup"].done()
            operation = McpConnectionOperationStore(store).get("failed-cleanup")
            assert operation.status == outcome and operation.stage == "cleanup"
            assert "private-cleanup-marker" not in operation.model_dump_json()
            with pytest.raises(PermissionError, match="清理"):
                await service.submit(McpConnectionRequestDTO(operation_id="after-failed-cleanup", server_id="secure", action="connect"))
            assert not owner.get_tool_ids() and endpoint.exchanges == 0
        finally:
            # 单独核对 teardown，不能让预期清理异常遮住上面的业务断言。
            with pytest.raises(ExceptionGroup):
                await case.__aexit__(None, None, None)


async def test_terminal_operation_blocks_new_intent_until_cleanup(
    oauth_endpoint_factory, connection_case, held_connection_cleanup,
):
    entered, release = held_connection_cleanup
    async with oauth_endpoint_factory() as endpoint, connection_case(endpoint) as (service, owner, _store):
        request = McpConnectionRequestDTO(operation_id="cleanup-original", server_id="secure", action="connect")
        await service.submit(request)
        await observe(service, request.operation_id, "waiting_user")
        cancellation = asyncio.create_task(service.cancel(request.operation_id))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            cancelled = await service.status(request.operation_id)
            assert cancelled.status == "cancelled"
            assert await service.submit(request) == cancelled
            with pytest.raises(PermissionError, match="清理"):
                await service.submit(request.model_copy(update={"operation_id": "cleanup-new"}))
            with pytest.raises(LookupError):
                await service.status("cleanup-new")
            assert not owner.get_tool_ids() and endpoint.exchanges == 0
        finally:
            release.set()
            async with asyncio.timeout(5):
                await cancellation


async def test_cancel_wait_is_bounded_without_abandoning_cleanup(
    oauth_endpoint_factory, connection_case, held_connection_cleanup, connection_api,
):
    entered, release = held_connection_cleanup
    async with (
        oauth_endpoint_factory() as endpoint,
        connection_case(endpoint) as (service, owner, store),
        connection_api(service) as client,
    ):
        request = McpConnectionRequestDTO(operation_id="cancel-deadline", server_id="secure", action="connect")
        await service.submit(request)
        await observe(service, request.operation_id, "waiting_user")
        alias = request.model_copy(update={"operation_id": "cancel-alias"})
        assert (await service.submit(alias)).operation_id == request.operation_id
        started = time.monotonic()
        cancellation = asyncio.create_task(client.post(
            f"/api/v1/mcp/connection-operations/{alias.operation_id}/cancel",
            headers={"X-Request-ID": "cancel-cleanup-timeout"},
        ))
        retry = None
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            original_task = service._tasks[request.operation_id]
            async with asyncio.timeout(33):
                response = await cancellation
            elapsed = time.monotonic() - started
            assert 30 <= elapsed < 33
            assert response.status_code == 503 and "清理" in response.text
            assert response.json()["request_id"] == response.headers["X-Request-ID"] == "cancel-cleanup-timeout"
            assert service._tasks[request.operation_id] is original_task and not original_task.done()
            cancelled = await service.status(alias.operation_id)
            assert cancelled.status == "cancelled" and cancelled.stage == "cleanup"
            assert cancelled.error_code == "cleanup_wait_timeout"
            cold = McpConnectionOperationStore(store).get(alias.operation_id)
            assert cold == cancelled
            blocked = await client.post("/api/v1/mcp/connection-operations", json={
                **request.model_dump(), "operation_id": "blocked-during-cleanup",
            })
            assert blocked.status_code == 409 and "清理" in blocked.text
            retry = asyncio.create_task(service.cancel(alias.operation_id))
            await asyncio.sleep(0)
            assert not original_task.done() and not retry.done()
        finally:
            release.set()
            async with asyncio.timeout(5):
                await asyncio.gather(cancellation, *(() if retry is None else (retry,)), return_exceptions=True)
        assert retry is not None and (await retry).status == "cancelled"
        assert not owner.get_tool_ids() and endpoint.exchanges == 0
        next_operation = await service.submit(request.model_copy(update={"operation_id": "after-cleanup"}))
        assert next_operation.operation_id == "after-cleanup"
        await observe(service, next_operation.operation_id, "waiting_user")
        async with httpx.AsyncClient() as client:
            redirect = await client.get(service.authorization(next_operation.operation_id).authorization_url)
        callback = {name: items[0] for name, items in parse_qs(urlparse(redirect.headers["location"]).query).items()}
        service.complete_authorization(next_operation.operation_id, **callback)
        assert (await observe(service, next_operation.operation_id, "succeeded")).status == "succeeded"
        assert owner.get_tool_ids() == {"mcp__secure__probe"}
        assert endpoint.exchanges == 1
