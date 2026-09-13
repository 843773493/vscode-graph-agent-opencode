from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from app.core.sqlite_state import (
    SQLiteDiagnostics,
    SQLiteStateDatabase,
    utc_now_text,
)
from app.services.infrastructure.config.state import (
    ConfigApplyClaimRecord,
    ConfigApplyJournalRecord,
    ConfigConflictError,
    ConfigEventCursorGoneError,
    ConfigEventInput,
    ConfigEventRecord,
    ConfigEventRelayState,
    ConfigLifecycleState,
    ConfigPendingCandidateRecord,
    ConfigResult,
    ConfigSourceJournalRecord,
    ConfigSourceLayerRecord,
    GatewayRestartIntentRecord,
    GatewayRuntimeGenerationRecord,
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    migrate_legacy_secret_payload,
    new_config_id,
    prepare_config_for_persistence,
    validate_state_transition,
)

_GATEWAY_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS gateway_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gateway_workspace_registry (
        workspace_id TEXT PRIMARY KEY,
        position INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_account (
        user_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS user_access_lease (
        user_id TEXT PRIMARY KEY REFERENCES user_account(user_id) ON DELETE CASCADE,
        lease_generation INTEGER NOT NULL,
        access_session_id TEXT NOT NULL UNIQUE,
        client_label TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_view_state (
        user_id TEXT NOT NULL REFERENCES user_account(user_id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_anchor TEXT,
        scroll_offset REAL NOT NULL DEFAULT 0,
        follow_latest INTEGER NOT NULL DEFAULT 0,
        projection_version INTEGER NOT NULL DEFAULT 1,
        tool_details_expanded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, workspace_id, session_id)
    );
    CREATE TABLE IF NOT EXISTS guest_tracking (
        guest_id TEXT PRIMARY KEY,
        tracking_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS config_source_layers (
        config_key TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        config_version INTEGER NOT NULL,
        payload_json TEXT,
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        source_generation INTEGER NOT NULL,
        previous_digest TEXT,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS config_revision_meta (
        config_domain TEXT PRIMARY KEY,
        next_revision INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_active_snapshot (
        config_domain TEXT PRIMARY KEY,
        active_revision INTEGER NOT NULL,
        candidate_id TEXT,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        layer_revisions_json TEXT NOT NULL,
        layer_digests_json TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        secret_bindings_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        promoted_generation TEXT,
        promoted_apply_id TEXT,
        promoted_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_pending_candidate (
        config_domain TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        pending_revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        source_baseline_json TEXT NOT NULL,
        candidate_digest TEXT NOT NULL,
        effective_digest TEXT NOT NULL,
        target_generation TEXT,
        fencing_token TEXT,
        state TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        last_attempt_id TEXT,
        last_apply_id TEXT,
        PRIMARY KEY (config_domain, candidate_id),
        UNIQUE (config_domain, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS config_apply_claim (
        config_domain TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        apply_id TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        target_generation TEXT,
        lease_expires_at TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        config_domain TEXT NOT NULL,
        candidate_id TEXT,
        attempt_id TEXT,
        apply_id TEXT,
        idempotency_key TEXT,
        commit_revision INTEGER,
        active_revision INTEGER,
        pending_revision INTEGER,
        source TEXT NOT NULL,
        result TEXT NOT NULL,
        changed_paths_json TEXT NOT NULL,
        applied_paths_json TEXT NOT NULL,
        deferred_paths_json TEXT NOT NULL,
        error TEXT,
        occurred_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS registry_meta (
        registry_key TEXT PRIMARY KEY,
        revision INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_source_owner (
        source_key TEXT PRIMARY KEY,
        next_generation INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_source_journal (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        source_path TEXT NOT NULL,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        previous_digest TEXT,
        origin TEXT NOT NULL,
        fanout_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS gateway_restart_intent (
        intent_id TEXT PRIMARY KEY,
        candidate_ref TEXT NOT NULL UNIQUE,
        candidate_id TEXT NOT NULL,
        base_active_revision INTEGER,
        old_generation TEXT,
        target_generation TEXT NOT NULL,
        fencing_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'applying', 'active', 'failed',
                      'recovery_required', 'discarded')
        ),
        requested_by TEXT NOT NULL,
        health_proof_json TEXT,
        last_error TEXT,
        requested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE config_events ADD COLUMN activation_scope TEXT NOT NULL DEFAULT 'unknown';
    """,
    """
    ALTER TABLE config_events ADD COLUMN relay_state TEXT NOT NULL DEFAULT 'pending';
    ALTER TABLE config_events ADD COLUMN relay_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE config_events ADD COLUMN relay_last_error TEXT;
    ALTER TABLE config_events ADD COLUMN relay_claimed_by TEXT;
    ALTER TABLE config_events ADD COLUMN relay_claimed_until TEXT;
    ALTER TABLE config_events ADD COLUMN relay_next_attempt_at TEXT
        NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';
    CREATE INDEX IF NOT EXISTS config_events_relay_idx
        ON config_events(relay_state, relay_next_attempt_at, event_seq);
    """,
    """
    CREATE TABLE IF NOT EXISTS registry_apply_journal (
        apply_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        target_revision INTEGER,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed', 'recovery_required')
        ),
        payload_digest TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS registry_apply_journal_state_idx
        ON registry_apply_journal(state, updated_at);
    """,
    """
    CREATE TABLE IF NOT EXISTS config_event_relay_delivery (
        event_id TEXT NOT NULL REFERENCES config_events(event_id) ON DELETE CASCADE,
        consumer_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('claimed', 'delivered', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        claimed_until TEXT,
        next_attempt_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (event_id, consumer_id)
    );
    CREATE INDEX IF NOT EXISTS config_event_relay_delivery_due_idx
        ON config_event_relay_delivery(consumer_id, state, next_attempt_at, event_id);
    """,
    """
    ALTER TABLE config_pending_candidate ADD COLUMN base_active_revision INTEGER;
    ALTER TABLE config_pending_candidate ADD COLUMN persistence_location TEXT
        NOT NULL DEFAULT '';
    """,
    """
    ALTER TABLE config_source_layers ADD COLUMN previous_payload_json TEXT;
    ALTER TABLE config_source_layers ADD COLUMN backup_path TEXT;
    ALTER TABLE config_pending_candidate ADD COLUMN source_generation INTEGER;
    ALTER TABLE config_active_snapshot ADD COLUMN state TEXT NOT NULL DEFAULT 'active';
    ALTER TABLE config_active_snapshot ADD COLUMN last_error TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS config_events_idempotency_result_unique
        ON config_events(config_domain, idempotency_key, result)
        WHERE idempotency_key IS NOT NULL;
    CREATE TABLE IF NOT EXISTS config_apply_journal (
        config_domain TEXT NOT NULL,
        apply_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        base_active_revision INTEGER,
        pending_revision INTEGER,
        source_baseline_json TEXT NOT NULL,
        active_baseline_json TEXT NOT NULL,
        registry_revision INTEGER,
        side_effects_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('applying', 'committed', 'failed',
                      'recovery_required', 'compensated')
        ),
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS config_apply_journal_domain_idx
        ON config_apply_journal(config_domain, state, updated_at);
    """,
    """
    CREATE TABLE IF NOT EXISTS gateway_runtime_generation (
        config_domain TEXT NOT NULL,
        generation_id TEXT PRIMARY KEY,
        process_id INTEGER,
        loaded_source TEXT NOT NULL CHECK (loaded_source IN ('active', 'pending')),
        candidate_id TEXT,
        active_revision INTEGER,
        pending_revision INTEGER,
        candidate_digest TEXT,
        effective_digest TEXT NOT NULL,
        secret_binding_digest TEXT,
        fencing_token TEXT,
        listener_state TEXT NOT NULL CHECK (
            listener_state IN ('reserved', 'serving', 'draining', 'closed')
        ),
        state TEXT NOT NULL CHECK (
            state IN ('starting', 'healthy', 'active', 'failed', 'closed')
        ),
        health_proof_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS gateway_runtime_generation_domain_idx
        ON gateway_runtime_generation(config_domain, state, updated_at);
    """,
    """
    ALTER TABLE config_pending_candidate ADD COLUMN secret_bindings_json TEXT
        NOT NULL DEFAULT '{}';
    """,
    """
    ALTER TABLE gateway_restart_intent ADD COLUMN gateway_id TEXT;
    ALTER TABLE gateway_restart_intent ADD COLUMN expires_at TEXT;
    CREATE INDEX IF NOT EXISTS gateway_restart_intent_gateway_idx
        ON gateway_restart_intent(gateway_id, state, updated_at);
    """,
)


@dataclass(frozen=True, slots=True)
class GatewayConfigRecord:
    config_key: str
    config_version: int
    payload: dict[str, object]


class GatewayStateStore:
    def __init__(self, *, path: Path, allow_shared_processes: bool = False) -> None:
        self._database = SQLiteStateDatabase(
            path=path,
            schema_version=len(_GATEWAY_MIGRATIONS),
            migrations=_GATEWAY_MIGRATIONS,
            # 只有 supervisor 管理的 pending successor 才允许共享 WAL；
            # 普通 Gateway 继续保留单进程 ownership lock。
            allow_shared_processes=allow_shared_processes,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

    def connection(self) -> sqlite3.Connection:
        return self._database.connection()

    def set_config(
        self,
        *,
        config_key: str,
        config_version: int,
        payload: dict[str, object],
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                """
                INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    config_key,
                    config_version,
                    dump_json(prepare_config_for_persistence(payload)),
                    utc_now_text(),
                ),
            )
        finally:
            connection.close()

    def migrate_legacy_config_secrets(self, config_key: str) -> tuple[str, ...]:
        """升级旧 Gateway 配置表和 source layer 中的秘密引用。

        字面量 key 保留原文（已受支持）；只有旧版本写入的不可逆
        ``literal-sha256:`` 摘要才会被记为阻断路径。
        """

        connection = self._database.connection()
        blocked: set[str] = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
            legacy_row = connection.execute(
                "SELECT payload_json FROM gateway_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if legacy_row is not None:
                migrated, paths = migrate_legacy_secret_payload(
                    json.loads(str(legacy_row[0]))
                )
                blocked.update(paths)
                connection.execute(
                    "UPDATE gateway_config SET payload_json = ?, updated_at = ? WHERE config_key = ?",
                    (dump_json(migrated), utc_now_text(), config_key),
                )
            source_row = connection.execute(
                """
                SELECT payload_json, previous_payload_json
                FROM config_source_layers WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if source_row is not None:
                migrated_payload = None
                migrated_previous = None
                if source_row[0] is not None:
                    migrated_payload, paths = migrate_legacy_secret_payload(
                        json.loads(str(source_row[0]))
                    )
                    blocked.update(paths)
                if source_row[1] is not None:
                    migrated_previous, paths = migrate_legacy_secret_payload(
                        json.loads(str(source_row[1]))
                    )
                    blocked.update(paths)
                connection.execute(
                    """
                    UPDATE config_source_layers
                    SET payload_json = ?, previous_payload_json = ?, updated_at = ?
                    WHERE config_key = ?
                    """,
                    (
                        dump_json(migrated_payload)
                        if migrated_payload is not None
                        else None,
                        dump_json(migrated_previous)
                        if migrated_previous is not None
                        else None,
                        utc_now_text(),
                        config_key,
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(sorted(blocked))

    def migrate_legacy_active_snapshot_secrets(
        self,
        *,
        config_domain: str,
    ) -> tuple[str, ...]:
        """升级旧 Gateway active payload，并显式标记无法恢复的旧摘要秘密。"""

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return ()
            migrated, blocked = migrate_legacy_secret_payload(json.loads(str(row[0])))
            connection.execute(
                """
                UPDATE config_active_snapshot
                SET payload_json = ?, secret_bindings_json = ?,
                    state = CASE WHEN ? = 1 THEN 'recovery_required' ELSE state END,
                    last_error = CASE WHEN ? = 1 THEN ? ELSE last_error END
                WHERE config_domain = ?
                """,
                (
                    dump_json(migrated),
                    dump_json(build_secret_binding_summary(migrated)),
                    int(bool(blocked)),
                    int(bool(blocked)),
                    "旧 Gateway active snapshot 含无法恢复的秘密摘要，需重新导入引用",
                    config_domain,
                ),
            )
            connection.execute("COMMIT")
            return blocked
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_config(self, config_key: str) -> GatewayConfigRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT config_key, config_version, payload_json FROM gateway_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[2]))
            if not isinstance(payload, dict):
                raise ValueError(f"Gateway SQLite 配置不是对象: key={config_key}")
            return GatewayConfigRecord(
                config_key=str(row[0]),
                config_version=int(row[1]),
                payload=payload,
            )
        finally:
            connection.close()

    def get_source_layer(self, config_key: str) -> ConfigSourceLayerRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, source_path, presence, config_version,
                       payload_json, layer_revision, layer_digest,
                       source_generation, previous_digest, updated_at,
                       previous_payload_json, backup_path
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ConfigSourceLayerRecord(
            config_key=str(row[0]),
            source_path=str(row[1]),
            presence=cast(str, row[2]),
            config_version=int(row[3]),
            payload=(
                load_json_object(str(row[4]), field="Gateway source layer payload")
                if row[4] is not None
                else None
            ),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            source_generation=int(row[7]),
            previous_digest=str(row[8]) if row[8] is not None else None,
            updated_at=datetime.fromisoformat(str(row[9])),
            previous_payload=(
                load_json_object(
                    str(row[10]), field="Gateway source layer previous payload"
                )
                if row[10] is not None
                else None
            ),
            backup_path=str(row[11]) if row[11] is not None else None,
        )

    def sync_config_source(
        self,
        *,
        config_key: str,
        source_path: Path,
        config_version: int,
        presence: str,
        payload: dict[str, object] | None,
        layer_digest: str | None,
        expected_layer_revision: int | None = None,
        expected_layer_digest: str | None = None,
        backup_path: Path | None = None,
        journal_origin: str | None = None,
        source_event_id: str | None = None,
        fanout_id: str | None = None,
    ) -> ConfigSourceLayerRecord:
        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 Gateway source layer presence: {presence}")
        if presence == "present" and payload is None:
            raise ValueError("Gateway present source layer 必须有 payload")
        if presence == "absent" and payload is not None:
            raise ValueError("Gateway absent source layer 的 payload 必须为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT source_path, presence, layer_revision, layer_digest,
                       source_generation, payload_json
                FROM config_source_layers WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if current is None:
                if (
                    expected_layer_revision is not None
                    or expected_layer_digest is not None
                ):
                    raise ConfigConflictError("Gateway source layer 初始 CAS 冲突")
                revision, generation, previous = 1, 1, None
                previous_payload_json = None
            else:
                current_path = str(current[0])
                current_presence = str(current[1])
                revision_now = int(current[2])
                digest_now = str(current[3]) if current[3] is not None else None
                if (
                    expected_layer_revision is not None
                    and revision_now != expected_layer_revision
                ) or (
                    expected_layer_digest is not None
                    and digest_now != expected_layer_digest
                ):
                    raise ConfigConflictError(
                        "Gateway source layer CAS 冲突: "
                        f"key={config_key}, revision={revision_now}, digest={digest_now}"
                    )
                if (
                    current_path == str(source_path.expanduser().resolve())
                    and current_presence == presence
                    and digest_now == layer_digest
                ):
                    sanitized_json = (
                        dump_json(prepare_config_for_persistence(payload))
                        if payload is not None
                        else None
                    )
                    now = utc_now_text()
                    connection.execute(
                        """
                        UPDATE config_source_layers
                        SET config_version = ?, payload_json = ?, updated_at = ?
                        WHERE config_key = ?
                        """,
                        (config_version, sanitized_json, now, config_key),
                    )
                    if presence == "present":
                        connection.execute(
                            """
                            INSERT INTO gateway_config(
                                config_key, config_version, payload_json, updated_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(config_key) DO UPDATE SET
                                config_version=excluded.config_version,
                                payload_json=excluded.payload_json,
                                updated_at=excluded.updated_at
                            """,
                            (config_key, config_version, sanitized_json, now),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM gateway_config WHERE config_key = ?",
                            (config_key,),
                        )
                    if journal_origin is not None:
                        self._append_config_source_journal_in_connection(
                            connection,
                            source_key=config_key,
                            source_event_id=(
                                source_event_id or f"{config_key}:layer:{revision_now}"
                            ),
                            source_path=source_path,
                            presence=presence,
                            layer_revision=revision_now,
                            layer_digest=layer_digest,
                            previous_digest=digest_now,
                            origin=journal_origin,
                            fanout_id=(
                                fanout_id
                                or f"fanout:{config_key}:event:{config_key}:layer:{revision_now}"
                            ),
                        )
                    connection.execute("COMMIT")
                    record = self.get_source_layer(config_key)
                    if record is None:
                        raise RuntimeError("Gateway source layer 去重后无法读取")
                    return record
                revision, generation, previous = (
                    revision_now + 1,
                    int(current[4]) + 1,
                    digest_now,
                )
                previous_payload_json = current[5]
            now = utc_now_text()
            payload_json = (
                dump_json(prepare_config_for_persistence(payload))
                if payload is not None
                else None
            )
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, source_path, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json, backup_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    source_path=excluded.source_path,
                    presence=excluded.presence,
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    source_generation=excluded.source_generation,
                    previous_digest=excluded.previous_digest,
                    updated_at=excluded.updated_at,
                    previous_payload_json=excluded.previous_payload_json,
                    backup_path=excluded.backup_path
                """,
                (
                    config_key,
                    str(source_path.expanduser().resolve()),
                    presence,
                    config_version,
                    payload_json,
                    revision,
                    layer_digest,
                    generation,
                    previous,
                    now,
                    previous_payload_json,
                    str(backup_path.expanduser().resolve())
                    if backup_path is not None
                    else None,
                ),
            )
            if journal_origin is not None:
                self._append_config_source_journal_in_connection(
                    connection,
                    source_key=config_key,
                    source_event_id=(
                        source_event_id or f"{config_key}:layer:{revision}"
                    ),
                    source_path=source_path,
                    presence=presence,
                    layer_revision=revision,
                    layer_digest=layer_digest,
                    previous_digest=previous,
                    origin=journal_origin,
                    fanout_id=(
                        fanout_id
                        or f"fanout:{config_key}:event:{config_key}:layer:{revision}"
                    ),
                )
            if presence == "present":
                connection.execute(
                    """
                    INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        config_key,
                        config_version,
                        dump_json(prepare_config_for_persistence(payload)),
                        now,
                    ),
                )
            else:
                connection.execute(
                    "DELETE FROM gateway_config WHERE config_key = ?",
                    (config_key,),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError("Gateway source layer 提交后无法读取")
        return record

    @staticmethod
    def _append_config_source_journal_in_connection(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> int:
        """在调用方事务内追加 source journal，避免 source/journal 裂脑。"""
        if presence not in {"present", "absent"}:
            raise ValueError("Gateway source journal presence 无效")
        existing = connection.execute(
            """
            SELECT source_key, source_generation, layer_revision, layer_digest,
                   presence
            FROM config_source_journal
            WHERE source_event_id = ?
            """,
            (source_event_id,),
        ).fetchone()
        if existing is not None:
            existing_digest = str(existing[3]) if existing[3] is not None else None
            if (
                str(existing[0]) != source_key
                or int(existing[2]) != layer_revision
                or existing_digest != layer_digest
                or str(existing[4]) != presence
            ):
                raise ConfigConflictError(
                    "Gateway source journal event_id 已绑定不同 source 记录"
                )
            return int(existing[1])
        latest = connection.execute(
            """
            SELECT source_generation, presence, layer_digest
            FROM config_source_journal
            WHERE source_key = ?
            ORDER BY source_generation DESC
            LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        current_generation = int(latest[0]) if latest is not None else 0
        if (
            expected_source_generation is not None
            and current_generation != expected_source_generation
        ):
            raise ConfigConflictError("Gateway source journal generation CAS 冲突")
        if (
            latest is not None
            and str(latest[1]) == presence
            and (str(latest[2]) if latest[2] is not None else None) == layer_digest
        ):
            return current_generation
        generation = current_generation + 1
        owner = connection.execute(
            "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if owner is not None and int(owner[0]) != generation:
            raise ConfigConflictError("Gateway source owner generation CAS 冲突")
        if owner is None:
            connection.execute(
                "INSERT INTO config_source_owner(source_key, next_generation) VALUES (?, ?)",
                (source_key, generation + 1),
            )
        else:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = ? WHERE source_key = ?",
                (generation + 1, source_key),
            )
        connection.execute(
            """
            INSERT INTO config_source_journal(
                source_key, source_generation, source_event_id, source_path,
                presence, layer_revision, layer_digest, previous_digest,
                origin, fanout_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                generation,
                source_event_id,
                str(source_path.expanduser().resolve()),
                presence,
                layer_revision,
                layer_digest,
                previous_digest,
                origin,
                fanout_id,
                utc_now_text(),
            ),
        )
        return generation

    def append_config_source_journal(
        self,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> ConfigSourceJournalRecord:
        if presence not in {"present", "absent"}:
            raise ValueError("Gateway source journal presence 无效")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal WHERE source_event_id = ?
                """,
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._source_journal_from_row(existing)
            latest = connection.execute(
                """
                SELECT source_generation, presence, layer_digest
                FROM config_source_journal WHERE source_key = ?
                ORDER BY source_generation DESC LIMIT 1
                """,
                (source_key,),
            ).fetchone()
            current_generation = int(latest[0]) if latest is not None else 0
            if (
                expected_source_generation is not None
                and current_generation != expected_source_generation
            ):
                raise ConfigConflictError("Gateway source journal generation CAS 冲突")
            if (
                latest is not None
                and str(latest[1]) == presence
                and (str(latest[2]) if latest[2] is not None else None) == layer_digest
            ):
                existing = connection.execute(
                    """
                    SELECT source_key, source_generation, source_event_id, source_path,
                           presence, layer_revision, layer_digest, previous_digest,
                           origin, fanout_id, created_at
                    FROM config_source_journal
                    WHERE source_key = ? AND source_generation = ?
                    """,
                    (source_key, current_generation),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Gateway source journal 去重后无法读取")
                connection.execute("COMMIT")
                return self._source_journal_from_row(existing)
            generation = current_generation + 1
            owner = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if owner is not None and int(owner[0]) != generation:
                raise ConfigConflictError("Gateway source owner generation CAS 冲突")
            if owner is None:
                connection.execute(
                    "INSERT INTO config_source_owner(source_key, next_generation) VALUES (?, ?)",
                    (source_key, generation + 1),
                )
            else:
                connection.execute(
                    "UPDATE config_source_owner SET next_generation = ? WHERE source_key = ?",
                    (generation + 1, source_key),
                )
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO config_source_journal(
                    source_key, source_generation, source_event_id, source_path,
                    presence, layer_revision, layer_digest, previous_digest,
                    origin, fanout_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    generation,
                    source_event_id,
                    str(source_path.expanduser().resolve()),
                    presence,
                    layer_revision,
                    layer_digest,
                    previous_digest,
                    origin,
                    fanout_id,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        row = self._latest_source_journal_row(source_key, generation)
        if row is None:
            raise RuntimeError("Gateway source journal 提交后无法读取")
        return self._source_journal_from_row(row)

    @staticmethod
    def _source_journal_from_row(row: sqlite3.Row) -> ConfigSourceJournalRecord:
        return ConfigSourceJournalRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            source_event_id=str(row[2]),
            source_path=str(row[3]),
            presence=cast(str, row[4]),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            previous_digest=str(row[7]) if row[7] is not None else None,
            origin=str(row[8]),
            fanout_id=str(row[9]),
            created_at=datetime.fromisoformat(str(row[10])),
        )

    def _latest_source_journal_row(self, source_key: str, generation: int):
        connection = self._database.connection()
        try:
            return connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, generation),
            ).fetchone()
        finally:
            connection.close()

    def source_generation_high_water_mark(self, *, source_key: str) -> int:
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) - 1 if row is not None else 0

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
                registry_row = connection.execute(
                    "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
                ).fetchone()
                actual_registry_revision = (
                    int(registry_row[0]) if registry_row is not None else 0
                )
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
                    SELECT config_key, source_path, presence, layer_revision,
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
                    if (
                        str(detail.get("path")) != str(row[1])
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

    def get_config_apply_claim(
        self,
        *,
        config_domain: str,
    ) -> ConfigApplyClaimRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, candidate_id, attempt_id, apply_id, owner,
                       base_active_revision, target_generation, lease_expires_at,
                       fencing_token, updated_at
                FROM config_apply_claim WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ConfigApplyClaimRecord(
            config_domain=str(row[0]),
            candidate_id=str(row[1]),
            attempt_id=str(row[2]),
            apply_id=str(row[3]),
            owner=str(row[4]),
            base_active_revision=int(row[5]) if row[5] is not None else None,
            target_generation=str(row[6]) if row[6] is not None else None,
            lease_expires_at=datetime.fromisoformat(str(row[7])),
            fencing_token=str(row[8]),
            updated_at=datetime.fromisoformat(str(row[9])),
        )

    def assert_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
    ) -> None:
        """在外部副作用前确认 claim 仍由当前 attempt 持有。"""

        claim = self.get_config_apply_claim(config_domain=config_domain)
        if (
            claim is None
            or claim.apply_id != apply_id
            or claim.fencing_token != fencing_token
            or claim.lease_expires_at <= datetime.now(UTC)
        ):
            raise ConfigConflictError(
                "Gateway 配置 apply claim 已失效或 fencing 不匹配: "
                f"domain={config_domain}, apply_id={apply_id}"
            )

    @staticmethod
    def _apply_journal_from_row(row: sqlite3.Row) -> ConfigApplyJournalRecord:
        side_effects = json.loads(str(row[10]))
        if not isinstance(side_effects, list) or not all(
            isinstance(item, dict) for item in side_effects
        ):
            raise TypeError("Gateway apply journal side_effects 结构无效")
        return ConfigApplyJournalRecord(
            config_domain=str(row[0]),
            apply_id=str(row[1]),
            candidate_id=str(row[2]),
            attempt_id=str(row[3]),
            owner=str(row[4]),
            base_active_revision=(int(row[5]) if row[5] is not None else None),
            pending_revision=(int(row[6]) if row[6] is not None else None),
            source_baseline=load_json_object(
                str(row[7]), field="Gateway apply journal source baseline"
            ),
            active_baseline=load_json_object(
                str(row[8]), field="Gateway apply journal active baseline"
            ),
            registry_revision=(int(row[9]) if row[9] is not None else None),
            side_effects=tuple(cast(dict[str, object], item) for item in side_effects),
            state=cast(str, row[11]),
            last_error=str(row[12]) if row[12] is not None else None,
            created_at=datetime.fromisoformat(str(row[13])),
            updated_at=datetime.fromisoformat(str(row[14])),
        )

    def get_config_apply_journal(
        self,
        *,
        apply_id: str,
    ) -> ConfigApplyJournalRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, apply_id, candidate_id, attempt_id, owner,
                       base_active_revision, pending_revision, source_baseline_json,
                       active_baseline_json, registry_revision, side_effects_json,
                       state, last_error, created_at, updated_at
                FROM config_apply_journal WHERE apply_id = ?
                """,
                (apply_id,),
            ).fetchone()
        finally:
            connection.close()
        return self._apply_journal_from_row(row) if row is not None else None

    def list_config_apply_journals(
        self,
        *,
        config_domain: str,
        states: tuple[str, ...] = (),
    ) -> tuple[ConfigApplyJournalRecord, ...]:
        query = """
            SELECT config_domain, apply_id, candidate_id, attempt_id, owner,
                   base_active_revision, pending_revision, source_baseline_json,
                   active_baseline_json, registry_revision, side_effects_json,
                   state, last_error, created_at, updated_at
            FROM config_apply_journal WHERE config_domain = ?
        """
        params: list[object] = [config_domain]
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" AND state IN ({placeholders})"
            params.extend(states)
        query += " ORDER BY updated_at ASC, apply_id ASC"
        connection = self._database.connection()
        try:
            rows = connection.execute(query, tuple(params)).fetchall()
        finally:
            connection.close()
        return tuple(self._apply_journal_from_row(row) for row in rows)

    def start_config_apply_journal(
        self,
        *,
        config_domain: str,
        apply_id: str,
        candidate_id: str,
        attempt_id: str,
        owner: str,
        base_active_revision: int | None,
        pending_revision: int | None,
        source_baseline: dict[str, object],
        active_baseline: dict[str, object],
        registry_revision: int | None = None,
    ) -> ConfigApplyJournalRecord:
        if not all((config_domain, apply_id, candidate_id, attempt_id, owner)):
            raise ValueError("Gateway apply journal 身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT apply_id FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if existing is None:
                now = utc_now_text()
                connection.execute(
                    """
                    INSERT INTO config_apply_journal(
                        config_domain, apply_id, candidate_id, attempt_id, owner,
                        base_active_revision, pending_revision, source_baseline_json,
                        active_baseline_json, registry_revision, side_effects_json,
                        state, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'applying', NULL, ?, ?)
                    """,
                    (
                        config_domain,
                        apply_id,
                        candidate_id,
                        attempt_id,
                        owner,
                        base_active_revision,
                        pending_revision,
                        dump_json(source_baseline),
                        dump_json(active_baseline),
                        registry_revision,
                        now,
                        now,
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Gateway apply journal 提交后无法读取")
        return result

    def update_config_apply_journal(
        self,
        *,
        apply_id: str,
        expected_state: str,
        state: str,
        side_effects: tuple[dict[str, object], ...] | None = None,
        last_error: str | None = None,
    ) -> ConfigApplyJournalRecord:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_apply_journal
                SET state = ?, last_error = ?, side_effects_json = COALESCE(?, side_effects_json),
                    updated_at = ?
                WHERE apply_id = ? AND state = ?
                """,
                (
                    state,
                    last_error,
                    dump_json(list(side_effects)) if side_effects is not None else None,
                    utc_now_text(),
                    apply_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway apply journal 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Gateway apply journal 更新后无法读取")
        return result

    def append_config_apply_side_effect(
        self,
        *,
        apply_id: str,
        side_effect: dict[str, object],
        expected_state: str = "applying",
    ) -> ConfigApplyJournalRecord:
        """在 Gateway journal 中幂等追加一个已观测的外部副作用。"""

        if not side_effect:
            raise ValueError("Gateway apply journal 副作用记录不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None or str(row[0]) != expected_state:
                raise ConfigConflictError(
                    "Gateway apply journal 副作用追加状态 CAS 失败"
                )
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Gateway apply journal side_effects 结构无效")
            if side_effect not in side_effects:
                side_effects.append(side_effect)
            cursor = connection.execute(
                """
                UPDATE config_apply_journal
                SET side_effects_json = ?, updated_at = ?
                WHERE apply_id = ? AND state = ?
                """,
                (dump_json(side_effects), utc_now_text(), apply_id, expected_state),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway apply journal 副作用追加状态 CAS 失败"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Gateway apply journal 副作用提交后无法读取")
        return result

    def record_config_apply_compensation(
        self,
        *,
        apply_id: str,
        compensation: dict[str, object],
        expected_state: str = "recovery_required",
    ) -> ConfigApplyJournalRecord:
        """记录外部副作用的补偿结果；不会假装 SQLite 能回滚外部资源。"""

        allowed_fields = {"resource", "action", "status", "error", "detail"}
        if not compensation or not set(compensation).issubset(allowed_fields):
            raise ValueError("Gateway 补偿记录只能包含资源、动作、状态和错误摘要")
        status = compensation.get("status")
        if status not in {"succeeded", "failed"}:
            raise ValueError("Gateway 补偿记录 status 必须是 succeeded 或 failed")
        if not isinstance(compensation.get("resource"), str) or not isinstance(
            compensation.get("action"), str
        ):
            raise ValueError("Gateway 补偿记录必须包含 resource 和 action")
        entry = {"phase": "compensation", **compensation}
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError("Gateway 补偿记录关联的 apply journal 不存在")
            current_state = str(row[0])
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Gateway apply journal side_effects 结构无效")
            if current_state == "compensated" and entry in side_effects:
                connection.execute("COMMIT")
            else:
                if current_state != expected_state:
                    raise ConfigConflictError(
                        "Gateway 补偿记录状态 CAS 失败: "
                        f"state={current_state}, expected={expected_state}"
                    )
                if entry not in side_effects:
                    side_effects.append(entry)
                next_state = (
                    "compensated" if status == "succeeded" else "recovery_required"
                )
                last_error = (
                    None
                    if status == "succeeded"
                    else str(compensation.get("error") or "外部副作用补偿失败")
                )
                cursor = connection.execute(
                    """
                    UPDATE config_apply_journal
                    SET state = ?, side_effects_json = ?, last_error = ?, updated_at = ?
                    WHERE apply_id = ? AND state = ?
                    """,
                    (
                        next_state,
                        dump_json(side_effects),
                        last_error,
                        utc_now_text(),
                        apply_id,
                        expected_state,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConfigConflictError("Gateway 补偿记录状态 CAS 失败")
                connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Gateway 补偿记录提交后无法读取")
        return result

    def recover_config_apply_journals(
        self,
        *,
        config_domain: str,
    ) -> tuple[ConfigApplyJournalRecord, ...]:
        journals = self.list_config_apply_journals(
            config_domain=config_domain,
            states=("applying",),
        )
        for journal in journals:
            self.update_config_apply_journal(
                apply_id=journal.apply_id,
                expected_state="applying",
                state="recovery_required",
                last_error="进程恢复发现未完成的外部 apply journal",
            )
        return self.list_config_apply_journals(
            config_domain=config_domain,
            states=("recovery_required",),
        )

    @staticmethod
    def _runtime_generation_from_row(
        row: sqlite3.Row,
    ) -> GatewayRuntimeGenerationRecord:
        proof = (
            load_json_object(str(row[13]), field="Gateway runtime health proof")
            if row[13] is not None
            else None
        )
        return GatewayRuntimeGenerationRecord(
            config_domain=str(row[0]),
            generation_id=str(row[1]),
            process_id=int(row[2]) if row[2] is not None else None,
            loaded_source=cast(str, row[3]),
            candidate_id=str(row[4]) if row[4] is not None else None,
            active_revision=int(row[5]) if row[5] is not None else None,
            pending_revision=int(row[6]) if row[6] is not None else None,
            candidate_digest=str(row[7]) if row[7] is not None else None,
            effective_digest=str(row[8]),
            secret_binding_digest=(str(row[9]) if row[9] is not None else None),
            fencing_token=str(row[10]) if row[10] is not None else None,
            listener_state=cast(str, row[11]),
            state=cast(str, row[12]),
            health_proof=proof,
            created_at=datetime.fromisoformat(str(row[14])),
            updated_at=datetime.fromisoformat(str(row[15])),
        )

    def get_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
    ) -> GatewayRuntimeGenerationRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, generation_id, process_id, loaded_source,
                       candidate_id, active_revision, pending_revision,
                       candidate_digest, effective_digest, secret_binding_digest,
                       fencing_token, listener_state, state, health_proof_json,
                       created_at, updated_at
                FROM gateway_runtime_generation WHERE generation_id = ?
                """,
                (generation_id,),
            ).fetchone()
        finally:
            connection.close()
        return self._runtime_generation_from_row(row) if row is not None else None

    def record_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        process_id: int | None,
        loaded_source: str,
        candidate_id: str | None,
        active_revision: int | None,
        pending_revision: int | None,
        candidate_digest: str | None,
        effective_digest: str,
        secret_binding_digest: str | None,
        fencing_token: str | None,
        listener_state: str,
        state: str,
        health_proof: dict[str, object] | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        if loaded_source not in {"active", "pending"}:
            raise ValueError("Gateway runtime loaded_source 无效")
        if listener_state not in {"reserved", "serving", "draining", "closed"}:
            raise ValueError("Gateway runtime listener_state 无效")
        if state not in {"starting", "healthy", "active", "failed", "closed"}:
            raise ValueError("Gateway runtime generation state 无效")
        if not generation_id or not effective_digest:
            raise ValueError("Gateway runtime generation 身份不能为空")
        now = utc_now_text()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO gateway_runtime_generation(
                    config_domain, generation_id, process_id, loaded_source,
                    candidate_id, active_revision, pending_revision, candidate_digest,
                    effective_digest, secret_binding_digest, fencing_token,
                    listener_state, state, health_proof_json, created_at, updated_at
                ) VALUES ('gateway', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(generation_id) DO UPDATE SET
                    process_id=excluded.process_id,
                    loaded_source=excluded.loaded_source,
                    candidate_id=excluded.candidate_id,
                    active_revision=excluded.active_revision,
                    pending_revision=excluded.pending_revision,
                    candidate_digest=excluded.candidate_digest,
                    effective_digest=excluded.effective_digest,
                    secret_binding_digest=excluded.secret_binding_digest,
                    fencing_token=excluded.fencing_token,
                    listener_state=excluded.listener_state,
                    state=excluded.state,
                    health_proof_json=excluded.health_proof_json,
                    updated_at=excluded.updated_at
                """,
                (
                    generation_id,
                    process_id,
                    loaded_source,
                    candidate_id,
                    active_revision,
                    pending_revision,
                    candidate_digest,
                    effective_digest,
                    secret_binding_digest,
                    fencing_token,
                    listener_state,
                    state,
                    dump_json(health_proof) if health_proof is not None else None,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime generation 提交后无法读取")
        return result

    def update_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_state: str,
        state: str,
        listener_state: str,
        health_proof: dict[str, object] | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        if state not in {"starting", "healthy", "active", "failed", "closed"}:
            raise ValueError("Gateway runtime generation state 无效")
        if listener_state not in {"reserved", "serving", "draining", "closed"}:
            raise ValueError("Gateway runtime listener_state 无效")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = ?, listener_state = ?, health_proof_json = COALESCE(?, health_proof_json),
                    updated_at = ?
                WHERE generation_id = ? AND state = ?
                """,
                (
                    state,
                    listener_state,
                    dump_json(health_proof) if health_proof is not None else None,
                    utc_now_text(),
                    generation_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway runtime generation 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime generation 更新后无法读取")
        return result

    def active_gateway_runtime_generation(
        self,
    ) -> GatewayRuntimeGenerationRecord | None:
        """返回最近一次完成 serving handoff 的 Gateway generation。"""

        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_domain, generation_id, process_id, loaded_source,
                       candidate_id, active_revision, pending_revision,
                       candidate_digest, effective_digest, secret_binding_digest,
                       fencing_token, listener_state, state, health_proof_json,
                       created_at, updated_at
                FROM gateway_runtime_generation
                WHERE config_domain = 'gateway'
                  AND state = 'active' AND listener_state = 'serving'
                ORDER BY updated_at DESC, generation_id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        return self._runtime_generation_from_row(row) if row is not None else None

    def handoff_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_old_generation: str | None,
        fencing_token: str,
    ) -> GatewayRuntimeGenerationRecord:
        """以 fencing CAS 将健康新 generation 切到 serving，并标记旧 generation 排空。"""

        if not generation_id or not fencing_token:
            raise ValueError("Gateway runtime handoff 缺少 generation 或 fencing token")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            new_row = connection.execute(
                """
                SELECT state, listener_state, fencing_token
                FROM gateway_runtime_generation
                WHERE generation_id = ? AND config_domain = 'gateway'
                """,
                (generation_id,),
            ).fetchone()
            if new_row is None:
                raise ConfigConflictError(
                    f"Gateway runtime handoff 找不到新 generation: {generation_id}"
                )
            if str(new_row[2]) != fencing_token:
                raise ConfigConflictError(
                    "Gateway runtime handoff fencing token 不匹配"
                )
            if str(new_row[0]) != "healthy" or str(new_row[1]) != "reserved":
                raise ConfigConflictError(
                    "Gateway runtime handoff 要求新 generation 为 healthy/reserved"
                )
            if expected_old_generation and expected_old_generation != generation_id:
                old_row = connection.execute(
                    """
                    SELECT state, listener_state
                    FROM gateway_runtime_generation
                    WHERE generation_id = ? AND config_domain = 'gateway'
                    """,
                    (expected_old_generation,),
                ).fetchone()
                if old_row is not None and str(old_row[0]) == "active":
                    if str(old_row[1]) != "serving":
                        raise ConfigConflictError(
                            "Gateway 旧 generation 不是 serving，不能执行 handoff"
                        )
                    cursor = connection.execute(
                        """
                        UPDATE gateway_runtime_generation
                        SET listener_state = 'draining', updated_at = ?
                        WHERE generation_id = ? AND state = 'active'
                          AND listener_state = 'serving'
                        """,
                        (utc_now_text(), expected_old_generation),
                    )
                    if cursor.rowcount != 1:
                        raise ConfigConflictError("Gateway 旧 generation 排空 CAS 失败")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'active', listener_state = 'serving', updated_at = ?
                WHERE generation_id = ? AND state = 'healthy'
                  AND listener_state = 'reserved' AND fencing_token = ?
                """,
                (utc_now_text(), generation_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 新 generation serving CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime handoff 后新 generation 消失")
        return result

    def rollback_gateway_runtime_handoff(
        self,
        *,
        generation_id: str,
        old_generation_id: str,
        fencing_token: str,
    ) -> tuple[GatewayRuntimeGenerationRecord, GatewayRuntimeGenerationRecord]:
        """新 generation 失败时恢复仍处于 draining 的旧 generation。"""

        if not all((generation_id, old_generation_id, fencing_token)):
            raise ValueError(
                "Gateway runtime rollback 缺少 generation 或 fencing token"
            )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            new_cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'failed', listener_state = 'closed', updated_at = ?
                WHERE generation_id = ? AND state = 'active'
                  AND listener_state = 'serving' AND fencing_token = ?
                """,
                (utc_now_text(), generation_id, fencing_token),
            )
            if new_cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 新 generation rollback CAS 失败")
            old_cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET listener_state = 'serving', updated_at = ?
                WHERE generation_id = ? AND state = 'active'
                  AND listener_state = 'draining'
                """,
                (utc_now_text(), old_generation_id),
            )
            if old_cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 旧 generation 已无法恢复，必须进入 recovery_required"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        new_record = self.get_gateway_runtime_generation(generation_id=generation_id)
        old_record = self.get_gateway_runtime_generation(
            generation_id=old_generation_id
        )
        if new_record is None or old_record is None:
            raise RuntimeError("Gateway runtime rollback 后 generation 记录不完整")
        return new_record, old_record

    def close_gateway_runtime_generation(
        self,
        *,
        generation_id: str,
        expected_states: tuple[str, ...] = ("starting", "healthy", "active"),
        fencing_token: str | None = None,
    ) -> GatewayRuntimeGenerationRecord:
        """幂等关闭已排空或已失败的 Gateway generation。"""

        if not generation_id or not expected_states:
            raise ValueError("Gateway runtime close 参数不能为空")
        placeholders = ", ".join("?" for _ in expected_states)
        params: list[object] = [utc_now_text(), generation_id, *expected_states]
        fencing_clause = ""
        if fencing_token is not None:
            fencing_clause = " AND fencing_token = ?"
            params.append(fencing_token)
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_runtime_generation
                SET state = 'closed', listener_state = 'closed', updated_at = ?
                WHERE generation_id = ? AND state IN ("""
                + placeholders
                + ")"
                + fencing_clause,
                params,
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway runtime close 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_gateway_runtime_generation(generation_id=generation_id)
        if result is None:
            raise RuntimeError("Gateway runtime close 后 generation 消失")
        return result

    def begin_config_apply(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        attempt_id: str,
        apply_id: str,
        owner: str,
        base_active_revision: int | None,
        target_generation: str | None,
        pending_revision: int,
        source_baseline: dict[str, object],
        active_baseline: dict[str, object],
        expected_candidate_state: ConfigLifecycleState = "candidate_validated",
        registry_revision: int | None = None,
        lease_seconds: float = 30,
    ) -> ConfigApplyClaimRecord:
        """原子创建 Gateway claim、apply journal 并推进候选到 applying。"""

        if lease_seconds <= 0:
            raise ValueError("Gateway 配置 apply lease 必须大于 0 秒")
        validate_state_transition(expected_candidate_state, "applying")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("Gateway apply 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                """
                SELECT pending_revision, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (config_domain, candidate_id),
            ).fetchone()
            if (
                pending is None
                or int(pending[0]) != pending_revision
                or str(pending[1]) != expected_candidate_state
            ):
                raise ConfigConflictError("Gateway begin apply 的候选状态 CAS 失败")
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError(
                    "Gateway begin apply 与候选 active 基线不一致"
                )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "Gateway begin apply 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            if registry_revision is not None:
                registry_row = connection.execute(
                    "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
                ).fetchone()
                current_registry_revision = (
                    int(registry_row[0]) if registry_row is not None else 0
                )
                if current_registry_revision != registry_revision:
                    raise ConfigConflictError(
                        "Gateway begin apply 的 registry revision 基线已变化: "
                        f"current={current_registry_revision}, "
                        f"expected={registry_revision}"
                    )
            existing = connection.execute(
                """
                SELECT attempt_id, apply_id, lease_expires_at, fencing_token
                FROM config_apply_claim WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            now = datetime.now(UTC)
            if existing is not None:
                same_apply = str(existing[1]) == apply_id
                if not same_apply and datetime.fromisoformat(str(existing[2])) > now:
                    raise ConfigConflictError(
                        "Gateway 配置 apply claim 仍由其他持有者租用: "
                        f"apply_id={existing[1]}"
                    )
            fencing_token = (
                str(existing[3])
                if existing is not None and str(existing[1]) == apply_id
                else new_config_id("fence")
            )
            connection.execute(
                """
                INSERT INTO config_apply_claim(
                    config_domain, candidate_id, attempt_id, apply_id, owner,
                    base_active_revision, target_generation, lease_expires_at,
                    fencing_token, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_domain) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    attempt_id=excluded.attempt_id,
                    apply_id=excluded.apply_id,
                    owner=excluded.owner,
                    base_active_revision=excluded.base_active_revision,
                    target_generation=excluded.target_generation,
                    lease_expires_at=excluded.lease_expires_at,
                    fencing_token=excluded.fencing_token,
                    updated_at=excluded.updated_at
                """,
                (
                    config_domain,
                    candidate_id,
                    attempt_id,
                    apply_id,
                    owner,
                    base_active_revision,
                    target_generation,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    fencing_token,
                    now.isoformat(),
                ),
            )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'applying', fencing_token = ?,
                    last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ? AND state = ?
                """,
                (
                    fencing_token,
                    attempt_id,
                    apply_id,
                    config_domain,
                    candidate_id,
                    expected_candidate_state,
                ),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Gateway begin apply 的候选状态 CAS 失败")
            journal = connection.execute(
                "SELECT candidate_id, attempt_id, owner, pending_revision, source_baseline_json, active_baseline_json "
                "FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if journal is None:
                now_text = now.isoformat()
                connection.execute(
                    """
                    INSERT INTO config_apply_journal(
                        config_domain, apply_id, candidate_id, attempt_id, owner,
                        base_active_revision, pending_revision, source_baseline_json,
                        active_baseline_json, registry_revision, side_effects_json,
                        state, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'applying', NULL, ?, ?)
                    """,
                    (
                        config_domain,
                        apply_id,
                        candidate_id,
                        attempt_id,
                        owner,
                        base_active_revision,
                        pending_revision,
                        dump_json(source_baseline),
                        dump_json(active_baseline),
                        registry_revision,
                        now_text,
                        now_text,
                    ),
                )
            elif (
                str(journal[0]) != candidate_id
                or str(journal[1]) != attempt_id
                or str(journal[2]) != owner
                or int(journal[3]) != pending_revision
                or str(journal[4]) != dump_json(source_baseline)
                or str(journal[5]) != dump_json(active_baseline)
            ):
                raise ConfigConflictError("Gateway begin apply 的 journal 身份不一致")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Gateway begin apply 提交后缺少 claim")
        return result

    def acquire_config_apply_claim(
        self,
        *,
        config_domain: str,
        candidate_id: str,
        attempt_id: str,
        apply_id: str,
        owner: str,
        base_active_revision: int | None,
        target_generation: str | None,
        lease_seconds: float = 30,
        fencing_token: str | None = None,
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("Gateway 配置 apply lease 必须大于 0 秒")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("Gateway 配置 apply claim 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            existing = connection.execute(
                """
                SELECT apply_id, lease_expires_at, fencing_token
                FROM config_apply_claim WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            pending = connection.execute(
                """
                SELECT pending_revision, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (config_domain, candidate_id),
            ).fetchone()
            if pending is None or str(pending[1]) not in {
                "candidate_validated",
                "pending_restart",
            }:
                raise ConfigConflictError(
                    "Gateway 配置 apply claim 只能绑定可应用的候选: "
                    f"domain={config_domain}, candidate={candidate_id}"
                )
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError(
                    "Gateway apply claim 与候选 active 基线不一致"
                )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "Gateway apply claim 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            if existing is not None:
                same_apply = str(existing[0]) == apply_id
                if not same_apply and datetime.fromisoformat(str(existing[1])) > now:
                    raise ConfigConflictError(
                        "Gateway 配置 apply claim 仍由其他持有者租用: "
                        f"domain={config_domain}, apply_id={existing[0]}"
                    )
            resolved_fencing_token = (
                fencing_token
                if fencing_token is not None
                else (
                    str(existing[2])
                    if existing is not None and str(existing[0]) == apply_id
                    else new_config_id("fence")
                )
            )
            connection.execute(
                """
                INSERT INTO config_apply_claim(
                    config_domain, candidate_id, attempt_id, apply_id, owner,
                    base_active_revision, target_generation, lease_expires_at,
                    fencing_token, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_domain) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    attempt_id=excluded.attempt_id,
                    apply_id=excluded.apply_id,
                    owner=excluded.owner,
                    base_active_revision=excluded.base_active_revision,
                    target_generation=excluded.target_generation,
                    lease_expires_at=excluded.lease_expires_at,
                    fencing_token=excluded.fencing_token,
                    updated_at=excluded.updated_at
                """,
                (
                    config_domain,
                    candidate_id,
                    attempt_id,
                    apply_id,
                    owner,
                    base_active_revision,
                    target_generation,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    resolved_fencing_token,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET fencing_token = ?, last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (
                    resolved_fencing_token,
                    attempt_id,
                    apply_id,
                    config_domain,
                    candidate_id,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Gateway 配置 apply claim 提交后无法读取")
        return result

    def renew_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
        lease_seconds: float = 30,
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("Gateway 配置 apply lease 必须大于 0 秒")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            cursor = connection.execute(
                """
                UPDATE config_apply_claim
                SET lease_expires_at = ?, updated_at = ?
                WHERE config_domain = ? AND apply_id = ? AND fencing_token = ?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    config_domain,
                    apply_id,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError("Gateway 配置 apply claim fencing 校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Gateway 配置 apply claim 更新后无法读取")
        return result

    def release_config_apply_claim(
        self,
        *,
        config_domain: str,
        apply_id: str,
        fencing_token: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM config_apply_claim
                WHERE config_domain = ? AND apply_id = ? AND fencing_token = ?
                """,
                (config_domain, apply_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 配置 apply claim 释放 fencing 校验失败"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_config_applies(self, *, config_domain: str) -> tuple[str, ...]:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            rows = connection.execute(
                """
                SELECT candidate_id FROM config_pending_candidate
                WHERE config_domain = ? AND state = 'applying'
                  AND candidate_id NOT IN (
                      SELECT candidate_id FROM config_apply_claim
                      WHERE config_domain = ? AND lease_expires_at > ?
                  )
                """,
                (config_domain, config_domain, now),
            ).fetchall()
            candidate_ids = tuple(str(row[0]) for row in rows)
            connection.executemany(
                """
                UPDATE config_pending_candidate
                SET state = 'recovery_required',
                    last_error = 'Gateway 启动恢复发现 apply lease 已过期'
                WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                """,
                ((config_domain, candidate_id) for candidate_id in candidate_ids),
            )
            if config_domain == "gateway" and candidate_ids:
                connection.executemany(
                    """
                    UPDATE gateway_restart_intent
                    SET state = 'recovery_required',
                        last_error = 'Gateway 启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE candidate_id = ? AND state = 'applying'
                    """,
                    ((utc_now_text(), candidate_id) for candidate_id in candidate_ids),
                )
                connection.executemany(
                    """
                    UPDATE config_apply_journal
                    SET state = 'recovery_required',
                        last_error = 'Gateway 启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    (
                        (utc_now_text(), config_domain, candidate_id)
                        for candidate_id in candidate_ids
                    ),
                )
            connection.execute("COMMIT")
            return candidate_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                SELECT config_key, source_path, presence, layer_revision,
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
                if (
                    str(detail.get("path")) != str(row[1])
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
                registry_row = connection.execute(
                    "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
                ).fetchone()
                registry_revision = (
                    int(registry_row[0]) if registry_row is not None else 0
                )
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

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ConfigEventRecord:
        def paths(value: object) -> tuple[str, ...]:
            parsed = json.loads(str(value))
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError("Gateway 配置事件路径无效")
            return tuple(parsed)

        def optional_datetime(value: object) -> datetime | None:
            return datetime.fromisoformat(str(value)) if value is not None else None

        return ConfigEventRecord(
            event_seq=int(row[0]),
            event_id=str(row[1]),
            config_domain=str(row[2]),
            candidate_id=str(row[3]) if row[3] is not None else None,
            attempt_id=str(row[4]) if row[4] is not None else None,
            apply_id=str(row[5]) if row[5] is not None else None,
            idempotency_key=str(row[6]) if row[6] is not None else None,
            commit_revision=int(row[7]) if row[7] is not None else None,
            active_revision=int(row[8]) if row[8] is not None else None,
            pending_revision=int(row[9]) if row[9] is not None else None,
            source=str(row[10]),
            result=cast(ConfigResult, str(row[11])),
            activation_scope=cast(str, str(row[12])),
            changed_paths=paths(row[13]),
            applied_paths=paths(row[14]),
            deferred_paths=paths(row[15]),
            error=str(row[16]) if row[16] is not None else None,
            occurred_at=datetime.fromisoformat(str(row[17])),
            relay_state=cast(ConfigEventRelayState, str(row[18])),
            relay_attempts=int(row[19]),
            relay_last_error=str(row[20]) if row[20] is not None else None,
            relay_claimed_by=str(row[21]) if row[21] is not None else None,
            relay_claimed_until=optional_datetime(row[22]),
            relay_next_attempt_at=optional_datetime(row[23]),
        )

    @staticmethod
    def _select_config_event(connection, event_id: str):
        return connection.execute(
            """
            SELECT event_seq, event_id, config_domain, candidate_id, attempt_id,
                   apply_id, idempotency_key, commit_revision, active_revision,
                   pending_revision, source, result, activation_scope,
                   changed_paths_json, applied_paths_json, deferred_paths_json,
                   error, occurred_at, relay_state, relay_attempts,
                   relay_last_error, relay_claimed_by, relay_claimed_until,
                   relay_next_attempt_at
            FROM config_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()

    @classmethod
    def _insert_config_event(
        cls,
        connection: sqlite3.Connection,
        event: ConfigEventInput,
    ) -> ConfigEventRecord:
        existing = connection.execute(
            """
            SELECT event_id
            FROM config_events
            WHERE event_id = ?
               OR (
                    config_domain = ? AND idempotency_key = ? AND result = ?
                    AND ? IS NOT NULL
               )
            ORDER BY event_seq ASC
            LIMIT 1
            """,
            (
                event.event_id,
                event.config_domain,
                event.idempotency_key,
                event.result,
                event.idempotency_key,
            ),
        ).fetchone()
        if existing is not None:
            row = cls._select_config_event(connection, str(existing[0]))
            if row is None:
                raise RuntimeError("Gateway 配置事件幂等记录读取失败")
            return cls._event_from_row(row)
        connection.execute(
            """
            INSERT INTO config_events(
                event_id, config_domain, candidate_id, attempt_id, apply_id,
                idempotency_key, commit_revision, active_revision, pending_revision,
                source, result, activation_scope, changed_paths_json,
                applied_paths_json, deferred_paths_json, error, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.config_domain,
                event.candidate_id,
                event.attempt_id,
                event.apply_id,
                event.idempotency_key,
                event.commit_revision,
                event.active_revision,
                event.pending_revision,
                event.source,
                event.result,
                event.activation_scope,
                dump_json(list(event.changed_paths)),
                dump_json(list(event.applied_paths)),
                dump_json(list(event.deferred_paths)),
                event.error,
                utc_now_text(),
            ),
        )
        row = cls._select_config_event(connection, event.event_id)
        if row is None:
            raise RuntimeError("Gateway 配置事件提交后无法读取")
        return cls._event_from_row(row)

    def append_config_event(self, event: ConfigEventInput) -> ConfigEventRecord:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._insert_config_event(connection, event)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_config_events_for_relay(
        self,
        *,
        config_domain: str,
        limit: int = 100,
    ) -> tuple[ConfigEventRecord, ...]:
        """返回尚未确认或已到重试时间的 outbox 事件。"""
        if limit < 1 or limit > 2000:
            raise ValueError("配置 outbox relay 分页参数无效")
        now = utc_now_text()
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT event_seq, event_id, config_domain, candidate_id, attempt_id,
                       apply_id, idempotency_key, commit_revision, active_revision,
                       pending_revision, source, result, activation_scope,
                       changed_paths_json, applied_paths_json, deferred_paths_json,
                       error, occurred_at, relay_state, relay_attempts,
                       relay_last_error, relay_claimed_by, relay_claimed_until,
                       relay_next_attempt_at
                FROM config_events
                WHERE config_domain = ? AND (
                    relay_state = 'pending'
                    OR (relay_state = 'failed' AND relay_next_attempt_at <= ?)
                    OR (relay_state = 'claimed' AND relay_claimed_until <= ?)
                )
                ORDER BY event_seq ASC
                LIMIT ?
                """,
                (config_domain, now, now, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def claim_config_events_for_consumer(
        self,
        *,
        config_domain: str,
        after: int,
        consumer_id: str,
        limit: int = 100,
        lease_seconds: float = 30.0,
    ) -> tuple[ConfigEventRecord, ...]:
        """按消费者独立 claim 事件，避免 SSE 消费者相互确认。"""
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Gateway 配置事件 relay 分页参数无效")
        if not consumer_id.strip():
            raise ValueError("Gateway 配置事件 consumer_id 不能为空")
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("Gateway 配置事件 relay lease 必须大于 0")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        claimed_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT e.event_seq, e.event_id, e.config_domain, e.candidate_id,
                       e.attempt_id, e.apply_id, e.idempotency_key,
                       e.commit_revision, e.active_revision, e.pending_revision,
                       e.source, e.result, e.activation_scope,
                       e.changed_paths_json, e.applied_paths_json,
                       e.deferred_paths_json, e.error, e.occurred_at,
                       e.relay_state, e.relay_attempts, e.relay_last_error,
                       e.relay_claimed_by, e.relay_claimed_until,
                       e.relay_next_attempt_at
                FROM config_events AS e
                LEFT JOIN config_event_relay_delivery AS d
                  ON d.event_id = e.event_id AND d.consumer_id = ?
                WHERE e.config_domain = ? AND e.event_seq > ? AND (
                    d.event_id IS NULL
                    OR (d.state = 'failed' AND d.next_attempt_at <= ?)
                    OR (d.state = 'claimed' AND d.claimed_until <= ?)
                )
                ORDER BY e.event_seq ASC
                LIMIT ?
                """,
                (consumer_id, config_domain, after, now_text, now_text, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO config_event_relay_delivery(
                        event_id, consumer_id, state, attempts, last_error,
                        claimed_until, next_attempt_at, updated_at
                    ) VALUES (?, ?, 'claimed', 1, NULL, ?, ?, ?)
                    ON CONFLICT(event_id, consumer_id) DO UPDATE SET
                        state = 'claimed', attempts = attempts + 1,
                        last_error = NULL, claimed_until = excluded.claimed_until,
                        next_attempt_at = excluded.next_attempt_at,
                        updated_at = excluded.updated_at
                    """,
                    (str(row[1]), consumer_id, claimed_until, now_text, now_text),
                )
            connection.execute("COMMIT")
            return tuple(self._event_from_row(row) for row in rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_config_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ) -> ConfigEventRecord:
        if not consumer_id.strip():
            raise ValueError("Gateway 配置事件 consumer_id 不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_event_relay_delivery
                SET state = 'delivered', last_error = NULL,
                    claimed_until = NULL, updated_at = ?
                WHERE event_id = ? AND consumer_id = ?
                  AND state IN ('claimed', 'delivered')
                """,
                (utc_now_text(), event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                raise ConfigConflictError("Gateway 配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"Gateway 配置事件不存在: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_config_event_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
        error: str,
        retry_after_seconds: float = 0.0,
    ) -> ConfigEventRecord:
        if not consumer_id.strip() or not error.strip():
            raise ValueError("Gateway 配置事件 relay consumer 和错误不能为空")
        if isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
            raise ValueError("Gateway 配置事件 relay 重试延迟不能为负数")
        next_attempt_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_event_relay_delivery
                SET state = 'failed', last_error = ?, claimed_until = NULL,
                    next_attempt_at = ?, updated_at = ?
                WHERE event_id = ? AND consumer_id = ? AND state = 'claimed'
                """,
                (error, next_attempt_at, utc_now_text(), event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                raise ConfigConflictError("Gateway 配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"Gateway 配置事件不存在: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_config_event_relay(
        self,
        *,
        event_id: str,
        consumer_id: str,
        lease_seconds: float = 30.0,
    ) -> ConfigEventRecord | None:
        """以租约 claim 单个 outbox 事件；过期 claim 可被恢复者接管。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("配置 outbox relay lease 必须大于 0")
        now = datetime.now(UTC)
        now_text = now.isoformat()
        claimed_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'claimed',
                    relay_attempts = relay_attempts + 1,
                    relay_last_error = NULL,
                    relay_claimed_by = ?,
                    relay_claimed_until = ?
                WHERE event_id = ? AND (
                    relay_state = 'pending'
                    OR (relay_state = 'failed' AND relay_next_attempt_at <= ?)
                    OR (relay_state = 'claimed' AND relay_claimed_until <= ?)
                )
                """,
                (consumer_id, claimed_until, event_id, now_text, now_text),
            )
            if cursor.rowcount != 1:
                connection.execute("COMMIT")
                return None
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise RuntimeError(f"配置 outbox claim 后事件消失: {event_id}")
            connection.execute("COMMIT")
            return self._event_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_config_event_relay_delivered(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ) -> ConfigEventRecord:
        """以 event_id 幂等确认 relay；重复确认不会生成第二条事件。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'delivered',
                    relay_last_error = NULL,
                    relay_claimed_by = NULL,
                    relay_claimed_until = NULL
                WHERE event_id = ? AND relay_state = 'claimed'
                  AND relay_claimed_by = ?
                """,
                (event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                row = self._select_config_event(connection, event_id)
                if row is None:
                    raise KeyError(f"配置 outbox 事件不存在: {event_id}")
                record = self._event_from_row(row)
                if record.relay_state != "delivered":
                    raise ConfigConflictError(
                        "配置 outbox relay claim 不属于当前 consumer"
                    )
            else:
                row = self._select_config_event(connection, event_id)
                if row is None:
                    raise RuntimeError(f"配置 outbox 确认后事件消失: {event_id}")
                record = self._event_from_row(row)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_config_event_relay(
        self,
        *,
        event_id: str,
        consumer_id: str,
        error: str,
        retry_after_seconds: float = 0.0,
    ) -> ConfigEventRecord:
        """记录 relay 失败并安排重试，保持原 event_id/cursor 不变。"""
        if not consumer_id.strip():
            raise ValueError("配置 outbox consumer_id 不能为空")
        if not error.strip():
            raise ValueError("配置 outbox relay 错误不能为空")
        if isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
            raise ValueError("配置 outbox relay 重试延迟不能为负数")
        next_attempt_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE config_events
                SET relay_state = 'failed',
                    relay_last_error = ?,
                    relay_claimed_by = NULL,
                    relay_claimed_until = NULL,
                    relay_next_attempt_at = ?
                WHERE event_id = ? AND relay_state = 'claimed'
                  AND relay_claimed_by = ?
                """,
                (error, next_attempt_at, event_id, consumer_id),
            )
            if cursor.rowcount == 0:
                row = self._select_config_event(connection, event_id)
                if row is None:
                    raise KeyError(f"配置 outbox 事件不存在: {event_id}")
                record = self._event_from_row(row)
                if record.relay_state != "delivered":
                    raise ConfigConflictError(
                        "配置 outbox relay claim 不属于当前 consumer"
                    )
            else:
                row = self._select_config_event(connection, event_id)
                if row is None:
                    raise RuntimeError(f"配置 outbox 失败记录后事件消失: {event_id}")
                record = self._event_from_row(row)
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_config_events(
        self, *, config_domain: str, after: int = 0, limit: int = 100
    ):
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Gateway 配置事件分页参数无效")
        self.ensure_config_event_cursor(config_domain=config_domain, after=after)
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT event_seq, event_id, config_domain, candidate_id, attempt_id,
                       apply_id, idempotency_key, commit_revision, active_revision,
                       pending_revision, source, result, activation_scope,
                       changed_paths_json, applied_paths_json, deferred_paths_json,
                       error, occurred_at, relay_state, relay_attempts,
                       relay_last_error, relay_claimed_by, relay_claimed_until,
                       relay_next_attempt_at
                FROM config_events
                WHERE config_domain = ? AND event_seq > ?
                ORDER BY event_seq ASC LIMIT ?
                """,
                (config_domain, after, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._event_from_row(row) for row in rows)

    def config_event_bounds(self, *, config_domain: str) -> tuple[int | None, int]:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT MIN(event_seq), MAX(event_seq)
                FROM config_events WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'config_events'"
            ).fetchone()
            return (
                int(row[0]) if row[0] is not None else None,
                int(sequence_row[0]) if sequence_row is not None else 0,
            )
        finally:
            connection.close()

    def ensure_config_event_cursor(self, *, config_domain: str, after: int) -> None:
        if after < 0:
            raise ValueError("Gateway 配置事件游标不能为负数")
        first, _ = self.config_event_bounds(config_domain=config_domain)
        if after > 0 and first is not None and after < first - 1:
            raise ConfigEventCursorGoneError(
                config_domain=config_domain,
                after=after,
                first=first,
            )

    def prune_config_events(
        self, *, config_domain: str, retention_days: int = 30
    ) -> int:
        if retention_days < 1:
            raise ValueError("Gateway 配置事件保留天数必须大于 0")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        connection = self._database.connection()
        try:
            cursor = connection.execute(
                "DELETE FROM config_events WHERE config_domain = ? AND occurred_at < ?",
                (config_domain, cutoff),
            )
            return int(cursor.rowcount)
        finally:
            connection.close()

    def load_workspace_registry(self) -> dict[str, object] | None:
        meta = self.get_config("workspace_registry_meta")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM gateway_workspace_registry
                ORDER BY position ASC, workspace_id ASC
                """
            ).fetchall()
            if meta is None and not rows:
                return None
            targets: list[object] = []
            for row in rows:
                payload = json.loads(str(row[0]))
                if not isinstance(payload, dict):
                    raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                targets.append(payload)
            metadata = meta.payload if meta is not None else {}
            return {
                "schema_version": int(metadata.get("schema_version", 10)),
                "registry_revision": int(metadata.get("registry_revision", 0)),
                "active_workspace_id": metadata.get("active_workspace_id"),
                "order_customized": bool(metadata.get("order_customized", False)),
                "runtime_generation": metadata.get("runtime_generation"),
                "remote_gateway_connections": metadata.get(
                    "remote_gateway_connections", []
                ),
                "targets": targets,
            }
        finally:
            connection.close()

    def get_registry_revision(self) -> int:
        """只读返回 Gateway workspace registry 的当前 revision。"""

        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def rebase_config_apply_registry_revision(
        self,
        *,
        apply_id: str,
        expected_registry_revision: int,
        allowed_owners: tuple[str, ...] = ("system", "config_batch"),
    ) -> int:
        """把受控启动自身产生的 registry 提交纳入配置 apply 基线。

        Gateway pending generation 在启动时会重建默认工作区、恢复托管运行时，
        以及按 pending 配置重建 remote projection。这些操作会正常推进 registry
        revision，但不能因此被最终 promotion 当成外部并发修改。只有当基线之后
        的每一条已提交 registry journal 都属于明确允许的启动 owner，才允许原子
        更新配置 apply journal；人工 CRUD 或缺失 journal 会保留 CAS 冲突。
        """

        if not apply_id:
            raise ValueError("Gateway 配置 apply_id 不能为空")
        if expected_registry_revision < 0:
            raise ValueError("Gateway registry revision 不能为负数")
        if not allowed_owners:
            raise ValueError("Gateway registry 启动 owner 白名单不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            journal = connection.execute(
                """
                SELECT registry_revision, state
                FROM config_apply_journal
                WHERE apply_id = ?
                """,
                (apply_id,),
            ).fetchone()
            if journal is None:
                raise ConfigConflictError(
                    f"Gateway 配置 apply journal 不存在: apply_id={apply_id}"
                )
            if str(journal[1]) != "applying":
                raise ConfigConflictError(
                    "Gateway 配置 apply journal 当前不可重设 registry 基线: "
                    f"state={journal[1]}"
                )
            journal_revision = (
                int(journal[0]) if journal[0] is not None else None
            )
            if journal_revision != expected_registry_revision:
                raise ConfigConflictError(
                    "Gateway 配置 apply journal registry 基线已变化: "
                    f"current={journal_revision}, expected={expected_registry_revision}"
                )
            registry_row = connection.execute(
                "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
            ).fetchone()
            current_revision = int(registry_row[0]) if registry_row is not None else 0
            if current_revision < expected_registry_revision:
                raise ConfigConflictError(
                    "Gateway registry revision 不能回退: "
                    f"current={current_revision}, expected={expected_registry_revision}"
                )
            if current_revision == expected_registry_revision:
                connection.execute("COMMIT")
                return current_revision

            rows = connection.execute(
                """
                SELECT base_revision, target_revision, owner
                FROM registry_apply_journal
                WHERE state = 'committed'
                  AND target_revision > ?
                  AND target_revision <= ?
                ORDER BY target_revision ASC
                """,
                (expected_registry_revision, current_revision),
            ).fetchall()
            next_revision = expected_registry_revision
            for row in rows:
                base_revision = int(row[0])
                target_revision = row[1]
                owner = str(row[2])
                if owner not in allowed_owners:
                    raise ConfigConflictError(
                        "Gateway pending 启动期间发现非启动 registry 修改: "
                        f"owner={owner}, base={base_revision}, target={target_revision}"
                    )
                if (
                    base_revision != next_revision
                    or target_revision is None
                    or int(target_revision) != next_revision + 1
                ):
                    raise ConfigConflictError(
                        "Gateway pending 启动期间 registry journal 不连续: "
                        f"expected_base={next_revision}, base={base_revision}, "
                        f"target={target_revision}"
                    )
                next_revision = int(target_revision)
            if next_revision != current_revision:
                raise ConfigConflictError(
                    "Gateway pending 启动期间 registry revision 缺少可审计 journal: "
                    f"current={current_revision}, covered={next_revision}"
                )
            updated = connection.execute(
                """
                UPDATE config_apply_journal
                SET registry_revision = ?, updated_at = ?
                WHERE apply_id = ? AND state = 'applying'
                  AND registry_revision = ?
                """,
                (
                    current_revision,
                    utc_now_text(),
                    apply_id,
                    expected_registry_revision,
                ),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError(
                    "Gateway 配置 apply journal registry 基线更新 CAS 失败"
                )
            connection.execute("COMMIT")
            return current_revision
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_registry_apply_journal(
        self,
        *,
        states: tuple[str, ...] = (),
    ) -> tuple[dict[str, object], ...]:
        """只读返回 registry 批处理的持久恢复记录。"""

        connection = self._database.connection()
        try:
            query = """
                SELECT apply_id, owner, base_revision, target_revision, state,
                       payload_digest, last_error, created_at, updated_at
                FROM registry_apply_journal
            """
            params: tuple[object, ...] = ()
            if states:
                placeholders = ",".join("?" for _ in states)
                query += f" WHERE state IN ({placeholders})"
                params = states
            query += " ORDER BY updated_at ASC, apply_id ASC"
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                "apply_id": str(row[0]),
                "owner": str(row[1]),
                "base_revision": int(row[2]),
                "target_revision": int(row[3]) if row[3] is not None else None,
                "state": str(row[4]),
                "payload_digest": str(row[5]),
                "last_error": str(row[6]) if row[6] is not None else None,
                "created_at": str(row[7]),
                "updated_at": str(row[8]),
            }
            for row in rows
        )

    def recover_registry_apply_journal(self) -> tuple[dict[str, object], ...]:
        """将遗留 applying 标记为 recovery_required，禁止静默重放整批。"""

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE registry_apply_journal
                SET state = 'recovery_required',
                    last_error = 'Gateway 进程在 registry apply journal 提交前退出',
                    updated_at = ?
                WHERE state = 'applying'
                """,
                (utc_now_text(),),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.list_registry_apply_journal(states=("recovery_required",))

    def replace_workspace_registry(
        self,
        payload: dict[str, object],
        *,
        expected_revision: int | None = None,
        owner: str = "registry",
    ) -> int:
        targets = payload.get("targets", [])
        remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(targets, list) or not isinstance(remote_connections, list):
            raise ValueError("Gateway SQLite 注册表 payload 结构无效")
        metadata = {
            "schema_version": int(payload.get("schema_version", 10)),
            "registry_revision": int(payload.get("registry_revision", 0)),
            "active_workspace_id": payload.get("active_workspace_id"),
            "order_customized": bool(payload.get("order_customized", False)),
            "runtime_generation": payload.get("runtime_generation"),
            "remote_gateway_connections": remote_connections,
        }
        apply_id = new_config_id("registry_apply")
        payload_digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._start_registry_apply_journal(
            apply_id=apply_id,
            owner=owner,
            expected_revision=expected_revision,
            payload_digest=payload_digest,
        )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_row = connection.execute(
                    "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
                ).fetchone()
                current_revision = int(current_row[0]) if current_row is not None else 0
                if (
                    expected_revision is not None
                    and current_revision != expected_revision
                ):
                    raise ConfigConflictError(
                        "Gateway registry revision CAS 冲突: "
                        f"current={current_revision}, expected={expected_revision}"
                    )
                existing_rows = connection.execute(
                    """
                    SELECT workspace_id, payload_json
                    FROM gateway_workspace_registry
                    ORDER BY position ASC, workspace_id ASC
                    """
                ).fetchall()
                existing_targets: dict[str, dict[str, object]] = {}
                for row in existing_rows:
                    existing_payload = json.loads(str(row[1]))
                    if not isinstance(existing_payload, dict):
                        raise ValueError("Gateway SQLite 已有工作区注册记录必须是对象")
                    existing_targets[str(row[0])] = existing_payload

                scope_owner = {
                    "config": "config",
                    "config_batch": "config",
                    "manual": "manual",
                    "manual_crud": "manual",
                    "system": "system",
                    "remote_projection": "remote_projection",
                }.get(owner)
                scope_target_owners = (
                    {
                        "config",
                        "remote_projection",
                    }
                    if scope_owner == "config"
                    else {scope_owner}
                )
                if scope_owner is not None:
                    current_meta_row = connection.execute(
                        """
                        SELECT payload_json
                        FROM gateway_config
                        WHERE config_key = 'workspace_registry_meta'
                        """
                    ).fetchone()
                    if current_meta_row is not None:
                        current_metadata = json.loads(str(current_meta_row[0]))
                        if isinstance(current_metadata, dict) and isinstance(
                            current_metadata.get("remote_gateway_connections"), list
                        ):
                            metadata["remote_gateway_connections"] = current_metadata[
                                "remote_gateway_connections"
                            ]
                if scope_owner is not None:
                    for target in targets:
                        if not isinstance(target, dict):
                            raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                        target_workspace_id = target.get("workspace_id")
                        target_owner = target.get("owner", "manual")
                        existing_target = (
                            existing_targets.get(target_workspace_id)
                            if isinstance(target_workspace_id, str)
                            else None
                        )
                        existing_owner = (
                            existing_target.get("owner", "manual")
                            if existing_target is not None
                            else None
                        )
                        system_update_existing_target = (
                            scope_owner == "system"
                            and existing_target is not None
                            and existing_owner == target_owner
                            and all(
                                existing_target.get(field) == target.get(field)
                                for field in (
                                    "owner",
                                    "target_namespace",
                                    "connection_id",
                                    "connection_kind",
                                    "root_path",
                                    "managed",
                                    "removable",
                                    "system_default",
                                    "remote_gateway_connection_id",
                                    "remote_workspace_id",
                                )
                            )
                        )
                        manual_update_system_default = (
                            scope_owner == "manual"
                            and existing_target is not None
                            and existing_owner == target_owner == "system"
                            and bool(existing_target.get("system_default"))
                            and bool(target.get("system_default"))
                            and all(
                                existing_target.get(field) == target.get(field)
                                for field in (
                                    "owner",
                                    "target_namespace",
                                    "connection_id",
                                    "connection_kind",
                                    "root_path",
                                    "managed",
                                    "removable",
                                    "system_default",
                                    "remote_gateway_connection_id",
                                    "remote_workspace_id",
                                )
                            )
                        )
                        if target_owner not in scope_target_owners and not (
                            system_update_existing_target
                            or manual_update_system_default
                            or (
                                existing_target is not None
                                and dump_json(existing_target) == dump_json(target)
                            )
                        ):
                            raise PermissionError(
                                "Gateway registry 批处理不能修改其他 target owner: "
                                f"batch={scope_owner}, workspace_id={target_workspace_id}, "
                                f"target={target_owner}"
                            )

                incoming_targets: dict[str, dict[str, object]] = {}
                for target in targets:
                    if not isinstance(target, dict):
                        raise ValueError("Gateway SQLite 工作区注册记录必须是对象")
                    workspace_id = target.get("workspace_id")
                    if not isinstance(workspace_id, str) or not workspace_id:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少 workspace_id"
                        )
                    if workspace_id in incoming_targets:
                        raise ValueError(
                            f"Gateway SQLite 工作区注册记录 workspace_id 重复: {workspace_id}"
                        )
                    target_owner = target.get("owner", "manual")
                    if target_owner not in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 owner 非法: "
                            f"workspace_id={workspace_id}, owner={target_owner}"
                        )
                    previous_target = existing_targets.get(workspace_id)
                    if (
                        previous_target is not None
                        and previous_target.get("owner") is not None
                        and previous_target.get("owner", "manual") != target_owner
                    ):
                        raise PermissionError(
                            "Gateway registry 不允许批处理改变 target owner: "
                            f"workspace_id={workspace_id}, "
                            f"current={previous_target.get('owner')}, "
                            f"requested={target_owner}"
                        )
                    incoming_targets[workspace_id] = target

                if scope_owner is None:
                    final_targets = [
                        target for target in targets if isinstance(target, dict)
                    ]
                else:
                    preserved_targets = [
                        existing_targets[workspace_id]
                        for workspace_id in existing_targets
                        if existing_targets[workspace_id].get("owner", "manual")
                        not in scope_target_owners
                        and workspace_id not in incoming_targets
                    ]
                    final_targets = [
                        *[target for target in targets if isinstance(target, dict)],
                        *preserved_targets,
                    ]

                final_target_ids: set[str] = set()
                target_identity_keys: set[tuple[str, str, str]] = set()
                for position, target in enumerate(final_targets):
                    workspace_id = target.get("workspace_id")
                    if not isinstance(workspace_id, str) or not workspace_id:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少 workspace_id"
                        )
                    if workspace_id in final_target_ids:
                        raise ValueError(
                            f"Gateway SQLite 最终注册记录 workspace_id 重复: {workspace_id}"
                        )
                    final_target_ids.add(workspace_id)
                    target_owner = target.get("owner", "manual")
                    if target_owner not in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }:
                        raise ValueError(
                            "Gateway SQLite 最终注册记录 owner 非法: "
                            f"workspace_id={workspace_id}, owner={target_owner}"
                        )
                    namespace = target.get("target_namespace", "gateway")
                    if not isinstance(namespace, str) or not namespace.strip():
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 target_namespace 无效: "
                            f"workspace_id={workspace_id}"
                        )
                    connection_id = target.get("connection_id")
                    if connection_id is not None and (
                        not isinstance(connection_id, str) or not connection_id
                    ):
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 connection_id 无效: "
                            f"workspace_id={workspace_id}"
                        )
                    identity_value = (
                        target.get("remote_workspace_id")
                        if target_owner == "remote_projection"
                        else connection_id or workspace_id
                    )
                    if not isinstance(identity_value, str) or not identity_value:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录缺少稳定 identity: "
                            f"workspace_id={workspace_id}"
                        )
                    identity_key = (
                        str(target_owner),
                        namespace,
                        identity_value,
                    )
                    if identity_key in target_identity_keys:
                        raise ValueError(
                            "Gateway SQLite 工作区注册记录 owner/namespace/identity 重复: "
                            f"{identity_key}"
                        )
                    target_identity_keys.add(identity_key)
                    connection.execute(
                        """
                        INSERT INTO gateway_workspace_registry(
                            workspace_id, position, active, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(workspace_id) DO UPDATE SET
                            position=excluded.position,
                            active=excluded.active,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            workspace_id,
                            position,
                            int(workspace_id == metadata["active_workspace_id"]),
                            json.dumps(target, ensure_ascii=False, sort_keys=True),
                            utc_now_text(),
                        ),
                    )
                stale_ids = set(existing_targets) - final_target_ids
                if stale_ids:
                    placeholders = ",".join("?" for _ in stale_ids)
                    connection.execute(
                        "DELETE FROM gateway_workspace_registry WHERE workspace_id IN ("
                        + placeholders
                        + ")",
                        tuple(stale_ids),
                    )
                next_revision = current_revision + 1
                metadata["registry_revision"] = next_revision
                connection.execute(
                    """
                    INSERT INTO gateway_config(config_key, config_version, payload_json, updated_at)
                    VALUES ('workspace_registry_meta', 1, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        utc_now_text(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO registry_meta(registry_key, revision)
                    VALUES ('workspace', ?)
                    ON CONFLICT(registry_key) DO UPDATE SET revision=excluded.revision
                    """,
                    (next_revision,),
                )
                connection.execute(
                    """
                    UPDATE registry_apply_journal
                    SET state = 'committed', target_revision = ?, updated_at = ?
                    WHERE apply_id = ? AND state = 'applying'
                    """,
                    (next_revision, utc_now_text(), apply_id),
                )
                connection.execute("COMMIT")
                return next_revision
            except Exception:
                connection.rollback()
                self._finish_registry_apply_journal(
                    apply_id=apply_id,
                    state="failed",
                    error="registry batch transaction 失败",
                )
                raise
        finally:
            connection.close()

    def _start_registry_apply_journal(
        self,
        *,
        apply_id: str,
        owner: str,
        expected_revision: int | None,
        payload_digest: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM registry_meta WHERE registry_key = 'workspace'"
            ).fetchone()
            current_revision = int(row[0]) if row is not None else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise ConfigConflictError(
                    "Gateway registry revision CAS 冲突: "
                    f"current={current_revision}, expected={expected_revision}"
                )
            connection.execute(
                """
                INSERT INTO registry_apply_journal(
                    apply_id, owner, base_revision, target_revision, state,
                    payload_digest, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, 'applying', ?, NULL, ?, ?)
                """,
                (
                    apply_id,
                    owner,
                    current_revision,
                    payload_digest,
                    utc_now_text(),
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_registry_apply_journal(
        self,
        *,
        apply_id: str,
        state: str,
        error: str,
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE registry_apply_journal
                SET state = ?, last_error = ?, updated_at = ?
                WHERE apply_id = ? AND state = 'applying'
                """,
                (state, error, utc_now_text(), apply_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> GatewayStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
