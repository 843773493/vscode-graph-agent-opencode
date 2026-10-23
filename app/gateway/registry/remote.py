from __future__ import annotations

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime


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
                if target.remote_gateway_connection_id == connection.connection_id:
                    self.invalidate_route(target.workspace_id)
        self._save(
            owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
        )

    def remote_gateway_connection(
        self,
        connection_id: str,
    ) -> RemoteGatewayConnection:
        connection = self._remote_gateway_connections.get(connection_id)
        if connection is None:
            raise LookupError(f"未知远程 Gateway 连接: {connection_id}")
        return connection
    def remote_gateway_url(self, connection_id: str) -> str:
        runtime = self._remote_gateway_runtimes.get(connection_id)
        if runtime is None:
            raise LookupError(f"远程 Gateway 隧道尚未连接: {connection_id}")
        return runtime.service_urls["workspace_api"]
    def remote_gateway_connections(self) -> tuple[RemoteGatewayConnection, ...]:
        return tuple(self._remote_gateway_connections.values())
    def _close_unused_remote_gateway(self, connection_id: str | None) -> None:
        if connection_id is None:
            return
        if any(
            target.remote_gateway_connection_id == connection_id
            for target in self._targets.values()
        ):
            return
        self._cleanup_remote_gateway_if_unused(connection_id)
    def _cleanup_remote_gateway_if_unused(self, connection_id: str) -> None:
        if self._has_route_references_for_connection(connection_id):
            return
        self._close_retired_remote_gateway_runtimes(connection_id)
        if any(
            target.remote_gateway_connection_id == connection_id
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
