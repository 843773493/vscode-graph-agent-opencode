"""真实 HTTP 与官方 SDK 的首次授权、PKCE、冷刷新和日志边界。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import partial
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from langchain_core.tools import tool

from app.agents.policy.tool_capability import ToolCapabilityPolicy
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.catalog_activation import (
    McpCatalogResourcePublisher,
)
from app.services.infrastructure.mcp.catalog_owner import (
    McpCatalogError,
    McpCatalogOwner,
)
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.oauth import McpOAuthProvider
from app.services.infrastructure.mcp.session_runtime import (
    production_mcp_session_factory,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from tests.integration.workspace_services.mcp.oauth_server import OAuthEndpoint
from tests.integration.workspace_services.mcp.oauth_server import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 共享真实 HTTP 资产
)


@pytest.fixture
def owner_factory():
    def create(endpoint: OAuthEndpoint, *, provider: McpOAuthProvider | None = None):
        factory = None if provider is None else partial(
            production_mcp_session_factory, workspace_root=endpoint.context.workspace_root, oauth_provider=provider,
        )
        return McpCatalogOwner(
            raw_config={"servers": {"secure": endpoint.configuration()}},
            workspace_root=endpoint.context.workspace_root,
            event_service=EventChannelService(), leases=ExternalResourceLeaseLedger(), session_factory=factory,
        )
    return create


@pytest.fixture
def provider_factory():
    def create(endpoint: OAuthEndpoint, *, wrong_state: bool = False):
        server, = parse_mcp_server_configs({"servers": {"secure": endpoint.configuration()}}, workspace_root=endpoint.context.workspace_root)
        callback: tuple[str, str | None] | None = None

        async def redirect(url: str) -> None:
            nonlocal callback
            async with httpx.AsyncClient() as browser:
                response = await browser.get(url)
            assert response.status_code == 303
            values = parse_qs(urlparse(response.headers["location"]).query)
            callback = (values["code"][0], "private-incorrect-state" if wrong_state else values["state"][0])

        async def receive() -> tuple[str, str | None]:
            assert callback is not None
            return callback

        return McpOAuthProvider(server=server, workspace_root=endpoint.context.workspace_root,
                                redirect_handler=redirect, callback_handler=receive)
    return create


async def test_supported_oauth_startup_keeps_local_catalog_available(oauth_endpoint_factory, owner_factory):
    async with oauth_endpoint_factory() as endpoint:
        owner = owner_factory(endpoint)
        await owner.start()
        try:
            assert owner.started and owner.publication_snapshot().status == "available"
            server, = owner.list_servers()
            assert server.status == "authorization_required" and server.tools == ()
            assert owner.get_tool_ids() == frozenset()
            assert endpoint.registrations == 1 and endpoint.exchanges == 0

            @tool
            def local_control() -> str:
                """本地控制资产，验证不依赖远端 session。"""
                return "local-ready"

            registry = ResourceRegistry()
            publisher = McpCatalogResourcePublisher(
                registry=registry, custom_tools=[local_control],
                policy_resolver=lambda metadata: {item.tool_id: ToolCapabilityPolicy(
                    execution_enabled=True, model_visible=False, confirmation_required=False,
                    timeout_ms=10000, max_result_bytes=10000, execution_locked=False, model_visibility_locked=False,
                ) for item in metadata},
            )
            unsubscribe = owner.subscribe_publication(publisher.publish)
            try:
                snapshot = registry.last_valid_snapshot("mcp-tool-catalog:v1")
                assert snapshot.available and [member.member_id for member in snapshot.members] == ["local_control"]
            finally:
                unsubscribe()
        finally:
            await owner.shutdown()


async def test_sdk_pkce_authorization_then_cold_start_and_expired_token_refresh(
    oauth_endpoint_factory, owner_factory, provider_factory,
):
    async with oauth_endpoint_factory() as endpoint:
        credentials = endpoint.context.workspace_root / ".boxteam/credentials/mcp-oauth"
        before = set(credentials.glob("*.json"))
        endpoint.expires_in = 1
        initial = owner_factory(endpoint)
        await initial.start()
        await initial.shutdown()
        owner = owner_factory(endpoint, provider=provider_factory(endpoint))
        await owner.start()
        assert owner.list_servers()[0].status == "ready"
        assert owner.get_tool_ids() == {"mcp__secure__probe"}
        assert endpoint.exchanges == endpoint.registrations == 1
        await owner.shutdown()
        # 冷启动必须恢复原绝对期限与非默认 token endpoint，不能延长一秒凭据。
        await asyncio.sleep(1.1)
        cold = owner_factory(endpoint)
        await cold.start()
        try:
            assert cold.list_servers()[0].status == "ready"
            assert endpoint.refreshes == 1 and endpoint.exchanges == endpoint.registrations == 1
            files = list(set(credentials.glob("*.json")) - before)
            assert len(files) == 1
            if os.name != "nt":
                assert files[0].stat().st_mode & 0o777 == 0o600
                assert files[0].parent.stat().st_mode & 0o777 == 0o700
        finally:
            await cold.shutdown()


@pytest.mark.parametrize("failure", ["metadata", "issuer", "pkce"])
async def test_unknown_authentication_never_becomes_authorization_required(
    oauth_endpoint_factory, owner_factory, failure: str,
):
    async with oauth_endpoint_factory() as endpoint:
        setattr(endpoint, {"metadata": "metadata_enabled", "issuer": "issuer_matches", "pkce": "pkce_supported"}[failure], False)
        owner = owner_factory(endpoint)
        with pytest.raises(McpCatalogError):
            await owner.start()
        assert not owner.started
        assert endpoint.registrations == endpoint.exchanges == 0


@pytest.mark.parametrize("failure", ["state", "token"])
async def test_sdk_failure_cannot_publish_ready_or_leak_exchange_data(
    oauth_endpoint_factory, owner_factory, provider_factory, caplog: pytest.LogCaptureFixture, failure: str,
):
    caplog.set_level(logging.DEBUG, logger="mcp.client.auth.oauth2")
    async with oauth_endpoint_factory() as endpoint:
        endpoint.fail_token = failure == "token"
        owner = owner_factory(endpoint, provider=provider_factory(endpoint, wrong_state=failure == "state"))
        with pytest.raises(McpCatalogError) as caught:
            await owner.start()
        assert not owner.started and endpoint.exchanges == 0
        assert all(secret not in caplog.text + str(caught.value) for secret in (
            endpoint.private_marker, endpoint.access_token, endpoint.refresh_token, "private-incorrect-state",
        ))
        assert any("MCP OAuth SDK" in record.message for record in caplog.records)


async def test_successful_token_exchange_does_not_hide_invalid_mcp_catalog(
    oauth_endpoint_factory, owner_factory, provider_factory,
):
    async with oauth_endpoint_factory() as endpoint:
        endpoint.definitions.write_text(json.dumps({"probe": {"type": "invalid-type"}}), encoding="utf-8")
        owner = owner_factory(endpoint, provider=provider_factory(endpoint))
        with pytest.raises(McpCatalogError):
            await owner.start()
        assert endpoint.exchanges == 1 and not owner.started


async def test_same_server_name_on_another_endpoint_never_borrows_credentials(
    oauth_endpoint_factory, owner_factory, provider_factory,
):
    async with oauth_endpoint_factory("first") as first, oauth_endpoint_factory("second") as second:
        first_owner = owner_factory(first, provider=provider_factory(first))
        await first_owner.start()
        await first_owner.shutdown()
        second_owner = owner_factory(second)
        await second_owner.start()
        try:
            assert second_owner.list_servers()[0].status == "authorization_required"
            assert second.registrations == 1 and second.exchanges == 0
            assert first.exchanges == 1
        finally:
            await second_owner.shutdown()
