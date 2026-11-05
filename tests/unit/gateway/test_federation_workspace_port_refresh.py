"""联邦只读工作区端口必须跟随 registry 的工作区集合 commit。

回归 F8：新增/移除本地工作区后，联邦冷 catalog 与 session main 端口必须立即
反映新的集合，而不是停留在启动时的快照。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.identity import load_or_create_signing_key
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime_proof import _refresh_federation_workspace_ports

GATEWAY_ID = "gateway_00000000000040008000000000000001"
WORKSPACE_ONE = "00000000-0000-4000-8000-000000000001"
WORKSPACE_TWO = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient(trust_env=False) as client:
        yield client


def _service(tmp_path: Path, http_client: httpx.AsyncClient) -> FederationRpcService:
    root = tmp_path / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return FederationRpcService(
        gateway_id=GATEWAY_ID,
        policy_store=FederationPolicyStore(),
        control_store=FederationControlStore(
            database=federation_control_database(gateway_root=root)
        ),
        signing_key=load_or_create_signing_key(root),
        catalog=WorkspaceCatalogPort(http_client=http_client),
        session_main=WorkspaceSessionMainPort(http_client=http_client),
        credential_store=FederationCredentialStore(
            storage_path=root / "credentials/federation.json"
        ),
        spoke_directory=None,
    )


def _local_target(workspace_id: str, backend_url: str, root: Path) -> WorkspaceTarget:
    return WorkspaceTarget(
        gateway_id=GATEWAY_ID,
        workspace_id=workspace_id,
        name=workspace_id,
        root_path=str(root),
        backend_url=backend_url,
        connection_kind="local",
    )


def _catalog_ids(service: FederationRpcService) -> dict[str, str]:
    port = service.catalog
    assert isinstance(port, WorkspaceCatalogPort)
    return dict(port._backend_urls)


def _session_main_ids(service: FederationRpcService) -> dict[str, str]:
    port = service.session_main
    assert isinstance(port, WorkspaceSessionMainPort)
    return dict(port._backend_urls)


def test_commit_observer_tracks_added_and_removed_workspaces(tmp_path: Path, http_client: httpx.AsyncClient) -> None:
    """registry 单点 commit 后，联邦端口立即同步新增与移除。"""

    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY_ID)
    service = _service(tmp_path, http_client)
    registry.add_commit_observer(
        lambda: _refresh_federation_workspace_ports(service, registry)
    )

    registry.upsert(
        _local_target(WORKSPACE_ONE, "http://127.0.0.1:41100", tmp_path / "one"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41100"}
        ),
        activate=False,
    )
    assert _catalog_ids(service) == {WORKSPACE_ONE: "http://127.0.0.1:41100"}
    assert _session_main_ids(service) == {
        WORKSPACE_ONE: "http://127.0.0.1:41100"
    }

    registry.upsert(
        _local_target(WORKSPACE_TWO, "http://127.0.0.1:41101", tmp_path / "two"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41101"}
        ),
        activate=False,
    )
    assert sorted(_catalog_ids(service)) == [WORKSPACE_ONE, WORKSPACE_TWO]

    registry.remove((GATEWAY_ID, WORKSPACE_ONE))
    assert _catalog_ids(service) == {WORKSPACE_TWO: "http://127.0.0.1:41101"}
    assert _session_main_ids(service) == {
        WORKSPACE_TWO: "http://127.0.0.1:41101"
    }


def test_remote_projection_workspaces_are_not_projected(tmp_path: Path, http_client: httpx.AsyncClient) -> None:
    """远端子工作区不参与本地联邦只读端口。"""

    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY_ID)
    service = _service(tmp_path, http_client)
    registry.add_commit_observer(
        lambda: _refresh_federation_workspace_ports(service, registry)
    )

    registry.upsert(
        _local_target(WORKSPACE_ONE, "http://127.0.0.1:41100", tmp_path / "local"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41100"}
        ),
        activate=False,
    )
    _refresh_federation_workspace_ports(service, registry)

    assert _catalog_ids(service) == {WORKSPACE_ONE: "http://127.0.0.1:41100"}
