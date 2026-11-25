"""ConfigPublicMixin：ConfigService 的公开配置构建、provider 列表与日志级别方法族（纯搬迁）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from app.runtime.network_proxy import ProxyRoutes
from app.schemas.gateway.secrets import ResolvedSecret
from app.schemas.internal_v2.config import (
    ConfigActiveOptionsInspectionDTO,
    ConfigDTO,
    ConfigReloadStatusDTO,
    ConfigSourceBaselineDTO,
    ConfigUpdateRequest,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.snapshot import ConfigSnapshot
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigPendingCandidateRecord,
    prepare_config_for_persistence,
    restore_environment_secret_references,
)
from configs.inheritance.values import thaw
from configs.network import ExplicitProxy, NetworkSettings

if TYPE_CHECKING:
    from app.services.infrastructure.config.instruction_inputs import (
        InstructionFileInputs,
    )
    from app.services.infrastructure.config_service import ConfigService


class ConfigPublicMixin:
    if TYPE_CHECKING:
        _instruction_inputs: InstructionFileInputs

    def get_reload_status_dto(self) -> ConfigReloadStatusDTO:
        service = cast("ConfigService", self)
        with service.publication_lock:
            status = service.get_reload_status()
            payload = asdict(status)
            payload.update(last_success_at=status.last_success_at.isoformat(),
                           last_attempt_at=status.last_attempt_at.isoformat(),
                           layer_digests=status.layer_digests or {}, mcp_cleanup=list(service.get_mcp_cleanup_diagnostics()))
            return ConfigReloadStatusDTO.model_validate(payload)

    async def update(self, payload: ConfigUpdateRequest) -> ConfigDTO:
        runtime_keys = frozenset(
            {
                "default_model",
                "default_orchestration",
                "max_concurrent_agents",
                "allow_shell_tools",
                "ignored_paths",
                "auto_summarize",
            }
        )
        update_data = {
            key: value
            for key, value in payload.model_dump(exclude_unset=True).items()
            if key in runtime_keys
        }
        if self._workspace_state_store is not None:
            required_cas_fields = {
                "base_layer_revision",
                "base_layer_digest",
                "expected_active_revision",
                "expected_active_digest",
            }
            missing_cas_fields = required_cas_fields.difference(
                payload.model_fields_set
            )
            if missing_cas_fields:
                raise ConfigConflictError(
                    "配置 API/UI 写入必须声明完整 CAS 基线，缺少: "
                    + ", ".join(sorted(missing_cas_fields))
                )
            existing = self._workspace_state_store.get_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN,
                idempotency_key=payload.idempotency_key,
            )
            if (
                existing is not None
                and existing.state in {"active", "pending_restart"}
                and self._is_replayed_runtime_update(
                    existing,
                    updates=update_data,
                )
            ):
                return self._build_public_config()
        self._sync_runtime_override_source(
            updates=update_data,
            base_layer_revision=payload.base_layer_revision,
            base_layer_digest=payload.base_layer_digest,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
        if self._snapshot_store.has_snapshot():
            await self.reload(
                candidate_applier=self._candidate_applier,
                idempotency_key=payload.idempotency_key,
            )
        return self._build_public_config()

    def _is_replayed_runtime_update(
        self,
        pending: ConfigPendingCandidateRecord,
        *,
        updates: dict[str, Any],
    ) -> bool:
        """只对同一 source 基线和同一 runtime payload 的重试做幂等短路。"""

        if self._workspace_state_store is None:
            return False
        baseline = pending.source_baseline.get(self._RUNTIME_OVERRIDE_CONFIG_KEY)
        source = self._workspace_state_store.get_source_layer(
            self._RUNTIME_OVERRIDE_CONFIG_KEY
        )
        if not isinstance(baseline, dict) or source is None:
            return False
        if (
            baseline.get("uri") != source.uri
            or baseline.get("presence") != source.presence
            or baseline.get("layer_revision") != source.layer_revision
            or baseline.get("layer_digest") != source.layer_digest
            or baseline.get("source_generation") != source.source_generation
        ):
            return False
        current_payload = source.payload or {}
        return all(
            key not in current_payload if value is None else current_payload.get(key) == value
            for key, value in updates.items()
        )

    def _build_public_config(self) -> ConfigDTO:
        snapshot = self._require_snapshot()
        config = snapshot.to_dict()
        reload_status = self.get_reload_status()
        public_config = self._load_public_runtime_config(config)

        default_model = public_config.get("default_model")
        if default_model is None:
            default_model = self._llm_resolution.resolve_default_model(snapshot.graph)

        default_orchestration = public_config.get(
            "default_orchestration", "single_agent"
        )
        max_concurrent_agents = public_config.get("max_concurrent_agents", 4)
        allow_shell_tools = public_config.get("allow_shell_tools", False)
        ignored_paths = public_config.get("ignored_paths", [])
        auto_summarize = public_config.get("auto_summarize", True)

        return ConfigDTO(
            default_model=default_model,
            default_orchestration=default_orchestration,
            max_concurrent_agents=max_concurrent_agents,
            allow_shell_tools=allow_shell_tools,
            ignored_paths=ignored_paths,
            auto_summarize=auto_summarize,
            metadata={
                "default_agent_id": self.get_default_agent_id(),
                "source": "workspace",
                "runtime_overrides": sorted(self._runtime_config_overrides.keys()),
                "revision": snapshot.revision,
                "instruction_inputs": self._instruction_inputs.diagnostics(config),
                # 来源位置一律只以 URI 表达：``source_details[].uri`` 是唯一出处。
                # 原先此处的 ``config_path``（真实 user 层路径）与 ``source_paths``
                # （跨 user/user_local/workspace/runtime_override 四层的真实路径列表）既无 URI
                # 替代（上述层共享同一 ``workspace.sqlite`` 边界载体，故不可寻址，与
                # URI scope 闭集无关），又会外泄真实路径，故 MUST 从对外响应体移除；
                # 不得以空串/省略号做替身。
                "source_details": [
                    {
                        "uri": source.uri,
                        "layer": source.layer,
                        "precedence": source.precedence,
                        "loaded": source.loaded,
                        "source_key": source.source_key,
                        "presence": source.presence,
                        "layer_revision": source.layer_revision,
                        "layer_digest": source.layer_digest,
                        "source_generation": source.source_generation,
                    }
                    for source in snapshot.source_details
                ],
                "policy_manifest": list(
                    workspace_config_policy().policy_manifest()
                ),
                "reload": {
                    "healthy": reload_status.healthy,
                    "restart_required": reload_status.restart_required,
                    "reason": reload_status.reason,
                    "changed_sections": list(reload_status.changed_sections),
                    "last_success_at": reload_status.last_success_at.isoformat(),
                    "last_attempt_at": reload_status.last_attempt_at.isoformat(),
                    "last_error": reload_status.last_error,
                    "state": reload_status.state,
                    "active_revision": reload_status.active_revision,
                    "pending_revision": reload_status.pending_revision,
                    "candidate_id": reload_status.candidate_id,
                    "attempt_id": reload_status.attempt_id,
                    "apply_id": reload_status.apply_id,
                    "layer_digests": reload_status.layer_digests or {},
                    "applied_paths": list(reload_status.applied_paths),
                    "deferred_paths": list(reload_status.deferred_paths),
                },
            },
        )

    def _load_public_runtime_config(self, config: dict[str, Any]) -> dict[str, Any]:
        raw_public_config = config.get("ui", {})
        if raw_public_config is None:
            raw_public_config = {}
        if not isinstance(raw_public_config, dict):
            raise ValueError("ui 配置必须是对象")
        return {**raw_public_config, **self._runtime_config_overrides}

    def list_llm_models(self) -> list[dict[str, Any]]:
        return self._llm_resolution.list_models(self._require_snapshot().graph)

    def get_active_options_inspection(
        self, *, gateway_id: str, provider_id: str, model_id: str | None = None,
    ) -> ConfigActiveOptionsInspectionDTO:
        """只投影同一已发布快照；文件变化和草稿仍由原预检／CAS 处理。"""
        service = cast("ConfigService", self)
        with service.publication_lock:
            if not service._snapshot_store.has_snapshot():
                raise RuntimeError("已发布选项检查要求工作区已完成配置初始化")
            snapshot = service.get_active_snapshot()
            store = service._workspace_state_store
            if store is None or service._workspace_root != store.workspace_root:
                raise ValueError("选项检查需要已绑定精确工作区的配置 owner")
            active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
            if active is None or active.effective_digest != snapshot.revision:
                raise ConfigConflictError("已发布配置与运行快照不一致，请重新加载配置来源")
            sources, _ = service._persisted_source_details(active.source_baseline)
            if {source.source_key: source for source in sources} != {
                source.source_key: source for source in snapshot.source_details
            }:
                raise ConfigConflictError("已发布配置来源身份与运行快照不一致，请重新加载配置来源")
        baseline = {source.source_key: ConfigSourceBaselineDTO(
            uri=source.uri, presence=source.presence, layer_revision=source.layer_revision,
            layer_digest=source.layer_digest, source_generation=source.source_generation,
            file_presence=source.presence if source.uri is not None else None,
            file_digest=source.layer_digest if source.uri is not None else None,
        ) for source in sources}
        return ConfigActiveOptionsInspectionDTO(
            gateway_id=gateway_id, workspace_id=snapshot.graph.owner_id,
            active_revision=active.active_revision, active_digest=active.effective_digest,
            source_baseline=baseline,
            provider_options=service._llm_resolution.inspect_provider(snapshot.graph, provider_id),
            model_options=service._llm_resolution.inspect_model_reference(snapshot.graph,
                ModelReferenceDTO(provider_id=provider_id, model_id=model_id)) if model_id is not None else None,
        )

    def get_llm_model(self, model: ModelReferenceDTO) -> dict[str, Any]:
        return self._llm_resolution.get_model(model)

    async def prepare_llm_credentials(self, providers: list[dict[str, Any]]) -> dict[str, ResolvedSecret]:
        from app.services.infrastructure.gateway_client_common import (
            GatewayTransportConnection,
        )
        from app.services.infrastructure.gateway_secrets.client import (
            GatewaySecretsClient,
        )

        references: list[str] = []
        for provider in providers:
            key = provider.get("api_key")
            if isinstance(key, str) and key.startswith("secret:"):
                references.append(key.removeprefix("secret:"))
            for name in ("network", "auth_network"):
                if name not in provider:
                    continue
                proxy = NetworkSettings.model_validate(provider[name]).proxy
                if isinstance(proxy, ExplicitProxy) and proxy.auth is not None:
                    references.append(proxy.auth.password.removeprefix("secret:"))
        references = list(dict.fromkeys(references))
        if not references:
            return {}
        return await GatewaySecretsClient(GatewayTransportConnection(config_service=cast("ConfigService", self))).resolve(references)

    def use_llm_credentials(self, credentials: dict[str, ResolvedSecret]):
        """只在构建／执行作用域展开；配置快照始终保留引用。"""
        return self._llm_resolution.use_secrets(credentials)

    async def resolve_llm_model(self, model: ModelReferenceDTO) -> dict[str, Any]:
        resolved = self.get_llm_model(model)
        credentials = await self.prepare_llm_credentials([resolved])
        with self.use_llm_credentials(credentials):
            return self._llm_resolution.expand_model(resolved)

    def freeze_model_recipe(self, reference: ModelReferenceDTO) -> dict[str, object]:
        """冻结已选模型的解析选项；凭据保留引用，沿原配置 owner 序列化。"""
        resolved = self._llm_resolution.resolve_model_reference(
            self._require_snapshot().graph, reference, expand_credentials=False,
        )
        return prepare_config_for_persistence(thaw(resolved))

    async def prepare_model_recipe(self, recipe: Mapping[str, object]) -> dict[str, Any]:
        """执行只展开首次冻结的模型，不重新查询热更新后的默认配置。"""
        resolved = cast(
            dict[str, Any], restore_environment_secret_references(dict(recipe)),
        )
        credentials = await self.prepare_llm_credentials([resolved])
        with self.use_llm_credentials(credentials):
            return self._llm_resolution.expand_model(resolved)

    async def resolve_workspace_proxy_routes(self, *, snapshot: ConfigSnapshot | None = None) -> ProxyRoutes:
        """当前或指定候选的默认出口；实际网络只交给原消费者创建和释放。"""
        graph = (snapshot if snapshot is not None else self._require_snapshot()).graph
        network = thaw(graph.resolve_scope().settings["network"])
        credentials = await self.prepare_llm_credentials([{"network": network}])
        proxy = NetworkSettings.model_validate(network).proxy
        password = (credentials[proxy.auth.password.removeprefix("secret:")].value.get_secret_value()
                    if isinstance(proxy, ExplicitProxy) and proxy.auth is not None else None)
        return ProxyRoutes.from_configuration(proxy, environment=graph.proxy_environment, password=password)

    def get_default_agent_runtime_config(self) -> dict[str, Any]:
        return self.get_agent_runtime_config(self.get_default_agent_id())

    def development_test_tools_enabled(self) -> bool:
        config = self._get_effective_config()
        development = config.get("development", {})
        if development is None:
            return False
        if not isinstance(development, dict):
            raise ValueError("development 配置必须是对象")
        enabled = development.get("test_tools", False)
        if not isinstance(enabled, bool):
            raise ValueError("development.test_tools 必须是布尔值")
        return enabled


__all__ = ["ConfigPublicMixin"]
