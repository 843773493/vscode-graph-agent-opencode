from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

import app.gateway.lifespan as gateway_lifespan
from app.gateway.config import GatewayConfig
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeHealthProof,
)


class _TrackedTransport(httpx.MockTransport):
    def __init__(self, events: list[str], index: int) -> None:
        def unexpected_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"生命周期测试不应发送 HTTP 请求: {request.url}")

        super().__init__(unexpected_request)
        self.events = events
        self.index = index
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        self.events.append(f"pool-close-{self.index}")
        await super().aclose()


class _ControlStore:
    def __init__(self, *, database: object) -> None:
        self.database = database

    def close(self) -> None:
        pass


@dataclass
class _LifespanCase:
    app: FastAPI
    clients: list[httpx.AsyncClient]
    transports: list[_TrackedTransport]
    events: list[str]
    channel_owner: Mock


@pytest.fixture
def lifespan_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _LifespanCase:
    clients: list[httpx.AsyncClient] = []
    transports: list[_TrackedTransport] = []
    events: list[str] = []
    original_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        transport = _TrackedTransport(events, len(clients))
        client = original_client(transport=transport, **kwargs)
        clients.append(client)
        transports.append(transport)
        return client

    def drain(name: str) -> None:
        assert not clients[0].is_closed
        events.append(name)

    def service(name: str) -> Mock:
        return Mock(start=AsyncMock(), stop=AsyncMock(side_effect=lambda: drain(name)))

    state = Mock()
    state.get_active_config_snapshot.return_value = SimpleNamespace(active_revision=1)
    state.record_gateway_runtime_generation.return_value = SimpleNamespace(state="starting")
    state.update_gateway_runtime_generation.side_effect = lambda **kwargs: SimpleNamespace(state=kwargs["state"])
    state.close.side_effect = lambda: drain("state")
    registry = Mock(active_workspace_id=None)
    registry.targets.return_value = ()
    registry.close.side_effect = lambda **kwargs: drain("registry")
    config = GatewayConfig(
        payload={"logging": {"runtime": {"level": "info", "pretty": True}}}, revision="pool-lifecycle-config", default_workspace_skill_groups=[],
        gateway_process_health_poll_interval_seconds=1,
        gateway_process_health_request_timeout_seconds=1,
        gateway_process_connection_drain_timeout_seconds=1,
        session_catalog_refresh_interval_seconds=1, session_catalog_max_concurrency=1,
        session_catalog_request_timeout_seconds=1,
    )
    channel_owner = service("channel")
    port_forward = Mock(
        reconcile_workspaces=AsyncMock(), restore=AsyncMock(),
        close=AsyncMock(side_effect=lambda: drain("ssh")),
    )
    for name in ("configure_application_logging", "load_boxteam_env", "get_gateway_local_token"):
        monkeypatch.setattr(gateway_lifespan, name, Mock())
    monkeypatch.setattr(gateway_lifespan, "_gateway_root", lambda: tmp_path)
    monkeypatch.setattr(gateway_lifespan, "GatewayStateStore", Mock(return_value=state))
    monkeypatch.setattr(gateway_lifespan, "load_or_create_gateway_id", Mock(return_value="gateway_pool"))
    monkeypatch.setattr(gateway_lifespan, "load_gateway_config", Mock(return_value=config))
    monkeypatch.setattr(gateway_lifespan, "create_registry", AsyncMock(return_value=registry))
    logging_stage = GatewayRuntimeConsumerStage(
        consumer_id="runtime-logging", generation="pool-generation", prepare=lambda: None,
        apply=lambda: None, health=lambda: GatewayRuntimeHealthProof("runtime-logging", "pool-generation", "healthy", {}),
        promote=lambda: None, rollback=lambda: None,
    )
    monkeypatch.setattr(gateway_lifespan, "RuntimeLoggingConsumer", Mock(return_value=SimpleNamespace(stage=lambda: logging_stage)))
    monkeypatch.setattr(gateway_lifespan, "GatewayConfigReloadService", Mock(return_value=service("config")))
    monkeypatch.setattr(gateway_lifespan, "_gateway_federation_policy_payload", Mock(return_value={}))
    monkeypatch.setattr(gateway_lifespan, "FederationControlStore", _ControlStore)
    monkeypatch.setattr(gateway_lifespan, "federation_control_database", Mock(return_value=object()))
    monkeypatch.setattr(gateway_lifespan, "load_or_create_signing_key", Mock(return_value=object()))
    monkeypatch.setattr(gateway_lifespan, "FederationRpcService", lambda **kwargs: SimpleNamespace(session_main=kwargs["session_main"]))
    monkeypatch.setattr(gateway_lifespan, "FederationChannelOwner", Mock(return_value=channel_owner))
    monkeypatch.setattr(gateway_lifespan, "_refresh_federation_workspace_ports", Mock())
    monkeypatch.setattr(gateway_lifespan, "SshPortForwardManager", Mock(return_value=port_forward))
    for name in (
        "UserAccessService", "UserProfileStore", "UserViewStateStore", "FederationCredentialStore",
        "GatewayWorkspaceRuntimeController", "WorkspaceNavigationStore",
        "GatewayResourceCatalogService",
    ):
        monkeypatch.setattr(gateway_lifespan, name, Mock())
    for name, label in (
        ("GatewaySessionCatalogSearchService", "catalog"),
    ):
        monkeypatch.setattr(gateway_lifespan, name, Mock(return_value=service(label)))
    monkeypatch.setattr(gateway_lifespan, "_cleanup_user_access_periodically", AsyncMock())
    monkeypatch.setattr(gateway_lifespan, "_restore_managed_local_runtimes", AsyncMock())
    monkeypatch.setattr(gateway_lifespan, "_should_preserve_gateway_generation_for_handoff", Mock(return_value=False))
    monkeypatch.setattr(gateway_lifespan.httpx, "AsyncClient", client_factory)
    return _LifespanCase(FastAPI(), clients, transports, events, channel_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["rpc_constructor", "channel_start"])
async def test_gateway_pool_closes_once_on_startup_failure(
    lifespan_case: _LifespanCase, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    failure = RuntimeError("确定性 Gateway 启动失败")
    if failure_stage == "rpc_constructor":
        monkeypatch.setattr(gateway_lifespan, "FederationRpcService", Mock(side_effect=failure))
    else:
        lifespan_case.channel_owner.start.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        async with gateway_lifespan.lifespan(lifespan_case.app):
            pytest.fail("启动失败不能进入就绪状态")
    assert raised.value is failure
    assert lifespan_case.clients
    assert all(client.is_closed for client in lifespan_case.clients)
    assert all(transport.close_calls == 1 for transport in lifespan_case.transports)


@pytest.mark.asyncio
async def test_gateway_pool_stays_open_until_all_consumers_drain(lifespan_case: _LifespanCase) -> None:
    async with gateway_lifespan.lifespan(lifespan_case.app):
        assert lifespan_case.app.state.http_client is lifespan_case.clients[0]
        assert lifespan_case.app.state.federation_rpc_service.session_main._http_client is lifespan_case.clients[0]
        assert not lifespan_case.clients[0].is_closed
    assert all(client.is_closed for client in lifespan_case.clients)
    assert all(transport.close_calls == 1 for transport in lifespan_case.transports)
    assert lifespan_case.events[-1] == "pool-close-0"
    assert {"catalog", "channel", "ssh", "registry", "config", "state"} <= set(lifespan_case.events)
