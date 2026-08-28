from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.abstractions.session_target import SessionTarget
from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.tools.session_history import (
    create_read_context_tool,
    create_search_context_tool,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadResultDTO,
    SessionContextSearchResultDTO,
)


class _FakeLocalQueryService:
    def __init__(self) -> None:
        self.read_resources: list[str] = []

    async def read_context(self, request):
        self.read_resources.append(request.resource)
        return SessionContextReadResultDTO(
            resource=request.resource,
            view=request.view,
            revision="ckpt-local",
        )


class _FakeWorkspaceClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.gateway_read_calls: list[str] = []

    async def read_context_in_workspace(self, workspace_id, request):
        self.read_calls.append((workspace_id, request.resource))
        return SessionContextReadResultDTO(
            resource=request.resource,
            view=request.view,
            revision="ckpt-remote",
        )

    async def search_context_in_workspace(self, workspace_id, request):
        self.search_calls.append((workspace_id, request.resource))
        return SessionContextSearchResultDTO(
            resource=request.resource,
            query=request.query,
            match_mode=request.match_mode,
            revision="ckpt-remote",
        )

    async def read_gateway_context(self, request):
        self.gateway_read_calls.append(request.resource)
        return SessionContextReadResultDTO(
            resource=request.resource,
            view="inventory",
            revision="gateway-revision",
        )


class _RevisionChangedLocalService:
    async def read_context(self, _request):
        raise SessionContextRevisionChangedError(
            expected_revision="old",
            actual_revision="new",
        )


class _FakeTargetResolver:
    def __init__(self, target: SessionTarget) -> None:
        self.target = target
        self.calls: list[tuple[str, str | None]] = []

    async def resolve_session(self, session_id, *, workspace_id=None):
        self.calls.append((session_id, workspace_id))
        return self.target


@pytest.mark.asyncio
async def test_read_context_current_session_uses_local_query_service():
    local_service = _FakeLocalQueryService()
    context = SimpleNamespace(
        session_context_query_service=local_service,
        workspace_session_context_client=_FakeWorkspaceClient(),
    )
    tool = create_read_context_tool(context)

    result = json.loads(
        await tool.ainvoke({"resource": "boxteam://session/ses_local"})
    )

    assert result["revision"] == "ckpt-local"
    assert local_service.read_resources == ["boxteam://session/ses_local"]


@pytest.mark.asyncio
async def test_search_context_workspace_resource_uses_gateway_client():
    workspace_client = _FakeWorkspaceClient()
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=workspace_client,
    )
    tool = create_search_context_tool(context)
    resource = "boxteam://workspace/gw_target/session/ses_remote"

    result = json.loads(
        await tool.ainvoke({"resource": resource, "query": "ALPHA"})
    )

    assert result["revision"] == "ckpt-remote"
    assert workspace_client.search_calls == [("gw_target", resource)]


@pytest.mark.asyncio
async def test_read_context_session_resource_uses_shared_target_resolver():
    workspace_client = _FakeWorkspaceClient()
    resolver = _FakeTargetResolver(
        SessionTarget(session_id="ses_remote", workspace_id="gw_target")
    )
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=workspace_client,
        session_target_resolver=resolver,
    )
    tool = create_read_context_tool(context)

    result = json.loads(
        await tool.ainvoke({"resource": "boxteam://session/ses_remote"})
    )

    assert result["resource"] == "boxteam://workspace/gw_target/session/ses_remote"
    assert resolver.calls == [("ses_remote", None)]
    assert workspace_client.read_calls == [
        ("gw_target", "boxteam://workspace/gw_target/session/ses_remote")
    ]


@pytest.mark.asyncio
async def test_search_context_session_resource_passes_explicit_workspace_to_resolver():
    workspace_client = _FakeWorkspaceClient()
    resolver = _FakeTargetResolver(
        SessionTarget(session_id="ses_remote", workspace_id="gw_target")
    )
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=workspace_client,
        session_target_resolver=resolver,
    )
    tool = create_search_context_tool(context)

    await tool.ainvoke(
        {
            "resource": "boxteam://session/ses_remote",
            "workspace_id": "gw_target",
            "query": "ALPHA",
        }
    )

    assert resolver.calls == [("ses_remote", "gw_target")]
    assert workspace_client.search_calls == [
        ("gw_target", "boxteam://workspace/gw_target/session/ses_remote")
    ]


@pytest.mark.asyncio
async def test_read_context_gateway_inventory_uses_gateway_client():
    workspace_client = _FakeWorkspaceClient()
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=workspace_client,
    )
    tool = create_read_context_tool(context)

    result = json.loads(
        await tool.ainvoke(
            {"resource": "boxteam://gateway/workspaces", "view": "inventory"}
        )
    )

    assert result["revision"] == "gateway-revision"
    assert workspace_client.gateway_read_calls == ["boxteam://gateway/workspaces"]


@pytest.mark.asyncio
async def test_read_context_rejects_non_boxteam_resource():
    context = SimpleNamespace(
        session_context_query_service=_FakeLocalQueryService(),
        workspace_session_context_client=_FakeWorkspaceClient(),
    )
    tool = create_read_context_tool(context)

    result = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "call_bad_resource",
            "name": tool.name,
            "args": {"resource": "file:///tmp/context.jsonl"},
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "boxteam://" in result.text


@pytest.mark.asyncio
async def test_custom_invoker_returns_revision_change_as_tool_error():
    context = SimpleNamespace(
        session_context_query_service=_RevisionChangedLocalService(),
        workspace_session_context_client=_FakeWorkspaceClient(),
    )
    target_tool = create_read_context_tool(context)
    invoker = create_custom_tool_invoker_tool([target_tool])

    result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_changed",
            "name": invoker.name,
            "args": {
                "tool_name": target_tool.name,
                "arguments": {
                    "resource": "boxteam://session/ses_target",
                    "expected_revision": "old",
                },
            },
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "expected=old" in result.text
    assert "actual=new" in result.text
