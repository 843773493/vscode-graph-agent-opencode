"""无浏览器的真实双工作区脚本搜索，以及产品模型入口调用扩展工具。"""

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from app.schemas.gateway.search import GatewayWorkspaceDiscoveryResultsDTO
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 显式复用原进程与模型回放 fixture。
)
from tests.integration.gateway.test_federated_search import Network
from tests.integration.gateway.test_federated_search import (
    network as network,  # noqa: PLC0414 - 复用原双后端隔离 fixture。
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)


async def test_python_search_without_browser_has_exact_scope_and_restart_continuation(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    transport = GatewaySessionContextClient(
        gateway_url=f"http://127.0.0.1:{network.gateway_port}", local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"],
    )
    observed = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=25) as ui:
            await acquire_gateway_guest(ui)
            gateway_id = (await ui.get("/api/gateway/health")).json()["data"]["gateway_id"]
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            route_a = next(item["workspace_id"] for item in listing["items"] if item["workspace_id"] not in {"route_b", "route_c"})
            owners = {item["workspace_id"]: {"gateway_id": gateway_id, "workspace_id": item["backend_workspace_id"]} for item in listing["items"]}
            sessions = {}
            for route in (route_a, "route_b"):
                ui.headers["X-BoxTeam-Workspace-Id"] = route
                sessions[route] = [await create_session(ui, f"Headless Search {index}") for index in range(3)]
        assert not transport._http_client.cookies
        query = {"category": "catalog", "query": "Headless Search", "limit": 1}

        async def search(payload: dict, request_id: str) -> list[FederatedSearchEventDTO]:
            request = FederatedSearchRequest.model_validate(payload)
            events = [event async for event in transport.resource_search.search(request, request_id=request_id)]
            assert all(event.request_id == request_id for event in events)
            observed.append({"request": payload, "events": [event.model_dump(mode="json") for event in events]})
            return events

        discovery = await transport.resource_search.discover_workspaces(query="Unrelated", request_id="req_headless_discover")
        assert [item.owner.model_dump() for item in discovery.items] == [owners["route_b"]]
        first = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [query]}, "req_headless_a")
        first_page, = [event for event in first if event.type == "page"]
        assert first_page.page is not None and first_page.page.catalog is not None
        assert first_page.page.catalog.items[0].node.session_id == sessions[route_a][0]

        both = await search({"scope": {"mode": "selected", "owners": [owners[route_a], owners["route_b"]]}, "queries": [query]}, "req_headless_ab")
        assert both[0].scope is not None
        frozen = both[0].scope.scope.model_dump(mode="json")
        page_b, = [event for event in both if event.page is not None and event.page.workspace_id == owners["route_b"]["workspace_id"]]
        assert page_b.branch is not None and page_b.branch.cursor is not None
        full = await search({"scope": {"mode": "all"}, "queries": [query]}, "req_headless_all")
        assert full[-1].summary is not None and full[-1].summary.state == "incomplete"
        stopped, = [item for item in full[-1].summary.branches if item.branch.owner.workspace_id == owners["route_c"]["workspace_id"]]
        assert stopped.state == "failed" and stopped.code == "target_unavailable" and stopped.retryable
        assert not (network.stopped / ".boxteam/sessions").exists()

        # 机器入口不创建游客；普通浏览器和机器入口仍使用各自的认证边界。
        denied = await transport._http_client.post(
            f"http://127.0.0.1:{network.gateway_port}/api/gateway/search",
            json={"scope": {"mode": "all"}, "queries": [query]}, headers=LOCAL_TOKEN_HEADERS,
        )
        assert denied.status_code == 401 and denied.json()["detail"] == "user_session_required"
        unauthorized = await transport._http_client.post(
            f"http://127.0.0.1:{network.gateway_port}/api/gateway/search/agent",
            json={"scope": {"mode": "all"}, "queries": [query]}, headers={"X-Local-Token": "wrong"},
        )
        assert unauthorized.status_code == 401
        assert not transport._http_client.cookies
        await asyncio.to_thread(close_gateway_process, gateway)
        gateway = None
        gateway = await asyncio.to_thread(network.start)
        resumed = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                "continuations": [{"branch": page_b.branch.branch.model_dump(mode="json"), "cursor": page_b.branch.cursor}]}, "req_headless_resume_b")
        resumed_page, = [event for event in resumed if event.type == "page"]
        assert resumed_page.page is not None and resumed_page.page.catalog is not None
        assert resumed_page.page.catalog.items[0].node.session_id == sessions["route_b"][1]
        assert resumed[-1].summary is not None and len(resumed[-1].summary.branches) == 1
        assert resumed[0].scope is not None and resumed[0].scope.scope.model_dump(mode="json") == frozen
    finally:
        await transport.aclose()
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
        (network.artifacts / "headless-search.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n")
    logs = [(network.artifacts / f"backend-{index}.stderr.log").read_text() for index in (0, 1)]
    actual = {request: [index for index, log in enumerate(logs) if f'request_id="{request}"' in log]
              for request in ("req_headless_discover", "req_headless_a", "req_headless_ab", "req_headless_all", "req_headless_resume_b")}
    assert actual == {"req_headless_discover": [], "req_headless_a": [0], "req_headless_ab": [0, 1],
                      "req_headless_all": [0, 1], "req_headless_resume_b": [1]}
    (network.artifacts / "actual-search-targets.json").write_text(json.dumps(actual, indent=2) + "\n")


