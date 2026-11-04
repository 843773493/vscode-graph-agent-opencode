from __future__ import annotations

from app.core.failures import failure_detail
from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime

from .core import WorkspaceTarget


class RegistryRemoteMixin:
    def upsert_remote_gateway(
        self,
        connection: RemoteGatewayConnection,
        *,
        runtime: WorkspaceRuntime | None = None,
    ) -> None:
        existing_connection = self._remote_gateway_connections.get(
            connection.connection_id
        )
        if (
            existing_connection is not None
            and existing_connection.source_owner != connection.source_owner
        ):
            raise PermissionError(
                "Gateway 远程连接 source owner 不允许通过 upsert 改变: "
                f"connection_id={connection.connection_id}, "
                f"current={existing_connection.source_owner}, "
                f"requested={connection.source_owner}"
            )
        self._remote_gateway_connections[connection.connection_id] = connection
        if runtime is not None:
            previous_runtime = self._remote_gateway_runtimes.get(
                connection.connection_id
            )
            if previous_runtime is not None:
                if self._has_route_references_for_connection(connection.connection_id):
                    self._register_retired_remote_gateway_runtime(
                        connection.connection_id,
                        previous_runtime,
                    )
                else:
                    try:
                        previous_runtime.close()
                    except Exception:  # noqa: BLE001 - 发布新隧道前保留失败的旧运行时，沿用 retired 重试语义。
                        self._register_retired_remote_gateway_runtime(
                            connection.connection_id,
                            previous_runtime,
                        )
            self._remote_gateway_runtimes[connection.connection_id] = runtime
            for target in self._targets.values():
                if connection.connection_id in target.remote_gateway_connection_ids:
                    self.invalidate_route(target.key)
        self._save(
            owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
        )

    def remote_connection_for_target(self, target: WorkspaceTarget) -> RemoteGatewayConnection:
        """连接关系不是资源身份；多条关系不能隐式选第一条或故障切换。"""
        if target.connection_kind != "remote_gateway" or len(target.remote_gateway_connection_ids) != 1:
            raise LookupError(f"工作区远端连接不能唯一确定: owner={target.key}, connections={target.remote_gateway_connection_ids}")
        connection = self.remote_gateway_connection(target.remote_gateway_connection_ids[0])
        if connection.remote_gateway_id != target.gateway_id:
            raise RuntimeError(f"工作区连接归属不匹配: owner={target.key}, connection={connection.connection_id}")
        return connection

    def remote_gateway_connection(
        self,
        connection_id: str,
    ) -> RemoteGatewayConnection:
        connection = self._remote_gateway_connections.get(connection_id)
        if connection is None:
            error = LookupError(f"未知远程 Gateway 连接: {connection_id}")
            raise LookupError(failure_detail(error, stage="gateway-remote-connection", label="未知远程 Gateway 连接")) from error
        return connection
    def remote_gateway_url(self, connection_id: str) -> str:
        runtime = self._remote_gateway_runtimes.get(connection_id)
        if runtime is None:
            error = LookupError(f"远程 Gateway 隧道尚未连接: {connection_id}")
            raise LookupError(failure_detail(error, stage="gateway-remote-url", label="远程 Gateway 隧道尚未连接")) from error
        return runtime.service_urls["workspace_api"]
    def remote_gateway_connections(self) -> tuple[RemoteGatewayConnection, ...]:
        return tuple(self._remote_gateway_connections.values())
    def _close_unused_remote_gateway(self, connection_id: str | None) -> None:
        if connection_id is None:
            return
        if any(
            connection_id in target.remote_gateway_connection_ids
            for target in self._targets.values()
        ):
            return
        self._cleanup_remote_gateway_if_unused(connection_id)
    def _cleanup_remote_gateway_if_unused(self, connection_id: str) -> None:
        if self._has_route_references_for_connection(connection_id):
            return
        self._close_retired_remote_gateway_runtimes(connection_id)
        if any(
            connection_id in target.remote_gateway_connection_ids
            for target in self._targets.values()
        ):
            return
        active_runtime = self._remote_gateway_runtimes.get(connection_id)
        if active_runtime is not None:
            active_runtime.close()
            self._remote_gateway_runtimes.pop(connection_id)
        self._remote_gateway_connections.pop(connection_id, None)
        FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).remove(connection_id)

    def _register_retired_remote_gateway_runtime(
        self,
        connection_id: str,
        retired_runtime: WorkspaceRuntime,
    ) -> None:
        retired_runtimes = self._retired_remote_gateway_runtimes.get(connection_id)
        if retired_runtimes is None:
            self._retired_remote_gateway_runtimes[connection_id] = [retired_runtime]
        else:
            retired_runtimes.append(retired_runtime)

    def _has_route_references_for_connection(self, connection_id: str) -> bool:
        return any(
            connection_id in referenced_connection_ids
            and any(self._route_reference_counts.get(workspace_id, (0, 0)))
            for workspace_id, referenced_connection_ids in (
                self._route_reference_connections.items()
            )
        )
    def _close_retired_remote_gateway_runtimes(self, connection_id: str) -> None:
        if self._has_route_references_for_connection(connection_id):
            return
        retired_runtimes = tuple(
            self._retired_remote_gateway_runtimes.get(connection_id, [])
        )
        successfully_closed_runtimes: list[WorkspaceRuntime] = []
        try:
            for retired_runtime in retired_runtimes:
                retired_runtime.close()
                successfully_closed_runtimes.append(retired_runtime)
        finally:
            if successfully_closed_runtimes:
                self._remove_retired_runtime_instances(
                    tuple(
                        (connection_id, retired_runtime)
                        for retired_runtime in successfully_closed_runtimes
                    )
                )

    def _remove_retired_runtime_instances(
        self,
        retired_runtime_entries: tuple[tuple[str, WorkspaceRuntime], ...],
    ) -> None:
        runtime_ids_by_connection: dict[str, set[int]] = {}
        for connection_id, runtime_to_remove in retired_runtime_entries:
            runtime_ids = runtime_ids_by_connection.get(connection_id)
            if runtime_ids is None:
                runtime_ids = set()
                runtime_ids_by_connection[connection_id] = runtime_ids
            runtime_ids.add(id(runtime_to_remove))

        for connection_id, runtime_ids in runtime_ids_by_connection.items():
            retired_runtimes = self._retired_remote_gateway_runtimes.get(connection_id)
            if retired_runtimes is None:
                continue
            retained_runtimes = [
                retired_runtime
                for retired_runtime in retired_runtimes
                if id(retired_runtime) not in runtime_ids
            ]
            if retained_runtimes:
                self._retired_remote_gateway_runtimes[
                    connection_id
                ] = retained_runtimes
            else:
                self._retired_remote_gateway_runtimes.pop(connection_id, None)

    def remove_remote_gateway(self, connection_id: str) -> None:
        """只解绑指定连接；其它连接仍引用的资源记录保持原 owner。"""
        from dataclasses import replace

        connection = self.remote_gateway_connection(connection_id)
        if connection.source_owner != "manual":
            raise PermissionError("配置管理的远端连接必须通过配置 owner 删除")
        affected = [target for target in self._targets.values() if connection_id in target.remote_gateway_connection_ids]
        for target in affected:
            self._assert_route_references_drained(target.key)
        previous = self._capture_registry_state()
        previous_commit = self._last_committed_snapshot
        for target in affected:
            remaining = tuple(identity for identity in target.remote_gateway_connection_ids if identity != connection_id)
            if remaining:
                self._targets[target.key] = replace(target, remote_gateway_connection_ids=remaining)
            else:
                del self._targets[target.key]
        for target in self._targets.values():
            if target.parent_workspace is not None and target.parent_workspace not in self._targets:
                target.parent_workspace = None
        self._remote_gateway_connections.pop(connection_id)
        if self._active_workspace not in self._targets:
            self._active_workspace = self._default_workspace_key()
        try:
            self._save(owner="remote_projection", remote_gateway_connection_ids=(connection_id,))
        except BaseException:
            if self._last_committed_snapshot is previous_commit:
                self._restore_registry_state(previous)
            raise
        finally:
            if self._last_committed_snapshot is not previous_commit:
                for target in affected:
                    self.invalidate_route(target.key)
                self._route_signatures = {key: self._route_signature(target) for key, target in self._targets.items()}
        self._cleanup_remote_gateway_if_unused(connection_id)
