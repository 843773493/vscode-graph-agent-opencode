"""经产品 ASGI 入口验证冻结范围；本层不启动网络或工作区进程。"""

from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceIdentityConflictError,
    WorkspaceTarget,
)
from app.schemas.gateway.search import MAX_SEARCH_WORKSPACES

GATEWAY = "gateway_" + "a" * 32
HEADERS = {"X-Local-Token": "scope-test-token", "X-Request-ID": "req_scope"}
OWNERS = [{"gateway_id": GATEWAY, "workspace_id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(1, 4)]


@dataclass(frozen=True)
class ScopeFixture:
    client: TestClient
    registry: GatewayWorkspaceRegistry
    navigation: WorkspaceNavigationStore
    root: Path
    state: GatewayStateStore

    def add(self, index: int, *, root_path: Path | None = None) -> WorkspaceTarget:
        target = WorkspaceTarget(
            gateway_id=GATEWAY, workspace_id=OWNERS[index]["workspace_id"], name=f"项目{index}",
            root_path=str(root_path or self.root / f"absent-{index}"), backend_url="",
            connection_kind="local", desired_running=False, managed=True,
        )
        self.registry.upsert(target, activate=False)
        return target

    def query(self, payload: dict) -> dict:
        response = self.client.post("/api/gateway/search/resolve", json=payload)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == HEADERS["X-Request-ID"]
        return response.json()["data"]


@pytest.fixture
def scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScopeFixture]:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", state_store=state, gateway_id=GATEWAY)
    navigation = WorkspaceNavigationStore(storage_path=tmp_path / "navigation.json")
    monkeypatch.setattr(app.state, "registry", registry, raising=False)
    monkeypatch.setattr(app.state, "workspace_navigation_store", navigation, raising=False)
    monkeypatch.setattr(app.state, "gateway_id", GATEWAY, raising=False)
    monkeypatch.setattr("app.gateway.auth.get_gateway_local_token", lambda: HEADERS["X-Local-Token"])

    def reject_io(*args: object, **kwargs: object) -> None:
        pytest.fail("范围解析不得探测健康、取得路由租约、连接或启动目标")

    for name in ("list_dtos", "resolve_service_url", "acquire_route_reference"):
        monkeypatch.setattr(registry, name, reject_io)
    # 不进入 app lifespan；这里仅验证产品路由与真实控制面存储组合。
    fixture = ScopeFixture(TestClient(app, headers=HEADERS), registry, navigation, tmp_path, state)
    fixture.add(0)
    fixture.add(1)
    navigation.list_tree(registry.targets())
    try:
        yield fixture
    finally:
        fixture.client.close()
        registry.close()
        state.close()


def test_origin_and_selected_preserve_exact_stable_owner_order(scope: ScopeFixture) -> None:
    revision, active = scope.registry.registry_revision, scope.registry.active_workspace
    navigation = (scope.root / "navigation.json").read_bytes()
    origin = scope.query({"mode": "origin", "origin": OWNERS[1]})
    assert origin["scope"]["owners"] == [OWNERS[1]]
    assert origin["branches"] == [{"owner": OWNERS[1], "state": "registered", "code": None, "message": None}]
    selected = scope.query({"mode": "selected", "owners": [OWNERS[1], OWNERS[0], OWNERS[1]]})
    assert selected["scope"]["owners"] == [OWNERS[1], OWNERS[0]]
    assert selected["branches"] == [
        {"owner": owner, "state": "registered", "code": None, "message": None} for owner in (OWNERS[1], OWNERS[0])
    ]
    assert (scope.registry.registry_revision, scope.registry.active_workspace) == (revision, active)
    assert (scope.root / "navigation.json").read_bytes() == navigation
    assert scope.registry._runtimes == {}
    assert all(not target.desired_running and target.root_path is not None and not Path(target.root_path).exists()
               for target in scope.registry.targets())


def test_all_and_resume_freeze_members_order_and_time_across_registration_changes(scope: ScopeFixture) -> None:
    first = scope.query({"mode": "all"})
    assert first["scope"]["owners"] == OWNERS[:2]
    previous = scope.registry.resolve((GATEWAY, OWNERS[1]["workspace_id"]))
    assert previous.root_path is not None
    scope.registry.remove(previous.key)
    replacement = scope.add(2, root_path=Path(previous.root_path))
    assert replacement.root_path == previous.root_path  # 同一物理位置的新 UUID 不能接管 B 的冻结分支。
    resumed = scope.query({"mode": "resume", "scope": first["scope"]})
    assert resumed["scope"] == first["scope"]
    assert [item["owner"] for item in resumed["branches"]] == OWNERS[:2]
    missing = resumed["branches"][1]
    assert missing["state"] == "unavailable" and missing["code"] == "owner_unavailable"
    assert missing["owner"] == OWNERS[1] and OWNERS[1]["workspace_id"] in missing["message"]
    assert resumed["branches"][0] == first["branches"][0]
    assert scope.query({"mode": "all"})["scope"]["owners"] == [OWNERS[0], OWNERS[2]]


def test_resume_rebinds_only_same_owner_after_route_update_and_registry_reopen(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    first = scope.query({"mode": "selected", "owners": [OWNERS[1]]})
    previous = scope.registry.resolve((GATEWAY, OWNERS[1]["workspace_id"]))
    scope.registry.remove(previous.key)
    replacement = scope.registry.upsert(replace(
        previous, name="重新登记", backend_url="http://replacement.test", target_generation="", runtime_lease_id=None,
    ), activate=False)
    assert replacement.key == previous.key and replacement.target_generation != previous.target_generation
    scope.registry.close()
    scope.state.close()
    state = GatewayStateStore(path=scope.root / "gateway.sqlite")
    reopened = GatewayWorkspaceRegistry(
        storage_path=scope.root / "registry.json", state_store=state, gateway_id=GATEWAY,
    )
    monkeypatch.setattr(app.state, "registry", reopened)
    try:
        result = scope.query({"mode": "resume", "scope": first["scope"]})
        assert result["scope"] == first["scope"]
        assert result["branches"] == first["branches"]
        assert reopened.resolve(replacement.key).backend_url == "http://replacement.test"
    finally:
        reopened.close()
        state.close()


def test_all_uses_original_navigation_order(scope: ScopeFixture) -> None:
    tree = scope.navigation.list_tree(scope.registry.targets())
    response = scope.client.put("/api/gateway/workspace-navigation/order", json={"node_ids": [node.node_id for node in reversed(tree.nodes)]})
    assert response.status_code == 200, response.text
    assert scope.query({"mode": "all"})["scope"]["owners"] == list(reversed(OWNERS[:2]))


@pytest.mark.parametrize("payload", [
    {}, {"mode": "selected", "owners": []}, {"mode": "origin"}, {"mode": "resume"},
    {"mode": "all", "origin": OWNERS[0]}, {"mode": "origin", "origin": OWNERS[0], "owners": [OWNERS[1]]},
    {"mode": "selected", "owners": [OWNERS[0]], "extra": "not-an-option"},
])
def test_missing_or_conflicting_scope_never_means_all(scope: ScopeFixture, payload: dict) -> None:
    response = scope.client.post("/api/gateway/search/resolve", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("owner", [
    OWNERS[2], {**OWNERS[0], "gateway_id": "gateway_" + "b" * 32},
], ids=["unregistered-workspace", "unregistered-gateway"])
def test_unknown_scope_rejects_entire_initial_set(scope: ScopeFixture, owner: dict) -> None:
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "selected", "owners": [OWNERS[0], owner]})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "owner_unavailable"
    assert "data" not in response.json()


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "route_0"),
    ("workspace_id", OWNERS[1]["workspace_id"].replace("-", "")),
    ("gateway_id", "gateway_scope"),
], ids=["legacy-route", "noncanonical-uuid", "invalid-gateway"])
@pytest.mark.parametrize("mode", ["initial", "resume"])
def test_legacy_or_noncanonical_owner_is_rejected_without_changing_scope(
    scope: ScopeFixture, field: str, value: str, mode: str,
) -> None:
    original = scope.query({"mode": "all"})
    before = (scope.state.load_workspace_registry(), scope.registry.registry_revision,
              scope.registry.active_workspace, (scope.root / "navigation.json").read_bytes())
    owners = [OWNERS[0], {**OWNERS[1], field: value}]
    payload = {"mode": "selected", "owners": owners} if mode == "initial" else {
        "mode": "resume", "scope": {**original["scope"], "owners": owners},
    }
    response = scope.client.post("/api/gateway/search/resolve", json=payload)
    assert response.status_code == 422, response.text
    assert response.headers["X-Request-ID"] == HEADERS["X-Request-ID"]
    assert "data" not in response.json()
    assert (scope.state.load_workspace_registry(), scope.registry.registry_revision,
            scope.registry.active_workspace, (scope.root / "navigation.json").read_bytes()) == before
    assert scope.registry._runtimes == {} and scope.registry._remote_gateway_runtimes == {}
    assert scope.query({"mode": "resume", "scope": original["scope"]}) == original


