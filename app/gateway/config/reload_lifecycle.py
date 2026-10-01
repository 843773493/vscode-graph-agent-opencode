from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config import ConfigFileWatcher, ConfigReloadStatus
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    build_secret_binding_summary,
    changed_json_paths,
    new_config_id,
    prepare_config_for_persistence,
)

from .loader import load_gateway_config
from .values import (
    GatewayConfig,
    GatewayConfigRuntimeApplier,
    GatewayConfigRuntimeRollback,
)


class ReloadLifecycleMixin:
    _CONFIG_DOMAIN = "gateway"
    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        config: GatewayConfig,
        config_path: Path,
        local_config_path: Path,
        schema_path: Path | None = None,
        on_runtime_config: GatewayConfigRuntimeApplier | None = None,
        gateway_id: str | None = None,
    ) -> None:
        self._state_store = state_store
        self._config = config
        self._config_path = config_path.expanduser().resolve()
        self._local_config_path = local_config_path.expanduser().resolve()
        self._schema_path = schema_path.expanduser().resolve() if schema_path else None
        self._on_runtime_config = on_runtime_config
        self._gateway_id = gateway_id
        self._watcher: ConfigFileWatcher | None = None
        now = datetime.now(timezone.utc)
        self._state_store.recover_expired_config_applies(
            config_domain=self._CONFIG_DOMAIN
        )
        active = state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        if active is not None and active.state != "active":
            raise ConfigConflictError(
                "Gateway active snapshot 当前需要恢复，禁止继续启动: "
                f"state={active.state}, error={active.last_error}"
            )

        pending = state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN
        )
        restart_intent = (
            state_store.get_gateway_restart_intent(candidate_id=pending.candidate_id)
            if pending is not None
            else None
        )
        if restart_intent is not None and restart_intent.state in {
            "active",
            "discarded",
        }:
            restart_intent = None
        visible_pending = (
            pending if pending is not None and pending.state != "active" else None
        )
        self._status = ConfigReloadStatus(
            healthy=True,
            revision=config.revision,
            last_success_at=now,
            last_attempt_at=now,
            last_error=None,
            state=(
                visible_pending.state
                if visible_pending is not None
                else "active"
            ),
            active_revision=active.active_revision if active is not None else None,
            pending_revision=(
                visible_pending.pending_revision
                if visible_pending is not None
                else None
            ),
            candidate_id=(
                visible_pending.candidate_id if visible_pending is not None else None
            ),
            candidate_ref=(
                restart_intent.candidate_ref if restart_intent is not None else None
            ),
            attempt_id=(
                visible_pending.last_attempt_id
                if visible_pending is not None
                else None
            ),
            apply_id=(
                visible_pending.last_apply_id if visible_pending is not None else None
            ),
        )
    def _assert_gateway_restart_intent_owner(self, intent) -> None:
        if self._gateway_id is None:
            return
        if intent.gateway_id is None:
            raise ConfigConflictError(
                "Gateway pending intent 缺少 gateway_id 绑定，必须重新生成 intent"
            )
        if intent.gateway_id != self._gateway_id:
            raise ConfigConflictError("Gateway pending intent 不属于当前 Gateway")
    @property
    def config(self) -> GatewayConfig:
        return self._config
    def status(self) -> ConfigReloadStatus:
        active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
        pending = self._state_store.get_pending_config_candidate(
            config_domain=self._CONFIG_DOMAIN
        )
        visible_pending = (
            pending if pending is not None and pending.state != "active" else None
        )
        if active is None and pending is None:
            return self._status
        state = visible_pending.state if visible_pending is not None else "active"
        restart_intent = (
            self._state_store.get_gateway_restart_intent(
                candidate_id=visible_pending.candidate_id
            )
            if visible_pending is not None
            else None
        )
        if restart_intent is not None and restart_intent.state in {
            "active",
            "discarded",
        }:
            restart_intent = None
        reason = self._status.reason
        if state == "discarded":
            reason = None
        elif state == "pending_restart":
            reason = "restart_required"
        elif state in {"conflict", "rejected", "recovery_required"}:
            reason = state
        return ConfigReloadStatus(
            healthy=(self._status.healthy or state == "discarded")
            and state
            not in {
                "conflict",
                "rejected",
                "recovery_required",
            },
            revision=(
                active.effective_digest if active is not None else self._status.revision
            ),
            last_success_at=self._status.last_success_at,
            last_attempt_at=self._status.last_attempt_at,
            last_error=(
                visible_pending.last_error
                if visible_pending is not None and visible_pending.last_error is not None
                else self._status.last_error
            ),
            restart_required=state == "pending_restart",
            reason=reason,
            changed_sections=self._status.changed_sections,
            state=state,
            active_revision=active.active_revision if active is not None else None,
            pending_revision=(
                visible_pending.pending_revision
                if visible_pending is not None
                else None
            ),
            candidate_id=(
                visible_pending.candidate_id if visible_pending is not None else None
            ),
            candidate_ref=(
                restart_intent.candidate_ref if restart_intent is not None else None
            ),
            attempt_id=(
                visible_pending.last_attempt_id
                if visible_pending is not None
                else None
            ),
            apply_id=(
                visible_pending.last_apply_id if visible_pending is not None else None
            ),
            layer_digests=active.layer_digests if active is not None else None,
            applied_paths=self._status.applied_paths,
            deferred_paths=self._status.deferred_paths,
        )
    def list_events(self, *, after: int = 0, limit: int = 100):
        return self._state_store.list_config_events(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
            limit=limit,
        )
    def claim_events_for_consumer(
        self,
        *,
        after: int,
        consumer_id: str,
        limit: int = 100,
    ):
        return self._state_store.claim_config_events_for_consumer(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
            consumer_id=consumer_id,
            limit=limit,
        )
    def mark_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ):
        return self._state_store.mark_config_event_delivered_for_consumer(
            event_id=event_id,
            consumer_id=consumer_id,
        )
    def ensure_event_cursor(self, *, after: int) -> None:
        self._state_store.ensure_config_event_cursor(
            config_domain=self._CONFIG_DOMAIN,
            after=after,
        )
    def initialize_active_snapshot(self) -> None:
        baseline, source_generation, revisions, digests = self._source_baseline(
            self._config
        )
        payload = prepare_config_for_persistence(self._config.payload)
        if not isinstance(payload, dict):
            raise TypeError("Gateway 脱敏 active payload 必须是对象")
        self._state_store.ensure_active_config_snapshot(
            config_domain=self._CONFIG_DOMAIN,
            payload=payload,
            source_baseline=baseline,
            source_generation=source_generation,
            layer_revisions=revisions,
            layer_digests=digests,
            effective_digest=self._config.revision,
            schema_version=2,
            promoted_generation="gateway-bootstrap",
            secret_bindings=build_secret_binding_summary(self._config.payload),
        )
    async def _renew_apply_claim(self, apply_id: str, fencing_token: str) -> None:
        """在 Gateway 外部 apply 期间续租 claim，丢失时由 promotion CAS 拒绝。"""

        while True:
            await asyncio.sleep(10)
            await asyncio.to_thread(
                self._state_store.renew_config_apply_claim,
                config_domain=self._CONFIG_DOMAIN,
                apply_id=apply_id,
                fencing_token=fencing_token,
                lease_seconds=30,
            )
    async def start(self) -> None:
        if self._watcher is not None:
            raise RuntimeError("Gateway 配置监听器不允许重复启动")
        self.initialize_active_snapshot()
        watcher = ConfigFileWatcher(
            directories={self._config_path.parent, self._local_config_path.parent},
            candidate_paths={self._config_path, self._local_config_path},
            on_change=self.reload,
        )
        await watcher.start()
        self._watcher = watcher
    async def stop(self) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            await watcher.stop()
    async def reload(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            candidate = load_gateway_config(
                config_path=self._config_path,
                local_config_path=self._local_config_path,
                schema_path=self._schema_path,
                state_store=self._state_store,
            )
            if candidate.revision == self._config.revision:
                self._status = replace(
                    self._status,
                    healthy=True,
                    last_attempt_at=now,
                    state="active",
                    reason=None,
                    last_error=None,
                )
                return
            changed_paths = changed_json_paths(
                self._config.payload,
                candidate.payload,
                array_identity_keys=gateway_config_policy().array_identity_keys(),
            )
            baseline, source_generation, revisions, digests = self._source_baseline(
                candidate
            )
            payload = prepare_config_for_persistence(candidate.payload)
            if not isinstance(payload, dict):
                raise TypeError("Gateway 脱敏 candidate payload 必须是对象")
            candidate_id = f"candidate_{source_generation}_{candidate.revision}"
            active = self._state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
            pending = self._state_store.create_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=candidate_id,
                idempotency_key=f"reload:{source_generation}:{candidate.revision}",
                payload=payload,
                source_baseline=baseline,
                candidate_digest=candidate.revision,
                effective_digest=candidate.revision,
                target_generation="gateway-runtime",
                fencing_token=None,
                state="candidate_validated",
                base_active_revision=(
                    active.active_revision if active is not None else None
                ),
                source_generation=source_generation,
            )
            if pending.state in {"rejected", "conflict", "recovery_required"}:
                pending = self._state_store.update_pending_config_candidate_state(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    expected_state=pending.state,
                    state="candidate_validated",
                    last_error=None,
                )
            attempt_id = new_config_id("attempt")
            apply_id = new_config_id("apply")
            registry_revision = self._state_store.get_registry_revision()
            claim = self._state_store.begin_config_apply(
                config_domain=self._CONFIG_DOMAIN,
                candidate_id=pending.candidate_id,
                attempt_id=attempt_id,
                apply_id=apply_id,
                owner="gateway-config-service",
                base_active_revision=active.active_revision
                if active is not None
                else None,
                target_generation="gateway-runtime",
                pending_revision=pending.pending_revision,
                source_baseline=pending.source_baseline,
                active_baseline=(active.source_baseline if active is not None else {}),
                registry_revision=registry_revision,
            )
            claim_renewal_task = asyncio.create_task(
                self._renew_apply_claim(claim.apply_id, claim.fencing_token)
            )
            def assert_apply_claim() -> None:
                self._state_store.assert_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )

            restart_paths = gateway_config_policy().restart_paths(changed_paths)
            if restart_paths:
                self._state_store.update_pending_config_candidate_state(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=pending.candidate_id,
                    expected_state="applying",
                    state="pending_restart",
                    last_error="Gateway 配置包含需要受控重启的运行时依赖",
                    event=ConfigEventInput(
                        event_id=f"config:{candidate_id}:restart_required",
                        config_domain=self._CONFIG_DOMAIN,
                        candidate_id=candidate_id,
                        attempt_id=attempt_id,
                        apply_id=apply_id,
                        idempotency_key=pending.idempotency_key,
                        commit_revision=None,
                        active_revision=None,
                        pending_revision=pending.pending_revision,
                        source="gateway-config-watcher",
                        result="restart_required",
                        activation_scope="restart_gateway",
                        changed_paths=changed_paths,
                        deferred_paths=changed_paths,
                        error="Gateway 配置包含需要受控重启的运行时依赖",
                    ),
                )
                active_runtime_generation = (
                    self._state_store.active_gateway_runtime_generation()
                )
                intent = self._state_store.request_gateway_restart(
                    candidate_ref=new_config_id("candidate_ref"),
                    candidate_id=candidate_id,
                    base_active_revision=(
                        active.active_revision if active is not None else None
                    ),
                    old_generation=(
                        (
                            active_runtime_generation.generation_id
                            if active_runtime_generation is not None
                            else active.promoted_generation
                        )
                        if active is not None
                        else None
                    ),
                    target_generation=new_config_id("gateway_generation"),
                    requested_by="gateway-config-watcher",
                    fencing_token=claim.fencing_token,
                    gateway_id=self._gateway_id,
                )
                self._status = replace(
                    self._status,
                    healthy=True,
                    revision=self._config.revision,
                    last_attempt_at=now,
                    last_error="Gateway 配置包含需要受控重启的运行时依赖",
                    restart_required=True,
                    reason="restart_required",
                    changed_sections=("gateway_runtime",),
                    state="pending_restart",
                    pending_revision=pending.pending_revision,
                    candidate_id=candidate_id,
                    candidate_ref=intent.candidate_ref,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    deferred_paths=changed_paths,
                )
                self._state_store.update_config_apply_journal(
                    apply_id=claim.apply_id,
                    expected_state="applying",
                    state="failed",
                    last_error="Gateway 配置等待受控重启",
                )
                claim_renewal_task.cancel()
                await asyncio.gather(claim_renewal_task, return_exceptions=True)
                self._state_store.release_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )
                return
            runtime_apply_started = False
            runtime_rollback: GatewayConfigRuntimeRollback | None = None
            try:
                if self._on_runtime_config is not None:
                    runtime_apply_started = True
                    runtime_rollback = await self._on_runtime_config(
                        candidate,
                        self._config,
                        claim.fencing_token,
                        assert_apply_claim,
                    )
                    self._state_store.append_config_apply_side_effect(
                        apply_id=claim.apply_id,
                        side_effect={
                            "resource": "gateway-runtime-config",
                            "action": "apply",
                            "candidate_id": candidate_id,
                            "changed_paths": list(changed_paths),
                        },
                    )
                active = self._state_store.get_active_config_snapshot(
                    self._CONFIG_DOMAIN
                )
                if active is None:
                    raise RuntimeError("Gateway active snapshot 在 promotion 前丢失")
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
                self._state_store.promote_active_config_snapshot(
                    config_domain=self._CONFIG_DOMAIN,
                    candidate_id=candidate_id,
                    payload=payload,
                    source_baseline=baseline,
                    source_generation=source_generation,
                    layer_revisions=revisions,
                    layer_digests=digests,
                    effective_digest=candidate.revision,
                    schema_version=2,
                    expected_active_revision=active.active_revision,
                    expected_pending_revision=pending.pending_revision,
                    expected_source_baseline=pending.source_baseline,
                    expected_source_generation=(
                        pending.source_generation
                        if pending.source_generation is not None
                        else source_generation
                    ),
                    expected_layer_revisions=pending_layer_revisions,
                    expected_layer_digests=pending_layer_digests,
                    expected_registry_revision=registry_revision,
                    expected_fencing_token=claim.fencing_token,
                    promoted_apply_id=claim.apply_id,
                    secret_bindings=build_secret_binding_summary(candidate.payload),
                    event=ConfigEventInput(
                        event_id=f"config:{candidate_id}:active",
                        config_domain=self._CONFIG_DOMAIN,
                        candidate_id=candidate_id,
                        attempt_id=attempt_id,
                        apply_id=apply_id,
                        idempotency_key=pending.idempotency_key,
                        commit_revision=None,
                        active_revision=None,
                        pending_revision=pending.pending_revision,
                        source="gateway-config-watcher",
                        result="applied",
                        activation_scope=gateway_config_policy().activation_scope_for(
                            changed_paths
                        ),
                        changed_paths=changed_paths,
                        applied_paths=changed_paths,
                    ),
                )
            except ConfigConflictError as error:
                (
                    recovery_required,
                    recovery_error,
                ) = await self._handle_runtime_apply_failure(
                    apply_id=claim.apply_id,
                    runtime_apply_started=runtime_apply_started,
                    runtime_rollback=runtime_rollback,
                    error=error,
                )
                error_detail = recovery_error or str(error)
                journal = self._state_store.get_config_apply_journal(
                    apply_id=claim.apply_id
                )
                recovery_required = recovery_required or bool(
                    journal and journal.side_effects and journal.state != "compensated"
                )
                if journal is not None and journal.state == "applying":
                    self._state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=error_detail,
                    )
                self._finish_gateway_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "conflict"),
                    result=("recovery_required" if recovery_required else "conflict"),
                    error=error_detail,
                    changed_paths=changed_paths,
                )
                raise
            except Exception as error:
                (
                    recovery_required,
                    recovery_error,
                ) = await self._handle_runtime_apply_failure(
                    apply_id=claim.apply_id,
                    runtime_apply_started=runtime_apply_started,
                    runtime_rollback=runtime_rollback,
                    error=error,
                )
                error_detail = recovery_error or str(error)
                journal = self._state_store.get_config_apply_journal(
                    apply_id=claim.apply_id
                )
                recovery_required = recovery_required or bool(
                    journal and journal.side_effects and journal.state != "compensated"
                )
                if journal is not None and journal.state == "applying":
                    self._state_store.update_config_apply_journal(
                        apply_id=claim.apply_id,
                        expected_state="applying",
                        state=("recovery_required" if recovery_required else "failed"),
                        last_error=error_detail,
                    )
                self._finish_gateway_candidate(
                    pending,
                    attempt_id=attempt_id,
                    apply_id=apply_id,
                    state=("recovery_required" if recovery_required else "rejected"),
                    result=(
                        "recovery_required" if recovery_required else "apply_failed"
                    ),
                    error=error_detail,
                    changed_paths=changed_paths,
                )
                raise
            finally:
                claim_renewal_task.cancel()
                await asyncio.gather(claim_renewal_task, return_exceptions=True)
                self._state_store.release_config_apply_claim(
                    config_domain=self._CONFIG_DOMAIN,
                    apply_id=claim.apply_id,
                    fencing_token=claim.fencing_token,
                )
            self._config = candidate
            self._status = replace(
                self._status,
                healthy=True,
                revision=candidate.revision,
                last_success_at=now,
                last_attempt_at=now,
                last_error=None,
                restart_required=False,
                reason=None,
                changed_sections=tuple(
                    sorted(
                        {
                            path.strip("/").split("/")[0]
                            for path in changed_paths
                            if path != "/"
                        }
                    )
                ),
                state="active",
                candidate_id=None,
                candidate_ref=None,
                attempt_id=None,
                apply_id=None,
                applied_paths=changed_paths,
                deferred_paths=(),
            )
        except Exception as error:
            self._status = replace(
                self._status,
                healthy=False,
                last_attempt_at=now,
                last_error=f"{type(error).__name__}: {error}",
                reason=self._status.reason or "invalid_config",
            )
            raise
    async def _handle_runtime_apply_failure(
        self,
        *,
        apply_id: str,
        runtime_apply_started: bool,
        runtime_rollback: GatewayConfigRuntimeRollback | None,
        error: Exception,
    ) -> tuple[bool, str | None]:
        """在 active promotion 失败时补偿已经发生的 Gateway 运行时变更。"""

        if not runtime_apply_started:
            return False, None
        if runtime_rollback is None:
            self._state_store.update_config_apply_journal(
                apply_id=apply_id,
                expected_state="applying",
                state="recovery_required",
                last_error=(f"{error}; Gateway 运行时 apply 未提供可执行的回退句柄"),
            )
            return (
                True,
                f"{error}; Gateway 运行时 apply 未提供可执行的回退句柄",
            )

        self._state_store.update_config_apply_journal(
            apply_id=apply_id,
            expected_state="applying",
            state="recovery_required",
            last_error=str(error),
        )
        try:
            await runtime_rollback()
        except Exception as rollback_error:  # noqa: BLE001 - 回退必须捕获所有运行时故障
            self._state_store.record_config_apply_compensation(
                apply_id=apply_id,
                compensation={
                    "resource": "gateway-runtime-config",
                    "action": "rollback",
                    "status": "failed",
                    "error": str(rollback_error),
                },
            )
            return (
                True,
                (f"{error}; Gateway 运行时 apply 回退失败: {rollback_error}"),
            )
        self._state_store.record_config_apply_compensation(
            apply_id=apply_id,
            compensation={
                "resource": "gateway-runtime-config",
                "action": "rollback",
                "status": "succeeded",
            },
        )
        return False, None
    def _source_baseline(
        self,
        config: GatewayConfig,
    ) -> tuple[dict[str, object], int, dict[str, int], dict[str, str | None]]:
        baseline: dict[str, object] = {}
        revisions: dict[str, int] = {}
        digests: dict[str, str | None] = {}
        source_generation = 0
        for source in config.source_details:
            key = source.source_key or f"{source.layer}:{source.precedence}"
            baseline[key] = {
                "vrn": source.vrn,
                "presence": source.presence,
                "layer_revision": source.layer_revision,
                "layer_digest": source.layer_digest,
                "source_generation": source.source_generation,
            }
            if source.layer_revision is not None:
                revisions[key] = source.layer_revision
            digests[key] = source.layer_digest
            if source.source_generation is not None:
                source_generation = max(source_generation, source.source_generation)
        return baseline, source_generation, revisions, digests
