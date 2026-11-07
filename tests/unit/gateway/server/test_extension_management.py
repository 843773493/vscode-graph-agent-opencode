"""私有管理通道只接受原生本机 Backend，保留同一请求身份。"""

from __future__ import annotations

import pytest

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
