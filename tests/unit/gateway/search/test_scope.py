"""经产品 ASGI 入口验证冻结范围；本层不启动网络或工作区进程。"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway.search import MAX_SEARCH_WORKSPACES

GATEWAY = "gateway_scope"
HEADERS = {"X-Local-Token": "scope-test-token", "X-Request-ID": "req_scope"}
OWNERS = [{"gateway_id": GATEWAY, "workspace_id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(1, 4)]


@dataclass(frozen=True)
class ScopeFixture:
    client: TestClient
    registry: GatewayWorkspaceRegistry
    navigation: WorkspaceNavigationStore
    root: Path
    state: GatewayStateStore

    def add(self, index: int, *, route: str | None = None, gateway: str | None = None) -> WorkspaceTarget:
        remote = gateway is not None
        target = WorkspaceTarget(
            workspace_id=route or f"route_{index}", name=f"项目{index}",
            root_path=None if remote else str(self.root / f"absent-{index}"), backend_url="",
            backend_workspace_id=OWNERS[index]["workspace_id"],
            connection_kind="remote_gateway" if remote else "local", desired_running=False,
            managed=not remote, owner="remote_projection" if remote else "manual",
            connection_id="remote" if remote else None,
            remote_gateway_connection_id="remote" if remote else None,
            remote_workspace_id="foreign-route" if remote else None,
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
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", state_store=state)
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
    revision, active = scope.registry.registry_revision, scope.registry.active_workspace_id
    navigation = (scope.root / "navigation.json").read_bytes()
    origin = scope.query({"mode": "origin", "origin": OWNERS[1]})
    assert origin["scope"]["owners"] == [OWNERS[1]]
    assert [item["target_id"] for item in origin["branches"]] == ["route_1"]
    selected = scope.query({"mode": "selected", "owners": [OWNERS[1], OWNERS[0], OWNERS[1]]})
    assert selected["scope"]["owners"] == [OWNERS[1], OWNERS[0]]
    assert [item["target_id"] for item in selected["branches"]] == ["route_1", "route_0"]
    assert all(item["state"] == "registered" and item["code"] is None for item in selected["branches"])
    assert (scope.registry.registry_revision, scope.registry.active_workspace_id) == (revision, active)
    assert (scope.root / "navigation.json").read_bytes() == navigation
    assert scope.registry._runtimes == {}
    assert all(not target.desired_running and not Path(target.root_path).exists() for target in scope.registry.targets())


def test_all_and_resume_freeze_members_order_and_time_across_registration_changes(scope: ScopeFixture) -> None:
    first = scope.query({"mode": "all"})
    assert first["scope"]["owners"] == OWNERS[:2]
    scope.registry.remove("route_1")
    scope.add(2, route="route_1")  # B 的旧 route 被 C 复用，不能把 B 的分支重定向到 C。
    resumed = scope.query({"mode": "resume", "scope": first["scope"]})
    assert resumed["scope"] == first["scope"]
    assert [item["owner"] for item in resumed["branches"]] == OWNERS[:2]
    missing = resumed["branches"][1]
    assert missing["state"] == "unavailable" and missing["code"] == "owner_unavailable"
    assert missing["target_id"] is None and OWNERS[1]["workspace_id"] in missing["message"]
    assert scope.query({"mode": "all"})["scope"]["owners"] == [OWNERS[0], OWNERS[2]]


def test_resume_rebinds_only_same_owner_after_route_rename_and_registry_reopen(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    first = scope.query({"mode": "selected", "owners": [OWNERS[1]]})
    scope.registry.remove("route_1")
    scope.add(1, route="replacement-route")
    scope.registry.close()
    scope.state.close()
    state = GatewayStateStore(path=scope.root / "gateway.sqlite")
    reopened = GatewayWorkspaceRegistry(
        storage_path=scope.root / "registry.json", state_store=state,
    )
    monkeypatch.setattr(app.state, "registry", reopened)
    try:
        result = scope.query({"mode": "resume", "scope": first["scope"]})
        assert result["scope"] == first["scope"]
        assert result["branches"][0]["target_id"] == "replacement-route"
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


@pytest.mark.parametrize("owner", [OWNERS[2], {**OWNERS[0], "gateway_id": "foreign"}, {**OWNERS[0], "workspace_id": "route_0"}])
def test_unknown_scope_rejects_entire_initial_set(scope: ScopeFixture, owner: dict) -> None:
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "selected", "owners": [OWNERS[0], owner]})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "owner_unavailable"
    assert "data" not in response.json()


def test_resume_rejects_different_gateway_unknown_version_and_duplicate_owners(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = scope.query({"mode": "all"})["scope"]
    for change in ({"version": 2}, {"owners": [OWNERS[0], OWNERS[0]]}, {"resolved_at": "2026-11-03T00:00:00"}):
        response = scope.client.post("/api/gateway/search/resolve", json={"mode": "resume", "scope": {**original, **change}})
        assert response.status_code == 422, response.text
    monkeypatch.setattr(app.state, "gateway_id", "gateway_replaced")
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "resume", "scope": original})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "scope_gateway_mismatch"


def test_ambiguous_owner_is_rejected_initially_and_retained_on_resume(scope: ScopeFixture) -> None:
    first = scope.query({"mode": "origin", "origin": OWNERS[0]})
    scope.add(0, route="duplicate-route")
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "origin", "origin": OWNERS[0]})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owner_ambiguous"
    result = scope.query({"mode": "resume", "scope": first["scope"]})
    assert result["branches"][0]["state"] == "ambiguous"
    assert result["branches"][0]["target_id"] is None


def test_remote_owner_preserves_gateway_without_connecting_or_recursing(scope: ScopeFixture) -> None:
    scope.registry.upsert_remote_gateway(RemoteGatewayConnection(
        connection_id="remote", name="Disconnected", host="absent.invalid", port=22, username="unused",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id="gateway_remote",
        protocol_version=1, source_owner="manual",
    ))
    scope.add(2, gateway="gateway_remote")
    result = scope.query({"mode": "all"})
    assert result["scope"]["owners"] == [*OWNERS[:2], {**OWNERS[2], "gateway_id": "gateway_remote"}]
    assert result["branches"][-1]["target_id"] == "route_2"
    assert scope.registry._remote_gateway_runtimes == {}


def test_scope_count_boundary_rejects_all_without_truncation(scope: ScopeFixture) -> None:
    for index in range(2, MAX_SEARCH_WORKSPACES):
        scope.registry.upsert(WorkspaceTarget(
            workspace_id=f"extra_{index}", name=f"extra {index}", root_path=str(scope.root / str(index)),
            backend_url="", backend_workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}",
            connection_kind="local", managed=True, desired_running=False,
        ), activate=False)
    assert len(scope.query({"mode": "all"})["scope"]["owners"]) == MAX_SEARCH_WORKSPACES
    scope.registry.upsert(WorkspaceTarget(
        workspace_id="one-extra", name="one extra", root_path=str(scope.root / "extra"), backend_url="",
        backend_workspace_id="00000000-0000-4000-8000-999999999999", connection_kind="local", managed=True, desired_running=False,
    ), activate=False)
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "scope_limit"
    assert "data" not in response.json()
    # 未选 C 不受全部登记数量影响；明确集合仍能取得精确 A。
    assert scope.query({"mode": "origin", "origin": OWNERS[0]})["scope"]["owners"] == [OWNERS[0]]


def test_empty_gateway_and_unauthorized_request_are_explicit(scope: ScopeFixture) -> None:
    scope.registry.remove("route_0")
    scope.registry.remove("route_1")
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "empty_scope"
    response = scope.client.post("/api/gateway/search/resolve", json={"mode": "all"}, headers={"X-Local-Token": "wrong"})
    assert response.status_code == 401
