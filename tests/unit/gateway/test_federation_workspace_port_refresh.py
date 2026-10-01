"""联邦只读工作区端口必须跟随 registry 的工作区集合 commit。

回归 F8：新增/移除本地工作区后，联邦冷 catalog 与 session main 端口必须立即
反映新的集合，而不是停留在启动时的快照。
"""

from __future__ import annotations

from pathlib import Path

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


def _service(tmp_path: Path) -> FederationRpcService:
    root = tmp_path / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return FederationRpcService(
        gateway_id="gateway_refresh",
        policy_store=FederationPolicyStore(),
        control_store=FederationControlStore(
            database=federation_control_database(gateway_root=root)
        ),
        signing_key=load_or_create_signing_key(root),
        catalog=WorkspaceCatalogPort(),
        session_main=WorkspaceSessionMainPort(),
        spoke_directory=None,
    )


def _local_target(workspace_id: str, backend_url: str, root: Path) -> WorkspaceTarget:
    return WorkspaceTarget(
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


def test_commit_observer_tracks_added_and_removed_workspaces(tmp_path: Path) -> None:
    """registry 单点 commit 后，联邦端口立即同步新增与移除。"""

    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    service = _service(tmp_path)
    registry.add_commit_observer(
        lambda: _refresh_federation_workspace_ports(service, registry)
    )

    registry.upsert(
        _local_target("gw_one", "http://127.0.0.1:41100", tmp_path / "one"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41100"}
        ),
        activate=False,
    )
    assert _catalog_ids(service) == {"gw_one": "http://127.0.0.1:41100"}
    assert _session_main_ids(service) == {"gw_one": "http://127.0.0.1:41100"}

    registry.upsert(
        _local_target("gw_two", "http://127.0.0.1:41101", tmp_path / "two"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41101"}
        ),
        activate=False,
    )
    assert sorted(_catalog_ids(service)) == ["gw_one", "gw_two"]

    registry.remove("gw_one")
    assert _catalog_ids(service) == {"gw_two": "http://127.0.0.1:41101"}
    assert _session_main_ids(service) == {"gw_two": "http://127.0.0.1:41101"}


def test_remote_projection_workspaces_are_not_projected(tmp_path: Path) -> None:
    """远端子工作区不参与本地联邦只读端口。"""

    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    service = _service(tmp_path)
    registry.add_commit_observer(
        lambda: _refresh_federation_workspace_ports(service, registry)
    )

    registry.upsert(
        _local_target("gw_local", "http://127.0.0.1:41100", tmp_path / "local"),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41100"}
        ),
        activate=False,
    )
    _refresh_federation_workspace_ports(service, registry)

    assert _catalog_ids(service) == {"gw_local": "http://127.0.0.1:41100"}
