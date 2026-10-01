"""rollout storage 备份/恢复/schema 迁移/维护/启动 owner（由 RolloutStorage 组合）。

这些能力需要同时理解 JSONL durability barrier、SQLite commit chain、备份文件、读取快照
生命周期与启动自检，原分处 5 个 mixin。方法体逐字平移，只把对 host 锁/连接/路径薄壳的
self.X 调用改写为 self._host.X；SQL、事务边界、锁序与错误文案保持不变。
本模块保留 owner 组合类、宿主薄壳、schema 迁移入口与启动初始化；其余方法族拆到
同目录的 fs safety / schema introspection / offline restore / fork recovery /
index reads / active view 六个 mixin，经同一 self 组合装配。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.hashing import sha256_hex as _hash_bytes
from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.format_dispatch import (
    require_v2_runtime,
)
from app.services.infrastructure.rollout_context.storage.maintenance_fs_safety import (
    RolloutMaintenanceFsSafetyMixin,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutManifest,
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.rollout_active_view import (
    RolloutActiveViewMixin,
)
from app.services.infrastructure.rollout_context.storage.rollout_fork_recovery import (
    RolloutForkRecoveryMixin,
)
from app.services.infrastructure.rollout_context.storage.rollout_index_reads import (
    RolloutIndexMixin,
)
from app.services.infrastructure.rollout_context.storage.rollout_offline_restore import (
    RolloutOfflineRestoreMixin,
)
from app.services.infrastructure.rollout_context.storage.rollout_schema_introspection import (
    RolloutSchemaIntrospectionMixin,
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
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        _RolloutOperationLock,
    )

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )

def reconcile_jsonl_tail(path: Path, committed_offset: int) -> None:
    """只按 SQLite committed offset 清理崩溃尾部，不扫描尾部猜测记录。"""
    committed_offset = strict_non_negative_int(
        committed_offset, field="database_meta.committed_jsonl_offset"
    )
    if not path.is_file():
        raise RuntimeError(f"committed JSONL 文件不存在: {path}")
    file_size = path.stat().st_size
    if file_size < committed_offset:
        raise RuntimeError(
            "rollout.jsonl 小于 SQLite committed offset: "
            f"file={file_size}, committed={committed_offset}"
        )
    if file_size == committed_offset:
        return
    with path.open("r+b") as stream:
        stream.truncate(committed_offset)
        stream.flush()
        os.fsync(stream.fileno())


__all__ = ["RolloutMaintenanceOwner"]


class RolloutMaintenanceOwner(
    RolloutSchemaUpgradeMixin,
    RolloutMaintenanceFsSafetyMixin,
    RolloutSchemaIntrospectionMixin,
    RolloutOfflineRestoreMixin,
    RolloutForkRecoveryMixin,
    RolloutIndexMixin,
    RolloutActiveViewMixin,
):
    """备份/恢复/schema 迁移/维护/启动 owner；只通过 host 的锁与连接薄壳工作。"""

    def __init__(self, host: RolloutStorage) -> None:
        self._host = host

    # 继承的 RolloutSchemaUpgradeMixin 通过 self 调用 host 的锁/连接/格式薄壳；
    # owner 只转发到唯一 host，不复制第二套连接、锁或提交路径。
    def _lock(
        self,
        thread_id: str,
        checkpoint_ns: str,
        *,
        session_id: str | None = None,
    ) -> _RolloutOperationLock:
        return self._host._lock(thread_id, checkpoint_ns, session_id=session_id)

    def _connect(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        return self._host._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=read_only
        )

    def _require_v2_runtime(self, connection: sqlite3.Connection) -> None:
        self._host._require_v2_runtime(connection)

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
        with self._host._lock(thread_id, checkpoint_ns):
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
        with self._host._lock(thread_id, checkpoint_ns):
            with self._host._connect(thread_id, checkpoint_ns, read_only=True) as source:
                state = source.execute("SELECT database_state FROM database_meta WHERE singleton_id=1").fetchone()
                artifact_retry = (pending_artifact_audit or allow_failed_retry) and state == ("recovery_required",)
                if artifact_retry:
                    self._host._require_v2_runtime(source)
                    self._validate_schema_state(
                        source, allow_older_schema=True,
                        pending_retry=(to_version - 1, to_version, migration_name, checksum),
                    )
                    self._validate_v2_commit_offsets(source, self._host.jsonl_path(thread_id, checkpoint_ns))
            if not artifact_retry:
                self.initialize(thread_id, checkpoint_ns, _allow_schema_upgrade=True)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
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
            backup_path = self._host.root(thread_id, checkpoint_ns) / (
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
                with self._host._connect(thread_id, checkpoint_ns) as connection:
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
                    control_sequence = self._host._insert_control(
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
                with self._host._connect(thread_id, checkpoint_ns) as connection:
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

    def _initialize_schema(self, thread_id: str, checkpoint_ns: str) -> None:
        del checkpoint_ns
        with self._host._connect(thread_id) as connection:
            initialize_rollout_schema(connection)

    def initialize(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
        *,
        validate_jsonl_items: bool = True,
        _allow_schema_upgrade: bool = False,
    ) -> RolloutManifest:
        thread_id = strict_text(thread_id, field="thread_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        with self._host._lock(thread_id, checkpoint_ns):
            root = self._host.root(thread_id, checkpoint_ns)
            root.mkdir(parents=True, exist_ok=True)
            if self._is_removed_rollout_layout(root):
                raise RuntimeError(f"rollout 使用了已移除的旧布局: {root}")
            # 正常 runtime 的初始化不能把已有 v1 source 当作空库升级或
            # fallback。v1 原件只能由显式 legacy_import_v1_to_v2 只读读取，
            # 迁移目标则必须是新建/空的 v2 rollout。
            existing_index = self._host.index_path(thread_id, checkpoint_ns)
            needs_schema = True
            if existing_index.is_file() and existing_index.stat().st_size > 0:
                with self._existing_index_connection(
                    thread_id,
                    checkpoint_ns,
                ) as existing_connection:
                    meta_columns = {
                        str(row[1])
                        for row in existing_connection.execute(
                            "PRAGMA table_info(database_meta)"
                        )
                    }
                    if "rollout_format_version" not in meta_columns:
                        raise FormatDispatchError(
                            "v1_migration_required: rollout 缺少 v2 format dispatch"
                        )
                    format_row = existing_connection.execute(
                        "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
                    ).fetchone()
                    if format_row is None:
                        # schema.py 先创建空的 v2 表，再由本方法原子创建
                        # database_meta。这个窗口不是 v1 artifact；只有已经
                        # 存在业务行却没有 v2 meta 时才是不可恢复的半成品。
                        v2_rows = sum(
                            strict_non_negative_int(
                                existing_connection.execute(
                                    f"SELECT COUNT(*) FROM {table}"
                                ).fetchone()[0],
                                field=f"{table}.count",
                            )
                            for table in (
                                "messages",
                                "item_catalog",
                                "storage_commits",
                                "turn_records",
                                "context_views",
                                "checkpoints",
                                "context_plans",
                                "context_plan_refs",
                                "context_plan_contributions",
                                "context_plan_seal_failures",
                                "tool_set_snapshots",
                            )
                        )
                        if v2_rows:
                            raise RuntimeError(
                                "v2 rollout database_meta 缺失但已有业务数据，"
                                "拒绝猜测半成品恢复: v2_migration_repair_required"
                            )
                        if "rollout_format_version" not in meta_columns:
                            raise FormatDispatchError(
                                "v1_migration_required: rollout database_meta 缺少 format row"
                            )
                        # 空的、已创建 v2 schema 继续走下面的 meta 初始化。
                    else:
                        needs_schema = False
                        require_v2_runtime(
                            strict_non_negative_int(
                                format_row[0],
                                field="database_meta.rollout_format_version",
                            )
                        )
                        self._validate_schema_state(
                            existing_connection, allow_older_schema=_allow_schema_upgrade
                        )
            path = self._host.jsonl_path(thread_id, checkpoint_ns)
            # 只读历史请求会频繁经过 initialize；已有文件不能重复 touch，
            # 否则会改变 rollout.jsonl 的 mtime，触发工作区文件监听并造成
            # 无意义的资源刷新。首次创建时才建立空的 canonical 文件。
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError(f"rollout.jsonl 不是安全普通文件: {path}")
            if not path.exists():
                path.touch()
            # 已有 authoritative meta 的索引只能验证；建表/改表由显式 schema migration 负责。
            if needs_schema:
                self._initialize_schema(thread_id, checkpoint_ns)
            self._host._validate_reasoning_projection_schema(thread_id, checkpoint_ns)
            if not self._host._legacy_migration_is_active((thread_id, checkpoint_ns)):
                self._reject_unpublished_migration(thread_id, checkpoint_ns)
            with self._host._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT * FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    rollout_id = self._host._rollout_id(thread_id)
                    timestamp = _now()
                    meta_result = connection.execute(
                        """INSERT INTO database_meta(singleton_id, rollout_id, session_id,
                            schema_version, message_format_version, database_state,
                            last_message_sequence, last_control_sequence, committed_jsonl_offset,
                            projection_epoch, created_at, updated_at, rollout_format_version)
                            VALUES (1, ?, ?, ?, ?, 'active', 0, 0, 0, 1, ?, ?, ?)""",
                        (
                            rollout_id,
                            thread_id,
                            storage_version.ROLLOUT_SCHEMA_VERSION,
                            storage_version.MESSAGE_FORMAT_VERSION,
                            timestamp,
                            timestamp,
                            storage_version.ROLLOUT_FORMAT_VERSION,
                        ),
                    )
                    if meta_result.rowcount != 1:
                        raise RuntimeError("rollout database_meta 创建失败")
                    branch_result = connection.execute(
                        "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES (?, 'root', 'active', NULL, NULL, ?, ?)",
                        ("branch-001", timestamp, timestamp),
                    )
                    if branch_result.rowcount != 1:
                        raise RuntimeError("rollout root branch 创建失败")
                    branch_meta_result = connection.execute(
                        "UPDATE database_meta SET active_branch_id = 'branch-001' WHERE singleton_id = 1"
                    )
                    if branch_meta_result.rowcount != 1:
                        raise RuntimeError("rollout active branch 写入失败")
                    migration_result = connection.execute(
                        "INSERT INTO schema_migrations(from_version, to_version, migration_name, migration_checksum, status, started_at, completed_at) VALUES (0, ?, ?, ?, 'completed', ?, ?)",
                        (
                            storage_version.ROLLOUT_SCHEMA_VERSION,
                            f"rollout_sqlite_v{storage_version.ROLLOUT_SCHEMA_VERSION}",
                            _hash_bytes(f"rollout_sqlite_v{storage_version.ROLLOUT_SCHEMA_VERSION}".encode()),
                            timestamp,
                            timestamp,
                        ),
                    )
                    if migration_result.rowcount != 1:
                        raise RuntimeError("rollout schema migration journal 创建失败")
                self._ensure_namespace_state(connection, checkpoint_ns, _now())
                active_branch = connection.execute(
                    "SELECT branch_id, head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?) AND status = 'active'",
                    (checkpoint_ns,),
                ).fetchone()
                if active_branch is None:
                    raise RuntimeError(
                        "active branch 缺失，不能创建 rollout context view"
                    )
                active_branch_id = strict_text(
                    active_branch[0], field="branches.branch_id"
                )
                active_head_view_id = strict_optional_text(
                    active_branch[1], field="branches.head_view_id"
                )
                if active_head_view_id is None:
                    # acceptance 可以先于第一个 LangGraph checkpoint 到达；
                    # 为这个空但真实存在的 active branch 建立 root view，
                    # 使 Turn/root 在 acceptance-time 就有稳定的 view-local
                    # 索引，而不是等下一次 checkpoint 偶然补齐。
                    initial_view_id = self._host._create_view(
                        connection,
                        active_branch_id,
                        None,
                        (),
                        _now(),
                        view_kind="root",
                    )
                    head_result = connection.execute(
                        "UPDATE branches SET head_view_id = ?, updated_at = ? WHERE branch_id = ?",
                        (initial_view_id, _now(), active_branch_id),
                    )
                    if head_result.rowcount != 1:
                        raise RuntimeError(
                            f"rollout active branch head view 更新失败: {active_branch_id}"
                        )
                self._validate_schema_state(connection, allow_older_schema=_allow_schema_upgrade)
                if (thread_id, checkpoint_ns) not in self._host._active_fork_materializations:
                    self._recover_fork_materialization(
                        thread_id,
                        checkpoint_ns,
                        connection,
                        path,
                    )
                committed_offset_row = connection.execute(
                    "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if committed_offset_row is None:
                    raise RuntimeError("rollout database_meta committed offset 缺失")
                committed_offset = strict_non_negative_int(
                    committed_offset_row[0],
                    field="database_meta.committed_jsonl_offset",
                )
                file_size = path.stat().st_size
                if file_size < committed_offset:
                    result = connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("rollout recovery_required 状态写入失败")
                    raise TypeError(
                        "rollout.jsonl 小于 SQLite 已提交偏移，无法安全恢复"
                    )
                # 先验证 meta、提交链及 catalog 的同一边界；损坏的 meta
                # 不能成为截断依据，否则会在报错前删除已提交 item。
                self._validate_v2_commit_offsets(
                    connection,
                    path,
                    validate_jsonl_items=validate_jsonl_items,
                )
                if file_size > committed_offset:
                    reconcile_jsonl_tail(path, committed_offset)
                return self._manifest_from_connection(
                    connection,
                    checkpoint_ns,
                    allow_migrating=self._host._legacy_migration_is_active(
                        (thread_id, checkpoint_ns)
                    ),
                )

    def _reject_unpublished_migration(
        self, thread_id: str, checkpoint_ns: str,
    ) -> None:
        """正常 runtime 不清空迁移半成品；恢复只由显式 migration owner 执行。"""
        with self._host._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            row = connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
        if row is not None and row[0] == "migrating":
            raise RuntimeError(
                "migration-installation-incomplete: target 尚未原子安装；"
                "保留原始 JSONL/SQLite，必须由显式 legacy migration 恢复审计"
            )
