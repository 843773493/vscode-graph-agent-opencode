"""通过产品 Gateway ASGI 入口验证纯控制面发现，不启动工作区或浏览器。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget

GATEWAY = "gateway_discovery"
BACKEND_IDS = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 4)]
HEADERS = {"X-Local-Token": "discovery-token", "X-Request-ID": "req_discovery"}


@dataclass(frozen=True)
class DiscoveryFixture:
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    navigation: WorkspaceNavigationStore
    navigation_path: Path


@pytest.fixture
async def discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DiscoveryFixture]:
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "registry.json", state_store=GatewayStateStore(path=tmp_path / "gateway.sqlite"),
    )
    for index, name in enumerate(("Alpha project", "Unrelated name", "Stopped workspace")):
        registry.upsert(WorkspaceTarget(
            workspace_id=f"route_{index}", name=name, root_path=str(tmp_path / f"absent-workspace-{index}"),
            backend_url="", connection_kind="local", backend_workspace_id=BACKEND_IDS[index],
            managed=True, desired_running=False,
        ), activate=False)
    navigation_path = tmp_path / "navigation.json"
    navigation = WorkspaceNavigationStore(storage_path=navigation_path)
    navigation.list_tree(registry.targets())
    monkeypatch.setattr(app.state, "registry", registry, raising=False)
    monkeypatch.setattr(app.state, "workspace_navigation_store", navigation, raising=False)
    monkeypatch.setattr(app.state, "gateway_id", GATEWAY, raising=False)
    monkeypatch.setattr("app.gateway.auth.get_gateway_local_token", lambda: HEADERS["X-Local-Token"])

    async def reject_probe(*, check_health: bool = True):
        pytest.fail("元数据发现不应列探目标健康或读取工作区数据")

    monkeypatch.setattr(registry, "list_dtos", reject_probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test", headers=HEADERS) as client:
        yield DiscoveryFixture(client, registry, navigation, navigation_path)
    registry.close()


async def _query(discovery: DiscoveryFixture, **params: str | int) -> dict:
    response = await discovery.client.get("/api/gateway/search/workspaces", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == HEADERS["X-Request-ID"]
    data = response.json()["data"]
    assert data["gateway_id"] == GATEWAY
    assert data["source"] == "registry" and data["resolved_at"]
    return data


@pytest.mark.asyncio
async def test_discovery_pages_preserve_navigation_and_include_stopped_targets(discovery: DiscoveryFixture) -> None:
    before = discovery.navigation_path.read_bytes()
    revision, active = discovery.registry.registry_revision, discovery.registry.active_workspace_id
    collected, cursor = [], None
    for _index in range(3):
        page = await _query(discovery, limit=1, **({"cursor": cursor} if cursor else {}))
        collected.extend(page["items"])
        cursor = page["cursor"]
        assert page["complete"] is (cursor is None)
    assert cursor is None
    assert [item["target_id"] for item in collected] == ["route_0", "route_1", "route_2"]
    assert [item["owner"] for item in collected] == [
        {"gateway_id": GATEWAY, "workspace_id": workspace_id} for workspace_id in BACKEND_IDS
    ]
    assert discovery.navigation_path.read_bytes() == before
    assert (discovery.registry.registry_revision, discovery.registry.active_workspace_id) == (revision, active)
    for target in discovery.registry.targets():
        assert not target.desired_running
        assert not Path(target.root_path).exists()
    assert discovery.registry._runtimes == {}


@pytest.mark.asyncio
async def test_discovery_matches_name_id_and_navigation_with_exact_owners(discovery: DiscoveryFixture) -> None:
    created = await discovery.client.post("/api/gateway/workspace-navigation/folders", json={"name": "报告集合"})
    assert created.status_code == 200, created.text
    folder = next(node for node in created.json()["data"]["nodes"] if node["kind"] == "workspace_folder")
    reference = next(node for node in created.json()["data"]["nodes"] if node["workspace_id"] == "route_1")
    moved = await discovery.client.patch(f"/api/gateway/workspace-navigation/nodes/{reference['node_id']}", json={"parent_node_id": folder["node_id"]})
    assert moved.status_code == 200, moved.text
    # 把较早节点移到较晚创建的父节点下，不能把按 parent_id 分组的 wire 顺序误认为前序遍历。
    outer_response = await discovery.client.post("/api/gateway/workspace-navigation/folders", json={"name": "Outer"})
    assert outer_response.status_code == 200, outer_response.text
    outer = next(node for node in outer_response.json()["data"]["nodes"] if node["name"] == "Outer")
    moved = await discovery.client.patch(f"/api/gateway/workspace-navigation/nodes/{folder['node_id']}", json={"parent_node_id": outer["node_id"]})
    assert moved.status_code == 200, moved.text
    before = discovery.navigation_path.read_bytes()
    for query, expected in (("lphpj", "route_0"), (BACKEND_IDS[1], "route_1"), ("报告", "route_1")):
        page = await _query(discovery, query=query)
        assert [item["target_id"] for item in page["items"]] == [expected]
        assert page["complete"] and page["cursor"] is None
    item = (await _query(discovery, query="报告"))["items"][0]
    assert item["breadcrumb_node_ids"] == [outer["node_id"], folder["node_id"], reference["node_id"]]
    assert item["breadcrumb_names"] == ["Outer", "报告集合", "Unrelated name"]
    empty = await _query(discovery, query="never-matches-any-target")
    assert empty["items"] == [] and empty["complete"] and empty["cursor"] is None
    assert discovery.navigation_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["query", "gateway", "registry", "navigation"])
async def test_discovery_rejects_cursor_after_its_metadata_changes(discovery: DiscoveryFixture, change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first = await _query(discovery, limit=1)
    params = {"cursor": first["cursor"], "limit": 1}
    if change == "query":
        params["query"] = "Alpha"
    elif change == "gateway":
        monkeypatch.setattr(app.state, "gateway_id", "gateway_replaced")
    elif change == "registry":
        discovery.registry.update("route_0", name="Renamed")
    else:
        response = await discovery.client.post("/api/gateway/workspace-navigation/folders", json={"name": "new folder"})
        assert response.status_code == 200, response.text
    response = await discovery.client.get("/api/gateway/search/workspaces", params=params)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["owner"] == "workspace_discovery"
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == HEADERS["X-Request-ID"]


@pytest.mark.asyncio
async def test_discovery_reports_remote_owner_without_resolving_or_connecting_route(discovery: DiscoveryFixture) -> None:
    discovery.registry.upsert_remote_gateway(RemoteGatewayConnection(
        connection_id="remote-disconnected", name="Remote", host="unused.invalid", port=22, username="unused",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id="gateway_remote",
        protocol_version=1, source_owner="manual",
    ))
    discovery.registry.upsert(WorkspaceTarget(
        workspace_id="remote-projection", name="Remote workspace", root_path=None, backend_url="",
        connection_kind="remote_gateway", backend_workspace_id=BACKEND_IDS[0], owner="remote_projection",
        connection_id="remote-disconnected",
        remote_gateway_connection_id="remote-disconnected", remote_workspace_id="remote-route-alias",
    ), activate=False)
    page = await _query(discovery, query="Remote workspace")
    assert [item["owner"] for item in page["items"]] == [{"gateway_id": "gateway_remote", "workspace_id": BACKEND_IDS[0]}]
    assert discovery.registry._remote_gateway_runtimes == {}
    assert discovery.registry._runtimes == {}


@pytest.mark.asyncio
async def test_discovery_reports_metadata_budget_without_partial_success(discovery: DiscoveryFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.gateway.control.workspace_discovery._MAX_DISCOVERY_NODES", 2)
    response = await discovery.client.get("/api/gateway/search/workspaces")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["owner"] == "workspace_discovery"
    assert "扫描上限" in response.json()["detail"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params,expected", [({"limit": 0}, 422), ({"limit": 201}, 422), ({"query": "x" * 513}, 422), ({"cursor": "?"}, 409)])
async def test_discovery_rejects_invalid_inputs(discovery: DiscoveryFixture, params: dict, expected: int) -> None:
    response = await discovery.client.get("/api/gateway/search/workspaces", params=params)
    assert response.status_code == expected, response.text


@pytest.mark.asyncio
async def test_discovery_requires_existing_gateway_management_auth(discovery: DiscoveryFixture) -> None:
    response = await discovery.client.get("/api/gateway/search/workspaces", headers={"X-Local-Token": "wrong"})
    assert response.status_code == 401
