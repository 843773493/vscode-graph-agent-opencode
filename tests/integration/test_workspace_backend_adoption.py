from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.backend_identity import load_or_create_backend_id
from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.core.path_utils import get_boxteam_home
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.local_workspace import (
    start_managed_backend_runtime,
    verify_managed_backend_group,
)
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.workspace_process import start_local_backend_process
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS

GATEWAY_ID = "gateway_" + "a" * 32


@dataclass(frozen=True, slots=True)
class _Backend:
    process: ManagedProcess
    workspace_root: Path
    workspace_id: str
    port: int
    generation: str
    manifest_digest: str


@pytest.fixture
async def adopted_backend(
    integration_workspace_root_path: str,
    integration_backend_port: int,
    tmp_path: Path,
) -> AsyncIterator[_Backend]:
    workspace_root = Path(integration_workspace_root_path)
    workspace_id = load_or_create_workspace_id(workspace_root)
    load_or_create_backend_id(get_boxteam_home())
    generation = "backend_generation_adoption"
    digest = mounted_workspace_manifest_digest((MountedWorkspace(workspace_id, workspace_root),))
    lease = E2EProcessLease(tmp_path / "backend-port.json", (integration_backend_port,))
    lease.acquire()
    try:
        # 使用正式启动、健康与日志证明，不再手写一份不完整的 Backend HTTP 协议。
        process = start_local_backend_process(
            project_root=Path.cwd(), mounts=(MountedWorkspace(workspace_id, workspace_root),),
            backend_generation=generation, port=integration_backend_port, log_dir=tmp_path / "logs",
        )
        try:
            await verify_managed_backend_group(
                ManagedBackendContract(
                    gateway_id=GATEWAY_ID, owner_id="backend_owner_adoption", generation=generation,
                    members=(ManagedBackendMember(workspace_id),),
                    backend_url=f"http://127.0.0.1:{integration_backend_port}",
                ),
                mounts=(MountedWorkspace(workspace_id, workspace_root),), process=process,
            )
            yield _Backend(process, workspace_root, workspace_id, integration_backend_port, generation, digest)
        finally:
            await asyncio.to_thread(process.close)
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_preferred_port_mismatch_rejects_without_stopping_adopted_backend(
    adopted_backend: _Backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = adopted_backend
    preferred_backend_port = (
        backend.port + 1 if backend.port < 65535 else backend.port - 1
    )

    def fail_if_started(**_: object) -> None:
        pytest.fail("接管端口不匹配时不应启动候选 Workspace API")

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        fail_if_started,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_browser_service_process",
        fail_if_started,
    )

    owner = ManagedBackendOwner(ManagedBackendContract(
        gateway_id=GATEWAY_ID, owner_id="backend_owner_adoption", generation=backend.generation,
        members=(ManagedBackendMember(backend.workspace_id),),
        backend_url=f"http://127.0.0.1:{backend.port}",
    ))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json", gateway_id=GATEWAY_ID)
    registry.upsert(WorkspaceTarget(
        gateway_id=GATEWAY_ID, workspace_id=backend.workspace_id, name="Adopted", root_path=str(backend.workspace_root),
        backend_url=owner.contract.backend_url, connection_kind="local", managed=True,
    ))
    registry.register_backend_owner(owner)
    with pytest.raises(RuntimeError, match="端口与首选端口不匹配") as raised:
        await start_managed_backend_runtime(
            owner=owner, registry=registry, project_root=Path.cwd(), log_dir=tmp_path / "logs",
            preferred_backend_port=preferred_backend_port, adopt_existing_backend=True,
        )

    assert f"actual={backend.port}" in str(raised.value)
    assert f"preferred={preferred_backend_port}" in str(raised.value)
    assert backend.process.is_alive()
    async with httpx.AsyncClient(timeout=2, headers=LOCAL_TOKEN_HEADERS) as client:
        response = await client.get(
            f"http://127.0.0.1:{backend.port}/api/v1/health",
            headers={"X-BoxTeam-Workspace-Id": backend.workspace_id},
        )
    assert response.status_code == 200
    assert response.json()["workspace_id"] == backend.workspace_id
    assert response.json()["process_id"] == backend.process.pid
