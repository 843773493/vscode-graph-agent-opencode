"""真实 LangChain 工具入参、发起归属与单链传输；网络在端口处替换。"""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.tools.resource_search import (
    create_discover_workspaces_tool,
    create_search_resources_tool,
)
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.gateway.search import GatewayWorkspaceDiscoveryResultsDTO
from app.schemas.search_federation import FederatedSearchEventDTO

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"


class SearchProbe:
    def __init__(self) -> None:
        self.calls = []

    async def search(self, request, *, request_id) -> AsyncIterator[FederatedSearchEventDTO]:
        self.calls.append(("search", request, request_id))
        yield FederatedSearchEventDTO.model_validate({
            "sequence": 1, "request_id": request_id, "type": "end",
            "summary": {"state": "complete", "branches": [], "returned_results": 0},
        })

    async def discover_workspaces(self, **arguments):
        self.calls.append(("discover", arguments))
        return GatewayWorkspaceDiscoveryResultsDTO.model_validate({
            "gateway_id": GATEWAY, "query": arguments["query"], "resolved_at": "2026-11-03T00:00:00Z", "complete": True,
        })


@pytest.fixture
def search_context():
    probe = SearchProbe()
    context = SimpleNamespace(
        workspace_session_context_client=SimpleNamespace(resource_search=probe),
        session_context_query_service=SimpleNamespace(workspace_id="stable-workspace-a"),
        invocation_context=SimpleNamespace(require_operation_id=lambda operation: f"operation-{operation}"),
    )
    token = set_current_gateway_id(GATEWAY)
    try:
        yield context, probe
    finally:
        reset_current_gateway_id(token)


async def test_default_tool_scope_uses_execution_owner_not_active_navigation(search_context):
    context, probe = search_context
    tool = create_search_resources_tool(context)
    result = json.loads(await tool.ainvoke({"queries": [{"category": "catalog", "query": "x"}]}))
    assert result[0]["type"] == "end" and result[0]["request_id"] == "operation-search_resources"
    _, request, request_id = probe.calls[0]
    assert request.scope.mode == "origin"
    assert request.scope.origin.model_dump() == {"gateway_id": GATEWAY, "workspace_id": "stable-workspace-a"}
    assert request_id == "operation-search_resources"
    assert "request_id" not in export_model_tool_json_schema(tool)["properties"]


@pytest.mark.parametrize("scope", [{"mode": "all"}, {"mode": "selected", "owners": [
    {"gateway_id": GATEWAY, "workspace_id": "B"}, {"gateway_id": GATEWAY, "workspace_id": "A"},
]}])
async def test_explicit_scope_is_not_prefiltered_by_discovery(search_context, scope):
    context, probe = search_context
    await create_search_resources_tool(context).ainvoke({"scope": scope, "queries": [{"category": "catalog", "query": "x"}]})
    assert len(probe.calls) == 1
    assert probe.calls[0][1].scope.model_dump(mode="json", exclude_defaults=True) == scope


@pytest.mark.parametrize("scope", [{"mode": "selected", "owners": []}, {"mode": "all", "origin": {"gateway_id": GATEWAY, "workspace_id": "A"}}])
async def test_invalid_scope_fails_before_transport(search_context, scope):
    context, probe = search_context
    with pytest.raises(ValidationError):
        await create_search_resources_tool(context).ainvoke({"scope": scope, "queries": [{"category": "catalog", "query": "x"}]})
    assert probe.calls == []


async def test_discovery_is_separate_read_and_does_not_implicitly_search(search_context):
    context, probe = search_context
    result = json.loads(await create_discover_workspaces_tool(context).ainvoke({"query": "project", "include_folders": True}))
    assert result["query"] == "project"
    assert probe.calls == [("discover", {"query": "project", "limit": 50, "cursor": None, "include_folders": True,
                                         "request_id": "operation-discover_workspaces"})]
