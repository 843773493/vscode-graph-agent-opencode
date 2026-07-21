from pathlib import Path

import httpx
import pytest

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import request_remote_gateway_management
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    remove_direct_managed_workspace,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime


@pytest.mark.asyncio
async def test_create_and_remove_direct_managed_workspace_preserves_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    workspace_root = tmp_path / "created" / "workspace"

    async def start_runtime(**_: object) -> WorkspaceRuntime:
        return WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41234"}
        )

    monkeypatch.setattr(
        "app.gateway.managed_workspaces.start_managed_local_workspace_runtime",
        start_runtime,
    )

    target = await create_direct_managed_workspace(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        root_path=str(workspace_root),
        name="Remote project",
        create_directory=True,
    )

    assert workspace_root.is_dir()
    assert target.name == "Remote project"
    assert target.managed is True
    assert registry.resolve(target.workspace_id).root_path == str(workspace_root)

    registry.upsert(
        WorkspaceTarget(
            workspace_id="kept",
            name="Kept",
            root_path=str(tmp_path / "kept"),
            backend_url="http://127.0.0.1:41235",
            connection_kind="local",
            managed=True,
        ),
        activate=False,
    )

    remove_direct_managed_workspace(registry, target.workspace_id)

    assert workspace_root.is_dir()
    with pytest.raises(LookupError, match="不存在"):
        registry.resolve(target.workspace_id)


@pytest.mark.asyncio
async def test_create_direct_managed_workspace_requires_existing_directory_when_disabled(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")

    with pytest.raises(FileNotFoundError, match="目录不存在"):
        await create_direct_managed_workspace(
            registry=registry,
            project_root=tmp_path,
            log_dir=tmp_path / "logs",
            root_path=str(tmp_path / "missing"),
            name=None,
            create_directory=False,
        )

    with pytest.raises(ValueError, match="绝对路径"):
        await create_direct_managed_workspace(
            registry=registry,
            project_root=tmp_path,
            log_dir=tmp_path / "logs",
            root_path="relative/workspace",
            name=None,
            create_directory=True,
        )


def test_remove_direct_managed_workspace_rejects_default_and_external_targets(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="default",
            name="Default",
            root_path=str(tmp_path / "default"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
            removable=False,
            system_default=True,
        )
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id="external",
            name="External",
            root_path=str(tmp_path / "external"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
            managed=False,
        ),
        activate=False,
    )

    with pytest.raises(PermissionError, match="默认工作区"):
        remove_direct_managed_workspace(registry, "default")
    with pytest.raises(ValueError, match="直接托管"):
        remove_direct_managed_workspace(registry, "external")


def test_remove_direct_managed_workspace_keeps_last_target(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="only",
            name="Only",
            root_path=str(tmp_path / "only"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
        )
    )

    with pytest.raises(ValueError, match="至少需要保留"):
        remove_direct_managed_workspace(registry, "only")


@pytest.mark.asyncio
async def test_remote_management_request_forwards_federation_and_request_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FederationCredentialStore(
        storage_path=tmp_path / "federation.json"
    ).issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_remote",
    )

    class ManagementClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 45

        async def __aenter__(self) -> "ManagementClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object] | None,
        ) -> httpx.Response:
            assert method == "POST"
            assert url == (
                "http://127.0.0.1:41000/api/gateway/"
                "federation/managed-workspaces"
            )
            assert headers == {
                "X-BoxTeam-Federation-Token": credential.token,
                "X-Request-ID": "request-test",
            }
            assert json == {"root_path": "/srv/project"}
            return httpx.Response(
                200,
                json={"data": {"items": []}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr("app.gateway.federation.httpx.AsyncClient", ManagementClient)

    result = await request_remote_gateway_management(
        gateway_url="http://127.0.0.1:41000/",
        credential=credential,
        method="POST",
        path="/api/gateway/federation/managed-workspaces",
        request_id="request-test",
        payload={"root_path": "/srv/project"},
    )

    assert result == {"items": []}
