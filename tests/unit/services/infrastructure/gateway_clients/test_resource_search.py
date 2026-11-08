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
from app.services.infrastructure.gateway_search.client import (
    IncompleteSearchStreamError,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)

REQUEST_ID = "req_resource_search"
OWNER = {"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": "00000000-0000-4000-8000-000000000001"}


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, wait: bool = False, failure: Exception | None = None) -> None:
        self.body = body
        self.wait = wait
        self.failure = failure
        self.closed = False
        self.blocked = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        # 故意在 Unicode 和 JSON／SSE 字段内部切块；不编写第二份解码器。
        for offset in range(0, len(self.body), 17):
            yield self.body[offset:offset + 17]
        if self.failure is not None:
            raise self.failure
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


def install_response(monkeypatch, client, events, *, wait=False, status=200, headers=None, failure=None):
    body = b": heartbeat\n\n" + b"".join(
        _encode_search_sse(FederatedSearchEventDTO.model_validate(event)) + b": heartbeat\n\n"
        for event in events
    )
    stream = Stream(body, wait=wait, failure=failure)
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


@pytest.mark.parametrize("event_count", [0, 1, 3])
async def test_normal_eof_preserves_machine_error_and_original_request_identity(client, monkeypatch, events, request_body, event_count):
    stream, requests = install_response(monkeypatch, client, events[:event_count])
    received = []
    with pytest.raises(IncompleteSearchStreamError) as caught:
        async for event in client.resource_search.search(request_body, request_id=REQUEST_ID):
            received.append(event)
    assert caught.value.code == "incomplete_stream"
    assert caught.value.request_id == REQUEST_ID
    assert str(caught.value) == "Gateway 搜索流在最终汇总前断开；已交付分支须按原范围独立续查"
    assert not hasattr(caught.value, "http_status")
    assert received == [FederatedSearchEventDTO.model_validate(event) for event in events[:event_count]]
    assert stream.closed and len(requests) == 1
    assert requests[0].headers["X-Request-ID"] == REQUEST_ID


@pytest.mark.parametrize("failure_type", [httpx.ReadError, httpx.ReadTimeout])
async def test_reader_failures_keep_their_original_type_and_message(client, monkeypatch, events, request_body, failure_type):
    failure = failure_type("原搜索连接读取失败")
    stream, requests = install_response(monkeypatch, client, events[:1], failure=failure)
    with pytest.raises(failure_type) as caught:
        _ = [event async for event in client.resource_search.search(request_body, request_id=REQUEST_ID)]
    assert caught.value is failure
    assert str(caught.value) == "原搜索连接读取失败"
    assert stream.closed and len(requests) == 1


@pytest.fixture
def diagnostic_request() -> FederatedSearchRequest:
    return FederatedSearchRequest.model_validate({
        "scope": {"mode": "origin", "origin": OWNER},
        "queries": [{"category": "runtime_log", "operation": "content", "match_mode": "literal", "query": "日志😀词",
                     "diagnostic_targets": [{"gateway_id": OWNER["gateway_id"], "scope": {"source": "workspace", "workspace_ids": [OWNER["workspace_id"]]}}]}],
    })


@pytest.fixture
def diagnostic_events(events: list[dict], diagnostic_request: FederatedSearchRequest) -> list[dict]:
    target = diagnostic_request.queries[0].diagnostic_targets[0]
    branch = {"branch": {"diagnostics": target.model_dump(mode="json"), "category": "runtime_log", "operation": "content"},
              "state": "limited", "code": "result_limit", "cursor": "original-log-cursor", "allocated_results": 1, "returned_results": 0}
    page = {"gateway_id": target.gateway_id, "scope": target.scope.model_dump(mode="json"), "query": diagnostic_request.queries[0].query,
            "items": [], "sources": [], "complete": False, "cursor": "original-log-cursor", "limited_reason": "result_limit",
            "scanned_sources": 0, "scanned_lines": 0, "scanned_bytes": 0}
    return [events[0], {"sequence": 2, "request_id": REQUEST_ID, "type": "page", "branch": branch, "diagnostic_logs": page},
            {"sequence": 3, "request_id": REQUEST_ID, "type": "branch", "branch": branch},
            {"sequence": 4, "request_id": REQUEST_ID, "type": "end", "summary": {"state": "incomplete", "branches": [branch], "returned_results": 0}}]


