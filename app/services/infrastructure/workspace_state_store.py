from __future__ import annotations

import asyncio
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
    ConfigActiveSnapshotRecord,
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
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    migrate_legacy_secret_payload,
    new_config_id,
    prepare_config_for_persistence,
    validate_state_transition,
)

_WORKSPACE_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS workspace_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspace_activity (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspace_event_cursors (
        cursor_key TEXT PRIMARY KEY,
        cursor_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
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
    CREATE TABLE IF NOT EXISTS config_source_fanout (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        workspace_id TEXT NOT NULL,
        status TEXT NOT NULL,
        layer_revision INTEGER,
        layer_digest TEXT,
        result TEXT,
        error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation, workspace_id),
        FOREIGN KEY (source_key, source_generation)
            REFERENCES config_source_journal(source_key, source_generation)
            ON DELETE CASCADE
    );
    """,
    """
    ALTER TABLE config_pending_candidate ADD COLUMN last_attempt_id TEXT;
    ALTER TABLE config_pending_candidate ADD COLUMN last_apply_id TEXT;
    """,
    """
    ALTER TABLE config_events ADD COLUMN activation_scope TEXT NOT NULL DEFAULT 'unknown';
    """,
    """
    ALTER TABLE config_pending_candidate ADD COLUMN candidate_ref TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS config_pending_candidate_ref_unique
        ON config_pending_candidate(candidate_ref)
        WHERE candidate_ref IS NOT NULL;
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
    ALTER TABLE config_pending_candidate ADD COLUMN secret_bindings_json TEXT
        NOT NULL DEFAULT '{}';
    """,
)


@dataclass(frozen=True, slots=True)
class WorkspaceActivityRecord:
    event_seq: int
    event_id: str
    session_id: str
    status: str
    summary: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceConfigRecord:
    config_key: str
    config_version: int
    payload: dict[str, object]


class WorkspaceActivityCursorGoneError(RuntimeError):
    pass


class WorkspaceActivityService:
    def __init__(self, *, workspace_root: Path, retention_days: int = 30) -> None:
        self.store = WorkspaceStateStore(workspace_root=workspace_root)
        self.retention_days = retention_days
        self._subscribers: set[asyncio.Queue[WorkspaceActivityRecord]] = set()
        self._subscriber_lock = asyncio.Lock()

    async def append(
        self,
        *,
        event_id: str,
        session_id: str,
        status: str,
        summary: str,
        occurred_at: str | None = None,
    ) -> WorkspaceActivityRecord:
        record = self.store.append_activity(
            event_id=event_id,
            session_id=session_id,
            status=status,
            summary=summary,
            occurred_at=occurred_at,
        )
        async with self._subscriber_lock:
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.put_nowait(record)
                except asyncio.QueueFull as error:
                    raise RuntimeError("Workspace 活动事件订阅者消费速度不足") from error
        return record

    def list(self, *, after: int = 0, limit: int = 100) -> tuple[WorkspaceActivityRecord, ...]:
        first_seq, latest_seq = self.store.activity_bounds()
        if (
            after > 0
            and latest_seq > after
            and (first_seq is None or first_seq > after + 1)
        ):
            raise WorkspaceActivityCursorGoneError(
                f"Workspace 活动事件游标已失效: after={after}, first={first_seq}"
            )
        records = self.store.list_activity(after=after, limit=limit)
        if after > 0 and records:
            first_seq = records[0].event_seq
            if first_seq > after + 1:
                raise WorkspaceActivityCursorGoneError(
                    f"Workspace 活动事件游标已失效: after={after}, first={first_seq}"
                )
        return records

    async def stream(self, *, after: int = 0):
        async with self._subscriber_lock:
            initial = self.list(after=after)
            subscriber: asyncio.Queue[WorkspaceActivityRecord] = asyncio.Queue(maxsize=100)
            self._subscribers.add(subscriber)
        try:
            for record in initial:
                yield record
            if initial:
                after = initial[-1].event_seq
            while True:
                try:
                    record = await asyncio.wait_for(subscriber.get(), timeout=15)
                except TimeoutError:
                    yield None
                    continue
                if record.event_seq <= after:
                    continue
                after = record.event_seq
                yield record
        finally:
            async with self._subscriber_lock:
                self._subscribers.discard(subscriber)

    def prune(self) -> int:
        return self.store.prune_activity(retention_days=self.retention_days)

    def diagnostics(self) -> SQLiteDiagnostics:
        return self.store.diagnostics()

    def close(self) -> None:
        self.store.close()


class WorkspaceStateStore:
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self._database = SQLiteStateDatabase(
            path=self.workspace_root / ".boxteam" / "state" / "workspace.sqlite",
            schema_version=len(_WORKSPACE_MIGRATIONS),
            migrations=_WORKSPACE_MIGRATIONS,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def connection(self) -> sqlite3.Connection:
        return self._database.connection()

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

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
                INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
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
        """升级旧表中的秘密；字面量只保留不可用摘要并返回阻断路径。"""

        connection = self._database.connection()
        blocked: set[str] = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
            legacy_row = connection.execute(
                "SELECT payload_json FROM workspace_config WHERE config_key = ?",
                (config_key,),
            ).fetchone()
            if legacy_row is not None:
                migrated, paths = migrate_legacy_secret_payload(
                    json.loads(str(legacy_row[0]))
                )
                blocked.update(paths)
                connection.execute(
                    "UPDATE workspace_config SET payload_json = ?, updated_at = ? WHERE config_key = ?",
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
                        dump_json(migrated_payload) if migrated_payload is not None else None,
                        dump_json(migrated_previous) if migrated_previous is not None else None,
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
        """升级旧 active payload，并对无法恢复的字面量建立恢复态。"""

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
            migrated, blocked = migrate_legacy_secret_payload(
                json.loads(str(row[0]))
            )
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
                    "旧 active snapshot 含无法恢复的字面量 secret，需重新导入引用",
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
            if row is None:
                return None
            payload = (
                load_json_object(str(row[4]), field="source layer payload")
                if row[4] is not None
                else None
            )
            return ConfigSourceLayerRecord(
                config_key=str(row[0]),
                source_path=str(row[1]),
                presence=str(row[2]),  # type: ignore[arg-type]
                config_version=int(row[3]),
                payload=payload,
                layer_revision=int(row[5]),
                layer_digest=str(row[6]) if row[6] is not None else None,
                source_generation=int(row[7]),
                previous_digest=str(row[8]) if row[8] is not None else None,
                updated_at=datetime.fromisoformat(str(row[9])),
                previous_payload=(
                    load_json_object(
                        str(row[10]), field="source layer previous payload"
                    )
                    if row[10] is not None
                    else None
                ),
                backup_path=str(row[11]) if row[11] is not None else None,
            )
        finally:
            connection.close()

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
        config_domain: str | None = None,
        expected_active_revision: int | None = None,
        expected_active_digest: str | None = None,
        enforce_layer_cas: bool = False,
        enforce_active_cas: bool = False,
    ) -> ConfigSourceLayerRecord:
        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 source layer presence: {presence}")
        if presence == "present" and payload is None:
            raise ValueError("present source layer 必须有 payload")
        if presence == "absent" and payload is not None:
            raise ValueError("absent source layer 的 payload 必须为空")
        if (expected_active_revision is None) != (expected_active_digest is None):
            raise ValueError("active CAS 必须同时提供 revision 和 digest")
        if (enforce_active_cas or expected_active_revision is not None) and not config_domain:
            raise ValueError("active CAS 必须声明 config_domain")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if enforce_active_cas or expected_active_revision is not None:
                active = connection.execute(
                    """
                    SELECT active_revision, effective_digest
                    FROM config_active_snapshot
                    WHERE config_domain = ?
                    """,
                    (config_domain,),
                ).fetchone()
                current_active_revision = int(active[0]) if active is not None else None
                current_active_digest = str(active[1]) if active is not None else None
                if (
                    current_active_revision != expected_active_revision
                    or current_active_digest != expected_active_digest
                ):
                    raise ConfigConflictError(
                        "active snapshot CAS 冲突: "
                        f"domain={config_domain}, "
                        f"current_revision={current_active_revision}, "
                        f"current_digest={current_active_digest}"
                    )
            row = connection.execute(
                """
                SELECT source_path, presence, layer_revision, layer_digest,
                       source_generation, payload_json
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                if expected_layer_revision is not None or expected_layer_digest is not None:
                    raise ConfigConflictError(
                        f"source layer 不存在但调用方声明了旧基线: key={config_key}"
                    )
                layer_revision = 1
                source_generation = 1
                previous_digest = None
                previous_payload_json = None
            else:
                current_path = str(row[0])
                current_presence = str(row[1])
                current_revision = int(row[2])
                current_digest = str(row[3]) if row[3] is not None else None
                if (
                    (enforce_layer_cas or expected_layer_revision is not None)
                    and current_revision != expected_layer_revision
                ) or (
                    (enforce_layer_cas or expected_layer_digest is not None)
                    and current_digest != expected_layer_digest
                ):
                    raise ConfigConflictError(
                        "source layer CAS 冲突: "
                        f"key={config_key}, current_revision={current_revision}, "
                        f"current_digest={current_digest}"
                    )
                if (
                    current_path == str(source_path.expanduser().resolve())
                    and current_presence == presence
                    and current_digest == layer_digest
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
                            INSERT INTO workspace_config(
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
                            "DELETE FROM workspace_config WHERE config_key = ?",
                            (config_key,),
                        )
                    if journal_origin is not None:
                        self._append_config_source_journal_in_connection(
                            connection,
                            source_key=config_key,
                            source_event_id=(
                                source_event_id
                                or f"{config_key}:layer:{current_revision}"
                            ),
                            source_path=source_path,
                            presence=presence,
                            layer_revision=current_revision,
                            layer_digest=layer_digest,
                            previous_digest=current_digest,
                            origin=journal_origin,
                            fanout_id=(
                                fanout_id
                                or f"fanout:{config_key}:event:{config_key}:layer:{current_revision}"
                            ),
                        )
                    connection.execute("COMMIT")
                    record = self.get_source_layer(config_key)
                    if record is None:
                        raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
                    return record
                layer_revision = current_revision + 1
                source_generation = int(row[4]) + 1
                previous_digest = current_digest
                previous_payload_json = row[5]

            payload_json = (
                dump_json(prepare_config_for_persistence(payload))
                if payload is not None
                else None
            )
            now = utc_now_text()
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
                    layer_revision,
                    layer_digest,
                    source_generation,
                    previous_digest,
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
                        source_event_id or f"{config_key}:layer:{layer_revision}"
                    ),
                    source_path=source_path,
                    presence=presence,
                    layer_revision=layer_revision,
                    layer_digest=layer_digest,
                    previous_digest=previous_digest,
                    origin=journal_origin,
                    fanout_id=(
                        fanout_id
                        or f"fanout:{config_key}:event:{config_key}:layer:{layer_revision}"
                    ),
                )
            if presence == "present":
                if payload is None:
                    raise RuntimeError("present source layer payload 在事务中丢失")
                connection.execute(
                    """
                    INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (config_key, config_version, payload_json, now),
                )
            else:
                connection.execute(
                    "DELETE FROM workspace_config WHERE config_key = ?",
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
            raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
        return record

    def update_source_generation(
        self,
        *,
        config_key: str,
        source_generation: int,
        expected_layer_revision: int,
        expected_layer_digest: str | None,
    ) -> ConfigSourceLayerRecord:
        """把本地 materialized layer 绑定到共享 source owner 的 generation。"""

        if source_generation < 1 or expected_layer_revision < 1:
            raise ValueError("source generation 或 layer revision 必须为正数")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT source_path, presence, config_version, payload_json,
                       layer_revision, layer_digest, source_generation,
                       previous_digest, updated_at, previous_payload_json, backup_path
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"source layer 不存在，无法绑定共享 generation: {config_key}"
                )
            current_revision = int(row[4])
            current_digest = str(row[5]) if row[5] is not None else None
            if (
                current_revision != expected_layer_revision
                or current_digest != expected_layer_digest
            ):
                raise ConfigConflictError(
                    "source layer generation 绑定 CAS 冲突: "
                    f"key={config_key}, revision={current_revision}, digest={current_digest}"
                )
            if int(row[6]) > source_generation:
                raise ConfigConflictError(
                    "source layer generation 不能回退: "
                    f"key={config_key}, current={row[6]}, requested={source_generation}"
                )
            connection.execute(
                """
                UPDATE config_source_layers
                SET source_generation = ?, updated_at = ?
                WHERE config_key = ?
                """,
                (source_generation, utc_now_text(), config_key),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError(f"source layer generation 绑定后无法读取: {config_key}")
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
            raise ValueError("source journal presence 无效")
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
                    "source journal event_id 已绑定不同 source 记录"
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
            raise ConfigConflictError("source journal generation CAS 冲突")
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
            raise ConfigConflictError("source owner generation CAS 冲突")
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
            raise ValueError(f"未知 source journal presence: {presence}")
        if not source_key or not source_event_id or not fanout_id:
            raise ValueError("source journal 身份不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_event_id = ?
                """,
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return ConfigSourceJournalRecord(
                    source_key=str(existing[0]),
                    source_generation=int(existing[1]),
                    source_event_id=str(existing[2]),
                    source_path=str(existing[3]),
                    presence=cast(str, existing[4]),
                    layer_revision=int(existing[5]),
                    layer_digest=(
                        str(existing[6]) if existing[6] is not None else None
                    ),
                    previous_digest=(
                        str(existing[7]) if existing[7] is not None else None
                    ),
                    origin=str(existing[8]),
                    fanout_id=str(existing[9]),
                    created_at=datetime.fromisoformat(str(existing[10])),
                )
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
                raise ConfigConflictError(
                    "source journal generation CAS 冲突: "
                    f"source_key={source_key}, current={current_generation}, "
                    f"expected={expected_source_generation}"
                )
            if (
                latest is not None
                and str(latest[1]) == presence
                and (
                    str(latest[2]) if latest[2] is not None else None
                )
                == layer_digest
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
                    raise RuntimeError("source journal 去重后无法读取最新记录")
                connection.execute("COMMIT")
                return ConfigSourceJournalRecord(
                    source_key=str(existing[0]),
                    source_generation=int(existing[1]),
                    source_event_id=str(existing[2]),
                    source_path=str(existing[3]),
                    presence=cast(str, existing[4]),
                    layer_revision=int(existing[5]),
                    layer_digest=(
                        str(existing[6]) if existing[6] is not None else None
                    ),
                    previous_digest=(
                        str(existing[7]) if existing[7] is not None else None
                    ),
                    origin=str(existing[8]),
                    fanout_id=str(existing[9]),
                    created_at=datetime.fromisoformat(str(existing[10])),
                )
            generation = current_generation + 1
            owner = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if owner is not None and int(owner[0]) != generation:
                raise ConfigConflictError(
                    "source owner generation 已被其他提交推进: "
                    f"source_key={source_key}, next={owner[0]}, expected={generation}"
                )
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
        records = self.list_config_source_journal(
            source_key=source_key,
            after_generation=generation - 1,
            limit=1,
        )
        if not records:
            raise RuntimeError("source journal 提交后无法读取")
        return records[0]

    def list_config_source_journal(
        self,
        *,
        source_key: str,
        after_generation: int = 0,
        limit: int = 100,
    ) -> tuple[ConfigSourceJournalRecord, ...]:
        if after_generation < 0 or limit < 1 or limit > 2000:
            raise ValueError("source journal 分页参数无效")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_key = ? AND source_generation > ?
                ORDER BY source_generation ASC
                LIMIT ?
                """,
                (source_key, after_generation, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            ConfigSourceJournalRecord(
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
            for row in rows
        )

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

    def record_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_id: str,
        status: str,
        layer_revision: int | None = None,
        layer_digest: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        if not workspace_id or not status:
            raise ValueError("fan-out 工作区和状态不能为空")
        if source_generation < 1:
            raise ValueError("fan-out source_generation 必须为正数")
        connection = self._database.connection()
        try:
            if connection.execute(
                """
                SELECT 1 FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, source_generation),
            ).fetchone() is None:
                raise ConfigConflictError("fan-out 关联的 source journal 不存在")
            connection.execute(
                """
                INSERT INTO config_source_fanout(
                    source_key, source_generation, workspace_id, status,
                    layer_revision, layer_digest, result, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_generation, workspace_id) DO UPDATE SET
                    status=excluded.status,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    source_generation,
                    workspace_id,
                    status,
                    layer_revision,
                    layer_digest,
                    result,
                    error,
                    utc_now_text(),
                ),
            )
        finally:
            connection.close()

    def prepare_config_source_fanout(
        self,
        *,
        source_key: str,
        workspace_id: str,
        after_generation: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """为停止后重新上线的 Workspace 建立逐 generation 的待导入记录。"""
        if not workspace_id.strip() or after_generation < 0:
            raise ValueError("fan-out workspace 或 high-water 无效")
        records = self.list_config_source_journal(
            source_key=source_key,
            after_generation=after_generation,
            limit=limit,
        )
        connection = self._database.connection()
        try:
            now = utc_now_text()
            for record in records:
                connection.execute(
                    """
                    INSERT INTO config_source_fanout(
                        source_key, source_generation, workspace_id, status,
                        layer_revision, layer_digest, result, error, updated_at
                    ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?)
                    ON CONFLICT(source_key, source_generation, workspace_id)
                    DO NOTHING
                    """,
                    (
                        source_key,
                        record.source_generation,
                        workspace_id,
                        now,
                    ),
                )
        finally:
            connection.close()
        return tuple(
            {
                "source_generation": record.source_generation,
                "source_event_id": record.source_event_id,
                "fanout_id": record.fanout_id,
                "status": "pending",
            }
            for record in records
        )

    def config_source_fanout_summary(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """汇总 fan-out，明确区分全部完成、进行中和 fanout_partial。"""
        if not workspace_ids or len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("fan-out workspace_ids 不能为空且不能重复")
        statuses = {
            str(item["workspace_id"]): str(item["status"])
            for item in self.list_config_source_fanout(
                source_key=source_key,
                source_generation=source_generation,
            )
        }
        missing = tuple(
            workspace_id for workspace_id in workspace_ids if workspace_id not in statuses
        )
        failed = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if statuses.get(workspace_id) in {"conflict", "failed"}
        )
        pending = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if workspace_id in statuses
            and statuses[workspace_id] not in {"applied", "conflict", "failed"}
        )
        if failed:
            result = "fanout_partial"
        elif missing or any(
            statuses.get(workspace_id) != "applied" for workspace_id in workspace_ids
        ):
            result = "pending"
        else:
            result = "applied"
        return {
            "source_key": source_key,
            "source_generation": source_generation,
            "result": result,
            "missing_workspace_ids": missing,
            "pending_workspace_ids": pending,
            "failed_workspace_ids": failed,
        }

    def list_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
    ) -> tuple[dict[str, object], ...]:
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT workspace_id, status, layer_revision, layer_digest, result,
                       error, updated_at
                FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ?
                ORDER BY workspace_id ASC
                """,
                (source_key, source_generation),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                "workspace_id": str(row[0]),
                "status": str(row[1]),
                "layer_revision": int(row[2]) if row[2] is not None else None,
                "layer_digest": str(row[3]) if row[3] is not None else None,
                "result": str(row[4]) if row[4] is not None else None,
                "error": str(row[5]) if row[5] is not None else None,
                "updated_at": str(row[6]),
            }
            for row in rows
        )

    def _next_config_revision(
        self,
        connection,
        *,
        config_domain: str,
    ) -> int:
        row = connection.execute(
            "SELECT next_revision FROM config_revision_meta WHERE config_domain = ?",
            (config_domain,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO config_revision_meta(config_domain, next_revision) VALUES (?, ?)",
                (config_domain, 2),
            )
            return 1
        revision = int(row[0])
        connection.execute(
            "UPDATE config_revision_meta SET next_revision = ? WHERE config_domain = ?",
            (revision + 1, config_domain),
        )
        return revision

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
                    """
                    SELECT config_key, source_path, presence, layer_revision,
                           layer_digest, source_generation
                    FROM config_source_layers
                    """
                ).fetchall()
                current_keys = {str(row[0]) for row in current_rows}
                if current_keys != set(expected_sources):
                    raise ConfigConflictError(
                        "source layer 集合 CAS 冲突: "
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
                FROM config_apply_claim
                WHERE config_domain = ?
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
            base_active_revision=(
                int(row[5]) if row[5] is not None else None
            ),
            target_generation=str(row[6]) if row[6] is not None else None,
            lease_expires_at=datetime.fromisoformat(str(row[7])),
            fencing_token=str(row[8]),
            updated_at=datetime.fromisoformat(str(row[9])),
        )

    @staticmethod
    def _apply_journal_from_row(row: sqlite3.Row) -> ConfigApplyJournalRecord:
        side_effects = json.loads(str(row[10]))
        if not isinstance(side_effects, list) or not all(
            isinstance(item, dict) for item in side_effects
        ):
            raise TypeError("Workspace apply journal side_effects 结构无效")
        return ConfigApplyJournalRecord(
            config_domain=str(row[0]),
            apply_id=str(row[1]),
            candidate_id=str(row[2]),
            attempt_id=str(row[3]),
            owner=str(row[4]),
            base_active_revision=(int(row[5]) if row[5] is not None else None),
            pending_revision=(int(row[6]) if row[6] is not None else None),
            source_baseline=load_json_object(
                str(row[7]), field="Workspace apply journal source baseline"
            ),
            active_baseline=load_json_object(
                str(row[8]), field="Workspace apply journal active baseline"
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
            raise ValueError("Workspace apply journal 身份字段不能为空")
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
            raise RuntimeError("Workspace apply journal 提交后无法读取")
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
                raise ConfigConflictError("Workspace apply journal 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace apply journal 更新后无法读取")
        return result

    def append_config_apply_side_effect(
        self,
        *,
        apply_id: str,
        side_effect: dict[str, object],
        expected_state: str = "applying",
    ) -> ConfigApplyJournalRecord:
        """在 journal 中幂等追加一个已观测的外部副作用。"""

        if not side_effect:
            raise ValueError("Workspace apply journal 副作用记录不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None or str(row[0]) != expected_state:
                raise ConfigConflictError(
                    "Workspace apply journal 副作用追加状态 CAS 失败"
                )
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Workspace apply journal side_effects 结构无效")
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
                    "Workspace apply journal 副作用追加状态 CAS 失败"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace apply journal 副作用提交后无法读取")
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
            raise ValueError("Workspace 补偿记录只能包含资源、动作、状态和错误摘要")
        status = compensation.get("status")
        if status not in {"succeeded", "failed"}:
            raise ValueError("Workspace 补偿记录 status 必须是 succeeded 或 failed")
        if not isinstance(compensation.get("resource"), str) or not isinstance(
            compensation.get("action"), str
        ):
            raise ValueError("Workspace 补偿记录必须包含 resource 和 action")
        entry = {"phase": "compensation", **compensation}
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, side_effects_json FROM config_apply_journal WHERE apply_id = ?",
                (apply_id,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError("Workspace 补偿记录关联的 apply journal 不存在")
            current_state = str(row[0])
            side_effects = json.loads(str(row[1]))
            if not isinstance(side_effects, list) or not all(
                isinstance(item, dict) for item in side_effects
            ):
                raise TypeError("Workspace apply journal side_effects 结构无效")
            if current_state == "compensated" and entry in side_effects:
                connection.execute("COMMIT")
            else:
                if current_state != expected_state:
                    raise ConfigConflictError(
                        "Workspace 补偿记录状态 CAS 失败: "
                        f"state={current_state}, expected={expected_state}"
                    )
                if entry not in side_effects:
                    side_effects.append(entry)
                next_state = "compensated" if status == "succeeded" else "recovery_required"
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
                    raise ConfigConflictError("Workspace 补偿记录状态 CAS 失败")
                connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_journal(apply_id=apply_id)
        if result is None:
            raise RuntimeError("Workspace 补偿记录提交后无法读取")
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
        """原子创建 claim、apply journal 并把候选推进到 applying。"""

        if lease_seconds <= 0:
            raise ValueError("配置 apply lease 必须大于 0 秒")
        validate_state_transition(expected_candidate_state, "applying")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("配置 apply 的身份字段不能为空")
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
                raise ConfigConflictError("Workspace begin apply 的候选状态 CAS 失败")
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError("Workspace begin apply 与候选 active 基线不一致")
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "Workspace begin apply 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            existing = connection.execute(
                """
                SELECT attempt_id, apply_id, lease_expires_at, fencing_token
                FROM config_apply_claim
                WHERE config_domain = ?
                """,
                (config_domain,),
            ).fetchone()
            now = datetime.now(UTC)
            if existing is not None:
                same_apply = str(existing[1]) == apply_id
                if not same_apply and datetime.fromisoformat(str(existing[2])) > now:
                    raise ConfigConflictError(
                        "Workspace 配置 apply claim 仍由其他持有者租用: "
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
                raise ConfigConflictError("Workspace begin apply 的候选状态 CAS 失败")
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
                raise ConfigConflictError("Workspace begin apply 的 journal 身份不一致")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("Workspace begin apply 提交后缺少 claim")
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
    ) -> ConfigApplyClaimRecord:
        if lease_seconds <= 0:
            raise ValueError("配置 apply lease 必须大于 0 秒")
        if not all((config_domain, candidate_id, attempt_id, apply_id, owner)):
            raise ValueError("配置 apply claim 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC)
            now_text = now.isoformat()
            existing = connection.execute(
                """
                SELECT candidate_id, attempt_id, apply_id, owner, base_active_revision,
                       target_generation, lease_expires_at, fencing_token
                FROM config_apply_claim
                WHERE config_domain = ?
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
                    "配置 apply claim 只能绑定可应用的候选: "
                    f"domain={config_domain}, candidate={candidate_id}"
                )
            if pending[2] is not None and base_active_revision != int(pending[2]):
                raise ConfigConflictError("配置 apply claim 与候选 active 基线不一致")
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = ?",
                (config_domain,),
            ).fetchone()
            current_active_revision = int(active[0]) if active is not None else None
            if current_active_revision != base_active_revision:
                raise ConfigConflictError(
                    "配置 apply claim 的 active revision 基线已变化: "
                    f"current={current_active_revision}, expected={base_active_revision}"
                )
            if existing is not None:
                same_apply = str(existing[2]) == apply_id
                lease_expires = datetime.fromisoformat(str(existing[6]))
                if not same_apply and lease_expires > now:
                    raise ConfigConflictError(
                        "配置 apply claim 仍由其他持有者租用: "
                        f"domain={config_domain}, apply_id={existing[2]}"
                    )
            fencing_token = (
                str(existing[7])
                if existing is not None and str(existing[2]) == apply_id
                else new_config_id("fence")
            )
            lease_expires_at = now + timedelta(seconds=lease_seconds)
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
                    lease_expires_at.isoformat(),
                    fencing_token,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE config_pending_candidate
                SET fencing_token = ?, last_attempt_id = ?, last_apply_id = ?
                WHERE config_domain = ? AND candidate_id = ?
                """,
                (
                    fencing_token,
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
            raise RuntimeError("配置 apply claim 提交后无法读取")
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
            raise ValueError("配置 apply lease 必须大于 0 秒")
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
                raise ConfigConflictError("配置 apply claim fencing 校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_config_apply_claim(config_domain=config_domain)
        if result is None:
            raise RuntimeError("配置 apply claim 更新后无法读取")
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
                raise ConfigConflictError("配置 apply claim 释放 fencing 校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_config_applies(
        self,
        *,
        config_domain: str,
    ) -> tuple[str, ...]:
        """把遗留的 applying 候选标为 recovery_required，避免启动时猜测。"""
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            rows = connection.execute(
                """
                SELECT candidate_id
                FROM config_pending_candidate
                WHERE config_domain = ? AND state = 'applying'
                  AND candidate_id NOT IN (
                      SELECT candidate_id FROM config_apply_claim
                      WHERE config_domain = ? AND lease_expires_at > ?
                  )
                """,
                (config_domain, config_domain, now),
            ).fetchall()
            candidate_ids = tuple(str(row[0]) for row in rows)
            if candidate_ids:
                connection.executemany(
                    """
                    UPDATE config_pending_candidate
                    SET state = 'recovery_required',
                        last_error = '启动恢复发现 apply lease 已过期'
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    ((config_domain, candidate_id) for candidate_id in candidate_ids),
                )
                connection.executemany(
                    """
                    UPDATE config_apply_journal
                    SET state = 'recovery_required',
                        last_error = '启动恢复发现 apply lease 已过期',
                        updated_at = ?
                    WHERE config_domain = ? AND candidate_id = ? AND state = 'applying'
                    """,
                    (
                        (utc_now_text(), config_domain, candidate_id)
                        for candidate_id in candidate_ids
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM config_apply_claim
                    WHERE config_domain = ? AND lease_expires_at <= ?
                    """,
                    (config_domain, now),
                )
            connection.execute("COMMIT")
            return candidate_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retry_pending_config_restart(
        self,
        *,
        candidate_ref: str,
        target_generation: str,
    ) -> ConfigPendingCandidateRecord:
        """复用 pending candidate 重试，且为新 generation 轮换 fencing token。"""

        if not candidate_ref or not target_generation:
            raise ValueError("Workspace pending retry 的身份字段不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate_id, state, base_active_revision
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"Workspace pending candidate 不存在: {candidate_ref}"
                )
            if str(row[1]) not in {"pending_restart", "recovery_required"}:
                raise ConfigConflictError(
                    "Workspace pending retry 只允许 pending_restart/recovery_required: "
                    f"state={row[1]}"
                )
            active = connection.execute(
                "SELECT active_revision FROM config_active_snapshot WHERE config_domain = 'workspace'"
            ).fetchone()
            active_revision = int(active[0]) if active is not None else None
            if active_revision != (
                int(row[2]) if row[2] is not None else None
            ):
                raise ConfigConflictError(
                    "Workspace pending retry 的 active revision 基线已变化"
                )
            claim = connection.execute(
                """
                SELECT lease_expires_at FROM config_apply_claim
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            now = datetime.now(UTC)
            if claim is not None:
                if datetime.fromisoformat(str(claim[0])) > now:
                    raise ConfigConflictError(
                        "Workspace pending retry 仍有未过期的 apply claim"
                    )
                connection.execute(
                    "DELETE FROM config_apply_claim WHERE config_domain = 'workspace'"
                )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'pending_restart', target_generation = ?,
                    fencing_token = ?, last_error = NULL
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                  AND state IN ('pending_restart', 'recovery_required')
                """,
                (target_generation, new_config_id("fence"), candidate_ref),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Workspace pending retry 状态 CAS 失败")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.load_pending_config_candidate(candidate_ref=candidate_ref)
        return result

    def discard_pending_config_candidate(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
        expected_source_baseline: dict[str, object],
        event: ConfigEventInput | None = None,
        reason: str = "用户显式丢弃 pending candidate",
    ) -> ConfigPendingCandidateRecord:
        """在已确认旧 active 和来源基线安全时原子丢弃 pending。

        recovery_required 不是可以盲目清理的垃圾状态。只有旧 active 仍然完整、所有
        source layer 仍与候选基线一致、且没有未排除的 apply claim/外部副作用时，才允许
        将候选转为 discarded。这样调用方不会用一次普通的删除请求掩盖未知运行时状态。
        """

        if not candidate_ref or not expected_active_digest:
            raise ValueError("Workspace pending discard 的身份和 active digest 不能为空")
        if expected_active_revision < 0:
            raise ValueError("Workspace pending discard 的 active revision 不能为负数")
        if not reason:
            raise ValueError("Workspace pending discard 原因不能为空")
        validate_state_transition("pending_restart", "discarded")
        validate_state_transition("recovery_required", "discarded")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                """
                SELECT candidate_id, pending_revision, state, last_apply_id,
                       source_baseline_json
                FROM config_pending_candidate
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                """,
                (candidate_ref,),
            ).fetchone()
            if pending is None:
                raise ConfigConflictError(
                    f"Workspace pending candidate 不存在: {candidate_ref}"
                )
            candidate_id = str(pending[0])
            pending_revision = int(pending[1])
            current_state = str(pending[2])
            if current_state == "discarded":
                connection.execute("COMMIT")
                result = self.get_pending_config_candidate(
                    config_domain="workspace", candidate_id=candidate_id
                )
                if result is None:
                    raise RuntimeError("Workspace discarded candidate 读取后消失")
                return result
            if current_state not in {"pending_restart", "recovery_required"}:
                raise ConfigConflictError(
                    "Workspace pending discard 只允许 pending_restart/recovery_required: "
                    f"state={current_state}"
                )

            active = connection.execute(
                """
                SELECT active_revision, effective_digest, state
                FROM config_active_snapshot
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            if (
                active is None
                or str(active[2]) != "active"
                or int(active[0]) != expected_active_revision
                or str(active[1]) != expected_active_digest
            ):
                raise ConfigConflictError(
                    "Workspace pending discard 缺少匹配的安全 active 基线"
                )

            claim = connection.execute(
                """
                SELECT apply_id, lease_expires_at
                FROM config_apply_claim
                WHERE config_domain = 'workspace'
                """
            ).fetchone()
            if claim is not None:
                raise ConfigConflictError(
                    "Workspace pending discard 仍有 apply claim，必须先完成恢复或补偿: "
                    f"apply_id={claim[0]}"
                )

            stored_baseline = load_json_object(
                str(pending[4]), field="Workspace pending source baseline"
            )
            expected_sources = {
                str(key): detail
                for key, detail in expected_source_baseline.items()
                if isinstance(detail, dict)
                and detail.get("layer_revision") is not None
            }
            if stored_baseline != expected_source_baseline:
                raise ConfigConflictError(
                    "Workspace pending discard 的 source baseline 与候选不一致"
                )
            current_rows = connection.execute(
                """
                SELECT config_key, source_path, presence, layer_revision,
                       layer_digest, source_generation
                FROM config_source_layers
                """
            ).fetchall()
            if {str(row[0]) for row in current_rows} != set(expected_sources):
                raise ConfigConflictError(
                    "Workspace pending discard 的 source layer 集合已变化"
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
                        "Workspace pending discard 的 source layer 基线已变化: "
                        f"key={row[0]}"
                    )

            apply_id = str(pending[3]) if pending[3] is not None else None
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
                        raise TypeError("Workspace apply journal 副作用结构无效")
                    if side_effects and str(journal[0]) != "compensated":
                        raise ConfigConflictError(
                            "Workspace pending discard 缺少外部副作用补偿证明"
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

            validate_state_transition(
                current_state,  # type: ignore[arg-type]
                "discarded",
            )
            updated = connection.execute(
                """
                UPDATE config_pending_candidate
                SET state = 'discarded', last_error = ?
                WHERE config_domain = 'workspace' AND candidate_ref = ?
                  AND state = ? AND pending_revision = ?
                """,
                (reason, candidate_ref, current_state, pending_revision),
            )
            if updated.rowcount != 1:
                raise ConfigConflictError("Workspace pending discard 状态 CAS 失败")
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
            config_domain="workspace", candidate_id=candidate_id
        )
        if result is None:
            raise RuntimeError("Workspace pending discard 提交后无法读取")
        return result

    @staticmethod
    def _event_from_row(row: sqlite3.Row | tuple[object, ...]) -> ConfigEventRecord:
        def string_tuple(value: object) -> tuple[str, ...]:
            parsed = json.loads(str(value))
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError("配置事件路径必须是字符串数组")
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
            changed_paths=string_tuple(row[13]),
            applied_paths=string_tuple(row[14]),
            deferred_paths=string_tuple(row[15]),
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
        connection,
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
                raise RuntimeError("配置事件幂等记录读取失败")
            return cls._event_from_row(row)
        connection.execute(
            """
            INSERT INTO config_events(
                event_id, config_domain, candidate_id, attempt_id, apply_id,
                idempotency_key, commit_revision, active_revision, pending_revision,
                source, result, activation_scope, changed_paths_json,
                applied_paths_json, deferred_paths_json, error, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
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
            raise RuntimeError(f"配置事件提交后无法读取: {event.event_id}")
        return cls._event_from_row(row)

    def append_config_event(
        self,
        *,
        event_id: str,
        config_domain: str,
        candidate_id: str | None,
        attempt_id: str | None,
        apply_id: str | None,
        idempotency_key: str | None,
        commit_revision: int | None,
        active_revision: int | None,
        pending_revision: int | None,
        source: str,
        result: ConfigResult,
        activation_scope: str = "unknown",
        changed_paths: tuple[str, ...] = (),
        applied_paths: tuple[str, ...] = (),
        deferred_paths: tuple[str, ...] = (),
        error: str | None = None,
    ) -> ConfigEventRecord:
        event = ConfigEventInput(
            event_id=event_id,
            config_domain=config_domain,
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            apply_id=apply_id,
            idempotency_key=idempotency_key,
            commit_revision=commit_revision,
            active_revision=active_revision,
            pending_revision=pending_revision,
            source=source,
            result=result,
            activation_scope=cast(str, activation_scope),
            changed_paths=changed_paths,
            applied_paths=applied_paths,
            deferred_paths=deferred_paths,
            error=error,
        )
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
        """按消费者独立 claim 事件，避免一个 SSE 消费者确认导致其他消费者丢事件。"""
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("配置事件 relay 分页参数无效")
        if not consumer_id.strip():
            raise ValueError("配置事件 consumer_id 不能为空")
        if isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("配置事件 relay lease 必须大于 0")
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
            raise ValueError("配置事件 consumer_id 不能为空")
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
                raise ConfigConflictError("配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"配置事件不存在: {event_id}")
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
            raise ValueError("配置事件 relay consumer 和错误不能为空")
        if isinstance(retry_after_seconds, bool) or retry_after_seconds < 0:
            raise ValueError("配置事件 relay 重试延迟不能为负数")
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
                raise ConfigConflictError("配置事件 relay 不属于当前 consumer")
            row = self._select_config_event(connection, event_id)
            if row is None:
                raise KeyError(f"配置事件不存在: {event_id}")
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
                    raise ConfigConflictError("配置 outbox relay claim 不属于当前 consumer")
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
                    raise ConfigConflictError("配置 outbox relay claim 不属于当前 consumer")
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
        self,
        *,
        config_domain: str,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[ConfigEventRecord, ...]:
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("配置事件分页参数无效")
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
                ORDER BY event_seq ASC
                LIMIT ?
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
            raise ValueError("配置事件游标不能为负数")
        first, _ = self.config_event_bounds(config_domain=config_domain)
        if after > 0 and first is not None and after < first - 1:
            raise ConfigEventCursorGoneError(
                config_domain=config_domain,
                after=after,
                first=first,
            )

    def prune_config_events(self, *, config_domain: str, retention_days: int = 30) -> int:
        if retention_days < 1:
            raise ValueError("配置事件保留天数必须大于 0")
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

    def get_config(self, config_key: str) -> WorkspaceConfigRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, config_version, payload_json
                FROM workspace_config
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[2]))
            if not isinstance(payload, dict):
                raise ValueError(f"Workspace SQLite 配置不是对象: key={config_key}")
            return WorkspaceConfigRecord(
                config_key=str(row[0]),
                config_version=int(row[1]),
                payload=payload,
            )
        finally:
            connection.close()

    def delete_config(self, config_key: str) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                "DELETE FROM workspace_config WHERE config_key = ?",
                (config_key,),
            )
        finally:
            connection.close()

    def append_activity(
        self,
        *,
        event_id: str,
        session_id: str,
        status: str,
        summary: str,
        occurred_at: str | None = None,
    ) -> WorkspaceActivityRecord:
        connection = self._database.connection()
        try:
            timestamp = occurred_at or utc_now_text()
            cursor = connection.execute(
                """
                INSERT INTO workspace_activity(
                    event_id, session_id, status, summary, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, session_id, status, summary, timestamp),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    """
                    SELECT event_seq, event_id, session_id, status, summary, occurred_at
                    FROM workspace_activity
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError(f"Workspace 活动事件去重后无法读取: {event_id}")
                return WorkspaceActivityRecord(
                    event_seq=int(existing[0]),
                    event_id=str(existing[1]),
                    session_id=str(existing[2]),
                    status=str(existing[3]),
                    summary=str(existing[4]),
                    occurred_at=str(existing[5]),
                )
            event_seq = int(cursor.lastrowid)
            return WorkspaceActivityRecord(
                event_seq=event_seq,
                event_id=event_id,
                session_id=session_id,
                status=status,
                summary=summary,
                occurred_at=timestamp,
            )
        finally:
            connection.close()

    def list_activity(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[WorkspaceActivityRecord, ...]:
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Workspace 活动事件分页参数无效")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT event_seq, event_id, session_id, status, summary, occurred_at
                FROM workspace_activity
                WHERE event_seq > ?
                ORDER BY event_seq ASC
                LIMIT ?
                """,
                (after, limit),
            ).fetchall()
            return tuple(
                WorkspaceActivityRecord(
                    event_seq=int(row[0]),
                    event_id=str(row[1]),
                    session_id=str(row[2]),
                    status=str(row[3]),
                    summary=str(row[4]),
                    occurred_at=str(row[5]),
                )
                for row in rows
            )
        finally:
            connection.close()

    def activity_bounds(self) -> tuple[int | None, int]:
        """返回当前保留事件的最小序号和 SQLite 已分配的最高序号。"""
        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT MIN(event_seq), MAX(event_seq) FROM workspace_activity"
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'workspace_activity'"
            ).fetchone()
            first = int(row[0]) if row[0] is not None else None
            latest = int(sequence_row[0]) if sequence_row is not None else 0
            return first, latest
        finally:
            connection.close()

    def prune_activity(self, *, retention_days: int = 30) -> int:
        if retention_days < 1:
            raise ValueError("Workspace 活动事件保留天数必须大于 0")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        connection = self._database.connection()
        try:
            cursor = connection.execute(
                "DELETE FROM workspace_activity WHERE occurred_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)
        finally:
            connection.close()

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> WorkspaceStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
