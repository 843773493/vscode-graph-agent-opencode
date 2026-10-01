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


def test_catalog_runtime_config_updates_next_sync_and_wakes_loop(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )

    service.update_runtime_config(
        refresh_interval_seconds=3,
        max_concurrency=2,
        request_timeout_seconds=4,
    )

    assert service._refresh_interval_seconds == 3
    assert service._max_concurrency == 2
    assert service._request_timeout_seconds == 4
    assert service._wake_event.is_set()


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


def test_catalog_sync_lock_pool_is_bounded_and_stable(tmp_path: Path) -> None:
    """目录同步锁池必须恒定有界，且同一 workspace_id 恒得同一把锁。

    若退回按 workspace_id 键的 dict，删除工作区后键永不回收，进程内存随历史
    工作区无界增长；分片锁池则保证「同 id 同锁」的互斥红线且池大小恒定。
    """

    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )

    assert service._sync_lock_for("ws-a") is service._sync_lock_for("ws-a")
    distinct = {id(service._sync_lock_for(f"ws-{index}")) for index in range(5000)}
    assert len(distinct) <= 64
    assert len(service._sync_locks) <= 64


@pytest.mark.asyncio
async def test_catalog_caches_are_pruned_when_workspaces_are_removed(
    tmp_path: Path,
) -> None:
    """已删除工作区的目录快照/错误/新鲜度缓存必须回收，不随历史工作区无界增长。

    快照含该工作区全部会话节点，是三者中最重的；若只增不减，长驻 Gateway
    内存会随历史工作区单调增长。
    """

    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )

    for index in range(3000):
        workspace_id = f"ws_pruned_{index}"
        target = WorkspaceTarget(
            workspace_id=workspace_id,
            name=workspace_id,
            root_path=f"/tmp/{workspace_id}",
            backend_url="",
            connection_kind="local",
            managed=True,
        )
        registry.upsert(target)
        service._snapshots[workspace_id] = object()  # type: ignore[assignment]
        service._fresh_workspace_ids.add(workspace_id)
        service._workspace_errors[workspace_id] = "boom"
        registry.remove(workspace_id)

    # 上一轮每工作区已留残留；同步一轮后必须全部回收（没有任何存活工作区）。
    await service._sync_all()

    assert service._snapshots == {}
    assert service._fresh_workspace_ids == set()
    assert service._workspace_errors == {}


@pytest.mark.asyncio
async def test_catalog_caches_are_retained_for_live_workspaces(tmp_path: Path) -> None:
    """回收不得误删存活工作区的缓存：仍有 backend_url 的工作区状态必须保留语义。"""

    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    live = WorkspaceTarget(
        workspace_id="ws_live",
        name="存活工作区",
        root_path="/tmp/ws_live",
        backend_url="",
        connection_kind="local",
        managed=True,
    )
    registry.upsert(live)
    service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )
    service._snapshots["ws_live"] = object()  # type: ignore[assignment]
    service._workspace_errors["ws_live"] = "旧错误先占位"

    await service._sync_all()

    # 存活工作区的快照保留；其 error 由本轮同步结果覆盖（无 backend_url → 记错误）。
    assert "ws_live" in service._snapshots
    assert service._workspace_errors["ws_live"] == (
        f"工作区后端尚未连接: {live.workspace_id}"
    )