async def test_diagnostics_use_same_original_sse_decoder_and_cursor(client, monkeypatch, diagnostic_events, diagnostic_request):
    stream, requests = install_response(monkeypatch, client, diagnostic_events)
    actual = [event async for event in client.resource_search.search(diagnostic_request, request_id=REQUEST_ID)]
    assert stream.closed and len(requests) == 1
    assert actual[1].page is None and actual[1].diagnostic_logs.query == "日志😀词"
    assert actual[-1].summary.branches[0].cursor == "original-log-cursor"
    assert actual[-1].summary.branches[0].branch.owner is None


@pytest.mark.parametrize("damage", ["query", "unselected_target", "missing_branch", "scope"])
async def test_diagnostics_cannot_change_query_source_or_omit_terminal(client, monkeypatch, diagnostic_events, diagnostic_request, damage):
    if damage == "query":
        diagnostic_events[1]["diagnostic_logs"]["query"] = "其它查询"
    elif damage == "unselected_target":
        for event in diagnostic_events:
            if "branch" in event:
                event["branch"]["branch"]["diagnostics"]["scope"]["workspace_ids"] = ["00000000-0000-4000-8000-000000000002"]
        diagnostic_events[1]["diagnostic_logs"]["scope"]["workspace_ids"] = ["00000000-0000-4000-8000-000000000002"]
    elif damage == "missing_branch":
        diagnostic_events = [diagnostic_events[0], {"sequence": 2, "request_id": REQUEST_ID, "type": "end",
                            "summary": {"state": "complete", "branches": [], "returned_results": 0}}]
    else:
        diagnostic_request = diagnostic_request.model_copy(update={"scope": diagnostic_request.scope.model_copy(update={"mode": "all", "origin": None})})
        diagnostic_events[0]["scope"]["scope"]["owners"] = [{**OWNER, "workspace_id": "00000000-0000-4000-8000-000000000002"}]
    stream, _ = install_response(monkeypatch, client, diagnostic_events)
    with pytest.raises(RuntimeError):
        _ = [event async for event in client.resource_search.search(diagnostic_request, request_id=REQUEST_ID)]
    assert stream.closed


@pytest.mark.parametrize("damage", ["request_id", "sequence", "scope", "page_owner", "count", "end", "duplicate", "early_end"])
async def test_protocol_damage_is_explicit_and_closes_connection(client, monkeypatch, events, request_body, damage):
    if damage == "request_id":
        events[1]["request_id"] = "another-request"
    elif damage == "sequence":
        events[1]["sequence"] = 6
    elif damage == "scope":
        events[0]["scope"]["scope"]["owners"][0]["workspace_id"] = "00000000-0000-4000-8000-000000000002"
    elif damage == "page_owner":
        events[1]["page"]["workspace_id"] = "00000000-0000-4000-8000-000000000002"
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


@pytest.mark.parametrize("wrong_target,event_count", [(False, 4), (True, 4), (False, 0), (False, 1), (False, 3)])
async def test_context_json_relay_shares_event_validation_and_preserves_grant(client, monkeypatch, events, wrong_target, event_count):
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
    payload = payload[:event_count]
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
        assert request.headers["X-Request-ID"] == REQUEST_ID
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"request_id": REQUEST_ID, "data": {"events": payload}}, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
    gateway_token, request_token = set_current_gateway_id(OWNER["gateway_id"]), set_current_request_id(REQUEST_ID)
    try:
        if event_count < len(events):
            with pytest.raises(IncompleteSearchStreamError) as caught:
                await client.search_gateway_context(query)
            assert caught.value.code == "incomplete_stream"
            assert caught.value.request_id == REQUEST_ID
            assert str(caught.value) == "Gateway 搜索流在最终汇总前断开；已交付分支须按原范围独立续查"
            assert not hasattr(caught.value, "http_status")
        elif wrong_target:
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


