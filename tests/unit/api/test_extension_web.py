"""私有 Web 运行入口绑定原 owner；原生库存及文件读取作为显式边界替身。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.testclient import WebSocketDenialResponse

from app.container import WorkspaceContainerRegistry
from app.schemas.gateway.extension_host import ExtensionHostConnectionDTO
from app.schemas.gateway.extension_installation import (
    InstalledExtension,
    ProfileExtensionInventory,
)
from app.schemas.gateway.extension_web import (
    ExtensionWebHostConnectionDTO,
    ExtensionWebHostOpenRequest,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    NativeFileRead,
    NativeFileStat,
    NativeManagementEnvironment,
)
from app.services.infrastructure.extension_host.process import NodeExtensionHost
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.unit.api.test_extension_host import (
    private_host_client as private_host_client,  # noqa: PLC0414
)

PATH = "/api/internal/extension-web-hosts/channel"
GATEWAY = "gateway_" + "a" * 32
KEY = "publisher.probe-1.0.0"
NEXT_KEY = "publisher.probe-2.0.0"
OTHER_KEY = "publisher.other-1.0.0"


def _entry(key: str) -> InstalledExtension:
    return InstalledExtension(source="profile", extension_key=key,
        description={"browser": "dist/main.js"}, is_valid=True, validations=[])


@dataclass
class WebCase:
    client: TestClient
    registry: WorkspaceContainerRegistry
    payload: dict[str, object]
    headers: dict[str, str]
    inventory: ProfileExtensionInventory
    reads: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture
def web_case(private_host_client: tuple[TestClient, WorkspaceContainerRegistry],
             monkeypatch: pytest.MonkeyPatch) -> Iterator[WebCase]:
    client, registry = private_host_client
    owner = registry.extension_hosts
    owner._management = ExtensionManagementClient(backend_id=owner.backend_id,
        gateway_url="http://127.0.0.1:8014", token="private-host-token", operation_timeout=2)
    owner.runtime = NodeHostRuntime(owner._home / "node", owner._home / "extensionHostProcess.mjs",
        "a" * 40, "1.145.0", "24.22.1")
    result = WebCase(client, registry, {"protocol_version": 1, "gateway_id": GATEWAY,
        "backend_id": owner.backend_id, "view_id": "window-one", "workspace_ids": [],
        "profile_id": "default-profile", "extension_keys": [KEY], "gateway_connection_id": "route-only"},
        {"X-Local-Token": "private-host-token", "X-BoxTeam-Gateway-Id": GATEWAY,
         "X-BoxTeam-Extension-Client-Id": "authenticated-client", "X-Request-ID": "web-private"},
        ProfileExtensionInventory(profile_id="default-profile", extensions=[_entry(KEY), _entry(OTHER_KEY)]))

    async def inventory(_management: ExtensionManagementClient) -> ProfileExtensionInventory:
        return result.inventory

    async def read(_management: ExtensionManagementClient, **selection: object) -> NativeFileRead:
        result.reads.append(selection)
        return NativeFileRead(content_base64="ZnJvemVuLWJ5dGVz")

    async def stat(_management: ExtensionManagementClient, **selection: object) -> NativeFileStat:
        result.reads.append(selection)
        return NativeFileStat(type=1, ctime=0, mtime=123, size=12, permissions=None)

    monkeypatch.setattr(ExtensionManagementClient, "inventory", inventory)
    monkeypatch.setattr(ExtensionManagementClient, "extension_file_read", read)
    monkeypatch.setattr(ExtensionManagementClient, "extension_file_stat", stat)
    try:
        yield result
    finally:
        assert client.portal is not None
        client.portal.call(registry.close_extension_hosts)


def _file(case: WebCase, binding: dict[str, object], *, key: str = KEY) -> dict[str, object]:
    return {**{name: case.payload[name] for name in ("protocol_version", "gateway_id", "backend_id", "profile_id")},
        "extension_key": key, "extension_id": "publisher.probe", "relative_segments": ["dist", "main.js"],
        "current_profile_member": False, "generation": binding["generation"], "view_id": binding["view_id"]}


def test_private_web_channel_returns_exact_frozen_subset_without_node_initialization(web_case: WebCase) -> None:
    owner = web_case.registry.extension_hosts
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(web_case.payload)
        binding = websocket.receive_json()
        assert binding == {**{name: value for name, value in web_case.payload.items() if name != "gateway_connection_id"},
            "client_id": "authenticated-client", "generation": binding["generation"], "request_id": "web-private",
            "inventory": {"profile_id": "default-profile", "extensions": [web_case.inventory.extensions[0].model_dump()]}}
        ExtensionWebHostConnectionDTO.model_validate(binding)
        assert len(owner.active) == 1 and owner.active[0].host_kind == "web"
        assert not (owner._home / "state/backend/extension-hosts").exists()
        assert owner._configuration_closed
    assert owner.active == ()


def test_web_disconnect_during_inventory_cancels_admission_before_context_exit(web_case: WebCase, monkeypatch: pytest.MonkeyPatch) -> None:
    scanning, cancelled = threading.Event(), threading.Event()

    async def inventory(_management: ExtensionManagementClient) -> ProfileExtensionInventory:
        scanning.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        pytest.fail("已断开的准入不能返回库存")

    monkeypatch.setattr(ExtensionManagementClient, "inventory", inventory)
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(web_case.payload)
        assert scanning.wait(2)
        websocket.close()
        assert cancelled.wait(2), "收到关闭后必须取消仍在等待的原准入"
    assert web_case.registry.extension_hosts.active == ()


@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"X-Local-Token": "local-dev-token"}, 401),
    ({"X-Local-Token": "private-host-token", "Origin": "https://browser.invalid"}, 403),
    ({"X-Local-Token": "private-host-token", "Sec-Fetch-Site": "same-origin"}, 403),
    ({"X-Local-Token": "private-host-token"}, 400),
])
def test_web_channel_rejects_untrusted_control_before_reservation(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry], headers: dict[str, str], status: int,
) -> None:
    client, registry = private_host_client
    with pytest.raises(WebSocketDenialResponse) as rejected, client.websocket_connect(PATH, headers=headers):
        pass
    assert rejected.value.status_code == status
    assert registry.extension_hosts.active == () and registry.extension_hosts._management is None


@pytest.mark.parametrize("change", ["gateway", "client", "profile", "old-key"])
def test_web_channel_does_not_trust_body_identity_or_stale_installation(web_case: WebCase, change: str) -> None:
    payload = dict(web_case.payload)
    if change == "gateway":
        payload["gateway_id"] = "gateway_" + "b" * 32
    elif change == "client":
        payload["client_id"] = "forged-client"
    elif change == "profile":
        payload["profile_id"] = "other-profile"
    else:
        payload["extension_keys"] = [NEXT_KEY]
    with pytest.raises(ValueError), web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(payload)
        websocket.receive_json()
    assert web_case.registry.extension_hosts.active == () and web_case.reads == []


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_retained_package_requires_open_web_binding_and_stops_after_close(web_case: WebCase, operation: str) -> None:
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(web_case.payload)
        binding = websocket.receive_json()
        payload = _file(web_case, binding)
        web_case.inventory = ProfileExtensionInventory(profile_id="default-profile", extensions=[])
        response = web_case.client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=web_case.headers)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "web-private"
        assert web_case.reads == [{name: payload[name] for name in (
            "profile_id", "extension_key", "extension_id", "relative_segments", "current_profile_member")}]
    rejected = web_case.client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=web_case.headers)
    assert rejected.status_code == 409 and len(web_case.reads) == 1


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize("change", ["key", "client", "missing-client", "backend", "profile", "view", "generation", "current", "missing-generation"])
def test_retained_package_rejects_binding_mismatch_before_native_read(web_case: WebCase, operation: str, change: str) -> None:
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(web_case.payload)
        payload = _file(web_case, websocket.receive_json())
        headers = dict(web_case.headers)
        if change == "key":
            payload["extension_key"] = OTHER_KEY
        elif change == "client":
            headers["X-BoxTeam-Extension-Client-Id"] = "another-client"
        elif change == "missing-client":
            del headers["X-BoxTeam-Extension-Client-Id"]
        elif change == "backend":
            payload["backend_id"] = str(uuid4())
        elif change == "profile":
            payload["profile_id"] = "other-profile"
        elif change == "view":
            payload["view_id"] = "another-window"
        elif change == "generation":
            payload["generation"] = str(uuid4())
        elif change == "current":
            payload["current_profile_member"] = True
        else:
            payload["generation"] = None
        response = web_case.client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
        assert response.status_code == 409 and web_case.reads == []


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_web_stop_during_native_read_prevents_late_disclosure(web_case: WebCase, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    original = getattr(ExtensionManagementClient, f"extension_file_{operation}")
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as websocket:
        websocket.send_json(web_case.payload)
        binding = websocket.receive_json()

        async def stop_after_read(management: ExtensionManagementClient, **selection: object) -> NativeFileRead | NativeFileStat:
            result = await original(management, **selection)
            web_case.registry.extension_hosts._sessions[binding["generation"]].stopping.set()
            return result

        monkeypatch.setattr(ExtensionManagementClient, f"extension_file_{operation}", stop_after_read)
        response = web_case.client.post(f"/api/internal/extension-files/{operation}",
            json=_file(web_case, binding), headers=web_case.headers)
        assert response.status_code == 409 and len(web_case.reads) == 1
        assert "content_base64" not in response.text and "permissions" not in response.text


def test_closing_one_window_keeps_other_window_frozen_version(web_case: WebCase) -> None:
    with web_case.client.websocket_connect(PATH, headers=web_case.headers) as first:
        first.send_json(web_case.payload)
        first_binding = first.receive_json()
        web_case.inventory = ProfileExtensionInventory(profile_id="default-profile", extensions=[_entry(NEXT_KEY)])
        with web_case.client.websocket_connect(PATH, headers=web_case.headers) as second:
            second.send_json({**web_case.payload, "view_id": "window-two", "extension_keys": [NEXT_KEY]})
            second_binding = second.receive_json()
            assert first_binding["generation"] != second_binding["generation"]
            web_case.inventory = ProfileExtensionInventory(profile_id="default-profile", extensions=[])
            for binding, key in ((first_binding, KEY), (second_binding, NEXT_KEY)):
                response = web_case.client.post("/api/internal/extension-files/read", json=_file(web_case, binding, key=key), headers=web_case.headers)
                assert response.status_code == 200, response.text
        rejected = web_case.client.post("/api/internal/extension-files/read", json=_file(web_case, second_binding, key=NEXT_KEY), headers=web_case.headers)
        assert rejected.status_code == 409
        assert web_case.client.post("/api/internal/extension-files/read", json=_file(web_case, first_binding), headers=web_case.headers).status_code == 200
    assert web_case.registry.extension_hosts.active == ()


def test_node_binding_keeps_existing_wire_fields_after_shared_identity_gains_kind(web_case: WebCase, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = web_case.registry.extension_hosts

    async def environment(_management: ExtensionManagementClient) -> NativeManagementEnvironment:
        return NativeManagementEnvironment(app_root_uri=owner._home.as_uri(),
            global_storage_home_uri=(owner._home / "globalStorage").as_uri(),
            workspace_storage_home_uri=(owner._home / "workspaceStorage").as_uri(),
            untitled_workspaces_home_uri=(owner._home / "Workspaces").as_uri(),
            profile_id="default-profile", settings_native_uri=(owner._home / "settings.json").as_uri())

    @asynccontextmanager
    async def connect(_host: NodeExtensionHost) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        yield asyncio.StreamReader(), Mock(spec=asyncio.StreamWriter)

    monkeypatch.setattr(ExtensionManagementClient, "environment", environment)
    monkeypatch.setattr(NodeExtensionHost, "connect", connect)
    payload = {name: web_case.payload[name] for name in ("protocol_version", "gateway_id", "backend_id", "view_id")}
    payload["workspace_ids"] = list(web_case.registry.mounted_workspaces.workspace_ids)
    with web_case.client.websocket_connect("/api/internal/extension-hosts/channel", headers=web_case.headers) as websocket:
        websocket.send_json(payload)
        binding = websocket.receive_json()
        assert "host_kind" not in binding
        assert {name: binding[name] for name in payload} == payload
        ExtensionHostConnectionDTO.model_validate(binding)
    assert owner.active == ()


@pytest.mark.parametrize("change", ["empty", "duplicate-key", "duplicate-workspace", "empty-key"])
def test_web_request_rejects_ambiguous_or_empty_run_set(web_case: WebCase, change: str) -> None:
    payload = dict(web_case.payload)
    if change == "empty":
        payload["extension_keys"] = []
    elif change == "duplicate-key":
        payload["extension_keys"] = [KEY, KEY]
    elif change == "duplicate-workspace":
        payload["workspace_ids"] = list(web_case.registry.mounted_workspaces.workspace_ids) * 2
    else:
        payload["extension_keys"] = [""]
    with pytest.raises(ValidationError):
        ExtensionWebHostOpenRequest.model_validate(payload)
