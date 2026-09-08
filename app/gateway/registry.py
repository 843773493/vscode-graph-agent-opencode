from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.core.path_utils import get_gateway_root
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id,
    build_workspace_id,
    is_legacy_workspace_id,
)
from app.schemas.gateway import (
    GatewayConfigReloadStatusDTO,
    GatewayConnectionKind,
    GatewayRemoteConnectionSummaryDTO,
    GatewayServiceStatus,
    GatewayServiceStatusDTO,
    GatewayWorkspaceDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError

_REGISTRY_SCHEMA_VERSION = 10
_UNSET = object()
RegistryTargetOwner = Literal["config", "manual", "system", "remote_projection"]


@dataclass(slots=True)
class WorkspaceTarget:
    workspace_id: str
    name: str
    root_path: str
    backend_url: str
    connection_kind: GatewayConnectionKind
    parent_workspace_id: str | None = None
    owner: RegistryTargetOwner = "manual"
    target_namespace: str = "gateway"
    connection_id: str | None = None
    target_generation: str = ""
    runtime_lease_id: str | None = None
    active_request_count: int = 0
    active_stream_count: int = 0
    name_customized: bool = False
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    desired_running: bool = False
    remote_gateway_connection_id: str | None = None
    remote_workspace_id: str | None = None
    remote_config_event_cursor: int | None = None
    remote_service_names: tuple[GatewayServiceName, ...] = ()
    local_service_urls: dict[str, str] = field(default_factory=dict)
    connection_error: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRouteLease:
    workspace_id: str
    revision: int
    invalidated: asyncio.Event

    @property
    def token(self) -> str:
        return f"{self.workspace_id}:{self.revision}"


@dataclass(slots=True)
class GatewayRegistryBatchHandle:
    """一次 registry batch 的 promotion/rollback 句柄。"""

    registry: GatewayWorkspaceRegistry
    previous_snapshot: dict[str, object]
    previous_signatures: dict[str, tuple[object, ...]]
    previous_remote_runtimes: dict[str, WorkspaceRuntime]
    previous_retired_remote_runtimes: dict[str, list[WorkspaceRuntime]]
    staged_runtimes: dict[str, WorkspaceRuntime]
    retired_runtimes: tuple[tuple[str, WorkspaceRuntime], ...]
    committed_revision: int
    deferred_retirement: bool
    finished: bool = False

    def promote(self) -> None:
        """在所有消费者 proof 和最终 CAS 成功后关闭旧 remote runtime。"""

        if self.finished:
            return
        if self.deferred_retirement:
            errors: list[str] = []
            for connection_id, runtime in self.retired_runtimes:
                if self.registry._has_route_references_for_connection(connection_id):
                    continue
                try:
                    runtime.close()
                except Exception as error:
                    errors.append(f"{connection_id}: {error}")
            if errors:
                raise RuntimeError(
                    "Gateway registry batch promotion 关闭旧 remote runtime 失败: "
                    + "; ".join(errors)
                )
            self.registry._remove_retired_runtime_instances(self.retired_runtimes)
        self.finished = True

    def rollback(self) -> None:
        """恢复 batch 前的 registry 和 tunnel 句柄。"""

        if self.finished:
            return
        self.registry._rollback_registry_batch(self)
        self.finished = True


class GatewayWorkspaceRegistry:
    def __init__(
        self,
        *,
        storage_path: Path,
        state_store: GatewayStateStore | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._state_store = state_store
        self._targets: dict[str, WorkspaceTarget] = {}
        self._active_workspace_id: str | None = None
        self._registry_revision = 0
        self._order_customized = False
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._remote_gateway_connections: dict[str, RemoteGatewayConnection] = {}
        self._remote_gateway_runtimes: dict[str, WorkspaceRuntime] = {}
        self._retired_remote_gateway_runtimes: dict[str, list[WorkspaceRuntime]] = {}
        self._runtime_generation: str | None = None
        self._route_revisions: dict[str, int] = {}
        self._route_change_events: dict[str, asyncio.Event] = {}
        self._route_reference_counts: dict[str, tuple[int, int]] = {}
        self._route_reference_connections: dict[str, set[str]] = {}
        self._route_signatures: dict[str, tuple[object, ...]] = {}
        self._load()
        self._route_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }
        self._last_committed_snapshot = self._capture_registry_state()

    @property
    def active_workspace_id(self) -> str | None:
        return self._active_workspace_id

    @property
    def registry_revision(self) -> int:
        """返回最近一次持久化成功的 registry revision。"""
        return self._registry_revision

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

    def upsert(
        self,
        target: WorkspaceTarget,
        *,
        runtime: WorkspaceRuntime | None = None,
        activate: bool = True,
        mutation_owner: str | None = None,
    ) -> WorkspaceTarget:
        if mutation_owner is not None and mutation_owner not in {
            "config",
            "config_batch",
            "manual",
            "manual_crud",
            "remote_projection",
            "system",
            "registry",
        }:
            raise ValueError(f"Gateway registry mutation owner 非法: {mutation_owner}")
        existing = self._targets.get(target.workspace_id)
        preserve_generation = False
        self._validate_target_identity(target)
        if existing is not None:
            if runtime is not None and self._has_route_references(
                target.workspace_id
            ):
                runtime.close()
                raise RuntimeError(
                    "Gateway 不能替换仍有代理引用的工作区运行时，请先完成排空: "
                    f"workspace_id={target.workspace_id}"
                )
            # TODO: 旧内存夹具和早期注册记录可能只设置 system_default，未同步写入 system owner；仅允许默认目标完成这一次规范化。
            legacy_system_owner_migration = (
                existing.owner == "manual"
                and target.owner == "system"
                and existing.system_default
                and target.system_default
            )
            if target.owner != existing.owner and not legacy_system_owner_migration:
                raise PermissionError(
                    "Gateway registry 不允许通过 upsert 改变 target owner: "
                    f"workspace_id={target.workspace_id}, "
                    f"current={existing.owner}, requested={target.owner}"
                )
            if target.target_generation == "":
                target.target_generation = existing.target_generation
                preserve_generation = True
            if target.runtime_lease_id is None:
                target.runtime_lease_id = existing.runtime_lease_id
        if target.target_generation == "":
            target.target_generation = f"target_generation_{uuid4().hex}"
        route_signature = self._route_signature(target)
        route_changed = (
            target.workspace_id not in self._targets
            or runtime is not None
            or self._route_signatures.get(target.workspace_id) != route_signature
        )
        if route_changed and existing is not None and preserve_generation:
            target.target_generation = f"target_generation_{uuid4().hex}"
        if runtime is not None:
            target.runtime_lease_id = f"runtime_lease_{uuid4().hex}"
        self._targets[target.workspace_id] = target
        if runtime is not None:
            if target.connection_kind == "local" and target.managed:
                target.desired_running = True
            previous = self._runtimes.pop(target.workspace_id, None)
            if previous is not None:
                previous_browser_url = previous.service_urls.get("browser_manager")
                replacement_browser_url = runtime.service_urls.get("browser_manager")
                if (
                    previous_browser_url is not None
                    and previous_browser_url == replacement_browser_url
                ):
                    previous.detach_process("browser_manager")
                previous_terminal_url = previous.service_urls.get("terminal_manager")
                replacement_terminal_url = runtime.service_urls.get("terminal_manager")
                if (
                    previous_terminal_url is not None
                    and previous_terminal_url == replacement_terminal_url
                ):
                    previous.detach_process("terminal_manager")
                previous.close()
            self._runtimes[target.workspace_id] = runtime
        self._route_signatures[target.workspace_id] = route_signature
        if route_changed:
            self.invalidate_route(target.workspace_id)
        if activate or self._active_workspace_id is None:
            self._active_workspace_id = target.workspace_id
        self._save(owner=mutation_owner or target.owner)
        return target

    @staticmethod
    def _validate_target_identity(target: WorkspaceTarget) -> None:
        if target.owner not in {"config", "manual", "system", "remote_projection"}:
            raise ValueError(f"Gateway registry target owner 非法: {target.owner}")
        if not target.target_namespace.strip():
            raise ValueError("Gateway registry target namespace 不能为空")
        if target.connection_id is not None and not target.connection_id.strip():
            raise ValueError("Gateway registry target connection_id 不能为空")
        if target.owner == "remote_projection" and (
            target.connection_id is None
            or target.remote_workspace_id is None
        ):
            raise ValueError(
                "remote_projection target 必须包含 connection_id 和 remote_workspace_id"
            )
        if target.active_request_count < 0 or target.active_stream_count < 0:
            raise ValueError("Gateway registry 活动引用计数不能为负数")

    def _capture_registry_state(self) -> dict[str, object]:
        return {
            "targets": deepcopy(self._targets),
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "remote_gateway_connections": deepcopy(self._remote_gateway_connections),
            "registry_revision": self._registry_revision,
            "runtime_generation": self._runtime_generation,
        }

    def _restore_registry_state(self, snapshot: dict[str, object]) -> None:
        self._targets = deepcopy(snapshot["targets"])
        self._active_workspace_id = snapshot["active_workspace_id"]
        self._order_customized = bool(snapshot["order_customized"])
        self._remote_gateway_connections = deepcopy(
            snapshot["remote_gateway_connections"]
        )
        self._registry_revision = int(snapshot["registry_revision"])
        runtime_generation = snapshot.get("runtime_generation")
        if runtime_generation is not None and not isinstance(runtime_generation, str):
            raise TypeError("Gateway registry runtime_generation 必须是字符串或 null")
        self._runtime_generation = runtime_generation
        self._route_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }

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

    def remove(self, workspace_id: str, *, owner: str | None = None) -> None:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if not target.removable or target.system_default:
            raise PermissionError(f"默认工作区不能删除: {target.name}")
        mutation_owner = owner or target.owner
        scope_owner = self._owner_scope(mutation_owner)
        if scope_owner is not None and scope_owner != target.owner:
            raise PermissionError(
                "Gateway registry 删除不能越过 target owner: "
                f"workspace_id={workspace_id}, current={target.owner}, "
                f"requested={scope_owner}"
            )
        if target.connection_kind == "local":
            self._assert_route_references_drained(workspace_id)
        self.invalidate_route(workspace_id)
        runtime = self._runtimes.pop(workspace_id, None)
        if runtime is not None:
            runtime.close()
        for child in self._targets.values():
            if child.parent_workspace_id == workspace_id:
                child.parent_workspace_id = None
        del self._targets[workspace_id]
        self._route_signatures.pop(workspace_id, None)
        self._close_unused_remote_gateway(target.remote_gateway_connection_id)
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = self._default_workspace_id()
        self._save(owner=mutation_owner)

    def remove_backend_aliases(self, *, backend_url: str, keep_workspace_id: str) -> None:
        normalized_backend_url = backend_url.rstrip("/")
        for workspace_id, target in self._targets.items():
            if (
                workspace_id != keep_workspace_id
                and target.connection_kind == "local"
                and target.backend_url.rstrip("/") == normalized_backend_url
            ):
                self._assert_route_references_drained(workspace_id)
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id:
                continue
            if target.backend_url.rstrip("/") != normalized_backend_url:
                continue
            runtime = self._runtimes.pop(workspace_id, None)
            if runtime is not None:
                runtime.close()
            self.invalidate_route(workspace_id)
            del self._targets[workspace_id]
            self._route_signatures.pop(workspace_id, None)
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = self._default_workspace_id()
            changed = True
        if changed:
            self._save(owner="system")

    def remove_system_default_aliases(self, *, keep_workspace_id: str) -> None:
        for workspace_id, target in self._targets.items():
            if (
                workspace_id != keep_workspace_id
                and target.system_default
                and target.connection_kind == "local"
            ):
                self._assert_route_references_drained(workspace_id)
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id or not target.system_default:
                continue
            runtime = self._runtimes.pop(workspace_id, None)
            if runtime is not None:
                runtime.close()
            self.invalidate_route(workspace_id)
            del self._targets[workspace_id]
            self._route_signatures.pop(workspace_id, None)
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = keep_workspace_id
            changed = True
        if changed:
            self._save(owner="system")

    def ensure_default_workspace_first(self) -> None:
        if self._order_customized:
            return
        default_workspace_id = self._default_workspace_id()
        if default_workspace_id is None:
            return
        first_workspace_id = next(iter(self._targets), None)
        if first_workspace_id == default_workspace_id:
            return
        self._targets = {
            default_workspace_id: self._targets[default_workspace_id],
            **{
                workspace_id: target
                for workspace_id, target in self._targets.items()
                if workspace_id != default_workspace_id
            },
        }
        self._save(owner="system")

    def reorder(self, workspace_ids: list[str]) -> None:
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("Gateway 工作区排序列表包含重复 ID")
        known_workspace_ids = set(self._targets)
        requested_workspace_ids = set(workspace_ids)
        unknown_workspace_ids = sorted(requested_workspace_ids - known_workspace_ids)
        missing_workspace_ids = sorted(known_workspace_ids - requested_workspace_ids)
        if unknown_workspace_ids:
            raise ValueError(f"Gateway 工作区排序包含未知 ID: {', '.join(unknown_workspace_ids)}")
        if missing_workspace_ids:
            raise ValueError(f"Gateway 工作区排序缺少 ID: {', '.join(missing_workspace_ids)}")
        self._targets = {
            workspace_id: self._targets[workspace_id]
            for workspace_id in workspace_ids
        }
        self._order_customized = True
        self._save(owner="manual_crud")

    def activate(self, workspace_id: str) -> None:
        if workspace_id not in self._targets:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        self._active_workspace_id = workspace_id
        self._save(owner="manual_crud")

    def rename(self, workspace_id: str, name: str) -> WorkspaceTarget:
        return self.update(workspace_id, name=name)

    def set_parent(
        self,
        workspace_id: str,
        parent_workspace_id: str | None,
    ) -> WorkspaceTarget:
        return self.update(
            workspace_id,
            parent_workspace_id=parent_workspace_id,
        )

    def update(
        self,
        workspace_id: str,
        *,
        name: str | object = _UNSET,
        parent_workspace_id: str | None | object = _UNSET,
    ) -> WorkspaceTarget:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if target.owner not in {"manual", "system"} or (
            target.owner == "system" and not target.system_default
        ):
            raise PermissionError(
                "Gateway manual CRUD 只能修改 manual target 或 system default target: "
                f"workspace_id={workspace_id}, owner={target.owner}"
            )

        normalized_name: str | None = None
        if name is not _UNSET:
            if not isinstance(name, str):
                raise TypeError("Gateway 工作区名称必须是字符串")
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Gateway 工作区名称不能为空")

        normalized_parent_workspace_id: str | None = None
        if parent_workspace_id is not _UNSET:
            if parent_workspace_id is not None and not isinstance(
                parent_workspace_id,
                str,
            ):
                raise TypeError("Gateway 父工作区 ID 必须是字符串或 null")
            normalized_parent_workspace_id = parent_workspace_id
            if normalized_parent_workspace_id == workspace_id:
                raise ValueError("工作区不能成为自己的父工作区")
            if (
                normalized_parent_workspace_id is not None
                and normalized_parent_workspace_id not in self._targets
            ):
                raise KeyError(
                    f"未知 Gateway 父工作区: {normalized_parent_workspace_id}"
                )
            ancestor_id = normalized_parent_workspace_id
            while ancestor_id is not None:
                if ancestor_id == workspace_id:
                    raise ValueError("工作区父子关系不能形成循环")
                ancestor_id = self._targets[ancestor_id].parent_workspace_id

        if normalized_name is not None:
            target.name = normalized_name
            target.name_customized = True
        if parent_workspace_id is not _UNSET:
            target.parent_workspace_id = normalized_parent_workspace_id
        self._save(owner="manual_crud")
        return target

    def resolve(self, workspace_id: str | None = None) -> WorkspaceTarget:
        target_id = workspace_id or self._active_workspace_id
        if target_id is None:
            raise LookupError("Gateway 尚未注册任何工作区")
        target = self._targets.get(target_id)
        if target is None:
            raise LookupError(f"Gateway 工作区不存在: {target_id}")
        return target

    @staticmethod
    def _route_signature(target: WorkspaceTarget) -> tuple[object, ...]:
        return (
            target.connection_kind,
            target.backend_url.rstrip("/"),
            target.remote_gateway_connection_id,
            target.remote_workspace_id,
        )

    def route_lease(self, workspace_id: str) -> WorkspaceRouteLease:
        self.resolve(workspace_id)
        revision = self._route_revisions.get(workspace_id, 0)
        invalidated = self._route_change_events.get(workspace_id)
        if invalidated is None:
            invalidated = asyncio.Event()
            self._route_change_events[workspace_id] = invalidated
        return WorkspaceRouteLease(
            workspace_id=workspace_id,
            revision=revision,
            invalidated=invalidated,
        )

    def acquire_route_reference(
        self,
        workspace_id: str,
        *,
        streaming: bool,
    ) -> WorkspaceRouteLease:
        """为代理请求保留路由引用，直到响应体或长连接真正结束。"""

        lease = self.route_lease(workspace_id)
        requests, streams = self._route_reference_counts.get(workspace_id, (0, 0))
        if streaming:
            streams += 1
        else:
            requests += 1
        self._route_reference_counts[workspace_id] = (requests, streams)
        target_connection_id = self.resolve(workspace_id).remote_gateway_connection_id
        if target_connection_id is not None:
            self._route_reference_connections.setdefault(workspace_id, set()).add(
                target_connection_id
            )
        return lease

    def release_route_reference(
        self,
        workspace_id: str,
        *,
        streaming: bool,
    ) -> None:
        """释放代理请求引用；计数不写入 registry 持久化快照。"""

        requests, streams = self._route_reference_counts.get(workspace_id, (0, 0))
        if streaming:
            if streams == 0:
                raise RuntimeError(f"Gateway 流引用重复释放: {workspace_id}")
            streams -= 1
        else:
            if requests == 0:
                raise RuntimeError(f"Gateway 请求引用重复释放: {workspace_id}")
            requests -= 1
        if requests or streams:
            self._route_reference_counts[workspace_id] = (requests, streams)
        else:
            self._route_reference_counts.pop(workspace_id, None)
            connection_ids = self._route_reference_connections.pop(workspace_id, set())
            for connection_id in connection_ids:
                self._cleanup_remote_gateway_if_unused(connection_id)

    def route_reference_counts(self, workspace_id: str) -> tuple[int, int]:
        """返回当前 Gateway 代理持有的普通请求数与长连接数。"""

        self.resolve(workspace_id)
        return self._route_reference_counts.get(workspace_id, (0, 0))

    def _has_route_references(self, workspace_id: str) -> bool:
        return any(self._route_reference_counts.get(workspace_id, (0, 0)))

    def _assert_route_references_drained(self, workspace_id: str) -> None:
        request_count, stream_count = self._route_reference_counts.get(
            workspace_id,
            (0, 0),
        )
        if request_count or stream_count:
            raise RuntimeError(
                "Gateway 不能关闭仍有代理引用的工作区运行时，请先完成排空: "
                f"workspace_id={workspace_id}, requests={request_count}, "
                f"streams={stream_count}"
            )

    def invalidate_route(self, workspace_id: str) -> None:
        """使现有代理租约失效，强制长连接重新解析当前工作区路由。"""
        self._route_revisions[workspace_id] = (
            self._route_revisions.get(workspace_id, 0) + 1
        )
        previous = self._route_change_events.get(workspace_id)
        if previous is not None:
            previous.set()
        self._route_change_events[workspace_id] = asyncio.Event()

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
            and existing_connection.source_owner != "legacy"
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
            previous = self._remote_gateway_runtimes.pop(connection.connection_id, None)
            if previous is not None:
                if self._has_route_references_for_connection(connection.connection_id):
                    self._retired_remote_gateway_runtimes.setdefault(
                        connection.connection_id,
                        [],
                    ).append(previous)
                else:
                    try:
                        previous.close()
                    except Exception:
                        self._retired_remote_gateway_runtimes.setdefault(
                            connection.connection_id,
                            [],
                        ).append(previous)
            self._remote_gateway_runtimes[connection.connection_id] = runtime
            for target in self._targets.values():
                if (
                    target.remote_gateway_connection_id
                    == connection.connection_id
                ):
                    self.invalidate_route(target.workspace_id)
        self._save(owner="remote_projection")

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
        if connection.source_owner not in {"config", "manual", "legacy"}:
            raise PermissionError("远程 Gateway 连接 source owner 非法")
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
            and existing_connection.source_owner != "legacy"
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
            if connection.source_owner in {"manual", "legacy"}
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
                and (
                    connection.source_owner == "config"
                    or (
                        connection.source_owner == "legacy"
                        and target.remote_gateway_connection_id
                        in configured_connection_by_id
                    )
                )
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
            if connection is not None and connection.source_owner in {
                "manual",
                "legacy",
            }:
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

    def _remove_retired_runtime_instances(
        self,
        runtimes: tuple[tuple[str, WorkspaceRuntime], ...],
    ) -> None:
        for connection_id, runtime in runtimes:
            retained = [
                item
                for item in self._retired_remote_gateway_runtimes.get(
                    connection_id,
                    [],
                )
                if item is not runtime
            ]
            if retained:
                self._retired_remote_gateway_runtimes[connection_id] = retained
            else:
                self._retired_remote_gateway_runtimes.pop(connection_id, None)

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
        runtime = self._remote_gateway_runtimes.pop(connection_id, None)
        if runtime is not None:
            runtime.close()
        self._remote_gateway_connections.pop(connection_id, None)
        FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).remove(connection_id)

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
        retired = self._retired_remote_gateway_runtimes.pop(connection_id, [])
        for runtime in retired:
            runtime.close()

    def resolve_service_url(
        self,
        workspace_id: str,
        service: GatewayServiceName,
    ) -> str:
        target = self.resolve(workspace_id)
        if target.connection_kind == "remote_gateway":
            connection_id = target.remote_gateway_connection_id
            remote_workspace_id = target.remote_workspace_id
            if connection_id is None or remote_workspace_id is None:
                raise RuntimeError(f"远程投影工作区缺少所属 Gateway 信息: {workspace_id}")
            gateway_url = self.remote_gateway_url(connection_id)
            if service not in target.remote_service_names:
                raise LookupError(
                    f"远程工作区未提供服务: workspace_id={workspace_id}, service={service}"
                )
            if service == "workspace_api":
                return gateway_url
            service_path = (
                "terminal-manager"
                if service == "terminal_manager"
                else "browser-manager"
            )
            return (
                f"{gateway_url}/api/gateway/workspaces/"
                f"{remote_workspace_id}/{service_path}"
            )
        if (
            not target.managed
            and service == "workspace_api"
            and target.backend_url
        ):
            # 外部编排的本地后端没有 Gateway runtime，但仍然可以直接通过
            # 持久化的 backend_url 代理工作区 API。
            return target.backend_url
        runtime = self._runtimes.get(target.workspace_id)
        if runtime is None:
            raise LookupError(f"工作区运行时尚未连接: {workspace_id}")
        service_url = runtime.service_urls.get(service)
        if service_url is None:
            raise LookupError(
                f"工作区未提供服务: workspace_id={workspace_id}, service={service}"
            )
        return service_url

    def targets(self) -> tuple[WorkspaceTarget, ...]:
        return tuple(self._targets.values())

    def has_target(self, workspace_id: str) -> bool:
        return workspace_id in self._targets

    def mark_connection_error(self, workspace_id: str, error: str) -> None:
        target = self.resolve(workspace_id)
        target.connection_error = error
        self._save(owner=target.owner)

    async def list_dtos(self, *, check_health: bool = True) -> list[GatewayWorkspaceDTO]:
        targets = list(self._targets.values())
        async with httpx.AsyncClient(timeout=2) as client:
            async def build_dto(target: WorkspaceTarget) -> GatewayWorkspaceDTO:
                runtime = self._runtimes.get(target.workspace_id)
                runtime_service_urls = (
                    dict(runtime.service_urls) if runtime is not None else {}
                )
                if target.connection_kind == "remote_gateway":
                    for service in (
                        "workspace_api",
                        "terminal_manager",
                        "browser_manager",
                    ):
                        try:
                            runtime_service_urls[service] = self.resolve_service_url(
                                target.workspace_id,
                                service,
                            )
                        except LookupError:
                            continue
                status = "ready" if "workspace_api" in runtime_service_urls else "offline"
                workspace_service_status: GatewayServiceStatus = (
                    "ready" if status == "ready" else "offline"
                )
                workspace_service_error: str | None = (
                    None
                    if status == "ready"
                    else target.connection_error
                    or f"工作区运行时尚未连接: {target.workspace_id}"
                )
                backend_url: str | None = None
                if check_health:
                    try:
                        backend_url = self.resolve_service_url(
                            target.workspace_id,
                            "workspace_api",
                        )
                        response = await client.get(
                            f"{backend_url.rstrip('/')}/api/v1/health",
                            headers=self._target_headers(target),
                        )
                        if response.status_code == 200:
                            status = "ready"
                            workspace_service_status = "ready"
                            workspace_service_error = None
                        else:
                            status = "offline"
                            workspace_service_status = "offline"
                            workspace_service_error = (
                                f"健康检查返回 HTTP {response.status_code}"
                            )
                    except Exception as error:
                        status = "offline"
                        workspace_service_status = "offline"
                        workspace_service_error = str(error)
                config_reload = GatewayConfigReloadStatusDTO()
                if check_health and status == "ready":
                    if backend_url is None:
                        raise RuntimeError(
                            f"工作区健康检查已通过但缺少后端地址: {target.workspace_id}"
                        )
                    try:
                        config_response = await client.get(
                            f"{backend_url.rstrip('/')}/api/v1/config/reload-status",
                            headers=self._target_headers(target),
                        )
                        if config_response.status_code != 200:
                            raise RuntimeError(
                                "配置状态接口返回 HTTP "
                                f"{config_response.status_code}"
                            )
                        config_payload = config_response.json()
                        config_data = config_payload.get("data")
                        if not isinstance(config_data, dict):
                            raise ValueError("配置状态接口缺少 data 对象")
                        config_reload = GatewayConfigReloadStatusDTO(
                            available=True,
                            healthy=config_data.get("healthy"),
                            revision=config_data.get("revision"),
                            restart_required=bool(
                                config_data.get("restart_required", False)
                            ),
                            reason=config_data.get("reason"),
                            changed_sections=list(
                                config_data.get("changed_sections", [])
                            ),
                            last_error=config_data.get("last_error"),
                        )
                    except Exception as error:
                        config_reload = GatewayConfigReloadStatusDTO(
                            available=False,
                            error=str(error),
                        )
                health_paths: dict[GatewayServiceName, str] = {
                    "workspace_api": "/api/v1/health",
                    "terminal_manager": "/health",
                    "browser_manager": "/health",
                }
                def service_dto(
                    service: GatewayServiceName,
                    service_status: GatewayServiceStatus,
                    *,
                    error: str | None = None,
                ) -> GatewayServiceStatusDTO:
                    local_url = (
                        runtime_service_urls.get(service)
                    )
                    parsed_url = urlparse(local_url) if local_url is not None else None
                    return GatewayServiceStatusDTO(
                        status=service_status,
                        health_path=health_paths[service],
                        local_url=local_url,
                        local_port=parsed_url.port if parsed_url is not None else None,
                        error=error,
                    )

                service_statuses: dict[str, GatewayServiceStatusDTO] = {
                    "workspace_api": service_dto(
                        "workspace_api",
                        workspace_service_status,
                        error=workspace_service_error,
                    )
                }
                for service, health_path in health_paths.items():
                    if service == "workspace_api":
                        continue
                    if service not in runtime_service_urls:
                        service_statuses[service] = service_dto(
                            service,
                            "unavailable",
                        )
                        continue
                    if not check_health:
                        service_statuses[service] = service_dto(service, "ready")
                        continue
                    service_url = runtime_service_urls[service]
                    try:
                        response = await client.get(
                            f"{service_url.rstrip('/')}{health_path}",
                            headers=self._target_headers(target),
                        )
                        service_statuses[service] = service_dto(
                            service,
                            "ready" if response.status_code == 200 else "offline",
                            error=(
                                None
                                if response.status_code == 200
                                else f"健康检查返回 HTTP {response.status_code}"
                            ),
                        )
                    except Exception as error:
                        service_statuses[service] = service_dto(
                            service,
                            "offline",
                            error=str(error),
                        )
                remote_connection = (
                    self.remote_gateway_connection(
                        target.remote_gateway_connection_id
                    )
                    if target.remote_gateway_connection_id is not None
                    else None
                )
                return GatewayWorkspaceDTO(
                    workspace_id=target.workspace_id,
                    parent_workspace_id=target.parent_workspace_id,
                    name=target.name,
                    root_path=target.root_path,
                    backend_url=target.backend_url,
                    connection_kind=target.connection_kind,
                    status=status,
                    active=target.workspace_id == self._active_workspace_id,
                    managed=target.managed,
                    removable=target.removable,
                    system_default=target.system_default,
                    runtime_action=(
                        (
                            "reconnect_remote_gateway"
                            if target.connection_error
                            else (
                                "safe_restart_managed_backend"
                                if target.managed
                                else "probe_external_backend"
                            )
                        )
                        if target.connection_kind == "remote_gateway"
                        else (
                            (
                                "safe_restart_managed_backend"
                                if runtime is not None
                                else "start_managed_backend"
                            )
                            if target.managed
                            else "probe_external_backend"
                        )
                    ),
                    config_reload=config_reload,
                    remote=(
                        GatewayRemoteConnectionSummaryDTO(
                            gateway_connection_id=remote_connection.connection_id,
                            remote_workspace_id=target.remote_workspace_id,
                            gateway_id=remote_connection.remote_gateway_id,
                            name=remote_connection.name,
                            host=remote_connection.host,
                            port=remote_connection.port,
                            username=remote_connection.username,
                            ssh_config_host=remote_connection.ssh_config_host,
                            remote_gateway_port=remote_connection.remote_gateway_port,
                            config_event_cursor=(
                                remote_connection.remote_config_event_cursor
                            ),
                            config_reload_state=remote_connection.remote_config_state,
                            restart_required=remote_connection.remote_restart_required,
                            candidate_ref=remote_connection.remote_candidate_ref,
                        )
                        if target.connection_kind == "remote_gateway"
                        and remote_connection is not None
                        and target.remote_workspace_id is not None
                        else None
                    ),
                    connection_error=target.connection_error,
                    services=service_statuses,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )

            return list(await asyncio.gather(*(build_dto(target) for target in targets)))

    @staticmethod
    def _target_headers(target: WorkspaceTarget) -> dict[str, str]:
        if target.connection_kind != "remote_gateway":
            return {"X-Local-Token": "local-dev-token"}
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError(
                f"远程投影工作区缺少连接信息: {target.workspace_id}"
            )
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        return {
            "X-BoxTeam-Federation-Token": credential.token,
            "X-BoxTeam-Workspace-Id": remote_workspace_id,
        }

    def _default_workspace_id(self) -> str | None:
        for target in self._targets.values():
            if target.system_default:
                return target.workspace_id
        return next(iter(self._targets), None)

    @staticmethod
    def _migrate_legacy_workspace_id(
        *,
        workspace_id: str,
        root_path: str,
        backend_url: str,
        connection_kind: GatewayConnectionKind,
        managed: bool,
        system_default: bool,
        schema_version: int,
    ) -> str:
        if (
            schema_version < 6
            and connection_kind == "local"
            and (managed or system_default)
        ):
            return build_managed_local_workspace_id(root_path)
        if not is_legacy_workspace_id(workspace_id):
            return workspace_id
        return build_workspace_id(
            connection_kind,
            root_path,
            backend_url,
        )

    def _load(self) -> None:
        state_payload = (
            self._state_store.load_workspace_registry()
            if self._state_store is not None
            else None
        )
        migrated_to_sqlite = state_payload is None and self._state_store is not None
        if state_payload is not None:
            payload = state_payload
            self._registry_revision = int(payload.get("registry_revision", 0))
        elif not self._storage_path.exists():
            return
        else:
            with self._storage_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        schema_version = payload.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError(
                f"Gateway registry schema_version 必须是正整数: {self._storage_path}"
            )
        if schema_version > _REGISTRY_SCHEMA_VERSION:
            raise ValueError(
                "Gateway registry 版本高于当前程序支持范围: "
                f"version={schema_version}, supported={_REGISTRY_SCHEMA_VERSION}"
            )
        runtime_generation = payload.get("runtime_generation")
        if runtime_generation is not None and not isinstance(runtime_generation, str):
            raise ValueError("Gateway registry runtime_generation 必须是字符串或 null")
        self._runtime_generation = runtime_generation
        raw_remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(raw_remote_connections, list):
            raise ValueError("Gateway registry remote_gateway_connections 必须是数组")
        for item in raw_remote_connections:
            if not isinstance(item, dict):
                raise ValueError("Gateway registry 远程 Gateway 连接必须是对象")
            connection = RemoteGatewayConnection(
                connection_id=str(item["connection_id"]),
                name=str(item["name"]),
                host=str(item["host"]),
                port=int(item["port"]),
                username=str(item["username"]),
                private_key_path=(
                    str(item["private_key_path"])
                    if item.get("private_key_path") is not None
                    else None
                ),
                ssh_config_host=(
                    str(item["ssh_config_host"])
                    if item.get("ssh_config_host") is not None
                    else None
                ),
                remote_gateway_port=int(item["remote_gateway_port"]),
                remote_gateway_id=str(item["remote_gateway_id"]),
                protocol_version=int(item["protocol_version"]),
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
                remote_pair_command=(
                    str(item["remote_pair_command"])
                    if item.get("remote_pair_command") is not None
                    else None
                ),
                source_owner=(
                    item.get("source_owner")
                    if item.get("source_owner") in {"config", "manual", "legacy"}
                    else "legacy"
                ),
            )
            self._remote_gateway_connections[connection.connection_id] = connection
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"Gateway registry targets 必须是数组: {self._storage_path}")
        persisted_active_id = payload.get("active_workspace_id")
        workspace_id_remap: dict[str, str] = {}
        parent_workspace_ids: dict[str, str | None] = {}
        migrated = schema_version < _REGISTRY_SCHEMA_VERSION
        for item in targets:
            if not isinstance(item, dict):
                raise ValueError(f"Gateway registry target 必须是对象: {self._storage_path}")
            original_workspace_id = str(item["workspace_id"])
            root_path = str(item["root_path"])
            backend_url = str(item["backend_url"])
            connection_kind = item["connection_kind"]
            if connection_kind == "ssh":
                raise ValueError(
                    "检测到旧 SSH 直连后端注册记录。该模式已移除；请删除旧远程"
                    "工作区并重新添加“远程 Gateway 连接”。旧字段 "
                    "remote_backend_host/remote_backend_port/remote_services "
                    "不能自动迁移。"
                )
            if connection_kind not in {"local", "remote_gateway"}:
                raise ValueError(
                    "Gateway registry connection_kind 非法: "
                    f"workspace_id={original_workspace_id}, kind={connection_kind}"
                )
            managed = bool(item.get("managed", False))
            system_default = bool(item.get("system_default", False))
            if schema_version >= 9:
                desired_running_value = item.get("desired_running", False)
                if not isinstance(desired_running_value, bool):
                    raise ValueError(
                        "Gateway registry desired_running 必须是布尔值: "
                        f"workspace_id={original_workspace_id}"
                    )
                desired_running = desired_running_value
            else:
                # TODO: schema<9 没有记录显式启动意图，只能可靠迁移当前激活的
                # 本地托管工作区；完成一次性迁移后移除此兼容分支。
                desired_running = (
                    connection_kind == "local"
                    and managed
                    and original_workspace_id == persisted_active_id
                )
            # TODO: 旧 12 位 Gateway ID 完成一次性迁移后，在下一个持久化格式大版本移除。
            workspace_id = self._migrate_legacy_workspace_id(
                workspace_id=original_workspace_id,
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                managed=managed,
                system_default=system_default,
                schema_version=schema_version,
            )
            if workspace_id in self._targets:
                raise ValueError(
                    "Gateway registry 工作区 ID 迁移后发生冲突: "
                    f"original={original_workspace_id}, migrated={workspace_id}"
                )
            workspace_id_remap[original_workspace_id] = workspace_id
            raw_parent_workspace_id = item.get("parent_workspace_id")
            if raw_parent_workspace_id is not None and not isinstance(
                raw_parent_workspace_id,
                str,
            ):
                raise ValueError(
                    "Gateway registry parent_workspace_id 必须是字符串或 null: "
                    f"workspace_id={original_workspace_id}"
                )
            parent_workspace_ids[workspace_id] = raw_parent_workspace_id
            migrated = migrated or workspace_id != original_workspace_id
            raw_local_service_urls = item.get("local_service_urls", {})
            if not isinstance(raw_local_service_urls, dict) or not all(
                isinstance(name, str) and isinstance(url, str)
                for name, url in raw_local_service_urls.items()
            ):
                raise ValueError(
                    "Gateway registry local_service_urls 必须是字符串映射: "
                    f"workspace_id={original_workspace_id}"
                )
            target = WorkspaceTarget(
                workspace_id=workspace_id,
                name=str(item["name"]),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                owner=(
                    item.get("owner")
                    if item.get("owner") in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }
                    else (
                        "remote_projection"
                        if connection_kind == "remote_gateway"
                        else "system"
                        if system_default
                        else "manual"
                    )
                ),
                target_namespace=str(item.get("target_namespace", "gateway")),
                connection_id=(
                    str(item["connection_id"])
                    if item.get("connection_id") is not None
                    else (
                        str(item["remote_gateway_connection_id"])
                        if connection_kind == "remote_gateway"
                        and item.get("remote_gateway_connection_id") is not None
                        else None
                    )
                ),
                target_generation=str(
                    item.get("target_generation") or "target_generation_legacy"
                ),
                runtime_lease_id=(
                    str(item["runtime_lease_id"])
                    if item.get("runtime_lease_id") is not None
                    else None
                ),
                active_request_count=int(item.get("active_request_count", 0)),
                active_stream_count=int(item.get("active_stream_count", 0)),
                # TODO: 所有 schema<4 的 Registry 完成一次性迁移后，在下一个持久化格式大版本移除默认值。
                name_customized=bool(item.get("name_customized", False)),
                managed=managed,
                removable=bool(item.get("removable", True)),
                system_default=system_default,
                desired_running=desired_running,
                remote_gateway_connection_id=(
                    str(item["remote_gateway_connection_id"])
                    if item.get("remote_gateway_connection_id") is not None
                    else None
                ),
                remote_workspace_id=(
                    str(item["remote_workspace_id"])
                    if item.get("remote_workspace_id") is not None
                    else None
                ),
                remote_config_event_cursor=(
                    int(item["remote_config_event_cursor"])
                    if item.get("remote_config_event_cursor") is not None
                    else None
                ),
                remote_service_names=tuple(item.get("remote_service_names", ())),
                local_service_urls={
                    str(name): str(url)
                    for name, url in raw_local_service_urls.items()
                },
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
            )
            self._validate_target_identity(target)
            if target.connection_kind == "remote_gateway":
                if (
                    target.remote_gateway_connection_id is None
                    or target.remote_workspace_id is None
                ):
                    raise ValueError(
                        "远程投影工作区缺少 remote_gateway_connection_id 或 "
                        f"remote_workspace_id: {target.workspace_id}"
                    )
                if (
                    target.remote_gateway_connection_id
                    not in self._remote_gateway_connections
                ):
                    raise ValueError(
                        "远程投影工作区引用未知 Gateway 连接: "
                        f"{target.remote_gateway_connection_id}"
                    )
            self._targets[target.workspace_id] = target
        for workspace_id, raw_parent_workspace_id in parent_workspace_ids.items():
            if raw_parent_workspace_id is None:
                continue
            parent_workspace_id = workspace_id_remap.get(
                raw_parent_workspace_id,
                raw_parent_workspace_id,
            )
            if parent_workspace_id not in self._targets:
                raise ValueError(
                    "Gateway registry 父工作区不存在: "
                    f"workspace_id={workspace_id}, parent_workspace_id={parent_workspace_id}"
                )
            self._targets[workspace_id].parent_workspace_id = parent_workspace_id
        self._validate_parent_graph()
        migrated_active_id = (
            workspace_id_remap.get(persisted_active_id, persisted_active_id)
            if isinstance(persisted_active_id, str)
            else None
        )
        if migrated_active_id is not None and migrated_active_id in self._targets:
            self._active_workspace_id = migrated_active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))
        self._order_customized = bool(payload.get("order_customized", False))
        if migrated or migrated_to_sqlite:
            if migrated_to_sqlite and self._storage_path.is_file():
                backup_path = self._storage_path.with_name(
                    f"{self._storage_path.name}.migrated.bak"
                )
                if not backup_path.exists():
                    shutil.copy2(self._storage_path, backup_path)
            self._save()

    def _validate_parent_graph(self) -> None:
        for workspace_id in self._targets:
            visited: set[str] = set()
            current_id: str | None = workspace_id
            while current_id is not None:
                if current_id in visited:
                    raise ValueError(
                        "Gateway registry 工作区父子关系形成循环: "
                        f"workspace_id={workspace_id}"
                    )
                visited.add(current_id)
                current_id = self._targets[current_id].parent_workspace_id

    @staticmethod
    def _owner_scope(owner: str) -> str | None:
        return {
            "config": "config",
            "config_batch": "config",
            "manual": "manual",
            "manual_crud": "manual",
            "system": "system",
            "remote_projection": "remote_projection",
        }.get(owner)

    def _save(self, *, owner: str = "registry") -> None:
        previous_state = getattr(self, "_last_committed_snapshot", None)
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "runtime_generation": self._runtime_generation,
            "remote_gateway_connections": [
                asdict(connection)
                for connection in self._remote_gateway_connections.values()
            ],
            "targets": [asdict(target) for target in self._targets.values()],
        }
        if self._state_store is not None:
            try:
                self._registry_revision = self._state_store.replace_workspace_registry(
                    payload,
                    expected_revision=self._registry_revision,
                    owner=owner,
                )
            except Exception:
                if previous_state is not None:
                    self._restore_registry_state(previous_state)
                raise
            self._last_committed_snapshot = self._capture_registry_state()
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._storage_path.name}.",
            dir=self._storage_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
            if os.name != "nt":
                temporary_path.chmod(0o600)
            os.replace(temporary_path, self._storage_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        self._last_committed_snapshot = self._capture_registry_state()