def test_resume_rejects_different_gateway_unknown_version_and_duplicate_owners(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = scope.query({"mode": "all"})["scope"]
    for change in ({"version": 2}, {"owners": [OWNERS[0], OWNERS[0]]}, {"resolved_at": "2026-11-03T00:00:00"},
                   {"gateway_id": "gateway_scope"}):
        response = scope.client.post("/api/gateway/search/resolve", json={"mode": "resume", "scope": {**original, **change}})
        assert response.status_code == 422, response.text
    monkeypatch.setattr(app.state, "gateway_id", "gateway_" + "b" * 32)
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "resume", "scope": original})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "scope_gateway_mismatch"


def test_duplicate_owner_root_is_rejected_without_changing_initial_or_resumed_scope(scope: ScopeFixture) -> None:
    first = scope.query({"mode": "origin", "origin": OWNERS[0]})
    original = scope.registry.resolve((GATEWAY, OWNERS[0]["workspace_id"]))
    before, revision = asdict(original), scope.registry.registry_revision
    stored = scope.state.load_workspace_registry()
    with pytest.raises(WorkspaceIdentityConflictError):
        scope.registry.upsert(replace(original, root_path=str(scope.root / "duplicate-root")), activate=False)
    assert asdict(scope.registry.resolve(original.key)) == before
    assert scope.registry.registry_revision == revision and scope.state.load_workspace_registry() == stored
    assert scope.query({"mode": "origin", "origin": OWNERS[0]})["branches"] == first["branches"]
    result = scope.query({"mode": "resume", "scope": first["scope"]})
    assert result["scope"] == first["scope"] and result["branches"] == first["branches"]


