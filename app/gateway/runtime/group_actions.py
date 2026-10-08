"""唯一 Backend owner 的完整成员生命周期动作。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from app.core.mounted_workspaces import MountedWorkspace
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.local_workspace import (
    rebuild_managed_backend_runtime,
    start_managed_backend_runtime,
    verify_managed_backend_group,
)
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.process import resolve_backend_workspace_id
from app.schemas.gateway.lifecycle import (
    GatewayRuntimeBlockerDTO,
    GatewayRuntimeGroupRequest,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
)
from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO


class ManagedBackendGroupActions:
    """由 controller 共享同一注册表、启动参数和配置 owner 调用。"""

    _registry: GatewayWorkspaceRegistry
    _project_root: Path
    _log_dir: Path

    def _validate_group_request(
        self, owner: ManagedBackendOwner, request: GatewayRuntimeGroupRequest | None,
    ) -> bool:
        contract = owner.contract
        if request is None or request.expected_backend_generation is None:
            if request is not None and request.desired_workspace_ids is not None:
                raise ValueError("成员变更必须提交完整 generation/成员前置条件")
            return len(contract.members) == 1
        if request.expected_backend_generation != contract.generation:
            raise ValueError("managed Backend generation 已陈旧；重新获取完整组状态")
        if set(request.expected_workspace_ids or ()) != set(contract.workspace_ids):
            raise ValueError("expected_workspace_ids 必须精确匹配 Backend owner 完整成员")
        return True

    async def _managed_group_action(
        self, workspace_id: str, action: Literal["start", "stop", "restart-safe", "restart-force"],
        *, request_id: str, group_request: GatewayRuntimeGroupRequest | None,
        remove_target: bool = False,
    ) -> GatewayRuntimeStateResultDTO | GatewayRuntimeRestartResultDTO:
        target = self._registry.resolve(workspace_id)
        if remove_target and target.connection_kind != "local":
            raise ValueError("成员受控删除只能操作当前 Gateway 的本地托管目标")
        if target.connection_kind == "remote_gateway":
            if action in {"start", "stop"}:
                return await self._delegated_runtime_state(target, action, request_id=request_id, group_request=group_request)
            return await self._delegated_restart(target, request_id=request_id, forced=action == "restart-force", group_request=group_request)
        if target.connection_kind != "local" or not target.managed:
            raise ValueError("只有 Gateway 托管本地工作区可以操作 Backend")
        # 拓扑操作先串行，随后在唯一 owner 锁内重核合同；成员不能各拿 shard 锁。
        async with self._registry.backend_topology_lock:
            target = self._registry.resolve(workspace_id)
            if remove_target:
                self._registry.validate_target_removal(workspace_id, owner="manual_crud")
            owner = self._registry.backend_owner(workspace_id)
            if owner is None and group_request is not None:
                raise ValueError("Backend owner 已释放；重新获取工作区状态")
            if owner is None and remove_target:
                if self._registry.has_runtime(workspace_id):
                    raise RuntimeError("managed 成员运行时缺少唯一 Backend owner")
                self._registry.remove(workspace_id, owner="manual_crud")
                return await self._group_result(workspace_id, "stopped", None, [workspace_id], restart=True, forced=action == "restart-force")
            if owner is None:
                backend_id = await asyncio.to_thread(resolve_backend_workspace_id, self._project_root, Path(target.root_path))
                if target.backend_workspace_id != backend_id:
                    raise RuntimeError("Gateway target 与根的 Backend UUID 不匹配")
                mount = MountedWorkspace(
                    backend_id, Path(target.root_path).resolve(),
                    target.local_service_urls.get("terminal_manager"), target.local_service_urls.get("browser_manager"),
                )
                owner = ManagedBackendOwner(ManagedBackendContract(
                    f"backend_owner_{uuid4().hex}", f"backend_generation_{uuid4().hex}",
                    (ManagedBackendMember(workspace_id, mount),), target.backend_url,
                ))
                self._registry.register_backend_owner(owner)
            async with owner.lock:
                allowed = self._validate_group_request(owner, group_request)
                if remove_target and action == "restart-force" and (
                    group_request is None or group_request.expected_backend_generation is None
                ):
                    allowed = False
                affected = list(owner.contract.workspace_ids)
                restart = action.startswith("restart")
                forced = action == "restart-force"
                if not allowed:
                    blockers = [GatewayRuntimeBlockerDTO(
                        workspace_id=workspace_id, kind="backend_group", resource_id=owner.contract.owner_id,
                        session_id="gateway-runtime", status="confirmation_required",
                        detail="共享 Backend 需要确认完整成员与 generation",
                    )]
                    return await self._group_result(workspace_id, "blocked", owner, affected, restart=restart, forced=forced, blockers=blockers)
                desired = group_request.desired_workspace_ids if group_request is not None else None
                if desired is not None and not restart:
                    raise ValueError("desired_workspace_ids 只用于完整组受控重启")
                desired_ids = tuple(desired) if desired is not None else owner.contract.workspace_ids
                if remove_target:
                    remaining_ids = tuple(member_id for member_id in owner.contract.workspace_ids if member_id != workspace_id)
                    if desired is not None and set(desired) != set(remaining_ids):
                        raise ValueError("删除成员的 desired_workspace_ids 必须精确等于其余完整成员")
                    desired_ids = remaining_ids
                members = []
                previous_members = {member.workspace_id: member for member in owner.contract.members}
                for member_id in desired_ids:
                    member_target = self._registry.resolve(member_id)
                    if member_target.connection_kind != "local" or not member_target.managed:
                        raise ValueError("加入项必须是已登记 local managed target")
                    other = self._registry.backend_owner(member_id)
                    if other is not None and other is not owner and (
                        other.process is not None or other.unpublished_runtimes or self._registry.has_runtime(member_id)
                        or other.lock.locked() or len(other.contract.members) != 1
                    ):
                        raise ValueError("加入项属于另一 owner；活动 singleton 必须先自身 stop")
                    if member_id in previous_members:
                        members.append(previous_members[member_id])
                    else:
                        if self._registry.has_runtime(member_id):
                            raise ValueError("加入项仍持有成员辅助运行时；先完成原组受控清理")
                        actual_id = await asyncio.to_thread(resolve_backend_workspace_id, self._project_root, Path(member_target.root_path))
                        if member_target.backend_workspace_id != actual_id:
                            raise RuntimeError("加入项 Backend UUID 与登记身份不匹配")
                        members.append(ManagedBackendMember(member_id, MountedWorkspace(
                            actual_id, Path(member_target.root_path).resolve(),
                            member_target.local_service_urls.get("terminal_manager"), member_target.local_service_urls.get("browser_manager"),
                        )))
                if members:
                    # 完整身份集合在排空前校验；空目标只表达退出，不构造空运行合同。
                    replace(owner.contract, members=tuple(members))
                for member in owner.contract.members:
                    member_id = member.workspace_id
                    member_target = self._registry.resolve(member_id)
                    if member_target.backend_workspace_id != member.mount.workspace_id or member_target.root_path != str(member.mount.workspace_root):
                        raise RuntimeError("当前成员 target 与 owner UUID/根合同不匹配")
                    if (action == "stop" or member_id not in desired_ids) and (
                        member_target.system_default or not member_target.removable
                    ):
                        raise PermissionError(f"默认工作区不能关闭或移出: {member_target.name}")
                affected = sorted(set(owner.contract.workspace_ids) | set(desired_ids))
                if action == "start":
                    if owner.process is not None and owner.process.is_alive() and owner.error is None:
                        raise ValueError("Backend 完整组已经启动")
                    old_contract = owner.contract
                    previous_runtimes = {
                        member_id: self._registry.managed_runtime(member_id)
                        for member_id in old_contract.workspace_ids if self._registry.has_runtime(member_id)
                    }
                    contract = None
                    try:
                        owner.close(timeout_seconds=self._connection_drain_timeout_seconds)
                        contract, runtimes = await start_managed_backend_runtime(
                            owner=owner, project_root=self._project_root, log_dir=self._log_dir,
                            reusable_runtimes=previous_runtimes, **self._startup_options(),
                        )
                        self._registry.publish_backend_runtime(owner, contract, runtimes)
                    except BaseException as start_error:
                        if contract is not None and owner.contract is contract:
                            # 发布已提交时只报告观察者错误，不能关闭已公布的完整组。
                            raise
                        try:
                            owner.close(timeout_seconds=self._connection_drain_timeout_seconds)
                        except BaseException as cleanup_error:  # noqa: BLE001 — 未关闭句柄留在唯一 owner 供重试。
                            start_error.add_note(f"启动发布失败清理错误: {cleanup_error}")
                        self._registry.mark_backend_error(owner, str(start_error))
                        raise
                    return await self._group_result(workspace_id, "started", owner, affected)
                if owner.process is None:
                    if desired is None and not remove_target:
                        raise ValueError("Backend 完整组尚未启动；使用完整组 start 恢复")
                    blockers = [blocker for member_id in affected for blocker in self._route_reference_blockers(member_id)]
                    if blockers and not forced:
                        return await self._group_result(workspace_id, "blocked", owner, affected, restart=True, blockers=blockers)
                    old_contract = owner.contract
                    changed = set(desired_ids) != set(old_contract.workspace_ids)
                    candidate_contract = replace(
                        old_contract, members=tuple(members) if changed else old_contract.members,
                        generation=f"backend_generation_{uuid4().hex}" if changed else old_contract.generation,
                    ) if desired_ids else None
                    try:
                        if forced:
                            await self._invalidate_and_drain_group_routes(affected)
                        owner.close(timeout_seconds=self._connection_drain_timeout_seconds)
                        self._registry.publish_backend_runtime(
                            owner, candidate_contract, None if desired_ids else {},
                            removed_target_id=workspace_id if remove_target else None,
                        )
                    except BaseException as exit_error:
                        if owner in self._registry.backend_owners() and owner.contract is not candidate_contract:
                            try:
                                self._registry.mark_backend_error(owner, str(exit_error))
                            except BaseException as record_error:  # noqa: BLE001 — 记录失败不能掩盖退出/CAS 的原异常。
                                exit_error.add_note(f"记录停机成员退出失败状态出错: {record_error}")
                        raise
                    return await self._group_result(
                        workspace_id, "stopped", owner, affected, restart=True, forced=forced, blockers=blockers if forced else None,
                    )
                startup_contract = await self._pending_startup_contract(target, request_id=request_id) if restart and workspace_id in desired_ids and not remove_target else None
                targets = [self._registry.resolve(member_id) for member_id in owner.contract.workspace_ids]
                drained: list[WorkspaceTarget] = []
                candidate_contract = None
                try:
                    for member_target in targets:
                        await self._runtime_action(member_target, "/api/v1/runtime/drain", request_id=request_id)
                        drained.append(member_target)
                    deadline = asyncio.get_running_loop().time() + self._drain_timeout_seconds
                    while True:
                        blockers = []
                        for member_target in targets:
                            status = await self._runtime_status(member_target, request_id=request_id)
                            blockers.extend(self._parse_blockers(status.get("blockers"), member_target.workspace_id))
                        for member_id in affected:
                            blockers.extend(self._route_reference_blockers(member_id))
                        if not blockers or forced:
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            await self._cancel_group_drain(drained, request_id=request_id)
                            return await self._group_result(workspace_id, "blocked", owner, affected, restart=restart, forced=forced, blockers=blockers)
                        await asyncio.sleep(self._drain_poll_interval_seconds)
                    if forced:
                        # 所有 mount 的 interrupted durable 提交成功之后，才能关闭唯一 Backend。
                        for member_target in targets:
                            await self._runtime_action(member_target, "/api/v1/runtime/drain/force", request_id=request_id)
                        await self._invalidate_and_drain_group_routes(affected)
                    if action == "stop":
                        self._registry.stop_backend_owner(owner)
                        return await self._group_result(workspace_id, "stopped", owner, affected)
                    old_contract = owner.contract
                    old_runtimes = {member_id: self._registry.managed_runtime(member_id) for member_id in old_contract.workspace_ids}
                    try:
                        if not desired_ids:
                            owner.close(timeout_seconds=self._connection_drain_timeout_seconds)
                            self._registry.publish_backend_runtime(
                                owner, None, {}, removed_target_id=workspace_id if remove_target else None,
                            )
                            return await self._group_result(workspace_id, "stopped", owner, affected, restart=True, forced=forced, blockers=blockers if forced else None)
                        candidate_contract, candidate_runtimes = await rebuild_managed_backend_runtime(
                            owner=owner, desired_members=tuple(members), runtimes=old_runtimes,
                            project_root=self._project_root, log_dir=self._log_dir,
                            startup_contracts={workspace_id: startup_contract} if startup_contract is not None else None,
                            **self._startup_options(),
                        )
                        if startup_contract is not None:
                            candidate_target = replace(target, backend_url=candidate_contract.backend_url)
                            await self._resolve_pending_restart(
                                candidate_target, candidate_ref=str(startup_contract["candidate_ref"]), request_id=request_id,
                            )
                        self._registry.publish_backend_runtime(
                            owner, candidate_contract, candidate_runtimes,
                            removed_target_id=workspace_id if remove_target else None,
                        )
                    except BaseException as publish_error:
                        if owner not in self._registry.backend_owners() or candidate_contract is not None and owner.contract is candidate_contract:
                            # 已提交后的观察者错误仍明确抛出，权威合同不能倒退。
                            raise
                        # 候选未公布时恢复原完整拓扑；配置提交结果由配置 owner 保持权威。
                        try:
                            owner.close(timeout_seconds=self._connection_drain_timeout_seconds)
                            restored_contract, restored = await start_managed_backend_runtime(
                                owner=owner, mount_contract=old_contract, reusable_runtimes=old_runtimes,
                                preferred_backend_port=urlparse(old_contract.backend_url).port,
                                preserve_generation=True, project_root=self._project_root,
                                log_dir=self._log_dir, **self._startup_options(),
                            )
                            self._registry.publish_backend_runtime(owner, restored_contract, restored)
                        except BaseException as recovery_error:  # noqa: BLE001 — 原候选失败仍为主异常。
                            self._registry.mark_backend_error(owner, f"Backend proof/CAS 失败且恢复失败: {recovery_error}")
                            publish_error.add_note(str(recovery_error))
                        raise
                    return await self._group_result(workspace_id, "restarted", owner, affected, restart=True, forced=forced, blockers=blockers if forced else None)
                except BaseException as error:
                    if owner not in self._registry.backend_owners() or candidate_contract is not None and owner.contract is candidate_contract:
                        raise
                    recovered = owner.process is not None and owner.error is None
                    if recovered:
                        try:
                            await verify_managed_backend_group(owner.contract)
                            await self._cancel_group_drain(drained, request_id=request_id)
                        except Exception as recovery_error:  # noqa: BLE001 — 全组恢复证据失败必须 fail-closed。
                            recovered = False
                            error.add_note(f"完整组恢复验证失败: {recovery_error}")
                    if not recovered:
                        self._registry.mark_backend_error(owner, str(error))
                    if startup_contract is not None:
                        try:
                            await self._record_pending_restart_failure(
                                target, candidate_ref=str(startup_contract["candidate_ref"]), error=str(error),
                                old_runtime_recovered=recovered, request_id=request_id,
                            )
                        except Exception as record_error:  # noqa: BLE001 — 失败记录不能覆盖主异常。
                            error.add_note(f"配置失败记录失败: {record_error}")
                    raise

    def _startup_options(self) -> dict[str, object]:
        return {
            "health_request_timeout_seconds": self._health_request_timeout_seconds,
            "health_poll_interval_seconds": self._health_poll_interval_seconds,
            "connection_drain_timeout_seconds": self._connection_drain_timeout_seconds,
            "default_skill_groups": self._default_skill_groups,
        }

    async def _invalidate_and_drain_group_routes(self, affected: list[str]) -> None:
        for member_id in affected:
            self._registry.invalidate_route(member_id)
        deadline = asyncio.get_running_loop().time() + self._connection_drain_timeout_seconds
        while blockers := [blocker for member_id in affected for blocker in self._route_reference_blockers(member_id)]:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("强制重启旧路由引用未释放: " + "; ".join(
                    f"{blocker.workspace_id}: {blocker.detail}" for blocker in blockers
                ))
            await asyncio.sleep(self._drain_poll_interval_seconds)

    async def _cancel_group_drain(self, targets: list[WorkspaceTarget], *, request_id: str) -> None:
        errors = []
        for target in targets:
            try:
                status = await self._runtime_status(target, request_id=request_id)
                if status.get("lifecycle_state") == "draining":
                    await self._runtime_action(target, "/api/v1/runtime/drain/cancel", request_id=request_id)
                elif status.get("lifecycle_state") != "ready" or status.get("accepting_jobs") is not True:
                    raise RuntimeError(f"成员未恢复 Job 准入: {status}")
            except Exception as error:  # noqa: BLE001 — 所有成员都尝试取消 drain，失败聚合暴露。
                errors.append(f"{target.workspace_id}: {error}")
        if errors:
            raise RuntimeError("完整组撤销 drain 失败: " + "; ".join(errors))

    async def _group_result(
        self, workspace_id: str, status: str, owner: ManagedBackendOwner | None, affected: list[str],
        *, restart: bool = False, forced: bool = False,
        blockers: list[GatewayRuntimeBlockerDTO] | None = None,
    ) -> GatewayRuntimeStateResultDTO | GatewayRuntimeRestartResultDTO:
        data = {
            "workspace_id": workspace_id, "status": status, "backend_generation": owner.contract.generation if owner is not None else None,
            "affected_workspace_ids": affected, "blockers": blockers or [],
            "workspaces": GatewayWorkspaceListDTO(active_workspace_id=self._registry.active_workspace_id, items=await self._registry.list_dtos()),
        }
        return GatewayRuntimeRestartResultDTO.model_validate({**data, "forced": forced}) if restart else GatewayRuntimeStateResultDTO.model_validate(data)
