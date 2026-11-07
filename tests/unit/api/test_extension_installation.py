"""Backend 仅为绑定 Web 代际读取已冻结扩展包，并保留原始管理错误。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.container import WorkspaceContainerRegistry
from app.schemas.gateway.extension_installation import (
    InstalledExtension,
    ProfileExtensionInventory,
)
from app.services.infrastructure.extension_host import owner as owner_module
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementClient,
    ExtensionManagementError,
    HostExtensionPublication,
    NativeExtensionPublication,
    NativeFileRead,
    NativeFileStat,
    NativeHostReference,
    NativeManagementEnvironment,
    NativeReferenceService,
)
from tests.unit.api.test_extension_host import (
    private_host_client as private_host_client,  # noqa: PLC0414
)

GATEWAY = "gateway_" + "a" * 32
KEY = "publisher.probe-1.0.0"


@pytest.fixture
def private_package(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, WorkspaceContainerRegistry, dict[str, object], dict[str, str], list[dict[str, object]]]]:
    client, registry = private_host_client
    owner = registry.extension_hosts
    owner._management = ExtensionManagementClient(backend_id=owner.backend_id,
        gateway_url="http://127.0.0.1:8014", token="private-host-token", operation_timeout=2)
    calls: list[dict[str, object]] = []

    async def environment(_management: ExtensionManagementClient) -> NativeManagementEnvironment:
        return NativeManagementEnvironment(app_root_uri=tmp_path.as_uri(),
            global_storage_home_uri=(tmp_path / "globalStorage").as_uri(),
            workspace_storage_home_uri=(tmp_path / "workspaceStorage").as_uri(),
            untitled_workspaces_home_uri=(tmp_path / "Workspaces").as_uri(),
            profile_id="default-profile", settings_native_uri=(tmp_path / "settings.json").as_uri(),
            reference_service=NativeReferenceService(url="http://127.0.0.1:45002/references",
                backend_id=owner.backend_id, runtime_instance_id="original-service"))

    async def reference(_management: ExtensionManagementClient, service: NativeReferenceService,
                        reference: NativeHostReference, *, operation: str) -> NativeHostReference:
        assert service.backend_id == owner.backend_id
        return reference

    async def publication(_management: ExtensionManagementClient, _service: object, _reference: object, *, profile_id: str,
                          extension_keys: list[str]) -> HostExtensionPublication:
        assert profile_id == "default-profile" and extension_keys == [KEY]
        return HostExtensionPublication(inventory=ProfileExtensionInventory(target_platform="linux-x64", profile_id=profile_id, extensions=[InstalledExtension(
            source="profile", extension_key=KEY, description={"browser": "dist/main.js"},
            is_valid=True, validations=[])]), environment=await environment(_management),
            publications=[NativeExtensionPublication(extension_id="publisher.probe", extension_key=KEY,
                native_uri=(tmp_path / "extensions" / KEY).as_uri(), extensions_root_uri=(tmp_path / "extensions").as_uri())])

    def file_read(publication: NativeExtensionPublication, *, extension_key: str,
                  relative_segments: object, file_operation: Literal["stat", "read"]) -> NativeFileRead | NativeFileStat:
        calls.append({"profile_id": "default-profile", "extension_key": extension_key,
            "extension_id": publication.extension_id, "relative_segments": relative_segments, "current_profile_member": False})
        return NativeFileStat(type=1, ctime=0, mtime=123, size=3, permissions=1) if file_operation == "stat" else NativeFileRead(content_base64="YWJj")

    monkeypatch.setattr(ExtensionManagementClient, "host_publication", publication)
    monkeypatch.setattr(ExtensionManagementClient, "environment", environment)
    monkeypatch.setattr(ExtensionManagementClient, "host_reference", reference)
    monkeypatch.setattr(owner_module, "publication_file", file_read)
    headers = {"X-Local-Token": "private-host-token", "X-BoxTeam-Gateway-Id": GATEWAY,
        "X-BoxTeam-Extension-Client-Id": "authenticated-client", "X-Request-ID": "package-private"}
    request = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": owner.backend_id,
        "view_id": "window-one", "workspace_ids": [], "profile_id": "default-profile", "extension_keys": [KEY]}
    websocket = client.websocket_connect("/api/internal/extension-web-hosts/channel", headers=headers)
    websocket_context = websocket.__enter__()
    websocket_context.send_json(request)
    binding = websocket_context.receive_json()
    payload: dict[str, object] = {"protocol_version": 1, "gateway_id": GATEWAY,
        "backend_id": owner.backend_id, "profile_id": "default-profile", "extension_key": KEY,
        "extension_id": "publisher.probe", "relative_segments": ["dist", "浏览器.js"],
        "generation": binding["generation"], "view_id": binding["view_id"], "current_profile_member": False}
    try:
        yield client, registry, payload, headers, calls
    finally:
        websocket.__exit__(None, None, None)
        assert client.portal is not None
        client.portal.call(registry.close_extension_hosts)


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_private_package_forwards_exact_publication_without_workspace_or_host(private_package, operation: str) -> None:
    client, registry, payload, headers, calls = private_package
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "package-private"
    expected = {"content_base64": "YWJj"} if operation == "read" else {"type": 1, "ctime": 0, "mtime": 123, "size": 3, "permissions": 1}
    assert response.json()["data"] == expected
    assert calls == [{name: payload[name] for name in (
        "profile_id", "extension_key", "extension_id", "relative_segments", "current_profile_member")}]
    assert len(registry.extension_hosts.active) == 1


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize("flag", ["missing", "retained"])
def test_private_package_requires_current_membership_without_runtime_binding(private_package, operation: str, flag: str) -> None:
    client, registry, payload, headers, calls = private_package
    if flag == "missing":
        del payload["current_profile_member"]
    else:
        payload["current_profile_member"] = True
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == (422 if flag == "missing" else 409)
    assert calls == [] and registry.extension_hosts._management is not None


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize("change", ["backend", "gateway", "draining", "workspace", "native-path"])
def test_private_package_rejects_wrong_target_before_node(private_package, operation: str, change: str) -> None:
    client, registry, payload, headers, calls = private_package
    if change == "backend":
        payload["backend_id"] = str(uuid4())
    elif change == "gateway":
        headers["X-BoxTeam-Gateway-Id"] = "gateway_" + "b" * 32
    elif change == "draining":
        registry.extension_hosts.close_admission()
    elif change == "workspace":
        payload["workspace_ids"] = list(registry.mounted_workspaces.workspace_ids)
    else:
        payload["native_uri"] = "file:///other-machine/secret"
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == (422 if change in {"workspace", "native-path"} else 400 if change == "gateway" else 409)
    assert calls == [] and registry.extension_hosts._management is not None


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"X-Local-Token": "local-dev-token"}, 401),
    ({"X-Local-Token": "private-host-token", "Origin": "https://browser.invalid"}, 403),
    ({"X-Local-Token": "private-host-token", "Sec-Fetch-Site": "same-origin"}, 403),
])
def test_private_package_preserves_control_authentication(private_package, operation: str,
                                                         headers: dict[str, str], status: int) -> None:
    client, registry, payload, _, calls = private_package
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == status and calls == [] and registry.extension_hosts._management is not None


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_private_package_does_not_disclose_after_backend_drain(private_package, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    client, registry, payload, headers, calls = private_package
    original = owner_module.publication_file

    def close_after_read(publication: NativeExtensionPublication, **selection):
        result = original(publication, **selection)
        registry.extension_hosts.close_admission()
        return result

    monkeypatch.setattr(owner_module, "publication_file", close_after_read)
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == 409 and len(calls) == 1
    assert "content_base64" not in response.text and "permissions" not in response.text


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize(("code", "status"), [
    ("EntryNotFound", 404), ("NoPermissions", 403), ("Invalid", 409),
    ("InstalledExtensionNotFound", 409), ("EntryTooLarge", 413), ("Unknown", 400),
])
def test_private_package_preserves_original_owner_error(private_package, monkeypatch: pytest.MonkeyPatch,
                                                      operation: str, code: str, status: int) -> None:
    client, _, payload, headers, _ = private_package

    def rejected(_publication: NativeExtensionPublication, **_selection: object):
        raise ExtensionManagementError("原包读取失败", code)

    monkeypatch.setattr(owner_module, "publication_file", rejected)
    response = client.post(f"/api/internal/extension-files/{operation}", json=payload, headers=headers)
    assert response.status_code == status
    assert response.json()["detail"] == {"owner": "extension_package", "code": code, "message": "原包读取失败"}
    assert response.headers["X-Request-ID"] == "package-private"
