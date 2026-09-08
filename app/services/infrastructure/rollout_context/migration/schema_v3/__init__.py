"""显式 schema2→3 artifact 升级入口；普通 runtime 不得导入本包。"""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from app.services.infrastructure.rollout_context.migration.artifacts import (
    read_regular,
    require_safe_path,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.binding import (
    audit_identity,
    bind_sql,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.details import (
    OLD_COLUMNS,
    upgrade_detail,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.journal import (
    PreparedSchemaV3Upgrade,
    digest,
    persist_prepared,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3DetailCapability,
    SchemaV3UpgradeError,
    rows,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.recovery import (
    resume_schema_v3_upgrade_audits,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.retry import (
    reuse_staged_ciphertexts,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.snapshots import (
    detail_purposes,
    load_snapshot,
    typed_snapshot,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.sql import (
    build_sql,
    fingerprint,
    validate_target,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    protected_detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.maintenance import (
    RolloutStorageMaintenanceMixin,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    execute_atomic_schema_sql,
)


def prepare_schema_v3_upgrade(
    connection: sqlite3.Connection, *, rollout_root: Path, session_id: str,
    checkpoint_ns: str, detail_capability: SchemaV3DetailCapability | None,
) -> PreparedSchemaV3Upgrade:
    """锁由显式调用方持有；预检源与内存中的完整 target 后才写备份/暂存。"""
    require_safe_path(rollout_root)
    if connection.in_transaction:
        raise SchemaV3UpgradeError("schema-upgrade-transaction-active: prepare 不能接收写事务")
    database = connection.execute("PRAGMA database_list").fetchall()
    if len(database) != 1 or Path(database[0][2]) != rollout_root / "index.sqlite":
        raise SchemaV3UpgradeError("source-mismatch: connection 不属于 rollout_root")
    version = connection.execute("SELECT schema_version,rollout_format_version FROM database_meta WHERE singleton_id=1").fetchone()
    if tuple(version or ()) != (2, 2):
        raise SchemaV3UpgradeError("schema-upgrade-source-mismatch: 只接受 rollout format2/schema2")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(context_plan_details)"))
    if columns != OLD_COLUMNS:
        raise SchemaV3UpgradeError("schema-upgrade-source-mismatch: 非冻结 schema2 detail 表")
    # 同进程打开/关闭第二个 SQLite fd 会释放 POSIX record locks；只校验
    # 路径元数据，数据库内容始终由已绑定的 SQLite connection 读取。
    index_path = rollout_root / "index.sqlite"
    require_safe_path(index_path)
    info = index_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SchemaV3UpgradeError("source-mismatch: index 必须是无硬链接的普通文件")
    read_regular(rollout_root / "rollout.jsonl")
    state = connection.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()[0]
    if state == "recovery_required":
        # None 仅用于只读失败预检；发布前由主线复核最终 bound SQL checksum。
        RolloutStorageMaintenanceMixin._validate_schema_state(
            connection, allow_older_schema=True,
            pending_retry=(2, 3, "v3_typed_detail_identity", None),
        )
    else:
        RolloutStorageMaintenanceMixin._validate_schema_state(connection, allow_older_schema=True)
    detail_rows = rows(connection, "context_plan_details")
    if any(row["protection"] == "protected" for row in detail_rows):
        if detail_capability is None:
            raise SchemaV3UpgradeError("schema-upgrade-protected-key-required: 必须显式注入既有 key")
        detail_capability.require_protected_key()
    RolloutStorageMaintenanceMixin._validate_v2_commit_offsets(connection, rollout_root / "rollout.jsonl")
    source_fingerprint = fingerprint(connection)
    assemblies = rows(connection, "context_assemblies")
    legacy = {row["assembly_id"]: load_snapshot(row, session_id) for row in assemblies}
    purposes = detail_purposes(assemblies, legacy)
    details = tuple(upgrade_detail(
        row, rollout_root=rollout_root, session_id=session_id, checkpoint_ns=row["checkpoint_ns"],
        purpose=purposes.get(row["detail_ref"], ("legacy_unbound", "legacy_audit", "private")),
        detail_capability=detail_capability,
    ) for row in detail_rows)
    detail_map = {detail.old_id: detail.record.detail_ref for detail in details}
    snapshots = {key: typed_snapshot(value, detail_map) for key, value in legacy.items()}
    script = build_sql(connection, details, snapshots)
    with closing(sqlite3.connect(":memory:")) as target:
        connection.backup(target)
        target.execute("BEGIN IMMEDIATE")
        execute_atomic_schema_sql(target, script)
        validate_target(target, checkpoint_ns=checkpoint_ns)
        target_fingerprint = fingerprint(target)
    original_files = {"rollout.jsonl": digest(read_regular(rollout_root / "rollout.jsonl"))}
    files: dict[str, bytes] = {}
    for detail in details:
        if detail.old_raw is not None:
            original_files[detail.old_path] = digest(detail.old_raw)
        if detail.new_raw is not None:
            relative = detail.record.relative_path.removeprefix("rollout/")
            files[relative] = detail.new_raw
        if detail.protected_old_raw is not None:
            if detail.protected_old_path is None:
                raise SchemaV3UpgradeError("source-mismatch: protected 原件 locator 缺失")
            original_files[detail.protected_old_path] = digest(detail.protected_old_raw)
        if detail.protected_new_raw is not None:
            relative = protected_detail_relative_path(detail.record.detail_ref).as_posix().removeprefix("rollout/")
            files[relative] = detail.protected_new_raw
    audit_id = audit_identity(source_fingerprint, original_files, script)
    retry = reuse_staged_ciphertexts(
        rollout_root / "schema-upgrade-v3" / audit_id, base_sql=script,
        source_fingerprint=source_fingerprint, target_fingerprint=target_fingerprint,
        original_files=original_files, files=files, details=details,
        checkpoint_ns=checkpoint_ns, detail_capability=detail_capability,
    )
    new_files = {relative: digest(raw) for relative, raw in files.items()}
    script = bind_sql(script, source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint, original_files=original_files,
        new_files=new_files, checkpoint_ns=checkpoint_ns)
    prepared = PreparedSchemaV3Upgrade(
        script, audit_id, rollout_root, checkpoint_ns, source_fingerprint, target_fingerprint,
        original_files, new_files, connection,
    )
    # 首次 prepare 不允许借用其他运行的目标 leaf，即使字节碰巧相同。
    for relative in files:
        path = rollout_root / relative
        require_safe_path(path)
        if path.exists() and not retry:
            raise SchemaV3UpgradeError("schema-upgrade-artifact-conflict: target leaf 已存在")
    persist_prepared(connection, prepared, files)
    return prepared


__all__ = ["PreparedSchemaV3Upgrade", "SchemaV3DetailCapability", "SchemaV3UpgradeError", "prepare_schema_v3_upgrade", "resume_schema_v3_upgrade_audits"]
