"""产品执行产生有效上下文，经无浏览器联邦查询、原读取及重启续页验收。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用真实Backend／统一模型回放。
)
from tests.integration.gateway.test_resource_search_tools import (
    agent_ports as agent_ports,  # noqa: PLC0414 - 沿当前测试文件隔离工作区、端口与产物。
)
from tests.support.api_helpers import create_session
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)


@pytest.mark.usefixtures("agent_ports")
@pytest.mark.parametrize("restart_source_case", [{"native_services": True}], indirect=True)
async def test_context_search_keeps_original_source_and_cursor_after_backend_restart(
    request: pytest.FixtureRequest, restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    backend = case.backends[-1]
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=case.workspace,
        default_backend_url=f"http://127.0.0.1:{backend.port}", port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=case.artifacts_dir)
    transport = GatewaySessionContextClient(gateway_url=f"http://127.0.0.1:{gateway.port}",
                                            local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
    original_client = case.client
    actual: dict[str, object] = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            gateway_id = (await ui.get("/api/gateway/health")).json()["data"]["gateway_id"]
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            route = listing["active_workspace_id"]
            owner = {"gateway_id": gateway_id, "workspace_id": next(item["backend_workspace_id"] for item in listing["items"] if item["workspace_id"] == route)}
            ui.headers["X-BoxTeam-Workspace-Id"] = route
            case.client = ui
            session_id = await create_session(ui, "Context Search Execution")
            await case.plan(session_id, [{"content": "assistant scope-proof🧪 done"}])
            accepted = await _send(case, session_id, "前缀🧪 scope-proof🧪 user input")
        case.client = original_client
        assert isinstance(accepted["job_id"], str)
        await _job(case, accepted["job_id"])
        assert not transport._http_client.cookies
        query = {"category": "context", "operation": "content", "query": "scope-proof🧪", "match_mode": "literal", "limit": 1,
                 "context": {"case_sensitive": True, "max_chars": 16384}}

        async def search(payload: dict, request_id: str) -> list[FederatedSearchEventDTO]:
            events = [event async for event in transport.resource_search.search(FederatedSearchRequest.model_validate(payload), request_id=request_id)]
            actual[request_id] = [event.model_dump(mode="json") for event in events]
            return events

        first = await search({"scope": {"mode": "origin", "origin": owner}, "queries": [query]}, "req_context_first")
        page, = [event.page for event in first if event.type == "page"]
        assert page is not None and page.context is not None and page.result_count == 1
        original = await original_client.post("/api/v1/context/search", json={
            "resource_identity": workspace_session_inventory_identity(owner["workspace_id"]),
            "uri": workspace_session_inventory_uri(owner["workspace_id"], gateway_authority=gateway_id),
            "scope": "workspace", "query": query["query"], "case_sensitive": True, "max_results": 1,
        }, headers={"X-BoxTeam-Gateway-Id": gateway_id})
        assert original.status_code == 200, original.text
        assert page.context == SessionContextSearchResultDTO.model_validate(original.json()["data"])
        hit, = page.context.matches
        assert hit.source == "effective_context" and hit.record_index is not None and hit.uri.endswith("/" + session_id)
        gateway_context = set_current_gateway_id(gateway_id)
        read_context = set_current_request_id("req_context_read")
        try:
            read = await transport.read_resource_context(SessionContextReadRequest(
                resource_identity=hit.resource_identity, uri=hit.uri, scope=hit.scope,
                view="records", record_index=hit.record_index, revision=hit.revision,
            ))
        finally:
            reset_current_request_id(read_context)
            reset_current_gateway_id(gateway_context)
        assert any(item.text and "scope-proof🧪" in item.text for item in read.items)
        actual["read"] = read.model_dump(mode="json")
        assert first[0].scope is not None
        assert first[-1].summary is not None
        branch, = first[-1].summary.branches
        assert branch.state == "limited" and branch.cursor is not None
        await case.restart(hard=False)
        resumed = await search({"scope": {"mode": "resume", "scope": first[0].scope.scope.model_dump(mode="json")}, "queries": [query],
            "continuations": [{"branch": branch.branch.model_dump(mode="json"), "cursor": branch.cursor}]}, "req_context_resume")
        following, = [event.page for event in resumed if event.type == "page"]
        assert following is not None and following.context is not None
        next_hit, = following.context.matches
        assert next_hit.resource_identity == hit.resource_identity and next_hit.record_index != hit.record_index
        assert following.context.revision == page.context.revision
        assert resumed[-1].summary is not None and resumed[-1].summary.state == "complete"
        for source in ("session_catalog", "session_information"):
            selected = {**query, "query": "Context Search Execution", "limit": 50, "context": {"sources": [source]}}
            response = await search({"scope": {"mode": "origin", "origin": owner}, "queries": [selected]}, f"req_context_{source}")
            result, = [event.page for event in response if event.type == "page"]
            assert result is not None and result.context is not None
            assert len(result.context.matches) == 1 and result.context.matches[0].source == source
        stale = await search({"scope": {"mode": "resume", "scope": first[0].scope.scope.model_dump(mode="json")},
            "queries": [{**query, "context": {"sources": ["session_catalog"]}}],
            "continuations": [{"branch": branch.branch.model_dump(mode="json"), "cursor": branch.cursor}]}, "req_context_changed_source")
        assert stale[-1].summary is not None
        rejected, = stale[-1].summary.branches
        assert rejected.state == "failed" and rejected.code == "owner_rejected" and rejected.cursor == branch.cursor
        assert len(case.requests()) == 1 and not transport._http_client.cookies

        # 经真实模型工具入口消费相同 Provider；不替产品准备执行或工具结果。
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            ui.headers["X-BoxTeam-Workspace-Id"] = route
            caller = await create_session(ui, "Context Search Tool Caller")
            calls = await case.plan(caller, [
                _tool_reply("invoke_extension_tool", {"tool_name": "search_resources", "arguments": {
                    "queries": [{**query, "query": "assistant scope-proof🧪 done", "limit": 20}],
                }}, "call_context_resources"),
                {"content": "CONTEXT_SEARCH_COMPLETE"},
            ])
            case.client = ui
            tool_execution = await _send(case, caller, "使用资源搜索检索先前会话中的助手回复。")
        case.client = original_client
        assert isinstance(tool_execution["job_id"], str)
        await _job(case, tool_execution["job_id"])
        tool_events = TypeAdapter(list[FederatedSearchEventDTO]).validate_python(
            case.tool_result(calls[0] + ":tool-call:call_context_resources"),
        )
        assert tool_events[0].scope is not None
        assert [item.model_dump(mode="json") for item in tool_events[0].scope.scope.owners] == [owner]
        tool_page, = [event.page for event in tool_events if event.type == "page"]
        assert tool_page is not None and tool_page.context is not None and tool_page.provider_id == "session-context"
        assert any(match.uri == hit.uri and match.source == "effective_context" for match in tool_page.context.matches)
        assert tool_events[-1].summary is not None and tool_events[-1].summary.state == "complete"
        final_wire = case.request(calls[1])
        messages = final_wire["messages"]
        assert isinstance(messages, list)
        tool_messages = [message for message in messages if message["role"] == "tool"]
        assert len(tool_messages) == 1 and tool_messages[0]["name"] == "invoke_extension_tool"
        assert hit.uri in tool_messages[0]["content"] and "effective_context" in tool_messages[0]["content"]
        assert len(case.requests()) == 3
        actual["tool_events"] = [event.model_dump(mode="json") for event in tool_events]
        actual["tool_final_wire"] = final_wire
        case.write_actual("context-search", actual)
    finally:
        case.client = original_client
        await transport.aclose()
        await asyncio.to_thread(close_gateway_process, gateway)
