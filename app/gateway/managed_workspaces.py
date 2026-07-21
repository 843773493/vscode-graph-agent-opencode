from __future__ import annotations

from pathlib import Path

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.local_workspace import start_managed_local_workspace_runtime
from app.gateway.schemas import GatewayManagedWorkspaceDTO
from app.gateway.workspace_ids import build_managed_local_workspace_id


async def list_direct_managed_workspaces(
    registry: GatewayWorkspaceRegistry,
) -> list[GatewayManagedWorkspaceDTO]:
    managed_workspaces = [
        workspace
        for workspace in await registry.list_dtos()
        if workspace.connection_kind == "local" and workspace.managed
    ]
    return [
        GatewayManagedWorkspaceDTO(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            root_path=workspace.root_path,
            status=workspace.status,
            removable=(
                workspace.removable
                and not workspace.system_default
                and len(managed_workspaces) > 1
            ),
            system_default=workspace.system_default,
        )
        for workspace in managed_workspaces
    ]


async def create_direct_managed_workspace(
    *,
    registry: GatewayWorkspaceRegistry,
    project_root: Path,
    log_dir: Path,
    root_path: str,
    name: str | None,
    create_directory: bool,
) -> WorkspaceTarget:
    requested_root = Path(root_path).expanduser()
    if not requested_root.is_absolute():
        raise ValueError(f"工作区路径必须是绝对路径: {root_path}")
    workspace_root = requested_root.resolve()
    if workspace_root.exists() and not workspace_root.is_dir():
        raise ValueError(f"工作区路径不是目录: {workspace_root}")
    if not workspace_root.exists():
        if not create_directory:
            raise FileNotFoundError(f"工作区目录不存在: {workspace_root}")
        workspace_root.mkdir(parents=True)

    workspace_id = build_managed_local_workspace_id(str(workspace_root))
    if any(target.workspace_id == workspace_id for target in registry.targets()):
        raise ValueError(f"工作区已经由当前 Gateway 托管: {workspace_root}")
    runtime = await start_managed_local_workspace_runtime(
        project_root=project_root,
        workspace_root=workspace_root,
        log_dir=log_dir,
    )
    target = WorkspaceTarget(
        workspace_id=workspace_id,
        name=name or workspace_root.name or "workspace",
        name_customized=bool(name),
        root_path=str(workspace_root),
        backend_url=runtime.service_urls["workspace_api"],
        connection_kind="local",
        managed=True,
    )
    registry.upsert(target, runtime=runtime, activate=False)
    return target


def remove_direct_managed_workspace(
    registry: GatewayWorkspaceRegistry,
    workspace_id: str,
) -> None:
    target = registry.resolve(workspace_id)
    if target.connection_kind != "local" or not target.managed:
        raise ValueError(f"工作区不属于当前 Gateway 的直接托管目标: {workspace_id}")
    if not target.removable or target.system_default:
        raise PermissionError(f"默认工作区不能删除: {target.name}")
    direct_managed_targets = [
        item
        for item in registry.targets()
        if item.connection_kind == "local" and item.managed
    ]
    if len(direct_managed_targets) == 1:
        raise ValueError("Gateway 至少需要保留一个直接托管工作区")
    registry.remove(workspace_id)
