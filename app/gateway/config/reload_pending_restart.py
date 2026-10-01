from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

from app.services.infrastructure.config import ConfigReloadStatus
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    build_secret_binding_summary,
    changed_json_paths,
    new_config_id,
    prepare_config_for_persistence,
)

from .loader import (
    _consumer_health_digests_from_proof,
    _require_gateway_consumer_health_digests,
)
from .sources import record_gateway_restart_startup_failure


class ReloadPendingRestartMixin:
    def load_pending_candidate(
        self,
        *,
        candidate_ref: str,
        gateway_id: str | None = None,
    ):
        """供受控新 Gateway generation 使用；ref 不匹配时绝不回退到 active。"""
        return self._state_store.load_gateway_pending_candidate(
            candidate_ref=candidate_ref,
            gateway_id=gateway_id,
        )
    def begin_pending_restart(self, *, candidate_ref: str):
        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway pending intent 已过期，必须显式重试"
            )
        self._assert_gateway_restart_intent_owner(intent)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None or pending.state not in {
            "pending_restart",
            "applying",
            "recovery_required",
        }:
            raise ConfigConflictError(
                "Gateway restart intent 绑定的 pending candidate 不存在或不可加载"
            )
        if intent.state in {"pending", "recovery_required"}:
            _, pending, _ = self._state_store.begin_gateway_restart_apply(
                candidate_ref=candidate_ref,
                attempt_id=new_config_id("attempt"),
                apply_id=new_config_id("apply"),
                owner="gateway-restart-supervisor",
            )
            return pending
        if intent.state != "applying":
            raise ConfigConflictError(
                f"Gateway restart intent 当前不可开始: state={intent.state}"
            )
        claim = self._state_store.get_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN
        )
        if claim is None or claim.candidate_id != intent.candidate_id:
            raise ConfigConflictError(
                "Gateway restart intent 已 applying，但缺少匹配 apply claim"
            )
        if claim.lease_expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway restart intent 的 apply claim 已过期，必须先进入 recovery_required"
            )
        return self.load_pending_candidate(
            candidate_ref=candidate_ref,
            gateway_id=self._gateway_id,
        )
    def record_pending_restart_failure(
        self,
        *,
        candidate_ref: str,
        target_generation: str,
        fencing_token: str,
        error: str,
    ) -> ConfigReloadStatus:
        """将失败的 Gateway pending 固定到恢复态，等待显式重试。"""

        if self._gateway_id is None:
            raise ConfigConflictError(
                "Gateway 启动失败回报缺少当前 Gateway identity"
            )
        record_gateway_restart_startup_failure(
            state_store=self._state_store,
            candidate_ref=candidate_ref,
            error=error,
            gateway_id=self._gateway_id,
            target_generation=target_generation,
            fencing_token=fencing_token,
        )
        return self.status()
    def retry_pending_restart(
        self,
        *,
        candidate_ref: str,
        requested_by: str = "gateway-restart-retry",
    ) -> ConfigReloadStatus:
        """为恢复态 pending 生成新的 generation/token，等待下一次受控重启。"""

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        self._state_store.retry_gateway_restart(
            candidate_ref=candidate_ref,
            target_generation=new_config_id("gateway_generation"),
            requested_by=requested_by,
        )
        return self.status()
    def discard_pending_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
    ) -> ConfigReloadStatus:
        """仅在旧 Gateway active/source 基线安全时丢弃 pending。"""

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None:
            raise ConfigConflictError(
                "Gateway restart intent 绑定的 pending candidate 不存在"
            )
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        if active is None:
            raise ConfigConflictError("Gateway pending discard 缺少 active snapshot")
        changed_paths = changed_json_paths(active.payload, pending.payload)
        self._state_store.discard_gateway_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
            expected_source_baseline=pending.source_baseline,
            reason="用户显式丢弃 Gateway pending candidate",
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:discarded",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=pending.last_apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision,
                pending_revision=pending.pending_revision,
                source="gateway-config-api",
                result="discarded",
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
            ),
        )
        return self.status()
    def resolve_pending_restart(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
    ) -> ConfigReloadStatus:
        """使用匹配的 Gateway proof 完成 recovery_required promotion。"""

        self.record_pending_restart_proof(
            candidate_ref=candidate_ref,
            health_proof=health_proof,
        )
        return self.status()
    def _finish_gateway_candidate(
        self,
        pending,
        *,
        attempt_id: str,
        apply_id: str,
        state,
        result,
        error: str,
        changed_paths: tuple[str, ...],
    ) -> None:
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        self._state_store.update_pending_config_candidate_state(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            expected_state="applying",
            state=state,
            last_error=error,
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:{result}",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision if active is not None else None,
                pending_revision=pending.pending_revision,
                source="gateway-config-watcher",
                result=result,
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
                error=error,
            ),
        )
    def record_pending_restart_proof(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
        runtime_generation_id: str | None = None,
        old_generation_id: str | None = None,
    ):
        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        if not health_proof.get("generation") or not health_proof.get("health_digest"):
            raise ValueError(
                "Gateway health proof 必须包含 generation 和 health_digest"
            )
        if health_proof["generation"] != intent.target_generation:
            raise ConfigConflictError("Gateway health proof generation 不匹配")
        consumer_health_digests = _require_gateway_consumer_health_digests(
            _consumer_health_digests_from_proof(health_proof)
        )
        expected_proof = self.build_pending_restart_health_proof(
            candidate_ref=candidate_ref,
            generation=intent.target_generation,
            consumer_health_digests=consumer_health_digests,
        )
        if health_proof != expected_proof:
            raise ConfigConflictError(
                "Gateway health proof 与 pending candidate 不匹配"
            )
        runtime_generation_id = runtime_generation_id or str(health_proof["generation"])
        old_generation_id = (
            old_generation_id
            if old_generation_id is not None
            else intent.old_generation
        )
        pending = self.begin_pending_restart(candidate_ref=candidate_ref)
        claim = self._state_store.get_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN
        )
        if claim is None or claim.candidate_id != pending.candidate_id:
            raise ConfigConflictError("Gateway pending promotion 缺少匹配 apply claim")
        journal = self._state_store.get_config_apply_journal(apply_id=claim.apply_id)
        if journal is None:
            raise ConfigConflictError("Gateway pending promotion 缺少 apply journal")
        baseline = pending.source_baseline
        if not isinstance(baseline, dict):
            raise TypeError("Gateway pending source baseline 必须是对象")
        layer_revisions: dict[str, int] = {}
        layer_digests: dict[str, str | None] = {}
        source_generation = 0
        for key, raw_detail in baseline.items():
            if not isinstance(key, str) or not isinstance(raw_detail, dict):
                raise TypeError("Gateway pending source baseline 结构无效")
            raw_revision = raw_detail.get("layer_revision")
            if raw_revision is not None:
                layer_revisions[key] = int(raw_revision)
            raw_digest = raw_detail.get("layer_digest")
            layer_digests[key] = str(raw_digest) if raw_digest is not None else None
            raw_generation = raw_detail.get("source_generation")
            if raw_generation is not None:
                source_generation = max(source_generation, int(raw_generation))
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        changed_paths = (
            changed_json_paths(active.payload, pending.payload)
            if active is not None
            else ()
        )
        promoted = self._state_store.promote_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=pending.candidate_id,
            payload=prepare_config_for_persistence(pending.payload),
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=layer_revisions,
            layer_digests=layer_digests,
            effective_digest=pending.effective_digest,
            schema_version=2,
            expected_active_revision=(
                active.active_revision if active is not None else None
            ),
            expected_pending_revision=pending.pending_revision,
            expected_source_baseline=baseline,
            expected_source_generation=(
                pending.source_generation
                if pending.source_generation is not None
                else source_generation
            ),
            expected_layer_revisions=layer_revisions,
            expected_layer_digests=layer_digests,
            expected_registry_revision=journal.registry_revision,
            expected_pending_state="applying",
            expected_fencing_token=claim.fencing_token,
            promoted_apply_id=claim.apply_id,
            secret_bindings=build_secret_binding_summary(
                pending.payload,
                resolve_environment=True,
            ),
            promoted_generation=intent.target_generation,
            gateway_candidate_ref=candidate_ref,
            gateway_health_proof=health_proof,
            gateway_runtime_generation_id=runtime_generation_id,
            gateway_old_generation_id=old_generation_id,
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:active",
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=claim.apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=None,
                pending_revision=pending.pending_revision,
                source="gateway-restart-supervisor",
                result="applied",
                activation_scope=gateway_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
                applied_paths=changed_paths,
            ),
        )
        self._status = replace(
            self._status,
            healthy=True,
            revision=pending.effective_digest,
            last_success_at=datetime.now(timezone.utc),
            last_error=None,
            restart_required=False,
            reason=None,
            state="active",
            active_revision=promoted.active_revision,
            pending_revision=None,
            candidate_id=None,
            candidate_ref=None,
            attempt_id=None,
            apply_id=None,
            applied_paths=(),
            deferred_paths=(),
        )
        self._state_store.release_config_apply_claim(
            config_domain=self._CONFIG_DOMAIN,
            apply_id=claim.apply_id,
            fencing_token=claim.fencing_token,
        )
        return promoted
    def build_pending_restart_health_proof(
        self,
        *,
        candidate_ref: str,
        generation: str,
        consumer_health_digests: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """构造不含秘密的 Gateway 自身健康证明。"""

        if self._gateway_id is None:
            raise ConfigConflictError(
                "Gateway health proof 缺少 gateway_id，不能用于 pending promotion"
            )

        intent = self._state_store.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        if intent is None:
            raise ConfigConflictError(f"Gateway candidate_ref 不存在: {candidate_ref}")
        self._assert_gateway_restart_intent_owner(intent)
        if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
            raise ConfigConflictError(
                "Gateway pending intent 已过期，必须显式重试"
            )
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN,
            candidate_id=intent.candidate_id,
        )
        if pending is None or pending.state not in {
            "pending_restart",
            "applying",
            "recovery_required",
        }:
            raise ConfigConflictError(
                "Gateway health proof 缺少可证明的 pending candidate"
            )
        if intent.state not in {"pending", "applying", "recovery_required"}:
            raise ConfigConflictError(
                "Gateway health proof 当前不允许用于该 restart intent: "
                f"state={intent.state}"
            )
        if pending.target_generation != generation:
            raise ConfigConflictError("Gateway health proof generation 不匹配 pending")
        if pending.fencing_token != intent.fencing_token:
            raise ConfigConflictError("Gateway health proof fencing token 不匹配")
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        secret_bindings = build_secret_binding_summary(
            pending.payload,
            resolve_environment=True,
        )
        secret_binding_digest = hashlib.sha256(
            json.dumps(
                secret_bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fencing_token_digest = hashlib.sha256(
            intent.fencing_token.encode("utf-8")
        ).hexdigest()
        proof_payload = {
            "config_domain": "gateway",
            "candidate_ref": candidate_ref,
            "candidate_id": pending.candidate_id,
            "generation": generation,
            "loaded_source": "pending",
            "active_revision": active.active_revision if active is not None else None,
            "pending_revision": pending.pending_revision,
            "candidate_digest": pending.candidate_digest,
            "effective_digest": pending.effective_digest,
            "secret_binding_digest": secret_binding_digest,
            "fencing_token_digest": fencing_token_digest,
            "consumer_health_digests": _require_gateway_consumer_health_digests(
                consumer_health_digests or {}
            ),
        }
        if self._gateway_id is not None:
            proof_payload["gateway_id"] = self._gateway_id
        health_digest = hashlib.sha256(
            json.dumps(
                proof_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {**proof_payload, "health_digest": health_digest}
