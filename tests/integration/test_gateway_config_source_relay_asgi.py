"""来源 Relay 的 ASGI、原发送器与连接引用集成；远端 HTTP 由 MockTransport 控制。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.types import Message

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.config import GatewayConfigReloadService
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.federation import (
    RemoteGatewayConnection,
    request_remote_gateway_management,
)
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes import health_config, workspaces_lifecycle
from app.gateway.routes._shared import get_gateway_config_reload_service
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.config import (
    GatewayConfigEventsDTO,
    GatewayConfigPendingDiscardRequest,
    GatewayConfigPendingHealthProofRequest,
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
STREAM_ENDPOINT = "/api/gateway/config/events/stream"


@dataclass
class RelayStream(httpx.AsyncByteStream):
    chunks: asyncio.Queue[bytes | BaseException | None] = field(default_factory=asyncio.Queue)
    read_entered: asyncio.Event = field(default_factory=asyncio.Event)
    close_entered: asyncio.Event = field(default_factory=asyncio.Event)
    close_release: asyncio.Event = field(default_factory=asyncio.Event)
    close_events: list[str] = field(default_factory=list)
    cancellations: list[asyncio.CancelledError] = field(default_factory=list)
    close_blocked: bool = False
    close_error: OSError | None = None
    close_calls: int = 0
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_entered.set()
        while True:
            try:
                chunk = await self.chunks.get()
            except asyncio.CancelledError as error:
                self.cancellations.append(error)
                raise
            if chunk is None:
                return
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.close_blocked:
            await self.close_release.wait()
        self.close_events.append("body-close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


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
    stream: RelayStream | None = None
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    close_events: list[str] = field(default_factory=list)
    client_close_entered: asyncio.Event = field(default_factory=asyncio.Event)
    client_close_release: asyncio.Event = field(default_factory=asyncio.Event)
    client_close_blocked: bool = False
    client_close_error: OSError | None = None
    response_type: str = "text/event-stream; charset=utf-8"
    response_request_id: str | None = None


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
        if reply.stream is not None:
            return httpx.Response(reply.status, stream=reply.stream, headers={
                "Content-Type": reply.response_type, "X-Request-ID": reply.response_request_id or request_id,
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
            })
        if reply.content is not None:
            return httpx.Response(reply.status, content=reply.content, headers=headers)
        if request.url.path == "/api/gateway/config/sources":
            data = GatewayConfigSourcesDTO(revision="remote-revision", schema_uri=None).model_dump(mode="json")
        elif request.url.path == "/api/gateway/config/events":
            data = GatewayConfigEventsDTO(cursor=17, events=[], has_more=False).model_dump(mode="json")
        elif not request.url.path.startswith("/api/gateway/config/sources/"):
            data = GatewayConfigReloadStatusDTO(candidate_ref="B原候选", state="pending").model_dump(mode="json")
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

    class RelayTransport(httpx.MockTransport):
        async def aclose(self) -> None:
            reply.client_close_entered.set()
            if reply.client_close_blocked:
                await reply.client_close_release.wait()
            await super().aclose()
            reply.close_events.append("client-close")
            if reply.client_close_error is not None:
                raise reply.client_close_error

    def outbound_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        outgoing = client_class(timeout=timeout, transport=RelayTransport(respond))
        reply.clients.append(outgoing)
        return outgoing

    monkeypatch.setattr(httpx, "AsyncClient", outbound_client)
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
        reply.client_close_release.set()
        if reply.stream is not None:
            reply.stream.close_release.set()
        await client.aclose()
        for outgoing in reply.clients:
            await outgoing.aclose()
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


async def test_native_only_operation_rejects_remote_target_without_side_effects(
    relay_case: RelayCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart = Mock()
    monkeypatch.setattr(health_config, "start_development_restart", restart)
    response = await relay_case.client.post("/api/gateway/runtime/restart-development",
                                            headers={TARGET_GATEWAY_ID_HEADER: REMOTE}, json={})
    assert response.status_code == 409, response.text
    restart.assert_not_called()
    assert not relay_case.reply.requests


@pytest.mark.parametrize(("method", "endpoint"), [
    ("GET", "reload-status"), ("POST", "retry-restart"), ("POST", "resolve-restart"),
    ("POST", "discard-restart"), ("GET", "events"),
])
async def test_management_relay_preserves_candidate_body_query_and_request_identity(
    relay_case: RelayCase, method: str, endpoint: str,
) -> None:
    case = relay_case
    if endpoint == "resolve-restart":
        payload = GatewayConfigPendingHealthProofRequest(
            candidate_ref="B原候选/+%", candidate_id="candidate", generation="generation", loaded_source="pending",
            pending_revision=8, candidate_digest="candidate-digest", effective_digest="effective",
            secret_binding_digest="binding", fencing_token_digest="fence", health_digest="health", gateway_id=REMOTE,
            consumer_health_digests={"consumer": "proof"},
        ).model_dump()
    elif endpoint == "discard-restart":
        payload = GatewayConfigPendingDiscardRequest(expected_active_revision=5, expected_active_digest="active").model_dump()
    else:
        payload = {}
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode() if method == "POST" else b""
    response = await case.client.request(method, "/api/gateway/config/" + endpoint, content=body, headers={
        TARGET_GATEWAY_ID_HEADER: REMOTE, "Content-Type": "application/json", "X-Request-ID": "management-request",
        "X-Local-Token": "never-forward", "Cookie": "never=forward", "X-BoxTeam-Gateway-Id": OTHER,
    }, params=[("candidate_ref", "B原候选/+%"), ("tag", "first"), ("tag", "second"), ("gateway_connection_id", CONNECTION)])
    assert response.status_code == 200, response.text
    assert response.content == case.reply.response_content
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "management-request"
    assert len(case.reply.requests) == 1
    forwarded = case.reply.requests[0]
    assert forwarded.method == method and forwarded.url.path == "/api/gateway/config/" + endpoint
    assert forwarded.content == body
    assert list(forwarded.url.params.multi_items()) == [("candidate_ref", "B原候选/+%"), ("tag", "first"), ("tag", "second")]
    assert forwarded.headers["X-BoxTeam-Federation-Token"] == case.credential.token
    assert forwarded.headers["X-BoxTeam-Gateway-Id"] == LOCAL and forwarded.headers[TARGET_GATEWAY_ID_HEADER] == REMOTE
    assert "X-Local-Token" not in forwarded.headers and "Cookie" not in forwarded.headers
    assert case.reply.clients[0].is_closed
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


@pytest.mark.parametrize("endpoint", ["reload-status", "events"])
@pytest.mark.parametrize("wrong", ["request-id", "data"])
async def test_management_success_requires_original_envelope_and_dto(
    relay_case: RelayCase, endpoint: str, wrong: str,
) -> None:
    case = relay_case
    case.reply.content = json.dumps({
        "request_id": "other-request" if wrong == "request-id" else "management-request",
        "data": {} if wrong == "request-id" else "malformed-native-dto",
    }).encode()
    response = await case.client.get("/api/gateway/config/" + endpoint, headers={
        TARGET_GATEWAY_ID_HEADER: REMOTE, "X-Request-ID": "management-request",
    })
    assert response.status_code == 502 and len(case.reply.requests) == 1
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


@pytest.mark.parametrize("status", [400, 403, 409, 410])
async def test_stream_cursor_or_permission_rejection_preserves_native_body(
    relay_case: RelayCase, status: int,
) -> None:
    case = relay_case
    case.reply.status = status
    case.reply.content = b'{ "code": "snapshot_required", "first_available": 17, "request_id": "stream-error" }\n'
    response = await case.client.get(STREAM_ENDPOINT, headers={
        TARGET_GATEWAY_ID_HEADER: REMOTE, "X-Request-ID": "stream-error", "Last-Event-ID": "bad-cursor",
    }, params={"after": 3, "gateway_connection_id": CONNECTION})
    assert response.status_code == status and response.content == case.reply.content
    assert response.headers["X-Request-ID"] == "stream-error"
    assert len(case.reply.requests) == 1 and case.reply.requests[0].headers["Last-Event-ID"] == "bad-cursor"
    assert list(case.reply.requests[0].url.params.multi_items()) == [("after", "3")]
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed and case.reply.clients[0].is_closed


async def _call_stream(case: RelayCase, send: Callable[[Message], Awaitable[None]]) -> None:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("流应由原上游或测试取消结束")

    await case.app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": STREAM_ENDPOINT, "raw_path": STREAM_ENDPOINT.encode(),
        "query_string": ("after=16&gateway_connection_id=" + CONNECTION).encode(),
        "headers": [(TARGET_GATEWAY_ID_HEADER.lower().encode(), REMOTE.encode()),
                    (b"x-request-id", b"stream-request"), (b"last-event-id", b"16")],
        "server": ("gateway-a", 80), "client": ("integration", 1),
    }, receive, send)


async def test_stream_forwards_original_frames_without_claim_ack_or_reconnection(relay_case: RelayCase) -> None:
    case = relay_case
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events)
    original = b'id: 17\nevent: config\ndata: {"source":"B"}\n\n: config-heartbeat\n\nevent: error\ndata: {"message":"revoked"}\n\n'
    for chunk in (original[:9], original[9:47], original[47:], None):
        stream.chunks.put_nowait(chunk)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await asyncio.wait_for(_call_stream(case, send), timeout=3)
    assert b"".join(message.get("body", b"") for message in messages) == original
    assert messages[0]["status"] == 200
    headers = httpx.Headers(messages[0]["headers"])
    assert headers["X-Request-ID"] == "stream-request" and headers["Cache-Control"] == "no-cache"
    assert headers["X-Accel-Buffering"] == "no" and "text/event-stream" in headers["Content-Type"]
    assert case.reply.close_events == ["body-close", "client-close"] and stream.close_calls == 1
    assert len(case.reply.requests) == 1
    forwarded = case.reply.requests[0]
    assert forwarded.headers["Last-Event-ID"] == "16"
    assert list(forwarded.url.params.multi_items()) == [("after", "16")]
    assert forwarded.extensions["timeout"] == {"connect": 45, "read": None, "write": 45, "pool": 45}
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


@pytest.mark.parametrize("cancelled", [False, True])
async def test_headers_send_failure_closes_unstarted_stream_and_original_connection(
    relay_case: RelayCase, cancelled: bool,
) -> None:
    case = relay_case
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events)
    primary = asyncio.CancelledError("响应头取消原实例") if cancelled else RuntimeError("响应头发送失败原实例")

    async def send(message: Message) -> None:
        assert message["type"] == "http.response.start" and not stream.read_entered.is_set()
        case.registry.upsert_remote_gateway(case.connection, runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://replacement.test"},
        ))
        assert not case.process.closed
        raise primary

    with pytest.raises(type(primary)) as caught:
        await asyncio.wait_for(_call_stream(case, send), timeout=3)
    assert caught.value is primary
    assert not stream.read_entered.is_set() and stream.close_calls == 1
    assert case.reply.close_events == ["body-close", "client-close"]
    assert case.reply.clients[0].is_closed and case.process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
    assert len(case.reply.requests) == 1 and case.reply.requests[0].url.host == "remote.test"
    case.registry.remove_remote_gateway(CONNECTION)


@pytest.mark.parametrize("blocked_close", ["body", "client"])
@pytest.mark.parametrize("status", [200, 410])
async def test_httpx_eof_auto_close_waits_for_cleanup_and_preserves_cancellation(
    relay_case: RelayCase, blocked_close: str, status: int,
) -> None:
    """自然 EOF 与错误 aread 都会先于 body finally 触发 HTTPX 自身关闭。"""
    case = relay_case
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events, close_blocked=blocked_close == "body")
    case.reply.client_close_blocked = blocked_close == "client"
    case.reply.status = status
    stream.chunks.put_nowait(b"snapshot_required" if status == 410 else b": heartbeat\n\n")
    stream.chunks.put_nowait(None)

    async def send(_: Message) -> None:
        return None

    task = asyncio.create_task(_call_stream(case, send))
    try:
        entered = stream.close_entered if blocked_close == "body" else case.reply.client_close_entered
        await asyncio.wait_for(entered.wait(), timeout=2)
        case.registry.upsert_remote_gateway(case.connection, runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://replacement.test"},
        ))
        assert not case.process.closed
        task.cancel("EOF自动关闭中的原始取消")
        await asyncio.sleep(0)
        task.cancel("EOF自动关闭中的重复取消")
        await asyncio.sleep(0)
        assert not task.done() and not case.process.closed
        with pytest.raises(RuntimeError, match="管理请求引用"):
            case.registry.remove_remote_gateway(CONNECTION)
        stream.close_release.set()
        case.reply.client_close_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=2)
        assert caught.value.args == ("EOF自动关闭中的原始取消",)
        assert stream.close_calls == 1 and case.reply.close_events == ["body-close", "client-close"]
        assert case.process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
        assert len(case.reply.requests) == 1
        case.registry.remove_remote_gateway(CONNECTION)
    finally:
        stream.close_release.set()
        case.reply.client_close_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("wrong", ["request-id", "content-type"])
async def test_stream_protocol_rejection_closes_without_reading_body(relay_case: RelayCase, wrong: str) -> None:
    case = relay_case
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events)
    case.reply.response_request_id = "wrong-request" if wrong == "request-id" else None
    case.reply.response_type = "application/json" if wrong == "content-type" else "text/event-stream"
    response = await case.client.get(STREAM_ENDPOINT, headers={TARGET_GATEWAY_ID_HEADER: REMOTE})
    assert response.status_code == 502 and not stream.read_entered.is_set()
    assert stream.close_calls == 1 and case.reply.close_events == ["body-close", "client-close"]
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


async def test_pending_transport_failure_does_not_replay_or_advertise_source_idempotency(relay_case: RelayCase) -> None:
    case = relay_case
    case.reply.error = True
    response = await case.client.post("/api/gateway/config/retry-restart", params={"candidate_ref": "B原候选"}, headers={
        TARGET_GATEWAY_ID_HEADER: REMOTE,
    })
    assert response.status_code == 502 and len(case.reply.requests) == 1
    assert "候选" in response.text and "幂等键" not in response.text
    assert case.reply.clients[0].is_closed
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed


async def test_management_header_deadline_keeps_httpx_error_and_original_consumer_status(
    relay_case: RelayCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = relay_case
    case.reply.blocked = True
    with pytest.raises(httpx.ReadTimeout) as caught:
        await request_remote_gateway_management(
            gateway_url="http://remote.test", credential=case.credential, method="GET",
            path="/api/gateway/federation/directories", request_id="header-deadline", timeout_seconds=0.02,
        )
    assert caught.value.request is case.reply.requests[0]
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert case.reply.clients[0].is_closed
    case.app.include_router(workspaces_lifecycle.router)
    case.app.dependency_overrides[verify_gateway_token] = lambda: "local-token"
    # 仅缩短原 helper 已有的预算参数，调用方与发送器仍走真实生产链。
    monkeypatch.setattr(workspaces_lifecycle, "request_remote_gateway_management",
                        partial(request_remote_gateway_management, timeout_seconds=0.02))
    response = await case.client.get("/api/gateway/local-directories", params={"gateway_connection_id": CONNECTION},
                                     headers={"X-Request-ID": "original-management-consumer"})
    assert response.status_code == 502 and "ReadTimeout" in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "original-management-consumer"
    assert len(case.reply.requests) == 2 and all(client.is_closed for client in case.reply.clients)


@pytest.mark.parametrize("blocked_close", ["body", "client"])
async def test_stream_cancellation_waits_for_complete_cleanup_before_releasing_tunnel(
    relay_case: RelayCase, blocked_close: str,
) -> None:
    case = relay_case
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events, close_blocked=blocked_close == "body")
    case.reply.client_close_blocked = blocked_close == "client"

    async def send(_: Message) -> None:
        return None

    task = asyncio.create_task(_call_stream(case, send))
    try:
        await asyncio.wait_for(stream.read_entered.wait(), timeout=2)
        case.registry.upsert_remote_gateway(case.connection, runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://replacement.test"},
        ))
        task.cancel("原始流取消")
        entered = stream.close_entered if blocked_close == "body" else case.reply.client_close_entered
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not case.process.closed
        with pytest.raises(RuntimeError, match="管理请求引用"):
            case.registry.remove_remote_gateway(CONNECTION)
        task.cancel("重复流取消")
        await asyncio.sleep(0)
        assert not task.done() and not case.process.closed
        stream.close_release.set()
        case.reply.client_close_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=2)
        assert caught.value is stream.cancellations[0]
        assert case.reply.close_events == ["body-close", "client-close"] and stream.close_calls == 1
        assert case.process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
        assert len(case.reply.requests) == 1 and case.registry.remote_gateway_url(CONNECTION) == "http://replacement.test"
        case.registry.remove_remote_gateway(CONNECTION)
    finally:
        stream.close_release.set()
        case.reply.client_close_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failed_close", ["body", "client"])
async def test_stream_close_failure_keeps_original_error_and_releases_reference(
    relay_case: RelayCase, failed_close: str,
) -> None:
    case = relay_case
    primary = OSError("原始关闭失败")
    stream = case.reply.stream = RelayStream(close_events=case.reply.close_events,
                                             close_error=primary if failed_close == "body" else None)
    case.reply.client_close_error = primary if failed_close == "client" else None
    stream.chunks.put_nowait(None)

    async def send(_: Message) -> None:
        return None

    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(_call_stream(case, send), timeout=3)
    assert caught.value is primary
    assert case.reply.close_events == ["body-close", "client-close"] and stream.close_calls == 1
    case.registry.remove_remote_gateway(CONNECTION)
    assert case.process.closed
