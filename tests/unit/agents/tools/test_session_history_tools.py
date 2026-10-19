"""规范资源 grammar、通信 read owner 与现有 inventory/search 的工具合同。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.abstractions.session_target import SessionTarget
from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.session_history import (
    create_read_context_tool,
    create_search_context_tool,
)
from app.core.session_control_communication_ledger.observations import (
    RemoteObservationError,
)
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.internal_v2.session_context import (
    SessionContextReadResultDTO,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.support.communication import SOURCE_SESSION, TARGET_SESSION, WORKSPACE_ID

REMOTE_WORKSPACE = "33333333-3333-4333-8333-333333333333"


def _session_input(workspace_id: str = WORKSPACE_ID) -> dict[str, str]:
    return {"resource_identity": str(session_resource_identity(workspace_id, TARGET_SESSION)),
            "uri": session_resource_uri(workspace_id, TARGET_SESSION), "scope": "workspace"}


class QueryProbe:
    workspace_id = WORKSPACE_ID

    def __init__(self):
        self.calls = []

    async def read_context(self, request):
        self.calls.append(("read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="local-read")

    async def search_context(self, request):
        self.calls.append(("search", request))
        return SessionContextSearchResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "query", "match_mode"}), revision="local-search")


class WorkspaceProbe:
    def __init__(self):
        self.calls = []

    async def read_gateway_context(self, request):
        self.calls.append(("gateway-read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="gateway-read")

    async def read_resource_context(self, request):
        self.calls.append(("resource-read", request))
        return SessionContextReadResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "view"}), revision="remote-read")

    async def search_resource_context(self, request):
        self.calls.append(("resource-search", request))
        return SessionContextSearchResultDTO(**request.model_dump(include={"resource_identity", "uri", "scope", "query", "match_mode"}), revision="remote-search")


class ResolverProbe:
    def __init__(self):
        self.calls = []

    async def resolve_session(self, session_id, *, backend_workspace_id):
        self.calls.append((session_id, backend_workspace_id))
        return SessionTarget(session_id=session_id, workspace_id="gw_target")


@pytest.fixture
def history_context(communication_probe, communication_invocation):
    return SimpleNamespace(
        session_id=SOURCE_SESSION, session_context_query_service=QueryProbe(),
        workspace_session_context_client=WorkspaceProbe(), session_target_resolver=ResolverProbe(),
        session_communication_service=communication_probe, invocation_context=communication_invocation,
    )


async def test_session_read_uses_durable_communication_owner(history_context, communication_probe):
    arguments = _session_input()
    communication_probe.read_result = SessionContextReadResultDTO(**arguments, view="overview", revision="frozen")
    result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "frozen"
    operation, call = communication_probe.calls[0]
    assert operation == "read" and call["session_id"] == SOURCE_SESSION
    assert call["thread_id"] == history_context.invocation_context.thread_binding.thread_id
    assert call["source_call_id"] == history_context.invocation_context.require_operation_id("read")
    assert history_context.session_context_query_service.calls == []
    assert history_context.session_target_resolver.calls == []


async def test_search_preserves_resource_identity_without_gateway_route_mapping(history_context):
    arguments = {**_session_input(REMOTE_WORKSPACE), "query": "ALPHA"}
    result = json.loads(await create_search_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "remote-search" and result["uri"] == arguments["uri"]
    assert history_context.session_target_resolver.calls == []
    route, request = history_context.workspace_session_context_client.calls[0]
    assert route == "resource-search" and request.uri == arguments["uri"]
    assert request.resource_identity == arguments["resource_identity"]


async def test_local_search_keeps_current_workspace_query(history_context):
    history_context.session_target_resolver = None
    result = json.loads(await create_search_context_tool(history_context).ainvoke({**_session_input(), "query": "ALPHA"}))
    assert result["revision"] == "local-search"
    assert history_context.session_context_query_service.calls[0][0] == "search"


async def test_gateway_inventory_requires_current_gateway_identity(history_context):
    token = set_current_gateway_id("gw-history")
    try:
        arguments = {"resource_identity": str(gateway_session_inventory_identity("gw-history")),
                     "uri": gateway_session_inventory_uri("gw-history"), "scope": "gateway", "view": "inventory"}
        result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
        assert result["revision"] == "gateway-read"
        assert history_context.workspace_session_context_client.calls[0][0] == "gateway-read"
    finally:
        reset_current_gateway_id(token)


async def test_workspace_inventory_keeps_cold_query(history_context):
    arguments = {"resource_identity": str(workspace_session_inventory_identity(WORKSPACE_ID)),
                 "uri": workspace_session_inventory_uri(WORKSPACE_ID), "scope": "workspace", "view": "inventory"}
    result = json.loads(await create_read_context_tool(history_context).ainvoke(arguments))
    assert result["revision"] == "local-read"
    assert history_context.session_context_query_service.calls[0][0] == "read"


async def test_read_rejects_non_boxteam_resource_as_tool_error(history_context):
    tool = create_read_context_tool(history_context)
    result = await tool.ainvoke({"type": "tool_call", "id": "call_bad_resource", "name": tool.name,
                                "args": {**_session_input(), "uri": "file:///context.jsonl"}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "boxteam://" in result.text


async def test_custom_invoker_propagates_revision_conflict(history_context, communication_probe):
    communication_probe.error = SessionContextRevisionChangedError(expected_revision="old", actual_revision="new")
    target = create_read_context_tool(history_context)
    invoker = create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
    result = await invoker.ainvoke({"type": "tool_call", "id": "call_changed", "name": invoker.name,
                                  "args": {"tool_name": target.name, "arguments": {**_session_input(), "revision": "old"}}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "expected=old" in result.text and "actual=new" in result.text


async def test_custom_invoker_exposes_observation_snapshot_error(history_context, communication_probe):
    communication_probe.error = RemoteObservationError("read-snapshot-mismatch")
    target = create_read_context_tool(history_context)
    invoker = create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
    result = await invoker.ainvoke({"type": "tool_call", "id": "call_stale_snapshot", "name": invoker.name,
                                  "args": {"tool_name": target.name, "arguments": _session_input()}})
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.text == "read-snapshot-mismatch"


async def test_custom_invoker_rejects_unknown_target_argument(history_context):
    target = create_read_context_tool(history_context)
    invoker = create_extension_tool_invoker_tool([target], catalog_binding_resolver=seal_extension_catalog_binding_from_tools([target]))
    result = await invoker.ainvoke({"tool_name": target.name, "arguments": {**_session_input(), "query": "未知 read 参数"}})
    assert result == "扩展工具 read_context 包含未知参数: query"


async def test_session_read_without_owner_fails_loudly(history_context):
    history_context.session_communication_service = None
    with pytest.raises(RuntimeError, match="持久通信 owner"):
        await create_read_context_tool(history_context).ainvoke(_session_input())


def test_read_schema_contains_only_business_parameters(history_context):
    schema = create_read_context_tool(history_context).args_schema.model_json_schema()["properties"]
    assert {"resource_identity", "uri", "scope", "cursor", "revision"} <= schema.keys()
    assert not {"resource", "workspace_id", "source_call_id", "thread_id", "observation_id"} & schema.keys()


@pytest.mark.parametrize("authority", [None, "gateway_remote"])
@pytest.mark.parametrize("operation", ["read", "search"])
async def test_remote_workspace_inventory_uses_resource_relay(history_context, authority, operation):
    arguments = {
        "resource_identity": str(workspace_session_inventory_identity(REMOTE_WORKSPACE)),
        "uri": workspace_session_inventory_uri(REMOTE_WORKSPACE, gateway_authority=authority),
        "scope": "workspace",
    }
    if operation == "search":
        arguments["query"] = "ALPHA"
        tool = create_search_context_tool(history_context)
    else:
        arguments["view"] = "inventory"
        tool = create_read_context_tool(history_context)
    result = json.loads(await tool.ainvoke(arguments))
    assert result["uri"] == arguments["uri"]
    name, request = history_context.workspace_session_context_client.calls[0]
    assert name == f"resource-{operation}"
    assert request.uri == arguments["uri"]
    assert request.resource_identity == arguments["resource_identity"]
    assert history_context.session_context_query_service.calls == []
    assert history_context.session_target_resolver.calls == []
