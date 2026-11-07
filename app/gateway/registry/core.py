from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.core.identifier import validate_gateway_id
from app.core.workspace_identity import validate_workspace_id
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.managed_backend import ManagedBackendOwner
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName
from app.schemas.gateway.workspaces import GatewayConnectionKind

if TYPE_CHECKING:
    from app.gateway.registry import GatewayWorkspaceRegistry


_REGISTRY_SCHEMA_VERSION = 14
WorkspaceKey = tuple[str, str]
WorkspaceSelector = str | WorkspaceKey
_UNSET = object()
RegistryTargetOwner = Literal["config", "manual", "system", "remote_projection"]


def workspace_owner_payload(key: WorkspaceKey | None) -> dict[str, str] | None:
    return {"gateway_id": key[0], "workspace_id": key[1]} if key is not None else None


def workspace_owner_key(value: object) -> WorkspaceKey | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"gateway_id", "workspace_id"}:
        raise TypeError("工作区 owner 必须完整包含 gateway_id 和 workspace_id")
    return validate_gateway_id(value["gateway_id"]), validate_workspace_id(value["workspace_id"])


class WorkspaceIdentityConflictError(ValueError):
    """同一原生 Gateway 的工作区 UUID 不能绑定到不同物理根。"""


@dataclass(slots=True)
class WorkspaceTarget:
    gateway_id: str
    workspace_id: str
    name: str
    root_path: str | None
    backend_url: str
    connection_kind: GatewayConnectionKind
    parent_workspace: WorkspaceKey | None = None
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
    remote_gateway_connection_ids: tuple[str, ...] = ()
    remote_service_names: tuple[GatewayServiceName, ...] = ()
    remote_backend_generation: str | None = None
    remote_affected_workspace_ids: tuple[str, ...] = ()
    local_service_urls: dict[str, str] = field(default_factory=dict)
    connection_error: str | None = None
    backend_id: str | None = None

    @property
    def key(self) -> WorkspaceKey:
        return self.gateway_id, self.workspace_id

@dataclass(frozen=True, slots=True)
class WorkspaceRouteLease:
    gateway_id: str
    workspace_id: str
    revision: int
    invalidated: asyncio.Event

    @property
    def token(self) -> str:
        return f"{self.gateway_id}:{self.workspace_id}:{self.revision}"
@dataclass(slots=True)
class GatewayRegistryBatchHandle:
    """一次 registry batch 的 promotion/rollback 句柄。"""

    registry: GatewayWorkspaceRegistry
    previous_snapshot: dict[str, object]
    previous_signatures: dict[WorkspaceKey, tuple[object, ...]]
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
            self.registry._deferred_registry_batches.pop(id(self), None)
            errors: list[str] = []
            closed_runtimes: list[tuple[str, WorkspaceRuntime]] = []
            for connection_id, runtime in self.retired_runtimes:
                if self.registry._has_route_references_for_connection(connection_id):
                    continue
                if self.registry._remote_runtime_retirement_deferred(runtime):
                    continue
                if not any(item is runtime for item in self.registry._retired_remote_gateway_runtimes.get(connection_id, [])):
                    continue
                try:
                    runtime.close()
                    closed_runtimes.append((connection_id, runtime))
                except Exception as error:  # noqa: BLE001 — 关闭全部远端运行时并汇总初始化失败的清理错误。
                    errors.append(f"{connection_id}: {error}")
            self.registry._remove_retired_runtime_instances(tuple(closed_runtimes))
            if errors:
                raise RuntimeError(
                    "Gateway registry batch promotion 关闭旧 remote runtime 失败: "
                    + "; ".join(errors)
                )
        self.finished = True

    def rollback(self) -> None:
        """恢复 batch 前的 registry 和 tunnel 句柄。"""

        if self.finished:
            return
        self.registry._deferred_registry_batches.pop(id(self), None)
        self.registry._rollback_registry_batch(self)
        self.finished = True


class RegistryCoreMixin:
    def __init__(
        self,
        *,
        storage_path: Path,
        state_store: GatewayStateStore | None = None,
        gateway_id: str | None = None,
        runtime_logging_config: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._state_store = state_store
        self._runtime_logging_config = runtime_logging_config
        self.gateway_id = validate_gateway_id(gateway_id) if gateway_id is not None else load_or_create_gateway_id(storage_path.with_name("identity.json"))
        self._targets: dict[WorkspaceKey, WorkspaceTarget] = {}
        self._active_workspace: WorkspaceKey | None = None
        self._registry_revision = 0
        self._order_customized = False
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._backend_owners: dict[str, ManagedBackendOwner] = {}
        self.backend_topology_lock = asyncio.Lock()
        self._remote_gateway_connections: dict[str, RemoteGatewayConnection] = {}
        self._remote_gateway_runtimes: dict[str, WorkspaceRuntime] = {}
        self._retired_remote_gateway_runtimes: dict[str, list[WorkspaceRuntime]] = {}
        self._deferred_registry_batches: dict[int, GatewayRegistryBatchHandle] = {}
        self._runtime_generation: str | None = None
        self._route_revisions: dict[WorkspaceKey, int] = {}
        self._route_change_events: dict[WorkspaceKey, asyncio.Event] = {}
        self._route_reference_counts: dict[WorkspaceKey, tuple[int, int]] = {}
        self._route_reference_connections: dict[WorkspaceKey, set[str]] = {}
        self._remote_gateway_reference_counts: dict[str, int] = {}
        self._route_signatures: dict[WorkspaceKey, tuple[object, ...]] = {}
        self._commit_observers: list[Callable[[], None]] = []
        self._load()
        self._route_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }
        self._last_committed_snapshot = self._capture_registry_state()
    @property
    def active_workspace(self) -> WorkspaceKey | None:
        return self._active_workspace

    def workspace_key(self, selector: WorkspaceSelector) -> WorkspaceKey:
        """裸 UUID 仅表示本机；跨 Gateway 必须显式给出完整 owner。"""
        if isinstance(selector, str):
            return self.gateway_id, validate_workspace_id(selector)
        if not isinstance(selector, tuple) or len(selector) != 2:
            raise TypeError("工作区选择必须是 UUID 或 (gateway_id, workspace_id)")
        return validate_gateway_id(selector[0]), validate_workspace_id(selector[1])


    def runtime_logging_startup(self) -> dict[str, object] | None:
        """在生命周期锁内取得当前权威配置，不缓存等待启动期间可能失效的副本。"""
        return self._runtime_logging_config() if self._runtime_logging_config is not None else None
    @property
    def registry_revision(self) -> int:
        """返回最近一次持久化成功的 registry revision。"""
        return self._registry_revision
