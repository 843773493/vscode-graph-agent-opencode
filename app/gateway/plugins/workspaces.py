"""从原 Workspace 配置 owner 交接已提交绑定；只保留当前运行投影。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import httpx
from pydantic import JsonValue

from app.core.trace_middleware import get_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime
from app.schemas.gateway.plugin_private_inputs import (
    PluginParameterDelivery,
    PluginParameterDeliveryRequest,
)
from app.schemas.gateway.service_plugin import (
    PluginBinding,
    WorkspacePluginConfigurationDTO,
)
from app.schemas.gateway.service_plugin_workspace import (
    WorkspacePluginCreation,
    WorkspacePluginSnapshot,
)
from app.schemas.internal_v2.common import APIResponse

logger = logging.getLogger(__name__)


class WorkspacePluginBindings:
    """沿 registry 提交触发交接；无轮询、持久 enabled 副本或隐式启动。"""

    def __init__(self, registry: GatewayWorkspaceRegistry, runtime: GatewayPluginRuntime) -> None:
        self.registry, self.runtime = registry, runtime
        self.runtime.workspace_parameters = self.deliver_parameters
        self.snapshots: dict[str, WorkspacePluginSnapshot] = {}
        self.states: dict[str, WorkspacePluginConfigurationDTO] = {}
        self._signatures: dict[str, tuple[object, ...]] = {}
        self._changed = asyncio.Event()
        self._handoff_lock = asyncio.Lock()
        self._pending: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def _targets(self) -> dict[str, WorkspaceTarget]:
        return {target.workspace_id: target for target in self.registry.targets()
                if target.gateway_id == self.registry.gateway_id and target.connection_kind == "local"}

    def _signature(self, target: WorkspaceTarget) -> tuple[object, ...]:
        owner = self.registry.backend_owner(target.key)
        return (target.backend_url, target.desired_running, target.connection_error,
                owner.contract.generation if owner else None,
                owner.process.pid if owner and owner.process else None, owner.error if owner else None)

    def _on_commit(self) -> None:
        if {key: self._signature(target) for key, target in self._targets().items()} != self._signatures:
            self._changed.set()

    def request_refresh(self, workspace_id: str) -> WorkspacePluginConfigurationDTO:
        target = self.registry.resolve((self.registry.gateway_id, workspace_id))
        if target.connection_kind != "local":
            raise ValueError("配置通知必须属于原生工作区")
        self._pending.add(workspace_id)
        self._changed.set()
        previous = self.snapshots.get(workspace_id)
        return WorkspacePluginConfigurationDTO(workspace_id=workspace_id, state="waiting",
            revision=previous.revision if previous else None)

    def start(self) -> None:
        self.registry.add_commit_observer(self._on_commit)
        self._task = asyncio.create_task(self._observe(), name="workspace-plugin-bindings")
        self._task.add_done_callback(self._report_failure)
        self._changed.set()

    @staticmethod
    def _report_failure(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Workspace 插件交接观察者失败", exc_info=(type(error), error, error.__traceback__))

    async def close(self) -> None:
        self.registry.remove_commit_observer(self._on_commit)
        if self._task is not None:
            self._task.cancel()
            results = await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

    async def reconcile(self) -> None:
        """显式 owner 交接用于重试；普通状态读取不进行网络发现。"""
        # 启动恢复也会调用 controller 回调；先完成旧实例接管，才处理绑定。
        if self._task is None:
            return
        async with self._handoff_lock:
            # 初始化经过 Workspace 的原候选验证，会回到 Gateway 核包；不能持拓扑锁等待它。
            # 冻结创建 intent 在此期间保护包版本，验证后由工作区 active/pending 接替。
            async with self.registry.backend_topology_lock:
                targets = self._targets()
                observed_signatures = {key: self._signature(target) for key, target in targets.items()}
            failed_initializations: set[str] = set()
            for workspace_id, target in targets.items():
                if (target.plugin_creation is None or not target.desired_running
                    or (self._signatures.get(workspace_id) == self._signature(target)
                        and workspace_id not in self._pending)):
                    continue
                try:
                    await self._initialize_creation(target, WorkspacePluginCreation.model_validate(target.plugin_creation))
                except (httpx.HTTPError, LookupError, ValueError, RuntimeError) as error:
                    self.states[workspace_id] = WorkspacePluginConfigurationDTO(
                        workspace_id=workspace_id, state="failed", error=str(error))
                    failed_initializations.add(workspace_id)
                    logger.exception("新建工作区插件初始化失败: workspace_id=%s", workspace_id)
            async with self.registry.backend_topology_lock:
                targets = self._targets()
                removed = (set(self._signatures) | set(self.snapshots)
                           | {slot.binding.workspace_id for slot in self.runtime.slots.values()
                              if slot.binding.workspace_id is not None}) - targets.keys()
                for workspace_id in removed:
                    await self.runtime.apply_scope([], revision="workspace-removed", workspace_id=workspace_id)
                    if self._stop_failure(workspace_id, revision="workspace-removed"):
                        continue
                    self.snapshots.pop(workspace_id, None)
                    self.states.pop(workspace_id, None)
                    self._pending.discard(workspace_id)
                signatures = {key: self._signature(target) for key, target in targets.items()}
                for workspace_id, signature in signatures.items():
                    # 等待初始化／拓扑锁时原启动可能完成；新输入留给下一次交接，不能误记为已处理。
                    if observed_signatures.get(workspace_id) != signature:
                        self._changed.set()
                        continue
                    if self._signatures.get(workspace_id) != signature or workspace_id in self._pending:
                        self._pending.discard(workspace_id)
                        if workspace_id not in failed_initializations:
                            await self._refresh(targets[workspace_id])
                self._signatures = {key: signature for key, signature in observed_signatures.items() if key in targets}

    async def _observe(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            await self.reconcile()

    async def _refresh(self, target: WorkspaceTarget) -> None:
        workspace_id = target.workspace_id
        previous = self.snapshots.get(workspace_id)
        revision = previous.revision if previous else None
        if target.managed and not target.desired_running:
            await self.runtime.apply_scope([], revision=revision or "workspace-stopped", workspace_id=workspace_id)
            if self._stop_failure(workspace_id, revision=revision):
                return
            self.states[workspace_id] = WorkspacePluginConfigurationDTO(
                workspace_id=workspace_id, state="stopped", revision=revision)
            return
        try:
            snapshot = await self.read(target)
        except (httpx.RequestError, LookupError) as error:
            # owner 暂不可达不代表停用；保留已接管实例，等原提交／显式交接。
            self.states[workspace_id] = WorkspacePluginConfigurationDTO(
                workspace_id=workspace_id, state="waiting", revision=revision, error=str(error))
            logger.warning("Workspace 插件等待配置交接: workspace_id=%s error=%s", workspace_id, error)
            return
        except (httpx.HTTPStatusError, ValueError, RuntimeError) as error:
            self.states[workspace_id] = WorkspacePluginConfigurationDTO(
                workspace_id=workspace_id, state="failed", revision=revision, error=str(error))
            logger.exception("Workspace 插件配置交接失败: workspace_id=%s", workspace_id)
            return
        # 已提交期望与运行状态分开：非法包／启动失败不得修改原 enabled。
        self.snapshots[workspace_id] = snapshot
        try:
            await self.runtime.apply_scope(snapshot.bindings, revision=snapshot.revision, workspace_id=workspace_id,
                                           parameters=snapshot.parameters)
        except (ValueError, RuntimeError) as error:
            self.states[workspace_id] = WorkspacePluginConfigurationDTO(
                workspace_id=workspace_id, state="failed", revision=snapshot.revision, error=str(error))
            logger.exception("Workspace 插件绑定应用失败: workspace_id=%s", workspace_id)
            return
        self.states[workspace_id] = WorkspacePluginConfigurationDTO(
            workspace_id=workspace_id, state="ready", revision=snapshot.revision)

    def _stop_failure(self, workspace_id: str, *, revision: str | None) -> bool:
        errors = [slot.error or slot.state for slot in self.runtime.slots.values()
                  if slot.binding.workspace_id == workspace_id]
        if errors:
            self.states[workspace_id] = WorkspacePluginConfigurationDTO(workspace_id=workspace_id,
                state="failed", revision=revision, error="; ".join(errors))
        return bool(errors)

    async def _initialize_creation(self, target: WorkspaceTarget, creation: WorkspacePluginCreation) -> None:
        """创建通知只调用原工作区 writer；失败保留冻结模板供显式重试。"""
        request_id = get_current_request_id() or creation.intent_id
        with self.registry.backend_management_routes(workspace_ids=(target.workspace_id,)) as routes:
            route, = routes
            response = await self.runtime.client.post(route.base_url + "/api/internal/service-plugins/initialize",
                json=creation.model_dump(mode="json"),
                headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id,
                         "X-BoxTeam-Workspace-Id": target.workspace_id,
                         "X-BoxTeam-Gateway-Id": self.registry.gateway_id})
            response.raise_for_status()
            result = APIResponse[bool].model_validate_json(response.content)
            if result.code != 0 or result.data is not True or result.request_id != request_id:
                raise ValueError("工作区默认插件初始化未获得原 writer 的提交回执")
        async with self.registry.backend_topology_lock:
            current = self.registry.resolve(target.key)
            snapshot = await self.read(current)
            self.snapshots[target.workspace_id] = snapshot
            self.registry.complete_plugin_creation(target.workspace_id, creation.intent_id)

    async def read(self, target: WorkspaceTarget) -> WorkspacePluginSnapshot:
        if target.connection_kind != "local" or target.gateway_id != self.registry.gateway_id:
            raise ValueError("工作区插件配置只能从原生 owner 交接")
        request_id = get_current_request_id() or f"workspace-plugin-handoff-{uuid4().hex}"
        with self.registry.backend_management_routes(workspace_ids=(target.workspace_id,)) as routes:
            if len(routes) != 1:
                raise ValueError("工作区插件交接必须有唯一原配置 owner")
            route = routes[0]
            response = await self.runtime.client.get(route.base_url + "/api/internal/service-plugins/bindings",
                headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id,
                         "X-BoxTeam-Workspace-Id": target.workspace_id,
                         "X-BoxTeam-Gateway-Id": self.registry.gateway_id})
            response.raise_for_status()
            result = APIResponse[WorkspacePluginSnapshot].model_validate_json(response.content)
            snapshot = result.data
            if (result.code != 0 or result.request_id != request_id or snapshot is None
                or snapshot.workspace_id != target.workspace_id):
                raise ValueError("工作区配置交接回执与原目标／请求不符")
            if route.owner is not None and snapshot.backend_generation != route.owner.contract.generation:
                raise ValueError("工作区配置交接来自已失效 Backend generation")
            self.registry.confirm_backend_management_identity(route, snapshot.backend_id)
            return snapshot

    async def deliver_parameters(self, binding: PluginBinding, revision: str) -> dict[str, JsonValue]:
        """只在新实例启动时读取原 owner；不缓存或写入接管材料。"""
        assert binding.workspace_id is not None
        request_id = get_current_request_id() or f"plugin-parameters-{uuid4().hex}"
        with self.registry.backend_management_routes(workspace_ids=(binding.workspace_id,)) as routes:
            route, = routes
            response = await self.runtime.client.post(route.base_url + "/api/internal/service-plugins/parameters",
                json=PluginParameterDeliveryRequest(binding=binding, revision=revision,
                    configuration_schema=self.runtime.declaration(binding).contributes.configuration).model_dump(mode="json"),
                headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id,
                         "X-BoxTeam-Workspace-Id": binding.workspace_id,
                         "X-BoxTeam-Gateway-Id": self.registry.gateway_id})
            response.raise_for_status()
            result = APIResponse[PluginParameterDelivery].model_validate_json(response.content)
            if result.code != 0 or result.request_id != request_id or result.data is None:
                raise ValueError("插件私有参数交付未获得原配置 owner 回执")
            return result.data.values.get_secret_value()[binding.package_id][binding.plugin_id]

    def configured_bindings(self) -> list[PluginBinding]:
        return [binding for snapshot in self.snapshots.values() for binding in snapshot.bindings]

    def referenced_bindings(self) -> list[PluginBinding]:
        return [binding for snapshot in self.snapshots.values() for binding in snapshot.referenced_bindings]

    async def read_references(self) -> list[PluginBinding]:
        """显式卸载前读取当前引用；普通业务请求不审计配置。调用方持拓扑锁。"""
        for workspace_id, target in self._targets().items():
            if target.managed and not target.desired_running:
                if workspace_id not in self.snapshots:
                    raise ValueError(f"工作区安装引用尚未交接，不能卸载: {workspace_id}")
                continue
            # 当前 active 或 pending 在上次通知之后仍可能变化，不能凭缓存删除版本。
            self.snapshots[workspace_id] = await self.read(target)
        return self.referenced_bindings()
