"""既有 v2 库的一次性 activation schema 升级（9.2）。

本模块只做「建表 + 记录 quarantine」。它**绝不**从当前文件、URI、middleware
状态或 generic read 记录补造 activation provenance/lineage：既有 assembly 在
升级前没有 activation 事实，因此逐条写入 ``resource_activation_migration_losses``
作为显式 loss/quarantine，而不是伪造一份默认 snapshot。

迁移是幂等的：已升级的库再次调用只返回既有版本与 loss 集合，不会重复建表、
不会覆盖已提交的 activation 行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage.resource_activation_schema import (
    RESOURCE_ACTIVATION_MIGRATION_NAME,
    RESOURCE_ACTIVATION_SCHEMA_SQL,
    RESOURCE_ACTIVATION_SCHEMA_VERSION,
    create_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
    ResourceActivationStoreError,
)

MIGRATION_LOSS_REASON_MISSING_ACTIVATION = "missing-activation-provenance"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checksum() -> str:
    return hashlib.sha256(RESOURCE_ACTIVATION_SCHEMA_SQL.encode("utf-8")).hexdigest()


def _read_version(connection: sqlite3.Connection) -> int | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'resource_activation_schema_state'"
    ).fetchone()
    if exists is None:
        return None
    row = connection.execute(
        "SELECT activation_schema_version FROM resource_activation_schema_state "
        "WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise ResourceActivationStoreError(
            "resource-activation-schema-conflict",
            "activation schema 缺少版本 marker row",
        )
    return int(row[0])


def upgrade_resource_activation_schema(
    connection: sqlite3.Connection,
) -> tuple[int, tuple[str, ...]]:
    """在调用方事务内升级既有 v2 库；返回 (版本, 本次 quarantine loss id)。

    调用方必须已持有 rollout owner 写锁并已开启事务。函数不提交。
    """

    if not connection.in_transaction:
        raise ResourceActivationStoreError(
            "resource-activation-schema-invalid",
            "activation upgrade 必须在既有事务内执行",
        )
    existing_version = _read_version(connection)
    if existing_version is not None:
        if existing_version != RESOURCE_ACTIVATION_SCHEMA_VERSION:
            raise ResourceActivationStoreError(
                "resource-activation-schema-conflict",
                f"activation schema 版本 {existing_version} 不能降级/重写",
            )
        return existing_version, ()
    session_row = connection.execute(
        "SELECT session_id FROM database_meta WHERE singleton_id = 1"
    ).fetchone()
    if session_row is None:
        raise ResourceActivationStoreError(
            "resource-activation-schema-invalid", "rollout database_meta 缺失"
        )
    owner_session_id = str(session_row[0])
    create_resource_activation_schema(connection)
    timestamp = _now()
    connection.execute(
        "INSERT INTO resource_activation_schema_state"
        "(singleton_id, activation_schema_version, updated_at) VALUES (1, ?, ?)",
        (RESOURCE_ACTIVATION_SCHEMA_VERSION, timestamp),
    )
    connection.execute(
        "INSERT INTO resource_activation_schema_migrations"
        "(from_version, to_version, migration_name, migration_checksum, status, "
        "started_at, completed_at) VALUES (0, ?, ?, ?, 'completed', ?, ?)",
        (
            RESOURCE_ACTIVATION_SCHEMA_VERSION,
            RESOURCE_ACTIVATION_MIGRATION_NAME,
            _checksum(),
            timestamp,
            timestamp,
        ),
    )
    # 既有 assembly 在升级前没有任何 activation 事实。逐条 quarantine，
    # 明确记录「缺失 provenance」，绝不从当前状态补造 snapshot/binding。
    loss_ids: list[str] = []
    rows = connection.execute(
        "SELECT assembly_id, session_id, turn_id, execution_id, plan_id, plan_hash "
        "FROM context_assemblies ORDER BY created_at, assembly_id"
    ).fetchall()
    for row in rows:
        assembly_id = str(row[0])
        detail = {
            "schema": "resource-activation-migration-loss:v1",
            "assembly_id": assembly_id,
            "session_id": str(row[1]),
            "turn_id": str(row[2]),
            "execution_id": str(row[3]),
            "plan_id": str(row[4]),
            "plan_hash": str(row[5]),
            "reason": (
                "assembly 早于 activation schema，缺失可核对的 provenance/lineage；"
                "迁移不读取当前文件/URI/middleware/generic read 补造"
            ),
        }
        loss_id = "loss:" + hashlib.sha256(
            canonical_json_bytes(detail)
        ).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO resource_activation_migration_losses"
            "(loss_id, session_id, thread_id, reason_code, assembly_id, plan_id, "
            "detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                loss_id,
                owner_session_id,
                "",
                MIGRATION_LOSS_REASON_MISSING_ACTIVATION,
                assembly_id,
                str(row[4]),
                canonical_json_bytes(detail).decode("utf-8"),
                timestamp,
            ),
        )
        loss_ids.append(loss_id)
    return RESOURCE_ACTIVATION_SCHEMA_VERSION, tuple(loss_ids)


def read_migration_losses(
    connection: sqlite3.Connection, *, session_id: str
) -> tuple[dict[str, object], ...]:
    """读取 quarantine loss；调用方负责显式展示，不做静默吸收。"""

    rows = connection.execute(
        "SELECT loss_id, reason_code, assembly_id, plan_id, detail_json "
        "FROM resource_activation_migration_losses WHERE session_id = ? "
        "ORDER BY created_at, loss_id",
        (session_id,),
    ).fetchall()
    return tuple(
        {
            "loss_id": str(row[0]),
            "reason_code": str(row[1]),
            "assembly_id": None if row[2] is None else str(row[2]),
            "plan_id": None if row[3] is None else str(row[3]),
            "detail": json.loads(str(row[4])),
        }
        for row in rows
    )


__all__ = [
    "MIGRATION_LOSS_REASON_MISSING_ACTIVATION",
    "read_migration_losses",
    "upgrade_resource_activation_schema",
]