@pytest.fixture
def agent_ports(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "service-ports.json",
                            tuple(ports.port(offset) for offset in (0, 20, 40, 42, 43)))
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


@pytest.mark.usefixtures("agent_ports")
@pytest.mark.parametrize("restart_source_case", [{"native_services": True}], indirect=True)
async def test_model_execution_invokes_discovery_and_resource_search_through_gateway(
    request: pytest.FixtureRequest, restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    backend = case.backends[-1]
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=case.workspace,
        default_backend_url=f"http://127.0.0.1:{backend.port}", port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=case.artifacts_dir)
    original_client = case.client
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as ui:
            await acquire_gateway_guest(ui)
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            route = listing["active_workspace_id"]
            owner = next(item["backend_workspace_id"] for item in listing["items"] if item["workspace_id"] == route)
            ui.headers["X-BoxTeam-Workspace-Id"] = route
            case.client = ui
            target = await create_session(ui, "Agent Search Target🧪")
            caller = await create_session(ui, "Agent Search Caller")
            calls = await case.plan(caller, [
                _tool_reply("invoke_extension_tool", {"tool_name": "discover_workspaces", "arguments": {}}, "call_discover"),
                _tool_reply("invoke_extension_tool", {"tool_name": "search_resources", "arguments": {
                    "queries": [{"category": "catalog", "query": "Agent Search Target🧪"}],
                }}, "call_resources"),
                {"content": "SEARCH_COMPLETE"},
            ])
            accepted = await _send(case, caller, "通过已登记工具查找工作区和目标会话；不要发送其它消息。")
        # 只保留既有后台执行；工具自己的 Gateway HTTP 连接没有 UI Cookie。
        case.client = original_client
        job_id = accepted["job_id"]
        assert isinstance(job_id, str)
        await _job(case, job_id)
        discovered = GatewayWorkspaceDiscoveryResultsDTO.model_validate(case.tool_result(calls[0] + ":tool-call:call_discover"))
        assert any(item.owner.workspace_id == owner for item in discovered.items)
        events = TypeAdapter(list[FederatedSearchEventDTO]).validate_python(case.tool_result(calls[1] + ":tool-call:call_resources"))
        assert events[0].scope is not None and events[0].scope.scope.mode == "origin"
        assert [item.workspace_id for item in events[0].scope.scope.owners] == [owner]
        page, = [event.page for event in events if event.type == "page"]
        assert page is not None and page.catalog is not None
        assert [item.node.session_id for item in page.catalog.items] == [target]
        assert events[-1].summary is not None and events[-1].summary.state == "complete"
        assert len(case.requests()) == 3
        wire = case.request(calls[2])
        messages = wire["messages"]
        assert isinstance(messages, list)
        results = [message for message in messages if message["role"] == "tool"]
        assert len(results) == 2 and all(message["name"] == "invoke_extension_tool" for message in results)
        case.write_actual("agent-resource-search", {"discovery": discovered.model_dump(mode="json"), "events": [event.model_dump(mode="json") for event in events],
                                                    "target": target, "caller": caller, "final_wire": wire})
    finally:
        case.client = original_client
        await asyncio.to_thread(close_gateway_process, gateway)
    actual_requests = re.findall(r'request_id="([^"]+)".*"(?:GET /api/gateway/search/workspaces(?:\?[^ ]*)?|POST /api/gateway/search/agent) HTTP',
                                 (case.artifacts_dir / "gateway.stderr.log").read_text())
    assert len(actual_requests) == len(set(actual_requests)) == 2
    assert actual_requests[1] == events[0].request_id
    case.write_actual("agent-search-request-ids", {"request_ids": actual_requests})
