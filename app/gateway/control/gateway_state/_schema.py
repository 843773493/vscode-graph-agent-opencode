"""Gateway 全局状态库的 schema 迁移序列与一次性镜像行迁移。

承载 ``_GATEWAY_MIGRATIONS``（按序号逐条执行的建表/加列/索引 DDL，序号即
``SQLiteStateDatabase.schema_version`` 的权威来源）与一次性显式迁移
``_migrate_gateway_config_into_source_layers``。迁移 DDL 由本模块单点定义，
链路 mixin 不重复声明表结构，也不改动迁移序号。

错误分类沿用 gateway_state 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` CAS/并发冲突、``RuntimeError`` 事务后读取失败。
"""

from __future__ import annotations

import logging
import sqlite3

from app.core.config_sources import config_revision
from app.services.infrastructure.config.state import load_json_object

# 日志器名固定为 facade 模块名，搬迁前后同一条日志走同一 logger（语义零变更）。
logger = logging.getLogger("app.gateway.control.gateway_state")


def _migrate_gateway_config_into_source_layers(
    connection: sqlite3.Connection,
) -> None:
    """一次性显式迁移：把 legacy ``gateway_config`` 中的 config 来源层镜像行并入权威
    ``config_source_layers`` 后物理删除这些镜像行；控制面独有 KV
    （``workspace_registry_meta``/``gateway_connection_ids``）MUST NOT 被迁移或删除。

    - 只有镜像 key（``gateway_*_mutable_override``）参与迁移；其余 key 一律保留。
    - 只有当镜像 key 在 ``gateway_config`` 有行、``config_source_layers`` 无同 key 行
      时才补一条等价 present layer 行；同 key 两处都在时必须逐字等价，冲突 fail-closed。
    - 只删镜像 key 行，不 DROP 表、不迁移控制面 KV。
    """

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gateway_config'"
    ).fetchone()
    if exists is None:
        return
    mirror_keys = (
        "gateway_mutable_override",
        "gateway_local_mutable_override",
    )
    for config_key in mirror_keys:
        row = connection.execute(
            "SELECT config_version, payload_json, updated_at FROM gateway_config "
            "WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        if row is None:
            continue
        config_version = int(row[0])
        payload_json = str(row[1])
        updated_at = str(row[2])
        try:
            payload = load_json_object(
                payload_json, field=f"gateway_config payload (key={config_key})"
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "legacy gateway_config 镜像行 payload 非法，无法并入权威 "
                f"config_source_layers: key={config_key}: {error}"
            ) from error
        layer = connection.execute(
            "SELECT presence, payload_json FROM config_source_layers "
            "WHERE config_key = ?",
            (config_key,),
        ).fetchone()
        if layer is not None:
            layer_payload = (
                load_json_object(str(layer[1]), field="Gateway source layer payload")
                if layer[1] is not None
                else None
            )
            if str(layer[0]) != "present" or layer_payload != payload:
                raise RuntimeError(
                    "legacy gateway_config 镜像行与权威 config_source_layers 同 key 但"
                    f"语义不一致，拒绝静默择一: key={config_key}"
                )
        else:
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
                "legacy gateway_config 镜像行已一次性并入 config_source_layers: key=%s",
                config_key,
            )
        connection.execute(
            "DELETE FROM gateway_config WHERE config_key = ?", (config_key,)
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
    # 配置来源位置改以 VRN 表达（real path 不持久化）：丢弃旧真实路径列、新增 VRN 列。
    """
    ALTER TABLE config_source_layers DROP COLUMN source_path;
    ALTER TABLE config_source_layers DROP COLUMN backup_path;
    ALTER TABLE config_source_layers ADD COLUMN vrn TEXT;
    ALTER TABLE config_source_journal DROP COLUMN source_path;
    ALTER TABLE config_source_journal ADD COLUMN vrn TEXT;
    """,
    # 一次性显式迁移：把 legacy gateway_config 中的 config 来源层镜像行并入权威
    # config_source_layers 后物理删除这些镜像行；控制面独有 KV 必须留在该表。
    _migrate_gateway_config_into_source_layers,
)
