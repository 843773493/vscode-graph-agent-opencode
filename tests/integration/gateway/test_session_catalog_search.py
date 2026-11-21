"""两个真实后端生成目录，经唯一联邦链验证在线、离线与重启续页。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from commentjson import loads as load_jsonc

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from tests.harness.performance.record import stage
from tests.harness.performance.report import distribution
from tests.harness.python.run_context import TestRunContext
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
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
    folders = {}
    # 关键目录必须经产品入口建立，不能把镜像或测试预写当作产品成功。
    for index, backend in enumerate(network.backends):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend.port}", headers={**LOCAL_TOKEN_HEADERS,
            "X-BoxTeam-Workspace-Id": backend.workspace_id, "X-BoxTeam-Gateway-Id": network.gateway_id,
            "X-BoxTeam-Navigation-Actor": "guest"}, timeout=25, trust_env=False) as client:
            folder = await submit_navigation_operation(client, "create_folder", name=f"目录报告 {index}")
            assert folder["state"] == "committed"
            folders[backend.workspace_id] = folder["created_node_id"]
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
            # 普通浏览经真实 Gateway 使用原镜像；写后范围只向目标请求所需页。
            owner = owners[0]
            headers = {"X-BoxTeam-Workspace-Id": owner["workspace_id"], "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"]}
            folder_id = folders[owner["workspace_id"]]
            original_cache = {path: path.stat().st_mtime_ns for path in cache.glob("*.json")}
            samples = []
            for sample in range(21):
                with stage("catalog.gateway.children", sample=sample, warm=sample > 0, nodes=4) as measured:
                    started = perf_counter()
                    response = await client.get("/api/v1/session-catalog/children", headers=headers,
                        params={"parent_node_id": folder_id},)
                    elapsed_ms = (perf_counter() - started) * 1000
                    measured.update(response_bytes=len(response.content), http_status=response.status_code)
                assert response.status_code == 200, response.text
                page = response.json()["data"]
                assert page["sync"]["source"] == "gateway_mirror" and not page["sync"]["stale"]
                assert page["sync"]["gateway_id"] == owner["gateway_id"]
                assert page["sync"]["workspace_id"] == owner["workspace_id"]
                assert page["parent_node_id"] == folder_id
                assert [item["node_id"] for item in page["items"]] == expected[owner["workspace_id"]]
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                samples.append({"elapsed_ms": elapsed_ms, "response_bytes": len(response.content)})
            assert {path: path.stat().st_mtime_ns for path in cache.glob("*.json")} == original_cache
            observations.append({"measurement": "warm_gateway_browse", "node_count": 4,
                "first": samples[0], "samples": samples[1:], **distribution([sample["elapsed_ms"] for sample in samples[1:]])})
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.backends[0].port}",
                headers={**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": owner["workspace_id"],
                    "X-BoxTeam-Gateway-Id": owner["gateway_id"], "X-BoxTeam-Navigation-Actor": "guest"}, timeout=25, trust_env=False) as authoritative:
                receipt = await submit_navigation_operation(authoritative, "rename_node", target_node_id=folder_id, name="写后目录")
                minimum = receipt["committed_catalog_revision"]
                assert minimum > page["catalog_revision"]
                for path, scope in (("children", {"parent_node_id": folder_id}),
                    (f"breadcrumb/{folder_id}", {}), ("export", {"node_id": folder_id})):
                    response = await client.get(f"/api/v1/session-catalog/{path}", headers=headers,
                        params={**scope, "minimum_catalog_revision": minimum})
                    assert response.status_code == 200, response.text
                    confirmed = response.json()["data"]
                    assert confirmed["catalog_revision"] >= minimum
                    assert confirmed["sync"]["workspace_id"] == owner["workspace_id"]
                    if path == "export":
                        assert confirmed["node_ids"] == [folder_id] and not confirmed["complete"]
                        assert confirmed["items"][0]["name"] == "写后目录"
                response = await client.get("/api/v1/session-catalog/breadcrumb/missing", headers=headers,
                    params={"minimum_catalog_revision": minimum + 1})
                assert response.status_code == 409, response.text
                # 配置来源走原 CAS 保存入口；只变 UI 配置同样推进目录投影 token，不改 catalog 数字。
                source_url = "/api/v1/config/sources/workspace_root_mutable_override"
                source = await authoritative.get(source_url)
                assert source.status_code == 200, source.text
                document = source.json()["data"]
                configuration = load_jsonc(document["jsonc"])
                configuration.setdefault("ui", {})["ignored_paths"] = ["catalog-projection-probe"]
                previous_version = (await authoritative.get("/api/v1/session-catalog/snapshot")).json()["data"]
                saved = await authoritative.put(source_url, json={
                    "gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"],
                    "source_key": document["source_key"], "jsonc": json.dumps(configuration, ensure_ascii=False),
                    "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                    "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                    "expected_active_digest": document["active_digest"], "idempotency_key": "catalog-projection-config",
                })
                assert saved.status_code == 200, saved.text
                version = (await authoritative.get("/api/v1/session-catalog/snapshot")).json()["data"]
                assert version["catalog_revision"] == previous_version["catalog_revision"]
                assert version["projection_revision"] != previous_version["projection_revision"]
                for path, scope in (("children", {"parent_node_id": folder_id}),
                    (f"breadcrumb/{folder_id}", {}), ("export", {"node_id": folder_id})):
                    response = await client.get(f"/api/v1/session-catalog/{path}",
                        headers={**headers, "X-BoxTeam-Catalog-Revision": version["projection_revision"]},
                        params={**scope, "minimum_catalog_revision": version["catalog_revision"]})
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["revision"] == version["projection_revision"]
                    assert response.headers["X-Request-ID"] == response.json()["request_id"]
                originals[owner["workspace_id"]] = (await authoritative.get("/api/v1/session-catalog/search", params={"query": "SCN", "limit": 1})).json()["data"]
            b_first = next(event for event in pages if event["page"]["workspace_id"] == owners[1]["workspace_id"])
            continuation = {"scope": {"mode": "resume", "scope": online[0]["scope"]["scope"]}, "queries": [query],
                "continuations": [{"branch": b_first["branch"]["branch"], "cursor": b_first["page"]["catalog"]["cursor"]}]}
            await asyncio.to_thread(close_backend_process, network.backends[1])
            offline_owner = owners[1]
            offline_revision = next(item["catalog_revision"] for item in cached if item["owner"]["workspace_id"] == offline_owner["workspace_id"])
            response = await client.get("/api/v1/session-catalog/children", headers={
                "X-BoxTeam-Workspace-Id": offline_owner["workspace_id"],
                "X-BoxTeam-Target-Gateway-Id": offline_owner["gateway_id"],
                "X-Request-ID": "req_catalog_disconnected_minimum",
            }, params={"parent_node_id": folders[offline_owner["workspace_id"]], "minimum_catalog_revision": offline_revision + 1})
            assert response.status_code == 502, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "req_catalog_disconnected_minimum"
            assert "无法连接工作区后端" in response.json()["detail"]
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
