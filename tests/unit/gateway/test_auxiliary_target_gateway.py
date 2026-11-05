from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.types import Message
from starlette.websockets import WebSocket

from app.core.path_utils import get_gateway_root
from app.gateway.auth import GatewayAuthContext, get_gateway_local_token
from app.gateway.auxiliary_proxy import _proxy_auxiliary_websocket, proxy_auxiliary_http
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, load_proxy_gateway_id
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    RemoteGatewayConnection,
    WorkspaceKey,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName

REMOTE_GATEWAY_ID = "gateway_" + "2" * 32
LOCAL_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
REMOTE_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def gateway_id() -> str:
    return load_proxy_gateway_id()


@pytest.fixture
def registry(tmp_path: Path, gateway_id: str) -> Iterator[GatewayWorkspaceRegistry]:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=gateway_id)
    result.upsert(
        WorkspaceTarget(
            gateway_id=gateway_id, workspace_id=LOCAL_WORKSPACE_ID, name="local", root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41100", connection_kind="local",
        ),
        runtime=WorkspaceRuntime(service_urls={
            "terminal_manager": "http://127.0.0.1:41101/api",
            "browser_manager": "http://127.0.0.1:41102/api",
        }),
        activate=False,
    )
    connection = RemoteGatewayConnection(
        connection_id="remote_connection", name="remote", host="remote.example",
        port=22, username="developer", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=8014, remote_gateway_id=REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual", remote_config_event_cursor=1,
    )
    result.apply_remote_projection_snapshot(
        connection=connection,
        projections=(WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY_ID, workspace_id=REMOTE_WORKSPACE_ID, name="remote", root_path=None, backend_url="",
            connection_kind="remote_gateway", owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("terminal_manager", "browser_manager"),
        ),),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://127.0.0.1:41104"}),
        activate=False,
    )
    yield result
    result.close()


@pytest.fixture
def credential() -> FederationCredential:
    return FederationCredentialStore(
        storage_path=get_gateway_root() / "credentials" / "federation.json",
    ).issue(connection_id="remote_connection", peer_gateway_id=REMOTE_GATEWAY_ID)


@pytest.fixture
def application(registry: GatewayWorkspaceRegistry) -> FastAPI:
    result = FastAPI()
    result.state.registry = registry
    return result


@pytest.fixture
def route_acquisitions(
    registry: GatewayWorkspaceRegistry, monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[WorkspaceKey, bool]]:
    calls: list[tuple[WorkspaceKey, bool]] = []
    acquire = registry.acquire_route_reference

    def record(workspace_key: WorkspaceKey, *, streaming: bool):
        calls.append((workspace_key, streaming))
        return acquire(workspace_key, streaming=streaming)

    monkeypatch.setattr(registry, "acquire_route_reference", record)
    return calls


@pytest.fixture
def upstream_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
async def http_client(
    application: FastAPI, upstream_requests: list[httpx.Request],
) -> AsyncIterator[httpx.AsyncClient]:
    def respond(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, content=b"upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        application.state.http_client = client
        yield client


@pytest.fixture
def websocket_upstreams(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, httpx.Headers]]:
    calls: list[tuple[str, httpx.Headers]] = []

    class Upstream:
        async def send(self, _message: str | bytes) -> None:
            raise AssertionError("本场景不发送客户端数据")

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def iterate() -> AsyncIterator[str | bytes]:
                if False:
                    yield b""
            return iterate()

    class Connect:
        async def __aenter__(self) -> Upstream:
            return Upstream()

        async def __aexit__(self, *_args: object) -> None:
            return None

    def connect(url: str, *, additional_headers: dict[str, str] | None, **_kwargs: object) -> Connect:
        calls.append((url, httpx.Headers(additional_headers)))
        return Connect()

    monkeypatch.setattr("app.gateway.auxiliary_proxy.connect", connect)
    return calls


