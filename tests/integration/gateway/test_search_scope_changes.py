"""真实双后端的 all 冻结范围；控制面改动不替查询完成领域步骤。"""

import asyncio
import json
from contextlib import closing
from pathlib import Path

import httpx

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.search_federation import FederatedSearchRequest
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.integration.gateway.test_federated_search import Network
from tests.integration.gateway.test_federated_search import (
    network as network,  # noqa: PLC0414 - 沿当前测试路径隔离双后端、端口和运行产物。
)
from tests.support.api_helpers import create_session
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
)
from tests.support.processes.workspace import workspace_backend_id_for_root
from tests.support.workspaces import prepare_default_test_workspace


async def test_all_scope_keeps_removed_owner_and_excludes_later_registration(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    origin = f"http://127.0.0.1:{network.gateway_port}"
    transport = GatewaySessionContextClient(gateway_url=origin,
        local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
    observations = []
    expected_dispatch = {}
    later_root = network.workspace / "later-offline-d"
    try:
        async with httpx.AsyncClient(base_url=origin, headers=LOCAL_TOKEN_HEADERS, timeout=25, trust_env=False) as client:
            await acquire_gateway_guest(client)
            gateway_id = (await client.get("/api/gateway/health")).json()["data"]["gateway_id"]
            listing = (await client.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            workspace_a, workspace_b, workspace_c = (
                workspace_backend_id_for_root(root) for root in (network.workspace, network.secondary, network.stopped)
            )
            assert gateway_id == network.gateway_id
            client.headers["X-BoxTeam-Target-Gateway-Id"] = gateway_id
            owners = {item["workspace_id"]: {"gateway_id": item["gateway_id"], "workspace_id": item["workspace_id"]}
                for item in listing["items"]}
            assert set(owners) == {workspace_a, workspace_b, workspace_c}
            assert all(owner["gateway_id"] == gateway_id for owner in owners.values())
            sessions = {}
            for route in (workspace_a, workspace_b):
                client.headers["X-BoxTeam-Workspace-Id"] = route
                sessions[route] = [await create_session(client, f"Scope marker {index}") for index in range(2)]

            async def search(payload: dict, request_id: str, dispatched: list[int]) -> list[dict]:
                events = [event.model_dump(mode="json") async for event in transport.resource_search.search(
                    FederatedSearchRequest.model_validate(payload), request_id=request_id)]
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert all(event["request_id"] == request_id for event in events)
                observations.append({"request_id": request_id, "request": payload, "events": events})
                expected_dispatch[request_id] = dispatched
                return events

            query = {"category": "catalog", "query": "Scope marker", "limit": 1}
            first = await search({"scope": {"mode": "origin", "origin": owners[workspace_a]}, "queries": [query]}, "req_scope_only_a", [0])
            assert first[0]["scope"]["scope"]["owners"] == [owners[workspace_a]]
            both = await search({"scope": {"mode": "selected", "owners": [owners[workspace_a], owners[workspace_b]]},
                "queries": [query]}, "req_scope_selected_ab", [0, 1])
            assert [branch["branch"]["owner"] for branch in both[-1]["summary"]["branches"]] == [owners[workspace_a], owners[workspace_b]]

            discovery = await transport.resource_search.discover_workspaces(query="Unrelated", request_id="req_scope_discovery")
            assert [item.owner.model_dump() for item in discovery.items] == [owners[workspace_b]]
            expected_dispatch["req_scope_discovery"] = []
            related = await search({"scope": {"mode": "selected", "owners": [item.owner.model_dump() for item in discovery.items]},
                "queries": [query]}, "req_scope_discovered_b", [1])
            related_page, = [event for event in related if event["type"] == "page"]
            assert related_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[workspace_b][0]

            roots = {}
            for route in (workspace_a, workspace_b):
                response = await client.get("/api/v1/workspace/files/roots", headers={"X-BoxTeam-Workspace-Id": route})
                response.raise_for_status()
                roots[route] = response.json()["data"]["workspace_uri"]
            target = roots[workspace_b] + "/search-two/needle-c.md"
            current = await client.get("/api/v1/workspace/files/content", params={"path": target},
                headers={"X-BoxTeam-Workspace-Id": workspace_b})
            current.raise_for_status()
            original = current.json()["data"]
            saved = await client.put("/api/v1/workspace/files/content", params={"path": target},
                headers={"X-BoxTeam-Workspace-Id": workspace_b},
                json={"content": original["content"] + "scope-body-only-b", "expected_revision": original["revision"]})
            saved.raise_for_status()
            content_query = {"category": "file", "operation": "content", "query": "scope-body-only-b", "match_mode": "literal",
                "file_targets": [{"owner": owners[route], "target": {"uri": roots[route] + "/search-two",
                    "search_options": {"include_globs": ["*.md"]}}} for route in (workspace_a, workspace_b)]}
            content = await search({"scope": {"mode": "all"}, "queries": [content_query]}, "req_scope_all_body", [0, 1])
            hits = [(page["page"]["workspace_id"], hit["uri"]) for page in content if page["type"] == "page"
                for hit in page["page"]["files"]["matches"]]
            assert hits == [(owners[workspace_b]["workspace_id"], target)]
            assert content[0]["scope"]["scope"]["mode"] == "all"

            # 空集合及未知身份必须在分发前拒绝，不能解释为 all 或只查有效子集。
            for request_id, scope, status in (
                ("req_scope_empty", {"mode": "selected", "owners": []}, 422),
                ("req_scope_unknown", {"mode": "selected", "owners": [owners[workspace_a],
                    {"gateway_id": gateway_id, "workspace_id": "00000000-0000-4000-8000-000000000099"}]}, 404),
            ):
                rejected = await client.post("/api/gateway/search/agent", json={"scope": scope, "queries": [query]},
                    headers={"X-Request-ID": request_id})
                assert rejected.status_code == status, rejected.text
                assert rejected.headers["X-Request-ID"] == request_id
                assert rejected.json()["request_id"] == request_id
                observations.append({"request_id": request_id, "status": status, "error": rejected.json()})
                expected_dispatch[request_id] = []

            full = await search({"scope": {"mode": "all"}, "queries": [query]}, "req_scope_all_initial", [0, 1])
            frozen = full[0]["scope"]["scope"]
            assert frozen["mode"] == "all" and {owner["workspace_id"] for owner in frozen["owners"]} == {
                owner["workspace_id"] for owner in owners.values()}
            original_branches = full[-1]["summary"]["branches"]
            stopped, = [branch for branch in original_branches if branch["branch"]["owner"] == owners[workspace_c]]
            assert stopped["state"] == "failed" and stopped["code"] == "target_unavailable"

            removed = await client.delete(f"/api/gateway/workspaces/{workspace_b}")
            removed.raise_for_status()
            assert workspace_b not in {item["workspace_id"] for item in removed.json()["data"]["items"]}
            await asyncio.to_thread(close_gateway_process, gateway)
            gateway = None
            # Gateway 停止后沿原 registry owner 准备离线登记，沿用 B 的显示名但使用独立 UUID。
            # 这里只验证查询读取控制面；不以此声称新增离线工作区的管理 UI 已验收。
            prepare_default_test_workspace(workspace_root=later_root,
                template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
                fresh_identity=True)
            later_owner = {"gateway_id": gateway_id, "workspace_id": workspace_backend_id_for_root(later_root)}
            assert later_owner["workspace_id"] not in owners
            control_root = network.gateway_home / "state/gateway"
            with (
                closing(GatewayStateStore(path=control_root / "gateway.sqlite")) as state,
                closing(GatewayWorkspaceRegistry(gateway_id=gateway_id, storage_path=control_root / "workspaces.json", state_store=state)) as registry,
            ):
                registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=later_owner["workspace_id"],
                    name="Unrelated workspace", root_path=str(later_root), backend_url="", connection_kind="local",
                    desired_running=False), activate=False)
            gateway = await asyncio.to_thread(network.start)

            resumed = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                "continuations": [{"branch": branch["branch"], "cursor": branch["cursor"]} for branch in original_branches]},
                "req_scope_all_frozen", [0])
            assert resumed[0]["scope"]["scope"] == frozen
            branches = resumed[-1]["summary"]["branches"]
            assert [branch["branch"] for branch in branches] == [branch["branch"] for branch in original_branches]
            missing, = [branch for branch in branches if branch["branch"]["owner"] == owners[workspace_b]]
            previous, = [branch for branch in original_branches if branch["branch"]["owner"] == owners[workspace_b]]
            assert missing["state"] == "failed" and missing["code"] == "owner_unavailable"
            assert missing["cursor"] == previous["cursor"] and missing["returned_results"] == 0
            a_page, = [event for event in resumed if event["type"] == "page"]
            assert a_page["page"]["workspace_id"] == owners[workspace_a]["workspace_id"]
            assert a_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[workspace_a][1]
            assert later_owner not in resumed[0]["scope"]["scope"]["owners"]

            refreshed = await search({"scope": {"mode": "all"}, "queries": [query]}, "req_scope_all_refreshed", [0])
            assert {owner["workspace_id"] for owner in refreshed[0]["scope"]["scope"]["owners"]} == {
                owners[workspace_a]["workspace_id"], owners[workspace_c]["workspace_id"], later_owner["workspace_id"]}
            assert not (network.stopped / ".boxteam/sessions").exists() and not (later_root / ".boxteam/sessions").exists()
            assert not transport._http_client.cookies
    finally:
        await transport.aclose()
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
        (network.artifacts / "scope-registration-changes.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
    logs = [(network.artifacts / f"backend-{index}.stderr.log").read_text(encoding="utf-8") for index in (0, 1)]
    actual_dispatch = {request_id: [index for index, log in enumerate(logs) if any(
        f'request_id="{request_id}"' in line and "logger=uvicorn.access " in line for line in log.splitlines())]
        for request_id in expected_dispatch}
    (network.artifacts / "actual-search-dispatch.json").write_text(json.dumps({
        "expected": expected_dispatch, "actual": actual_dispatch,
    }, indent=2) + "\n")
    assert actual_dispatch == expected_dispatch
