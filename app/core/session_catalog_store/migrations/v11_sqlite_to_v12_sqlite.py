"""停写后转换固定来源 bf0f2e1d 的 catalog v11；原件由维护入口完整备份。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from app.core.session_catalog_store._schema import (
    _IDX_NODES_PARENT_DDL,
    _IDX_NODES_SESSION_LIST_DDL,
    _IDX_NODES_WORKSPACE_DDL,
    _NAVIGATION_REVISION_TRIGGER_DDLS,
    _NODE_COLUMNS,
    _NODES_TABLE_DDL,
)
from app.core.session_catalog_store.contracts import SessionCreationRecord
from app.core.session_catalog_store.creation_envelope import (
    compute_session_creation_preimage_hash,
    validate_session_creation_envelope,
)
from app.core.session_catalog_store.creation_record_rows import (
    session_creation_record_from_row,
)
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.core.session_catalog_store.schema_validation import require_catalog_schema
from app.core.session_catalog_store.sqlite_integrity import (
    verify_catalog_sqlite_integrity,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO


def _model(provider: str | None) -> str | None:
    return ModelReferenceDTO(provider_id=provider, model_id="default").canonical_json() if provider is not None else None


def _converted_creation(row: sqlite3.Row) -> SessionCreationRecord:
    record = session_creation_record_from_row(row)
    metadata = json.loads(record.session_metadata_json)
    if (record.state == "preparing" or record.purpose != "normal" or record.request_intent_json is not None
            or not isinstance(metadata, dict) or set(metadata) != {
                "kind", "delegation", "generation_origin", "current_agent_id", "current_provider_id", "context_source_session_id",
            }):
        raise ValueError("catalog v11 转换只支持已结算的普通创建意图；保留原件，不重解释待恢复或 fork 意图")
    def preimage(value: dict[str, object]) -> str:
        return compute_session_creation_preimage_hash(
            workspace_id=record.workspace_id, parent_node_id=record.parent_node_id,
            title=record.display_name, title_source=record.title_source, purpose=record.purpose,
            graph_binding=record.graph_binding, session_metadata=value,
        )
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if canonical(metadata) != record.session_metadata_json or preimage(metadata) != record.preimage_hash:
        raise ValueError("catalog v11 原创建意图 preimage 不一致，保留原件")
    metadata["current_model_json"] = _model(metadata.pop("current_provider_id"))
    converted = replace(record, session_metadata_json=canonical(metadata), preimage_hash=preimage(metadata))
    validate_session_creation_envelope(converted)
    return converted


def validate_catalog_v11(connection: sqlite3.Connection) -> None:
    """首写前确认来源、结算边界及全部可确定转换。"""
    connection.row_factory = sqlite3.Row
    _register_session_list_functions(connection)
    if connection.execute("PRAGMA user_version").fetchone()[0] != 11:
        raise ValueError("本转换只支持 catalog v11")
    require_catalog_schema(connection, database_path=Path("session-catalog.sqlite"), current=11, database_label="conversion source")
    verify_catalog_sqlite_integrity(connection, database_path=Path("session-catalog.sqlite"), database_label="conversion source")
    if (connection.execute("SELECT 1 FROM fork_source_captures LIMIT 1").fetchone()
            or connection.execute("SELECT 1 FROM nodes WHERE state<>'active' LIMIT 1").fetchone()
            or connection.execute("SELECT 1 FROM subtree_delete_records WHERE state NOT IN ('completed','aborted') LIMIT 1").fetchone()):
        raise ValueError("catalog v11 含 fork capture 或未完成删除，须保留原件并先确认其恢复合同")
    for row in connection.execute("SELECT * FROM session_creation_records"):
        _converted_creation(row)
    for row in connection.execute("SELECT current_provider_id FROM nodes"):
        _model(row[0])


def upgrade_catalog_v11(connection: sqlite3.Connection) -> None:
    """只转换维护入口提供的独立候选；原创建意图和 hash 保存在完整原库备份。"""
    validate_catalog_v11(connection)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER nodes_navigation_revision_{name}")
        connection.execute(_NODES_TABLE_DDL.replace("nodes (", "nodes_v12 (", 1))
        for row in connection.execute("SELECT * FROM nodes"):
            values = [row[name] if name != "current_model_json" else _model(row["current_provider_id"])
                      for name in _NODE_COLUMNS.split(", ")]
            connection.execute(f"INSERT INTO nodes_v12({_NODE_COLUMNS}) VALUES ({','.join('?' for _ in values)})", values)
        connection.execute("DROP TABLE nodes")
        connection.execute("ALTER TABLE nodes_v12 RENAME TO nodes")
        for ddl in (_IDX_NODES_PARENT_DDL, _IDX_NODES_WORKSPACE_DDL, _IDX_NODES_SESSION_LIST_DDL, *_NAVIGATION_REVISION_TRIGGER_DDLS):
            connection.execute(ddl)
        for row in connection.execute("SELECT * FROM session_creation_records").fetchall():
            record = _converted_creation(row)
            connection.execute("UPDATE session_creation_records SET session_metadata_json=?,preimage_hash=? "
                               "WHERE session_creation_idempotency_key=?",
                               (record.session_metadata_json, record.preimage_hash, record.session_creation_idempotency_key))
        connection.execute("PRAGMA user_version=12")
        require_catalog_schema(connection, database_path=Path("session-catalog.sqlite"), current=12, database_label="converted catalog")
        verify_catalog_sqlite_integrity(connection, database_path=Path("session-catalog.sqlite"), database_label="converted catalog")
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
