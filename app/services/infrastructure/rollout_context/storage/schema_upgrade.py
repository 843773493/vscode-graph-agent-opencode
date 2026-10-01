"""显式 v2 SQLite 版本升级；不得修改 canonical JSONL 或隐式修补业务数据。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from app.services.infrastructure.rollout_context.storage import schema
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


class SchemaUpgradePlan(Protocol):
    """只读准备的 SQL 升级及同事务验证，不拥有连接或提交。"""

    @property
    def migration_sql(self) -> str: ...

    def verify_migrated(self, connection: sqlite3.Connection) -> None: ...


class SchemaUpgradeArtifacts(SchemaUpgradePlan, Protocol):
    """显式升级注入的 artifact 事务参与者；storage 不解析旧文件。"""

    @property
    def migration_sql(self) -> str: ...

    @property
    def audit_id(self) -> str: ...

    def publish(self) -> None: ...

    def verify_migrated(self, connection: sqlite3.Connection) -> None: ...

    def verify_committed(self, connection: sqlite3.Connection) -> None: ...

    def discard_uncommitted(self, connection: sqlite3.Connection) -> None: ...


def validate_schema_journal(
    connection: sqlite3.Connection, schema_version: int, *,
    pending_retry: tuple[int, int, str, str | None] | None = None,
) -> None:
    """保留失败审计；仅同一合同的后续成功或显式重试可以跨过失败记录。"""
    rows = connection.execute(
        "SELECT from_version, to_version, migration_name, migration_checksum, status, completed_at "
        "FROM schema_migrations ORDER BY migration_id",
    ).fetchall()
    if not rows:
        raise RuntimeError("rollout schema_migrations 缺失")
    if pending_retry is not None:
        state = connection.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()
        if state != ("recovery_required",) or pending_retry[:2] != (schema_version, schema_version + 1):
            raise RuntimeError("schema-upgrade-retry-conflict: 不属于当前失败版本")
    completed_version = 0
    pending_count = 0
    for index, row in enumerate(rows):
        source = strict_non_negative_int(row[0], field="migration.from_version")
        target = strict_non_negative_int(row[1], field="migration.to_version")
        name = strict_text(row[2], field="migration.name")
        checksum = strict_text(row[3], field="migration.checksum")
        if row[4] not in {"completed", "failed"} or not isinstance(row[5], str) or not row[5]:
            raise RuntimeError("rollout 存在未完成的 SQLite schema migration")
        if source != completed_version or target <= source or (source != 0 and target != source + 1):
            raise RuntimeError("schema-upgrade-journal-conflict: migration 版本链断裂")
        if index == 0 and (source != 0 or row[4] != "completed" or checksum != hashlib.sha256(f"rollout_sqlite_v{target}".encode()).hexdigest()):
            raise RuntimeError("rollout bootstrap schema migration checksum 不匹配")
        if row[4] == "completed":
            completed_version = target
            continue
        contract = (source, target, name, checksum)
        resolved = any(
            tuple(later[:4]) == contract and later[4] == "completed" and later[5]
            for later in rows[index + 1:]
        )
        if resolved:
            continue
        if pending_retry is None or contract[:3] != pending_retry[:3] or (
            pending_retry[3] is not None and checksum != pending_retry[3]
        ):
            raise RuntimeError("schema-upgrade-retry-conflict: 存在未完成或合同不一致的 SQLite schema migration")
        pending_count += 1
    if completed_version != schema_version:
        raise RuntimeError("rollout schema_version 与 schema_migrations 不一致")
    if pending_retry is not None and pending_count == 0:
        raise RuntimeError("schema-upgrade-retry-conflict: 缺少可认证的失败尝试")


def execute_atomic_schema_sql(connection: sqlite3.Connection, script: str) -> None:
    """逐条执行完整 SQLite statement，禁止脚本绕过外层事务边界。"""
    if not connection.in_transaction:
        raise RuntimeError("schema migration 必须在既有事务内执行")

    def authorize(action: int, *_args: object) -> int:
        if action in {
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        pending: list[str] = []
        for character in script:
            pending.append(character)
            if character == ";" and sqlite3.complete_statement("".join(pending)):
                connection.execute("".join(pending))
                pending.clear()
        remainder = "".join(pending).strip()
        if remainder:
            connection.execute(remainder)
    finally:
        connection.set_authorizer(None)


def _contribution_upgrade_sql(connection: sqlite3.Connection) -> str:
    """从唯一 schema owner 取得目标 DDL，不维护第二套 contribution 定义。"""
    statements: list[str] = []
    with closing(sqlite3.connect(":memory:")) as target_schema:
        schema.initialize_rollout_schema(target_schema)
        for table in ("context_contributions", "context_assembly_contributions"):
            temporary = f"schema_upgrade_{table}"
            ddl = target_schema.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            prefix = f"CREATE TABLE {table}"
            if not ddl.startswith(prefix):
                raise RuntimeError(f"schema owner 的建表声明无法识别: {table}")
            columns = tuple(row[1] for row in target_schema.execute(f"PRAGMA table_info({table})"))
            existing = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            if existing != columns:
                raise RuntimeError(
                    f"schema-upgrade-source-mismatch: {table} 字段不属于支持的 v1 schema"
                )
            fields = ",".join(columns)
            statements.extend((
                ddl.replace(prefix, f"CREATE TABLE {temporary}", 1),
                f"INSERT INTO {temporary} ({fields}) SELECT {fields} FROM {table}",
                f"DROP TABLE {table}",
                f"ALTER TABLE {temporary} RENAME TO {table}",
            ))
            statements.extend(
                row[0] for row in target_schema.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                    "AND sql IS NOT NULL ORDER BY name", (table,)
                )
            )
    return ";\n".join(statements) + ";"


def _typed_details_upgrade_sql(connection: sqlite3.Connection) -> str:
    """空 detail registry 的 DDL；非空升级由显式注入的 artifact owner 生成。"""
    if _has_context_artifacts(connection):
        raise RuntimeError("schema-upgrade-detail-remap-required: 旧 v2 assembly/detail 必须完成显式 artifact 重映射")
    with closing(sqlite3.connect(":memory:")) as target_schema:
        schema.initialize_rollout_schema(target_schema)
        ddl = target_schema.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'context_plan_details'"
        ).fetchone()[0]
    return f"DROP TABLE context_plan_details; {ddl};"


def _has_context_artifacts(connection: sqlite3.Connection) -> bool:
    tables = (
        "context_plan_details", "context_assemblies", "assembly_item_refs",
        "context_assembly_selections", "tool_set_snapshots",
    )
    return any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in tables)


class RolloutSchemaUpgradeMixin:
    """已注册版本路径的显式升级入口，不参与普通 runtime 初始化。"""

    def upgrade_v2_schema(
        self, thread_id: str, *, checkpoint_ns: str = "",
        prepare_artifact_upgrade: Callable[[sqlite3.Connection], SchemaUpgradeArtifacts] | None = None,
        resume_artifact_upgrade: Callable[[sqlite3.Connection], bool] | None = None,
        prepare_plan_upgrade: Callable[[sqlite3.Connection], SchemaUpgradePlan] | None = None,
    ) -> RolloutReadSnapshot:
        thread_id = strict_text(thread_id, field="schema_upgrade.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        with self._lock(thread_id, checkpoint_ns):
            with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
                self._require_v2_runtime(connection)
                current = strict_non_negative_int(connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
                ).fetchone()[0], field="schema_version")
                if current not in {1, 2, 3, 4} or schema.ROLLOUT_SCHEMA_VERSION != 4:
                    raise RuntimeError(f"schema-upgrade-path-unavailable: {current}")
                if current < 4 and prepare_plan_upgrade is None:
                    raise RuntimeError("schema-upgrade-plan-import-required: 显式升级必须注入 plan import owner")
                script = _contribution_upgrade_sql(connection) if current == 1 else None
                has_artifacts = current < 3 and _has_context_artifacts(connection)
                if has_artifacts and prepare_artifact_upgrade is None:
                    raise RuntimeError("schema-upgrade-detail-remap-required: 非空升级必须注入 artifact owner")
                # TODO: schema1 非空 artifact 仍需冻结其完整预检合同；不能先更新
                # schema1 再让旧正文解析失败。该输入继续在任何发布前明确拒绝。
                if has_artifacts and current == 1:
                    raise RuntimeError("schema-upgrade-source-mismatch: 非空 schema1 artifact 尚无完整升级路径")
                detail_script = _typed_details_upgrade_sql(connection) if current < 3 and not has_artifacts else None
            if current == 3 and resume_artifact_upgrade is not None:
                with self._connect(thread_id, checkpoint_ns) as connection:
                    if resume_artifact_upgrade(connection):
                        _activate_verified_artifact_upgrade(connection)
            if script is not None:
                self._migrate_schema_locked(
                    thread_id, checkpoint_ns=checkpoint_ns,
                    to_version=2, migration_name="v2_contribution_hash_token",
                    migration_sql=script,
                )
            if detail_script is not None:
                self._migrate_schema_locked(
                    thread_id, checkpoint_ns=checkpoint_ns, to_version=3,
                    migration_name="v3_typed_detail_identity", migration_sql=detail_script,
                )
            if has_artifacts:
                self._upgrade_context_artifacts_locked(
                    thread_id, checkpoint_ns=checkpoint_ns,
                    prepare=prepare_artifact_upgrade,
                )
            if current < 4:
                with self._connect(thread_id, checkpoint_ns, read_only=True) as source:
                    prepared_plan = prepare_plan_upgrade(source)
                self._migrate_schema_locked(
                    thread_id, checkpoint_ns=checkpoint_ns, to_version=4,
                    migration_name="v4_context_plan_registry",
                    migration_sql=prepared_plan.migration_sql,
                    validate_migrated=prepared_plan.verify_migrated,
                    allow_failed_retry=True,
                )
        return self.validate_index(thread_id, checkpoint_ns)

    def _upgrade_context_artifacts_locked(
        self, thread_id: str, *, checkpoint_ns: str,
        prepare: Callable[[sqlite3.Connection], SchemaUpgradeArtifacts],
    ) -> None:
        """持有 owner 锁时发布新文件、验证并提交 SQL；原件始终保留。"""
        with self._connect(thread_id, checkpoint_ns, read_only=True) as source:
            prepared = prepare(source)
            state = source.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()
            if state == ("recovery_required",):
                # prepare 的只读扫描可以接收 pending journal；发布前必须再次
                # 核对最终绑定了 audit/文件指纹的 SQL checksum，不能换脚本重试。
                self._validate_schema_state(
                    source, allow_older_schema=True,
                    pending_retry=(2, 3, "v3_typed_detail_identity", hashlib.sha256(prepared.migration_sql.encode()).hexdigest()),
                )
            try:
                prepared.publish()
            except BaseException:
                prepared.discard_uncommitted(source)
                raise
        try:
            self._migrate_schema_locked(
                thread_id, checkpoint_ns=checkpoint_ns, to_version=3,
                migration_name="v3_typed_detail_identity",
                migration_sql=prepared.migration_sql,
                validate_migrated=prepared.verify_migrated,
                pending_artifact_audit=True,
            )
        except BaseException:
            with self._connect(thread_id, checkpoint_ns, read_only=True) as failed:
                prepared.discard_uncommitted(failed)
            raise
        # SQL 已提交后，只能验证并完成审计。此处失败不得清理已被引用的新文件。
        with self._connect(thread_id, checkpoint_ns) as committed:
            prepared.verify_committed(committed)
            _activate_verified_artifact_upgrade(committed)


def _activate_verified_artifact_upgrade(connection: sqlite3.Connection) -> None:
    """只有已核验的 schema3 artifact audit 可以解除 migrating 写入隔离。"""
    row = connection.execute(
        "SELECT schema_version, database_state FROM database_meta WHERE singleton_id = 1"
    ).fetchone()
    if row is None or row[0] != 3 or row[1] not in {"active", "migrating"}:
        raise RuntimeError("schema-upgrade-state-conflict: 已验证 artifact 的数据库状态不匹配")
    if row[1] == "active":
        return
    result = connection.execute(
        "UPDATE database_meta SET database_state = 'active', updated_at = ? "
        "WHERE singleton_id = 1 AND schema_version = 3 AND database_state = 'migrating'",
        (datetime.now(UTC).isoformat(),),
    )
    if result.rowcount != 1:
        raise RuntimeError("schema-upgrade-state-conflict: audit 激活未命中唯一 owner")
    connection.commit()
