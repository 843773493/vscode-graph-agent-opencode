"""Workspace 控制面 SQLite 状态库的装配点与 lifecycle/config-kv 方法族。

WorkspaceStateStore 组合 workspace activity、config event、config source 以及本模块
导入的 apply / snapshot / restart 各族 mixin；本模块另承载 _WORKSPACE_MIGRATIONS DDL、
一次性来源层迁移回调与 lifecycle / config-kv 方法。"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from app.core.config_sources import config_revision
from app.core.sqlite_state import (
    SQLiteDiagnostics,
    SQLiteStateDatabase,
    utc_now_text,
)
from app.services.infrastructure.config.state import (
    build_secret_binding_summary,
    dump_json,
    load_json_object,
    migrate_legacy_secret_payload,
)
from app.services.infrastructure.workspace_activity.workspace_activity import (
    WorkspaceActivityCursorGoneError,
    WorkspaceActivityMixin,
    WorkspaceActivityRecord,
    WorkspaceActivityService,
)
from app.services.infrastructure.workspace_config_events.workspace_config_events import (
    WorkspaceConfigEventMixin,
)
from app.services.infrastructure.workspace_config_source.workspace_config_source import (
    WorkspaceConfigSourceMixin,
)
from app.services.infrastructure.workspace_state_store_apply import (
    WorkspaceStateStoreApplyMixin,
)
from app.services.infrastructure.workspace_state_store_pending import (
    WorkspaceStateStorePendingMixin,
)
from app.services.infrastructure.workspace_state_store_restart import (
    WorkspaceStateStoreRestartMixin,
)
from app.services.infrastructure.workspace_state_store_snapshot import (
    WorkspaceStateStoreSnapshotMixin,
)

__all__ = [
    "WorkspaceActivityCursorGoneError",
    "WorkspaceActivityRecord",
    "WorkspaceActivityService",
    "WorkspaceStateStore",
]

logger = logging.getLogger(__name__)


def _migrate_workspace_config_into_source_layers(
    connection: sqlite3.Connection,
) -> None:
    """一次性显式迁移：把 legacy ``workspace_config`` 残留行并入权威 layer 表后删表。

    - 只有当 ``workspace_config`` 有行、``config_source_layers`` 无同 key 行时，
      才补一条等价 present layer 行（``layer_revision``/``source_generation``/``vrn``
      按下标初始化，digest 由 payload 计算）——这是运行时覆盖读从旧表迁到权威表的
      唯一活兼容来源，必须还原语义。
    - 同 key 两表都在时必须逐字等价，冲突即 fail-closed，绝不静默择一。
    - 迁移后物理 DROP 该表；不双读、不留别名、不扫描重建。
    """

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_config'"
    ).fetchone()
    if exists is None:
        return
    rows = connection.execute(
        "SELECT config_key, config_version, payload_json, updated_at "
        "FROM workspace_config"
    ).fetchall()
    for row in rows:
        config_key = str(row[0])
        config_version = int(row[1])
        payload_json = str(row[2])
        updated_at = str(row[3])
        try:
            payload = load_json_object(
                payload_json, field=f"workspace_config payload (key={config_key})"
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "legacy workspace_config 迁移失败：payload 非法，无法并入 "
                f"config_source_layers: key={config_key}: {error}"
            ) from error
        layer = connection.execute(
            "SELECT presence, payload_json FROM config_source_layers "
            "WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        if layer is not None:
            layer_payload = (
                load_json_object(str(layer[1]), field="source layer payload")
                if layer[1] is not None
                else None
            )
            if str(layer[0]) != "present" or layer_payload != payload:
                raise RuntimeError(
                    "legacy workspace_config 与权威 config_source_layers 同 key 但"
                    f"语义不一致，拒绝静默择一: key={config_key}"
                )
            continue
        connection.execute(
            """
            INSERT INTO config_source_layers(
                config_key, vrn, presence, config_version, payload_json,
                layer_revision, layer_digest, source_generation, previous_digest,
                updated_at, previous_payload_json
            ) VALUES (?, NULL, 'present', ?, ?, 1, ?, 1, NULL, ?, NULL)
            """,
            (
                config_key,
                config_version,
                payload_json,
                config_revision(payload),
                updated_at,
            ),
        )
        logger.warning(
            "legacy workspace_config 行已一次性并入 config_source_layers: key=%s",
            config_key,
        )
    connection.execute("DROP TABLE workspace_config")


_WORKSPACE_MIGRATIONS = (
    """
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
    # 配置来源位置改以 VRN 表达（real path 不持久化）：丢弃旧真实路径列、新增 VRN 列。
    # 旧值一律不迁移（列本身即被删除）；新列可空，sqlite 层不可寻址即写 NULL。
    """
    ALTER TABLE config_source_layers DROP COLUMN source_path;
    ALTER TABLE config_source_layers DROP COLUMN backup_path;
    ALTER TABLE config_source_layers ADD COLUMN vrn TEXT;
    ALTER TABLE config_source_journal DROP COLUMN source_path;
    ALTER TABLE config_source_journal ADD COLUMN vrn TEXT;
    """,
    # 一次性显式迁移：把 legacy workspace_config 残留行并入权威 config_source_layers
    # 后物理 DROP 该镜像表（彻底根除表级双轨）。遇同 key 语义冲突 fail-closed。
    _migrate_workspace_config_into_source_layers,
)


class WorkspaceStateStore(
    WorkspaceActivityMixin,
    WorkspaceConfigEventMixin,
    WorkspaceConfigSourceMixin,
    WorkspaceStateStoreApplyMixin,
    WorkspaceStateStoreSnapshotMixin,
    WorkspaceStateStorePendingMixin,
    WorkspaceStateStoreRestartMixin,
):
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

    def migrate_legacy_config_secrets(self, config_key: str) -> tuple[str, ...]:
        """升级权威 layer 表中的秘密引用。

        字面量 key 保留原文（已受支持）；只有旧版本写入的不可逆
        ``literal-sha256:`` 摘要才会被记为阻断路径。
        """

        connection = self._database.connection()
        blocked: set[str] = set()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
        """升级旧 active payload，并对无法恢复的旧摘要秘密建立恢复态。"""

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
                    "旧 active snapshot 含无法恢复的秘密摘要，需重新导入引用",
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

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> WorkspaceStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
