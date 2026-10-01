"""Workspace pending config candidate 方法族。

WorkspaceStateStorePendingMixin 只承载本族方法，由 WorkspaceStateStore 装配；
依赖宿主提供的 _database，不复制其它族 SQL。"""

from __future__ import annotations

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

__all__ = ["WorkspaceStateStorePendingMixin"]


class WorkspaceStateStorePendingMixin:
    def get_pending_config_candidate(
        self,
        *,
        config_domain: str,
        candidate_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConfigPendingCandidateRecord | None:
        if candidate_id is not None and idempotency_key is not None:
            raise ValueError("读取 pending candidate 不能同时指定两个身份")
        connection = self._database.connection()
        try:
            if candidate_id is None and idempotency_key is None:
                row = connection.execute(
                    """
                    SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                           payload_json, source_baseline_json, candidate_digest,
                           effective_digest, target_generation, fencing_token, state,
                           last_error, created_at, last_attempt_id, last_apply_id,
                           candidate_ref, base_active_revision, persistence_location,
                           source_generation, secret_bindings_json
                    FROM config_pending_candidate
                    WHERE config_domain = ?
                    ORDER BY pending_revision DESC
                    LIMIT 1
                    """,
                    (config_domain,),
                ).fetchone()
            else:
                field = "candidate_id" if candidate_id is not None else "idempotency_key"
                value = candidate_id if candidate_id is not None else idempotency_key
                row = connection.execute(
                    f"""
                    SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                           payload_json, source_baseline_json, candidate_digest,
                           effective_digest, target_generation, fencing_token, state,
                           last_error, created_at, last_attempt_id, last_apply_id,
                           candidate_ref, base_active_revision, persistence_location,
                           source_generation, secret_bindings_json
                    FROM config_pending_candidate
                    WHERE config_domain = ? AND {field} = ?
                    """,
                    (config_domain, value),
                ).fetchone()
            if row is None:
                return None
            return ConfigPendingCandidateRecord(
                config_domain=str(row[0]),
                candidate_id=str(row[1]),
                idempotency_key=str(row[2]),
                pending_revision=int(row[3]),
                payload=load_json_object(str(row[4]), field="pending candidate payload"),
                source_baseline=load_json_object(
                    str(row[5]), field="pending candidate source baseline"
                ),
                candidate_digest=str(row[6]),
                effective_digest=str(row[7]),
                target_generation=(
                    str(row[8]) if row[8] is not None else None
                ),
                fencing_token=str(row[9]) if row[9] is not None else None,
                state=cast(ConfigLifecycleState, str(row[10])),
                last_error=str(row[11]) if row[11] is not None else None,
                created_at=datetime.fromisoformat(str(row[12])),
                last_attempt_id=str(row[13]) if row[13] is not None else None,
                last_apply_id=str(row[14]) if row[14] is not None else None,
                candidate_ref=str(row[15]) if row[15] is not None else None,
                base_active_revision=(
                    int(row[16]) if row[16] is not None else None
                ),
                persistence_location=str(row[17]) if row[17] else None,
                source_generation=(
                    int(row[18]) if row[18] is not None else None
                ),
                secret_bindings=load_json_object(
                    str(row[19]), field="pending candidate secret bindings"
                ),
            )
        finally:
            connection.close()

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
        candidate_ref: str | None = None,
        base_active_revision: int | None = None,
        persistence_location: str | None = None,
        source_generation: int | None = None,
        secret_bindings: dict[str, object] | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigPendingCandidateRecord:
        if state != "candidate_validated":
            validate_state_transition("none", state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT candidate_id, payload_json, candidate_digest, state,
                       source_baseline_json, base_active_revision
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
                    or str(existing[4]) != dump_json(source_baseline)
                    or (
                        existing[5] is not None
                        and int(existing[5]) != base_active_revision
                    )
                ):
                    raise ConfigConflictError(
                        "idempotency_key 已绑定不同 pending candidate: "
                        f"domain={config_domain}, key={idempotency_key}"
                    )
                existing_state = cast(ConfigLifecycleState, str(existing[3]))
                if existing_state != state:
                    validate_state_transition(existing_state, state)
                    connection.execute(
                        """
                        UPDATE config_pending_candidate
                        SET state = ?, last_error = ?, candidate_ref = COALESCE(?, candidate_ref)
                        WHERE config_domain = ? AND candidate_id = ? AND state = ?
                        """,
                        (
                            state,
                            last_error,
                            candidate_ref,
                            config_domain,
                            candidate_id,
                            existing_state,
                        ),
                    )
                if event is not None:
                    self._insert_config_event(connection, event)
                connection.execute("COMMIT")
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
                        last_error, created_at, candidate_ref,
                        base_active_revision, persistence_location, source_generation,
                        secret_bindings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        candidate_ref,
                        base_active_revision,
                        persistence_location or str(self.path),
                        source_generation,
                        dump_json(secret_bindings or build_secret_binding_summary(payload)),
                    ),
                )
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
        result = self.get_pending_config_candidate(
            config_domain=config_domain,
            candidate_id=candidate_id,
        )
        if result is None:
            raise RuntimeError(f"pending candidate 提交后无法读取: {candidate_id}")
        return result

    def update_pending_config_candidate_state(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        expected_state: ConfigLifecycleState,
        state: ConfigLifecycleState,
        last_error: str | None = None,
        candidate_ref: str | None = None,
        target_generation: str | None = None,
        fencing_token: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigPendingCandidateRecord:
        validate_state_transition(expected_state, state)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT pending_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (config_domain, candidate_id, expected_state),
            ).fetchone()
            if existing is None:
                raise ConfigConflictError(
                    "pending candidate 状态 CAS 失败: "
                    f"domain={config_domain}, candidate={candidate_id}, expected={expected_state}"
                )
            cursor = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = ?, last_error = ?,
                    candidate_ref = COALESCE(?, candidate_ref),
                    target_generation = COALESCE(?, target_generation),
                    fencing_token = COALESCE(?, fencing_token)
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (
                    state,
                    last_error,
                    candidate_ref,
                    target_generation,
                    fencing_token,
                    config_domain,
                    candidate_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "pending candidate 状态 CAS 失败: "
                    f"domain={config_domain}, candidate={candidate_id}, expected={expected_state}"
                )
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        pending_revision=(
                            event.pending_revision
                            if event.pending_revision is not None
                            else int(existing[0])
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
            raise RuntimeError(f"pending candidate 更新后无法读取: {candidate_id}")
        return result

    def load_pending_config_candidate(
        self,
        *,
        candidate_ref: str,
        allow_recovery: bool = False,
        allow_discarded: bool = False,
    ) -> ConfigPendingCandidateRecord:
        """按不透明 ref 精确读取 Workspace pending，禁止回退到 active。"""

        if not candidate_ref:
            raise ValueError("Workspace candidate_ref 不能为空")
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, candidate_id, idempotency_key, pending_revision,
                       payload_json, source_baseline_json, candidate_digest,
                       effective_digest, target_generation, fencing_token, state,
                       last_error, created_at, last_attempt_id, last_apply_id,
                       candidate_ref
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ConfigConflictError(
                f"Workspace candidate_ref 不存在或不属于当前工作区: {candidate_ref}"
            )
        result = self.get_pending_config_candidate(
            config_domain="workspace",
            candidate_id=str(row[1]),
        )
        if result is None:
            raise RuntimeError("Workspace pending candidate 读取后消失")
        if result.candidate_ref != candidate_ref:
            raise ConfigConflictError("Workspace candidate_ref 读取校验失败")
        allowed_states = {"pending_restart", "applying"}
        if allow_recovery:
            allowed_states.add("recovery_required")
        if allow_discarded:
            allowed_states.add("discarded")
        if result.state not in allowed_states:
            raise ConfigConflictError(
                "Workspace candidate_ref 当前不允许启动: "
                f"candidate={result.candidate_id}, state={result.state}"
            )
        return result
