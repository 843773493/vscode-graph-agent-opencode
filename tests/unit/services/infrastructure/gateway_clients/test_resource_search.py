"""原联邦编码经真实 httpx／第三方 SSE 解码，保留范围和结算事实。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime

import httpx
import pytest

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.gateway.routes.search import _encode_search_sse
from app.schemas.gateway.search import GatewaySearchScopeResolutionDTO
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
    GatewayContextSearchRequest,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)

REQUEST_ID = "req_resource_search"
OWNER = {"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": "workspace-a"}


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, wait: bool = False) -> None:
        self.body = body
        self.wait = wait
        self.closed = False
        self.blocked = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        # 故意在 Unicode 和 JSON／SSE 字段内部切块；不编写第二份解码器。
        for offset in range(0, len(self.body), 17):
            yield self.body[offset:offset + 17]
        if self.wait:
            self.blocked.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def request_body() -> FederatedSearchRequest:
    return FederatedSearchRequest.model_validate({
        "scope": {"mode": "origin", "origin": OWNER},
        "queries": [{"category": "catalog", "query": "测试🧪", "limit": 1}],
    })


@pytest.fixture
def events() -> list[dict]:
    scope = GatewaySearchScopeResolutionDTO.model_validate({
        "scope": {"gateway_id": OWNER["gateway_id"], "resolved_at": datetime.now(UTC), "mode": "origin", "owners": [OWNER]},
        "branches": [{"owner": OWNER, "state": "registered", "target_id": "route-a"}],
    })
    branch = {"branch": {"owner": OWNER, "category": "catalog", "operation": "name"}, "state": "limited",
              "code": "result_limit", "cursor": "opaque-owner-cursor", "allocated_results": 1, "returned_results": 0}
    page = {"provider_id": "session-catalog", "workspace_id": OWNER["workspace_id"], "category": "catalog",
            "operation": "name", "contract_version": 1, "state": "limited", "limited_reason": "result_limit",
            "catalog": {"revision": "owner-revision", "catalog_revision": 7, "cursor": "opaque-owner-cursor", "items": []}}
    return [
        {"sequence": 1, "request_id": REQUEST_ID, "type": "scope", "scope": scope.model_dump(mode="json")},
        {"sequence": 2, "request_id": REQUEST_ID, "type": "page", "branch": branch, "page": page},
        {"sequence": 3, "request_id": REQUEST_ID, "type": "branch", "branch": branch},
        {"sequence": 4, "request_id": REQUEST_ID, "type": "end", "summary": {"state": "incomplete", "branches": [branch], "returned_results": 0}},
    ]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GatewaySessionContextClient]:
    # 连接生命周期仍由现有 transport 独占；HTTP 边界在每个用例显式注入。
    transport = GatewaySessionContextClient(gateway_url="http://gateway.test", local_token_supplier=lambda: "private-token")
    yield transport
    await transport.aclose()


def install_response(monkeypatch, client, events, *, wait=False, status=200, headers=None):
    body = b": heartbeat\n\n" + b"".join(
        _encode_search_sse(FederatedSearchEventDTO.model_validate(event)) + b": heartbeat\n\n"
        for event in events
    )
    stream = Stream(body, wait=wait)
    requests = []

    async def send(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, stream=stream, headers=headers or {
            "X-Request-ID": REQUEST_ID, "Content-Type": "text/event-stream",
        })

    monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
    return stream, requests


async def test_stream_preserves_domain_cursor_and_uses_only_one_gateway_request(client, monkeypatch, events, request_body):
    stream, requests = install_response(monkeypatch, client, events)
    actual = [event async for event in client.resource_search.search(request_body, request_id=REQUEST_ID)]
    assert actual == [FederatedSearchEventDTO.model_validate(event) for event in events]
    assert stream.closed and len(requests) == 1
    request, = requests
    assert request.url.path == "/api/gateway/search/agent"
    assert request.headers["X-Request-ID"] == REQUEST_ID and request.headers["X-Local-Token"] == "private-token"
    body = json.loads(request.content)
    assert body["scope"] == request_body.scope.model_dump(mode="json")
    assert time.time() < body["deadline_at"] <= time.time() + 15
    assert request_body.deadline_at is None


@pytest.mark.parametrize("damage", ["request_id", "sequence", "scope", "page_owner", "count", "end", "duplicate", "early_end"])
async def test_protocol_damage_is_explicit_and_closes_connection(client, monkeypatch, events, request_body, damage):
    if damage == "request_id":
        events[1]["request_id"] = "another-request"
    elif damage == "sequence":
        events[1]["sequence"] = 6
    elif damage == "scope":
        events[0]["scope"]["scope"]["owners"][0]["workspace_id"] = "workspace-b"
    elif damage == "page_owner":
        events[1]["page"]["workspace_id"] = "workspace-b"
    elif damage == "count":
        events[1]["branch"]["returned_results"] = 1
    elif damage == "end":
        events[-1]["summary"]["branches"] = []
    elif damage == "duplicate":
        events.append(events[-1])
    elif damage == "early_end":
        events.pop()
    stream, _ = install_response(monkeypatch, client, events)
    with pytest.raises(RuntimeError):
        _ = [event async for event in client.resource_search.search(request_body, request_id=REQUEST_ID)]
    assert stream.closed


@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_and_cancellation_close_the_same_stream(client, monkeypatch, events, request_body, cancel):
    stream, requests = install_response(monkeypatch, client, events[:1], wait=True)
    request_body = request_body.model_copy(update={"deadline_at": time.time() + 0.1})

    async def consume():
        return [event async for event in client.resource_search.search(request_body, request_id=REQUEST_ID)]

    task = asyncio.create_task(consume())
    await stream.blocked.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert stream.closed and len(requests) == 1


async def test_caller_closing_generator_closes_response_without_waiting_for_next_frame(client, monkeypatch, events, request_body):
    stream, _ = install_response(monkeypatch, client, events[:1], wait=True)
    async with aclosing(client.resource_search.search(request_body, request_id=REQUEST_ID)) as received:
        assert (await anext(received)).type == "scope"
    assert stream.closed


async def test_expired_request_is_not_sent(client, monkeypatch, events, request_body):
    _, requests = install_response(monkeypatch, client, events)
    with pytest.raises(TimeoutError, match="不发起请求"):
        _ = [event async for event in client.resource_search.search(
            request_body.model_copy(update={"deadline_at": time.time() - 1}), request_id=REQUEST_ID,
        )]
    assert requests == []


async def test_discovery_reads_only_control_plane_and_preserves_cursor(client, monkeypatch):
    requests = []
    data = {"gateway_id": OWNER["gateway_id"], "query": "project", "resolved_at": datetime.now(UTC).isoformat(),
            "items": [], "folders": [], "cursor": "registry-cursor", "complete": False}

    async def send(request):
        requests.append(request)
        return httpx.Response(200, json={"request_id": REQUEST_ID, "data": data}, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
    result = await client.resource_search.discover_workspaces(query="project", cursor="prior", include_folders=True, request_id=REQUEST_ID)
    request, = requests
    assert request.url.path == "/api/gateway/search/workspaces"
    assert dict(request.url.params) == {"query": "project", "limit": "50", "include_folders": "true", "cursor": "prior"}
    assert result.cursor == "registry-cursor" and not result.complete


async def test_gateway_rejection_preserves_registered_reason(client, monkeypatch, request_body):
    async def send(request):
        return httpx.Response(400, json={"request_id": REQUEST_ID, "detail": {"code": "owner_mismatch", "message": "目标不属于冻结范围"}},
                              headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
    with pytest.raises(WorkspaceSessionContextAccessError, match="status=400.*code=owner_mismatch; 目标不属于冻结范围"):
        _ = [event async for event in client.resource_search.search(request_body, request_id=REQUEST_ID)]


@pytest.mark.parametrize("wrong_target", [False, True])
async def test_context_json_relay_shares_event_validation_and_preserves_grant(client, monkeypatch, events, wrong_target):
    payload = json.loads(json.dumps(events))
    for event in payload:
        branches = [event["branch"]] if "branch" in event else event.get("summary", {}).get("branches", [])
        for branch in branches:
            branch["branch"]["category"] = "context"
            branch["branch"]["operation"] = "content"
        if "page" in event:
            page = event["page"]
            del page["catalog"]
            page.update(category="context", operation="content", provider_id="session-context", context={
                "resource_identity": str(workspace_session_inventory_identity(OWNER["workspace_id"])),
                "uri": workspace_session_inventory_uri(OWNER["workspace_id"], gateway_authority=OWNER["gateway_id"]),
                "scope": "workspace", "query": "needle", "match_mode": "literal", "revision": "original-owner-revision",
                "has_more": True, "next_cursor": "opaque-owner-cursor",
            })
    target_gateway = "gateway_" + "b" * 32 if wrong_target else OWNER["gateway_id"]
    query = GatewayContextSearchRequest.model_validate({
        "source_gateway_id": OWNER["gateway_id"],
        "resource_target": {"gateway_id": target_gateway, "resource_identity": str(gateway_session_inventory_identity(target_gateway)),
            "uri": gateway_session_inventory_uri(target_gateway, gateway_authority=target_gateway), "scope": "gateway"},
        "resource_search": {"scope": {"mode": "all"}, "queries": [{
            "category": "context", "operation": "content", "query": "needle", "match_mode": "literal",
        }]},
    })
    received = []

    async def send(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"request_id": REQUEST_ID, "data": {"events": payload}}, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
    gateway_token, request_token = set_current_gateway_id(OWNER["gateway_id"]), set_current_request_id(REQUEST_ID)
    try:
        if wrong_target:
            with pytest.raises(RuntimeError, match="回执归属错误"):
                await client.search_gateway_context(query)
        else:
            result = await client.search_gateway_context(query)
            assert [event.model_dump(mode="json") for event in result.events] == [FederatedSearchEventDTO.model_validate(event).model_dump(mode="json") for event in payload]
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)
    assert len(received) == 1 and received[0]["operation"] == "read"
    assert received[0]["resource_target"] == query.resource_target.to_payload()
    assert received[0]["body"] == query.model_dump(mode="json")
