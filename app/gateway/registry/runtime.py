from __future__ import annotations

from typing import Literal

from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.gateway.runtime.workspace import WorkspaceRuntime


class RegistryRuntimeMixin:
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
            runtimes = 0
            for workspace_id, runtime in self._runtimes.items():
                target = self.resolve(workspace_id)
                if target.connection_kind != "local":
                    continue
                runtime.assert_healthy()
                if runtime.gateway_generation not in {None, generation}:
                    raise RuntimeError(
                        "Gateway Workspace runtime generation 不匹配: "
                        f"workspace_id={workspace_id}, "
                        f"expected={generation}, actual={runtime.gateway_generation}"
                    )
                runtimes += 1
            details = {"local_runtime_count": runtimes}
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
    def prepare_workspace_process_generation(
        self,
        generation: str,
    ) -> dict[str, str | None]:
        if not generation.strip():
            raise ValueError("Workspace process generation 不能为空")
        previous: dict[str, str | None] = {}
        for workspace_id, runtime in self._runtimes.items():
            target = self.resolve(workspace_id)
            if target.connection_kind != "local":
                continue
            runtime.assert_healthy()
            previous[workspace_id] = runtime.gateway_generation
        return previous
    def apply_workspace_process_generation(
        self,
        generation: str,
    ) -> dict[str, str | None]:
        previous = self.prepare_workspace_process_generation(generation)
        for workspace_id in previous:
            self._runtimes[workspace_id].gateway_generation = generation
        return previous
    def promote_workspace_process_generation(self, generation: str) -> None:
        for workspace_id, runtime in self._runtimes.items():
            target = self.resolve(workspace_id)
            if (
                target.connection_kind == "local"
                and runtime.gateway_generation != generation
            ):
                raise RuntimeError(
                    "Workspace process generation promotion 不匹配: "
                    f"workspace_id={workspace_id}, expected={generation}, "
                    f"actual={runtime.gateway_generation}"
                )
    def rollback_workspace_process_generation(
        self,
        generation: str,
        previous: dict[str, str | None],
    ) -> None:
        for workspace_id, previous_generation in previous.items():
            runtime = self._runtimes.get(workspace_id)
            if runtime is None:
                raise RuntimeError(
                    "Workspace process generation 回退发现 runtime 已被替换: "
                    f"workspace_id={workspace_id}"
                )
            if runtime.gateway_generation == previous_generation:
                continue
            if runtime.gateway_generation != generation:
                raise RuntimeError(
                    "Workspace process generation 回退发现 runtime 已被替换: "
                    f"workspace_id={workspace_id}"
                )
            runtime.gateway_generation = previous_generation
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
    def stop_managed_runtime(self, workspace_id: str) -> None:
        target = self.resolve(workspace_id)
        if target.connection_kind != "local" or not target.managed:
            raise ValueError(f"工作区不属于当前 Gateway 的本地托管目标: {workspace_id}")
        if target.system_default or not target.removable:
            raise PermissionError(f"默认工作区不能关闭: {target.name}")
        self._assert_route_references_drained(workspace_id)
        runtime = self._runtimes.pop(workspace_id, None)
        if runtime is None:
            raise ValueError(f"工作区后端尚未启动: {target.name}")
        self.invalidate_route(workspace_id)
        runtime.close()
        target.desired_running = False
        target.connection_error = None
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = self._default_workspace_id()
        self._save(owner=target.owner)
    def close(
        self,
        *,
        preserve_browser_managers: bool = False,
        preserve_terminal_managers: bool = False,
        preserve_workspace_backends: bool = False,
    ) -> None:
        errors: list[str] = []
        for workspace_id in tuple(self._targets):
            self.invalidate_route(workspace_id)

        local_runtimes = list(self._runtimes.items())
        remote_runtimes = list(self._remote_gateway_runtimes.items())
        retired_remote_runtimes = [
            (connection_id, runtime)
            for connection_id, runtimes in self._retired_remote_gateway_runtimes.items()
            for runtime in runtimes
        ]
        if preserve_browser_managers:
            for _, runtime in local_runtimes:
                runtime.detach_process("browser_manager")
        if preserve_terminal_managers:
            for _, runtime in local_runtimes:
                runtime.detach_process("terminal_manager")
        if preserve_workspace_backends:
            for workspace_id, runtime in local_runtimes:
                target = self._targets.get(workspace_id)
                if target is not None and target.managed:
                    runtime.detach_process("workspace_api")

        for runtime_id, runtime in (
            *local_runtimes,
            *remote_runtimes,
            *retired_remote_runtimes,
        ):
            try:
                runtime.request_terminate()
            except Exception as error:
                errors.append(f"{runtime_id} 发送终止信号失败: {error}")

        for runtime_id, runtime in (
            *local_runtimes,
            *remote_runtimes,
            *retired_remote_runtimes,
        ):
            try:
                runtime.wait_closed()
            except Exception as error:
                errors.append(f"{runtime_id}: {error}")
        self._runtimes.clear()
        self._remote_gateway_runtimes.clear()
        if errors:
            raise RuntimeError("关闭 Gateway 托管进程失败: " + "; ".join(errors))
