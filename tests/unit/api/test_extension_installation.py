"""私有库存入口复用真实 Backend owner；仅管理进程读取为显式边界。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.container import WorkspaceContainerRegistry
from app.schemas.gateway.extension_installation import ProfileExtensionInventory
from app.services.infrastructure.extension_host.management import (
    NodeExtensionManagement,
)
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.unit.api.test_extension_host import (
    private_host_client as private_host_client,  # noqa: PLC0414
)

PATH = "/api/internal/extension-installations/inventory"
GATEWAY = "gateway_" + "a" * 32


@pytest.fixture
def private_inventory(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry], monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, WorkspaceContainerRegistry, dict[str, object], dict[str, str], list[str]]]:
    client, registry = private_host_client
    owner = registry.extension_hosts
    owner.runtime = NodeHostRuntime(Path(sys.executable), owner._home / "component/extensionHostProcess.mjs", "a" * 40, "1.145.0", "24.22.1")
    calls: list[str] = []

    async def inventory(_management: NodeExtensionManagement) -> ProfileExtensionInventory:
        calls.append(owner.backend_id)
        return ProfileExtensionInventory(profile_id="default-profile", extensions=[{
            "source": "profile", "description": {"identifier": {"value": "publisher.probe"}, "extensionLocation": {
                "scheme": "file", "path": "/target/extensions/probe"}, "version": "1.0.0"},
            "is_valid": False, "validations": [{"severity": 3, "message": "原 manifest 缺失"}],
        }])

    monkeypatch.setattr(NodeExtensionManagement, "inventory", inventory)
    payload = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": owner.backend_id,
               "workspace_ids": list(registry.mounted_workspaces.workspace_ids)}
    headers = {"X-Local-Token": "private-host-token", "X-BoxTeam-Gateway-Id": GATEWAY, "X-Request-ID": "inventory-private"}
    try:
        yield client, registry, payload, headers, calls
    finally:
        assert client.portal is not None
        client.portal.call(registry.close_extension_hosts)


def test_private_inventory_returns_actual_owner_default_profile_and_invalid_entries(private_inventory) -> None:
    client, registry, payload, headers, calls = private_inventory
    response = client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "inventory-private"
    data = response.json()["data"]
    assert {name: data[name] for name in payload} == payload
    assert data["profile_scope"] == "default"
    assert data["inventory"]["profile_id"] == "default-profile"
    [extension] = data["inventory"]["extensions"]
    assert extension["description"]["extensionLocation"]["path"] == "/target/extensions/probe"
    assert extension["is_valid"] is False and extension["validations"] == [{"severity": 3, "message": "原 manifest 缺失"}]
    assert calls == [payload["backend_id"]] and registry.extension_hosts.active == ()
    assert not (registry.extension_hosts._home / "state/backend/extension-hosts").exists()


@pytest.mark.parametrize("change", ["backend", "workspace", "duplicate", "missing-backend", "missing-runtime", "draining", "gateway"])
def test_private_inventory_rejects_wrong_target_before_management(private_inventory, change: str) -> None:
    client, registry, payload, headers, calls = private_inventory
    if change == "backend":
        payload["backend_id"] = str(uuid4())
    elif change == "workspace":
        payload["workspace_ids"] = [str(uuid4())]
    elif change == "duplicate":
        payload["workspace_ids"] *= 2
    elif change == "missing-backend":
        del payload["backend_id"]
    elif change == "missing-runtime":
        registry.extension_hosts.runtime = None
    elif change == "draining":
        registry.extension_hosts.close_admission()
    else:
        headers["X-BoxTeam-Gateway-Id"] = "gateway_" + "b" * 32
    response = client.post(PATH, json=payload, headers=headers)
    assert response.status_code == (422 if change in {"duplicate", "missing-backend"} else 400 if change == "gateway" else 409)
    assert response.headers["X-Request-ID"] == "inventory-private"
    assert calls == [] and registry.extension_hosts._management is None


@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"X-Local-Token": "local-dev-token"}, 401),
    ({"X-Local-Token": "private-host-token", "Origin": "https://browser.invalid"}, 403),
    ({"X-Local-Token": "private-host-token", "Sec-Fetch-Site": "same-origin"}, 403),
])
def test_private_inventory_keeps_control_authentication(private_inventory, headers: dict[str, str], status: int) -> None:
    client, registry, payload, _, calls = private_inventory
    response = client.post(PATH, json=payload, headers=headers)
    assert response.status_code == status and calls == [] and registry.extension_hosts._management is None


def test_private_inventory_checks_owner_again_before_disclosure(private_inventory, monkeypatch: pytest.MonkeyPatch) -> None:
    client, registry, payload, headers, calls = private_inventory

    async def inventory(_management: NodeExtensionManagement) -> ProfileExtensionInventory:
        calls.append("read")
        registry.extension_hosts.close_admission()
        return ProfileExtensionInventory(profile_id="must-not-be-disclosed", extensions=[])

    monkeypatch.setattr(NodeExtensionManagement, "inventory", inventory)
    response = client.post(PATH, json=payload, headers=headers)
    assert response.status_code == 409 and calls == ["read"]
    assert "must-not-be-disclosed" not in response.text
