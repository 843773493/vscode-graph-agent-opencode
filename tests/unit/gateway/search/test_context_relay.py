"""授权上下文 relay 与普通搜索共用范围、页预算及原 owner 续点。"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.federation.workspace_port import WorkspaceSessionMainPort
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.search.service import GatewaySearchService
from app.schemas.internal_v2.communication import (
    CommunicationGatewayRequest,
    ContextQueryRequest,
)
from app.schemas.search_federation import (
    GatewayContextSearchRequest,
    GatewayContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)

GATEWAY = "gateway_" + "a" * 32
OWNERS = [{"gateway_id": GATEWAY, "workspace_id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(1, 4)]


@dataclass
class ContextRelay:
    registry: GatewayWorkspaceRegistry
    port: WorkspaceSessionMainPort = field(init=False)
    requests: list[httpx.Request] = field(default_factory=list)
    wrong_owner: bool = False

    def query(self, **updates: object) -> GatewayContextSearchRequest:
        return GatewayContextSearchRequest.model_validate({
            "source_gateway_id": GATEWAY,
            "resource_target": {"gateway_id": GATEWAY,
                "resource_identity": str(gateway_session_inventory_identity(GATEWAY)),
                "uri": gateway_session_inventory_uri(GATEWAY), "scope": "gateway"},
            "resource_search": {"scope": {"mode": "selected", "owners": OWNERS[:2]},
                "queries": [{"category": "context", "operation": "content", "query": "needle", "match_mode": "literal",
                             "limit": 2, "context": {"sources": ["session_information"], "max_chars": 2048}}],
                **updates},
        })

    async def receive(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = json.loads(request.content)
        owner = query["workspace_id"]
        context_owner = OWNERS[2]["workspace_id"] if self.wrong_owner and owner == OWNERS[0]["workspace_id"] else owner
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
            "request_id": request.headers["X-Request-ID"], "data": {
                "provider_id": "session-context", "workspace_id": owner, "category": "context", "operation": "content",
                "contract_version": 1, "state": "limited", "limited_reason": "result_limit", "context": {
                    "resource_identity": str(workspace_session_inventory_identity(context_owner)),
                    "uri": workspace_session_inventory_uri(context_owner, gateway_authority=GATEWAY), "scope": "workspace",
                    "query": query["query"], "match_mode": query["match_mode"], "revision": owner + "-revision",
                    "has_more": True, "next_cursor": owner + "-cursor", "truncated": True,
                },
            },
        })


@pytest.fixture
async def relay(tmp_path: Path) -> AsyncIterator[ContextRelay]:
    gateway_token = set_current_gateway_id(GATEWAY)
    request_token = set_current_request_id("req_context_relay")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    for index, owner in enumerate(OWNERS):
        registry.upsert(WorkspaceTarget(workspace_id=f"route-{index}", name="same", root_path=str(tmp_path / str(index)),
            backend_workspace_id=owner["workspace_id"], backend_url=f"http://owner-{index}.test", connection_kind="local"), activate=False)
    fixture = ContextRelay(registry=registry)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.receive)) as client:
        service = GatewaySearchService(registry, WorkspaceNavigationStore(storage_path=tmp_path / "navigation.json"), client, gateway_id=GATEWAY)
        fixture.port = WorkspaceSessionMainPort(http_client=client, search_service=service)
        try:
            yield fixture
        finally:
            registry.close()
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)


async def test_context_relay_delivers_owner_events_and_only_resumes_explicit_branch(relay: ContextRelay) -> None:
    query = relay.query()
    envelope = CommunicationGatewayRequest(operation="read", resource_target=query.resource_target, body=query.model_dump(mode="json"))
    reply = await relay.port.execute(operation=envelope.operation, target=query.resource_target.to_payload(),
                                     body=envelope.body, origin=GATEWAY, timeout=5)
    events = GatewayContextSearchResultDTO.model_validate(reply).events
    assert events[0].type == "scope" and events[-1].type == "end"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.request_id for event in events} == {"req_context_relay"}
    assert {request.url.host for request in relay.requests} == {"owner-0.test", "owner-1.test"}
    assert events[-1].summary is not None and events[0].scope is not None
    branches = events[-1].summary.branches
    assert [branch.cursor for branch in branches] == [owner["workspace_id"] + "-cursor" for owner in OWNERS[:2]]
    for request in relay.requests:
        body = json.loads(request.content)
        assert request.url.path == "/api/v1/search" and request.headers["X-Request-ID"] == "req_context_relay"
        assert body["context"]["sources"] == ["session_information"] and body["context"]["max_chars"] == 2048
    resumed = relay.query(scope={"mode": "resume", "scope": events[0].scope.scope},
                           continuations=[{"branch": branches[1].branch, "cursor": branches[1].cursor}])
    relay.requests.clear()
    result = await relay.port.search_gateway_context(resumed)
    assert len(relay.requests) == 1 and relay.requests[0].url.host == "owner-1.test"
    assert json.loads(relay.requests[0].content)["cursor"] == branches[1].cursor
    assert result.events[0].scope is not None and result.events[0].scope.scope == events[0].scope.scope
    assert all(relay.registry.route_reference_counts(f"route-{index}") == (0, 0) for index in range(3))


@pytest.mark.parametrize("change", [{"category": "file"}, {"operation": "name"}])
def test_context_grant_cannot_be_used_for_other_searches(relay: ContextRelay, change: dict) -> None:
    payload = relay.query().model_dump(mode="json")
    payload["resource_search"]["queries"][0].update(change)
    with pytest.raises(ValidationError):
        CommunicationGatewayRequest.model_validate({"operation": "read", "resource_target": payload["resource_target"], "body": payload})


def test_legacy_global_context_search_contract_is_rejected(relay: ContextRelay) -> None:
    target = relay.query().resource_target
    with pytest.raises(ValidationError, match="独立分支续查"):
        ContextQueryRequest.model_validate({"source_gateway_id": GATEWAY, "resource_target": target,
            "search": {"resource_identity": target.resource_identity, "uri": target.uri, "scope": target.scope, "query": "needle"}})


async def test_context_page_from_another_owner_is_not_published(relay: ContextRelay) -> None:
    relay.wrong_owner = True
    result = await relay.port.search_gateway_context(relay.query())
    assert result.events[-1].summary is not None
    wrong, healthy = result.events[-1].summary.branches
    assert wrong.state == "failed" and wrong.code == "invalid_response" and wrong.cursor is None
    assert healthy.state == "limited" and healthy.cursor == OWNERS[1]["workspace_id"] + "-cursor"
    assert [event.branch.branch.owner.workspace_id for event in result.events if event.type == "page" and event.branch] == [OWNERS[1]["workspace_id"]]