def _request(application: FastAPI, headers: dict[str, str]) -> Request:
    request = Request({
        "type": "http", "method": "GET", "path": "/health", "app": application,
        "query_string": b"", "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    })
    request._body = b""
    request.state.request_id = "req_auxiliary_target_gateway"
    return request


def _websocket(
    application: FastAPI, headers: dict[str, str], *, query_string: bytes | None = None,
) -> tuple[WebSocket, list[Message]]:
    messages: list[Message] = []
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive() -> Message:
        return await incoming.get()

    async def send(message: Message) -> None:
        messages.append(message)

    return WebSocket({
        "type": "websocket", "path": "/terminal", "app": application,
        "query_string": query_string if query_string is not None else f"token={get_gateway_local_token()}".encode(),
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    }, receive, send), messages


@pytest.mark.parametrize("protocol", ["http", "websocket"])
@pytest.mark.parametrize("workspace_id,carrier", [
    pytest.param(LOCAL_WORKSPACE_ID, "empty", id="local-empty"),
    pytest.param(LOCAL_WORKSPACE_ID, "address", id="local-address"),
    pytest.param(LOCAL_WORKSPACE_ID, "mismatch", id="local-wrong-owner"),
    pytest.param(REMOTE_WORKSPACE_ID, "empty", id="remote-empty"),
    pytest.param(REMOTE_WORKSPACE_ID, "address", id="remote-address"),
    pytest.param(REMOTE_WORKSPACE_ID, "mismatch", id="remote-wrong-owner"),
    pytest.param(REMOTE_WORKSPACE_ID, "missing", id="remote-missing-owner"),
])
async def test_auxiliary_rejects_target_gateway_before_route(
    application: FastAPI, gateway_id: str, registry: GatewayWorkspaceRegistry,
    http_client: httpx.AsyncClient, upstream_requests: list[httpx.Request],
    websocket_upstreams: list[tuple[str, httpx.Headers]], route_acquisitions: list[tuple[WorkspaceKey, bool]],
    protocol: str, workspace_id: str, carrier: str,
) -> None:
    value = {
        "empty": "", "address": "127.0.0.1:8014",
        "mismatch": REMOTE_GATEWAY_ID if workspace_id == LOCAL_WORKSPACE_ID else gateway_id,
        "missing": None,
    }[carrier]
    headers = {"X-BoxTeam-Gateway-Id": REMOTE_GATEWAY_ID}
    if value is not None:
        headers[TARGET_GATEWAY_ID_HEADER] = value
    if protocol == "http":
        with pytest.raises(HTTPException) as captured:
            await proxy_auxiliary_http(
                workspace_id, "terminal-manager", "health", _request(application, headers),
                GatewayAuthContext(kind="local"),
            )
        if carrier in {"mismatch", "missing"}:
            assert captured.value.status_code == 404
            assert captured.value.detail == "Gateway 工作区不存在: stage=gateway-workspace-resolve, error=LookupError"
            cause = captured.value.__cause__
            assert isinstance(cause, LookupError)
            assert isinstance(cause.__cause__, LookupError)
            attempted_owner = (value or gateway_id, workspace_id)
            assert str(cause.__cause__) == f"Gateway 工作区不存在: {attempted_owner}"
        else:
            assert captured.value.status_code == 400
            assert "非法 gateway_id" in str(captured.value.detail)
    else:
        websocket, messages = _websocket(application, headers)
        await _proxy_auxiliary_websocket(
            websocket=websocket, workspace_id=workspace_id,
            service="terminal_manager", socket_path="terminal",
        )
        assert messages == [{"type": "websocket.close", "code": 1008, "reason": "auxiliary_websocket_access_failed"}]
    assert route_acquisitions == []
    assert upstream_requests == [] and websocket_upstreams == []
    owner = gateway_id if workspace_id == LOCAL_WORKSPACE_ID else REMOTE_GATEWAY_ID
    assert registry.route_reference_counts((owner, workspace_id)) == (0, 0)


@pytest.mark.parametrize("protocol", ["http", "websocket"])
@pytest.mark.parametrize("workspace_id,explicit", [
    pytest.param(LOCAL_WORKSPACE_ID, False, id="local-default-owner"),
    pytest.param(LOCAL_WORKSPACE_ID, True, id="local-explicit-owner"),
    pytest.param(REMOTE_WORKSPACE_ID, True, id="remote-explicit-owner"),
])
@pytest.mark.parametrize("service", ["terminal_manager", "browser_manager"])
async def test_auxiliary_forwards_authoritative_target_and_preserves_workspace_contract(
    application: FastAPI, gateway_id: str, registry: GatewayWorkspaceRegistry,
    credential: FederationCredential, http_client: httpx.AsyncClient,
    upstream_requests: list[httpx.Request], websocket_upstreams: list[tuple[str, httpx.Headers]],
    route_acquisitions: list[tuple[WorkspaceKey, bool]], protocol: str, workspace_id: str, explicit: bool,
    service: GatewayServiceName,
) -> None:
    remote = workspace_id == REMOTE_WORKSPACE_ID
    owner = REMOTE_GATEWAY_ID if remote else gateway_id
    headers = {"X-BoxTeam-Gateway-Id": "gateway_" + "3" * 32,
               "X-BoxTeam-Workspace-Id": workspace_id if protocol == "websocket" else (LOCAL_WORKSPACE_ID if remote else REMOTE_WORKSPACE_ID),
               "X-Request-ID": "req_forged_sender", "X-Local-Token": get_gateway_local_token()}
    if explicit:
        headers[TARGET_GATEWAY_ID_HEADER] = owner
    service_path = "terminal-manager" if service == "terminal_manager" else "browser-manager"
    if protocol == "http":
        response = await proxy_auxiliary_http(
            workspace_id, service_path, "health", _request(application, headers),
            GatewayAuthContext(kind="local"),
        )
        assert response.status_code == 200 and response.body == b"upstream"
        assert len(upstream_requests) == 1
        forwarded = upstream_requests[0].headers
        assert forwarded.get_list("X-BoxTeam-Workspace-Id") == [workspace_id]
        assert forwarded.get_list("X-Request-ID") == ["req_auxiliary_target_gateway"]
        if remote:
            assert upstream_requests[0].url.path == f"/api/gateway/workspaces/{REMOTE_WORKSPACE_ID}/{service_path}/health"
        else:
            assert upstream_requests[0].url.path == "/api/health"
        assert route_acquisitions == [((owner, workspace_id), False)]
    else:
        websocket, messages = _websocket(application, headers)
        socket_path = "terminal" if service == "terminal_manager" else "browser"
        await _proxy_auxiliary_websocket(
            websocket=websocket, workspace_id=workspace_id, service=service, socket_path=socket_path,
        )
        assert messages == [{"type": "websocket.accept", "subprotocol": None, "headers": []}]
        assert len(websocket_upstreams) == 1
        url, forwarded = websocket_upstreams[0]
        if remote:
            assert url == f"ws://127.0.0.1:41104/api/gateway/workspaces/{REMOTE_WORKSPACE_ID}/{service_path}/{socket_path}"
            assert "X-BoxTeam-Workspace-Id" not in forwarded
        else:
            port = 41101 if service == "terminal_manager" else 41102
            assert url == f"ws://127.0.0.1:{port}/api/{socket_path}"
            assert forwarded.get_list("X-BoxTeam-Workspace-Id") == [LOCAL_WORKSPACE_ID]
        assert "X-Request-ID" not in forwarded
        assert route_acquisitions == [((owner, workspace_id), True)]
    assert forwarded.get_list("X-BoxTeam-Gateway-Id") == [gateway_id]
    assert forwarded.get_list(TARGET_GATEWAY_ID_HEADER) == ([REMOTE_GATEWAY_ID] if remote else [])
    assert forwarded.get_list("X-BoxTeam-Federation-Token") == ([credential.token] if remote else [])
    assert "X-BoxTeam-Backend-Workspace-Id" not in forwarded
    assert "X-Local-Token" not in forwarded
    assert registry.route_reference_counts((owner, workspace_id)) == (0, 0)


@pytest.mark.parametrize("protocol,rejection", [
    ("http", "backend"), ("http", "federation"), ("http", "management"),
    ("websocket", "backend"), ("websocket", "federation"),
    ("websocket", "token"), ("websocket", "workspace_query"), ("websocket", "workspace_header"),
])
async def test_correct_gateway_target_preserves_existing_access_boundaries(
    application: FastAPI, gateway_id: str, registry: GatewayWorkspaceRegistry,
    credential: FederationCredential, http_client: httpx.AsyncClient,
    upstream_requests: list[httpx.Request], websocket_upstreams: list[tuple[str, httpx.Headers]],
    route_acquisitions: list[tuple[WorkspaceKey, bool]], protocol: str, rejection: str,
) -> None:
    workspace_id = REMOTE_WORKSPACE_ID if rejection == "federation" else LOCAL_WORKSPACE_ID
    owner = REMOTE_GATEWAY_ID if rejection == "federation" else gateway_id
    headers = {TARGET_GATEWAY_ID_HEADER: owner}
    if rejection == "backend":
        headers["X-BoxTeam-Backend-Workspace-Id"] = REMOTE_WORKSPACE_ID
    elif rejection == "federation":
        headers["X-BoxTeam-Federation-Token"] = credential.token
    elif rejection == "workspace_header":
        headers["X-BoxTeam-Workspace-Id"] = REMOTE_WORKSPACE_ID
    if protocol == "http":
        with pytest.raises(HTTPException) as captured:
            await proxy_auxiliary_http(
                workspace_id, "terminal-manager", "api/terminals" if rejection == "management" else "health",
                _request(application, headers),
                GatewayAuthContext(kind="federation" if rejection == "federation" else "local"),
            )
        assert captured.value.status_code == (403 if rejection == "management" else 400)
        if rejection == "management":
            assert "/api/v1/resources" in str(captured.value.detail)
        elif rejection == "federation":
            assert captured.value.detail == "bounded federation 禁止代理嵌套远程辅助服务"
        else:
            assert captured.value.detail == "旧 Backend 工作区身份头已失效"
    else:
        query_string = None
        if rejection == "token":
            query_string = b"token=wrong"
        elif rejection == "workspace_query":
            query_string = f"token={get_gateway_local_token()}&workspace_id={REMOTE_WORKSPACE_ID}".encode()
        websocket, messages = _websocket(application, headers, query_string=query_string)
        await _proxy_auxiliary_websocket(
            websocket=websocket, workspace_id=workspace_id,
            service="terminal_manager", socket_path="terminal",
        )
        assert messages == [{"type": "websocket.close", "code": 1008, "reason": "auxiliary_websocket_access_failed"}]
    assert route_acquisitions == []
    assert upstream_requests == [] and websocket_upstreams == []
    assert registry.route_reference_counts((owner, workspace_id)) == (0, 0)
