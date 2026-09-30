"""ConfigPublicMixin：ConfigService 的公开配置构建、provider 列表与日志级别方法族（纯搬迁）。"""

from __future__ import annotations

from typing import Any

from app.schemas.internal_v2.config import ConfigDTO, ConfigUpdateRequest
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigPendingCandidateRecord,
)


class ConfigPublicMixin:
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
            baseline.get("vrn") != source.vrn
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
        config = self._get_effective_config()
        snapshot = self._require_snapshot()
        reload_status = self.get_reload_status()
        public_config = self._load_public_runtime_config(config)

        default_model = public_config.get("default_model")
        if default_model is None:
            default_model = self._llm_resolution.resolve_default_model(config)

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
                # 来源位置一律只以 VRN 表达：``source_details[].vrn`` 是唯一出处。
                # 原先此处的 ``config_path``（真实 user 层路径）与 ``source_paths``
                # （跨 user/user_local/workspace/sqlite 四层的真实路径列表）既无 VRN
                # 替代（上述层共享同一 ``workspace.sqlite`` 边界载体，故不可寻址，与
                # VRN scope 闭集无关），又会外泄真实路径，故 MUST 从对外响应体移除；
                # 不得以空串/省略号做替身。
                "source_details": [
                    {
                        "vrn": source.vrn,
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

    def get_llm_providers(self) -> list[dict]:
        return self._llm_resolution.get_llm_providers()

    def get_llm_provider(self, provider_id: str) -> dict[str, Any]:
        return self._llm_resolution.get_llm_provider(provider_id)

    def get_default_agent_runtime_config(self) -> dict[str, Any]:
        return self.get_agent_runtime_config(self.get_default_agent_id())

    def get_logger_level(self) -> str:
        config = self._get_effective_config()
        logger_config = config.get("logger", {})
        if not isinstance(logger_config, dict):
            raise ValueError("logger 配置必须是对象")
        level = logger_config.get("level", "info")
        if not isinstance(level, str):
            raise ValueError("logger.level 必须是字符串")
        normalized_level = level.strip().upper()
        if normalized_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError(
                "logger.level 仅支持 debug、info、warning、error、critical"
            )
        return normalized_level

    def get_logger_pretty(self) -> bool:
        config = self._get_effective_config()
        logger_config = config.get("logger", {})
        if not isinstance(logger_config, dict):
            raise ValueError("logger 配置必须是对象")
        pretty = logger_config.get("pretty", True)
        if not isinstance(pretty, bool):
            raise ValueError("logger.pretty 必须是布尔值")
        return pretty

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
