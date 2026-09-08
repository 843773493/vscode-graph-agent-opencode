"""将冻结 schema3 fixture 的 typed detail 显式编码为既定 schema2 输入。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.migration.schema_v3.legacy_hash import (
    legacy_hashes,
)
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    OLD_DETAIL_DDL,
)
from tests.integration.backend.sessions.rollout_context.schema_v4_helpers import (
    Schema3Artifact,
)


def freeze_schema2_details(source: Schema3Artifact) -> None:
    """fixture 已具有真实 schema3 结构；只换成冻结 schema2 的 detail DDL/envelope。"""
    with closing(sqlite3.connect(source.index)) as connection, connection:
        connection.row_factory = sqlite3.Row
        details = [dict(row) for row in connection.execute("SELECT * FROM context_plan_details")]
        mapping = {row["detail_ref"]: row["detail_id"] for row in details}
        connection.execute("DROP TABLE context_plan_details")
        connection.execute(OLD_DETAIL_DDL)
        for row in details:
            typed_path = source.root / row["relative_path"].removeprefix("rollout/")
            current = json.loads(typed_path.read_bytes())
            old = {key: current[key] for key in (
                "detail", "created_at", "detail_content_hash", "protection",
                "redacted_stable_digest", "sensitive", "source_revision", "protected_body",
            )}
            old.update(format_version=1, assembly_id=row["assembly_id"], content_length=row["content_length"], gc_after=row["expires_at"])
            relative = f"rollout/context-plan-details/{row['assembly_id']}/{row['detail_id']}.json"
            old_path = source.root / relative.removeprefix("rollout/")
            old_path.write_bytes(canonical_json_bytes(old))
            typed_path.unlink()
            connection.execute("INSERT INTO context_plan_details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row["detail_id"], source.session_id, row["checkpoint_ns"], row["assembly_id"], relative,
                sha256_jcs(old), row["source_revision"], row["content_length"], row["redacted_stable_digest"],
                row["protection"], row["availability"], row["required"], row["sensitive"], row["status"], row["created_at"], row["expires_at"],
            ))
        for table in ("context_assemblies", "assembly_item_refs", "context_assembly_selections"):
            for key, old_id in mapping.items():
                connection.execute(f"UPDATE {table} SET detail_ref=? WHERE detail_ref=?", (old_id, key))
        for row in list(connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies")):
            value = json.loads(row["snapshot_json"])
            for entry in value["selection"]:
                if entry["detail_ref"] is not None:
                    entry["detail_ref"] = entry["detail_ref"]["detail_id"]
                if entry["ref"]["ref_type"] != "tool_set":
                    entry["ref"]["detail_ref"] = entry["detail_ref"]
            for ref in value["refs"]:
                ref["detail_ref"] = next((entry["detail_ref"] for entry in value["selection"] if entry["ref"]["ref_id"] == ref["ref_id"] and entry["ref"]["ref_type"] == ref["ref_type"]), None)
            value["plan_hash"], value["request_hash"] = legacy_hashes(value)
            connection.execute("UPDATE context_assemblies SET snapshot_json=?,plan_hash=?,request_hash=? WHERE assembly_id=?", (
                canonical_json_bytes(value).decode(), value["plan_hash"], value["request_hash"], row["assembly_id"],
            ))
        connection.execute("UPDATE database_meta SET schema_version=2")
        connection.execute("UPDATE schema_migrations SET to_version=2,migration_name='rollout_sqlite_v2',migration_checksum=?", (
            hashlib.sha256(b"rollout_sqlite_v2").hexdigest(),
        ))
