"""无浏览器的真实双工作区脚本搜索，以及产品模型入口调用扩展工具。"""

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
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
from tests.integration.gateway.test_federated_search import Network
from tests.integration.gateway.test_federated_search import (
    network as network,  # noqa: PLC0414 - 复用原双后端隔离 fixture。
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
)
from tests.support.processes.workspace import workspace_backend_id_for_root
from tests.support.session_navigation.http_operations import submit_navigation_operation
from tests.support.terminal_plugin import enable_developer_tools_plugins


async def test_python_search_without_browser_has_exact_scope_and_restart_continuation(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    transport = GatewaySessionContextClient(
        gateway_url=f"http://127.0.0.1:{network.gateway_port}", local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"],
    )
    observed = []
    trees = []
    rejected_request_ids = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=25) as ui:
            gateway_id = (await ui.get("/api/gateway/health")).json()["data"]["gateway_id"]
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            workspace_a, workspace_b, workspace_c = (
                workspace_backend_id_for_root(root) for root in (network.workspace, network.secondary, network.stopped)
            )
            assert gateway_id == network.gateway_id
            ui.headers["X-BoxTeam-Target-Gateway-Id"] = gateway_id
            owners = {item["workspace_id"]: {"gateway_id": item["gateway_id"], "workspace_id": item["workspace_id"]} for item in listing["items"]}
            assert set(owners) == {workspace_a, workspace_b, workspace_c}
            assert all(owner["gateway_id"] == gateway_id for owner in owners.values())
            sessions = {}
            folders = {}
            for route in (workspace_a, workspace_b):
                ui.headers["X-BoxTeam-Workspace-Id"] = route
                sessions[route] = [await create_session(ui, f"Headless Search {index}") for index in range(3)]
                root = await submit_navigation_operation(ui, "create_folder", name="Headless Folder")
                tail = await submit_navigation_operation(ui, "create_folder", name="Headless Tail")
                nested = await submit_navigation_operation(ui, "create_folder", name="Headless Nested",
                                                           parent_node_id=root["created_node_id"])
                assert root["state"] == tail["state"] == nested["state"] == "committed"
                folders[route] = (root["created_node_id"], tail["created_node_id"], nested["created_node_id"])
                for session_id in sessions[route]:
                    moved = await submit_navigation_operation(ui, "move_node", target_node_id=session_id,
                                                              parent_node_id=nested["created_node_id"])
                    assert moved["state"] == "committed"

            async def read_tree(route: str, path: str, params: dict, *, status: int = 200) -> dict:
                request_id = f"req_headless_tree_{len(trees)}"
                response = await ui.get(f"/api/v1/session-catalog/{path}", params=params,
                    headers={"X-BoxTeam-Workspace-Id": route, "X-Request-ID": request_id})
                body = response.json()
                trees.append({"owner": owners[route], "path": path, "params": params,
                              "status": response.status_code, "response": body})
                assert response.status_code == status, response.text
                assert body["request_id"] == response.headers["X-Request-ID"] == request_id
                return body["data"] if status == 200 else body

            # 两个同名目录树由产品生成；交错分页，不以显示名合并或重用另一 owner 的续点。
            roots = {route: await read_tree(route, "roots", {"limit": 1}) for route in sessions}
            children = {route: await read_tree(route, "children", {"parent_node_id": folders[route][2], "limit": 1})
                        for route in sessions}
            for route, expected_sessions in sessions.items():
                root_id, tail_id, nested_id = folders[route]
                first = roots[route]
                assert first["total"] == 2 and first["parent_node_id"] is None and first["cursor"]
                next_root = await read_tree(route, "roots", {"limit": 1, "cursor": first["cursor"]})
                assert {node["node_id"] for page in (first, next_root) for node in page["items"]} == {root_id, tail_id}
                assert next_root["cursor"] is None
                branch = await read_tree(route, "children", {"parent_node_id": root_id, "limit": 1})
                assert [(node["node_id"], node["name"], node["parent_node_id"]) for node in branch["items"]] == [
                    (nested_id, "Headless Nested", root_id)]
                page = children[route]
                found = []
                while True:
                    assert page["total"] == 3 and page["parent_node_id"] == nested_id
                    assert len(page["items"]) == 1
                    node, = page["items"]
                    assert node["kind"] == "session" and node["parent_node_id"] == nested_id
                    found.append(node["session_id"])
                    breadcrumb = await read_tree(route, f"breadcrumb/{node['node_id']}", {})
                    assert [item["node_id"] for item in breadcrumb["items"]] == [root_id, nested_id, node["node_id"]]
                    assert [item["name"] for item in breadcrumb["items"]] == ["Headless Folder", "Headless Nested", node["name"]]
                    if page["cursor"] is None:
                        break
                    assert len(found) < 3
                    page = await read_tree(route, "children", {"parent_node_id": nested_id, "limit": 1, "cursor": page["cursor"]})
                assert found == expected_sessions
            rejected = await read_tree(workspace_b, "roots", {"limit": 1, "cursor": roots[workspace_a]["cursor"]}, status=409)
            assert rejected["detail"]["code"] == "stale_catalog_cursor"
            assert rejected["detail"]["owner"] == "session_catalog" and rejected["detail"]["reason"] == "revision"
            assert set(folders[workspace_a]).isdisjoint(folders[workspace_b])
            assert set(sessions[workspace_a]).isdisjoint(sessions[workspace_b])
        assert not transport._http_client.cookies
        query = {"category": "catalog", "query": "Headless Search", "limit": 1}

        async def search(payload: dict, request_id: str) -> list[FederatedSearchEventDTO]:
            request = FederatedSearchRequest.model_validate(payload)
            events = [event async for event in transport.resource_search.search(request, request_id=request_id)]
            assert all(event.request_id == request_id for event in events)
            observed.append({"request": payload, "events": [event.model_dump(mode="json") for event in events]})
            return events

        discovery = await transport.resource_search.discover_workspaces(query="Unrelated", request_id="req_headless_discover")
        assert [item.owner.model_dump() for item in discovery.items] == [owners[workspace_b]]
        first = await search({"scope": {"mode": "origin", "origin": owners[workspace_a]}, "queries": [query]}, "req_headless_a")
        first_page, = [event for event in first if event.type == "page"]
        assert first_page.page is not None and first_page.page.catalog is not None
        assert first_page.page.catalog.items[0].node.session_id == sessions[workspace_a][0]

        both = await search({"scope": {"mode": "selected", "owners": [owners[workspace_a], owners[workspace_b]]}, "queries": [query]}, "req_headless_ab")
        assert both[0].scope is not None
        frozen = both[0].scope.scope.model_dump(mode="json")
        page_b, = [event for event in both if event.page is not None and event.page.workspace_id == owners[workspace_b]["workspace_id"]]
        assert page_b.branch is not None and page_b.branch.cursor is not None
        full = await search({"scope": {"mode": "all"}, "queries": [query]}, "req_headless_all")
        assert full[-1].summary is not None and full[-1].summary.state == "incomplete"
        stopped, = [item for item in full[-1].summary.branches
                    if item.branch.owner is not None and item.branch.owner.workspace_id == owners[workspace_c]["workspace_id"]]
        assert stopped.state == "failed" and stopped.code == "target_unavailable" and stopped.retryable
        assert not (network.stopped / ".boxteam/sessions").exists()

        # 普通入口与机器入口复用本机凭据，不创建游客或要求用户租约。
        browser_search = await transport._http_client.post(
            f"http://127.0.0.1:{network.gateway_port}/api/gateway/search",
            json={"scope": {"mode": "all"}, "queries": [query]}, headers=LOCAL_TOKEN_HEADERS,
        )
        assert browser_search.status_code == 200, browser_search.text
        assert browser_search.headers["content-type"].startswith("text/event-stream")
        def access_counts() -> dict[str, int]:
            database = network.gateway_home / "state/gateway/gateway.sqlite"
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in ("user_account", "user_access_lease", "guest_tracking")}

        access_before = access_counts()
        children_before = {process.pid for process in psutil.Process(gateway.process.pid).children(recursive=True)}
        auth_checks = []
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", trust_env=False) as unauthenticated:
            for endpoint in ("/api/gateway/search", "/api/gateway/search/agent"):
                for credentials in ({}, {"X-Local-Token": "wrong"}):
                    request_id = f"req_search_rejected_{len(auth_checks)}"
                    rejected_request_ids.append(request_id)
                    unauthorized = await unauthenticated.post(endpoint, json={
                        "scope": {"mode": "origin", "origin": owners[workspace_c]},
                        "queries": [{"category": "message", "operation": "content", "query": "must-not-read"}],
                    }, headers={**credentials, "X-Request-ID": request_id})
                    assert unauthorized.status_code == 401, unauthorized.text
                    assert unauthorized.json()["detail"] == "invalid local token"
                    assert unauthorized.headers["X-Request-ID"] == unauthorized.json()["request_id"] == request_id
                    assert "set-cookie" not in unauthorized.headers and not unauthenticated.cookies
                    auth_checks.append({"endpoint": endpoint, "credentials": "wrong" if credentials else "missing",
                        "status": unauthorized.status_code, "response": unauthorized.json()})
        assert access_counts() == access_before == {"user_account": 0, "user_access_lease": 0, "guest_tracking": 0}
        children_after = {process.pid for process in psutil.Process(gateway.process.pid).children(recursive=True)}
        assert children_after == children_before
        assert not (network.stopped / ".boxteam/sessions").exists()
        observed.append({"auth_checks": auth_checks, "access_before": access_before, "access_after": access_counts(),
                         "gateway_children_before": sorted(children_before), "gateway_children_after": sorted(children_after)})
        assert not transport._http_client.cookies
        await asyncio.to_thread(close_gateway_process, gateway)
        gateway = None
        gateway = await asyncio.to_thread(network.start)
        resumed = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                "continuations": [{"branch": page_b.branch.branch.model_dump(mode="json"), "cursor": page_b.branch.cursor}]}, "req_headless_resume_b")
        resumed_page, = [event for event in resumed if event.type == "page"]
        assert resumed_page.page is not None and resumed_page.page.catalog is not None
        assert resumed_page.page.catalog.items[0].node.session_id == sessions[workspace_b][1]
        assert resumed[-1].summary is not None and len(resumed[-1].summary.branches) == 1
        assert resumed[0].scope is not None and resumed[0].scope.scope.model_dump(mode="json") == frozen
    finally:
        await transport.aclose()
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
        (network.artifacts / "headless-search.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n")
        (network.artifacts / "headless-trees.json").write_text(json.dumps(trees, ensure_ascii=False, indent=2) + "\n")
    logs = [(network.artifacts / f"backend-{index}.stderr.log").read_text(encoding="utf-8") for index in (0, 1)]
    actual = {request: [index for index, log in enumerate(logs) if f'request_id="{request}"' in log]
              for request in ("req_headless_discover", "req_headless_a", "req_headless_ab", "req_headless_all", "req_headless_resume_b")}
    assert actual == {"req_headless_discover": [], "req_headless_a": [0], "req_headless_ab": [0, 1],
                      "req_headless_all": [0, 1], "req_headless_resume_b": [1]}
    rejected_dispatch = {request_id: [index for index, log in enumerate(logs) if f'request_id="{request_id}"' in log]
                         for request_id in rejected_request_ids}
    assert rejected_dispatch == {request_id: [] for request_id in rejected_request_ids}
    actual.update(rejected_dispatch)
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