def test_remote_owner_preserves_gateway_without_connecting_or_recursing(scope: ScopeFixture) -> None:
    remote_gateway = "gateway_" + "c" * 32
    connection = RemoteGatewayConnection(
        connection_id="remote", name="Disconnected", host="absent.invalid", port=22, username="unused",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id=remote_gateway,
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
    )
    target = WorkspaceTarget(
        gateway_id=remote_gateway, workspace_id=OWNERS[2]["workspace_id"], name="远端项目",
        root_path=None, backend_url="", connection_kind="remote_gateway", owner="remote_projection",
        target_namespace="gateway-remote", remote_gateway_connection_ids=(connection.connection_id,),
    )
    scope.registry.apply_remote_projection_snapshot(connection=connection, projections=(target,))
    result = scope.query({"mode": "all"})
    assert result["scope"]["owners"] == [*OWNERS[:2], {**OWNERS[2], "gateway_id": remote_gateway}]
    assert result["branches"][-1] == {
        "owner": {"gateway_id": remote_gateway, "workspace_id": target.workspace_id},
        "state": "registered", "code": None, "message": None,
    }
    assert scope.registry._remote_gateway_runtimes == {}


def test_scope_count_boundary_rejects_all_without_truncation(scope: ScopeFixture) -> None:
    for index in range(2, MAX_SEARCH_WORKSPACES):
        scope.registry.upsert(WorkspaceTarget(
            gateway_id=GATEWAY, workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}",
            name=f"extra {index}", root_path=str(scope.root / str(index)), backend_url="",
            connection_kind="local", managed=True, desired_running=False,
        ), activate=False)
    assert len(scope.query({"mode": "all"})["scope"]["owners"]) == MAX_SEARCH_WORKSPACES
    scope.registry.upsert(WorkspaceTarget(
        gateway_id=GATEWAY, workspace_id="00000000-0000-4000-8000-999999999999",
        name="one extra", root_path=str(scope.root / "extra"), backend_url="",
        connection_kind="local", managed=True, desired_running=False,
    ), activate=False)
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "scope_limit"
    assert "data" not in response.json()
    # 未选 C 不受全部登记数量影响；明确集合仍能取得精确 A。
    assert scope.query({"mode": "origin", "origin": OWNERS[0]})["scope"]["owners"] == [OWNERS[0]]


def test_empty_gateway_and_unauthorized_request_are_explicit(scope: ScopeFixture) -> None:
    scope.registry.remove((GATEWAY, OWNERS[0]["workspace_id"]))
    scope.registry.remove((GATEWAY, OWNERS[1]["workspace_id"]))
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "empty_scope"
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"}, headers={"X-Local-Token": "wrong"})
    assert response.status_code == 401


def test_control_folder_discovery_keeps_its_own_paging_scope(scope: ScopeFixture) -> None:
    first = scope.client.post("/api/gateway/workspace-navigation/folders", json={"name": "Needle parent"})
    assert first.status_code == 200, first.text
    parent = next(node for node in first.json()["data"]["nodes"] if node["name"] == "Needle parent")
    second = scope.client.post("/api/gateway/workspace-navigation/folders", json={"name": "Needle child", "parent_node_id": parent["node_id"]})
    assert second.status_code == 200, second.text
    params = {"query": "ndl", "include_folders": True, "limit": 1}
    response = scope.client.get("/api/gateway/search/workspaces", params=params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert not data["items"] and len(data["folders"]) == 1 and not data["complete"]
    assert data["folders"][0]["navigation_node_id"] == parent["node_id"]
    followup = scope.client.get("/api/gateway/search/workspaces", params={**params, "cursor": data["cursor"]})
    assert followup.status_code == 200, followup.text
    child = followup.json()["data"]
    assert child["complete"] and child["folders"][0]["breadcrumb_names"] == ["Needle parent", "Needle child"]
    mismatch = scope.client.get("/api/gateway/search/workspaces", params={**params, "include_folders": False, "cursor": data["cursor"]})
    assert mismatch.status_code == 409
    assert scope.query({"mode": "all"})["scope"]["owners"] == OWNERS[:2]