@pytest.fixture
def ordered_context_search(events: list[dict]) -> tuple[FederatedSearchRequest, list[dict]]:
    owners = [OWNER, {**OWNER, "workspace_id": "00000000-0000-4000-8000-000000000002"}]
    scope = json.loads(json.dumps(events[0]))
    scope["scope"]["scope"].update(mode="selected", owners=owners)
    scope["scope"]["branches"] = [{"owner": owner, "state": "registered"} for owner in owners]
    request = FederatedSearchRequest.model_validate({
        "scope": {"mode": "selected", "owners": owners},
        "queries": [{"category": "context", "operation": "content", "query": "needle", "match_mode": "literal"}],
    })
    branches = [{"branch": {"owner": owner, "category": "context", "operation": "content"},
                 "state": "limited", "allocated_results": 1, "returned_results": 0,
                 "code": "result_limit", "cursor": f"owner-{index}-cursor"} for index, owner in enumerate(owners)]
    payload = [scope]
    # B 先完成不改变冻结范围或最终汇总的 A、B 顺序。
    for branch in reversed(branches):
        owner = branch["branch"]["owner"]
        page = {"provider_id": "session-context", "workspace_id": owner["workspace_id"], "category": "context",
                "operation": "content", "contract_version": 1, "state": "limited", "limited_reason": "result_limit",
                "context": {"resource_identity": str(workspace_session_inventory_identity(owner["workspace_id"])),
                            "uri": workspace_session_inventory_uri(owner["workspace_id"], gateway_authority=owner["gateway_id"]),
                            "scope": "workspace", "query": "needle", "match_mode": "literal", "revision": "original-owner-revision",
                            "has_more": True, "next_cursor": branch["cursor"]}}
        payload.extend([{"type": "page", "branch": branch, "page": page}, {"type": "branch", "branch": branch}])
    payload.append({"type": "end", "summary": {"state": "incomplete", "order": "category-scope-owner-v1",
                                               "branches": branches, "returned_results": 0}})
    for sequence, event in enumerate(payload, 1):
        event.update(sequence=sequence, request_id=REQUEST_ID)
    return request, payload


@pytest.mark.parametrize("mode,repeated", [("selected", False), ("selected", True), ("resume", False)])
@pytest.mark.parametrize("encoding", ["sse", "relay"])
@pytest.mark.parametrize("reordered", [False, True])
async def test_explicit_scope_order_is_preserved_independently_of_page_arrival(
    client, monkeypatch, ordered_context_search, mode, repeated, encoding, reordered,
):
    request, payload = ordered_context_search
    owners = list(request.scope.owners)
    if repeated:
        request = request.model_copy(update={"scope": request.scope.model_copy(update={"owners": (*request.scope.owners, owners[0])})})
    if mode == "resume":
        request = FederatedSearchRequest.model_validate({**request.model_dump(), "scope": {
            "mode": "resume", "scope": payload[0]["scope"]["scope"],
        }})
    if reordered:
        payload[0]["scope"]["scope"]["owners"].reverse()
    stream = None
    if encoding == "sse":
        stream, requests = install_response(monkeypatch, client, payload)

        async def consume_stream():
            return [event async for event in client.resource_search.search(request, request_id=REQUEST_ID)]

        consume = consume_stream
    else:
        requests = []

        async def send(http_request):
            requests.append(http_request)
            return httpx.Response(200, json={"request_id": REQUEST_ID, "data": {"events": payload}},
                                  headers={"X-Request-ID": REQUEST_ID})

        monkeypatch.setattr(client._http_client, "_transport", httpx.MockTransport(send))
        query = GatewayContextSearchRequest.model_validate({
            "source_gateway_id": OWNER["gateway_id"], "resource_search": request,
            "resource_target": {"gateway_id": OWNER["gateway_id"], "scope": "gateway",
                                "resource_identity": str(gateway_session_inventory_identity(OWNER["gateway_id"])),
                                "uri": gateway_session_inventory_uri(OWNER["gateway_id"], gateway_authority=OWNER["gateway_id"])},
        })

        async def consume_relay():
            result = await client.search_gateway_context(query)
            return result.events

        consume = consume_relay

    gateway_token, request_token = set_current_gateway_id(OWNER["gateway_id"]), set_current_request_id(REQUEST_ID)
    try:
        if reordered:
            with pytest.raises(RuntimeError, match="实际范围与明确选择不一致"):
                await consume()
        else:
            actual = await consume()
            assert list(actual[0].scope.scope.owners) == owners
            assert [event.branch.branch.owner for event in actual if event.type == "page"] == list(reversed(owners))
            assert [branch.branch.owner for branch in actual[-1].summary.branches] == owners
            assert [branch.cursor for branch in actual[-1].summary.branches] == ["owner-0-cursor", "owner-1-cursor"]
            assert actual[-1].summary.order == "category-scope-owner-v1"
            assert all(event.request_id == REQUEST_ID for event in actual)
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)
    assert len(requests) == 1
    assert requests[0].url.path == ("/api/gateway/search/agent" if encoding == "sse" else "/api/gateway/federation/relay")
    sent = json.loads(requests[0].content)
    sent_scope = sent["scope"] if encoding == "sse" else sent["body"]["resource_search"]["scope"]
    assert sent_scope == request.scope.model_dump(mode="json")
    if stream is not None:
        assert stream.closed
