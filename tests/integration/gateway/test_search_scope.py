"""真实 Gateway 重启后的范围续用；停止目标作为查询输入登记，不构造执行历史。"""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)
from tests.support.workspaces import prepare_default_test_workspace


@dataclass(frozen=True)
class ScopeNetwork:
    workspace: Path
    gateway_home: Path
    gateway_id: str
    artifacts: Path
    gateway_port: int
    backend_port: int
    stopped_roots: tuple[Path, Path]

    def register_stopped_target(self, root: Path, *, replace: bool = False) -> None:
        """仅在 Gateway 停止时准备登记输入；不为测试生成任何查询结果或领域历史。"""
        gateway_root = self.gateway_home / "state/gateway"
        state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
        registry = GatewayWorkspaceRegistry(gateway_id=self.gateway_id, storage_path=gateway_root / "workspaces.json", state_store=state)
        try:
            if replace:
                previous_id = workspace_backend_id_for_root(self.stopped_roots[0])
                owner = registry.backend_owner((self.gateway_id, previous_id))
                assert owner is not None
                registry.discard_stopped_backend_owner(owner)
                registry.remove((self.gateway_id, previous_id))
            workspace_id = workspace_backend_id_for_root(root)
            registry.upsert(WorkspaceTarget(
                gateway_id=self.gateway_id, workspace_id=workspace_id, name="Stopped workspace", root_path=str(root), backend_url="",
                connection_kind="local",
                managed=True, desired_running=False,
            ), activate=False)
            registry.register_backend_owner(ManagedBackendOwner(ManagedBackendContract(
                gateway_id=self.gateway_id, owner_id=f"scope_backend_{workspace_id}", generation=f"scope_generation_{workspace_id}",
                members=(ManagedBackendMember(workspace_id),), backend_url="",
            )))
        finally:
            registry.close()
            state.close()

    def start_gateway(self) -> GatewayProcess:
        return start_gateway_process(
            workspace_root=self.workspace, default_backend_url=f"http://127.0.0.1:{self.backend_port}",
            port=self.gateway_port, boxteam_home=self.gateway_home, artifacts_dir=self.artifacts,
        )


@pytest.fixture
def network(request: pytest.FixtureRequest, integration_workspace_root_path: str) -> Iterator[ScopeNetwork]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    ports = integration_port_block_for_file(Path(request.node.path))
    lease = E2EProcessLease(runtime / "service-ports.json", (ports.port(0), ports.port(20)))
    lease.acquire()
    try:
        workspace = Path(integration_workspace_root_path)
        stopped_roots = (workspace / "stopped-b", workspace / "stopped-c")
        for root in stopped_roots:
            prepare_default_test_workspace(
                workspace_root=root, template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
                fresh_identity=True,
            )
        gateway_home = runtime / "boxteam-home"
        gateway_id = load_or_create_gateway_id(gateway_home / "state/gateway/identity.json")
        fixture = ScopeNetwork(workspace, gateway_home, gateway_id, artifacts, ports.port(20), ports.port(0), stopped_roots)
        fixture.register_stopped_target(stopped_roots[0])
        backend = start_backend_process(
            workspace_root=str(workspace), port=fixture.backend_port, log_name="scope-backend",
            boxteam_home=runtime / "backend-home", artifacts_dir=artifacts,
        )
        try:
            yield fixture
        finally:
            close_backend_process(backend)
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_frozen_all_survives_gateway_restart_and_same_name_workspace_replacement(network: ScopeNetwork) -> None:
    gateway = await asyncio.to_thread(network.start_gateway)
    observations = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=20, trust_env=False,
        ) as client:
            async def resolve(payload: dict, request_id: str) -> dict:
                response = await client.post("/api/gateway/search/resolve", json=payload, headers={"X-Request-ID": request_id})
                observations.append({"request": payload, "status": response.status_code, "body": response.json()})
                assert response.status_code == 200, response.text
                assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
                return response.json()["data"]

            first = await resolve({"mode": "all"}, "req_scope_first")
            assert len(first["scope"]["owners"]) == 2
            assert all(branch["state"] == "registered" for branch in first["branches"])
            stopped_owner = {"gateway_id": network.gateway_id, "workspace_id": workspace_backend_id_for_root(network.stopped_roots[0])}
            assert next(branch for branch in first["branches"] if branch["owner"] == stopped_owner)["state"] == "registered"
            before = {path.relative_to(network.stopped_roots[0]).as_posix(): path.read_bytes() for path in network.stopped_roots[0].rglob("*") if path.is_file()}
            selected = await resolve({"mode": "origin", "origin": stopped_owner}, "req_scope_selected")
            assert selected["scope"]["owners"] == [stopped_owner]
            assert before == {path.relative_to(network.stopped_roots[0]).as_posix(): path.read_bytes() for path in network.stopped_roots[0].rglob("*") if path.is_file()}

            await asyncio.to_thread(close_gateway_process, gateway)
            gateway = None
            network.register_stopped_target(network.stopped_roots[1], replace=True)
            gateway = await asyncio.to_thread(network.start_gateway)
            resumed = await resolve({"mode": "resume", "scope": first["scope"]}, "req_scope_resume")
            assert resumed["scope"] == first["scope"]
            old_branch = next(branch for branch in resumed["branches"] if branch["owner"] == stopped_owner)
            assert old_branch["state"] == "unavailable" and old_branch["code"] == "owner_unavailable"
            fresh = await resolve({"mode": "all"}, "req_scope_fresh")
            assert len(fresh["scope"]["owners"]) == 2 and stopped_owner not in fresh["scope"]["owners"]
            current_owner = {"gateway_id": network.gateway_id, "workspace_id": workspace_backend_id_for_root(network.stopped_roots[1])}
            assert next(branch for branch in fresh["branches"] if branch["owner"] == current_owner)["state"] == "registered"
            assert current_owner != stopped_owner
            assert not (network.stopped_roots[0] / ".boxteam/sessions").exists()
            assert not (network.stopped_roots[1] / ".boxteam/sessions").exists()
    finally:
        (network.artifacts / "scope-network.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
