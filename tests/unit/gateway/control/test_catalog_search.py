from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.control.catalog_search import (
    GatewaySessionCatalogSearchService,
    _CatalogSnapshot,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway_control import (
    WorkspaceNavigationNodeDTO,
    WorkspaceNavigationTreeDTO,
)
from app.schemas.internal_v2.session_navigation import SessionCatalogNodeDTO


@pytest.fixture
def catalog_service(tmp_path: Path) -> GatewaySessionCatalogSearchService:
    return GatewaySessionCatalogSearchService(
        registry=GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
        ),
        http_client=_FailingHttpClient(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )


@pytest.fixture
def catalog_snapshot() -> _CatalogSnapshot:
    folder = SessionCatalogNodeDTO(
        node_id="folder-needle",
        kind="folder",
        name="报告",
        node_revision=1,
        folder_id="folder-needle",
        has_children=True,
    )
    session = SessionCatalogNodeDTO(
        node_id="session-needle",
        kind="session",
        name="目标 Needle",
        node_revision=1,
        session_id="session-needle",
        parent_node_id=folder.node_id,
        created_at=datetime.fromisoformat("2026-10-21T00:30:00+08:00"),
    )
    return _CatalogSnapshot(
        revision="revision-one",
        updated_at=datetime.now(UTC),
        nodes_by_id={node.node_id: node for node in (folder, session)},
    )


def test_catalog_cache_round_trip_preserves_logical_search_without_locators(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
) -> None:
    target = _offline_target()
    catalog_service._save_snapshot(target.workspace_id, catalog_snapshot)
    path = catalog_service._cache_path(target.workspace_id)
    raw = path.read_text()
    assert json.loads(raw)["schema_version"] == 2
    assert "storage_relative_path" not in raw
    assert target.root_path not in raw
    loaded = catalog_service._load_snapshot(target.workspace_id)
    assert loaded == catalog_snapshot
    assert loaded is not None
    for query in (
        "needle",
        "session-needle",
        "2026/10/20",
        "2026/10/20/session-needle",
    ):
        matches = catalog_service._search_snapshot(target, loaded, query, limit=10)
        expected_ids = (
            ["folder-needle", "session-needle"]
            if query == "needle"
            else ["session-needle"]
        )
        assert [item.node_id for item in matches] == expected_ids
        for item in matches:
            assert "storage_relative_path" not in item.model_dump()
        session = next(item for item in matches if item.node_id == "session-needle")
        assert session.relative_path == "报告/目标 Needle"
        assert session.breadcrumb_node_ids == ["folder-needle", "session-needle"]
        assert "2026/10/20" not in session.model_dump_json()
    assert (
        catalog_service._search_snapshot(target, loaded, "2026/10/21", limit=10) == []
    )


def test_catalog_search_limits_matches_after_stable_name_sort(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
    target = _offline_target()
    nodes = [
        SessionCatalogNodeDTO(
            node_id="node-z",
            kind="folder",
            name="Zulu needle",
            node_revision=1,
        ),
        SessionCatalogNodeDTO(
            node_id="node-b",
            kind="folder",
            name="Alpha needle",
            node_revision=1,
        ),
        SessionCatalogNodeDTO(
            node_id="node-a",
            kind="folder",
            name="alpha needle",
            node_revision=1,
        ),
    ]
    snapshot = _CatalogSnapshot(
        revision="revision-one",
        updated_at=datetime.now(UTC),
        nodes_by_id={node.node_id: node for node in nodes},
    )

    matches = catalog_service._search_snapshot(target, snapshot, "needle", limit=2)

    assert [item.node_id for item in matches] == ["node-a", "node-b"]
    assert catalog_service._search_snapshot(target, snapshot, "needle", limit=0) == []
    assert [
        item.node_id
        for item in catalog_service._search_snapshot(
            target,
            snapshot,
            "needle",
            limit=-1,
        )
    ] == ["node-a", "node-b"]


def test_workspace_folder_search_uses_stable_path_sort_and_limit(
    catalog_service: GatewaySessionCatalogSearchService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        WorkspaceNavigationNodeDTO(
            node_id="folder-b",
            kind="workspace_folder",
            name="Beta needle",
        ),
        WorkspaceNavigationNodeDTO(
            node_id="folder-child",
            kind="workspace_folder",
            name="Needle child",
            parent_node_id="folder-a",
        ),
        WorkspaceNavigationNodeDTO(
            node_id="folder-z",
            kind="workspace_folder",
            name="Alpha needle",
        ),
        WorkspaceNavigationNodeDTO(
            node_id="folder-a",
            kind="workspace_folder",
            name="alpha NEEDLE",
        ),
    ]
    tree = WorkspaceNavigationTreeDTO(revision="test", nodes=nodes)
    monkeypatch.setattr(
        catalog_service._navigation_store,
        "list_tree",
        lambda _targets: tree,
    )

    matches = catalog_service._search_workspace_folders("needle", limit=3)

    assert [item.node_id for item in matches] == [
        "folder-a",
        "folder-z",
        "folder-child",
    ]
    assert [item.relative_path for item in matches] == [
        "alpha NEEDLE",
        "Alpha needle",
        "alpha NEEDLE/Needle child",
    ]
    assert catalog_service._search_workspace_folders("needle", limit=0) == []
    assert [
        item.node_id
        for item in catalog_service._search_workspace_folders("needle", limit=-1)
    ] == ["folder-a", "folder-z", "folder-child"]


@pytest.mark.parametrize("schema_version", [1, 2])
def test_catalog_cache_rejects_old_locator_without_rewriting_or_echoing_it(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
    schema_version: int,
) -> None:
    target = _offline_target()
    catalog_service._save_snapshot(target.workspace_id, catalog_snapshot)
    path = catalog_service._cache_path(target.workspace_id)
    payload = json.loads(path.read_text())
    payload["schema_version"] = schema_version
    locator = "2026/10/20/private-legacy-session"
    payload["items"][1]["storage_relative_path"] = locator
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises((RuntimeError, ValidationError)) as caught:
        catalog_service._load_snapshot(target.workspace_id)
    assert locator not in str(caught.value)
    assert str(path) not in str(caught.value)
    if schema_version == 1:
        assert "版本不兼容" in str(caught.value)
    else:
        assert "storage_relative_path" in str(caught.value)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_catalog_sync_rejects_legacy_export_and_preserves_existing_snapshot(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
) -> None:
    target = replace(_offline_target(), backend_url="http://workspace.invalid")
    catalog_service._save_snapshot(target.workspace_id, catalog_snapshot)
    catalog_service._snapshots[target.workspace_id] = catalog_snapshot
    path = catalog_service._cache_path(target.workspace_id)
    before = path.read_bytes()
    nodes = [
        node.model_dump(mode="json") for node in catalog_snapshot.nodes_by_id.values()
    ]
    locator = "2026/10/20/private-backend-session"
    nodes[1]["storage_relative_path"] = locator
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": {"revision": "bad-revision", "items": nodes}},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        catalog_service._http_client = client
        with pytest.raises(ValidationError) as caught:
            await catalog_service._sync_workspace(target, request_id="req-test")
    assert locator not in str(caught.value)
    assert path.read_bytes() == before
    assert catalog_service._snapshots[target.workspace_id] is catalog_snapshot
    assert target.workspace_id not in catalog_service._fresh_workspace_ids


class _FailingHttpClient:
    async def get(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("backend_url 为空时不应发出 HTTP 请求")


def test_catalog_runtime_config_updates_next_sync_and_wakes_loop(
    tmp_path: Path,
) -> None:
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
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
