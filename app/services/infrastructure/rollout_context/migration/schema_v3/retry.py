"""重试只复用经过哈希与注入 key 认证的密文，不重写随机 nonce。"""

from __future__ import annotations

from pathlib import Path

from app.services.infrastructure.rollout_context.migration.artifacts import (
    require_safe_path,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.binding import (
    digest,
    read_prepared_audit,
    validate_bound_sql,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    DetailUpgrade,
    SchemaV3DetailCapability,
    SchemaV3UpgradeError,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.publication_reader import (
    read_audit_publication,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    protected_detail_relative_path,
)


def reuse_staged_ciphertexts(
    audit: Path, *, base_sql: str, source_fingerprint: str, target_fingerprint: str,
    original_files: dict[str, str], files: dict[str, bytes], details: tuple[DetailUpgrade, ...],
    checkpoint_ns: str, detail_capability: SchemaV3DetailCapability | None,
) -> bool:
    require_safe_path(audit)
    require_safe_path(audit / "prepared.json")
    metadata = None
    if (audit / "prepared.json").exists():
        metadata, sql = read_prepared_audit(audit, checkpoint_ns=checkpoint_ns)
        if (metadata["source_fingerprint"] != source_fingerprint
            or metadata["target_fingerprint"] != target_fingerprint
            or metadata["original_files"] != original_files
            or set(metadata["new_files"]) != set(files)
            or validate_bound_sql(sql, metadata) != base_sql):
            raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: retry 计划改变")
        for relative, expected in metadata["new_files"].items():
            if digest(read_audit_publication(audit / "staged" / relative)) != expected:
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: staged hash")
    for detail in details:
        if detail.protected_new_raw is None:
            continue
        relative = protected_detail_relative_path(detail.record.detail_ref).as_posix().removeprefix("rollout/")
        path = audit / "staged" / relative
        require_safe_path(path)
        if not path.exists():
            continue
        public = detail.record.relative_path.removeprefix("rollout/")
        if read_audit_publication(audit / "staged" / public) != files[public]:
            raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: retry typed manifest 改变")
        raw = read_audit_publication(path)
        if detail_capability is None:
            raise SchemaV3UpgradeError("schema-upgrade-protected-key-required")
        detail_capability.verify_prepared_detail(
            record=detail.record, manifest_bytes=files[public], protected_bytes=raw,
        )
        # 没有最终 prepared 的 staging 也只能逐份认证复用，不当作完整升级状态。
        files[relative] = raw
    if metadata is not None and metadata["new_files"] != {path: digest(raw) for path, raw in files.items()}:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: retry 文件集合改变")
    return metadata is not None
