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
from app.services.business.session_navigation.catalog_query import (
    catalog_projection_revision,
)

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
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"data": {"workspace_id": OWNER.workspace_id, "catalog_revision": 1, "projection_revision": "config-one"}})
        return httpx.Response(200, json={"data": {"revision": catalog_projection_revision(1, "config-one"), "catalog_revision": 1, "items": [], "complete": True,
            "sync": {"gateway_id": remote_gateway_id, "workspace_id": OWNER.workspace_id, "source": "authoritative", "stale": False}}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            service = GatewaySessionCatalogSearchService(
                registry=registry, http_client=client, cache_dir=tmp_path / "indexes", gateway_id=OWNER.gateway_id,
            )
            await service._sync_all()
            assert [request.url.path for request in requests] == ["/api/v1/session-catalog/version", "/api/v1/session-catalog/export"]
            assert requests[1].url.params["minimum_catalog_revision"] == "1"
            request = requests[0]
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
        owner=OWNER, catalog_revision=7, revision=catalog_projection_revision(7, "config-one"),
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
        owner=OWNER, catalog_revision=7, revision=catalog_projection_revision(7, "config-one"),
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
    catalog_service._registry.upsert(target)
    catalog_service._save_snapshot(catalog_snapshot)
    catalog_service._snapshots[OWNER] = catalog_snapshot
    path = catalog_service._cache_path(OWNER)
    before = path.read_bytes()
    nodes = [
        node.model_dump(mode="json") for node in catalog_snapshot.nodes_by_id.values()
    ]
    locator = "2026/10/20/private-backend-session"
    nodes[1]["storage_relative_path"] = locator
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"data": {"workspace_id": OWNER.workspace_id, "catalog_revision": 8, "projection_revision": "config-one"}})
        return httpx.Response(200, json={"data": {"revision": "bad-revision", "catalog_revision": 8, "items": nodes}})
    transport = httpx.MockTransport(respond)
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
                return httpx.Response(200, json={"data": {"workspace_id": target.workspace_id, "catalog_revision": native_path, "projection_revision": "config-one"}})
        revision = 8 if failed and failure == "cache" and request.url.host == "failed.test" else 7
        workspace_id = target.workspace_id if request.url.host == "failed.test" else healthy.workspace_id
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"data": {"workspace_id": workspace_id, "catalog_revision": revision, "projection_revision": "config-one"}})
        return httpx.Response(200, json={"data": {"revision": catalog_projection_revision(revision, "config-one"), "catalog_revision": revision, "items": nodes,
            "complete": True, "sync": {"gateway_id": OWNER.gateway_id, "workspace_id": workspace_id, "source": "authoritative", "stale": False}}})

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
        failed_requests = [request for request in calls if request.url.host == "failed.test"]
        assert all(request.headers["X-Request-ID"] in diagnostic.getMessage() for request in failed_requests)
        expected_owners = {"failed.test": target.workspace_id, "healthy.test": healthy.workspace_id}
        assert len(calls) == (3 if failure == "cache" else 2) and {request.url.host for request in calls} == set(expected_owners)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_authoritative_access_rejection_removes_memory_and_restart_mirror(
    catalog_service: GatewaySessionCatalogSearchService,
    catalog_snapshot: _CatalogSnapshot,
    status: int,
) -> None:
    target = replace(_offline_target(), backend_url="http://workspace.test")
    catalog_service._registry.upsert(target)
    catalog_service._snapshots[OWNER] = catalog_snapshot
    catalog_service._confirmed_at[OWNER] = datetime.now(UTC)
    catalog_service._save_snapshot(catalog_snapshot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        catalog_service._http_client = client
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await catalog_service._sync_workspace(target, request_id="req_rejected")
    assert caught.value.response.status_code == status
    assert OWNER not in catalog_service._snapshots
    assert OWNER not in catalog_service._confirmed_at
    assert catalog_service._load_snapshot(OWNER) is None
    assert f"status={status}" in catalog_service._workspace_errors[target.key]


@pytest.mark.asyncio
async def test_warm_browse_and_periodic_sync_share_version_read_without_export_or_disk(
    catalog_service, catalog_snapshot, monkeypatch,
):
    import asyncio
    target = replace(_offline_target(), backend_url="http://workspace.test")
    catalog_service._registry.upsert(target)
    catalog_service._snapshots[OWNER] = catalog_snapshot
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def respond(request):
        requests.append(request)
        assert request.url.path.endswith("/version")
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"data": {"workspace_id": OWNER.workspace_id, "catalog_revision": 7, "projection_revision": "config-one"}})
    def no_write(snapshot):
        raise AssertionError("未变目录不应落盘")
    monkeypatch.setattr(catalog_service, "_save_snapshot", no_write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog_service._http_client = client
        pending = asyncio.create_task(catalog_service._sync_workspace(target, request_id="req_periodic"))
        await entered.wait()
        joined = asyncio.create_task(catalog_service._sync_workspace(target, request_id="req_request"))
        for index in range(20):
            page = catalog_service.browse_mirror(target, path=["session-catalog", "children"], query=httpx.QueryParams(), request_id=f"req_browse_{index}")
            assert page.catalog_revision == 7 and page.sync.stale and page.sync.source == "gateway_mirror"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        assert await joined is catalog_snapshot
    assert len(requests) == 1
    assert catalog_service._snapshots[OWNER] is catalog_snapshot
    assert OWNER in catalog_service._confirmed_at


@pytest.mark.asyncio
async def test_mirror_103_cannot_confirm_105_or_absence_and_revoked_owner(catalog_service, catalog_snapshot):
    import asyncio
    target = replace(_offline_target(), backend_url="http://workspace.test")
    catalog_service._registry.upsert(target)
    catalog_service._snapshots[OWNER] = replace(catalog_snapshot, catalog_revision=103)
    hold = asyncio.Event()
    async def respond(request):
        await hold.wait()
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog_service._http_client = client
        for path, query in (
            (["session-catalog", "children"], {"minimum_catalog_revision": "105"}),
            (["session-catalog", "breadcrumb", "new-parent"], {"minimum_catalog_revision": "105"}),
            (["session-catalog", "export"], {"node_id": "new-parent", "minimum_catalog_revision": "105"}),
            (["session-catalog", "export"], {"node_id": "new-parent"}),
        ):
            assert catalog_service.browse_mirror(target, path=path, query=httpx.QueryParams(query), request_id="req_105") is None
        # catalog 数字不变时，权威配置 token 也不能被旧镜像冒充确认。
        assert catalog_service.browse_mirror(target, path=["session-catalog", "children"], query=httpx.QueryParams(),
            request_id="req_projection", known_revision="new-config-projection") is None
        catalog_service._registry.remove(target.key)
        with pytest.raises(RuntimeError, match="登记归属已改变"):
            catalog_service.browse_mirror(target, path=["session-catalog", "children"], query=httpx.QueryParams(), request_id="req_revoked")
        assert OWNER not in catalog_service._snapshots
        for task in tuple(catalog_service._in_flight.values()):
            task.cancel()
        await asyncio.gather(*catalog_service._in_flight.values(), return_exceptions=True)
