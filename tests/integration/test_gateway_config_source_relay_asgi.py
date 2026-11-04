"""来源 Relay 的 ASGI、原发送器与连接引用集成；远端 HTTP 由 MockTransport 控制。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.config import GatewayConfigReloadService
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes import health_config
from app.gateway.routes._shared import get_gateway_config_reload_service
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.config import (
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDocumentDTO,
    GatewayConfigSourceEditRequest,
    GatewayConfigSourcesDTO,
)
from tests.support.gateway_managed_backends import ProcessProbe

LOCAL = "gateway_" + "a" * 32
REMOTE = "gateway_" + "b" * 32
OTHER = "gateway_" + "c" * 32
CONNECTION = "rgw_source_relay"
SOURCE = "gateway_mutable_override"
ENDPOINT = "/api/gateway/config/sources/" + SOURCE


@dataclass
class RelayReply:
    status: int = 200
    content: bytes | None = None
    error: bool = False
    wrong_owner: str | None = None
    wrong_source: bool = False
    response_content: bytes | None = None
    blocked: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[httpx.Request] = field(default_factory=list)


@dataclass
class RelayCase:
    app: FastAPI
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    connection: RemoteGatewayConnection
    credential: FederationCredential
    store: FederationCredentialStore
    process: ProcessProbe
    reply: RelayReply
    local_owner: Mock
    body: bytes


@pytest.fixture
async def relay_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RelayCase]:
    from app.gateway.main import gateway_http_exception_handler

    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL, storage_path=tmp_path / "workspaces.json")
    connection = RemoteGatewayConnection(
        connection_id=CONNECTION, name="B", host="remote.test", port=22, username="tester",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=REMOTE, protocol_version=2, source_owner="manual",
    )
    process = ProcessProbe("ssh_tunnel")
    registry.upsert_remote_gateway(connection, runtime=WorkspaceRuntime(
        service_urls={"workspace_api": "http://remote.test"}, processes={"ssh_tunnel": process},
    ))
    store = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json")
    credential = store.issue(connection_id=CONNECTION, peer_gateway_id=LOCAL)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(gateway_http_exception_handler)
    application.include_router(health_config.router)
    application.state.gateway_id = LOCAL
    application.state.registry = registry
    application.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    local_owner = Mock(spec=GatewayConfigReloadService)
    application.dependency_overrides[get_gateway_config_reload_service] = lambda: local_owner
    client_class = httpx.AsyncClient
    client = client_class(transport=httpx.ASGITransport(app=application), base_url="http://gateway-a")
    reply = RelayReply()

    async def respond(request: httpx.Request) -> httpx.Response:
        reply.requests.append(request)
        reply.entered.set()
        if reply.blocked:
            await reply.release.wait()
        if reply.error:
            raise httpx.ReadTimeout("远端结果未知", request=request)
        request_id = request.headers["X-Request-ID"]
        headers = {"Content-Type": "application/json; charset=utf-8", "X-Request-ID": request_id}
        if reply.content is not None:
            return httpx.Response(reply.status, content=reply.content, headers=headers)
        if request.url.path == "/api/gateway/config/sources":
            data = GatewayConfigSourcesDTO(revision="remote-revision", schema_uri=None).model_dump(mode="json")
        else:
            data = GatewayConfigSourceDocumentDTO(
                gateway_id=reply.wrong_owner or REMOTE, source_key=SOURCE if not reply.wrong_source else "other",
                layer="user", writable=True, jsonc="{ // B 原文\n}\n", file_presence="present",
                json_schema={}, effective={}, source_effective={}, layers=[], origins=[],
                status=GatewayConfigReloadStatusDTO(),
            ).model_dump(mode="json")
            if request.url.path.endswith("/preflight"):
                data = {"document": data, "candidate": {}, "candidate_digest": "candidate", "changed_paths": []}
        content = json.dumps({"code": 0, "data": data, "request_id": request_id}, ensure_ascii=False, indent=2).encode()
        reply.response_content = content
        return httpx.Response(reply.status, content=content, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond),
    ))
    payload = GatewayConfigSourceEditRequest(
        gateway_id=REMOTE, source_key=SOURCE, jsonc='{ // 注释与空白原样\n  "value": 1.0\n}\n',
        base_file_digest="file", base_layer_revision=3, base_layer_digest="layer",
        expected_active_revision=5, expected_active_digest="active", idempotency_key="原键/+% 空格",
    )
    case = RelayCase(application, client, registry, connection, credential, store, process, reply, local_owner,
                     json.dumps(payload.model_dump(), ensure_ascii=False, indent=2).encode())
    try:
        yield case
    finally:
        await client.aclose()
        assert not local_owner.mock_calls, "远端来源操作不应调用本机配置 owner"


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/gateway/config/sources"), ("GET", ENDPOINT),
                                              ("POST", ENDPOINT + "/preflight"), ("PUT", ENDPOINT)])
async def test_relay_preserves_body_key_credentials_target_and_request_id(
    relay_case: RelayCase, method: str, path: str,
) -> None:
    case = relay_case
    headers = {TARGET_GATEWAY_ID_HEADER: REMOTE, "X-Request-ID": "same-request", "Content-Type": "application/json",
               "X-Local-Token": "never-forward", "Cookie": "private=cookie", "X-BoxTeam-Gateway-Id": OTHER}
    body = case.body if method != "GET" else b""
    response = await case.client.request(method, path, content=body, headers=headers,
                                         params={"idempotency_key": "原键/+% 空格", "gateway_connection_id": CONNECTION})
    assert response.status_code == 200, response.text
    assert response.content == case.reply.response_content
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "same-request"
    assert len(case.reply.requests) == 1
    sent = case.reply.requests[0]
    assert sent.method == method and sent.url.path == path and sent.content == body
    assert list(sent.url.params.multi_items()) == [("idempotency_key", "原键/+% 空格")]
    assert sent.headers["X-BoxTeam-Federation-Token"] == case.credential.token
    assert sent.headers[TARGET_GATEWAY_ID_HEADER] == REMOTE
    assert sent.headers["X-BoxTeam-Gateway-Id"] == LOCAL
    assert sent.headers["X-Request-ID"] == "same-request"
    assert "X-Local-Token" not in sent.headers and "Cookie" not in sent.headers
    assert not case.process.closed and case.registry.remote_gateway_connection(CONNECTION) == case.connection


async def test_header_only_unique_connection_reaches_registered_target(relay_case: RelayCase) -> None:
    response = await relay_case.client.get(ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE})
    assert response.status_code == 200, response.text
    assert len(relay_case.reply.requests) == 1


@pytest.mark.parametrize(("target", "connection_id", "status"), [
    (None, CONNECTION, 400), ("invalid", None, 400), (REMOTE, "rgw_unknown", 404),
    (OTHER, CONNECTION, 409), (OTHER, None, 404), (LOCAL, CONNECTION, 409),
])
async def test_invalid_target_never_sends(
    relay_case: RelayCase, target: str | None, connection_id: str | None, status: int,
) -> None:
    headers = {TARGET_GATEWAY_ID_HEADER: target} if target is not None else {}
    params = {"gateway_connection_id": connection_id} if connection_id is not None else {}
    response = await relay_case.client.get(ENDPOINT, headers=headers, params=params)
    assert response.status_code == status, response.text
    assert not relay_case.reply.requests


async def test_multiple_connections_require_explicit_locator(relay_case: RelayCase) -> None:
    case = relay_case
    case.registry.upsert_remote_gateway(replace(case.connection, connection_id="rgw_other", connection_error="离线"))
    response = await case.client.get(ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE})
    assert response.status_code == 409 and not case.reply.requests
    explicit = await case.client.get(ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE},
                                     params={"gateway_connection_id": CONNECTION})
    assert explicit.status_code == 200 and len(case.reply.requests) == 1


@pytest.mark.parametrize(("unavailable", "status"), [("offline", 404), ("exited", 409), ("credential", 404), ("expired", 409)])
async def test_unavailable_connection_never_sends(relay_case: RelayCase, unavailable: str, status: int) -> None:
    case = relay_case
    if unavailable == "offline":
        case.registry.upsert_remote_gateway(replace(case.connection, connection_error="离线"))
    elif unavailable == "exited":
        case.process.alive = False
    elif unavailable == "credential":
        case.store.remove(CONNECTION)
    else:
        case.store.put(replace(case.credential, expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    response = await case.client.get(ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE})
    assert response.status_code == status, response.text
    assert not case.reply.requests


async def test_federation_inbound_cannot_nest_relay(relay_case: RelayCase) -> None:
    case = relay_case
    case.app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="federation", peer_gateway_id=OTHER)
    response = await case.client.put(ENDPOINT, content=case.body, headers={TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json"})
    assert response.status_code == 403, response.text
    assert not case.reply.requests


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 503])
async def test_native_rejection_keeps_status_and_original_redacted_body(relay_case: RelayCase, status: int) -> None:
    case = relay_case
    case.reply.status = status
    case.reply.content = b'{ "code": 409, "message": "native refusal", "data": null, "request_id": "refusal" }\n'
    response = await case.client.put(ENDPOINT, content=case.body, headers={
        TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json", "X-Request-ID": "refusal",
    })
    assert response.status_code == status and response.content == case.reply.content
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.headers["X-Request-ID"] == "refusal" and len(case.reply.requests) == 1
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
@pytest.mark.parametrize("wrong", ["gateway", "source"])
async def test_success_with_wrong_owner_is_rejected(relay_case: RelayCase, method: str, wrong: str) -> None:
    case = relay_case
    case.reply.wrong_owner = OTHER if wrong == "gateway" else None
    case.reply.wrong_source = wrong == "source"
    response = await case.client.request(method, ENDPOINT + ("/preflight" if method == "POST" else ""),
                                         content=case.body if method != "GET" else b"",
                                         headers={TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json"})
    assert response.status_code == 502 and len(case.reply.requests) == 1


async def test_transport_failure_does_not_replay_and_original_key_remains_queryable(relay_case: RelayCase) -> None:
    case = relay_case
    case.reply.error = True
    failed = await case.client.put(ENDPOINT, content=case.body, headers={TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json"})
    assert failed.status_code == 502 and len(case.reply.requests) == 1
    case.reply.error = False
    query = await case.client.get(ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE}, params={"idempotency_key": "原键/+% 空格"})
    assert query.status_code == 200 and len(case.reply.requests) == 2
    assert case.reply.requests[-1].url.params["idempotency_key"] == "原键/+% 空格"


async def test_cancellation_releases_frozen_tunnel_after_connection_replacement(relay_case: RelayCase) -> None:
    case = relay_case
    case.reply.blocked = True
    task = asyncio.create_task(case.client.put(ENDPOINT, content=case.body,
                               headers={TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json"}))
    await asyncio.wait_for(case.reply.entered.wait(), timeout=3)
    case.registry.upsert_remote_gateway(case.connection, runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://replacement.test"}))
    assert not case.process.closed
    with pytest.raises(RuntimeError, match="管理请求引用"):
        case.registry.remove_remote_gateway(CONNECTION)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(case.reply.requests) == 1 and case.reply.requests[0].url.host == "remote.test"
    assert case.process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
    assert case.registry.remote_gateway_url(CONNECTION) == "http://replacement.test"
    case.registry.remove_remote_gateway(CONNECTION)


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "config/reload-status"), ("POST", "config/retry-restart"), ("POST", "config/resolve-restart"),
    ("POST", "config/discard-restart"), ("GET", "config/events"), ("GET", "config/events/stream"),
    ("POST", "runtime/restart-development"),
])
async def test_native_only_operation_rejects_remote_target_without_side_effects(
    relay_case: RelayCase, monkeypatch: pytest.MonkeyPatch, method: str, path: str,
) -> None:
    restart = Mock()
    monkeypatch.setattr(health_config, "start_development_restart", restart)
    response = await relay_case.client.request(method, "/api/gateway/" + path,
                                               headers={TARGET_GATEWAY_ID_HEADER: REMOTE}, json={})
    assert response.status_code == 409, response.text
    restart.assert_not_called()
    assert not relay_case.reply.requests
