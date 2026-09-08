"""稳定重试 identity 与 SQL journal 对完整 artifact manifest 的内容绑定。"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3UpgradeError,
    object_json,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.publication_reader import (
    read_audit_publication,
)

_PREFIX = "-- schema-v3-artifacts-sha256:"
_FIELDS = {"audit_id", "source_fingerprint", "target_fingerprint", "migration_checksum", "original_files", "new_files", "checkpoint_ns"}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def audit_identity(source: str, original_files: dict[str, str], base_sql: str) -> str:
    # 新密文 nonce 不参与重试定位；完整 new_files 由提交的 SQL checksum 绑定。
    return "schema2-to-3-" + digest(canonical_json_bytes({
        "source": source, "original_files": original_files, "sql": digest(base_sql.encode()),
    }))


def bind_sql(base_sql: str, *, source_fingerprint: str, target_fingerprint: str,
             original_files: dict[str, str], new_files: dict[str, str], checkpoint_ns: str) -> str:
    binding = canonical_json_bytes({
        "source_fingerprint": source_fingerprint, "target_fingerprint": target_fingerprint,
        "original_files": original_files, "new_files": new_files, "checkpoint_ns": checkpoint_ns,
    })
    return _PREFIX + digest(binding) + "\n" + base_sql


def validate_bound_sql(sql: str, value: dict[str, object]) -> str:
    prefix, separator, base_sql = sql.partition("\n")
    if not separator or not prefix.startswith(_PREFIX) or bind_sql(
        base_sql, **{field: value[field] for field in (
            "source_fingerprint", "target_fingerprint", "original_files", "new_files", "checkpoint_ns",
        )},
    ) != sql:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: SQL artifact manifest binding")
    return base_sql


def read_prepared_audit(audit: Path, *, checkpoint_ns: str) -> tuple[dict[str, object], str]:
    value = object_json(read_audit_publication(audit / "prepared.json"), field="prepared audit")
    if set(value) != _FIELDS or value["audit_id"] != audit.name or value["checkpoint_ns"] != checkpoint_ns:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: prepared identity/namespace")
    for field in ("original_files", "new_files"):
        if not isinstance(value[field], dict):
            raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: file manifest 非 object")
        for relative, expected in value[field].items():
            parsed = PurePosixPath(relative)
            if not parsed.parts or parsed.is_absolute() or ".." in parsed.parts or "\\" in relative or parsed.as_posix() != relative:
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: 路径越界")
            if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: 非 SHA-256 file hash")
    sql = read_audit_publication(audit / "migration.sql").decode("utf-8")
    if digest(sql.encode()) != value["migration_checksum"]:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: SQL checksum")
    base_sql = validate_bound_sql(sql, value)
    if audit_identity(value["source_fingerprint"], value["original_files"], base_sql) != audit.name:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: audit seed hash")
    return value, sql
