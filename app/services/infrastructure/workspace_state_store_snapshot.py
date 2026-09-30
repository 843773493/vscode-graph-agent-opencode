"""Workspace active snapshot 方法族。

WorkspaceStateStoreSnapshotMixin 只承载本族方法，由 WorkspaceStateStore 装配；
依赖宿主提供的 _database 与模块级 _SOURCE_LAYER_BASELINE_SELECT。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigActiveSnapshotRecord,
    ConfigConflictError,
    ConfigEventInput,
    ConfigLifecycleState,
    dump_json,
    load_json_object,
    validate_state_transition,
)
from app.services.infrastructure.workspace_state_store_sql import (
    _SOURCE_LAYER_BASELINE_SELECT,
)

__all__ = ["WorkspaceStateStoreSnapshotMixin"]


class WorkspaceStateStoreSnapshotMixin:
    def get_active_config_snapshot(
        self,
        config_domain: str,
    ) -> ConfigActiveSnapshotRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, active_revision, candidate_id, payload_json,
                       source_baseline_json, source_generation, layer_revisions_json,
                       layer_digests_json, effective_digest, secret_bindings_json,
                       schema_version, promoted_generation, promoted_apply_id, promoted_at,
                       state, last_error
                FROM config_active_snapshot
                WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            if row is None:
                return None
            try:
                return ConfigActiveSnapshotRecord(
                    config_domain=str(row[0]),
                    active_revision=int(row[1]),
                    candidate_id=str(row[2]) if row[2] is not None else None,
                    payload=load_json_object(
                        str(row[3]), field="active snapshot payload"
                    ),
                    source_baseline=load_json_object(
                        str(row[4]), field="active snapshot source baseline"
                    ),
                    source_generation=int(row[5]),
                    layer_revisions={
                        str(key): int(value)
                        for key, value in load_json_object(
                            str(row[6]), field="active snapshot layer revisions"
                        ).items()
                    },
                    layer_digests={
                        str(key): cast(str | None, value)
                        for key, value in load_json_object(
                            str(row[7]), field="active snapshot layer digests"
                        ).items()
                    },
                    effective_digest=str(row[8]),
                    secret_bindings=load_json_object(
                        str(row[9]), field="active snapshot secret bindings"
                    ),
                    schema_version=int(row[10]),
                    promoted_generation=(
                        str(row[11]) if row[11] is not None else None
                    ),
                    promoted_apply_id=str(row[12]) if row[12] is not None else None,
                    promoted_at=datetime.fromisoformat(str(row[13])),
                    state=cast(ConfigLifecycleState, str(row[14])),
                    last_error=str(row[15]) if row[15] is not None else None,
                )
            except Exception as error:
                self.mark_active_config_snapshot_recovery_required(
                    config_domain=config_domain,
                    error=(
                        "active snapshot 损坏: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
                raise
        finally:
            connection.close()

    def mark_active_config_snapshot_recovery_required(
        self,
        *,
        config_domain: str,
        error: str,
    ) -> None:
        """在 active payload 损坏时保留记录并显式进入恢复态。"""

        if not error:
            raise ValueError("active snapshot 恢复错误不能为空")
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
                raise ConfigConflictError("active snapshot 恢复标记目标不存在")
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
        secret_bindings: dict[str, object],
        schema_version: int,
        promoted_generation: str | None = None,
        promoted_apply_id: str | None = None,
    ) -> ConfigActiveSnapshotRecord:
        existing = self.get_active_config_snapshot(config_domain)
        if existing is not None:
            return existing
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT 1 FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            if existing_row is None:
                active_revision = self._next_config_revision(
                    connection,
                    config_domain=config_domain,
                )
                connection.execute(
                    """
                    INSERT INTO config_active_snapshot(
                        config_domain, active_revision, candidate_id, payload_json,
                        source_baseline_json, source_generation, layer_revisions_json,
                        layer_digests_json, effective_digest, secret_bindings_json,
                        schema_version, promoted_generation, promoted_apply_id, promoted_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_domain,
                        active_revision,
                        dump_json(payload),
                        dump_json(source_baseline),
                        source_generation,
                        dump_json(layer_revisions),
                        dump_json(layer_digests),
                        effective_digest,
                        dump_json(secret_bindings),
                        schema_version,
                        promoted_generation,
                        promoted_apply_id,
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
            raise RuntimeError(f"active snapshot 提交后无法读取: {config_domain}")
        return result

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
        secret_bindings: dict[str, object],
        schema_version: int,
        promoted_generation: str | None = None,
        promoted_apply_id: str | None = None,
        expected_active_revision: int | None = None,
        expected_source_generation: int | None = None,
        expected_source_baseline: dict[str, object] | None = None,
        expected_layer_revisions: dict[str, int] | None = None,
        expected_layer_digests: dict[str, str | None] | None = None,
        expected_pending_revision: int | None = None,
        expected_pending_state: ConfigLifecycleState | None = None,
        expected_fencing_token: str | None = None,
        event: ConfigEventInput | None = None,
    ) -> ConfigActiveSnapshotRecord:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active_row = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = (
                int(active_row[0]) if active_row is not None else None
            )
            if current_active_revision != expected_active_revision:
                raise ConfigConflictError(
                    "active snapshot CAS 冲突: "
                    f"domain={config_domain}, current={current_active_revision}, "
                    f"expected={expected_active_revision}"
                )
            if expected_source_generation is not None:
                source_keys = tuple((expected_layer_revisions or {}).keys())
                if not source_keys:
                    current_source_generation = 0
                else:
                    placeholders = ",".join("?" for _ in source_keys)
                    source_rows = connection.execute(
                        """
                        SELECT source_generation
                        FROM config_source_layers
                        WHERE config_key IN ("""
                        + placeholders
                        + ")",
                        source_keys,
                    ).fetchall()
                    current_source_generation = max(
                        (int(row[0]) for row in source_rows),
                        default=0,
                    )
                if current_source_generation != expected_source_generation:
                    raise ConfigConflictError(
                        "source generation CAS 冲突: "
                        f"domain={config_domain}, current={current_source_generation}, "
                        f"expected={expected_source_generation}"
                    )
            if expected_source_baseline is not None:
                expected_sources = {
                    str(key): detail
                    for key, detail in expected_source_baseline.items()
                    if isinstance(detail, dict)
                    and detail.get("layer_revision") is not None
                }
                current_rows = connection.execute(
                    _SOURCE_LAYER_BASELINE_SELECT
                ).fetchall()
                current_keys = {str(row[0]) for row in current_rows}
                if current_keys != set(expected_sources):
                    raise ConfigConflictError(
                        "source layer 集合 CAS 冲突: "
                        f"current={sorted(current_keys)}, expected={sorted(expected_sources)}"
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
                            "source layer 完整基线 CAS 冲突: "
                            f"key={row[0]}"
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
                        "source layer revision CAS 冲突: "
                        f"key={source_key}, expected={expected_revision}"
                    )
                expected_digest = (expected_layer_digests or {}).get(source_key)
                current_digest = str(row[1]) if row[1] is not None else None
                if (
                    source_key in (expected_layer_digests or {})
                    and current_digest != expected_digest
                ):
                    raise ConfigConflictError(
                        "source layer digest CAS 冲突: "
                        f"key={source_key}, current={current_digest}, "
                        f"expected={expected_digest}"
                    )
            pending_row = connection.execute(
                """
                SELECT pending_revision, state
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (config_domain, candidate_id),
            ).fetchone()
            if expected_pending_revision is not None and (
                pending_row is None or int(pending_row[0]) != expected_pending_revision
            ):
                raise ConfigConflictError(
                    "pending candidate revision CAS 冲突: "
                    f"candidate={candidate_id}, expected={expected_pending_revision}"
                )
            if expected_pending_state is not None and (
                pending_row is None or str(pending_row[1]) != expected_pending_state
            ):
                raise ConfigConflictError(
                    "pending candidate 状态 CAS 冲突: "
                    f"candidate={candidate_id}, expected={expected_pending_state}"
                )
            if expected_fencing_token is not None:
                claim_row = connection.execute(
                    """
                    SELECT fencing_token, candidate_id
                    FROM config_apply_claim
                    WHERE config_domain = ?
                    """,
                    (config_domain,),
                ).fetchone()
                if (
                    claim_row is None
                    or str(claim_row[0]) != expected_fencing_token
                    or str(claim_row[1]) != candidate_id
                ):
                    raise ConfigConflictError("active promotion fencing 校验失败")
            if active_row is None:
                active_revision = self._next_config_revision(
                    connection,
                    config_domain=config_domain,
                )
                connection.execute(
                    """
                    INSERT INTO config_active_snapshot(
                        config_domain, active_revision, candidate_id, payload_json,
                        source_baseline_json, source_generation, layer_revisions_json,
                        layer_digests_json, effective_digest, secret_bindings_json,
                        schema_version, promoted_generation, promoted_apply_id, promoted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        config_domain,
                        active_revision,
                        candidate_id,
                        dump_json(payload),
                        dump_json(source_baseline),
                        source_generation,
                        dump_json(layer_revisions),
                        dump_json(layer_digests),
                        effective_digest,
                        dump_json(secret_bindings),
                        schema_version,
                        promoted_generation,
                        promoted_apply_id,
                        utc_now_text(),
                    ),
                )
            else:
                active_revision = self._next_config_revision(
                    connection,
                    config_domain=config_domain,
                )
                connection.execute(
                    """
                    UPDATE config_active_snapshot
                    SET active_revision = ?, candidate_id = ?, payload_json = ?,
                        source_baseline_json = ?, source_generation = ?,
                        layer_revisions_json = ?, layer_digests_json = ?,
                        effective_digest = ?, secret_bindings_json = ?,
                        schema_version = ?, promoted_generation = ?,
                        promoted_apply_id = ?, promoted_at = ?
                    WHERE config_domain = ?
                    """,
                    (
                        active_revision,
                        candidate_id,
                        dump_json(payload),
                        dump_json(source_baseline),
                        source_generation,
                        dump_json(layer_revisions),
                        dump_json(layer_digests),
                        effective_digest,
                        dump_json(secret_bindings),
                        schema_version,
                        promoted_generation,
                        promoted_apply_id,
                        utc_now_text(),
                        config_domain,
                    ),
                )
            if pending_row is not None and expected_pending_state is not None:
                validate_state_transition(expected_pending_state, "active")
                connection.execute(
                    """
                    UPDATE config_pending_candidate
                    SET state = 'active', last_error = NULL
                    WHERE config_domain = ? AND candidate_id = ? AND state = ?
                    """,
                    (config_domain, candidate_id, expected_pending_state),
                )
            if event is not None:
                self._insert_config_event(
                    connection,
                    replace(
                        event,
                        commit_revision=(
                            event.commit_revision
                            if event.commit_revision is not None
                            else active_revision
                        ),
                        active_revision=(
                            event.active_revision
                            if event.active_revision is not None
                            else active_revision
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
                        "Workspace active promotion 的 apply journal CAS 失败"
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_active_config_snapshot(config_domain)
        if result is None:
            raise RuntimeError(f"active snapshot promotion 后无法读取: {config_domain}")
        return result
