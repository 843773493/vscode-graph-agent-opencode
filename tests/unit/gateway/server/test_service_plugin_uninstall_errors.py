"""服务插件卸载的包状态错误映射。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.server import service_plugins


@asynccontextmanager
async def _mutation_guard() -> AsyncIterator[None]:
    yield


@pytest.fixture
async def uninstall_gateway(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, Mock]]:
    gateway_id = "gateway_" + "a" * 32
    owner = Mock()
    owner.references.return_value = []
    owner.packages.get.side_effect = FileNotFoundError(
        "插件包不存在: example.missing@9.0.0"
    )

    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "registry.json", gateway_id=gateway_id
    )
    app = FastAPI()
    app.include_router(service_plugins.router)
    app.state.registry = registry
    app.state.service_plugins = owner
    app.state.gateway_config_reload = SimpleNamespace(
        mutation_guard=_mutation_guard,
        config=SimpleNamespace(payload={}),
    )
    app.state.skill_source_owner = SimpleNamespace(
        read_references=Mock(return_value=[])
    )
    app.state.gateway_state = SimpleNamespace(
        get_pending_config_candidate=Mock(return_value=None)
    )
    app.state.workspace_plugin_bindings = Mock()
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(
        kind="local"
    )
    app.dependency_overrides[service_plugins.plugin_owner] = lambda: owner
    try:
        yield app, owner
    finally:
        registry.close()


async def test_uninstall_missing_package_returns_conflict_without_deleting(
    uninstall_gateway: tuple[FastAPI, Mock],
) -> None:
    app, owner = uninstall_gateway
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/gateway/service-plugins/uninstall",
            json={
                "gateway_id": "gateway_" + "a" * 32,
                "package_id": "example.missing",
                "package_version": "9.0.0",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "插件包不存在: example.missing@9.0.0"
    owner.packages.get.assert_called_once_with("example.missing", "9.0.0")
    owner.packages.uninstall.assert_not_called()
