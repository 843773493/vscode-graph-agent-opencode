"""active config snapshot 与 pending candidate 垂直链路。

承载 ``config_revision_meta`` 的 revision 分配、``config_active_snapshot`` 的
读取/恢复标记/幂等建立，以及 ``config_pending_candidate`` 的读取/
create-or-get/状态 CAS 更新。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    validate_state_transition,
)


class ConfigActiveSnapshotMixin:
    """active snapshot 与 pending candidate 方法族（唯一实现点）。"""

    def _next_config_revision(
        self, connection: sqlite3.Connection, *, config_domain: str
    ) -> int:
        row = connection.execute(
            "SELECT next_revision FROM config_revision_meta WHERE config_domain = ?",
            (config_domain,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO config_revision_meta(config_domain, next_revision) VALUES (?, 2)",
                (config_domain,),
            )
            return 1
        revision = int(row[0])
        connection.execute(
            "UPDATE config_revision_meta SET next_revision = ? WHERE config_domain = ?",
            (revision + 1, config_domain),
        )
        return revision

    def get_active_config_snapshot(self, config_domain: str):
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, active_revision, candidate_id, payload_json,
                       source_baseline_json, source_generation, layer_revisions_json,
                       layer_digests_json, effective_digest, secret_bindings_json,
                       schema_version, promoted_generation, promoted_apply_id, promoted_at,
                       state, last_error
                FROM config_active_snapshot WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        from app.services.infrastructure.config.state import ConfigActiveSnapshotRecord

        try:
            return ConfigActiveSnapshotRecord(
                config_domain=str(row[0]),
                active_revision=int(row[1]),
                candidate_id=str(row[2]) if row[2] is not None else None,
                payload=load_json_object(str(row[3]), field="Gateway active payload"),
                source_baseline=load_json_object(
                    str(row[4]), field="Gateway active baseline"
                ),
                source_generation=int(row[5]),
                layer_revisions={
                    str(k): int(v)
                    for k, v in load_json_object(
                        str(row[6]), field="Gateway active revisions"
                    ).items()
                },
                layer_digests={
                    str(k): cast(str | None, v)
                    for k, v in load_json_object(
                        str(row[7]), field="Gateway active digests"
                    ).items()
                },
                effective_digest=str(row[8]),
                secret_bindings=load_json_object(
                    str(row[9]), field="Gateway active secrets"
                ),
                schema_version=int(row[10]),
                promoted_generation=str(row[11]) if row[11] is not None else None,
                promoted_apply_id=str(row[12]) if row[12] is not None else None,
                promoted_at=datetime.fromisoformat(str(row[13])),
                state=cast(ConfigLifecycleState, str(row[14])),
                last_error=str(row[15]) if row[15] is not None else None,
            )
        except Exception as error:
            self.mark_active_config_snapshot_recovery_required(
                config_domain=config_domain,
                error=f"Gateway active snapshot 损坏: {type(error).__name__}: {error}",
            )
            raise

    def mark_active_config_snapshot_recovery_required(
        self,
        *,
        config_domain: str,
        error: str,
    ) -> None:
        """在 active payload 损坏时保留记录并显式进入恢复态。"""

        if not error:
            raise ValueError("Gateway active snapshot 恢复错误不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_active_snapshot
                SET state = 'recovery_required', last_error = ?
                WHERE config_domain = ?
                """,
                (error, config_domain),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway active snapshot 恢复标记目标不存在")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_active_config_snapshot(
        self,
        *,
        config_domain: str,
        payload: dict[str, object],
        source_baseline: dict[str, object],
        source_generation: int,
        layer_revisions: dict[str, int],
        layer_digests: dict[str, str | None],
        effective_digest: str,
        schema_version: int,
        promoted_generation: str | None = None,
        secret_bindings: dict[str, object] | None = None,
    ):
        existing = self.get_active_config_snapshot(config_domain)
        if existing is not None:
            return existing
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM config_active_snapshot WHERE config_domain = ?",
                    (config_domain,),
                ).fetchone()
                is None
            ):
                revision = self._next_config_revision(
                    connection, config_domain=config_domain
                )
                connection.execute(
                    """
                    INSERT INTO config_active_snapshot(
                        config_domain, active_revision, candidate_id, payload_json,
                        source_baseline_json, source_generation, layer_revisions_json,
                        layer_digests_json, effective_digest, secret_bindings_json,
                        schema_version, promoted_generation, promoted_apply_id, promoted_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        config_domain,
                        revision,
                        dump_json(payload),
                        dump_json(source_baseline),
                        source_generation,
                        dump_json(layer_revisions),
                        dump_json(layer_digests),
                        effective_digest,
                        dump_json(secret_bindings or {}),
                        schema_version,
                        promoted_generation,
                        utc_now_text(),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_active_config_snapshot(config_domain)
        if result is None:
            raise RuntimeError("Gateway active snapshot 提交后无法读取")
        return result

    def get_pending_config_candidate(
        self,
        *,
        config_domain: str,
        candidate_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigPendingCandidateRecord | None:
        if candidate_id is not None and idempotency_key is not None:
            raise ValueError("Gateway pending candidate 不能同时指定两个身份")
        field = "candidate_id" if candidate_id is not None else "idempotency_key"
        value = candidate_id if candidate_id is not None else idempotency_key
        query = """
            SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                   payload_json, source_baseline_json, candidate_digest,
                   effective_digest, target_generation, fencing_token, state,
                   last_error, created_at, last_attempt_id, last_apply_id,
                   base_active_revision, persistence_location, source_generation,
                   secret_bindings_json
            FROM config_pending_candidate
            """ + (
            "WHERE config_domain = ? ORDER BY pending_revision DESC LIMIT 1"
            if value is None
            else f"WHERE config_domain = ? AND {field} = ?"
        )
        params = (config_domain,) if value is None else (config_domain, value)
        connection = self._database.connection()
        try:
            row = connection.execute(query, params).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ConfigPendingCandidateRecord(
            config_domain=str(row[0]),
            candidate_id=str(row[1]),
            idempotency_key=str(row[2]),
            pending_revision=int(row[3]),
            payload=load_json_object(str(row[4]), field="Gateway pending payload"),
            source_baseline=load_json_object(
                str(row[5]), field="Gateway pending baseline"
            ),
            candidate_digest=str(row[6]),
            effective_digest=str(row[7]),
            target_generation=str(row[8]) if row[8] is not None else None,
            fencing_token=str(row[9]) if row[9] is not None else None,
            state=cast(ConfigLifecycleState, str(row[10])),
            last_error=str(row[11]) if row[11] is not None else None,
            created_at=datetime.fromisoformat(str(row[12])),
            last_attempt_id=str(row[13]) if row[13] is not None else None,
            last_apply_id=str(row[14]) if row[14] is not None else None,
            base_active_revision=(int(row[15]) if row[15] is not None else None),
            persistence_location=str(row[16]) if row[16] else None,
            source_generation=(int(row[17]) if row[17] is not None else None),
            secret_bindings=load_json_object(
                str(row[18]), field="Gateway pending secret bindings"
            ),
        )

    def create_pending_config_candidate(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        idempotency_key: str,
        payload: dict[str, object],
        source_baseline: dict[str, object],
        candidate_digest: str,
        effective_digest: str,
        target_generation: str | None,
        fencing_token: str | None,
        state: ConfigLifecycleState,
        last_error: str | None = None,
        base_active_revision: int | None = None,
        persistence_location: str | None = None,
        source_generation: int | None = None,
        secret_bindings: dict[str, object] | None = None,
    ) -> ConfigPendingCandidateRecord:
        validate_state_transition("none", state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT candidate_id, payload_json, candidate_digest,
                       source_baseline_json, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND idempotency_key = ?
                """,
                (config_domain, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != candidate_id
                    or str(existing[1]) != dump_json(payload)
                    or str(existing[2]) != candidate_digest
                    or str(existing[3]) != dump_json(source_baseline)
                    or (
                        existing[5] is not None
                        and int(existing[5]) != base_active_revision
                    )
                ):
                    raise ConfigConflictError("Gateway pending idempotency CAS 冲突")
                existing_state = cast(ConfigLifecycleState, str(existing[4]))
                if existing_state != state:
                    validate_state_transition(existing_state, state)
                    connection.execute(
                        """
                        UPDATE config_pending_candidate
                        SET state = ?, last_error = ?
                        WHERE config_domain = ? AND candidate_id = ? AND state = ?
                        """,
                        (
                            state,
                            last_error,
                            config_domain,
                            candidate_id,
                            existing_state,
                        ),
                    )
            else:
                pending_revision = self._next_config_revision(
                    connection,
                    config_domain=config_domain,
                )
                connection.execute(
                    """
                    INSERT INTO config_pending_candidate(
                        config_domain, candidate_id, idempotency_key, pending_revision,
                        payload_json, source_baseline_json, candidate_digest,
                        effective_digest, target_generation, fencing_token, state,
                        last_error, created_at, last_attempt_id, last_apply_id,
                        base_active_revision, persistence_location, source_generation,
                        secret_bindings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        config_domain,
                        candidate_id,
                        idempotency_key,
                        pending_revision,
                        dump_json(payload),
                        dump_json(source_baseline),
                        candidate_digest,
                        effective_digest,
                        target_generation,
                        fencing_token,
                        state,
                        last_error,
                        utc_now_text(),
                        base_active_revision,
                        persistence_location or str(self.path),
                        source_generation,
                        dump_json(
                            secret_bindings or build_secret_binding_summary(payload)
                        ),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_pending_config_candidate(
            config_domain=config_domain,
            candidate_id=candidate_id,
        )
        if result is None:
            raise RuntimeError("Gateway pending candidate 提交后无法读取")
        return result

    def update_pending_config_candidate_state(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        expected_state: ConfigLifecycleState,
        state: ConfigLifecycleState,
        last_error: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigPendingCandidateRecord:
        validate_state_transition(expected_state, state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = ?, last_error = ?
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (state, last_error, config_domain, candidate_id, expected_state),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway pending 状态 CAS 冲突")
            if event is not None:
                pending = connection.execute(
                    """
                    SELECT pending_revision FROM config_pending_candidate
                    WHERE config_domain = ? AND candidate_id = ?
                    """,
                    (config_domain, candidate_id),
                ).fetchone()
                if pending is None:
                    raise RuntimeError("Gateway pending 事件关联记录消失")
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else int(pending[0])
                        ),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_pending_config_candidate(
            config_domain=config_domain,
            candidate_id=candidate_id,
        )
        if result is None:
            raise RuntimeError("Gateway pending 状态更新后无法读取")
        return result
