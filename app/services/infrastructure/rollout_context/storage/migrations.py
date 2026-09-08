"""v2 SQLite schema versioning与字段迁移 owner。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    initialize_rollout_schema,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    RolloutSchemaUpgradeMixin,
    execute_atomic_schema_sql,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutManifest,
        RolloutReadSnapshot,
    )

_DEFAULT_NAMESPACE = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RolloutSchemaMigrationMixin(RolloutSchemaUpgradeMixin):
    def migrate_schema(
        self,
        thread_id: str,
        *,
        to_version: int,
        migration_name: str,
        migration_sql: str,
        checkpoint_ns: str = "",
    ) -> RolloutReadSnapshot:
        """执行一个事务性的 SQLite schema migration。"""
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        to_version = strict_non_negative_int(to_version, field="to_version")
        migration_name = strict_text(migration_name, field="migration_name")
        migration_sql = strict_text(migration_sql, field="migration_sql")
        if to_version < 1:
            raise ValueError("SQLite schema version 必须从 1 开始")
        with self._lock(thread_id, checkpoint_ns):
            self._migrate_schema_locked(
                thread_id, checkpoint_ns=checkpoint_ns, to_version=to_version,
                migration_name=migration_name, migration_sql=migration_sql,
            )
        return self.validate_index(thread_id, checkpoint_ns)

    def _migrate_schema_locked(
        self, thread_id: str, *, checkpoint_ns: str, to_version: int,
        migration_name: str, migration_sql: str,
        validate_migrated: Callable[[sqlite3.Connection], None] | None = None,
        pending_artifact_audit: bool = False,
        allow_failed_retry: bool = False,
    ) -> None:
        """调用方持有 owner 写锁；不能在锁内获取独立读 snapshot。"""
        if pending_artifact_audit and (to_version != 3 or validate_migrated is None):
            raise ValueError("schema3 artifact 隔离必须同时提供 COMMIT 前验证")
        checksum = _hash_bytes(migration_sql.encode("utf-8"))
        with self._lock(thread_id, checkpoint_ns):
            with self._connect(thread_id, checkpoint_ns, read_only=True) as source:
                state = source.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()
                artifact_retry = (pending_artifact_audit or allow_failed_retry) and state == ("recovery_required",)
                if artifact_retry:
                    self._require_v2_runtime(source)
                    self._validate_schema_state(
                        source, allow_older_schema=True,
                        pending_retry=(to_version - 1, to_version, migration_name, checksum),
                    )
                    self._validate_v2_commit_offsets(source, self.jsonl_path(thread_id, checkpoint_ns))
            if not artifact_retry:
                self.initialize(thread_id, checkpoint_ns, _allow_schema_upgrade=True)
            with self._connect(thread_id, checkpoint_ns) as connection:
                row = connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if row is None:
                    raise RuntimeError("rollout database_meta 缺失")
                current_version = strict_non_negative_int(
                    row[0], field="database_meta.schema_version"
                )
            if to_version != current_version + 1:
                raise ValueError(
                    "SQLite migration 必须按版本顺序执行: "
                    f"current={current_version}, target={to_version}"
                )
            if to_version > storage_version.ROLLOUT_SCHEMA_VERSION:
                raise RuntimeError(
                    "当前程序尚未声明目标 SQLite schema 版本: "
                    f"target={to_version}, supported={storage_version.ROLLOUT_SCHEMA_VERSION}"
                )
            backup_path = self.root(thread_id, checkpoint_ns) / (
                f"index.sqlite.migration-{uuid4().hex}.backup"
            )
            self._backup_index_unlocked(
                thread_id,
                checkpoint_ns,
                destination=backup_path,
            )
            transaction_id = uuid4().hex
            timestamp = _now()
            try:
                with self._connect(thread_id, checkpoint_ns) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    migration_cursor = connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at) VALUES (?, ?, ?, ?, 'started', ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                        ),
                    )
                    migration_id = strict_non_negative_int(
                        migration_cursor.lastrowid,
                        field="schema_migrations.migration_id",
                    )
                    execute_atomic_schema_sql(connection, migration_sql)
                    meta_result = connection.execute(
                        "UPDATE database_meta SET schema_version = ?, updated_at = ?, "
                        "database_state = CASE WHEN ? THEN 'migrating' ELSE database_state END "
                        "WHERE singleton_id = 1",
                        (to_version, timestamp, pending_artifact_audit),
                    )
                    if meta_result.rowcount != 1:
                        raise RuntimeError("SQLite migration database_meta 更新失败")
                    migration_result = connection.execute(
                        "UPDATE schema_migrations SET status = 'completed', completed_at = ? WHERE migration_id = ?",
                        (timestamp, migration_id),
                    )
                    if migration_result.rowcount != 1:
                        raise RuntimeError("SQLite migration journal 更新失败")
                    control_sequence = self._insert_control(
                        connection,
                        "schema_migration",
                        "schema",
                        migration_name,
                        None,
                        None,
                        None,
                        {"from_version": current_version, "to_version": to_version},
                        transaction_id,
                        timestamp,
                    )
                    control_result = connection.execute(
                        "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                        (control_sequence, timestamp),
                    )
                    if control_result.rowcount != 1:
                        raise RuntimeError("SQLite migration control sequence 更新失败")
                    # 外部 artifact 已先发布；必须在 SQLite COMMIT 前校验新
                    # manifest/引用。失败进入同一回滚边界，不能先提交版本再发现损坏。
                    if artifact_retry and not pending_artifact_audit:
                        # 失败记录保留原样；只有相同合同的已验证 completed
                        # journal 允许显式升级事务解除 recovery_required。
                        connection.execute(
                            "UPDATE database_meta SET database_state='active' WHERE singleton_id=1"
                        )
                    if validate_migrated is not None:
                        validate_migrated(connection)
                    connection.commit()
            except BaseException as error:
                self._restore_index_backup_unlocked(
                    thread_id,
                    checkpoint_ns,
                    backup_path,
                )
                with self._connect(thread_id, checkpoint_ns) as connection:
                    failure_time = _now()
                    connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at, error_message) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)",
                        (
                            current_version,
                            to_version,
                            migration_name,
                            checksum,
                            timestamp,
                            failure_time,
                            str(error),
                        ),
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (failure_time,),
                    )
                    connection.commit()
                raise
    def _manifest_from_connection(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        *,
        allow_migrating: bool = False,
    ) -> RolloutManifest:
        meta_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in meta_columns:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version"
            )
        row = connection.execute(
            "SELECT rollout_id, active_branch_id, last_message_sequence, projection_epoch, last_commit_id, database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        rollout_id = strict_text(row[0], field="database_meta.rollout_id")
        active_branch_id = strict_optional_text(
            row[1], field="database_meta.active_branch_id"
        )
        last_message_sequence = strict_non_negative_int(
            row[2], field="database_meta.last_message_sequence"
        )
        _projection_epoch = strict_non_negative_int(
            row[3], field="database_meta.projection_epoch"
        )
        _last_commit_id = strict_optional_non_negative_int(
            row[4], field="database_meta.last_commit_id"
        )
        database_state = strict_text(row[5], field="database_meta.database_state")
        if database_state != "active" and not (
            allow_migrating and database_state == "migrating"
        ):
            raise RuntimeError(f"rollout SQLite 状态不可读取: {database_state}")
        if active_branch_id is None:
            raise RuntimeError("database_meta.active_branch_id 缺失")
        namespace_state = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if namespace_state is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        namespace_branch_id = strict_text(
            namespace_state[0], field="checkpoint_namespace_state.active_branch_id"
        )
        namespace_projection_epoch = strict_non_negative_int(
            namespace_state[1], field="checkpoint_namespace_state.projection_epoch"
        )
        latest = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE checkpoint_ns = ? AND status = 'active' ORDER BY commit_id DESC LIMIT 1",
            (checkpoint_ns,),
        ).fetchone()
        latest_checkpoint_id = (
            strict_text(latest[0], field="checkpoints.checkpoint_id")
            if latest is not None
            else None
        )
        format_row = connection.execute(
            "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if format_row is None:
            raise FormatDispatchError(
                "v1_migration_required: database_meta 缺少 rollout_format_version row"
            )
        rollout_format = strict_non_negative_int(
            format_row[0], field="database_meta.rollout_format_version"
        )
        for required_meta_column in ("history_view_revision", "source_overlay_epoch"):
            if required_meta_column not in meta_columns:
                raise RuntimeError(
                    f"v2 rollout database_meta 缺少必需字段: {required_meta_column}"
                )
        revision_row = connection.execute(
            "SELECT history_view_revision, source_overlay_epoch "
            "FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("v2 rollout database_meta revision row 缺失")
        history_view_revision = strict_non_negative_int(
            revision_row[0], field="database_meta.history_view_revision"
        )
        source_overlay_epoch = strict_non_negative_int(
            revision_row[1], field="database_meta.source_overlay_epoch"
        )
        from app.services.infrastructure.rollout_context.storage.primitives import (
            RolloutManifest,
        )

        return RolloutManifest(
            rollout_id,
            checkpoint_ns,
            namespace_branch_id,
            last_message_sequence,
            latest_checkpoint_id,
            namespace_projection_epoch,
            rollout_format,
            history_view_revision,
            source_overlay_epoch,
        )

    @staticmethod
    def _namespace_state(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
    ) -> tuple[str, int]:
        row = connection.execute(
            "SELECT active_branch_id, projection_epoch FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"rollout checkpoint namespace 状态缺失: {checkpoint_ns!r}"
            )
        return (
            strict_text(row[0], field="checkpoint_namespace_state.active_branch_id"),
            strict_non_negative_int(
                row[1], field="checkpoint_namespace_state.projection_epoch"
            ),
        )

    def _ensure_namespace_state(
        self,
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        timestamp: str,
    ) -> None:
        existing = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if existing is not None:
            strict_text(
                existing[0], field="checkpoint_namespace_state.active_branch_id"
            )
            return
        if checkpoint_ns == _DEFAULT_NAMESPACE:
            meta = connection.execute(
                "SELECT active_branch_id FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None or meta[0] is None:
                raise RuntimeError(
                    "rollout 默认 checkpoint namespace 缺少 active branch"
                )
            branch_id = strict_text(meta[0], field="database_meta.active_branch_id")
        else:
            branch_id = "branch-" + uuid4().hex[:12]
            branch_result = connection.execute(
                "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                (branch_id, timestamp, timestamp),
            )
            if branch_result.rowcount != 1:
                raise RuntimeError(
                    f"rollout namespace branch 创建失败: {checkpoint_ns!r}"
                )
        namespace_result = connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
            (checkpoint_ns, branch_id, timestamp, timestamp),
        )
        if namespace_result.rowcount != 1:
            raise RuntimeError(f"rollout namespace state 创建失败: {checkpoint_ns!r}")

    def _initialize_schema(self, thread_id: str, checkpoint_ns: str) -> None:
        del checkpoint_ns
        with self._connect(thread_id) as connection:
            initialize_rollout_schema(connection)
