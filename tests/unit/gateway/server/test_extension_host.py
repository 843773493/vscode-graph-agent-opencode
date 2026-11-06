"""认证与失效经过真实 Gateway 路由；只替换目标网络传输。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server import extension_host
from app.gateway.server.extension_host import _upstream as real_upstream
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostOpenRequest,
)

GATEWAY = "gateway_" + "a" * 32
PEER = "gateway_" + "b" * 32
PATH = "/api/gateway/extension-hosts/channel"


@dataclass
class Harness:
    client: TestClient
    registry: GatewayWorkspaceRegistry
    users: UserAccessService
    access: UserAccessContext
    policy: FederationPolicyStore
    request: ExtensionHostOpenRequest
    calls: list[dict[str, str]]
    token: str
    federation_token: str


@pytest.fixture
def host_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY)
    workspaces = [str(uuid4()), str(uuid4())]
    for workspace_id in workspaces:
        registry.upsert(WorkspaceTarget(gateway_id=GATEWAY, workspace_id=workspace_id, name="same",
            root_path=str(tmp_path / workspace_id), backend_url="http://127.0.0.1:41100", connection_kind="local"),
            activate=False)
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    users = UserAccessService(state=state)
    users.create_user(display_name="Host test", user_id="host-test")
    access = users.acquire_user(user_id="host-test", client_label="window", takeover=False)
    policy = FederationPolicyStore()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.state.registry = registry
    application.state.user_access_service = users
    application.state.federation_policy_store = policy
    application.include_router(extension_host.router)
    calls: list[dict[str, str]] = []

    async def upstream(websocket, *, url, headers, payload, **_):
        calls.append({**headers, "url": url})
        result = ExtensionHostConnectionDTO(**payload.model_dump(), client_id=headers[extension_host.CLIENT_HEADER],
            generation=str(uuid4()), request_id=headers["X-Request-ID"])
        await websocket.send_text(result.model_dump_json())
        await asyncio.Event().wait()

    monkeypatch.setattr(extension_host, "_upstream", upstream)
    federation = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="test-peer", peer_gateway_id=PEER)
    with TestClient(application) as client:
        client.cookies.set(USER_ACCESS_COOKIE_NAME, access.access_session_id)
        try:
            yield Harness(client, registry, users, access, policy, ExtensionHostOpenRequest.model_validate({
                "protocol_version": 1, "gateway_id": GATEWAY, "backend_id": str(uuid4()),
                "view_id": "window-a", "workspace_ids": workspaces,
            }), calls, get_gateway_local_token(), federation.token)
        finally:
            registry.close()
            state.close()


def test_requires_local_token_and_user_lease(host_gateway: Harness) -> None:
    h = host_gateway
    with pytest.raises(WebSocketDenialResponse) as missing_token, h.client.websocket_connect(PATH):
        pass
    assert missing_token.value.status_code == 401
    h.client.cookies.clear()
    with pytest.raises(WebSocketDenialResponse) as missing_user, h.client.websocket_connect(f"{PATH}?token={h.token}"):
        pass
    assert missing_user.value.status_code == 401
    assert h.calls == []


@pytest.mark.parametrize("invalidate", ["route", "user"])
def test_idle_invalidation_releases_all_routes(host_gateway: Harness, invalidate: str) -> None:
    h = host_gateway
    with h.client.websocket_connect(f"{PATH}?token={h.token}", headers={
        "X-Request-ID": "host-request", extension_host.CLIENT_HEADER: "forged-client"}) as socket:
        socket.send_json(h.request.model_dump())
        binding = socket.receive_json()
        assert binding["request_id"] == "host-request"
        assert socket.extra_headers is not None
        assert dict(socket.extra_headers)[b"x-request-id"] == b"host-request"
        assert binding["client_id"] not in {"forged-client", h.access.access_session_id}
        assert len(binding["client_id"]) == 64
        assert h.calls[0]["X-Local-Token"] == h.token
        assert all("cookie" not in name.lower() for name in h.calls[0])
        for workspace_id in h.request.workspace_ids:
            assert h.registry.route_reference_counts((GATEWAY, workspace_id)) == (0, 1)
        assert h.client.portal is not None
        if invalidate == "route":
            h.client.portal.call(h.registry.invalidate_route, (GATEWAY, h.request.workspace_ids[1]))
        else:
            h.client.portal.call(h.users.release, h.access)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011
    for workspace_id in h.request.workspace_ids:
        assert h.registry.route_reference_counts((GATEWAY, workspace_id)) == (0, 0)


@pytest.mark.parametrize("change", ["unknown-gateway", "duplicate-workspace", "version", "boolean-version", "client-id", "mixed-backend"])
def test_bad_open_request_never_reaches_target(host_gateway: Harness, change: str) -> None:
    h = host_gateway
    payload = h.request.model_dump()
    if change == "unknown-gateway":
        payload["gateway_id"] = PEER
    elif change == "duplicate-workspace":
        payload["workspace_ids"] = [h.request.workspace_ids[0]] * 2
    elif change == "version":
        payload["protocol_version"] = 2
    elif change == "boolean-version":
        payload["protocol_version"] = True
    elif change == "client-id":
        payload["client_id"] = "forged"
    else:
        second = h.registry.resolve((GATEWAY, h.request.workspace_ids[1]))
        second.backend_url = "http://127.0.0.1:41101"
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(payload)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert h.calls == []
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


@pytest.mark.parametrize("field", ["client_id", "request_id", "backend_id", "workspace_ids"])
def test_wrong_target_handshake_is_never_disclosed(host_gateway: Harness, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    h = host_gateway
    monkeypatch.setattr(extension_host, "_upstream", real_upstream)

    class Upstream:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers
            self.request = h.request

        async def send(self, raw: str) -> None:
            self.request = ExtensionHostOpenRequest.model_validate_json(raw)

        async def recv(self) -> str:
            binding = {**self.request.model_dump(), "client_id": self.headers[extension_host.CLIENT_HEADER],
                       "generation": str(uuid4()), "request_id": self.headers["X-Request-ID"]}
            binding[field] = [str(uuid4())] if field == "workspace_ids" else str(uuid4())
            return ExtensionHostConnectionDTO.model_validate(binding).model_dump_json()

    @asynccontextmanager
    async def connect(_url: str, *, additional_headers: dict[str, str], **_):
        yield Upstream(additional_headers)

    monkeypatch.setattr(extension_host, "connect", connect)
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(h.request.model_dump())
        with pytest.raises(WebSocketDisconnect) as rejected:
            socket.receive_json()
        assert rejected.value.code == 1011
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_native_federation_permission_and_idle_revocation(host_gateway: Harness) -> None:
    h = host_gateway
    headers = {"X-BoxTeam-Federation-Token": h.federation_token, extension_host.CLIENT_HEADER: "origin-client"}
    with pytest.raises(WebSocketDenialResponse) as denied, h.client.websocket_connect(PATH, headers=headers):
        pass
    assert denied.value.status_code == 403
    assert h.calls == []
    h.policy.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow",
        "principal_ref": principal_ref_for_gateway(PEER)}]})
    with h.client.websocket_connect(PATH, headers=headers) as socket:
        socket.send_json(h.request.model_dump())
        binding = socket.receive_json()
        assert binding["client_id"] == f"peer:{PEER}:origin-client"
        assert h.client.portal is not None
        h.client.portal.call(h.policy.publish, {"rules": []})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_idle_channel_expires_without_renewing_user_lease(host_gateway: Harness) -> None:
    h = host_gateway
    # 时间边界装具只缩短原租约；解析、到期检查与关闭仍走原产品入口。
    expires = (datetime.now(UTC) + timedelta(seconds=0.2)).isoformat()
    connection = h.users._state.connection()
    try:
        connection.execute("UPDATE user_access_lease SET expires_at = ? WHERE access_session_id = ?",
                           (expires, h.access.access_session_id))
    finally:
        connection.close()
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(h.request.model_dump())
        assert socket.receive_json()["view_id"] == h.request.view_id
        with pytest.raises(WebSocketDisconnect) as expired:
            socket.receive_json()
        assert expired.value.code == 1011
    assert h.users.expires_at(h.access) == expires
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_public_shapes_follow_descriptor() -> None:
    from app.protocol.generated.boxteam.gateway.v2 import public_pb2
    from app.protocol.schema import assert_public_schema

    for model in (ExtensionHostOpenRequest, ExtensionHostConnectionDTO):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])
