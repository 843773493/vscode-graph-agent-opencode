from __future__ import annotations

from pathlib import Path

import pytest

from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


class _FailingHttpClient:
    async def get(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("backend_url 为空时不应发出 HTTP 请求")


def _offline_target() -> WorkspaceTarget:
    return WorkspaceTarget(
        workspace_id="gw_offline_without_backend",
        name="未连接工作区",
        root_path="/tmp/offline-without-backend",
        backend_url="",
        connection_kind="local",
        managed=True,
    )


@pytest.mark.asyncio
async def test_catalog_sync_skips_local_workspace_without_backend_url(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    target = _offline_target()
    registry.upsert(target)
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )

    await service._sync_all()

    assert service._workspace_errors[target.workspace_id] == (
        f"工作区后端尚未连接: {target.workspace_id}"
    )


@pytest.mark.asyncio
async def test_catalog_search_reports_unconnected_workspace_without_malformed_url(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    target = _offline_target()
    registry.upsert(target)
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )

    result = await service.search(
        "未连接",
        limit_per_workspace=10,
        request_id="req_catalog_test",
    )

    assert result.items == []
    assert result.workspaces[0].status == "unavailable"
    assert result.workspaces[0].error == (
        f"RuntimeError: 工作区后端尚未连接: {target.workspace_id}"
    )
