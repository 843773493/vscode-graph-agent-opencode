from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.config.state import ConfigConflictError

from .core import GatewayRegistryBatchHandle, WorkspaceKey, WorkspaceTarget

if TYPE_CHECKING:
    from . import GatewayWorkspaceRegistry


class RegistryProjectionMixin:
    def validate_remote_projection_cursor(
        self,
        connection_id: str,
        incoming_cursor: int | None,
    ) -> None:
        """拒绝旧远端快照覆盖新投影；跳跃值只能来自完整快照。"""

        if incoming_cursor is None:
            return
        if (
            isinstance(incoming_cursor, bool)
            or not isinstance(incoming_cursor, int)
            or incoming_cursor < 0
        ):
            raise ValueError("远程 Gateway 配置事件游标必须是非负整数")
        existing = self._remote_gateway_connections.get(connection_id)
        if existing is None or existing.remote_config_event_cursor is None:
            return
        if incoming_cursor < existing.remote_config_event_cursor:
            raise ConfigConflictError(
                "远程 Gateway 配置快照游标回退，保留当前投影: "
                f"connection_id={connection_id}, "
                f"current={existing.remote_config_event_cursor}, "
                f"incoming={incoming_cursor}"
            )
    def _stage_remote_projections(
        self, *, projections: dict[str, tuple[WorkspaceTarget, ...]],
        connections: dict[str, RemoteGatewayConnection], replacing_connection_ids: set[str],
    ) -> dict[WorkspaceKey, WorkspaceTarget]:
        """按资源 owner 合并连接关系；单连接快照和配置批次使用同一条链路。"""
        staged: dict[WorkspaceKey, WorkspaceTarget] = {}
        for key, target in self._targets.items():
            if target.connection_kind == "local":
                staged[key] = target
                continue
            remaining = tuple(identity for identity in target.remote_gateway_connection_ids if identity not in replacing_connection_ids)
            if remaining:
                staged[key] = replace(target, remote_gateway_connection_ids=remaining)
        for connection_id, incoming in projections.items():
            connection = connections[connection_id]
            seen: set[WorkspaceKey] = set()
            for target in incoming:
                self._validate_target_identity(target)
                if target.gateway_id != connection.remote_gateway_id or target.remote_gateway_connection_ids != (connection_id,):
                    raise PermissionError("远端快照归属必须与提交连接的原生 Gateway 完全一致")
                if target.key in seen:
                    raise ValueError(f"远端连接快照包含重复工作区 owner: {target.key}")
                seen.add(target.key)
                existing = self._targets.get(target.key)
                if existing is not None and existing.owner != "remote_projection":
                    raise PermissionError(f"远端快照不能覆盖其它 owner: {target.key}")
                merged = staged.get(target.key)
                links = set(merged.remote_gateway_connection_ids) if merged is not None else set()
                links.add(connection_id)
                staged[target.key] = replace(
                    target, remote_gateway_connection_ids=tuple(sorted(links)), connection_id=None,
                    parent_workspace=existing.parent_workspace if existing is not None else target.parent_workspace,
                )
        for key, target in staged.items():
            existing = self._targets.get(key)
            if existing is not None and self._route_signature(existing) == self._route_signature(target):
                target.target_generation = target.target_generation or existing.target_generation
                target.runtime_lease_id = target.runtime_lease_id or existing.runtime_lease_id
            else:
                if self._has_route_references(key):
                    raise RuntimeError(f"远端快照不能替换仍有引用的 route: {key}")
                target.target_generation = f"target_generation_{uuid4().hex}"
                target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
        for key in self._targets.keys() - staged.keys():
            if self._has_route_references(key):
                raise RuntimeError(f"远端快照不能删除仍有引用的 route: {key}")
        for key, target in tuple(staged.items()):
            if target.parent_workspace is not None and target.parent_workspace not in staged:
                staged[key] = replace(target, parent_workspace=None)
        return staged

    def apply_remote_projection_snapshot(
        self,
        *,
        connection: RemoteGatewayConnection,
        projections: tuple[WorkspaceTarget, ...],
        runtime: WorkspaceRuntime | None = None,
        activate: bool = False,
    ) -> None:
        """以一次 registry CAS 应用单个远程 Gateway 的完整快照。"""

        self.validate_remote_projection_cursor(
            connection.connection_id,
            connection.remote_config_event_cursor,
        )
        previous_snapshot = self._capture_registry_state()
        previous_signatures = dict(self._route_signatures)
        previous_commit = self._last_committed_snapshot
        previous_runtime = self._remote_gateway_runtimes.get(
            connection.connection_id
        )
        existing_connection = self._remote_gateway_connections.get(
            connection.connection_id
        )
        if (
            existing_connection is not None
            and existing_connection.source_owner != connection.source_owner
        ):
            raise PermissionError(
                "Gateway 远程连接 source owner 不允许通过快照改变: "
                f"connection_id={connection.connection_id}, "
                f"current={existing_connection.source_owner}, "
                f"requested={connection.source_owner}"
            )
        self._targets = self._stage_remote_projections(
            projections={connection.connection_id: projections},
            connections={connection.connection_id: connection},
            replacing_connection_ids={connection.connection_id},
        )
        staged_targets = {target.key: target for target in projections}
        self._remote_gateway_connections[connection.connection_id] = connection
        if activate and staged_targets:
            self._active_workspace = next(iter(staged_targets))
        elif self._active_workspace not in self._targets:
            self._active_workspace = self._default_workspace_key()
        self._validate_parent_graph()
        post_commit_error: BaseException | None = None
        try:
            self._save(
                owner="remote_projection",
                remote_gateway_connection_ids=(connection.connection_id,),
            )
        except BaseException as error:
            if self._last_committed_snapshot is previous_commit:
                self._restore_registry_state(previous_snapshot)
                self._route_signatures = previous_signatures
                raise
            post_commit_error = error

        new_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }
        for workspace_id, signature in new_signatures.items():
            if previous_signatures.get(workspace_id) != signature:
                self.invalidate_route(workspace_id)
        for workspace_id in previous_signatures:
            if workspace_id not in new_signatures:
                self.invalidate_route(workspace_id)
        self._route_signatures = new_signatures

        if runtime is not None:
            self._remote_gateway_runtimes[connection.connection_id] = runtime
            if previous_runtime is not None and previous_runtime is not runtime:
                if self._has_route_references_for_connection(connection.connection_id):
                    self._register_retired_remote_gateway_runtime(
                        connection.connection_id,
                        previous_runtime,
                    )
                else:
                    try:
                        previous_runtime.close()
                    except Exception:
                        self._register_retired_remote_gateway_runtime(
                            connection.connection_id,
                            previous_runtime,
                        )
        if post_commit_error is not None:
            raise post_commit_error.with_traceback(post_commit_error.__traceback__)
    def apply_remote_projection_batch(
        self,
        *,
        connections: tuple[RemoteGatewayConnection, ...],
        runtimes: dict[str, WorkspaceRuntime],
        projections: dict[str, tuple[WorkspaceTarget, ...]],
        activate_connection_ids: tuple[str, ...] = (),
        defer_retiring_runtimes: bool = False,
    ) -> GatewayRegistryBatchHandle:
        """一次性提交配置驱动的远程投影，并保留旧 tunnel 直到可安全切换。

        连接建立和远端健康探测必须在调用方完成；本方法只负责本地
        registry batch 的 owner/identity/route lease/CAS 边界。任何仍有代理
        引用的 route 都不允许被配置 batch 替换或删除。
        """

        configured_connection_by_id = {
            connection.connection_id: connection for connection in connections
        }
        if len(configured_connection_by_id) != len(connections):
            raise ValueError("Gateway registry 配置 batch 包含重复 connection_id")
        if any(
            connection.source_owner != "config" for connection in connections
        ):
            raise PermissionError("Gateway 配置 batch 只能提交 config source owner")
        if set(configured_connection_by_id) != set(projections) or set(
            configured_connection_by_id
        ) != set(runtimes):
            raise ValueError(
                "Gateway registry 配置 batch 的 connection、runtime、projection 集合不一致"
            )

        previous_snapshot = self._capture_registry_state()
        previous_signatures = dict(self._route_signatures)
        previous_commit = self._last_committed_snapshot
        previous_remote_runtimes = dict(self._remote_gateway_runtimes)
        previous_retired_remote_runtimes = {
            connection_id: list(runtimes)
            for connection_id, runtimes in self._retired_remote_gateway_runtimes.items()
        }
        previous_connections = dict(self._remote_gateway_connections)
        for connection in connections:
            self.validate_remote_projection_cursor(
                connection.connection_id,
                connection.remote_config_event_cursor,
            )
        manual_connections = {
            connection_id: connection
            for connection_id, connection in previous_connections.items()
            if connection.source_owner == "manual"
        }
        if set(manual_connections) & set(configured_connection_by_id):
            raise ValueError(
                "Gateway 配置 batch 的 connection_id 与人工远程连接冲突"
            )
        connection_by_id = {
            **manual_connections,
            **configured_connection_by_id,
        }
        self._targets = self._stage_remote_projections(
            projections=projections,
            connections=configured_connection_by_id,
            replacing_connection_ids={
                identity for identity, connection in previous_connections.items()
                if connection.source_owner == "config"
            } | set(configured_connection_by_id),
        )
        self._remote_gateway_connections = dict(connection_by_id)
        self._active_workspace = self._select_batch_active_workspace(
            activate_connection_ids=activate_connection_ids,
        )
        self._validate_parent_graph()
        post_commit_error: BaseException | None = None
        try:
            self._save(owner="config_batch")
        except BaseException as error:
            if self._last_committed_snapshot is previous_commit:
                self._restore_registry_state(previous_snapshot)
                self._route_signatures = previous_signatures
                raise
            post_commit_error = error

        new_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }
        for workspace_id, signature in new_signatures.items():
            if previous_signatures.get(workspace_id) != signature:
                self.invalidate_route(workspace_id)
        for workspace_id in previous_signatures:
            if workspace_id not in new_signatures:
                self.invalidate_route(workspace_id)
        self._route_signatures = new_signatures

        for connection_id, runtime in runtimes.items():
            previous_runtime = previous_remote_runtimes.get(connection_id)
            self._remote_gateway_runtimes[connection_id] = runtime
            if previous_runtime is not None and previous_runtime is not runtime:
                if defer_retiring_runtimes or self._has_route_references_for_connection(
                    connection_id
                ):
                    self._register_retired_remote_gateway_runtime(
                        connection_id,
                        previous_runtime,
                    )
                else:
                    try:
                        previous_runtime.close()
                    except Exception:
                        # 新 route 已经通过 registry CAS 提交；旧 tunnel 不能
                        # 影响新 runtime 的可用性，保留到 Gateway 关闭时再收尾。
                        self._register_retired_remote_gateway_runtime(
                            connection_id,
                            previous_runtime,
                        )
        for connection_id, previous_runtime in previous_remote_runtimes.items():
            if connection_id in runtimes:
                continue
            connection = previous_connections.get(connection_id)
            if connection is not None and connection.source_owner == "manual":
                continue
            if defer_retiring_runtimes or self._has_route_references_for_connection(
                connection_id
            ):
                self._register_retired_remote_gateway_runtime(
                    connection_id,
                    previous_runtime,
                )
            else:
                try:
                    previous_runtime.close()
                except Exception:
                    self._register_retired_remote_gateway_runtime(
                        connection_id,
                        previous_runtime,
                    )

        retired_runtimes = tuple(
            (connection_id, runtime)
            for connection_id, runtime_items in (
                self._retired_remote_gateway_runtimes.items()
            )
            for runtime in runtime_items
            if not any(
                runtime is previous_runtime
                for previous_runtime in previous_retired_remote_runtimes.get(
                    connection_id,
                    [],
                )
            )
        )
        handle = GatewayRegistryBatchHandle(
            registry=self,
            previous_snapshot=previous_snapshot,
            previous_signatures=previous_signatures,
            previous_remote_runtimes=previous_remote_runtimes,
            previous_retired_remote_runtimes=previous_retired_remote_runtimes,
            staged_runtimes=dict(runtimes),
            retired_runtimes=retired_runtimes,
            committed_revision=self._registry_revision,
            deferred_retirement=defer_retiring_runtimes,
        )
        if defer_retiring_runtimes:
            cast("GatewayWorkspaceRegistry", self)._deferred_registry_batches[id(handle)] = handle
        if post_commit_error is not None:
            try:
                handle.promote()
            # 仅附加同步旧 runtime 的普通收尾错误，进程级异常继续直传。
            except Exception as retirement_error:  # noqa: BLE001
                note = (
                    "Gateway registry batch 已提交，但旧 remote runtime retirement "
                    f"未完成: {retirement_error}"
                )
                post_commit_error.add_note(note)
            raise post_commit_error.with_traceback(
                post_commit_error.__traceback__
            )
        return handle
    def _rollback_registry_batch(self, handle: GatewayRegistryBatchHandle) -> None:
        """补偿已提交 batch；CAS 失败时保留 recovery_required 所需证据。"""

        registry = cast("GatewayWorkspaceRegistry", self)
        current_runtimes = list(registry._remote_gateway_runtimes.items()) + [
            (connection_id, runtime)
            for connection_id, runtimes in registry._retired_remote_gateway_runtimes.items()
            for runtime in runtimes
        ]
        registry._restore_registry_state(handle.previous_snapshot)
        self._remote_gateway_runtimes = dict(handle.previous_remote_runtimes)
        restored_ids = {id(runtime) for runtime in handle.previous_remote_runtimes.values()}
        self._retired_remote_gateway_runtimes = {
            connection_id: [runtime for runtime in runtimes if id(runtime) not in restored_ids]
            for connection_id, runtimes in (
                handle.previous_retired_remote_runtimes.items()
            )
        }
        tracked_ids = restored_ids | {
            id(runtime) for runtimes in registry._retired_remote_gateway_runtimes.values() for runtime in runtimes
        }
        # 回滚先恢复旧 active；本批新增且仍在途的代留在原 retired owner，最后一次释放再关闭。
        cleanup_connections: set[str] = set()
        for connection_id, runtime in current_runtimes:
            if id(runtime) not in tracked_ids:
                registry._register_retired_remote_gateway_runtime(connection_id, runtime)
                tracked_ids.add(id(runtime))
                cleanup_connections.add(connection_id)
        for connection_id in cleanup_connections:
            registry._close_retired_remote_gateway_runtimes(connection_id)
        self._route_signatures = dict(handle.previous_signatures)
        # registry revision 已在 batch 提交后递增；补偿写入必须以当前 revision
        # 为 CAS 基线，不能把持久 revision 伪造回旧值。
        self._registry_revision = handle.committed_revision
        registry._save(owner="config_batch")
    def _select_batch_active_workspace(
        self,
        *,
        activate_connection_ids: tuple[str, ...],
    ) -> WorkspaceKey | None:
        for connection_id in activate_connection_ids:
            for target in self._targets.values():
                if (
                    target.owner == "remote_projection"
                    and connection_id in target.remote_gateway_connection_ids
                ):
                    return target.key
        if self._active_workspace in self._targets:
            return self._active_workspace
        return self._default_workspace_key()
