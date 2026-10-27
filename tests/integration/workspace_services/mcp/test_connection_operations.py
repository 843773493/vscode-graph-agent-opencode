"""连接操作的真实 HTTP 期限与发布前 fencing，不用 helper 伪造授权状态。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.lifecycle import LifetimeScope
from app.schemas.internal_v2.mcp import McpConnectionRequestDTO
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
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
        store = WorkspaceStateStore(workspace_root=root)
        owner = McpCatalogOwner(
            raw_config={"servers": {"secure": endpoint.configuration()}}, workspace_root=root,
            event_service=EventChannelService(), leases=ExternalResourceLeaseLedger(),
        )
        service = McpConnectionService(owner=owner, store=store, workspace_id="test-workspace")
        await owner.start()
        service.start()
        try:
            yield service, owner, store
        finally:
            await service.close()
            await owner.shutdown()
            store.close()
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
