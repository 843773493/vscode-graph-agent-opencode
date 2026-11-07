"""私有管理通道只接受原生本机 Backend，保留同一请求身份。"""

from __future__ import annotations

import httpx
import pytest
from starlette.responses import Response

from app.gateway.server import extension_management as management
from app.gateway.server.extension_management import router
from tests.unit.gateway.server.test_extension_installation import (
    host_gateway as host_gateway,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_installation import (
    host_initialization as host_initialization,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_installation import (
    native_control as native_control,  # noqa: PLC0414
)


@pytest.mark.parametrize("denial", [None, "missing-token", "federation", "remote-address", "origin", "fetch", "wrong-domain", "unconfigured"])
def test_private_control_preserves_original_scope_and_trace(host_gateway, native_control, denial):
    h = host_gateway
    h.application.include_router(router)
    h.client._transport.client = ("192.0.2.1" if denial == "remote-address" else "127.0.0.1", 45000)
    headers = {"X-Request-ID": "private-control"}
    if denial != "missing-token":
        headers["X-BoxTeam-Federation-Token" if denial == "federation" else "X-Local-Token"] = (
            h.federation_token if denial == "federation" else h.token)
    if denial == "origin":
        headers["Origin"] = "http://127.0.0.1"
    if denial == "fetch":
        headers["Sec-Fetch-Site"] = "same-origin"
    if denial == "unconfigured":
        h.application.state.extension_management = None
    backend_id = "00000000-0000-0000-0000-000000000000" if denial == "wrong-domain" else h.request.backend_id
    response = h.client.post(f"/api/gateway/internal/extension-management/{backend_id}", json={"method": "inventory"}, headers=headers)
    expected = (200 if denial is None else 401 if denial in {"missing-token", "federation"}
        else 409 if denial == "wrong-domain" else 503 if denial == "unconfigured" else 403)
    assert response.status_code == expected, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "private-control"
    assert native_control.calls == ([{"method": "inventory"}] if denial is None else [])
    if denial is None:
        assert response.json()["data"]["result"]["profile_id"] == "default-profile"
    else:
        assert "data" not in response.json()


@pytest.mark.parametrize("method", ["eval", "shell", "watch", "configuration-read", "configuration-write", "configuration-view"])
def test_private_control_rejects_unregistered_operations_before_node(host_gateway, native_control, method):
    h = host_gateway
    h.application.include_router(router)
    h.client._transport.client = ("127.0.0.1", 45000)
    response = h.client.post(f"/api/gateway/internal/extension-management/{h.request.backend_id}",
        json={"method": method, "native_uri": "file:///tmp/arbitrary", "script": "must not execute"},
        headers={"X-Local-Token": h.token, "X-Request-ID": "rejected-operation"})
    assert response.status_code == 422, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "rejected-operation"
    assert native_control.calls == []


@pytest.fixture
def download_route(host_gateway, native_control, monkeypatch: pytest.MonkeyPatch):
    h = host_gateway
    h.application.include_router(router)
    h.client._transport.client = ("127.0.0.1", 45000)
    capability, request_id = "a" * 32, "original-download-request"
    native_control.owner._download_admission = capability, request_id
    received: list[management.ExtensionDownloadRequest] = []

    async def download(payload: management.ExtensionDownloadRequest) -> Response:
        received.append(payload)
        return Response(b"target-package", status_code=206, headers={"X-Origin": "preserved"})

    monkeypatch.setattr(management, "extension_download", download)
    url = f"/api/gateway/internal/extension-management/{native_control.owner.backend_id}/downloads/{capability}"
    headers = {"X-Local-Token": h.token, "X-Request-ID": request_id}
    payload = {"call_site": "extensionGalleryService.download", "url": "https://assets.example/package.vsix?sig=private",
        "strict_ssl": True}
    return h, native_control.owner, url, headers, payload, received


@pytest.mark.parametrize("denial", [None, "missing-token", "federation", "remote", "origin", "fetch", "domain", "capability", "request", "unregistered"])
def test_download_callback_checks_local_auth_original_capability_and_request_before_transmission(download_route, denial):
    h, owner, url, headers, payload, received = download_route
    if denial == "missing-token":
        headers.pop("X-Local-Token")
    if denial == "federation":
        headers = {"X-BoxTeam-Federation-Token": h.federation_token, "X-Request-ID": headers["X-Request-ID"]}
    if denial == "remote":
        h.client._transport.client = ("192.0.2.1", 45000)
    if denial == "origin":
        headers["Origin"] = "http://127.0.0.1"
    if denial == "fetch":
        headers["Sec-Fetch-Site"] = "same-origin"
    if denial == "domain":
        url = url.replace(owner.backend_id, "00000000-0000-0000-0000-000000000000")
    if denial == "capability":
        url = url.replace("a" * 32, "b" * 32)
    if denial == "request":
        headers["X-Request-ID"] = "another-request"
    if denial == "unregistered":
        payload["call_site"] = "arbitrary-fetch"
    # 原回执未知不阻断已受理安装；普通管理健康准入仍然拒绝新请求。
    owner._failure = httpx.ReadTimeout("原控制回执未知")
    response = h.client.post(url, headers=headers, json=payload)
    expected = 206 if denial is None else 401 if denial in {"missing-token", "federation"} else 422 if denial == "unregistered" else 403
    assert response.status_code == expected, response.text
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert len(received) == (1 if denial is None else 0)
    if denial is None:
        assert response.content == b"target-package" and response.headers["X-Origin"] == "preserved"


def test_download_transport_failure_reports_type_without_signed_url_or_proxy_secret(download_route, monkeypatch: pytest.MonkeyPatch):
    h, _, url, headers, payload, _ = download_route

    async def fail(_payload: management.ExtensionDownloadRequest) -> Response:
        raise httpx.ConnectError("https://assets.example/package.vsix?sig=private proxy=private-proxy-credential")

    monkeypatch.setattr(management, "extension_download", fail)
    response = h.client.post(url, headers=headers, json=payload)
    assert response.status_code == 502 and response.headers[management.DOWNLOAD_ERROR_HEADER] == "ConnectError"
    assert response.text == "目标扩展下载传输失败: ConnectError"


def test_install_callback_uses_actual_listener_instead_of_host_header(download_route, monkeypatch: pytest.MonkeyPatch):
    h, owner, _, headers, _, _ = download_route
    seen: list[str | None] = []

    async def install(command, *, download_base_url=None):
        assert command["method"] == "install"
        seen.append(download_base_url)
        return {"error": "fixture-terminal", "code": "ExpectedTestResult"}

    monkeypatch.setattr(owner, "request", install)
    response = h.client.post(f"/api/gateway/internal/extension-management/{owner.backend_id}",
        json={"method": "install", "vsix_path": "/target/package.vsix"}, headers={**headers, "Host": "untrusted.example:9000"})
    assert response.status_code == 200, response.text
    assert seen == ["http://127.0.0.1:80"]
