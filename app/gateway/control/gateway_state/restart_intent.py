"""gateway_restart_intent 垂直链路（受控重启意图状态机）。

承载 ``gateway_restart_intent`` 的行投影与 get/request/retry/discard 状态机。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    GatewayRestartIntentRecord,
    load_json_object,
    new_config_id,
    validate_state_transition,
)


class GatewayRestartIntentMixin:
    """gateway restart intent 方法族（唯一实现点）。"""

    @staticmethod
    def _gateway_restart_intent_from_row(row) -> GatewayRestartIntentRecord:
        return GatewayRestartIntentRecord(
            intent_id=str(row[0]),
            candidate_ref=str(row[1]),
            candidate_id=str(row[2]),
            gateway_id=str(row[3]) if row[3] is not None else None,
            base_active_revision=int(row[4]) if row[4] is not None else None,
            old_generation=str(row[5]) if row[5] is not None else None,
            target_generation=str(row[6]),
            fencing_token=str(row[7]),
            state=cast(str, row[8]),
            requested_by=str(row[9]),
            health_proof=(
                load_json_object(str(row[10]), field="Gateway health proof")
                if row[10] is not None
                else None
            ),
            last_error=str(row[11]) if row[11] is not None else None,
            requested_at=datetime.fromisoformat(str(row[12])),
            expires_at=(
                datetime.fromisoformat(str(row[13])) if row[13] is not None else None
            ),
            updated_at=datetime.fromisoformat(str(row[14])),
        )

    def get_gateway_restart_intent(
        self,
        *,
        candidate_ref: str | None = None,
        candidate_id: str | None = None,
    ) -> GatewayRestartIntentRecord | None:
        if (candidate_ref is None) == (candidate_id is None):
            raise ValueError(
                "Gateway restart intent 必须指定 candidate_ref 或 candidate_id"
            )
        field = "candidate_ref" if candidate_ref is not None else "candidate_id"
        value = candidate_ref if candidate_ref is not None else candidate_id
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT intent_id, candidate_ref, candidate_id, gateway_id,
                       base_active_revision, old_generation, target_generation,
                       fencing_token, state, requested_by, health_proof_json,
                       last_error, requested_at, expires_at, updated_at
                FROM gateway_restart_intent
                WHERE """
                + field
                + " = ? ORDER BY updated_at DESC LIMIT 1",
                (value,),
            ).fetchone()
        finally:
            connection.close()
        return self._gateway_restart_intent_from_row(row) if row is not None else None

    def request_gateway_restart(
        self,
        *,
        candidate_ref: str,
        candidate_id: str,
        base_active_revision: int | None,
        old_generation: str | None,
        target_generation: str,
        requested_by: str,
        fencing_token: str | None = None,
        gateway_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> GatewayRestartIntentRecord:
        if not all((candidate_ref, candidate_id, target_generation, requested_by)):
            raise ValueError("Gateway restart intent 的身份字段不能为空")
        if gateway_id is not None and not gateway_id:
            raise ValueError("Gateway restart intent 的 gateway_id 不能为空")
        now_datetime = datetime.now(UTC)
        resolved_expires_at = expires_at or (now_datetime + timedelta(seconds=120))
        if (
            resolved_expires_at.tzinfo is None
            or resolved_expires_at <= now_datetime
        ):
            raise ValueError("Gateway restart intent 的 expires_at 必须是未来时间")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT intent_id, candidate_ref, candidate_id, gateway_id,
                       base_active_revision, old_generation, target_generation,
                       fencing_token, state, requested_by, health_proof_json,
                       last_error, requested_at, expires_at, updated_at
                FROM gateway_restart_intent
                WHERE candidate_id = ? AND state IN ('pending', 'applying')
                ORDER BY requested_at DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._gateway_restart_intent_from_row(existing)
            pending = connection.execute(
                """
                SELECT state FROM config_pending_candidate
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if pending is None or str(pending[0]) != "pending_restart":
                raise ConfigConflictError(
                    "Gateway restart intent 只能绑定 pending_restart candidate"
                )
            now = utc_now_text()
            resolved_fencing_token = fencing_token or new_config_id("fence")
            connection.execute(
                """
                INSERT INTO gateway_restart_intent(
                    intent_id, candidate_ref, candidate_id, gateway_id,
                    base_active_revision, old_generation, target_generation,
                    fencing_token, state, requested_by, health_proof_json,
                    last_error, requested_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    new_config_id("restart"),
                    candidate_ref,
                    candidate_id,
                    gateway_id,
                    base_active_revision,
                    old_generation,
                    target_generation,
                    resolved_fencing_token,
                    requested_by,
                    now,
                    resolved_expires_at.isoformat(),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET target_generation = ?, fencing_token = ?
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (
                    target_generation,
                    resolved_fencing_token,
                    candidate_id,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        if result is None:
            raise RuntimeError("Gateway restart intent 提交后无法读取")
        return result

    def retry_gateway_restart(
        self,
        *,
        candidate_ref: str,
        target_generation: str,
        requested_by: str,
    ) -> GatewayRestartIntentRecord:
        """以新的 generation 和 fencing token 显式重试失败的 Gateway pending。"""

        if not all((candidate_ref, target_generation, requested_by)):
            raise ValueError("Gateway restart retry 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT intent_id, candidate_id, state, expires_at
                FROM gateway_restart_intent
                WHERE candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"Gateway restart intent 不存在: {candidate_ref}"
                )
            intent_state = str(row[2])
            intent_expired = row[3] is None or datetime.fromisoformat(
                str(row[3])
            ) <= datetime.now(UTC)
            if intent_state not in {"failed", "recovery_required"} and not (
                intent_state == "pending" and intent_expired
            ):
                raise ConfigConflictError(
                    "Gateway restart retry 只允许恢复 failed/recovery_required "
                    "intent，或已过期的 pending intent"
                )
            candidate_id = str(row[1])
            pending = connection.execute(
                """
                SELECT state FROM config_pending_candidate
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if pending is None or str(pending[0]) not in {
                "pending_restart",
                "recovery_required",
            }:
                raise ConfigConflictError(
                    "Gateway restart retry 缺少可重试的 pending candidate"
                )
            now = utc_now_text()
            expires_at = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
            claim = connection.execute(
                """
                SELECT apply_id, lease_expires_at
                FROM config_apply_claim WHERE config_domain = 'gateway'
                """
            ).fetchone()
            if claim is not None:
                if datetime.fromisoformat(str(claim[1])) > datetime.now(UTC):
                    raise ConfigConflictError(
                        "Gateway restart retry 仍有未过期的 apply claim"
                    )
                connection.execute(
                    "DELETE FROM config_apply_claim WHERE config_domain = 'gateway'"
                )
            fencing_token = new_config_id("fence")
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'pending_restart',
                    target_generation = ?, fencing_token = ?, last_error = NULL
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (target_generation, fencing_token, candidate_id),
            )
            cursor = connection.execute(
                """
                UPDATE gateway_restart_intent
                SET target_generation = ?, fencing_token = ?, state = 'pending',
                    requested_by = ?, health_proof_json = NULL, last_error = NULL,
                    expires_at = ?, updated_at = ?
                WHERE candidate_ref = ?
                  AND (
                      state IN ('failed', 'recovery_required')
                      OR (
                          state = 'pending'
                          AND (expires_at IS NULL OR expires_at <= ?)
                      )
                  )
                """,
                (
                    target_generation,
                    fencing_token,
                    requested_by,
                    expires_at,
                    now,
                    candidate_ref,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway restart retry intent CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        if result is None:
            raise RuntimeError("Gateway restart retry 提交后无法读取")
        return result

    def discard_gateway_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
        expected_source_baseline: dict[str, object],
        event: ConfigEventInput | None = None,
        reason: str = "用户显式丢弃 Gateway pending candidate",
    ) -> GatewayRestartIntentRecord:
        """在旧 Gateway active 和外部副作用均可证明安全时丢弃 pending。"""

        if not candidate_ref or not expected_active_digest or not reason:
            raise ValueError("Gateway pending discard 的参数不能为空")
        if expected_active_revision < 0:
            raise ValueError("Gateway pending discard 的 active revision 不能为负数")
        validate_state_transition("pending_restart", "discarded")
        validate_state_transition("recovery_required", "discarded")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                """
                SELECT intent_id, candidate_ref, candidate_id, gateway_id,
                       base_active_revision, old_generation, target_generation,
                       fencing_token, state, requested_by, health_proof_json,
                       last_error, requested_at, expires_at, updated_at
                FROM gateway_restart_intent
                WHERE candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if intent is None:
                raise ConfigConflictError(
                    f"Gateway restart intent 不存在: {candidate_ref}"
                )
            intent_state = str(intent[8])
            if intent_state == "discarded":
                connection.execute("COMMIT")
                result = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
                if result is None:
                    raise RuntimeError("Gateway discarded intent 读取后消失")
                return result
            if intent_state not in {"pending", "failed", "recovery_required"}:
                raise ConfigConflictError(
                    "Gateway pending discard 只允许 pending/failed/recovery_required intent: "
                    f"state={intent_state}"
                )
            candidate_id = str(intent[2])
            pending = connection.execute(
                """
                SELECT pending_revision, state, last_apply_id, source_baseline_json
                FROM config_pending_candidate
                WHERE config_domain = 'gateway' AND candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if pending is None or str(pending[1]) not in {
                "pending_restart",
                "recovery_required",
            }:
                raise ConfigConflictError(
                    "Gateway restart discard 缺少可丢弃的 pending candidate"
                )
            pending_revision = int(pending[0])
            current_state = str(pending[1])

            active = connection.execute(
                """
                SELECT active_revision, effective_digest, state
                FROM config_active_snapshot
                WHERE config_domain = 'gateway'
                """
            ).fetchone()
            if (
                active is None
                or str(active[2]) != "active"
                or int(active[0]) != expected_active_revision
                or str(active[1]) != expected_active_digest
            ):
                raise ConfigConflictError(
                    "Gateway pending discard 缺少匹配的安全 active 基线"
                )

            claim = connection.execute(
                """
                SELECT apply_id
                FROM config_apply_claim
                WHERE config_domain = 'gateway'
                """
            ).fetchone()
            if claim is not None:
                raise ConfigConflictError(
                    "Gateway pending discard 仍有 apply claim，必须先完成恢复或补偿: "
                    f"apply_id={claim[0]}"
                )

            stored_baseline = load_json_object(
                str(pending[3]), field="Gateway pending source baseline"
            )
            if stored_baseline != expected_source_baseline:
                raise ConfigConflictError(
                    "Gateway pending discard 的 source baseline 与候选不一致"
                )
            expected_sources = {
                str(key): detail
                for key, detail in expected_source_baseline.items()
                if isinstance(detail, dict) and detail.get("layer_revision") is not None
            }
            current_rows = connection.execute(
                """
                SELECT config_key, vrn, presence, layer_revision,
                       layer_digest, source_generation
                FROM config_source_layers
                """
            ).fetchall()
            if {str(row[0]) for row in current_rows} != set(expected_sources):
                raise ConfigConflictError(
                    "Gateway pending discard 的 source layer 集合已变化"
                )
            for row in current_rows:
                detail = expected_sources[str(row[0])]
                actual_vrn = str(row[1]) if row[1] is not None else None
                expected_vrn = (
                    str(detail.get("vrn")) if detail.get("vrn") is not None else None
                )
                if (
                    expected_vrn != actual_vrn
                    or str(detail.get("presence")) != str(row[2])
                    or int(detail["layer_revision"]) != int(row[3])
                    or (
                        str(detail.get("layer_digest"))
                        if detail.get("layer_digest") is not None
                        else None
                    )
                    != (str(row[4]) if row[4] is not None else None)
                    or int(detail.get("source_generation", 0)) != int(row[5])
                ):
                    raise ConfigConflictError(
                        "Gateway pending discard 的 source layer 基线已变化: "
                        f"key={row[0]}"
                    )

            apply_id = str(pending[2]) if pending[2] is not None else None
            if apply_id is not None:
                journal = connection.execute(
                    """
                    SELECT state, side_effects_json
                    FROM config_apply_journal
                    WHERE apply_id = ?
                    """,
                    (apply_id,),
                ).fetchone()
                if journal is not None:
                    side_effects = json.loads(str(journal[1]))
                    if not isinstance(side_effects, list) or not all(
                        isinstance(item, dict) for item in side_effects
                    ):
                        raise TypeError("Gateway apply journal 副作用结构无效")
                    if side_effects and str(journal[0]) != "compensated":
                        raise ConfigConflictError(
                            "Gateway pending discard 缺少外部副作用补偿证明"
                        )
                    if str(journal[0]) in {
                        "applying",
                        "failed",
                        "recovery_required",
                    }:
                        connection.execute(
                            """
                            UPDATE config_apply_journal
                            SET state = 'compensated', last_error = ?, updated_at = ?
                            WHERE apply_id = ? AND state = ?
                            """,
                            (
                                reason,
                                utc_now_text(),
                                apply_id,
                                str(journal[0]),
                            ),
                        )

            validate_state_transition(current_state, "discarded")
            candidate_cursor = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'discarded', last_error = ?
                WHERE config_domain = 'gateway' AND candidate_id = ?
                  AND state = ? AND pending_revision = ?
                """,
                (reason, candidate_id, current_state, pending_revision),
            )
            if candidate_cursor.rowcount != 1:
                raise ConfigConflictError("Gateway pending discard 状态 CAS 失败")
            intent_cursor = connection.execute(
                """
                UPDATE gateway_restart_intent
                SET state = 'discarded', health_proof_json = NULL,
                    last_error = ?, updated_at = ?
                WHERE candidate_ref = ? AND candidate_id = ? AND state = ?
                """,
                (
                    reason,
                    utc_now_text(),
                    candidate_ref,
                    candidate_id,
                    intent_state,
                ),
            )
            if intent_cursor.rowcount != 1:
                raise ConfigConflictError("Gateway restart intent discard CAS 失败")
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else pending_revision
                        ),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_restart_intent(candidate_ref=candidate_ref)
        if result is None:
            raise RuntimeError("Gateway pending discard 提交后无法读取 intent")
        return result
