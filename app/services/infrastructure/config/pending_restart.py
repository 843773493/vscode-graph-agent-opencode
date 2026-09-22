from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.snapshot import ConfigReloadStatus
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    ConfigResult,
    build_secret_binding_summary,
    changed_json_paths,
    dump_json,
    new_config_id,
    prepare_config_for_persistence,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class PendingRestartCoordinator:
    """集中承载 Workspace pending candidate 的待重启契约链路。"""

    def __init__(
        self,
        *,
        store: WorkspaceStateStore | None,
        config_domain: str,
        reload_status_provider: Callable[[], ConfigReloadStatus],
    ) -> None:
        self._store = store
        self._config_domain = config_domain
        self._reload_status_provider = reload_status_provider

    def get_pending_startup_contract(
        self,
        *,
        candidate_ref: str,
    ) -> dict[str, object]:
        """只返回新 Workspace generation 所需的 pending 绑定元数据。"""

        if self._store is None:
            raise ConfigConflictError("当前 Workspace 没有 Workspace-owned 状态库")
        pending = self._store.load_pending_config_candidate(
            candidate_ref=candidate_ref
        )
        if not pending.target_generation or not pending.fencing_token:
            raise ConfigConflictError(
                "Workspace pending candidate 缺少 target generation 或 fencing token"
            )
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
        return {
            "config_domain": self._config_domain,
            "candidate_ref": candidate_ref,
            "candidate_id": pending.candidate_id,
            "pending_revision": pending.pending_revision,
            "candidate_digest": pending.candidate_digest,
            "effective_digest": pending.effective_digest,
            "target_generation": pending.target_generation,
            "fencing_token": pending.fencing_token,
            "secret_binding_digest": secret_binding_digest,
        }

    def record_pending_restart_failure(
        self,
        *,
        candidate_ref: str,
        error: str,
        old_runtime_recovered: bool = True,
    ) -> ConfigReloadStatus:
        """记录重启失败，并根据旧 generation 是否恢复选择恢复状态。"""

        if not error:
            raise ValueError("Workspace restart_failed 错误不能为空")
        if self._store is None:
            raise ConfigConflictError("当前 Workspace 没有 Workspace-owned 状态库")
        pending = self._store.load_pending_config_candidate(
            candidate_ref=candidate_ref
        )
        active = self._store.get_active_config_snapshot(
            self._config_domain
        )
        target_state: ConfigLifecycleState = (
            "pending_restart" if old_runtime_recovered else "recovery_required"
        )
        result: ConfigResult = (
            "restart_failed" if old_runtime_recovered else "recovery_required"
        )
        self._store.update_pending_config_candidate_state(
            config_domain=self._config_domain,
            candidate_id=pending.candidate_id,
            expected_state=pending.state,
            state=target_state,
            last_error=error,
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:{result}",
                config_domain=self._config_domain,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=pending.last_apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision if active is not None else None,
                pending_revision=pending.pending_revision,
                source="gateway-runtime-controller",
                result=result,
                activation_scope="restart_workspace",
                deferred_paths=(() if old_runtime_recovered else ()),
                error=error,
            ),
        )
        if pending.last_apply_id is not None:
            journal = self._store.get_config_apply_journal(
                apply_id=pending.last_apply_id
            )
            if journal is not None and journal.state == "applying":
                self._store.update_config_apply_journal(
                    apply_id=journal.apply_id,
                    expected_state="applying",
                    state=("failed" if old_runtime_recovered else "recovery_required"),
                    last_error=error,
                )
        claim = self._store.get_config_apply_claim(
            config_domain=self._config_domain
        )
        if claim is not None and claim.candidate_id == pending.candidate_id:
            self._store.release_config_apply_claim(
                config_domain=self._config_domain,
                apply_id=claim.apply_id,
                fencing_token=claim.fencing_token,
            )
        return self._reload_status_provider()

    def retry_pending_restart(self, *, candidate_ref: str) -> ConfigReloadStatus:
        """为 Workspace pending candidate 创建新的受控重启 generation。"""

        if self._store is None:
            raise ConfigConflictError("当前 Workspace 没有 Workspace-owned 状态库")
        self._store.retry_pending_config_restart(
            candidate_ref=candidate_ref,
            target_generation=new_config_id("workspace_generation"),
        )
        return self._reload_status_provider()

    def resolve_pending_restart(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
    ) -> ConfigReloadStatus:
        """用新 generation 的完整 proof 提升已成功启动的 Workspace candidate。"""

        if self._store is None:
            raise ConfigConflictError("当前 Workspace 没有 Workspace-owned 状态库")
        pending = self._store.load_pending_config_candidate(
            candidate_ref=candidate_ref,
            allow_recovery=True,
        )
        if pending.state not in {"pending_restart", "recovery_required"}:
            raise ConfigConflictError(
                "Workspace pending resolve 只允许 pending_restart 或 "
                "recovery_required candidate: "
                f"state={pending.state}"
            )
        expected_proof = self.build_pending_restart_health_proof(
            candidate_ref=candidate_ref,
            pending=pending,
        )
        if health_proof != expected_proof:
            raise ConfigConflictError(
                "Workspace pending resolve 的 health proof 与 candidate 不匹配"
            )
        if not pending.target_generation or not pending.fencing_token:
            raise ConfigConflictError(
                "Workspace pending candidate 缺少 target generation 或 fencing token"
            )
        active = self._store.get_active_config_snapshot(
            self._config_domain
        )
        if active is None or active.state != "active":
            raise ConfigConflictError(
                "Workspace pending resolve 缺少可确认的旧 active snapshot"
            )

        attempt_id = new_config_id("attempt")
        apply_id = new_config_id("apply")
        resolve_source = (
            "workspace-recovery-resolve"
            if pending.state == "recovery_required"
            else "workspace-restart-resolve"
        )
        resolve_event_suffix = (
            "recovery_active"
            if pending.state == "recovery_required"
            else "restart_active"
        )
        claim = self._store.begin_config_apply(
            config_domain=self._config_domain,
            candidate_id=pending.candidate_id,
            attempt_id=attempt_id,
            apply_id=apply_id,
            owner=resolve_source,
            base_active_revision=active.active_revision,
            target_generation=pending.target_generation,
            pending_revision=pending.pending_revision,
            source_baseline=pending.source_baseline,
            active_baseline=active.source_baseline,
            expected_candidate_state=pending.state,
        )
        baseline = pending.source_baseline
        layer_revisions: dict[str, int] = {}
        layer_digests: dict[str, str | None] = {}
        source_generation = 0
        for key, raw_detail in baseline.items():
            if not isinstance(key, str) or not isinstance(raw_detail, dict):
                raise TypeError("Workspace recovery source baseline 结构无效")
            raw_revision = raw_detail.get("layer_revision")
            if raw_revision is not None:
                layer_revisions[key] = int(raw_revision)
            raw_digest = raw_detail.get("layer_digest")
            layer_digests[key] = str(raw_digest) if raw_digest is not None else None
            raw_generation = raw_detail.get("source_generation")
            if raw_generation is not None:
                source_generation = max(source_generation, int(raw_generation))
        changed_paths = changed_json_paths(active.payload, pending.payload)
        try:
            self._store.promote_active_config_snapshot(
                config_domain=self._config_domain,
                candidate_id=pending.candidate_id,
                payload=prepare_config_for_persistence(pending.payload),
                source_baseline=baseline,
                source_generation=source_generation,
                layer_revisions=layer_revisions,
                layer_digests=layer_digests,
                effective_digest=pending.effective_digest,
                secret_bindings=build_secret_binding_summary(
                    pending.payload,
                    resolve_environment=True,
                ),
                schema_version=1,
                promoted_generation=pending.target_generation,
                promoted_apply_id=claim.apply_id,
                expected_active_revision=active.active_revision,
                expected_source_generation=(
                    pending.source_generation
                    if pending.source_generation is not None
                    else source_generation
                ),
                expected_source_baseline=baseline,
                expected_layer_revisions=layer_revisions,
                expected_layer_digests=layer_digests,
                expected_pending_revision=pending.pending_revision,
                expected_pending_state="applying",
                expected_fencing_token=claim.fencing_token,
                event=ConfigEventInput(
                    event_id=f"config:{pending.candidate_id}:{resolve_event_suffix}",
                    config_domain=self._config_domain,
                    candidate_id=pending.candidate_id,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    idempotency_key=pending.idempotency_key,
                    commit_revision=None,
                    active_revision=None,
                    pending_revision=pending.pending_revision,
                    source=resolve_source,
                    result="applied",
                    activation_scope=workspace_config_policy().activation_scope_for(
                        changed_paths
                    ),
                    changed_paths=changed_paths,
                    applied_paths=changed_paths,
                ),
            )
        except Exception as error:
            self._store.update_config_apply_journal(
                apply_id=claim.apply_id,
                expected_state="applying",
                state="recovery_required",
                last_error=str(error),
            )
            self._store.update_pending_config_candidate_state(
                config_domain=self._config_domain,
                candidate_id=pending.candidate_id,
                expected_state="applying",
                state="recovery_required",
                last_error=str(error),
                event=ConfigEventInput(
                    event_id=f"config:{pending.candidate_id}:recovery_required",
                    config_domain=self._config_domain,
                    candidate_id=pending.candidate_id,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    idempotency_key=pending.idempotency_key,
                    commit_revision=None,
                    active_revision=active.active_revision,
                    pending_revision=pending.pending_revision,
                    source=resolve_source,
                    result="recovery_required",
                    activation_scope=workspace_config_policy().activation_scope_for(
                        changed_paths
                    ),
                    changed_paths=changed_paths,
                    deferred_paths=changed_paths,
                    error=str(error),
                ),
            )
            self._store.release_config_apply_claim(
                config_domain=self._config_domain,
                apply_id=claim.apply_id,
                fencing_token=claim.fencing_token,
            )
            raise
        self._store.release_config_apply_claim(
            config_domain=self._config_domain,
            apply_id=claim.apply_id,
            fencing_token=claim.fencing_token,
        )
        return self._reload_status_provider()

    def discard_pending_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
    ) -> ConfigReloadStatus:
        """仅在旧 active/source 基线可证明安全时丢弃 pending。"""

        if self._store is None:
            raise ConfigConflictError("当前 Workspace 没有 Workspace-owned 状态库")
        pending = self._store.load_pending_config_candidate(
            candidate_ref=candidate_ref,
            allow_recovery=True,
            allow_discarded=True,
        )
        active = self._store.get_active_config_snapshot(
            self._config_domain
        )
        if active is None:
            raise ConfigConflictError("Workspace pending discard 缺少 active snapshot")
        changed_paths = changed_json_paths(active.payload, pending.payload)
        self._store.discard_pending_config_candidate(
            candidate_ref=candidate_ref,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
            expected_source_baseline=pending.source_baseline,
            reason="用户显式丢弃 Workspace pending candidate",
            event=ConfigEventInput(
                event_id=f"config:{pending.candidate_id}:discarded",
                config_domain=self._config_domain,
                candidate_id=pending.candidate_id,
                attempt_id=pending.last_attempt_id,
                apply_id=pending.last_apply_id,
                idempotency_key=pending.idempotency_key,
                commit_revision=None,
                active_revision=active.active_revision,
                pending_revision=pending.pending_revision,
                source="workspace-config-api",
                result="discarded",
                activation_scope=workspace_config_policy().activation_scope_for(
                    changed_paths
                ),
                changed_paths=changed_paths,
            ),
        )
        return self._reload_status_provider()

    @staticmethod
    def build_pending_restart_health_proof(
        *,
        candidate_ref: str,
        pending: ConfigPendingCandidateRecord,
    ) -> dict[str, object]:
        if not pending.target_generation or not pending.fencing_token:
            raise ConfigConflictError(
                "Workspace pending candidate 缺少 target generation 或 fencing token"
            )
        secret_bindings = build_secret_binding_summary(
            pending.payload,
            resolve_environment=True,
        )
        secret_binding_digest = hashlib.sha256(
            dump_json(secret_bindings).encode("utf-8")
        ).hexdigest()
        del candidate_ref
        return {
            "config_domain": "workspace",
            "loaded_source": "pending",
            "candidate_id": pending.candidate_id,
            "loaded_commit_revision": pending.pending_revision,
            "effective_digest": pending.effective_digest,
            "candidate_digest": pending.candidate_digest,
            "secret_binding_digest": secret_binding_digest,
            "generation_id": pending.target_generation,
            "fencing_token_digest": hashlib.sha256(
                pending.fencing_token.encode("utf-8")
            ).hexdigest(),
        }
