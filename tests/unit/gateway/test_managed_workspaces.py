from dataclasses import replace
from pathlib import Path
from typing import Self

import httpx
import pytest

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import request_remote_gateway_management
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    remove_direct_managed_workspace,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.support.gateway_managed_backends import ProcessProbe


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R19 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例不经外部网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/
    ``[::1]``）会让用例内（或被测 gateway 代码内）构造的 httpx 客户端在
    代理解析时直接抛 ``InvalidURL: Invalid port: ':1]'``——属外部环境噪
    声混入测试。与 R18 对 gateway 会话上下文客户端的同类修复一致（任务
    书许可的 monkeypatch 清 ``*_proxy`` 做法）；清掉后客户端不取代理，
    与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def controlled_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(registry: GatewayWorkspaceRegistry) -> GatewayWorkspaceRuntimeController:
        controller = GatewayWorkspaceRuntimeController(
            registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs",
            drain_timeout_seconds=0,
        )
        async def action(*_, **__):
            return {}
        async def status(*_, **__):
            return {"lifecycle_state": "ready", "accepting_jobs": True, "blockers": []}
        monkeypatch.setattr(controller, "_runtime_action", action)
        monkeypatch.setattr(controller, "_runtime_status", status)
        return controller
    return build


@pytest.mark.asyncio
async def test_create_and_remove_direct_managed_workspace_preserves_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_controller,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    workspace_root = tmp_path / "created" / "workspace"
    monkeypatch.setattr(
        "app.gateway.managed_workspaces.resolve_backend_workspace_id",
        lambda *_, **__: "00000000-0000-4000-8000-000000000001",
    )

    backend = ProcessProbe("backend")
    async def start_runtime(*, owner, **_):
        owner.set_process(backend)
        member = owner.contract.members[0]
        contract = replace(owner.contract, backend_url="http://127.0.0.1:42000")
        runtime = WorkspaceRuntime(
            service_urls={"terminal_manager": "http://127.0.0.1:42001", "browser_manager": "http://127.0.0.1:42002"},
            backend_owner=owner, backend_workspace_id=member.backend_workspace_id,
        )
        owner.unpublished_runtimes[member.workspace_id] = runtime
        return contract, {member.workspace_id: runtime}
    async def listed():
        return []
    monkeypatch.setattr("app.gateway.runtime.group_actions.resolve_backend_workspace_id", lambda *_, **__: "00000000-0000-4000-8000-000000000001")
    async def bindings_valid(*_):
        return None
    monkeypatch.setattr("app.gateway.runtime.group_actions.validate_managed_backend_bindings", bindings_valid)
    monkeypatch.setattr("app.gateway.runtime.group_actions.start_managed_backend_runtime", start_runtime)
    monkeypatch.setattr(registry, "list_dtos", listed)

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
    assert target.backend_url == "http://127.0.0.1:42000"
    assert registry.has_runtime(target.workspace_id) is True
    owner = registry.backend_owner(target.workspace_id)
    assert owner is not None and owner.process is backend
    assert owner.contract.workspace_ids == (target.workspace_id,)
    assert registry.resolve(target.workspace_id).root_path == str(workspace_root)

    registry.upsert(
        WorkspaceTarget(
            workspace_id="kept",
            name="Kept",
            root_path=str(tmp_path / "kept"),
            backend_url="http://127.0.0.1:41235",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
        ),
        activate=False,
    )

    result = await remove_direct_managed_workspace(
        registry, target.workspace_id, controller=controlled_controller(registry), request_id="remove",
    )
    assert result.status == "stopped"

    assert workspace_root.is_dir()
    assert backend.closed
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


@pytest.mark.asyncio
async def test_remove_direct_managed_workspace_rejects_default_and_external_targets(
    tmp_path: Path, controlled_controller,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="default",
            name="Default",
            root_path=str(tmp_path / "default"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=False,
        ),
        activate=False,
    )

    with pytest.raises(PermissionError, match="默认工作区"):
        await remove_direct_managed_workspace(registry, "default", controller=controlled_controller(registry), request_id="default")
    with pytest.raises(ValueError, match="直接托管"):
        await remove_direct_managed_workspace(registry, "external", controller=controlled_controller(registry), request_id="external")


@pytest.mark.asyncio
async def test_remove_direct_managed_workspace_keeps_last_target(tmp_path: Path, controlled_controller) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="only",
            name="Only",
            root_path=str(tmp_path / "only"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
        )
    )

    with pytest.raises(ValueError, match="至少需要保留"):
        await remove_direct_managed_workspace(registry, "only", controller=controlled_controller(registry), request_id="only")


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

        async def __aenter__(self) -> Self:
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
