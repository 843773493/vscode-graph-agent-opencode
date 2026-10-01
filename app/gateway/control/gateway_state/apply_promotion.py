"""受控启动的 active snapshot 提升垂直链路。

承载 ``promote_active_config_snapshot``：在同一事务内做 candidate/pending 与
registry revision 的 CAS、写入新的 active snapshot 与补偿 / 事件，是受控
启动把 candidate 变为 active 的唯一提交点。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

from dataclasses import replace

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    dump_json,
    validate_state_transition,
)


class ConfigApplyPromotionMixin:
    """active snapshot 提升方法族（唯一实现点）。"""

    def promote_active_config_snapshot(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        payload: dict[str, object],
        source_baseline: dict[str, object],
        source_generation: int,
        layer_revisions: dict[str, int],
        layer_digests: dict[str, str | None],
        effective_digest: str,
        schema_version: int,
        expected_active_revision: int | None,
        expected_pending_revision: int,
        expected_pending_state: ConfigLifecycleState = "applying",
        expected_source_baseline: dict[str, object] | None = None,
        expected_source_generation: int | None = None,
        expected_layer_revisions: dict[str, int] | None = None,
        expected_layer_digests: dict[str, str | None] | None = None,
        expected_registry_revision: int | None = None,
        expected_fencing_token: str | None = None,
        promoted_apply_id: str | None = None,
        secret_bindings: dict[str, object] | None = None,
        event: ConfigEventInput | None = None,
        promoted_generation: str = "gateway-runtime",
        gateway_candidate_ref: str | None = None,
        gateway_health_proof: dict[str, object] | None = None,
        gateway_runtime_generation_id: str | None = None,
        gateway_old_generation_id: str | None = None,
    ):
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if gateway_candidate_ref is not None:
                if (
                    gateway_health_proof is None
                    or gateway_runtime_generation_id is None
                ):
                    raise ValueError("Gateway promotion 缺少 runtime generation proof")
                generation_row = connection.execute(
                    """
                    SELECT state, listener_state, fencing_token, health_proof_json
                    FROM gateway_runtime_generation
                    WHERE config_domain = 'gateway' AND generation_id = ?
                    """,
                    (gateway_runtime_generation_id,),
                ).fetchone()
                if (
                    generation_row is None
                    or str(generation_row[0]) != "healthy"
                    or str(generation_row[1]) != "reserved"
                    or expected_fencing_token is None
                    or str(generation_row[2]) != expected_fencing_token
                    or str(generation_row[3]) != dump_json(gateway_health_proof)
                ):
                    raise ConfigConflictError(
                        "Gateway promotion runtime generation proof/fencing 校验失败"
                    )
                if gateway_old_generation_id == gateway_runtime_generation_id:
                    raise ConfigConflictError(
                        "Gateway promotion 的新旧 generation 不能相同"
                    )
                if gateway_old_generation_id is not None:
                    old_generation_row = connection.execute(
                        """
                        SELECT state, listener_state
                        FROM gateway_runtime_generation
                        WHERE config_domain = 'gateway' AND generation_id = ?
                        """,
                        (gateway_old_generation_id,),
                    ).fetchone()
                    if old_generation_row is None or (
                        str(old_generation_row[0]) != "active"
                        or str(old_generation_row[1]) not in {"serving", "draining"}
                    ):
                        raise ConfigConflictError(
                            "Gateway promotion 的旧 generation 不是 active/serving 或 active/draining"
                        )
                else:
                    active_generation = connection.execute(
                        """
                        SELECT generation_id
                        FROM gateway_runtime_generation
                        WHERE config_domain = 'gateway'
                          AND state = 'active' AND listener_state = 'serving'
                          AND generation_id != ?
                        LIMIT 1
                        """,
                        (gateway_runtime_generation_id,),
                    ).fetchone()
                    if active_generation is not None:
                        raise ConfigConflictError(
                            "Gateway promotion 缺少当前 serving generation"
                        )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            actual_active = int(active[0]) if active is not None else None
            if actual_active != expected_active_revision:
                raise ConfigConflictError("Gateway active revision CAS 冲突")
            if expected_registry_revision is not None:
                actual_registry_revision = self._read_registry_revision(connection)
                if actual_registry_revision != expected_registry_revision:
                    raise ConfigConflictError(
                        "Gateway active promotion registry revision CAS 冲突: "
                        f"current={actual_registry_revision}, "
                        f"expected={expected_registry_revision}"
                    )
            pending = connection.execute(
                """
                SELECT pending_revision, state FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (config_domain, candidate_id),
            ).fetchone()
            if (
                pending is None
                or int(pending[0]) != expected_pending_revision
                or str(pending[1]) != expected_pending_state
            ):
                raise ConfigConflictError("Gateway pending promotion CAS 冲突")
            if expected_fencing_token is not None:
                claim = connection.execute(
                    """
                    SELECT candidate_id, fencing_token
                    FROM config_apply_claim
                    WHERE config_domain = ?
                    """,
                    (config_domain,),
                ).fetchone()
                if (
                    claim is None
                    or str(claim[0]) != candidate_id
                    or str(claim[1]) != expected_fencing_token
                ):
                    raise ConfigConflictError(
                        "Gateway active promotion fencing 校验失败"
                    )
            source_keys = tuple(expected_layer_revisions or layer_revisions)
            if source_keys:
                placeholders = ",".join("?" for _ in source_keys)
                source_rows = connection.execute(
                    "SELECT config_key, source_generation, layer_revision, layer_digest "
                    "FROM config_source_layers WHERE config_key IN ("
                    + placeholders
                    + ")",
                    source_keys,
                ).fetchall()
                source_by_key = {str(row[0]): row for row in source_rows}
                current_generation = max(
                    (int(row[1]) for row in source_rows),
                    default=0,
                )
                expected_generation = (
                    expected_source_generation
                    if expected_source_generation is not None
                    else source_generation
                )
                if current_generation != expected_generation:
                    raise ConfigConflictError("Gateway source generation CAS 冲突")
                for source_key, expected_revision in (
                    expected_layer_revisions or layer_revisions
                ).items():
                    row = source_by_key.get(source_key)
                    if row is None or int(row[2]) != expected_revision:
                        raise ConfigConflictError(
                            f"Gateway source layer revision CAS 冲突: {source_key}"
                        )
                    expected_digest = (expected_layer_digests or layer_digests).get(
                        source_key
                    )
                    actual_digest = str(row[3]) if row[3] is not None else None
                    if (
                        source_key in (expected_layer_digests or layer_digests)
                        and actual_digest != expected_digest
                    ):
                        raise ConfigConflictError(
                            f"Gateway source layer digest CAS 冲突: {source_key}"
                        )
            if expected_source_baseline is not None:
                expected_sources = {
                    str(key): detail
                    for key, detail in expected_source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                current_rows = connection.execute(
                    """
                    SELECT config_key, vrn, presence, layer_revision,
                           layer_digest, source_generation
                    FROM config_source_layers
                    """
                ).fetchall()
                current_keys = {str(row[0]) for row in current_rows}
                if current_keys != set(expected_sources):
                    raise ConfigConflictError(
                        "Gateway source layer 集合 CAS 冲突: "
                        f"current={sorted(current_keys)}, expected={sorted(expected_sources)}"
                    )
                for row in current_rows:
                    detail = expected_sources[str(row[0])]
                    actual_vrn = str(row[1]) if row[1] is not None else None
                    expected_vrn = (
                        str(detail.get("vrn"))
                        if detail.get("vrn") is not None
                        else None
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
                            f"Gateway source layer 完整基线 CAS 冲突: key={row[0]}"
                        )
            for source_key, expected_revision in (
                expected_layer_revisions or {}
            ).items():
                row = connection.execute(
                    """
                    SELECT layer_revision, layer_digest
                    FROM config_source_layers
                    WHERE config_key = ?
                    """,
                    (source_key,),
                ).fetchone()
                if row is None or int(row[0]) != expected_revision:
                    raise ConfigConflictError(
                        "Gateway source layer revision CAS 冲突: "
                        f"key={source_key}, expected={expected_revision}"
                    )
                expected_digest = (expected_layer_digests or {}).get(source_key)
                actual_digest = str(row[1]) if row[1] is not None else None
                if (
                    source_key in (expected_layer_digests or {})
                    and actual_digest != expected_digest
                ):
                    raise ConfigConflictError(
                        "Gateway source layer digest CAS 冲突: "
                        f"key={source_key}, current={actual_digest}, "
                        f"expected={expected_digest}"
                    )
            revision = self._next_config_revision(
                connection, config_domain=config_domain
            )
            values = (
                config_domain,
                revision,
                candidate_id,
                dump_json(payload),
                dump_json(source_baseline),
                source_generation,
                dump_json(layer_revisions),
                dump_json(layer_digests),
                effective_digest,
                dump_json(secret_bindings or {}),
                schema_version,
                promoted_generation,
                promoted_apply_id,
                utc_now_text(),
            )
            connection.execute(
                """
                INSERT INTO config_active_snapshot(
                    config_domain, active_revision, candidate_id, payload_json,
                    source_baseline_json, source_generation, layer_revisions_json,
                    layer_digests_json, effective_digest, secret_bindings_json,
                    schema_version, promoted_generation, promoted_apply_id, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_domain) DO UPDATE SET
                    active_revision=excluded.active_revision,
                    candidate_id=excluded.candidate_id,
                    payload_json=excluded.payload_json,
                    source_baseline_json=excluded.source_baseline_json,
                    source_generation=excluded.source_generation,
                    layer_revisions_json=excluded.layer_revisions_json,
                    layer_digests_json=excluded.layer_digests_json,
                    effective_digest=excluded.effective_digest,
                    secret_bindings_json=excluded.secret_bindings_json,
                    schema_version=excluded.schema_version,
                    promoted_generation=excluded.promoted_generation,
                    promoted_apply_id=excluded.promoted_apply_id,
                    promoted_at=excluded.promoted_at
                """,
                values,
            )
            validate_state_transition(expected_pending_state, "active")
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'active', last_error = NULL
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (config_domain, candidate_id, expected_pending_state),
            )
            if gateway_candidate_ref is not None:
                intent_cursor = connection.execute(
                    """
                    UPDATE gateway_restart_intent
                    SET state = 'active', health_proof_json = ?,
                        last_error = NULL, updated_at = ?
                    WHERE candidate_ref = ? AND candidate_id = ?
                      AND state = 'applying' AND fencing_token = ?
                    """,
                    (
                        dump_json(gateway_health_proof),
                        utc_now_text(),
                        gateway_candidate_ref,
                        candidate_id,
                        expected_fencing_token,
                    ),
                )
                if intent_cursor.rowcount != 1:
                    raise ConfigConflictError(
                        "Gateway restart intent promotion fencing/CAS 校验失败"
                    )
                if gateway_old_generation_id is not None:
                    old_cursor = connection.execute(
                        """
                        UPDATE gateway_runtime_generation
                        SET listener_state = 'draining', updated_at = ?
                        WHERE config_domain = 'gateway' AND generation_id = ?
                          AND state = 'active'
                          AND listener_state IN ('serving', 'draining')
                        """,
                        (utc_now_text(), gateway_old_generation_id),
                    )
                    if old_cursor.rowcount != 1:
                        raise ConfigConflictError(
                            "Gateway promotion 旧 generation 排空 CAS 失败"
                        )
                new_cursor = connection.execute(
                    """
                    UPDATE gateway_runtime_generation
                    SET state = 'active', listener_state = 'serving', updated_at = ?
                    WHERE config_domain = 'gateway' AND generation_id = ?
                      AND state = 'healthy' AND listener_state = 'reserved'
                      AND fencing_token = ?
                    """,
                    (
                        utc_now_text(),
                        gateway_runtime_generation_id,
                        expected_fencing_token,
                    ),
                )
                if new_cursor.rowcount != 1:
                    raise ConfigConflictError(
                        "Gateway promotion 新 generation serving CAS 失败"
                    )
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        commit_revision=(
                            event.commit_revision
                            if event.commit_revision is not None
                            else revision
                        ),
                        active_revision=(
                            event.active_revision
                            if event.active_revision is not None
                            else revision
                        ),
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else expected_pending_revision
                        ),
                    ),
                )
            if promoted_apply_id is not None:
                journal_cursor = connection.execute(
                    """
                    UPDATE config_apply_journal
                    SET state = 'committed', last_error = NULL, updated_at = ?
                    WHERE apply_id = ? AND state = 'applying'
                    """,
                    (utc_now_text(), promoted_apply_id),
                )
                if journal_cursor.rowcount != 1:
                    raise ConfigConflictError(
                        "Gateway active promotion 的 apply journal CAS 失败"
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_active_config_snapshot(config_domain)
        if result is None:
            raise RuntimeError("Gateway active promotion 后无法读取")
        return result
