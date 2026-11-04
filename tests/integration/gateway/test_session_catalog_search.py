"""两个真实后端生成目录，经唯一联邦链验证在线、离线与重启续页。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
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
    BackendProcess,
    close_backend_process,
    start_backend_process,
)
from tests.support.session_navigation.http_operations import submit_navigation_operation
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass(frozen=True)
class CatalogNetwork:
    workspace: Path
    home: Path
    artifacts: Path
    port: int
    gateway_id: str
    backends: tuple[BackendProcess, ...]

    def start(self):
        return start_gateway_process(workspace_root=self.workspace, default_backend_url=f"http://127.0.0.1:{self.backends[0].port}",
                                     port=self.port, boxteam_home=self.home, artifacts_dir=self.artifacts)


@pytest.fixture
def catalog_network(request: pytest.FixtureRequest, integration_workspace_root_path: str,
                    monkeypatch: pytest.MonkeyPatch) -> Iterator[CatalogNetwork]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    workspace = Path(integration_workspace_root_path)
    secondary = workspace / "catalog-secondary"
    prepare_default_test_workspace(workspace_root=secondary, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    install_test_workspace_config(workspace_root=secondary, config_path=workspace / ".boxteam/workspace.jsonc",
                                  schema_path=workspace / ".boxteam/workspace_schema.jsonc")
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    home = runtime / "gateway-home"
    root = home / "state/gateway"
    token = root / "credentials/local-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(LOCAL_TOKEN_HEADERS["X-Local-Token"] + "\n", encoding="utf-8")
    token.chmod(0o600)
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    with ExitStack() as cleanup:
        lease = E2EProcessLease(runtime / "ports.json", tuple(ports.port(offset) for offset in (0, 1, 20)))
        lease.acquire()
        cleanup.callback(lease.release)
        backends = []
        for index, target_root in enumerate((workspace, secondary)):
            backend = start_backend_process(workspace_root=str(target_root), port=ports.port(index), log_name=f"catalog-backend-{index}",
                                            boxteam_home=home, artifacts_dir=context.artifacts_dir)
            cleanup.callback(close_backend_process, backend)
            backends.append(backend)
        state = GatewayStateStore(path=root / "gateway.sqlite")
        registry = GatewayWorkspaceRegistry(storage_path=root / "workspaces.json", state_store=state, gateway_id=gateway_id)
        try:
            # primary 沿启动器原默认登记；只预登记第二目标，避免重复稳定身份。
            for index, backend in enumerate(backends[1:], start=1):
                registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=backend.workspace_id,
                    name=f"Catalog workspace {index}", root_path=backend.workspace_root, backend_url=f"http://127.0.0.1:{backend.port}",
                    connection_kind="local", desired_running=False), activate=False)
        finally:
            registry.close()
            state.close()
        yield CatalogNetwork(workspace, home, context.artifacts_dir, ports.port(20), gateway_id, tuple(backends))


async def test_gateway_catalog_owner_and_offline_mirror_share_original_pages(catalog_network: CatalogNetwork) -> None:
    network = catalog_network
    expected, originals = {}, {}
    # 关键目录必须经产品入口建立，不能把镜像或测试预写当作产品成功。
    for index, backend in enumerate(network.backends):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend.port}", headers={**LOCAL_TOKEN_HEADERS,
            "X-BoxTeam-Workspace-Id": backend.workspace_id, "X-BoxTeam-Gateway-Id": network.gateway_id,
            "X-BoxTeam-Navigation-Actor": "guest"}, timeout=25, trust_env=False) as client:
            folder = await submit_navigation_operation(client, "create_folder", name=f"目录报告 {index}")
            assert folder["state"] == "committed"
            ids = []
            for number in range(3):
                session_id = await create_session(client, f"Shared Catalog Needle {number}")
                moved = await submit_navigation_operation(client, "move_node", target_node_id=session_id,
                                                          parent_node_id=folder["created_node_id"])
                assert moved["state"] == "committed"
                ids.append(session_id)
            expected[backend.workspace_id] = ids
            response = await client.get("/api/v1/session-catalog/search", params={"query": "SCN", "limit": 1})
            assert response.status_code == 200, response.text
            originals[backend.workspace_id] = response.json()["data"]
    gateway = await asyncio.to_thread(network.start)
    observations = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.port}", headers=LOCAL_TOKEN_HEADERS,
                                     timeout=25, trust_env=False) as client:
            await acquire_gateway_guest(client)
            owners = [{"gateway_id": network.gateway_id, "workspace_id": backend.workspace_id} for backend in network.backends]
            query = {"category": "catalog", "query": "SCN", "limit": 1, "allow_catalog_mirror": True}

            async def search(payload: dict) -> list[dict]:
                request_id = f"req_catalog_mirror_{len(observations)}"
                response = await client.post("/api/gateway/search", json=payload, headers={"X-Request-ID": request_id})
                assert response.status_code == 200, response.text
                assert response.headers["X-Request-ID"] == request_id
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert all(event["request_id"] == request_id for event in events)
                assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
                observations.append({"request": payload, "events": events})
                return events

            online = await search({"scope": {"mode": "selected", "owners": owners}, "queries": [query]})
            pages = [event for event in online if event["type"] == "page"]
            assert len(pages) == 2
            for event in pages:
                assert event["source"]["kind"] == "owner" and not event["source"]["stale"]
                assert event["page"]["catalog"] == originals[event["page"]["workspace_id"]]
                assert event["page"]["catalog"]["items"][0]["relative_path"].startswith("目录报告 ")
            assert (await client.get("/api/gateway/session-catalog/search", params={"query": "SCN"})).status_code == 404
            # 后台同步仍是原控制面职责；查询本身不负责填充镜像。
            cache = network.home / "state/gateway/indexes/session-catalogs/owner-v3"
            for _ in range(100):
                cached = [json.loads(path.read_text()) for path in cache.glob("*.json")]
                if {item["owner"]["workspace_id"] for item in cached} == set(expected):
                    break
                await asyncio.sleep(0.1)
            assert {item["owner"]["workspace_id"] for item in cached} == set(expected)
            assert all(item["schema_version"] == 3 and item["catalog_revision"] >= 1 for item in cached)
            b_first = next(event for event in pages if event["page"]["workspace_id"] == owners[1]["workspace_id"])
            continuation = {"scope": {"mode": "resume", "scope": online[0]["scope"]["scope"]}, "queries": [query],
                "continuations": [{"branch": b_first["branch"]["branch"], "cursor": b_first["page"]["catalog"]["cursor"]}]}
            await asyncio.to_thread(close_backend_process, network.backends[1])
            stale = await search(continuation)
            b_next, = [event for event in stale if event["type"] == "page"]
            assert b_next["source"]["kind"] == "gateway_catalog_mirror" and b_next["source"]["stale"]
            assert b_next["source"]["updated_at"] and b_next["source"]["unavailable_reason"]
            assert b_next["page"]["catalog"]["items"][0]["node"]["session_id"] == expected[owners[1]["workspace_id"]][1]
            live_only = {**continuation, "queries": [{**query, "allow_catalog_mirror": False}]}
            failed = await search(live_only)
            assert not any(event["type"] == "page" for event in failed)
            assert failed[-1]["summary"]["branches"][0]["code"] == "target_unavailable"
            close_gateway_process(gateway)
            gateway = await asyncio.to_thread(network.start)
            await acquire_gateway_guest(client)
            restarted = await search(continuation)
            restored, = [event for event in restarted if event["type"] == "page"]
            assert restored["page"] == b_next["page"] and restored["source"] == b_next["source"]
            # 一个 owner 的镜像与 cursor 不因另一工作区推进而改变。
            online_a = await search({"scope": {"mode": "origin", "origin": owners[0]}, "queries": [query]})
            a, = [event for event in online_a if event["type"] == "page"]
            assert a["source"]["kind"] == "owner" and a["page"]["catalog"] == originals[owners[0]["workspace_id"]]
    finally:
        close_gateway_process(gateway)
        (network.artifacts / "catalog-federation-network.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
