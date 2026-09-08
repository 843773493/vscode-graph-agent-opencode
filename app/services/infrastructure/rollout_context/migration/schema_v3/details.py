"""schema2 detail envelope 到新 typed leaf；旧正文与 retention 不可覆盖。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.migration.artifacts import (
    read_regular,
    require_safe_path,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    DetailUpgrade,
    SchemaV3DetailCapability,
    SchemaV3UpgradeError,
    object_json,
    require_equal,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)

OLD_COLUMNS = (
    "detail_ref", "session_id", "checkpoint_ns", "assembly_id", "relative_path",
    "content_hash", "source_revision", "content_length", "redacted_stable_digest",
    "protection", "availability", "required", "sensitive", "status", "created_at", "gc_after",
)
OLD_FIELDS = frozenset({
    "format_version", "assembly_id", "content_length", "created_at", "detail",
    "detail_content_hash", "gc_after", "protected_body", "protection",
    "redacted_stable_digest", "sensitive", "source_revision",
})


def upgrade_detail(
    row: dict[str, object], *, rollout_root: Path, session_id: str,
    checkpoint_ns: str, purpose: tuple[str, str, str],
    detail_capability: SchemaV3DetailCapability | None,
) -> DetailUpgrade:
    if set(row) != set(OLD_COLUMNS):
        raise SchemaV3UpgradeError("schema-upgrade-source-mismatch: 非 schema2 detail registry")
    require_equal(row["session_id"], session_id, "detail session")
    require_equal(row["checkpoint_ns"], checkpoint_ns, "detail namespace")
    # 复用新 identity 的路径字符校验，但绝不把该值当新 detail identity 发布。
    old_ref = DetailRef(session_id, row["assembly_id"], row["detail_ref"])
    old_path = f"context-plan-details/{old_ref.assembly_id}/{old_ref.detail_id}.json"
    require_equal(row["relative_path"], "rollout/" + old_path, "旧 detail locator")
    for field in ("required", "sensitive"):
        if type(row[field]) is not int or row[field] not in (0, 1):
            raise SchemaV3UpgradeError(f"source-mismatch: detail {field} 非 SQLite 0/1")
    new_id = "detail-schema3-" + sha256_jcs({
        "owner": old_ref.to_dict(), "envelope_hash": row["content_hash"],
    }).rsplit(":", 1)[1]
    ref = DetailRef(session_id, old_ref.assembly_id, new_id)
    kind, retention, visibility = purpose
    record = DetailRecord(
        session_id=session_id, assembly_id=ref.assembly_id, detail_id=ref.detail_id,
        detail_kind=kind, retention_class=retention, visibility=visibility,
        relative_path=detail_relative_path(ref).as_posix(),
        content_hash=row["content_hash"], length=row["content_length"],
        source_revision=row["source_revision"], required=row["required"] == 1,
        sensitive=row["sensitive"] == 1, status=row["status"], expires_at=row["gc_after"],
        checkpoint_ns=checkpoint_ns, redacted_stable_digest=row["redacted_stable_digest"],
        protection=row["protection"], availability=row["availability"],
    )
    source_path = rollout_root / old_path
    require_safe_path(source_path)
    if record.status == "unavailable" and not source_path.exists():
        return DetailUpgrade(old_ref.detail_id, old_path, None, record, None, row["created_at"])
    raw = read_regular(source_path)
    value = object_json(raw, field="schema2 detail")
    if set(value) != OLD_FIELDS or canonical_json_bytes(value) != raw:
        raise SchemaV3UpgradeError("source-mismatch: 非冻结 schema2 detail envelope")
    require_equal(value["format_version"], 1, "旧 detail format_version")
    require_equal(sha256_jcs(value), row["content_hash"], "旧 detail envelope hash")
    # registry.created_at 是首次 SQL 注册时刻，envelope.created_at 是正文创建
    # 时刻；分别保留，不能添加历史 writer 从未承诺的跨时钟相等约束。
    for field in ("assembly_id", "source_revision", "content_length", "gc_after", "protection", "redacted_stable_digest"):
        require_equal(value[field], row[field], "旧 detail " + field)
    require_equal(value["sensitive"], record.sensitive, "旧 detail sensitive")
    require_equal(value["protected_body"], record.protection == "protected", "旧 detail protected_body")
    if record.sensitive:
        require_equal(value["detail_content_hash"], None, "旧敏感 detail 正文 hash")
        if value["detail"] != {"redacted": True, "redacted_stable_digest": record.redacted_stable_digest}:
            raise SchemaV3UpgradeError("source-mismatch: 旧敏感 detail marker 非法")
        if record.protection == "redacted":
            # TODO: 旧不可逆 marker 无 value_type；新类型合同未提供 unknown，
            # 不能用 object 或其他虚构值生成看似完整的新 marker。
            raise SchemaV3UpgradeError("schema-upgrade-redacted-type-required: 旧 marker 缺少可信 value_type")
        if detail_capability is None:
            raise SchemaV3UpgradeError("schema-upgrade-protected-key-required")
        protected_old_path = f"context-plan-details-protected/{old_ref.assembly_id}/{old_ref.detail_id}.bin"
        protected_old_raw = read_regular(rollout_root / protected_old_path)
        prepared = detail_capability.prepare_legacy_detail(
            legacy_envelope=value, legacy_blob=protected_old_raw,
            legacy_session_id=session_id, legacy_detail_id=old_ref.detail_id,
            target_ref=ref, detail_kind=kind, retention_class=retention,
            visibility=visibility, required=record.required, checkpoint_ns=checkpoint_ns,
            expected_digest=record.redacted_stable_digest,
        )
        _, parsed = parse_detail_payload(prepared.manifest_bytes, detail_ref=ref)
        if parsed != prepared.record or parsed != replace(record, content_hash=parsed.content_hash):
            raise SchemaV3UpgradeError("source-mismatch: protected capability 改变目标 manifest")
        if not isinstance(prepared.protected_bytes, bytes) or not prepared.protected_bytes:
            raise SchemaV3UpgradeError("source-mismatch: protected capability 未返回密文 bytes")
        return DetailUpgrade(old_ref.detail_id, old_path, raw, parsed, prepared.manifest_bytes,
            row["created_at"], protected_old_path, protected_old_raw, prepared.protected_bytes)
    else:
        require_equal(value["detail_content_hash"], sha256_jcs(value["detail"]), "旧 detail 正文 hash")
        require_equal(len(canonical_json_bytes(value["detail"])), record.length, "旧 detail 正文 length")
    payload = {
        "format_version": 2, "detail_ref": ref.to_dict(), "detail_kind": kind,
        "retention_class": retention, "visibility": visibility, "length": record.length,
        "source_revision": record.source_revision, "required": record.required,
        "sensitive": record.sensitive, "protection": record.protection,
        "expires_at": record.expires_at, "checkpoint_ns": checkpoint_ns,
        "detail_content_hash": value["detail_content_hash"],
        "redacted_stable_digest": record.redacted_stable_digest,
        "protected_body": False, "detail": value["detail"], "created_at": value["created_at"],
    }
    new_raw = canonical_json_bytes(payload)
    _, parsed = parse_detail_payload(new_raw, detail_ref=ref)
    record = replace(record, content_hash=parsed.content_hash)
    return DetailUpgrade(old_ref.detail_id, old_path, raw, record, new_raw, row["created_at"])
