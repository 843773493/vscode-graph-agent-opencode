"""产品执行产生有效上下文，经无浏览器联邦查询、原读取及重启续页验收。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from app.abstractions.session_context import SessionContextSearchPatternError
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
    GatewayContextSearchRequest,
    GatewayContextSearchResultDTO,
)
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_uri,
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
    assert case.mount is not None
    backend = case.backends[-1]
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=case.workspace,
        default_backend_url=f"http://127.0.0.1:{backend.port}", port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=case.artifacts_dir)
    transport = GatewaySessionContextClient(gateway_url=f"http://127.0.0.1:{gateway.port}",
                                            local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
    original_client = case.client
    actual: dict[str, object] = {}
    query = {"category": "context", "operation": "content", "query": "scope-proof🧪", "match_mode": "literal", "limit": 1,
             "context": {"case_sensitive": True, "max_chars": 16384}}

    async def search(payload: dict, request_id: str) -> list[FederatedSearchEventDTO]:
        events = [event async for event in transport.resource_search.search(FederatedSearchRequest.model_validate(payload), request_id=request_id)]
        actual[request_id] = [event.model_dump(mode="json") for event in events]
        return events

    async def gateway_context_search(payload: dict, request_id: str) -> GatewayContextSearchResultDTO:
        gateway_token = set_current_gateway_id(gateway_id)
        request_token = set_current_request_id(request_id)
        try:
            result = await transport.search_gateway_context(GatewayContextSearchRequest.model_validate({
                "source_gateway_id": gateway_id,
                "resource_target": {"gateway_id": gateway_id,
                    "resource_identity": str(gateway_session_inventory_identity(gateway_id)),
                    "uri": gateway_session_inventory_uri(gateway_id), "scope": "gateway"},
                "resource_search": payload,
            }))
        finally:
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)
        actual[request_id] = result.model_dump(mode="json")
        assert {event.request_id for event in result.events} == {request_id}
        return result

    expected_error = {
        "owner": "session_context_query", "code": "invalid_regex", "reason": "invalid_search_pattern",
        "message": "query 不是有效正则表达式: unterminated character set at position 0",
    }

    def require_pattern_failure(events: list[FederatedSearchEventDTO], request_id: str) -> None:
        assert {event.request_id for event in events} == {request_id}
        assert not any(event.type == "page" for event in events)
        terminal, = [event.branch for event in events if event.type == "branch"]
        assert terminal is not None and terminal.state == "failed" and terminal.code == expected_error["code"]
        assert terminal.error_owner == expected_error["owner"] and terminal.reason == expected_error["reason"]
        assert terminal.message == expected_error["message"] and terminal.http_status == 400 and not terminal.retryable
        assert terminal.cursor is None and terminal.returned_results == 0
        assert events[-1].summary is not None and events[-1].summary.state == "incomplete"
        assert events[-1].summary.branches == [terminal] and events[-1].summary.returned_results == 0

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            gateway_id = (await ui.get("/api/gateway/health")).json()["data"]["gateway_id"]
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            owner = listing["active_workspace"]
            assert owner == {"gateway_id": gateway_id, "workspace_id": case.mount.workspace_id}
            ui.headers.update({
                "X-BoxTeam-Workspace-Id": owner["workspace_id"],
                "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
            })
            empty_request = {
                "resource_identity": str(workspace_session_inventory_identity(owner["workspace_id"])),
                "uri": workspace_session_inventory_uri(owner["workspace_id"]),
                "scope": "workspace", "query": "[", "match_mode": "regex",
            }
            empty = await ui.post("/api/v1/context/search", json={**empty_request, "query": r"never.*"})
            assert empty.status_code == 200, empty.text
            assert empty.json()["data"]["total_matches"] == 0 and empty.json()["data"]["matches"] == []
            actual["empty_inventory"] = empty.json()
            invalid_query = {**query, "query": "[", "match_mode": "regex"}
            for endpoint, payload in (("/api/v1/context/search", empty_request),
                                      ("/api/v1/search", {"workspace_id": owner["workspace_id"], **invalid_query})):
                for client_name, client in (("backend", original_client), ("gateway", ui)):
                    request_id = f"req_invalid_regex_{client_name}_{'context' if endpoint.endswith('context/search') else 'provider'}"
                    rejected = await client.post(endpoint, json=payload,
                        headers={"X-BoxTeam-Gateway-Id": gateway_id, "X-Request-ID": request_id})
                    actual[request_id] = {"status": rejected.status_code, "headers": dict(rejected.headers), "body": rejected.json()}
                    assert rejected.status_code == 400, rejected.text
                    assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == request_id
                    assert rejected.json()["detail"] == expected_error
            failed = await search({"scope": {"mode": "origin", "origin": owner}, "queries": [invalid_query]}, "req_invalid_regex_sse")
            require_pattern_failure(failed, "req_invalid_regex_sse")
            failed_json = await gateway_context_search({"scope": {"mode": "all"}, "queries": [invalid_query]}, "req_invalid_regex_json")
            require_pattern_failure(failed_json.events, "req_invalid_regex_json")

            exact_request = {**empty_request, "uri": workspace_session_inventory_uri(owner["workspace_id"], gateway_authority=gateway_id)}
            exact_target = {"gateway_id": gateway_id, **{key: exact_request[key] for key in ("resource_identity", "uri", "scope")}}
            relay_id = "req_invalid_regex_exact_relay"
            relay = await ui.post("/api/gateway/federation/relay", headers={"X-Request-ID": relay_id}, json={
                "operation": "read", "resource_target": exact_target,
                "body": {"source_gateway_id": gateway_id, "resource_target": exact_target, "search": exact_request},
            })
            actual[relay_id] = {"status": relay.status_code, "headers": dict(relay.headers), "body": relay.json()}
            assert relay.status_code == 409, relay.text
            assert relay.headers["X-Request-ID"] == relay.json()["request_id"] == relay_id
            assert relay.json()["detail"]["code"] == "federation-target-owner-rejected"
            assert relay.json()["detail"]["detail"] == expected_error
            gateway_token = set_current_gateway_id(gateway_id)
            request_token = set_current_request_id("req_invalid_regex_exact_client")
            try:
                with pytest.raises(SessionContextSearchPatternError) as caught:
                    await transport.search_resource_context(SessionContextSearchRequest.model_validate(exact_request))
                assert caught.value.detail() == expected_error
                assert "request_id=req_invalid_regex_exact_client" in caught.value.__notes__[0]
                actual["exact_client_error"] = {"detail": caught.value.detail(), "notes": caught.value.__notes__}
            finally:
                reset_current_request_id(request_token)
                reset_current_gateway_id(gateway_token)
            case.client = ui
            session_id = await create_session(ui, "Context Search Execution")
            await case.plan(session_id, [{"content": "assistant scope-proof🧪 done"}])
            accepted = await _send(case, session_id, "前缀🧪 scope-proof🧪 user input")
        case.client = original_client
        assert isinstance(accepted["job_id"], str)
        await _job(case, accepted["job_id"])
        assert not transport._http_client.cookies

        regex_query = {**query, "query": r"scope-proof🧪 (user input|done)$", "match_mode": "regex", "limit": 20}
        regex_sse = await search({"scope": {"mode": "origin", "origin": owner}, "queries": [regex_query]}, "req_context_valid_regex_sse")
        regex_page, = [event.page for event in regex_sse if event.type == "page"]
        assert regex_page is not None and regex_page.context is not None and regex_page.context.total_matches == 2
        regex_json = await gateway_context_search({"scope": {"mode": "all"}, "queries": [regex_query]}, "req_context_valid_regex_json")
        regex_json_page, = [event.page for event in regex_json.events if event.type == "page"]
        assert regex_json_page is not None and regex_json_page.context == regex_page.context
        assert regex_sse[-1].summary is not None and regex_sse[-1].summary.state == "complete"
        assert regex_json.events[-1].summary is not None and regex_json.events[-1].summary.state == "complete"
        gateway_token = set_current_gateway_id(gateway_id)
        request_token = set_current_request_id("req_context_valid_regex_exact")
        try:
            regex_exact = await transport.search_resource_context(SessionContextSearchRequest.model_validate({
                **exact_request, "query": regex_query["query"], "case_sensitive": True, "max_results": 20,
            }))
            actual["valid_regex_exact"] = regex_exact.model_dump(mode="json")
            # 同 Gateway 的精确 relay 沿原简称，集合和命中都保持原 Workspace／Session 归属。
            expected_exact = regex_page.context.model_copy(update={
                "uri": workspace_session_inventory_uri(owner["workspace_id"]),
                "matches": [match.model_copy(update={"uri": session_resource_uri(owner["workspace_id"], session_id)})
                            for match in regex_page.context.matches],
            })
            assert regex_exact.returned_chars == len(regex_exact.model_dump_json())
            assert regex_exact.model_dump(exclude={"returned_chars"}) == expected_exact.model_dump(exclude={"returned_chars"})
        finally:
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)

        gateway_first = await gateway_context_search({"scope": {"mode": "all"}, "queries": [query]}, "req_gateway_context_first")
        gateway_page, = [event.page for event in gateway_first.events if event.type == "page"]
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
        assert gateway_page is not None and gateway_page.context == page.context
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
        assert gateway_first.events[-1].summary is not None and gateway_first.events[0].scope is not None
        gateway_branch, = gateway_first.events[-1].summary.branches
        gateway_resumed = await gateway_context_search({
            "scope": {"mode": "resume", "scope": gateway_first.events[0].scope.scope.model_dump(mode="json")},
            "queries": [query], "continuations": [{"branch": gateway_branch.branch.model_dump(mode="json"), "cursor": gateway_branch.cursor}],
        }, "req_gateway_context_resume")
        gateway_following, = [event.page for event in gateway_resumed.events if event.type == "page"]
        assert gateway_following is not None and gateway_following.context == following.context
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
            ui.headers.update({
                "X-BoxTeam-Workspace-Id": owner["workspace_id"],
                "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
            })
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
        # 同一执行入口验证 search_context 的 Gateway inventory，不让测试手工写工具结果。
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            ui.headers.update({
                "X-BoxTeam-Workspace-Id": owner["workspace_id"],
                "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
            })
            gateway_caller = await create_session(ui, "Gateway Context Tool Caller")
            gateway_calls = await case.plan(gateway_caller, [
                _tool_reply("invoke_extension_tool", {"tool_name": "search_context", "arguments": {
                    "resource_identity": str(gateway_session_inventory_identity(gateway_id)),
                    "uri": gateway_session_inventory_uri(gateway_id), "scope": "gateway",
                    "query": "assistant scope-proof🧪 done",
                }}, "call_gateway_context"),
                {"content": "GATEWAY_CONTEXT_COMPLETE"},
            ])
            case.client = ui
            gateway_execution = await _send(case, gateway_caller, "从 Gateway inventory 搜索先前执行的有效上下文。")
        case.client = original_client
        assert isinstance(gateway_execution["job_id"], str)
        await _job(case, gateway_execution["job_id"])
        gateway_tool = GatewayContextSearchResultDTO.model_validate(case.tool_result(gateway_calls[0] + ":tool-call:call_gateway_context"))
        assert gateway_tool.events[0].scope is not None and gateway_tool.events[-1].summary is not None
        assert [item.model_dump(mode="json") for item in gateway_tool.events[0].scope.scope.owners] == [owner]
        assert gateway_tool.events[-1].summary.state == "complete"
        assert any(match.uri == hit.uri and match.source == "effective_context" for event in gateway_tool.events
                   if event.page is not None and event.page.context is not None for match in event.page.context.matches)
        gateway_wire = case.request(gateway_calls[1])
        gateway_messages = gateway_wire["messages"]
        assert isinstance(gateway_messages, list)
        assert any(isinstance(message, dict) and message["role"] == "tool" and hit.uri in message["content"] for message in gateway_messages)
        assert len(case.requests()) == 5
        actual["gateway_tool"] = gateway_tool.model_dump(mode="json")
        actual["gateway_final_wire"] = gateway_wire

        # 精确资源经真实工具入口走 relay，原拒绝不能升级为 Agent 内部失败。
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            ui.headers.update({"X-BoxTeam-Workspace-Id": owner["workspace_id"], "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"]})
            invalid_caller = await create_session(ui, "Invalid Context Regex Caller")
            invalid_calls = await case.plan(invalid_caller, [
                _tool_reply("invoke_extension_tool", {"tool_name": "search_context", "arguments": exact_request}, "call_invalid_context_regex"),
                {"content": "CONTEXT_REGEX_REJECTED"},
            ])
            case.client = ui
            ui.headers["X-Request-ID"] = "req_invalid_regex_tool"
            invalid_execution = await _send(case, invalid_caller, "报告上下文查询参数错误。")
        case.client = original_client
        assert isinstance(invalid_execution["job_id"], str)
        await _job(case, invalid_execution["job_id"])
        invalid_result = case.tool_result(invalid_calls[0] + ":tool-call:call_invalid_context_regex")
        assert {key: invalid_result[key] for key in expected_error} == expected_error
        assert invalid_result["request_id"] == "req_invalid_regex_tool"
        invalid_wire = case.request(invalid_calls[1])
        invalid_messages = [message for message in invalid_wire["messages"] if message["role"] == "tool"]
        assert len(invalid_messages) == 1 and invalid_messages[0]["name"] == "invoke_extension_tool"
        assert TypeAdapter(dict[str, str]).validate_json(invalid_messages[0]["content"]) == invalid_result
        assert len(case.requests()) == 7
        actual["invalid_tool"] = invalid_result
        actual["invalid_tool_final_wire"] = invalid_wire
    finally:
        case.write_actual("context-search", actual)
        case.client = original_client
        await transport.aclose()
        await asyncio.to_thread(close_gateway_process, gateway)
