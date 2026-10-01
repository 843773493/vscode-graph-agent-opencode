from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName
from app.schemas.gateway import GatewayConnectionKind

if TYPE_CHECKING:
    from app.gateway.registry import GatewayWorkspaceRegistry


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


class RegistryCoreMixin:
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
        self._commit_observers: list[Callable[[], None]] = []
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
