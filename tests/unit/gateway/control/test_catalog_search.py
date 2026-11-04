from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core.path_utils import get_gateway_root
from app.gateway.control import catalog_search
from app.gateway.control.catalog_search import (
    GatewaySessionCatalogSearchService,
    _CatalogSnapshot,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.internal_v2.session_navigation import SessionCatalogNodeDTO

OWNER = GatewaySearchWorkspaceOwnerDTO(gateway_id="gateway_" + "a" * 32, workspace_id="00000000-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_remote_catalog_sync_uses_registered_target_gateway_and_original_sender(tmp_path: Path) -> None:
    remote_gateway_id = "gateway_" + "c" * 32
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=OWNER.gateway_id)
    connection = RemoteGatewayConnection(
        connection_id="catalog-query", name="远端", host="remote.test", port=22, username="unit",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=remote_gateway_id, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
    )
    projection = WorkspaceTarget(
        gateway_id=remote_gateway_id, workspace_id=OWNER.workspace_id,
        name="远端目录", root_path=None, backend_url="", connection_kind="remote_gateway",
        owner="remote_projection", target_namespace="gateway-remote",
        remote_gateway_connection_ids=(connection.connection_id,),
    )
    registry.apply_remote_projection_snapshot(
        connection=connection, projections=(projection,),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}),
    )
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="catalog-query", peer_gateway_id=OWNER.gateway_id,
    )
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"revision": "revision-one", "catalog_revision": 1, "items": []}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            service = GatewaySessionCatalogSearchService(
                registry=registry, http_client=client, cache_dir=tmp_path / "indexes", gateway_id=OWNER.gateway_id,
            )
            await service._sync_all()
            request, = requests
            assert str(request.url) == "http://remote.test/api/v1/session-catalog/export"
            assert request.headers[TARGET_GATEWAY_ID_HEADER] == remote_gateway_id
            assert request.headers["X-BoxTeam-Gateway-Id"] == OWNER.gateway_id
            assert request.headers["X-BoxTeam-Workspace-Id"] == OWNER.workspace_id
            assert request.headers["X-BoxTeam-Federation-Token"] == credential.token
            assert "X-Local-Token" not in request.headers
            assert request.headers["X-Request-ID"].startswith("req_")
            snapshot, = service._snapshots.values()
            assert snapshot.owner == GatewaySearchWorkspaceOwnerDTO(gateway_id=remote_gateway_id, workspace_id=OWNER.workspace_id)
            assert service._workspace_errors == {}
    finally:
        registry.close()


@pytest.fixture
def catalog_service(tmp_path: Path) -> Iterator[GatewaySessionCatalogSearchService]:
    with (
        closing(GatewayStateStore(path=tmp_path / "gateway.sqlite")) as state_store,
        closing(GatewayWorkspaceRegistry(
            gateway_id=OWNER.gateway_id,
            storage_path=tmp_path / "workspaces.json",
            state_store=state_store,
        )) as registry,
    ):
        yield GatewaySessionCatalogSearchService(
            registry=registry,
            http_client=_FailingHttpClient(),  # type: ignore[arg-type]
            cache_dir=tmp_path / "indexes",
            gateway_id=OWNER.gateway_id,
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
        owner=OWNER, catalog_revision=7, revision="revision-one",
        updated_at=datetime.now(UTC),
        nodes_by_id={node.node_id: node for node in (folder, session)},
    )


def _mirror(service: GatewaySessionCatalogSearchService, snapshot: _CatalogSnapshot, query: str, *, limit: int):
    service._snapshots[snapshot.owner] = snapshot
    page, updated_at = service.search_mirror(snapshot.owner, query=query, limit=limit, cursor=None, deadline_at=None)
    assert updated_at == snapshot.updated_at
    return page.items


