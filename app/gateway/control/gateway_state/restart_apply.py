"""受控重启的 apply 启动、进度与失败记录垂直链路。

承载 ``begin_gateway_restart_apply``、``update_gateway_restart_intent``、
``record_gateway_restart_startup_failure`` 与 ``load_gateway_pending_candidate``。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigApplyClaimRecord,
    ConfigConflictError,
    ConfigEventInput,
    ConfigPendingCandidateRecord,
    GatewayRestartIntentRecord,
    dump_json,
)


class GatewayRestartApplyMixin:
    """restart apply 与进度记录方法族（唯一实现点）。"""

    def begin_gateway_restart_apply(
        self,
        *,
        candidate_ref: str,
        attempt_id: str,
        apply_id: str,
        owner: str,
        lease_seconds: float = 30,
    ) -> tuple[
        GatewayRestartIntentRecord,
        ConfigPendingCandidateRecord,
        ConfigApplyClaimRecord,
    ]:
        """在一个事务中把 pending intent、claim、candidate 和 journal 置为 applying。"""

        if lease_seconds <= 0:
            raise ValueError("Gateway restart apply lease 必须大于 0 秒")
        if not all((candidate_ref, attempt_id, apply_id, owner)):
            raise ValueError("Gateway restart apply 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                """
                SELECT candidate_id, base_active_revision, target_generation,
                       fencing_token, state
                FROM gateway_restart_intent
                WHERE candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if intent is None:
                raise ConfigConflictError(
                    f"Gateway restart intent 不存在: {candidate_ref}"
                )
            candidate_id = str(intent[0])
            intent_state = str(intent[4])
            pending = connection.execute(
                """
                SELECT pending_revision, state, source_baseline_json
                FROM config_pending_candidate
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if pending is None:
                raise ConfigConflictError(
                    "Gateway restart intent 绑定的 pending candidate 不存在"
                )
            now = datetime.now(UTC)
            if intent_state in {"pending", "recovery_required"}:
                expected_pending_state = (
                    "pending_restart"
                    if intent_state == "pending"
                    else "recovery_required"
                )
                if str(pending[1]) != expected_pending_state:
                    raise ConfigConflictError(
                        "Gateway restart intent 的 candidate 状态与 intent 不匹配: "
                        f"intent={intent_state}, candidate={pending[1]}"
                    )
                existing_claim = connection.execute(
                    """
                    SELECT candidate_id, apply_id, fencing_token, lease_expires_at
                    FROM config_apply_claim WHERE config_domain = 'gateway'
                    """
                ).fetchone()
                if existing_claim is not None:
                    if datetime.fromisoformat(str(existing_claim[3])) > now:
                        raise ConfigConflictError(
                            "Gateway restart apply 仍有未过期的 claim"
                        )
                    connection.execute(
                        "DELETE FROM config_apply_claim WHERE config_domain = 'gateway'"
                    )
                lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
                connection.execute(
                    """
                    INSERT INTO config_apply_claim(
                        config_domain, candidate_id, attempt_id, apply_id, owner,
                        base_active_revision, target_generation, lease_expires_at,
                        fencing_token, updated_at
                    ) VALUES ('gateway', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        attempt_id,
                        apply_id,
                        owner,
                        int(intent[1]) if intent[1] is not None else None,
                        str(intent[2]),
                        lease_expires_at,
                        str(intent[3]),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE config_pending_candidate
                    SET state = 'applying', last_attempt_id = ?, last_apply_id = ?,
                        fencing_token = ?
                    WHERE config_domain = 'gateway' AND candidate_id = ?
                      AND state = ?
                    """,
                    (
                        attempt_id,
                        apply_id,
                        str(intent[3]),
                        candidate_id,
                        expected_pending_state,
                    ),
                )
                intent_cursor = connection.execute(
                    """
                    UPDATE gateway_restart_intent
                    SET state = 'applying', updated_at = ?
                    WHERE candidate_ref = ? AND state = ?
                      AND fencing_token = ?
                    """,
                    (
                        now.isoformat(),
                        candidate_ref,
                        intent_state,
                        str(intent[3]),
                    ),
                )
                if intent_cursor.rowcount != 1:
                    raise ConfigConflictError(
                        "Gateway restart intent applying CAS 失败"
                    )
                active = connection.execute(
                    """
                    SELECT active_revision, source_baseline_json
                    FROM config_active_snapshot WHERE config_domain = 'gateway'
                    """
                ).fetchone()
                registry_revision = self._read_registry_revision(connection)
                connection.execute(
                    """
                    INSERT INTO config_apply_journal(
                        config_domain, apply_id, candidate_id, attempt_id, owner,
                        base_active_revision, pending_revision, source_baseline_json,
                        active_baseline_json, registry_revision, side_effects_json,
                        state, last_error, created_at, updated_at
                    ) VALUES ('gateway', ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]',
                              'applying', NULL, ?, ?)
                    """,
                    (
                        apply_id,
                        candidate_id,
                        attempt_id,
                        owner,
                        int(intent[1]) if intent[1] is not None else None,
                        int(pending[0]),
                        str(pending[2]),
                        str(active[1]) if active is not None else "{}",
                        registry_revision,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            elif intent_state == "applying":
                claim = connection.execute(
                    """
                    SELECT candidate_id, apply_id, fencing_token, lease_expires_at
                    FROM config_apply_claim WHERE config_domain = 'gateway'
                    """
                ).fetchone()
                if (
                    claim is None
                    or str(claim[0]) != candidate_id
                    or str(claim[2]) != str(intent[3])
                    or datetime.fromisoformat(str(claim[3])) <= now
                ):
                    raise ConfigConflictError(
                        "Gateway applying intent 缺少未过期的匹配 claim"
                    )
            else:
                raise ConfigConflictError(
                    f"Gateway restart intent 当前不可开始: state={intent_state}"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result_intent = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        result_pending = self.get_pending_config_candidate(
            config_domain="gateway", candidate_id=candidate_id
        )
        result_claim = self.get_config_apply_claim(config_domain="gateway")
        if result_intent is None or result_pending is None or result_claim is None:
            raise RuntimeError("Gateway restart apply 提交后记录不完整")
        return result_intent, result_pending, result_claim

    def update_gateway_restart_intent(
        self,
        *,
        candidate_ref: str,
        expected_state: str,
        state: str,
        fencing_token: str,
        health_proof: dict[str, object] | None = None,
        last_error: str | None = None,
    ) -> GatewayRestartIntentRecord:
        allowed = {
            "pending": {"applying", "discarded", "recovery_required"},
            "applying": {"active", "failed", "recovery_required"},
            "failed": {"applying", "discarded", "recovery_required"},
            "recovery_required": {"applying", "discarded", "recovery_required"},
            "active": set(),
            "discarded": set(),
        }
        if state not in allowed.get(expected_state, set()):
            raise ValueError(
                f"非法 Gateway restart intent 转换: {expected_state} -> {state}"
            )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_restart_intent
                SET state = ?, health_proof_json = ?, last_error = ?, updated_at = ?
                WHERE candidate_ref = ? AND state = ? AND fencing_token = ?
                """,
                (
                    state,
                    dump_json(health_proof) if health_proof is not None else None,
                    last_error,
                    utc_now_text(),
                    candidate_ref,
                    expected_state,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway restart intent fencing/CAS 校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        if result is None:
            raise RuntimeError("Gateway restart intent 更新后无法读取")
        return result

    def record_gateway_restart_startup_failure(
        self,
        *,
        candidate_ref: str,
        gateway_id: str,
        target_generation: str,
        fencing_token: str,
        error: str,
    ) -> None:
        """原子记录与当前启动契约匹配的 Gateway 早期失败。

        早期 loader 失败时还没有可用的 ``GatewayConfigReloadService``，因此
        这里必须自己完成 intent、candidate、apply journal 和 claim 的事务边界。
        """

        if not all((candidate_ref, gateway_id, target_generation, fencing_token, error)):
            raise ValueError("Gateway 启动失败记录缺少启动契约字段")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                """
                SELECT candidate_id, gateway_id, target_generation, fencing_token,
                       state, expires_at
                FROM gateway_restart_intent
                WHERE candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if intent is None:
                raise ConfigConflictError(
                    f"Gateway candidate_ref 不存在: {candidate_ref}"
                )
            if str(intent[1]) != gateway_id:
                raise ConfigConflictError("Gateway pending intent 不属于当前 Gateway")
            if str(intent[2]) != target_generation:
                raise ConfigConflictError("Gateway pending 启动 generation 不匹配")
            if str(intent[3]) != fencing_token:
                raise ConfigConflictError("Gateway pending 启动 fencing token 不匹配")
            expires_at = intent[5]
            if expires_at is None or datetime.fromisoformat(str(expires_at)) <= datetime.now(UTC):
                raise ConfigConflictError("Gateway pending intent 已过期，拒绝记录启动失败")
            intent_state = str(intent[4])
            if intent_state not in {"pending", "applying", "recovery_required"}:
                raise ConfigConflictError(
                    "Gateway pending intent 当前不可记录启动失败: "
                    f"state={intent_state}"
                )

            candidate_id = str(intent[0])
            pending = connection.execute(
                """
                SELECT pending_revision, state, target_generation, fencing_token,
                       last_attempt_id, last_apply_id, idempotency_key
                FROM config_pending_candidate
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if pending is None:
                raise ConfigConflictError(
                    "Gateway restart intent 绑定的 pending candidate 不存在"
                )
            if (
                str(pending[2]) != target_generation
                or str(pending[3]) != fencing_token
            ):
                raise ConfigConflictError(
                    "Gateway pending candidate 与启动契约 generation/fencing 不匹配"
                )
            pending_state = str(pending[1])
            expected_pending_state = {
                "pending": "pending_restart",
                "applying": "applying",
                "recovery_required": "recovery_required",
            }[intent_state]
            if pending_state != expected_pending_state:
                raise ConfigConflictError(
                    "Gateway restart intent 与 pending candidate 状态不匹配: "
                    f"intent={intent_state}, candidate={pending_state}"
                )
            if pending_state not in {
                "pending_restart",
                "applying",
                "recovery_required",
            }:
                raise ConfigConflictError(
                    "Gateway pending candidate 当前不可记录启动失败: "
                    f"state={pending_state}"
                )

            claim = connection.execute(
                """
                SELECT candidate_id, target_generation, fencing_token
                FROM config_apply_claim
                WHERE config_domain = 'gateway'
                """
            ).fetchone()
            if claim is not None and (
                str(claim[0]) != candidate_id
                or str(claim[1]) != target_generation
                or str(claim[2]) != fencing_token
            ):
                raise ConfigConflictError(
                    "Gateway 启动失败对应的 apply claim 已被其他 generation 取代"
                )
            if intent_state == "applying" and claim is None:
                raise ConfigConflictError(
                    "Gateway applying intent 缺少匹配的 apply claim"
                )

            now = utc_now_text()
            if intent_state in {"pending", "applying"}:
                intent_cursor = connection.execute(
                    """
                    UPDATE gateway_restart_intent
                    SET state = 'recovery_required', last_error = ?, updated_at = ?
                    WHERE candidate_ref = ? AND state = ?
                      AND gateway_id = ? AND target_generation = ?
                      AND fencing_token = ?
                    """,
                    (
                        error,
                        now,
                        candidate_ref,
                        intent_state,
                        gateway_id,
                        target_generation,
                        fencing_token,
                    ),
                )
                if intent_cursor.rowcount != 1:
                    raise ConfigConflictError(
                        "Gateway pending 启动失败 intent CAS 校验失败"
                    )

            if pending_state in {"pending_restart", "applying"}:
                connection.execute(
                    """
                    UPDATE config_pending_candidate
                    SET state = 'recovery_required', last_error = ?
                    WHERE config_domain = 'gateway' AND candidate_id = ?
                      AND state = ? AND target_generation = ?
                      AND fencing_token = ?
                    """,
                    (
                        error,
                        candidate_id,
                        pending_state,
                        target_generation,
                        fencing_token,
                    ),
                )
                active = connection.execute(
                    """
                    SELECT active_revision FROM config_active_snapshot
                    WHERE config_domain = 'gateway'
                    """
                ).fetchone()
                self._insert_config_event(
                    connection,
                    ConfigEventInput(
                        event_id=f"config:{candidate_id}:gateway_recovery_required",
                        config_domain="gateway",
                        candidate_id=candidate_id,
                        attempt_id=(
                            str(pending[4]) if pending[4] is not None else None
                        ),
                        apply_id=(
                            str(pending[5]) if pending[5] is not None else None
                        ),
                        idempotency_key=str(pending[6]),
                        commit_revision=None,
                        active_revision=(
                            int(active[0]) if active is not None else None
                        ),
                        pending_revision=int(pending[0]),
                        source="gateway-runtime-generation",
                        result="recovery_required",
                        activation_scope="restart_gateway",
                        error=error,
                    ),
                )

            apply_id = str(pending[5]) if pending[5] is not None else None
            if apply_id is not None:
                connection.execute(
                    """
                    UPDATE config_apply_journal
                    SET state = 'recovery_required', last_error = ?, updated_at = ?
                    WHERE config_domain = 'gateway' AND apply_id = ?
                      AND candidate_id = ? AND state = 'applying'
                    """,
                    (error, now, apply_id, candidate_id),
                )
            if claim is not None:
                connection.execute(
                    """
                    DELETE FROM config_apply_claim
                    WHERE config_domain = 'gateway' AND candidate_id = ?
                      AND target_generation = ? AND fencing_token = ?
                    """,
                    (candidate_id, target_generation, fencing_token),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_gateway_pending_candidate(
        self,
        *,
        candidate_ref: str,
        gateway_id: str | None = None,
    ):
        """只按持久 restart intent 加载 pending；不匹配时禁止回退到 active。"""
        intent = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        if intent is None or intent.state not in {"pending", "applying"}:
            raise ConfigConflictError(
                "Gateway candidate_ref 不匹配可加载的 pending intent"
            )
        if intent.expires_at is None or intent.expires_at <= datetime.now(UTC):
            raise ConfigConflictError(
                "Gateway pending intent 已过期，必须显式重试"
            )
        if gateway_id is not None:
            if intent.gateway_id is None:
                raise ConfigConflictError(
                    "Gateway pending intent 缺少 gateway_id 绑定，必须重新生成 intent"
                )
            if intent.gateway_id != gateway_id:
                raise ConfigConflictError(
                    "Gateway pending intent 不属于当前 Gateway"
                )
        pending = self.get_pending_config_candidate(
            config_domain="gateway",
            candidate_id=intent.candidate_id,
        )
        if pending is None or pending.state not in {"pending_restart", "applying"}:
            raise ConfigConflictError(
                "Gateway pending candidate 与 restart intent 不匹配"
            )
        return pending
