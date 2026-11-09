from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.config import GatewayConfigReloadService
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceSelector,
    WorkspaceTarget,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    workspace_default_bindings,
)
from app.schemas.gateway.lifecycle import (
    GatewayRuntimeGroupRequest,
    GatewayRuntimeRestartResultDTO,
)
from app.schemas.gateway.service_plugin_workspace import WorkspacePluginCreation
from app.schemas.gateway.workspaces import GatewayManagedWorkspaceDTO


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
            gateway_id=workspace.gateway_id,
            workspace_id=workspace.workspace_id,
            name=workspace.name,
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
    request_id: str,
    config: GatewayConfigReloadService,
    plugins: GatewayPluginRuntime,
) -> WorkspaceTarget:
    requested_root = Path(root_path).expanduser()
    if not requested_root.is_absolute():
        raise ValueError("工作区路径必须是目标机器的绝对路径")
    workspace_root = requested_root.resolve()
    if workspace_root.exists() and not workspace_root.is_dir():
        raise ValueError("所选工作区资源不是目录")
    if not workspace_root.exists() and not create_directory:
        raise FileNotFoundError("所选工作区目录不存在")
    # 目录身份由原 CLI 初始化；Gateway 不读取工作区业务配置。
    new_directory = not workspace_root.exists()
    async with config.mutation_guard():
        frozen = None
        if new_directory:
            entries = workspace_default_bindings(config.config.payload)
            plugins.validate_defaults(config.config.payload)
            frozen = WorkspacePluginCreation(intent_id=f"workspace-create-{uuid4().hex}",
                revision=config.config.revision, bindings=entries)

        async def register() -> WorkspaceTarget:
            workspace_id = await asyncio.to_thread(
                registry.resolve_local_workspace_identity, project_root, workspace_root,
                create_directory=create_directory,
            )
            if registry.has_target(workspace_id):
                raise ValueError("所选工作区已经由当前 Gateway 托管")
            return registry.upsert(WorkspaceTarget(
                gateway_id=registry.gateway_id, workspace_id=workspace_id,
                name=name or workspace_root.name or "workspace", name_customized=bool(name),
                root_path=str(workspace_root), backend_url="", connection_kind="local",
                owner="manual", managed=True,
                plugin_creation=frozen.model_dump(mode="json") if frozen is not None else None,
            ), activate=False)

        # 身份 CLI 已可能创建目录；取消也须完成登记，保留冻结输入供原生命周期重试／删除。
        registration = asyncio.create_task(register())
        try:
            target = await asyncio.shield(registration)
        except asyncio.CancelledError:
            async def settle() -> None:
                await registration

            await run_cleanup_shielded(settle)
            raise
        workspace_id = target.workspace_id
    controller = GatewayWorkspaceRuntimeController(
        registry=registry, project_root=project_root, log_dir=log_dir,
    )
    await controller.start_managed_backend(workspace_id, request_id=request_id)
    return registry.resolve(workspace_id)


async def remove_direct_managed_workspace(
    registry: GatewayWorkspaceRegistry,
    workspace_id: WorkspaceSelector,
    *, controller: GatewayWorkspaceRuntimeController, request_id: str,
    group_request: GatewayRuntimeGroupRequest | None = None, force: bool = False,
) -> GatewayRuntimeRestartResultDTO:
    target = registry.resolve(workspace_id)
    if target.gateway_id != registry.gateway_id or target.connection_kind != "local" or not target.managed:
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
    return await controller.remove_managed_workspace(
        target.key, request_id=request_id, group_request=group_request, force=force,
    )
