from __future__ import annotations

from uuid import uuid4

from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.config.state import ConfigConflictError

from .core import GatewayRegistryBatchHandle, WorkspaceTarget


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
        staged_targets: dict[str, WorkspaceTarget] = {}
        for target in projections:
            self._validate_target_identity(target)
            if (
                target.owner != "remote_projection"
                or target.remote_gateway_connection_id != connection.connection_id
            ):
                raise PermissionError(
                    "远程快照只能提交对应 connection 的 remote_projection target"
                )
            if target.workspace_id in staged_targets:
                raise ValueError(
                    "远程 Gateway 快照包含重复 workspace_id: "
                    f"{target.workspace_id}"
                )
            existing = self._targets.get(target.workspace_id)
            if existing is not None and existing.owner != "remote_projection":
                raise PermissionError(
                    "远程快照不能越过现有 target owner: "
                    f"workspace_id={target.workspace_id}, current={existing.owner}"
                )
            if existing is not None:
                if self._has_route_references(target.workspace_id) and (
                    self._route_signature(existing) != self._route_signature(target)
                ):
                    raise RuntimeError(
                        "远程快照不能替换仍有引用的 route: "
                        f"workspace_id={target.workspace_id}"
                    )
                if not target.target_generation:
                    target.target_generation = existing.target_generation
                if target.runtime_lease_id is None:
                    target.runtime_lease_id = existing.runtime_lease_id
                if self._route_signature(existing) != self._route_signature(target):
                    target.target_generation = f"target_generation_{uuid4().hex}"
                    target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
            else:
                target.target_generation = (
                    target.target_generation or f"target_generation_{uuid4().hex}"
                )
                target.runtime_lease_id = (
                    target.runtime_lease_id or f"runtime_lease_{uuid4().hex}"
                )
            staged_targets[target.workspace_id] = target

        stale_workspace_ids = {
            target.workspace_id
            for target in self._targets.values()
            if target.owner == "remote_projection"
            and target.remote_gateway_connection_id == connection.connection_id
            and target.workspace_id not in staged_targets
        }
        blocked_stale = {
            workspace_id
            for workspace_id in stale_workspace_ids
            if self._has_route_references(workspace_id)
        }
        if blocked_stale:
            raise RuntimeError(
                "远程快照不能删除仍有引用的 route: "
                + ", ".join(sorted(blocked_stale))
            )

        self._targets = {
            workspace_id: target
            for workspace_id, target in self._targets.items()
            if workspace_id not in stale_workspace_ids
            and not (
                target.owner == "remote_projection"
                and target.remote_gateway_connection_id == connection.connection_id
                and workspace_id in staged_targets
            )
        }
        self._targets.update(staged_targets)
        self._remote_gateway_connections[connection.connection_id] = connection
        if activate and staged_targets:
            self._active_workspace_id = next(iter(staged_targets))
        elif self._active_workspace_id not in self._targets:
            self._active_workspace_id = self._default_workspace_id()
        self._validate_parent_graph()
        try:
            self._save(owner="remote_projection")
        except Exception:
            self._restore_registry_state(previous_snapshot)
            self._route_signatures = previous_signatures
            raise

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
                    self._retired_remote_gateway_runtimes.setdefault(
                        connection.connection_id,
                        [],
                    ).append(previous_runtime)
                else:
                    try:
                        previous_runtime.close()
                    except Exception:
                        self._retired_remote_gateway_runtimes.setdefault(
                            connection.connection_id,
                            [],
                        ).append(previous_runtime)
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
            and connection_id not in configured_connection_by_id
        }
        if set(manual_connections) & set(configured_connection_by_id):
            raise ValueError(
                "Gateway 配置 batch 的 connection_id 与人工远程连接冲突"
            )
        connection_by_id = {
            **manual_connections,
            **configured_connection_by_id,
        }
        staged_targets: dict[str, WorkspaceTarget] = {
            workspace_id: target
            for workspace_targets in projections.values()
            for target in workspace_targets
            for workspace_id in (target.workspace_id,)
        }
        if len(staged_targets) != sum(len(items) for items in projections.values()):
            raise ValueError("Gateway registry 配置 batch 包含重复 projected workspace_id")

        def is_config_projection(target: WorkspaceTarget) -> bool:
            connection = previous_connections.get(
                target.remote_gateway_connection_id
            )
            return (
                target.owner == "remote_projection"
                and connection is not None
                and connection.source_owner == "config"
            )

        for target in staged_targets.values():
            self._validate_target_identity(target)
            if target.owner != "remote_projection":
                raise PermissionError(
                    "配置 batch 只能提交 remote_projection target: "
                    f"workspace_id={target.workspace_id}, owner={target.owner}"
                )
            existing = self._targets.get(target.workspace_id)
            if existing is not None and existing.owner != target.owner:
                raise PermissionError(
                    "配置 batch 不能越过现有 target owner: "
                    f"workspace_id={target.workspace_id}, current={existing.owner}"
                )
            if existing is not None:
                if self._has_route_references(target.workspace_id) and (
                    self._route_signature(existing) != self._route_signature(target)
                ):
                    raise RuntimeError(
                        "Gateway registry 配置 batch 不能替换仍有引用的 route: "
                        f"workspace_id={target.workspace_id}"
                    )
                if not target.target_generation:
                    target.target_generation = existing.target_generation
                if target.runtime_lease_id is None:
                    target.runtime_lease_id = existing.runtime_lease_id
                if self._route_signature(existing) != self._route_signature(target):
                    target.target_generation = f"target_generation_{uuid4().hex}"
                    target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
            else:
                target.target_generation = (
                    target.target_generation or f"target_generation_{uuid4().hex}"
                )
                target.runtime_lease_id = (
                    target.runtime_lease_id or f"runtime_lease_{uuid4().hex}"
                )

        stale_workspace_ids = {
            target.workspace_id
            for target in self._targets.values()
            if is_config_projection(target)
            and target.workspace_id not in staged_targets
        }
        blocked_stale = {
            workspace_id
            for workspace_id in stale_workspace_ids
            if self._has_route_references(workspace_id)
        }
        if blocked_stale:
            raise RuntimeError(
                "Gateway registry 配置 batch 不能删除仍有引用的 remote route: "
                + ", ".join(sorted(blocked_stale))
            )

        self._targets = {
            workspace_id: target
            for workspace_id, target in self._targets.items()
            if not is_config_projection(target) or workspace_id in staged_targets
        }
        self._targets.update(staged_targets)
        self._remote_gateway_connections = dict(connection_by_id)
        self._active_workspace_id = self._select_batch_active_workspace(
            activate_connection_ids=activate_connection_ids,
        )
        self._validate_parent_graph()
        try:
            self._save(owner="config_batch")
        except Exception:
            self._restore_registry_state(previous_snapshot)
            self._route_signatures = previous_signatures
            raise

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
                    self._retired_remote_gateway_runtimes.setdefault(
                        connection_id,
                        [],
                    ).append(previous_runtime)
                else:
                    try:
                        previous_runtime.close()
                    except Exception:
                        # 新 route 已经通过 registry CAS 提交；旧 tunnel 不能
                        # 影响新 runtime 的可用性，保留到 Gateway 关闭时再收尾。
                        self._retired_remote_gateway_runtimes.setdefault(
                            connection_id,
                            [],
                        ).append(previous_runtime)
        for connection_id, previous_runtime in previous_remote_runtimes.items():
            if connection_id in runtimes:
                continue
            connection = previous_connections.get(connection_id)
            if connection is not None and connection.source_owner == "manual":
                continue
            if defer_retiring_runtimes or self._has_route_references_for_connection(
                connection_id
            ):
                self._retired_remote_gateway_runtimes.setdefault(
                    connection_id,
                    [],
                ).append(previous_runtime)
            else:
                try:
                    previous_runtime.close()
                except Exception:
                    self._retired_remote_gateway_runtimes.setdefault(
                        connection_id,
                        [],
                    ).append(previous_runtime)

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
        return GatewayRegistryBatchHandle(
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
    def _rollback_registry_batch(self, handle: GatewayRegistryBatchHandle) -> None:
        """补偿已提交 batch；CAS 失败时保留 recovery_required 所需证据。"""

        for connection_id, runtime in handle.staged_runtimes.items():
            if self._remote_gateway_runtimes.get(connection_id) is runtime:
                runtime.close()
        self._restore_registry_state(handle.previous_snapshot)
        self._remote_gateway_runtimes = dict(handle.previous_remote_runtimes)
        self._retired_remote_gateway_runtimes = {
            connection_id: list(runtimes)
            for connection_id, runtimes in (
                handle.previous_retired_remote_runtimes.items()
            )
        }
        self._route_signatures = dict(handle.previous_signatures)
        # registry revision 已在 batch 提交后递增；补偿写入必须以当前 revision
        # 为 CAS 基线，不能把持久 revision 伪造回旧值。
        self._registry_revision = handle.committed_revision
        self._save(owner="config_batch")
    def _select_batch_active_workspace(
        self,
        *,
        activate_connection_ids: tuple[str, ...],
    ) -> str | None:
        for connection_id in activate_connection_ids:
            for target in self._targets.values():
                if (
                    target.owner == "remote_projection"
                    and target.remote_gateway_connection_id == connection_id
                ):
                    return target.workspace_id
        if self._active_workspace_id in self._targets:
            return self._active_workspace_id
        return self._default_workspace_id()
