"""真实 Gateway 和两个 Backend 的名称联邦；关键目录由产品创建入口生成。"""

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from tests.harness.python.run_context import TestRunContext
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass(frozen=True)
class Network:
    workspace: Path
    secondary: Path
    stopped: Path
    gateway_home: Path
    gateway_port: int
    backend_port: int
    artifacts: Path

    def start(self):
        return start_gateway_process(
            workspace_root=self.workspace, default_backend_url=f"http://127.0.0.1:{self.backend_port}",
            port=self.gateway_port, boxteam_home=self.gateway_home, artifacts_dir=self.artifacts,
        )


@pytest.fixture
def network(request: pytest.FixtureRequest, integration_workspace_root_path: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Network]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    workspace = Path(integration_workspace_root_path)
    secondary, stopped = workspace / "secondary-b", workspace / "stopped-c"
    for root in (secondary, stopped):
        prepare_default_test_workspace(workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
        install_test_workspace_config(workspace_root=root, config_path=workspace / ".boxteam/workspace.jsonc",
                                      schema_path=workspace / ".boxteam/workspace_schema.jsonc")
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    lease = E2EProcessLease(runtime / "service-ports.json", tuple(ports.port(offset) for offset in (0, 1, 20)))
    fixture = Network(workspace, secondary, stopped, runtime / "gateway-home", ports.port(20), ports.port(0), context.artifacts_dir)
    with ExitStack() as cleanup:
        lease.acquire()
        cleanup.callback(lease.release)
        for index, root in enumerate((workspace, secondary)):
            backend = start_backend_process(workspace_root=str(root), port=ports.port(index), log_name=f"backend-{index}",
                                            boxteam_home=runtime / f"backend-home-{index}", artifacts_dir=fixture.artifacts)
            cleanup.callback(close_backend_process, backend)
        # 外部后端登记是查询输入，目录历史不通过磁盘写入或 mock 准备。
        gateway_root = fixture.gateway_home / "state/gateway"
        state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
        registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json", state_store=state)
        try:
            for route, root, url in (("route_b", secondary, f"http://127.0.0.1:{ports.port(1)}"), ("route_c", stopped, "")):
                registry.upsert(WorkspaceTarget(
                    workspace_id=route, backend_workspace_id=workspace_backend_id_for_root(root), name="Unrelated workspace" if route == "route_b" else "Stopped",
                    root_path=str(root), backend_url=url, connection_kind="local", desired_running=False,
                ), activate=False)
        finally:
            registry.close()
            state.close()
        yield fixture


@pytest.mark.asyncio
async def test_exact_scopes_partial_failure_and_restart_continuation(network: Network) -> None:
    gateway = await asyncio.to_thread(network.start)
    observations = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=25, trust_env=False) as client:
            await acquire_gateway_guest(client)
            health = (await client.get("/api/gateway/health")).json()["data"]
            gateway_id = health["gateway_id"]
            listing = (await client.get("/api/gateway/workspaces", params={"check_health": False})).json()["data"]
            route_a = next(item["workspace_id"] for item in listing["items"]
                           if item["backend_workspace_id"] == workspace_backend_id_for_root(network.workspace))
            owners = {item["workspace_id"]: {"gateway_id": gateway_id, "workspace_id": item["backend_workspace_id"]} for item in listing["items"]}
            assert route_a != "route_b" and owners[route_a] != owners["route_b"]
            sessions = {}
            for route in (route_a, "route_b"):
                client.headers["X-BoxTeam-Workspace-Id"] = route
                sessions[route] = [await create_session(client, f"Shared Search {index}") for index in range(3)]

            async def search(payload: dict, request_id: str) -> list[dict]:
                async with client.stream("POST", "/api/gateway/search", json=payload, headers={"X-Request-ID": request_id}) as response:
                    assert response.status_code == 200, (await response.aread()).decode()
                    assert response.headers["X-Request-ID"] == request_id
                    events = []
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line.removeprefix("data: "))
                            assert event["request_id"] == request_id
                            events.append(event)
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
                observations.append({"request": payload, "events": events})
                return events

            query = {"category": "catalog", "query": "ShSe", "limit": 1}
            first = await search({"scope": {"mode": "origin", "origin": owners[route_a]}, "queries": [query]}, "req_federation_a")
            assert first[0]["scope"]["scope"]["owners"] == [owners[route_a]]
            a_page, = [event for event in first if event["type"] == "page"]
            assert a_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[route_a][0]

            both = await search({"scope": {"mode": "selected", "owners": [owners[route_a], owners["route_b"]]}, "queries": [query]}, "req_federation_ab")
            pages = [event for event in both if event["type"] == "page"]
            assert len(pages) == 2
            assert {event["page"]["workspace_id"] for event in pages} == {owners[route_a]["workspace_id"], owners["route_b"]["workspace_id"]}
            b_page = next(event for event in pages if event["page"]["workspace_id"] == owners["route_b"]["workspace_id"])
            frozen = both[0]["scope"]["scope"]
            await asyncio.to_thread(close_gateway_process, gateway)
            gateway = None
            gateway = await asyncio.to_thread(network.start)
            resumed = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                    "continuations": [{"branch": b_page["branch"]["branch"], "cursor": b_page["branch"]["cursor"]}]}, "req_federation_resume")
            resumed_page, = [event for event in resumed if event["type"] == "page"]
            assert resumed[0]["scope"]["scope"] == frozen
            assert resumed_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions["route_b"][1]
            assert len(resumed[-1]["summary"]["branches"]) == 1

            # 通过产品修改 B 的目录版本，A 的旧页仍可继续；不替换 owner 响应制造冲突。
            client.headers["X-BoxTeam-Workspace-Id"] = "route_b"
            await create_session(client, "Revision bump")
            invalidated = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                        "continuations": [{"branch": item["branch"]["branch"], "cursor": item["branch"]["cursor"]} for item in pages]}, "req_federation_conflict")
            a_continued, = [event for event in invalidated if event["type"] == "page"]
            assert a_continued["branch"]["branch"]["owner"] == owners[route_a]
            assert a_continued["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions[route_a][1]
            failed = next(item for item in invalidated[-1]["summary"]["branches"] if item["branch"]["owner"] == owners["route_b"])
            assert failed["state"] == "failed" and failed["code"] == "owner_rejected"
            assert failed["cursor"] == b_page["branch"]["cursor"]
            retried = await search({"scope": {"mode": "resume", "scope": frozen}, "queries": [query],
                                    "continuations": [{"branch": b_page["branch"]["branch"]}]}, "req_federation_retry")
            retry_page, = [event for event in retried if event["type"] == "page"]
            assert retry_page["branch"]["branch"]["owner"] == owners["route_b"]
            assert retry_page["page"]["catalog"]["items"][0]["node"]["session_id"] == sessions["route_b"][0]
            assert len(retried[-1]["summary"]["branches"]) == 1

            full = await search({"scope": {"mode": "all"}, "queries": [{**query, "limit": 50}]}, "req_federation_all")
            assert full[-1]["summary"]["state"] == "incomplete"
            assert len(full[-1]["summary"]["branches"]) == 3
            stopped = next(item for item in full[-1]["summary"]["branches"] if item["branch"]["owner"] == owners["route_c"])
            assert stopped["code"] == "target_unavailable" and stopped["state"] == "failed"
            assert sum(len(event["page"]["catalog"]["items"]) for event in full if event["type"] == "page") == 6
            assert full[-1]["summary"]["returned_results"] == 6
            assert not (network.stopped / ".boxteam/sessions").exists()
    finally:
        (network.artifacts / "federation-network.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