def test_catalog_cache_round_trip_preserves_logical_search_without_locators(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
) -> None:
    catalog_service._save_snapshot(catalog_snapshot)
    path = catalog_service._cache_path(OWNER)
    raw = path.read_text()
    assert json.loads(raw)["schema_version"] == 3
    assert "storage_relative_path" not in raw
    assert _offline_target().root_path not in raw
    loaded = catalog_service._load_snapshot(OWNER)
    assert loaded == catalog_snapshot
    assert loaded is not None
    for query in (
        "needle",
        "ndl",
        "报/标n",
        "session-needle",
        "2026/10/20",
        "2026/10/20/session-needle",
    ):
        matches = _mirror(catalog_service, loaded, query, limit=10)
        expected_ids = (
            ["folder-needle", "session-needle"]
            if query in {"needle", "ndl"}
            else ["session-needle"]
        )
        assert [item.node.node_id for item in matches] == expected_ids
        for item in matches:
            assert "storage_relative_path" not in item.model_dump()
        session = next(item for item in matches if item.node.node_id == "session-needle")
        assert session.relative_path == "报告/目标 Needle"
        assert [item.node_id for item in session.breadcrumb] == ["folder-needle", "session-needle"]
        assert "2026/10/20" not in session.model_dump_json()
    assert (
        _mirror(catalog_service, loaded, "2026/10/21", limit=10) == []
    )
    assert _mirror(catalog_service, loaded, "目标session", limit=10) == []
    assert _mirror(catalog_service, loaded, "neld", limit=10) == []


def test_catalog_search_limits_matches_after_stable_name_sort(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
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
        owner=OWNER, catalog_revision=7, revision="revision-one",
        updated_at=datetime.now(UTC),
        nodes_by_id={node.node_id: node for node in nodes},
    )

    matches = _mirror(catalog_service, snapshot, "needle", limit=2)

    assert [item.node.node_id for item in matches] == ["node-a", "node-b"]
    assert _mirror(catalog_service, snapshot, "needle", limit=0) == []



