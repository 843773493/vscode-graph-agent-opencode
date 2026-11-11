"""ConfigReloadMixin：ConfigService 的 reload、shadow 生命周期与候选快照提交方法族（纯搬迁）。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx

from app.core.failures import failure_detail
from app.core.lifecycle import LifetimeScope
from app.core.trace_middleware import get_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.schemas.gateway.plugin_private_inputs import WorkspacePluginSchemas
from app.schemas.gateway.service_plugin import PluginBinding, PluginSlotStatusDTO
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.config import (
    ConfigFileWatcher,
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.services.infrastructure.config.contracts import ConfigPublication
from app.services.infrastructure.config.plugin_private_inputs import (
    PluginPrivateInputStore,
    has_parameter_references,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.shadow_scope import (
    ConfigShadowLifecycleError,
    ShadowReconcile,
)
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    ConfigResult,
    build_secret_binding_summary,
    changed_json_paths,
    new_config_id,
    prepare_config_for_persistence,
    redact_config_payload,
)
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings_for_root,
)
from app.services.infrastructure.config_service.config_service_common import (
    ConfigCandidateApplier,
    logger,
)
from app.services.infrastructure.config_service.config_snapshot import (
    ConfigSnapshotMixin,
)
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from configs.plugin_parameters import PluginParameters, plugin_parameters
from configs.runtime import validate_config_payload

if TYPE_CHECKING:
    from app.services.infrastructure.config.instruction_inputs import (
        InstructionFileInputs,
    )
    from app.services.infrastructure.config.shadow_adapter import (
        ConfigShadowLifecycleAdapter,
    )
    from app.services.infrastructure.config.store import ConfigSnapshotStore
    from app.services.infrastructure.config_service import ConfigService
    from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class ConfigReloadMixin:
    if TYPE_CHECKING:
        _instruction_inputs: InstructionFileInputs
        _snapshot_store: ConfigSnapshotStore
        _shadow_reconcile: ShadowReconcile | None
        _shadow_lifecycle: ConfigShadowLifecycleAdapter
        _apply_recovery_task: asyncio.Task[None] | None
        _workspace_state_store: WorkspaceStateStore | None
        _workspace_root: Path | None
        _CONFIG_DOMAIN: str
        _mcp_plugin_preparation: tuple[asyncio.Task[ConfigPublication], tuple[PluginBinding, ...]] | None

    def _cancel_disabled_plugin_preparation(self, candidate: ConfigSnapshot) -> None:
        """关闭只撤销涉及该绑定的未发布准备，结算仍走原 reload／journal。"""
        preparing = self._mcp_plugin_preparation
        if preparing is None:
            return
        task, bindings = preparing
        enabled = {(binding.package_id, binding.plugin_id, binding.workspace_id)
            for binding in workspace_plugin_bindings_for_root(candidate.to_dict(), workspace_root=self._workspace_root)
            if binding.enabled}
        if any((binding.package_id, binding.plugin_id, binding.workspace_id) not in enabled for binding in bindings):
            task.cancel()

    def validate_candidate(
        self,
        snapshot: ConfigSnapshot,
        *,
        mcp_tool_names: frozenset[str],
    ) -> None:
        config = snapshot.to_dict()
        self._validate_agent_tool_policies(
            config,
            mcp_tool_names=mcp_tool_names,
        )

    def _validate_shadow_config(self, config: Mapping[str, object]) -> None:
        """shadow generation 的纯内存 schema 校验入口。

        source 文件稳定读取和工具策略预检仍由 candidate builder 负责；这里
        禁止重读磁盘，否则 bootstrap 会把尚未构建的坏 candidate 误算成旧
        active generation 的失败。
        """
        validate_config_payload(dict(config), schema=self._load_schema())

    async def _reconcile_shadow_config(
        self,
        config: Mapping[str, object],
        scope: LifetimeScope,
    ) -> ConfigPublication:
        """候选只准备资源；同步激活与配置发布共用同一提交点。"""
        if self._shadow_reconcile is not None:
            return await self._shadow_reconcile(config, scope)
        return lambda: None

    async def _ensure_shadow_started(self) -> None:
        readiness = self._shadow_lifecycle.readiness
        if readiness == "ready":
            return
        if readiness != "cold":
            raise ConfigShadowLifecycleError(
                "readiness_gate_closed",
                f"workspace 配置 shadow lifecycle 不可启动: readiness={readiness}",
            )
        await self._shadow_lifecycle.bootstrap(self._require_snapshot().to_dict())

    async def _renew_apply_claim(self, apply_id: str, fencing_token: str) -> None:
        """在外部副作用期间续租 claim；claim 丢失由最终 CAS 明确暴露。"""

        while True:
            await asyncio.sleep(10)
            if self._workspace_state_store is None:
                return
            await asyncio.to_thread(
                self._workspace_state_store.renew_config_apply_claim,
                config_domain=self._CONFIG_DOMAIN,
                apply_id=apply_id,
                fencing_token=fencing_token,
                lease_seconds=30,
            )

    async def reload(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
        idempotency_key: str | None = None,
        before_build: Callable[[], bool] | None = None,
        source_edit: dict[str, object] | None = None,
    ) -> bool:
        async def apply_through_shadow(
            previous: ConfigSnapshot,
            candidate: ConfigSnapshot,
            publish_snapshot: ConfigPublication,
        ) -> None:
            nonlocal source_edit, idempotency_key
            # 合法 candidate_ref 启动已加载相同 pending 正文时，只刷新原元数据。
            if (
                source_edit is None
                and (saved := cast("ConfigService", self)._saved_source_edit_candidate(candidate)) is not None
                and (saved.state == "candidate_validated" or candidate.revision != previous.revision)
            ):
                source_edit, idempotency_key = saved.source_edit, saved.idempotency_key
            if self._shadow_lifecycle.readiness == "cold":
                await self._shadow_lifecycle.bootstrap(previous.to_dict())
            if candidate.revision == previous.revision:
                if source_edit is not None:
                    pending = self._prepare_candidate(previous, candidate, idempotency_key=idempotency_key, source_edit=source_edit)
                    if self._workspace_state_store is None:
                        raise ConfigConflictError("来源编辑必须使用原工作区状态库")
                    self._workspace_state_store.complete_unchanged_config_candidate(
                        config_domain=self._CONFIG_DOMAIN, candidate_id=pending.candidate_id,
                    )
                async def refresh_sources(_generation, publish_shadow: ConfigPublication) -> None:
                    with self._snapshot_store.publication_lock:
                        publish_snapshot()
                        publish_shadow()

                await self._shadow_lifecycle.apply_candidate(
                    candidate.to_dict(),
                    expected_generation=self._shadow_lifecycle.generation,
                    publish_hook=refresh_sources,
                )
                return

            pending = self._prepare_candidate(previous, candidate, idempotency_key=idempotency_key, source_edit=source_edit)
            changed_paths = changed_json_paths(previous.to_dict(), candidate.to_dict())
            attempt_id = new_config_id("attempt")
            apply_id = new_config_id("apply")
            active = (
                self._workspace_state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
                if self._workspace_state_store is not None else None
            )
            base_active_revision = active.active_revision if active is not None else None
            claim = None
            claim_renewal_task: asyncio.Task[None] | None = None
            if self._workspace_state_store is not None:
                claim = self._workspace_state_store.begin_config_apply(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    owner="workspace-config-service",
                    base_active_revision=base_active_revision,
                    target_generation=new_config_id("workspace_generation"),
                    pending_revision=pending.pending_revision,
                    source_baseline=pending.source_baseline,
                    active_baseline=active.source_baseline if active is not None else {},
                )
                claim_renewal_task = asyncio.create_task(
                    self._renew_apply_claim(claim.apply_id, claim.fencing_token)
                )
            committed = False

            async def prepare_runtime(_config: Mapping[str, object], scope: LifetimeScope) -> ConfigPublication:
                # 仅准备阶段可由关闭撤销；同步 publication 和已发布后的排空不再取消。
                service_keys = {(server.service.package_id, server.service.plugin_id, server.service.workspace_id)
                    for server in parse_mcp_server_configs(_config.get("mcp"), workspace_root=self._workspace_root)
                    if server.enabled and server.service is not None}
                bindings = tuple(binding for binding in workspace_plugin_bindings_for_root(
                    _config, workspace_root=self._workspace_root,
                ) if binding.enabled and (binding.package_id, binding.plugin_id, binding.workspace_id) in service_keys)
                if not bindings:
                    return await prepare_connections(_config, scope)
                task = asyncio.create_task(prepare_connections(_config, scope), name="workspace-mcp-plugin-candidate")
                self._mcp_plugin_preparation = (task, bindings)
                try:
                    return await task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and not current.cancelling():
                        raise ConfigConflictError("MCP 插件候选已被关闭操作撤销") from None
                    raise
                finally:
                    self._mcp_plugin_preparation = None

            async def prepare_connections(_config: Mapping[str, object], scope: LifetimeScope) -> ConfigPublication:
                if candidate_applier is None:
                    return lambda: None

                def record_side_effect(effect: dict[str, object]) -> None:
                    if self._workspace_state_store is None or claim is None:
                        raise ConfigConflictError("破坏性配置准备必须先取得持久 apply journal／claim")
                    current_claim = self._workspace_state_store.get_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                    )
                    if (
                        current_claim is None
                        or current_claim.apply_id != claim.apply_id
                        or current_claim.fencing_token != claim.fencing_token
                        or current_claim.lease_expires_at <= datetime.now(UTC)
                    ):
                        raise ConfigConflictError("破坏性配置准备的 apply claim 已失效")
                    self._workspace_state_store.append_config_apply_side_effect(
                        apply_id=claim.apply_id, side_effect=effect,
                    )

                for binding, expected_binding in self._mcp_plugin_candidate_bindings(previous, candidate):
                    record_side_effect({
                        "resource": "workspace-plugin-runtime",
                        "action": "candidate-start-intent",
                        "package_id": binding.package_id,
                        "plugin_id": binding.plugin_id,
                        "workspace_id": binding.workspace_id,
                        "candidate_revision": candidate.revision,
                    })
                    await self._apply_gateway_workspace_plugin_candidate(
                        previous=previous,
                        candidate=candidate,
                        candidate_id=pending.candidate_id,
                        binding=binding,
                        expected_binding=expected_binding,
                        gateway_id=str(source_edit["gateway_id"]) if source_edit is not None else None,
                    )
                    record_side_effect({
                        "resource": "workspace-plugin-runtime",
                        "action": "candidate-ready",
                        "package_id": binding.package_id,
                        "plugin_id": binding.plugin_id,
                        "workspace_id": binding.workspace_id,
                        "candidate_revision": candidate.revision,
                    })

                activate = await candidate_applier(previous, candidate, scope, record_side_effect)
                if not callable(activate):
                    raise TypeError("配置候选准备必须返回同步激活回调")
                if self._workspace_state_store is not None and claim is not None:
                    self._workspace_state_store.append_config_apply_side_effect(
                        apply_id=claim.apply_id,
                        side_effect={
                            "resource": "workspace-config-applier",
                            "action": "prepare",
                            "candidate_id": pending.candidate_id,
                            "changed_paths": list(changed_paths),
                        },
                    )
                return activate

            async def publish_candidate(_generation, publish_shadow: ConfigPublication) -> None:
                nonlocal committed
                # 网络准备及旧 scope 排空均在锁外；SQLite CAS 到全部内存切换
                # 之间不让配置读取、目录捕获或调用准入观察到半发布。
                with self._snapshot_store.publication_lock:
                    pending_layer_revisions = {
                        str(key): int(detail["layer_revision"])
                        for key, detail in pending.source_baseline.items()
                        if isinstance(detail, dict)
                        and detail.get("layer_revision") is not None
                    }
                    pending_layer_digests = {
                        str(key): (
                            str(detail.get("layer_digest"))
                            if detail.get("layer_digest") is not None
                            else None
                        )
                        for key, detail in pending.source_baseline.items()
                        if isinstance(detail, dict)
                        and detail.get("layer_revision") is not None
                    }
                    self._persist_active_snapshot(
                        candidate,
                        candidate_id=pending.candidate_id,
                        expected_active_revision=base_active_revision,
                        expected_pending_revision=pending.pending_revision,
                        expected_pending_state="applying",
                        expected_fencing_token=(claim.fencing_token if claim else None),
                        expected_source_baseline=pending.source_baseline,
                        expected_source_generation=pending.source_generation,
                        expected_layer_revisions=pending_layer_revisions,
                        expected_layer_digests=pending_layer_digests,
                        apply_id=apply_id,
                        event=ConfigEventInput(
                            event_id=f"config:{pending.candidate_id}:active",
                            config_domain=self._CONFIG_DOMAIN,
                            candidate_id=pending.candidate_id,
                            attempt_id=attempt_id,
                            apply_id=apply_id,
                            idempotency_key=pending.idempotency_key,
                            commit_revision=None,
                            active_revision=None,
                            pending_revision=pending.pending_revision,
                            source="workspace-config-service",
                            result="applied" if changed_paths else "unchanged",
                            activation_scope=workspace_config_policy().activation_scope_for(
                                changed_paths
                            ),
                            changed_paths=changed_paths,
                            applied_paths=changed_paths,
                        ),
                    )
                    committed = True
                    publish_snapshot()
                    publish_shadow()

            try:
                await self._validate_gateway_workspace_plugin_bindings(
                    previous=previous,
                    candidate=candidate,
                    candidate_id=pending.candidate_id,
                    gateway_id=str(source_edit["gateway_id"]) if source_edit is not None else None,
                )
                await self._shadow_lifecycle.apply_candidate(
                    candidate.to_dict(),
                    expected_generation=self._shadow_lifecycle.generation,
                    publish_hook=publish_candidate,
                    prepare=prepare_runtime,
                )
            except BaseException as error:
                # 提交后的激活或旧 scope 清理失败不能重写已应用的持久事实。
                if committed:
                    try:
                        await self._notify_workspace_plugin_binding_change(
                            previous=previous, candidate=candidate,
                            candidate_id=pending.candidate_id,
                        )
                    except BaseException as notification_error:  # noqa: BLE001
                        raise BaseExceptionGroup(
                            "Workspace 配置已提交，但激活收尾和插件刷新通知均失败",
                            [error, notification_error],
                        ) from None
                    raise
                journal = (
                    self._workspace_state_store.get_config_apply_journal(apply_id=claim.apply_id)
                    if self._workspace_state_store is not None and claim is not None else None
                )
                recovery_required = bool(journal and journal.side_effects)
                state: ConfigLifecycleState
                result: ConfigResult
                if isinstance(error, ConfigRestartRequiredError):
                    state, result = "pending_restart", "restart_required"
                elif recovery_required:
                    state, result = "recovery_required", "recovery_required"
                elif isinstance(error, ConfigConflictError):
                    state, result = "conflict", "conflict"
                else:
                    state, result = "rejected", "apply_failed"
                error_detail = failure_detail(error, stage=result, label="Workspace 配置应用失败")
                self._finish_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=state,
                    result=result,
                    error=error_detail,
                    changed_paths=changed_paths,
                    candidate_ref=new_config_id("candidate_ref") if state == "pending_restart" else None,
                    target_generation=claim.target_generation if claim else None,
                    fencing_token=claim.fencing_token if claim else None,
                )
                if self._workspace_state_store is not None and claim is not None:
                    self._workspace_state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state="recovery_required" if recovery_required else "failed",
                        last_error=error_detail,
                    )
                raise
            else:
                await self._notify_workspace_plugin_binding_change(
                    previous=previous, candidate=candidate,
                    candidate_id=pending.candidate_id,
                )
            finally:
                if claim_renewal_task is not None:
                    claim_renewal_task.cancel()
                    await asyncio.gather(claim_renewal_task, return_exceptions=True)
                if self._workspace_state_store is not None and claim is not None:
                    self._workspace_state_store.release_config_apply_claim(
                        config_domain=self._CONFIG_DOMAIN,
                        apply_id=claim.apply_id,
                        fencing_token=claim.fencing_token,
                    )

        try:
            return await self._snapshot_store.reload(
                candidate_applier=apply_through_shadow,
                apply_unchanged=True,
                before_build=before_build,
            )
        finally:
            if self._watcher is not None:
                self._update_config_watch_paths(self._watcher)

    async def get_workspace_plugin_schemas(self, bindings: list[PluginBinding], *,
                                           gateway_id: str | None = None) -> WorkspacePluginSchemas:
        service = cast("ConfigService", self)
        workspace_id = bindings[0].workspace_id
        if not workspace_id or any(binding.workspace_id != workspace_id for binding in bindings):
            raise ConfigConflictError("插件参数声明缺少唯一工作区归属")
        request_id = get_current_request_id() or f"plugin-schemas-{uuid4().hex}"
        async with httpx.AsyncClient(timeout=service.get_gateway_connection_timeout_seconds(), trust_env=False) as client:
            response = await client.post(
                service.get_gateway_connection_url().rstrip("/")
                + f"/api/internal/service-plugins/workspaces/{workspace_id}/schemas",
                json=[binding.model_dump(mode="json") for binding in bindings],
                headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id})
            if response.status_code == 409:
                raise ConfigConflictError("Gateway 拒绝确切版本的插件参数声明")
            response.raise_for_status()
        result = APIResponse[WorkspacePluginSchemas].model_validate_json(response.content)
        data = result.data
        if (result.code != 0 or result.request_id != request_id or data is None
            or data.workspace_id != workspace_id or [plugin.binding for plugin in data.plugins] != bindings
            or (gateway_id is not None and data.gateway_id != gateway_id)):
            raise ConfigConflictError("插件参数声明回执与原生目标或确切绑定不一致")
        return data

    async def _validate_gateway_workspace_plugin_bindings(
        self,
        *,
        previous: ConfigSnapshot,
        candidate: ConfigSnapshot,
        candidate_id: str,
        gateway_id: str | None = None,
    ) -> None:
        previous_bindings = workspace_plugin_bindings_for_root(
            previous.to_dict(), workspace_root=self._workspace_root,
        )
        bindings = workspace_plugin_bindings_for_root(
            candidate.to_dict(), workspace_root=self._workspace_root,
        )
        parameters = plugin_parameters(candidate.to_dict())
        if not bindings or (bindings == previous_bindings and parameters == plugin_parameters(previous.to_dict())):
            return
        if self._workspace_state_store is None:
            raise ConfigConflictError(
                "Workspace 插件候选校验需要已绑定的配置存储 owner"
            )
        workspace_id = bindings[0].workspace_id
        if not workspace_id or any(
            binding.workspace_id != workspace_id for binding in bindings
        ):
            raise ConfigConflictError("Workspace 插件绑定缺少唯一真实 workspace_id")

        service = cast("ConfigService", self)
        if self._workspace_root is None:
            raise ConfigConflictError("Workspace 插件私有参数缺少工作区配置 owner")
        selected: PluginParameters = {}
        for binding in bindings:
            selected.setdefault(binding.package_id, {})[binding.plugin_id] = parameters.get(
                binding.package_id, {}).get(binding.plugin_id, {})
        resolved = selected
        if has_parameter_references(selected):
            declarations = await self.get_workspace_plugin_schemas(bindings, gateway_id=gateway_id)
            store = PluginPrivateInputStore(directory=self._workspace_root / ".boxteam/credentials/plugins",
                gateway_id=declarations.gateway_id, workspace_id=workspace_id)
            resolved = {}
            try:
                for declaration in declarations.plugins:
                    binding = declaration.binding
                    resolved.setdefault(binding.package_id, {})[binding.plugin_id] = store.resolve(
                        selected[binding.package_id][binding.plugin_id], package_id=binding.package_id,
                        plugin_id=binding.plugin_id, schema=declaration.configuration_schema)
            except ValueError as error:
                raise ConfigConflictError(str(error)) from error
        gateway_url = service.get_gateway_connection_url().rstrip("/")
        request_id = get_current_request_id() or f"config-{candidate_id}"
        async with httpx.AsyncClient(
            timeout=service.get_gateway_connection_timeout_seconds(),
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{gateway_url}/api/internal/service-plugins/workspaces/{workspace_id}/validate",
                json={"bindings": [binding.model_dump(mode="json") for binding in bindings],
                      "parameters": selected,
                      "delivery": {"values": resolved}},
                headers={
                    "X-Local-Token": get_gateway_local_token(),
                    "X-Request-ID": request_id,
                },
            )
            if response.status_code == 409:
                raise ConfigConflictError(
                    f"Gateway 拒绝 Workspace 插件绑定 candidate (HTTP 409): {response.json()['detail']}"
                )
            response.raise_for_status()
        result = APIResponse[bool].model_validate_json(response.content)
        if (
            result.code != 0
            or result.request_id != request_id
            or result.data is not True
        ):
            raise ConfigConflictError(
                "Gateway Workspace 插件绑定校验回执无效或拒绝 candidate"
            )

    async def _apply_gateway_workspace_plugin_candidate(
        self, *, previous: ConfigSnapshot, candidate: ConfigSnapshot, candidate_id: str,
        binding: PluginBinding, expected_binding: PluginBinding | None,
        gateway_id: str | None = None,
    ) -> None:
        previous_bindings = workspace_plugin_bindings_for_root(
            previous.to_dict(), workspace_root=self._workspace_root,
        )
        candidate_bindings = workspace_plugin_bindings_for_root(
            candidate.to_dict(), workspace_root=self._workspace_root,
        )
        previous_binding = next((item for item in previous_bindings if (
            item.package_id, item.plugin_id, item.workspace_id,
        ) == (binding.package_id, binding.plugin_id, binding.workspace_id)), None)
        if (previous_binding != expected_binding or binding not in candidate_bindings
                or not binding.enabled):
            raise ConfigConflictError("插件参数替换的原绑定或候选绑定已失效")
        workspace_id = binding.workspace_id
        if workspace_id is None or self._workspace_root is None:
            raise ConfigConflictError("插件参数替换缺少真实 Workspace owner")
        parameters = plugin_parameters(candidate.to_dict())
        selected = {binding.package_id: {binding.plugin_id: parameters.get(binding.package_id, {}).get(binding.plugin_id, {})}}
        declarations = await self.get_workspace_plugin_schemas([binding], gateway_id=gateway_id)
        store = PluginPrivateInputStore(
            directory=self._workspace_root / ".boxteam/credentials/plugins",
            gateway_id=declarations.gateway_id, workspace_id=workspace_id,
        )
        resolved = {binding.package_id: {binding.plugin_id: store.resolve(
            selected[binding.package_id][binding.plugin_id], package_id=binding.package_id,
            plugin_id=binding.plugin_id,
            schema=declarations.plugins[0].configuration_schema,
        )}}
        candidate_revision = candidate.revision
        gateway_url = cast("ConfigService", self).get_gateway_connection_url().rstrip("/")
        request_id = get_current_request_id() or f"config-{candidate_id}"
        async with httpx.AsyncClient(
            timeout=cast("ConfigService", self).get_gateway_connection_timeout_seconds(), trust_env=False,
        ) as client:
            response = await client.post(
                f"{gateway_url}/api/internal/service-plugins/workspaces/{workspace_id}/apply-candidate",
                json={"expected_revision": previous.revision, "candidate_revision": candidate_revision,
                      "binding": binding.model_dump(mode="json"),
                      "expected_binding": expected_binding.model_dump(mode="json") if expected_binding else None,
                      "bindings": [item.model_dump(mode="json") for item in candidate_bindings],
                      "parameters": selected,
                      "delivery": {"values": resolved}},
                headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id},
            )
            if response.status_code == 409:
                raise ConfigConflictError("Gateway 拒绝插件参数候选应用 (HTTP 409)")
            response.raise_for_status()
        result = APIResponse[PluginSlotStatusDTO].model_validate_json(response.content)
        if result.code != 0 or result.request_id != request_id or result.data is None or result.data.state != "ready":
            raise ConfigConflictError("Gateway 插件参数候选没有返回精确 ready 回执")

    def _mcp_plugin_candidate_bindings(
        self, previous: ConfigSnapshot, candidate: ConfigSnapshot,
    ) -> list[tuple[PluginBinding, PluginBinding | None]]:
        """只重启本轮影响已启用 MCP 插件服务的绑定。"""
        previous_config, candidate_config = previous.to_dict(), candidate.to_dict()
        previous_bindings = workspace_plugin_bindings_for_root(previous_config, workspace_root=self._workspace_root)
        candidate_bindings = workspace_plugin_bindings_for_root(candidate_config, workspace_root=self._workspace_root)
        old_by_key = {(item.package_id, item.plugin_id, item.workspace_id): item for item in previous_bindings}
        new_by_key = {(item.package_id, item.plugin_id, item.workspace_id): item for item in candidate_bindings}
        old_parameters, new_parameters = plugin_parameters(previous_config), plugin_parameters(candidate_config)
        configured = {server.server_id: server for server in parse_mcp_server_configs(
            candidate_config.get("mcp"), workspace_root=self._workspace_root,
        )}
        changed: dict[tuple[str, str, str | None], tuple[PluginBinding, PluginBinding | None]] = {}
        for server in configured.values():
            if not server.enabled:
                continue
            service = server.service
            if service is None:
                continue
            key = (service.package_id, service.plugin_id, service.workspace_id)
            new_binding, old_binding = new_by_key.get(key), old_by_key.get(key)
            if new_binding is None or not new_binding.enabled:
                continue
            if service.package_version != new_binding.package_version:
                continue
            params_changed = old_parameters.get(key[0], {}).get(key[1], {}) != new_parameters.get(key[0], {}).get(key[1], {})
            if old_binding != new_binding or params_changed:
                changed[key] = (new_binding, old_binding)
        return list(changed.values())

    async def _notify_gateway_workspace_plugins(
        self, *, workspace_id: str | None, candidate_id: str,
    ) -> None:
        if not workspace_id:
            raise RuntimeError("Workspace 插件绑定提交缺少真实 workspace_id")
        service = cast("ConfigService", self)
        gateway_url = service.get_gateway_connection_url().rstrip("/")
        token = get_gateway_local_token()
        request_id = get_current_request_id() or f"config-{candidate_id}"
        timeout = service.get_gateway_connection_timeout_seconds()
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{gateway_url}/api/internal/service-plugins/workspaces/{workspace_id}/refresh",
                headers={"X-Local-Token": token, "X-Request-ID": request_id},
            )
            response.raise_for_status()

    async def _notify_workspace_plugin_binding_change(
        self, *, previous: ConfigSnapshot, candidate: ConfigSnapshot,
        candidate_id: str,
    ) -> None:
        previous_bindings = workspace_plugin_bindings_for_root(
            previous.to_dict(), workspace_root=self._workspace_root,
        )
        candidate_bindings = workspace_plugin_bindings_for_root(
            candidate.to_dict(), workspace_root=self._workspace_root,
        )
        if (previous_bindings != candidate_bindings
            or plugin_parameters(previous.to_dict()) != plugin_parameters(candidate.to_dict())):
            await self._notify_gateway_workspace_plugins(
                workspace_id=(previous_bindings or candidate_bindings)[0].workspace_id,
                candidate_id=candidate_id,
            )

    def _prepare_candidate(
        self,
        previous: ConfigSnapshot,
        snapshot: ConfigSnapshot,
        *,
        idempotency_key: str | None = None,
        source_edit: dict[str, object] | None = None,
    ) -> ConfigPendingCandidateRecord:
        if self._workspace_state_store is None:
            payload = redact_config_payload(snapshot.to_dict())
            if not isinstance(payload, dict):
                raise TypeError("脱敏配置候选必须是对象")
            return ConfigPendingCandidateRecord(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=f"candidate_{snapshot.revision}",
                idempotency_key=idempotency_key or f"reload:{snapshot.revision}",
                pending_revision=0,
                payload=payload,
                source_baseline={},
                candidate_digest=snapshot.revision,
                effective_digest=snapshot.revision,
                target_generation=None,
                fencing_token=None,
                state="candidate_validated",
                last_error=None,
                created_at=snapshot.loaded_at,
            )
        baseline, source_generation, _, _ = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置候选必须是对象")
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        candidate_id = f"candidate_{source_generation}_{snapshot.revision}"
        if source_edit is not None:
            candidate_id = "candidate_source_" + hashlib.sha256(str(idempotency_key).encode()).hexdigest()
            existing = self._workspace_state_store.get_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN, candidate_id=candidate_id,
            )
            if existing is not None:
                return self._workspace_state_store.bind_source_edit_config_candidate(
                    candidate=existing, payload=payload, source_baseline=baseline,
                    candidate_digest=snapshot.revision, source_generation=source_generation,
                    source_edit=source_edit,
                )
        pending = self._workspace_state_store.create_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=candidate_id,
            idempotency_key=(
                idempotency_key or f"reload:{source_generation}:{snapshot.revision}"
            ),
            payload=payload,
            source_baseline=baseline,
            candidate_digest=snapshot.revision,
            effective_digest=snapshot.revision,
            target_generation="workspace-runtime",
            fencing_token=None,
            state="candidate_validated",
            base_active_revision=(active.active_revision if active is not None else None),
            source_generation=source_generation,
            source_edit=source_edit,
        )
        if pending.source_baseline != baseline:
            raise ConfigConflictError(
                "重复 candidate 的 source baseline 与当前候选不一致"
            )
        if pending.state in {"rejected", "conflict", "recovery_required"}:
            pending = self._workspace_state_store.update_pending_config_candidate_state(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                expected_state=pending.state,
                state="candidate_validated",
                last_error=None,
            )
        elif pending.state != "candidate_validated":
            raise ConfigConflictError(
                "重复 candidate 已经处于不可自动重试状态: "
                f"candidate={pending.candidate_id}, state={pending.state}"
            )
        return pending

    def _finish_candidate(
        self,
        pending: ConfigPendingCandidateRecord,
        *,
        attempt_id: str,
        apply_id: str,
        state: ConfigLifecycleState,
        result: ConfigResult,
        error: str,
        changed_paths: tuple[str, ...] = (),
        candidate_ref: str | None = None,
        target_generation: str | None = None,
        fencing_token: str | None = None,
    ) -> None:
        if self._workspace_state_store is None:
            return
        event_id = f"config:{pending.candidate_id}:{result}"
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        self._workspace_state_store.update_pending_config_candidate_state(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            expected_state="applying",
            state=state,
            last_error=error,
            candidate_ref=candidate_ref,
            target_generation=target_generation,
            fencing_token=fencing_token,
            event=ConfigEventInput(
                event_id=event_id,
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision if active is not None else None,
                pending_revision=pending.pending_revision,
                source="workspace-config-service",
                result=result,
                activation_scope=("restart_workspace" if result == "restart_required" else
                    workspace_config_policy().activation_scope_for(changed_paths)),
                changed_paths=changed_paths,
                deferred_paths=(
                    changed_paths if result == "restart_required" else ()
                ),
                error=error,
            ),
        )

    def _source_baseline(
        self,
        snapshot: ConfigSnapshot,
    ) -> tuple[dict[str, object], int, dict[str, int], dict[str, str | None]]:
        baseline: dict[str, object] = {}
        layer_revisions: dict[str, int] = {}
        layer_digests: dict[str, str | None] = {}
        source_generation = 0
        for source in snapshot.source_details:
            layer_key = source.source_key or f"{source.layer}:{source.precedence}"
            # 持久化键 MUST 可由权威表逐字还原（见 `_resolve_persisted_layer`）：
            # 否则恢复路径会退化成有损兜底。这里 fail-closed，绝不写入无法还原的键。
            ConfigSnapshotMixin._resolve_persisted_layer(layer_key)
            # 只持久化 URI（可为 None），绝不持久化 real path。
            baseline[layer_key] = {
                "uri": source.uri,
                "presence": source.presence,
                "layer_revision": source.layer_revision,
                "layer_digest": source.layer_digest,
                "source_generation": source.source_generation,
            }
            if source.layer_revision is not None:
                layer_revisions[layer_key] = source.layer_revision
            layer_digests[layer_key] = source.layer_digest
            if source.source_generation is not None:
                source_generation = max(source_generation, source.source_generation)
        return baseline, source_generation, layer_revisions, layer_digests

    def _persist_initial_active_snapshot(self) -> None:
        if self._workspace_state_store is None:
            return
        snapshot = self.get_snapshot()
        baseline, source_generation, layer_revisions, layer_digests = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置快照必须是对象")
        self._workspace_state_store.ensure_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=snapshot.revision,
            secret_bindings=build_secret_binding_summary(snapshot.to_dict()),
            schema_version=2,
            promoted_generation="bootstrap",
        )

    def _persist_active_snapshot(
        self,
        snapshot: ConfigSnapshot,
        *,
        candidate_id: str | None = None,
        expected_active_revision: int | None = None,
        expected_pending_revision: int | None = None,
        expected_pending_state: ConfigLifecycleState | None = None,
        expected_fencing_token: str | None = None,
        expected_source_baseline: dict[str, object] | None = None,
        expected_source_generation: int | None = None,
        expected_layer_revisions: dict[str, int] | None = None,
        expected_layer_digests: dict[str, str | None] | None = None,
        apply_id: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> None:
        if self._workspace_state_store is None:
            return
        baseline, source_generation, layer_revisions, layer_digests = (
            self._source_baseline(snapshot)
        )
        payload = prepare_config_for_persistence(snapshot.to_dict())
        if not isinstance(payload, dict):
            raise TypeError("脱敏配置快照必须是对象")
        self._workspace_state_store.promote_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=candidate_id or f"candidate_{snapshot.revision}",
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=snapshot.revision,
            secret_bindings=build_secret_binding_summary(snapshot.to_dict()),
            schema_version=2,
            promoted_generation="workspace-runtime",
            promoted_apply_id=apply_id,
            expected_active_revision=expected_active_revision,
            expected_source_generation=(
                expected_source_generation
                if expected_source_generation is not None
                else source_generation
            ),
            expected_source_baseline=expected_source_baseline,
            expected_layer_revisions=(
                expected_layer_revisions
                if expected_layer_revisions is not None
                else layer_revisions
            ),
            expected_layer_digests=(
                expected_layer_digests
                if expected_layer_digests is not None
                else layer_digests
            ),
            expected_pending_revision=expected_pending_revision,
            expected_pending_state=expected_pending_state,
            expected_fencing_token=expected_fencing_token,
            event=event,
        )

    async def start_watching(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
        shadow_reconcile: ShadowReconcile | None = None,
    ) -> None:
        if self._watcher is not None:
            raise RuntimeError("配置文件监听器不允许重复启动")
        if shadow_reconcile is not None:
            if self._shadow_lifecycle.readiness != "cold":
                raise RuntimeError("配置资源 reconciler 必须在 bootstrap 前绑定")
            self._shadow_reconcile = shadow_reconcile
        await self._ensure_shadow_started()
        self._candidate_applier = candidate_applier
        directories, candidate_paths = self._config_watch_paths()
        watcher = ConfigFileWatcher(
            directories=directories,
            candidate_paths=candidate_paths,
            on_change=lambda: self._reload_from_watcher(
                candidate_applier=candidate_applier,
            ),
        )
        await watcher.start()
        self._watcher = watcher
        self._update_config_watch_paths(watcher)
        if (
            self._workspace_state_store is not None
            and self._workspace_state_store.get_config_apply_claim(config_domain=self._CONFIG_DOMAIN) is not None
        ):
            # 快速重启时旧 lease 可能尚未过期；不能只做一次启动扫描后
            # 永久停在 applying，也不能提前抢走仍有效的 fencing claim。
            self._apply_recovery_task = asyncio.create_task(
                self._recover_startup_apply(candidate_applier=candidate_applier),
                name="workspace-config-apply-recovery",
            )
            self._apply_recovery_task.add_done_callback(self._report_apply_recovery_failure)

    def _report_apply_recovery_failure(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("workspace 配置启动对账失败", exc_info=(type(error), error, error.__traceback__))

    async def _recover_startup_apply(
        self, *, candidate_applier: ConfigCandidateApplier | None,
    ) -> None:
        store = self._workspace_state_store
        if store is None:
            raise RuntimeError("配置启动对账缺少 workspace store")
        while (claim := store.get_config_apply_claim(config_domain=self._CONFIG_DOMAIN)) is not None:
            await asyncio.sleep(max(0, (claim.lease_expires_at - datetime.now(UTC)).total_seconds()))
            journal = store.get_config_apply_journal(apply_id=claim.apply_id)
            store.recover_expired_config_applies(config_domain=self._CONFIG_DOMAIN)
            if journal is not None and any(
                effect.get("resource") == "workspace-plugin-runtime"
                for effect in journal.side_effects
            ):
                return
        # 只有未触及插件候选等破坏性外部副作用时，才沿原 reload 恢复 HTTP 等可重建连接。
        await self._reload_from_watcher(candidate_applier=candidate_applier)

    def _config_watch_paths(self) -> tuple[set[Path], set[Path]]:
        directories = {self._get_workspace_config_path().parent}
        candidate_paths = {
            self._get_workspace_config_path(),
            self._get_workspace_local_config_path(),
        }
        if self._workspace_root is not None:
            workspace_config_dir = self._workspace_root / ".boxteam"
            directories.add(workspace_config_dir)
            candidate_paths.add(workspace_config_dir / "workspace.jsonc")
        instruction_paths = self._instruction_inputs.watch_paths(
            self._snapshot_store.current().to_dict(),
            include_candidate=not self._snapshot_store.status().healthy,
        )
        candidate_paths.update(instruction_paths)
        for path in instruction_paths:
            parent = path.parent
            while not parent.is_dir():
                candidate_paths.add(parent)
                parent = parent.parent
            directories.add(parent)
        return directories, candidate_paths

    def _update_config_watch_paths(self, watcher: ConfigFileWatcher) -> None:
        directories, candidates = self._config_watch_paths()
        watcher.replace_candidates(directories=directories, candidate_paths=candidates)

    async def stop_watching(self) -> None:
        recovery, self._apply_recovery_task = self._apply_recovery_task, None
        if recovery is not None:
            recovery.cancel()
            try:
                await recovery
            except asyncio.CancelledError:
                pass
        watcher = self._watcher
        self._watcher = None
        if watcher is None:
            self._candidate_applier = None
            return
        await watcher.stop()
        self._candidate_applier = None

    async def close(self) -> None:
        """停止文件监听并关闭 workspace 配置 shadow generation。"""
        await self.stop_watching()
        await self._shadow_lifecycle.close()

    async def _reload_from_watcher(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None,
    ) -> None:
        try:
            await self.reload(candidate_applier=candidate_applier)
        except Exception as error:  # noqa: BLE001 —— reload 已记录完整堆栈并继续监听的显式兜底
            # reload 已写入失败状态并发布 failed 事件；watcher 继续等待后续修复，
            # 同时必须记录完整堆栈，不能把失败静默转换成成功。
            logger.exception("workspace 配置 watcher reload 失败", exc_info=error)
            return


__all__ = ["ConfigReloadMixin"]
