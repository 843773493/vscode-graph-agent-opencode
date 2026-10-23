"""创建 journal 的唯一行读取与不可变投影，供创建和 fork capture owner 共用。"""

from __future__ import annotations

import sqlite3

from app.core.identifier import validate_session_id
from app.core.session_catalog_store._schema import _CREATION_RECORD_COLUMNS
from app.core.session_catalog_store.contracts import SessionCreationRecord
from app.core.session_catalog_store.validators import (
    validate_operation_journal_metadata,
)


def fetch_session_creation_record_row(
    connection: sqlite3.Connection,
    idempotency_key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT {_CREATION_RECORD_COLUMNS} FROM session_creation_records "
        "WHERE session_creation_idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()


def session_creation_record_from_row(row: sqlite3.Row) -> SessionCreationRecord:
    validate_operation_journal_metadata(
        idempotency_key=row["session_creation_idempotency_key"], state=row["state"],
        abort_reason=row["abort_reason"], record_created_at=row["record_created_at"],
        record_updated_at=row["record_updated_at"],
    )
    parent_id, parent_revision = row["parent_node_id"], row["parent_revision"]
    if parent_id is None:
        if parent_revision is not None:
            raise ValueError("根级 creation record 不得携带 parent_revision")
    else:
        validate_session_id(parent_id)
        if not isinstance(parent_revision, int) or parent_revision < 1:
            raise ValueError("creation record 必须保留正整数冻结 parent_revision")
    return SessionCreationRecord(
        session_creation_idempotency_key=str(
            row["session_creation_idempotency_key"]
        ),
        session_id=str(row["session_id"]),
        main_thread_id=str(row["main_thread_id"]),
        workspace_id=str(row["workspace_id"]),
        parent_node_id=(
            str(row["parent_node_id"])
            if row["parent_node_id"] is not None
            else None
        ),
        display_name=str(row["display_name"]),
        title_source=str(row["title_source"]),
        purpose=str(row["purpose"]),
        graph_id=str(row["graph_id"]),
        graph_revision=int(row["graph_revision"]),
        graph_schema_hash=str(row["graph_schema_hash"]),
        capability_profile_hash=str(row["capability_profile_hash"]),
        session_metadata_json=str(row["session_metadata_json"]),
        request_intent_json=(
            str(row["request_intent_json"])
            if row["request_intent_json"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
        storage_relative_locator=str(row["storage_relative_locator"]),
        preimage_hash=str(row["preimage_hash"]),
        parent_revision=(
            int(row["parent_revision"])
            if row["parent_revision"] is not None
            else None
        ),
        state=str(row["state"]),
        abort_reason=(
            str(row["abort_reason"]) if row["abort_reason"] is not None else None
        ),
        record_created_at=str(row["record_created_at"]),
        record_updated_at=str(row["record_updated_at"]),
        fork_delivery_request_id=row["fork_delivery_request_id"],
    )
