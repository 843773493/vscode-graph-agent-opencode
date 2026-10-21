"""assembly共享内容21→22的显式升级；正文转换仍由唯一assembly owner实现。"""

from __future__ import annotations

import json
import sqlite3

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
    store_snapshot,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    persisted_runtime_seal_hash,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ASSEMBLY_CONTENT_SCHEMA_SQL,
)


def migrate_assembly_content(connection: sqlite3.Connection) -> None:
    """原迁移事务内转引用，核对完整历史JCS与seal，不改变历史请求或canonical事实。"""
    rows = connection.execute(
        "SELECT a.assembly_id,a.snapshot_json,a.detail_ref,p.seal_input_hash,p.seal_hash "
        "FROM context_assemblies a LEFT JOIN context_plans p "
        "ON p.session_id=a.session_id AND p.thread_id=a.thread_id AND p.plan_id=a.plan_id",
    ).fetchall()
    for assembly_id, raw, detail, input_hash, seal_hash in rows:
        snapshot = json.loads(raw)
        ContextAssemblySnapshot.from_dict(snapshot).validate_hashes()
        if persisted_runtime_seal_hash(raw, detail, input_hash) != seal_hash:
            raise ValueError(f"source-mismatch: assembly migration 历史seal不一致: {assembly_id}")
        encoded = store_snapshot(connection, snapshot)
        rebuilt = canonical_json_bytes(load_snapshot(connection, encoded)).decode("utf-8")
        if rebuilt != raw:
            raise ValueError(f"source-mismatch: assembly migration 历史JCS改变: {assembly_id}")
        connection.execute("UPDATE context_assemblies SET snapshot_json=? WHERE assembly_id=?", (encoded, assembly_id))

__all__ = ["ASSEMBLY_CONTENT_SCHEMA_SQL", "migrate_assembly_content"]