def test_mirror_search_uses_owner_creation_key_before_id_for_same_name(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
    # ID 字典序与冻结创建日期相反，镜像首屏也应沿原目录 owner 的顺序选取。
    nodes = [
        SessionCatalogNodeDTO(
            node_id=node_id, session_id=node_id, kind="session", name="Same Needle",
            node_revision=1, created_at=datetime.fromisoformat(created_at),
        )
        for node_id, created_at in (
            ("session-a", "2026-10-21T00:00:00+00:00"),
            ("session-z", "2026-10-20T00:00:00+00:00"),
        )
    ]
    snapshot = _CatalogSnapshot(
        owner=OWNER, catalog_revision=7, revision="same-name", updated_at=datetime.now(UTC),
        nodes_by_id={node.node_id: node for node in nodes},
    )
    matches = _mirror(catalog_service, snapshot, "sndl", limit=1)
    assert [item.node.node_id for item in matches] == ["session-z"]


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_catalog_cache_rejects_old_locator_without_rewriting_or_echoing_it(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
    schema_version: int,
) -> None:
    catalog_service._save_snapshot(catalog_snapshot)
    path = catalog_service._cache_path(OWNER)
    payload = json.loads(path.read_text())
    payload["schema_version"] = schema_version
    locator = "2026/10/20/private-legacy-session"
    payload["items"][1]["storage_relative_path"] = locator
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises((RuntimeError, ValidationError)) as caught:
        catalog_service._load_snapshot(OWNER)
    assert locator not in str(caught.value)
    assert str(path) not in str(caught.value)
    if schema_version != 3:
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
    catalog_service._save_snapshot(catalog_snapshot)
    catalog_service._snapshots[OWNER] = catalog_snapshot
    path = catalog_service._cache_path(OWNER)
    before = path.read_bytes()
    nodes = [
        node.model_dump(mode="json") for node in catalog_snapshot.nodes_by_id.values()
    ]
    locator = "2026/10/20/private-backend-session"
    nodes[1]["storage_relative_path"] = locator
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": {"revision": "bad-revision", "catalog_revision": 7, "items": nodes}},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        catalog_service._http_client = client
        with pytest.raises(ValidationError) as caught:
            await catalog_service._sync_workspace(target, request_id="req-test")
    assert locator not in str(caught.value)
    assert path.read_bytes() == before
    assert catalog_service._snapshots[OWNER] is catalog_snapshot


class _FailingHttpClient:
    async def get(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("backend_url 为空时不应发出 HTTP 请求")


def test_catalog_runtime_config_updates_next_sync_and_wakes_loop(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
    catalog_service.update_runtime_config(
        refresh_interval_seconds=3,
        max_concurrency=2,
        request_timeout_seconds=4,
    )

    assert catalog_service._refresh_interval_seconds == 3
    assert catalog_service._max_concurrency == 2
    assert catalog_service._request_timeout_seconds == 4
    assert catalog_service._wake_event.is_set()


def _offline_target() -> WorkspaceTarget:
    return WorkspaceTarget(
        gateway_id=OWNER.gateway_id,
        workspace_id=OWNER.workspace_id,
        name="未连接工作区",
        root_path="/tmp/offline-without-backend",
        backend_url="",
        connection_kind="local",
        managed=True,
    )


@pytest.mark.asyncio
async def test_catalog_sync_skips_local_workspace_without_backend_url(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
    target = _offline_target()
    catalog_service._registry.upsert(target)

    await catalog_service._sync_all()

    assert catalog_service._workspace_errors[target.key] == (
        f"工作区后端尚未连接: {target.workspace_id}"
    )


def test_missing_mirror_does_not_sync(catalog_service: GatewaySessionCatalogSearchService) -> None:
    with pytest.raises(LookupError, match="没有可用目录镜像"):
        catalog_service.search_mirror(OWNER, query="needle", limit=10, cursor=None, deadline_at=None)


def test_catalog_sync_lock_pool_is_bounded_and_stable(catalog_service: GatewaySessionCatalogSearchService) -> None:
    """目录同步锁池必须恒定有界，且同一完整工作区 owner 恒得同一把锁。

    若退回按完整 owner 键的 dict，删除工作区后键永不回收，进程内存随历史
    工作区无界增长；分片锁池则保证「同 id 同锁」的互斥红线且池大小恒定。
    """

    workspace_key = (OWNER.gateway_id, OWNER.workspace_id)
    assert catalog_service._sync_lock_for(workspace_key) is catalog_service._sync_lock_for(workspace_key)
    distinct = {
        id(catalog_service._sync_lock_for((OWNER.gateway_id, f"00000000-0000-4000-8000-{index:012x}")))
        for index in range(5000)
    }
    assert len(distinct) <= 64
    assert len(catalog_service._sync_locks) <= 64


@pytest.mark.asyncio
async def test_catalog_caches_are_pruned_when_workspaces_are_removed(
    catalog_service: GatewaySessionCatalogSearchService,
) -> None:
    """已删除工作区的目录快照/错误/新鲜度缓存必须回收，不随历史工作区无界增长。

    快照含该工作区全部会话节点，是三者中最重的；若只增不减，长驻 Gateway
    内存会随历史工作区单调增长。
    """

    # 直接准备同规模残留缓存；此处验证回收，不用6000次注册表写入替测试准备历史。
    for index in range(3000):
        workspace_id = f"00000000-0000-4000-8000-{index:012x}"
        catalog_service._snapshots[GatewaySearchWorkspaceOwnerDTO(gateway_id=OWNER.gateway_id, workspace_id=workspace_id)] = object()  # type: ignore[assignment]
        catalog_service._workspace_errors[(OWNER.gateway_id, workspace_id)] = "boom"

    # 上一轮每工作区已留残留；同步一轮后必须全部回收（没有任何存活工作区）。
    await catalog_service._sync_all()

    assert catalog_service._snapshots == {}
    assert catalog_service._workspace_errors == {}


@pytest.mark.asyncio
async def test_catalog_caches_are_retained_for_live_workspaces(
    catalog_service: GatewaySessionCatalogSearchService, tmp_path: Path,
) -> None:
    """回收不得误删存活工作区的缓存：仍有 backend_url 的工作区状态必须保留语义。"""

    live = WorkspaceTarget(
        gateway_id=OWNER.gateway_id,
        workspace_id=OWNER.workspace_id,
        name="存活工作区",
        root_path=str(tmp_path / "live"),
        backend_url="",
        connection_kind="local",
        managed=True,
    )
    catalog_service._registry.upsert(live)
    catalog_service._snapshots[OWNER] = object()  # type: ignore[assignment]
    catalog_service._workspace_errors[live.key] = "旧错误先占位"

    await catalog_service._sync_all()

    # 存活工作区的快照保留；其 error 由本轮同步结果覆盖（无 backend_url → 记错误）。
    assert OWNER in catalog_service._snapshots
    assert catalog_service._workspace_errors[live.key] == (
        f"工作区后端尚未连接: {live.workspace_id}"
    )


@pytest.mark.parametrize("native_path", [
    "/private/catalog-search-marker", r"C:\private\catalog-search-marker", r"\\host\private\catalog-search-marker",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("entry, failure", [
    ("initial", "http"), ("initial", "transport"), ("initial", "validation"),
    ("refresh", "http"), ("refresh", "transport"), ("refresh", "validation"), ("refresh", "cache"),
])
async def test_catalog_failures_preserve_target_and_snapshot_without_publishing_native_details(
    catalog_service: GatewaySessionCatalogSearchService, catalog_snapshot: _CatalogSnapshot,
    native_path: str, entry: str, failure: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    target = replace(_offline_target(), backend_url="http://failed.test")
    healthy = replace(target, workspace_id="00000000-0000-4000-8000-000000000002",
                      root_path="/tmp/catalog-healthy", backend_url="http://healthy.test")
    for item in (target, healthy):
        catalog_service._registry.upsert(item)
    calls: list[httpx.Request] = []
    failed = False
    native_error = httpx.ReadError("目录传输失败: " + native_path)
    cache_error = PermissionError(13, "拒绝写入目录缓存", native_path)
    nodes = [node.model_dump(mode="json") for node in catalog_snapshot.nodes_by_id.values()]

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "failed.test" and failed:
            if failure == "transport":
                raise native_error
            if failure == "http":
                return httpx.Response(503, extensions={"reason_phrase": native_path.encode()})
            if failure == "validation":
                return httpx.Response(200, json={"data": {"revision": "bad", "catalog_revision": 7, "items": [
                    {**nodes[0], "node_revision": native_path},
                ]}})
        return httpx.Response(200, json={"data": {"revision": catalog_snapshot.revision, "catalog_revision": 7, "items": nodes}})

    original_write = catalog_search.atomic_write_json

    def write(path: Path, payload: dict[str, object]) -> None:
        if failed and failure == "cache" and path == catalog_service._cache_path(OWNER):
            raise cache_error
        original_write(path, payload)

    monkeypatch.setattr(catalog_search, "atomic_write_json", write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog_service._http_client = client
        if entry == "refresh":
            await catalog_service._sync_all()
        else:
            await catalog_service._sync_workspace(healthy, request_id="req_healthy_seed")
        path = catalog_service._cache_path(OWNER)
        previous_bytes = path.read_bytes() if path.exists() else None
        previous_snapshot = catalog_service._snapshots.get(OWNER)
        calls.clear()
        failed = True
        await catalog_service._sync_all()
        message = catalog_service._workspace_errors[target.key]
        if entry == "refresh":
            page, _ = catalog_service.search_mirror(OWNER, query="needle", limit=10, cursor=None, deadline_at=None)
            assert page.items
        else:
            with pytest.raises(LookupError):
                catalog_service.search_mirror(OWNER, query="needle", limit=10, cursor=None, deadline_at=None)
        assert healthy.key not in catalog_service._workspace_errors
        assert "catalog-search-marker" not in message
        assert "stage=session_catalog_sync" in message and target.workspace_id in message
        diagnostic, = [record for record in caplog.records if record.name == "app.core.failures"]
        assert diagnostic.exc_info is not None
        original = diagnostic.exc_info[1]
        assert original is not None
        failed_request, = [request for request in calls if request.url.host == "failed.test"]
        assert failed_request.headers["X-Request-ID"] in diagnostic.getMessage()
        expected_owners = {"failed.test": target.workspace_id, "healthy.test": healthy.workspace_id}
        assert len(calls) == 2 and {request.url.host for request in calls} == set(expected_owners)
        assert all(request.headers["X-BoxTeam-Workspace-Id"] == expected_owners[request.url.host] for request in calls)
        if failure == "transport":
            assert original is native_error
        elif failure == "cache":
            assert original is cache_error
            assert "errno=13" in message
        elif failure == "validation":
            assert isinstance(original, ValidationError) and original.errors()[0]["input"] == native_path
        else:
            assert isinstance(original, httpx.HTTPStatusError) and original.response.status_code == 503
            assert "status=503" in message
        if failure != "validation":
            assert "catalog-search-marker" in caplog.text
        assert (path.read_bytes() if path.exists() else None) == previous_bytes
        assert catalog_service._snapshots.get(OWNER) is previous_snapshot
        failed = False
        await catalog_service._sync_all()
        assert catalog_service._workspace_errors == {}
        assert catalog_service._snapshots[OWNER].owner == OWNER
