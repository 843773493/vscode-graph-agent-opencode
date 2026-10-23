from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendOwner,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime.workspace_process import resolve_backend_workspace_ids

if TYPE_CHECKING:
    from . import GatewayWorkspaceRegistry


class RegistryRuntimeMixin:
    def backend_owner(self, workspace_id: str) -> ManagedBackendOwner | None:
        self.resolve(workspace_id)
        return next((owner for owner in self._backend_owners.values() if workspace_id in owner.contract.workspace_ids), None)

    def backend_owners(self) -> tuple[ManagedBackendOwner, ...]:
        return tuple(self._backend_owners.values())

    def local_workspace_root(self, workspace_id: str, backend_workspace_id: str) -> Path:
        """只按精确 target/Backend 身份取得唯一 registry root binding。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        target = registry.resolve(workspace_id)
        if target.connection_kind != "local" or target.backend_workspace_id != backend_workspace_id:
            raise RuntimeError(f"Gateway root binding 身份不匹配: workspace_id={workspace_id}")
        if not isinstance(target.root_path, str) or not target.root_path or not Path(target.root_path).is_absolute():
            raise RuntimeError(f"Gateway 缺少绝对 root binding: workspace_id={workspace_id}")
        return Path(target.root_path)

    def resolve_backend_mounts(self, contract: ManagedBackendContract) -> tuple[MountedWorkspace, ...]:
        """组合同只给身份；根与辅助服务地址只从当前 registry 组装成启动输入。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        mounts = []
        for member in contract.members:
            target = registry.resolve(member.workspace_id)
            if not target.managed:
                raise RuntimeError(f"Backend 成员不是托管 target: {member.workspace_id}")
            mounts.append(MountedWorkspace(
                member.backend_workspace_id,
                self.local_workspace_root(member.workspace_id, member.backend_workspace_id),
                target.local_service_urls.get("terminal_manager"),
                target.local_service_urls.get("browser_manager"),
            ))
        if len({mount.workspace_root for mount in mounts}) != len(mounts):
            raise RuntimeError("Backend 完整成员的 root binding 重复")
        return tuple(mounts)

    def validate_local_root_bindings(self, project_root: Path) -> None:
        """冷启动在任何写入/进程前只读核验全部本地 binding，包括停机成员。"""
        registry = cast("GatewayWorkspaceRegistry", self)
        managed_ids = {target.workspace_id for target in registry.targets() if target.connection_kind == "local" and target.managed}
        member_ids = {member_id for owner in self.backend_owners() for member_id in owner.contract.workspace_ids}
        if member_ids != managed_ids:
            raise RuntimeError(f"Backend 完整成员集合与 registry 不匹配: missing={sorted(managed_ids-member_ids)}, extra={sorted(member_ids-managed_ids)}")
        for owner in self.backend_owners():
            self.resolve_backend_mounts(owner.contract)
        local_root_bindings: list[tuple[str, str, Path]] = []
        for target in registry.targets():
            if target.connection_kind != "local":
                continue
            if target.backend_workspace_id is None:
                raise RuntimeError(f"本地 root binding 缺少 Backend UUID: {target.workspace_id}")
            root = self.local_workspace_root(target.workspace_id, target.backend_workspace_id)
            local_root_bindings.append(
                (target.workspace_id, target.backend_workspace_id, root)
            )

        actual_ids = resolve_backend_workspace_ids(
            project_root,
            tuple(root for _, _, root in local_root_bindings),
            initialize=False,
        )
        for (workspace_id, expected_id, _), actual_id in zip(
            local_root_bindings,
            actual_ids,
            strict=True,
        ):
            if actual_id != expected_id:
                raise RuntimeError(
                    "Gateway registry 工作区 backend UUID 与本地身份文件不一致: "
                    f"workspace_id={workspace_id}, expected={expected_id}, actual={actual_id}"
                )

    def register_backend_owner(self, owner: ManagedBackendOwner) -> None:
        """在启动任何进程前登记显式完整合同，失败句柄仍有唯一回收入口。"""
        if owner.contract.owner_id in self._backend_owners:
            raise ValueError("Backend owner 已登记")
        self.resolve_backend_mounts(owner.contract)
        for member in owner.contract.members:
            target = self.resolve(member.workspace_id)
            if self.backend_owner(member.workspace_id) is not None:
                raise ValueError("Backend 成员已属于其它 owner")
            if target.connection_kind != "local" or not target.managed or target.backend_workspace_id != member.backend_workspace_id:
                raise ValueError("Backend 完整合同与已登记 target 不匹配")
        previous_commit = getattr(self, "_last_committed_snapshot", None)
        self._backend_owners[owner.contract.owner_id] = owner
        try:
            self._save(owner="system")
        except BaseException:
            if getattr(self, "_last_committed_snapshot", None) is previous_commit:
                self._backend_owners.pop(owner.contract.owner_id, None)
            raise

    def discard_stopped_backend_owner(self, owner: ManagedBackendOwner) -> None:
        if owner.lock.locked() or self.backend_topology_lock.locked():
            raise RuntimeError("Backend 完整组生命周期操作进行中")
        if len(owner.contract.members) != 1 or owner.process is not None or owner.unpublished_runtimes or any(member_id in self._runtimes for member_id in owner.contract.workspace_ids):
            raise ValueError("只能释放没有运行时的显式 singleton owner")
        self._backend_owners.pop(owner.contract.owner_id)
        previous_commit = getattr(self, "_last_committed_snapshot", None)
        try:
            self._save(owner="system")
        except BaseException:
            if getattr(self, "_last_committed_snapshot", None) is previous_commit:
                self._backend_owners[owner.contract.owner_id] = owner
            raise

    def publish_backend_runtime(
        self, owner: ManagedBackendOwner, contract: ManagedBackendContract | None,
        runtimes: dict[str, WorkspaceRuntime] | None,
        *, removed_target_id: str | None = None,
    ) -> None:
        """原子公布在线 runtime、停机恢复合同（runtimes=None）或完整退出。"""
        stopped = runtimes is None
        published_runtimes = runtimes or {}
        desired_ids = contract.workspace_ids if contract is not None else ()
        if (stopped or contract is None) and (
            owner.process is not None or owner.unpublished_runtimes
            or not owner.lock.locked() or not self.backend_topology_lock.locked()
        ):
            raise RuntimeError("停机或空目标只能由拓扑及 owner 锁内受控关闭完成后提交")
        if not stopped and set(published_runtimes) != set(desired_ids):
            raise ValueError("公布 Backend 必须包含完整成员 runtime")
        if contract is not None and contract.owner_id != owner.contract.owner_id:
            raise ValueError("公布合同不能改变唯一 Backend owner ID")
        registered = self._backend_owners.get(owner.contract.owner_id)
        if registered is not None and registered is not owner:
            raise ValueError("Backend owner ID 已由另一个唯一对象持有")
        previous_contract = owner.contract
        if removed_target_id is not None:
            self.validate_target_removal(removed_target_id, owner="manual_crud")
            if removed_target_id not in previous_contract.workspace_ids or removed_target_id in desired_ids:
                raise ValueError("受控删除只能移除本 owner 中已退出的路由目标")
        previous_commit = getattr(self, "_last_committed_snapshot", None)
        affected = set(previous_contract.workspace_ids) | set(desired_ids)
        staged_targets = dict(self._targets)
        staged_runtimes = dict(self._runtimes)
        staged_owners = dict(self._backend_owners)
        runtime_urls = {}
        for member in contract.members if contract is not None else ():
            target = self.resolve(member.workspace_id)
            if target.connection_kind != "local" or not target.managed:
                raise ValueError("Backend 成员必须是已登记 local managed target")
            self.local_workspace_root(member.workspace_id, member.backend_workspace_id)
            other = self.backend_owner(member.workspace_id)
            if other is not None and other is not owner:
                if other.process is not None or other.unpublished_runtimes:
                    raise ValueError("不能合并另一个活动 Backend owner")
                if set(other.contract.workspace_ids) - set(contract.workspace_ids):
                    raise ValueError("加入成员仍属于另一完整组；先显式移出原组")
                staged_owners.pop(other.contract.owner_id, None)
            if not stopped:
                runtime = published_runtimes[member.workspace_id]
                if any(not runtime.service_urls.get(name) for name in ("terminal_manager", "browser_manager")):
                    raise RuntimeError("Backend 完整 runtime 缺少辅助服务地址")
                if runtime.backend_workspace_id != member.backend_workspace_id:
                    raise RuntimeError("Backend runtime 与完整成员 UUID 不匹配")
                runtime_urls[member.workspace_id] = {**runtime.service_urls, "workspace_api": contract.backend_url}
                staged_runtimes[member.workspace_id] = runtime
            staged_targets[member.workspace_id] = replace(
                target, backend_url=contract.backend_url,
                local_service_urls=target.local_service_urls if stopped else {
                    name: published_runtimes[member.workspace_id].service_urls[name]
                    for name in ("terminal_manager", "browser_manager")
                    if name in published_runtimes[member.workspace_id].service_urls
                },
                desired_running=not stopped, connection_error=None,
                target_generation=f"target_generation_{uuid4().hex}",
                runtime_lease_id=None if stopped else f"runtime_lease_{uuid4().hex}",
            )
        for workspace_id in affected:
            self._assert_route_references_drained(workspace_id)
        # 停机合同清理全部残余辅助服务；失败引用与旧权威合同保留供重试。
        closing_ids = affected if stopped else set(previous_contract.workspace_ids) - set(desired_ids)
        for workspace_id in closing_ids:
            runtime = self._runtimes.get(workspace_id)
            if runtime is not None:
                runtime.close()
            staged_runtimes.pop(workspace_id, None)
            if workspace_id not in desired_ids:
                staged_targets[workspace_id] = replace(
                    self.resolve(workspace_id), backend_url="", local_service_urls={},
                    desired_running=False, connection_error=None, runtime_lease_id=None,
                    target_generation=f"target_generation_{uuid4().hex}",
                )
        if removed_target_id is not None:
            staged_targets.pop(removed_target_id)
            for workspace_id, target in tuple(staged_targets.items()):
                if target.parent_workspace_id == removed_target_id:
                    staged_targets[workspace_id] = replace(target, parent_workspace_id=None)
        if contract is None:
            staged_owners.pop(previous_contract.owner_id)
        else:
            staged_owners[contract.owner_id] = owner

        def publish() -> None:
            if contract is not None:
                owner.contract = contract
                if stopped:
                    owner.error = None
            self._targets = staged_targets
            self._runtimes = staged_runtimes
            self._backend_owners = staged_owners
            for workspace_id, runtime in published_runtimes.items():
                runtime.backend_owner = owner
                runtime.service_urls = runtime_urls[workspace_id]
            self._route_signatures = {
                workspace_id: self._route_signature(target) for workspace_id, target in staged_targets.items()
            }

        try:
            self._save(
                owner="registry" if removed_target_id is not None else "system", targets=staged_targets,
                backend_contracts=tuple(
                    contract if candidate_owner is owner else candidate_owner.contract
                    for candidate_owner in staged_owners.values()
                ),
                publish=publish,
            )
        except BaseException as error:
            if getattr(self, "_last_committed_snapshot", None) is not previous_commit:
                error.add_note("Backend 完整合同已提交；提交观察者失败，不能回滚为旧合同")
            raise
        finally:
            if getattr(self, "_last_committed_snapshot", None) is not previous_commit:
                for workspace_id, runtime in published_runtimes.items():
                    if owner.unpublished_runtimes.get(workspace_id) is runtime:
                        owner.unpublished_runtimes.pop(workspace_id)
                for workspace_id in affected:
                    self.invalidate_route(workspace_id)

    def mark_backend_error(self, owner: ManagedBackendOwner, error: str) -> None:
        staged_targets = dict(self._targets)
        for workspace_id in owner.contract.workspace_ids:
            staged_targets[workspace_id] = replace(self.resolve(workspace_id), connection_error=error)

        def publish() -> None:
            owner.error = error
            for workspace_id in owner.contract.workspace_ids:
                self.resolve(workspace_id).connection_error = error
                self.invalidate_route(workspace_id)

        self._save(owner="system", targets=staged_targets, publish=publish)

    def stop_backend_owner(self, owner: ManagedBackendOwner) -> None:
        for workspace_id in owner.contract.workspace_ids:
            target = self.resolve(workspace_id)
            if target.system_default or not target.removable:
                raise PermissionError(f"默认工作区不能关闭: {target.name}")
            self._assert_route_references_drained(workspace_id)
        owner.close()
        errors = []
        for workspace_id in owner.contract.workspace_ids:
            runtime = self._runtimes.get(workspace_id)
            if runtime is not None:
                try:
                    runtime.close()
                except Exception as error:  # noqa: BLE001 — 尝试全部成员，失败 runtime 留待整组 start 或关闭重试。
                    errors.append(f"{workspace_id}: {error}")
                else:
                    self._runtimes.pop(workspace_id)
            self.invalidate_route(workspace_id)
            target = self.resolve(workspace_id)
            target.desired_running = False
            target.connection_error = None
        if self._active_workspace_id in owner.contract.workspace_ids:
            self._active_workspace_id = self._default_workspace_id()
        if errors:
            owner.error = "完整 Backend 已停止，成员辅助服务关闭失败: " + "; ".join(errors)
            for workspace_id in owner.contract.workspace_ids:
                self.resolve(workspace_id).connection_error = owner.error
        self._save(owner="system")
        if errors:
            raise RuntimeError(owner.error)

    def managed_runtime(self, workspace_id: str) -> WorkspaceRuntime:
        target = self.resolve(workspace_id)
        if not target.managed:
            raise ValueError(f"工作区不由 Gateway 托管: {workspace_id}")
        runtime = self._runtimes.get(workspace_id)
        if runtime is None:
            raise RuntimeError(f"托管工作区缺少运行时: {workspace_id}")
        return runtime
    def has_runtime(self, workspace_id: str) -> bool:
        self.resolve(workspace_id)
        return workspace_id in self._runtimes
    def runtime_service_urls(self, workspace_id: str) -> dict[str, str]:
        """返回当前 Gateway 直接持有的工作区服务地址快照。"""
        self.resolve(workspace_id)
        runtime = self._runtimes.get(workspace_id)
        return dict(runtime.service_urls) if runtime is not None else {}
    def assert_runtime_consumers_healthy(self) -> None:
        """确认 Workspace process 与配置驱动 remote projection 已可服务。"""

        errors: list[str] = []
        for workspace_id, runtime in self._runtimes.items():
            try:
                runtime.assert_healthy()
            except RuntimeError as error:
                errors.append(f"workspace_id={workspace_id}: {error}")

        for connection in self._remote_gateway_connections.values():
            if connection.source_owner != "config":
                continue
            runtime = self._remote_gateway_runtimes.get(connection.connection_id)
            if runtime is None:
                errors.append(
                    f"connection_id={connection.connection_id}: remote Gateway runtime 未连接"
                )
                continue
            try:
                runtime.assert_healthy()
            except RuntimeError as error:
                errors.append(f"connection_id={connection.connection_id}: {error}")
            projection_errors = [
                target.workspace_id
                for target in self._targets.values()
                if target.remote_gateway_connection_id == connection.connection_id
                and target.connection_error is not None
            ]
            if projection_errors:
                errors.append(
                    f"connection_id={connection.connection_id}: projection 不健康，"
                    f"workspace_id={','.join(sorted(projection_errors))}"
                )
        if errors:
            raise RuntimeError("Gateway runtime consumer 健康检查失败: " + "; ".join(errors))
    def runtime_health_proof(
        self,
        *,
        consumer_id: Literal[
            "registry-batch",
            "workspace-process",
            "remote-projection",
        ],
        generation: str,
        fencing_token_digest: str | None = None,
    ) -> GatewayRuntimeHealthProof:
        """为 registry 相关消费者生成不含地址和秘密的健康证明。"""

        if consumer_id == "registry-batch":
            for target in self._targets.values():
                self._validate_target_identity(target)
            if self._runtime_generation not in {None, generation}:
                raise RuntimeError(
                    "Gateway registry runtime generation 不匹配: "
                    f"expected={generation}, actual={self._runtime_generation}"
                )
            details = {
                "registry_revision": self._registry_revision,
                "target_count": len(self._targets),
                "runtime_generation": self._runtime_generation,
            }
        elif consumer_id == "workspace-process":
            contracts = []
            for owner in self._backend_owners.values():
                if owner.process is None:
                    continue
                owner.assert_healthy()
                if owner.gateway_generation not in {None, generation}:
                    raise RuntimeError("Backend owner Gateway generation 不匹配")
                if any(self._runtimes.get(member.workspace_id) is None for member in owner.contract.members):
                    raise RuntimeError("Backend owner 缺少完整成员 runtime")
                contracts.append({
                    "owner_id": owner.contract.owner_id, "backend_generation": owner.contract.generation,
                    "affected_workspace_ids": list(owner.contract.workspace_ids),
                    "mount_manifest_digest": mounted_workspace_manifest_digest(self.resolve_backend_mounts(owner.contract)),
                })
            details = {"managed_backend_count": len(contracts), "backends": contracts}

        else:
            remote_targets = 0
            remote_runtimes = 0
            for connection in self._remote_gateway_connections.values():
                if connection.source_owner != "config":
                    continue
                remote_runtimes += 1
                runtime = self._remote_gateway_runtimes.get(connection.connection_id)
                if runtime is None:
                    raise RuntimeError(
                        "Gateway remote projection runtime 未连接: "
                        f"connection_id={connection.connection_id}"
                    )
                runtime.assert_healthy()
                if runtime.gateway_generation not in {None, generation}:
                    raise RuntimeError(
                        "Gateway remote runtime generation 不匹配: "
                        f"connection_id={connection.connection_id}, "
                        f"expected={generation}, actual={runtime.gateway_generation}"
                    )
                for target in self._targets.values():
                    if (
                        target.remote_gateway_connection_id
                        == connection.connection_id
                    ):
                        remote_targets += 1
                        if target.connection_error is not None:
                            raise RuntimeError(
                                "Gateway remote projection 不健康: "
                                f"workspace_id={target.workspace_id}"
                            )
            details = {
                "config_remote_runtime_count": remote_runtimes,
                "remote_projection_target_count": remote_targets,
            }
        return GatewayRuntimeHealthProof(
            consumer_id=consumer_id,
            generation=generation,
            state="healthy",
            details=details,
            fencing_token_digest=fencing_token_digest,
        )
    @property
    def runtime_generation(self) -> str | None:
        """返回当前 registry runtime lease 所属的 Gateway generation。"""

        return self._runtime_generation
    def prepare_runtime_generation(self, generation: str) -> str | None:
        """校验 registry batch 可绑定到新的 Gateway generation。"""

        if not generation.strip():
            raise ValueError("Gateway registry generation 不能为空")
        for target in self._targets.values():
            self._validate_target_identity(target)
        return self._runtime_generation
    def apply_runtime_generation(self, generation: str) -> str | None:
        """持久化 registry batch 的 generation lease。"""

        previous_generation = self.prepare_runtime_generation(generation)
        self._runtime_generation = generation
        self._save(owner="system")
        return previous_generation
    def promote_runtime_generation(self, generation: str) -> None:
        if self._runtime_generation != generation:
            raise RuntimeError(
                "Gateway registry generation promotion 不匹配: "
                f"expected={generation}, actual={self._runtime_generation}"
            )
    def rollback_runtime_generation(
        self,
        generation: str,
        previous_generation: str | None,
    ) -> None:
        if self._runtime_generation == previous_generation:
            return
        if self._runtime_generation != generation:
            raise RuntimeError(
                "Gateway registry generation 回退发现当前 lease 已被替换: "
                f"expected={generation}, actual={self._runtime_generation}"
            )
        self._runtime_generation = previous_generation
        self._save(owner="system")
    def prepare_workspace_process_generation(self, generation: str) -> dict[str, str | None]:
        if not generation.strip():
            raise ValueError("Workspace process generation 不能为空")
        previous: dict[str, str | None] = {}
        for owner in self._backend_owners.values():
            if owner.process is not None:
                owner.assert_healthy()
                previous[owner.contract.owner_id] = owner.gateway_generation
        return previous

    def apply_workspace_process_generation(self, generation: str) -> dict[str, str | None]:
        previous = self.prepare_workspace_process_generation(generation)
        for owner_id in previous:
            self._backend_owners[owner_id].gateway_generation = generation
        return previous

    def promote_workspace_process_generation(self, generation: str) -> None:
        for owner in self._backend_owners.values():
            if owner.process is not None and owner.gateway_generation != generation:
                raise RuntimeError("Backend owner Gateway generation promotion 不匹配")

    def rollback_workspace_process_generation(self, generation: str, previous: dict[str, str | None]) -> None:
        for owner_id, previous_generation in previous.items():
            owner = self._backend_owners.get(owner_id)
            if owner is None or owner.gateway_generation not in {generation, previous_generation}:
                raise RuntimeError("Backend owner Gateway generation 回退发现 owner 已替换")
            owner.gateway_generation = previous_generation

    def prepare_remote_projection_generation(
        self,
        generation: str,
    ) -> dict[str, str | None]:
        if not generation.strip():
            raise ValueError("Remote projection generation 不能为空")
        previous: dict[str, str | None] = {}
        for connection_id, runtime in self._remote_gateway_runtimes.items():
            runtime.assert_healthy()
            previous[connection_id] = runtime.gateway_generation
        return previous
    def apply_remote_projection_generation(
        self,
        generation: str,
    ) -> dict[str, str | None]:
        previous = self.prepare_remote_projection_generation(generation)
        for connection_id in previous:
            self._remote_gateway_runtimes[connection_id].gateway_generation = generation
        return previous
    def promote_remote_projection_generation(self, generation: str) -> None:
        for connection_id, runtime in self._remote_gateway_runtimes.items():
            if runtime.gateway_generation != generation:
                raise RuntimeError(
                    "Remote projection generation promotion 不匹配: "
                    f"connection_id={connection_id}, expected={generation}, "
                    f"actual={runtime.gateway_generation}"
                )
    def rollback_remote_projection_generation(
        self,
        generation: str,
        previous: dict[str, str | None],
    ) -> None:
        for connection_id, previous_generation in previous.items():
            runtime = self._remote_gateway_runtimes.get(connection_id)
            if runtime is None:
                raise RuntimeError(
                    "Remote projection generation 回退发现 runtime 已被替换: "
                    f"connection_id={connection_id}"
                )
            if runtime.gateway_generation == previous_generation:
                continue
            if runtime.gateway_generation != generation:
                raise RuntimeError(
                    "Remote projection generation 回退发现 runtime 已被替换: "
                    f"connection_id={connection_id}"
                )
            runtime.gateway_generation = previous_generation
    def close(
        self,
        *,
        preserve_browser_managers: bool = False,
        preserve_terminal_managers: bool = False,
        preserve_workspace_backends: bool = False,
        detach_adopted_managed_processes: bool = False,
    ) -> None:
        errors: list[str] = []
        for workspace_id in tuple(self._targets):
            self.invalidate_route(workspace_id)

        failed_backend_members: set[str] = set()
        for backend_owner in self._backend_owners.values():
            try:
                # 未发布候选不属于可移交合同，必须先关闭 Backend，再回收其辅助服务。
                if not backend_owner.unpublished_runtimes and (
                    preserve_workspace_backends or (detach_adopted_managed_processes and backend_owner.adopted)
                ):
                    backend_owner.detach()
                else:
                    backend_owner.close()
            except Exception as error:  # noqa: BLE001 — 完整回收后汇总，各失败 owner 保留依赖供重试。
                failed_backend_members.update(backend_owner.contract.workspace_ids)
                errors.append(f"{backend_owner.contract.owner_id}: {error}")
        local_runtimes = [
            (workspace_id, runtime) for workspace_id, runtime in self._runtimes.items()
            if workspace_id not in failed_backend_members
        ]
        remote_runtimes = list(self._remote_gateway_runtimes.items())
        retired_remote_runtimes = [
            (connection_id, runtime)
            for connection_id, runtimes in self._retired_remote_gateway_runtimes.items()
            for runtime in runtimes
        ]
        if detach_adopted_managed_processes:
            for _, runtime in local_runtimes:
                runtime.detach_adopted_processes()
        if preserve_browser_managers:
            for _, runtime in local_runtimes:
                runtime.detach_process("browser_manager")
        if preserve_terminal_managers:
            for _, runtime in local_runtimes:
                runtime.detach_process("terminal_manager")

        for runtime_id, runtime in (
            *local_runtimes,
            *remote_runtimes,
            *retired_remote_runtimes,
        ):
            try:
                runtime.request_terminate()
            except Exception as error:  # noqa: BLE001 — 回收全部已拥有运行时并聚合清理错误。
                errors.append(f"{runtime_id} 发送终止信号失败: {error}")

        for runtime_id, runtime in (
            *local_runtimes,
            *remote_runtimes,
            *retired_remote_runtimes,
        ):
            try:
                runtime.wait_closed()
            except Exception as error:  # noqa: BLE001 — 回收全部已拥有运行时并聚合清理错误。
                errors.append(f"{runtime_id}: {error}")
            else:
                # 同名 active/retired 可能并存，只移除本次确实关闭成功的对象。
                if self._runtimes.get(runtime_id) is runtime:
                    self._runtimes.pop(runtime_id)
                if self._remote_gateway_runtimes.get(runtime_id) is runtime:
                    self._remote_gateway_runtimes.pop(runtime_id)
                self._remove_retired_runtime_instances(((runtime_id, runtime),))
        if errors:
            raise RuntimeError("关闭 Gateway 托管进程失败: " + "; ".join(errors))