@pytest.mark.parametrize("network", [{"model_stream": True}], indirect=True)
async def test_model_execution_invokes_discovery_and_resource_search_through_gateway(
    network: Network,
) -> None:
    assert network.model is not None
    gateway = await asyncio.to_thread(network.start)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=30, trust_env=False) as ui:
            listing = (await ui.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            owners = [{"gateway_id": network.gateway_id, "workspace_id": workspace_backend_id_for_root(root)}
                      for root in (network.workspace, network.secondary, network.stopped)]
            assert {(item["gateway_id"], item["workspace_id"]) for item in listing["items"]} == {
                (owner["gateway_id"], owner["workspace_id"]) for owner in owners}
            ui.headers["X-BoxTeam-Target-Gateway-Id"] = network.gateway_id
            case = RestartRuntimeSourceCase(ui, network.model, network.workspace, network.artifacts, network.backends)
            targets, main_threads = [], []
            for owner in owners[:2]:
                ui.headers["X-BoxTeam-Workspace-Id"] = owner["workspace_id"]
                await enable_developer_tools_plugins(ui, gateway_id=network.gateway_id, workspace_id=owner["workspace_id"],
                                                     plugin_ids=("terminal", "browser"))
                target = await create_session(ui, "Agent Search Target🧪")
                main = await ui.get(f"/api/v1/sessions/{target}/main-thread")
                main.raise_for_status()
                targets.append(target)
                main_threads.append(main.json()["data"]["thread_id"])

            # 唯一正文由 B 的原消息入口执行并提交 canonical，不手工写执行状态。
            word = "message-body-only-b-🧪"
            assert all(word not in item["name"] for item in listing["items"])
            await case.plan(targets[1], [{"content": word}])
            source = await _send(case, targets[1], "保存用于跨工作区搜索的真实消息正文。")
            await _job(case, source["job_id"])

            # 原 Gateway 导航元数据把 A+B 归入同一组；停止的 C 不属于该组。
            group_name = "Agent Related Projects"
            folder = await ui.post("/api/gateway/workspace-navigation/folders", json={"name": group_name})
            folder.raise_for_status()
            folder_id, = [node["node_id"] for node in folder.json()["data"]["nodes"]
                          if node["kind"] == "workspace_folder" and node["name"] == group_name]
            for owner in owners[:2]:
                node_id, = [node["node_id"] for node in folder.json()["data"]["nodes"] if node.get("workspace") == owner]
                placed = await ui.put("/api/gateway/workspace-navigation/placement",
                                     json={"node_id": node_id, "parent_node_id": folder_id, "mode": "last"})
                placed.raise_for_status()

            ui.headers["X-BoxTeam-Workspace-Id"] = owners[0]["workspace_id"]
            caller = await create_session(ui, "Agent Search Caller")
            calls = await case.plan(caller, [
                _tool_reply("invoke_extension_tool", {"tool_name": "discover_workspaces", "arguments": {"query": group_name}}, "call_discover"),
                _tool_reply("invoke_extension_tool", {"tool_name": "search_resources", "arguments": {
                    "scope": {"mode": "selected", "owners": owners[:2]},
                    "queries": [{"category": category, "query": "Agent Search Target🧪"} for category in ("catalog", "thread")],
                }}, "call_resources"),
                _tool_reply("invoke_extension_tool", {"tool_name": "search_resources", "arguments": {
                    "queries": [{"category": "catalog", "query": "Agent Search Target🧪"}],
                }}, "call_origin"),
                _tool_reply("invoke_extension_tool", {"tool_name": "search_resources", "arguments": {
                    "scope": {"mode": "all"},
                    "queries": [{"category": "message", "operation": "content", "query": word, "match_mode": "literal"}],
                }}, "call_messages"),
                {"content": "SEARCH_COMPLETE"},
            ])
            children_before = {process.pid for process in psutil.Process(gateway.process.pid).children(recursive=True)}
            accepted = await _send(case, caller, "通过已登记工具查找工作区和目标会话；不要发送其它消息。")
            await _job(case, accepted["job_id"])
            raw_results = {name: case.tool_result(calls[index] + ":tool-call:" + name)
                           for index, name in enumerate(("call_discover", "call_resources", "call_origin", "call_messages"))}
            discovered = GatewayWorkspaceDiscoveryResultsDTO.model_validate(raw_results["call_discover"])
            assert discovered.query == group_name and discovered.complete
            assert [item.owner.model_dump() for item in discovered.items] == owners[:2]
            assert all(item.breadcrumb_names[0] == group_name for item in discovered.items)
            events = TypeAdapter(list[FederatedSearchEventDTO]).validate_python(raw_results["call_resources"])
            assert events[0].scope is not None and events[0].scope.scope.mode == "selected"
            assert [item.model_dump() for item in events[0].scope.scope.owners] == [item.owner.model_dump() for item in discovered.items]
            pages = [event.page for event in events if event.type == "page"]
            assert len(pages) == 4 and events[-1].summary is not None and events[-1].summary.state == "complete"
            for owner, target, thread in zip(owners[:2], targets, main_threads, strict=True):
                catalog, = [page.catalog for page in pages if page is not None and page.workspace_id == owner["workspace_id"] and page.catalog]
                threads, = [page.threads for page in pages if page is not None and page.workspace_id == owner["workspace_id"] and page.threads]
                assert [item.node.session_id for item in catalog.items] == [target]
                hit, = threads.items
                assert (hit.workspace_id, hit.session_id, hit.thread_id, hit.kind) == (owner["workspace_id"], target, thread, "main")
                assert hit.thread_uri.startswith(f"boxteam://{network.gateway_id}/") and thread in hit.thread_uri

            origin = TypeAdapter(list[FederatedSearchEventDTO]).validate_python(raw_results["call_origin"])
            assert origin[0].scope is not None and origin[0].scope.scope.mode == "origin"
            assert [item.model_dump() for item in origin[0].scope.scope.owners] == owners[:1]
            messages = TypeAdapter(list[FederatedSearchEventDTO]).validate_python(raw_results["call_messages"])
            assert messages[0].scope is not None and messages[0].scope.scope.mode == "all"
            assert [item.model_dump() for item in messages[0].scope.scope.owners] == [owners[2], *owners[:2]]
            message_hits = [hit for event in messages if event.page is not None and event.page.messages is not None
                            for hit in event.page.messages.items]
            message, = message_hits
            assert (message.thread.workspace_id, message.thread.session_id, message.thread.thread_id) == (
                owners[1]["workspace_id"], targets[1], main_threads[1])
            assert message.match.role == "assistant" and word in message.match.preview
            assert message.match.reference.category == "message"
            assert messages[-1].summary is not None
            stopped, = [branch for branch in messages[-1].summary.branches if branch.branch.owner is not None
                        and branch.branch.owner.model_dump() == owners[2]]
            assert stopped.state == "failed" and stopped.code == "target_unavailable"
            located = await ui.post(f"/api/v1/sessions/{targets[1]}/threads/{main_threads[1]}/history",
                headers={"X-BoxTeam-Workspace-Id": owners[1]["workspace_id"]}, json={"direction": "around",
                    "anchor_turn_id": message.match.turn_id, "anchor_message": message.match.reference.model_dump(mode="json")})
            located.raise_for_status()
            assert located.json()["data"]["located_message"]["content"] == word

            wire = case.request(calls[-1])
            tool_messages = [message for message in wire["messages"] if message["role"] == "tool"]
            assert len(tool_messages) == 4 and all(message["name"] == "invoke_extension_tool" for message in tool_messages)
            assert {message["tool_call_id"]: json.loads(message["content"]) for message in tool_messages} == raw_results
            discovery_wire, = [message for message in case.request(calls[1])["messages"] if message["role"] == "tool"]
            assert json.loads(discovery_wire["content"]) == raw_results["call_discover"]
            assert len(case.requests()) == 6 and case.call_count(targets[0], main_threads[0]) == 0
            assert case.call_count(targets[1], main_threads[1]) == 1
            assert not ui.cookies and not (network.stopped / ".boxteam/sessions").exists()
            assert {process.pid for process in psutil.Process(gateway.process.pid).children(recursive=True)} == children_before
            case.write_actual("agent-resource-search", {"discovery": discovered.model_dump(mode="json"), "events": raw_results["call_resources"],
                "origin": raw_results["call_origin"], "all_messages": raw_results["call_messages"], "targets": targets,
                "main_threads": main_threads, "owners": owners, "caller": caller, "source_job": source,
                "message_location": located.json(), "discovery_wire": case.request(calls[1]), "final_wire": wire})
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
    actual_requests = re.findall(r'request_id="([^"]+)".*"(?:GET /api/gateway/search/workspaces(?:\?[^ ]*)?|POST /api/gateway/search/agent) HTTP',
                                 (network.artifacts / "gateway.stderr.log").read_text(encoding="utf-8"))
    assert len(actual_requests) == len(set(actual_requests)) == 4
    assert actual_requests[1] == events[0].request_id
    assert actual_requests[2:] == [origin[0].request_id, messages[0].request_id]
    logs = [(network.artifacts / f"backend-{index}.stderr.log").read_text(encoding="utf-8") for index in (0, 1)]
    dispatch = {request_id: [index for index, log in enumerate(logs) if any(
        f'request_id="{request_id}"' in line and "logger=uvicorn.access " in line and "POST /api/v1/search " in line
        for line in log.splitlines())] for request_id in actual_requests}
    assert list(dispatch.values()) == [[], [0, 1], [0], [0, 1]]
    case.write_actual("agent-search-request-ids", {"request_ids": actual_requests, "dispatch": dispatch})
