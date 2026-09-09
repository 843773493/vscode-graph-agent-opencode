"""rollout 启动、恢复前置和 active context view 修复 owner。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.migration.dispatch import (
    require_v2_runtime,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.recovery import (
    reconcile_jsonl_tail,
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
    )

_VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages AS visible_user_message "
    "WHERE visible_user_message.turn_id = t.turn_id "
    "AND visible_user_message.role = 'user' "
    "AND visible_user_message.visibility = 'visible')"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    import rfc8785

    return rfc8785.dumps(value).decode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class RolloutStartupMixin:
    @contextmanager
    def _existing_index_connection(
        self,
        thread_id: str,
        checkpoint_ns: str,
    ) -> Iterator[sqlite3.Connection]:
        """读取既有 authority；完全损坏时只报告恢复要求，不创建新库。"""
        try:
            with self._connect(
                thread_id,
                checkpoint_ns,
                read_only=True,
            ) as connection:
                yield connection
        except sqlite3.DatabaseError as error:
            raise RuntimeError(
                "recovery_required: rollout SQLite 无法读取；"
                "必须从已验证的 SQLite backup 执行显式恢复，禁止从 JSONL 重建: "
                f"{self.index_path(thread_id, checkpoint_ns)}"
            ) from error

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
        with self._lock(thread_id, checkpoint_ns):
            root = self.root(thread_id, checkpoint_ns)
            root.mkdir(parents=True, exist_ok=True)
            if self._is_removed_rollout_layout(root):
                raise RuntimeError(f"rollout 使用了已移除的旧布局: {root}")
            # 正常 runtime 的初始化不能把已有 v1 source 当作空库升级或
            # fallback。v1 原件只能由显式 legacy_import_v1_to_v2 只读读取，
            # 迁移目标则必须是新建/空的 v2 rollout。
            existing_index = self.index_path(thread_id, checkpoint_ns)
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
            path = self.jsonl_path(thread_id, checkpoint_ns)
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
            self._validate_reasoning_projection_schema(thread_id, checkpoint_ns)
            if not self._legacy_migration_is_active((thread_id, checkpoint_ns)):
                self._reject_unpublished_migration(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                meta = connection.execute(
                    "SELECT * FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    rollout_id = self._rollout_id(thread_id)
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
                    initial_view_id = self._create_view(
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
                if (thread_id, checkpoint_ns) not in self._active_fork_materializations:
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
                    allow_migrating=self._legacy_migration_is_active(
                        (thread_id, checkpoint_ns)
                    ),
                )

    def _reject_unpublished_migration(
        self, thread_id: str, checkpoint_ns: str,
    ) -> None:
        """正常 runtime 不清空迁移半成品；恢复只由显式 migration owner 执行。"""
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            row = connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
        if row is not None and row[0] == "migrating":
            raise RuntimeError(
                "migration-installation-incomplete: target 尚未原子安装；"
                "保留原始 JSONL/SQLite，必须由显式 legacy migration 恢复审计"
            )

    def repair_active_context_view(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """修复 active view 的 Turn 索引，不重写 canonical 消息文件。

        旧版本按全局消息序号连续性判断 Turn 完整性。并发执行时不同 Turn
        的消息会交错，导致 view 的消息范围存在但 ``context_view_turns`` 被
        错误删空。这里依据每个 Turn 自身的消息集合重新计算索引；只有索引
        与规范结果不一致时才写 SQLite，避免普通只读请求产生文件监听噪声。
        """
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                namespace = self._namespace_state(connection, checkpoint_ns)
                branch_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (namespace[0],),
                ).fetchone()
                if branch_row is None:
                    return False
                view_id = strict_optional_text(
                    branch_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                visible_sequences = set(
                    self._view_message_sequences_from_connection(connection, view_id)
                )
                expected: list[tuple[str, int, int | None, int | None]] = []
                turn_rows = connection.execute(
                    f"SELECT turn_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence FROM turns AS t WHERE t.turn_kind = 'normal' AND t.user_message_sequence IS NOT NULL AND {_VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY t.turn_ordinal"
                ).fetchall()
                for row in turn_rows:
                    turn_id = strict_text(row[0], field="turns.turn_id")
                    first_sequence = strict_non_negative_int(
                        row[1], field=f"turns.first_message_sequence:{turn_id}"
                    )
                    last_sequence = strict_non_negative_int(
                        row[2], field=f"turns.last_message_sequence:{turn_id}"
                    )
                    user_sequence = strict_optional_non_negative_int(
                        row[3], field=f"turns.user_message_sequence:{turn_id}"
                    )
                    final_sequence = strict_optional_non_negative_int(
                        row[4], field=f"turns.final_message_sequence:{turn_id}"
                    )
                    if (
                        first_sequence == 0
                        or last_sequence == 0
                        or last_sequence < first_sequence
                        or user_sequence is None
                    ):
                        raise RuntimeError(
                            f"active view repair Turn message range 非法: {turn_id}"
                        )
                    turn_sequences = {
                        strict_non_negative_int(
                            message_row[0],
                            field=f"messages.message_sequence:{turn_id}",
                        )
                        for message_row in connection.execute(
                            "SELECT message_sequence FROM messages WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchall()
                    }
                    if not turn_sequences:
                        raise RuntimeError(
                            f"active view repair Turn 没有 message: {turn_id}"
                        )
                    if turn_sequences.issubset(visible_sequences):
                        expected.append(
                            (
                                turn_id,
                                first_sequence,
                                user_sequence,
                                final_sequence,
                            )
                        )
                current = [
                    (
                        strict_text(row[0], field="context_view_turns.turn_id"),
                        strict_non_negative_int(
                            row[1], field="context_view_turns.logical_turn_ordinal"
                        ),
                        strict_optional_non_negative_int(
                            row[2], field="context_view_turns.user_message_sequence"
                        ),
                        strict_optional_non_negative_int(
                            row[3], field="context_view_turns.final_message_sequence"
                        ),
                    )
                    for row in connection.execute(
                        "SELECT turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence FROM context_view_turns WHERE view_id = ? ORDER BY logical_turn_ordinal",
                        (view_id,),
                    ).fetchall()
                ]
                normalized_expected = [
                    (turn_id, ordinal, user_sequence, final_sequence)
                    for ordinal, (
                        turn_id,
                        _first,
                        user_sequence,
                        final_sequence,
                    ) in enumerate(expected, start=1)
                ]
                view_header = connection.execute(
                    "SELECT head_turn_id, head_message_sequence, logical_turn_count FROM context_views WHERE view_id = ?",
                    (view_id,),
                ).fetchone()
                if view_header is None:
                    raise RuntimeError(f"active context view 不存在: {view_id}")
                stored_head_turn_id = strict_optional_text(
                    view_header[0], field="context_views.head_turn_id"
                )
                stored_head_sequence = strict_non_negative_int(
                    view_header[1], field="context_views.head_message_sequence"
                )
                stored_turn_count = strict_non_negative_int(
                    view_header[2], field="context_views.logical_turn_count"
                )
                expected_head = (
                    normalized_expected[-1][0] if normalized_expected else None
                )
                expected_sequence = max(visible_sequences, default=0)
                if (
                    current == normalized_expected
                    and stored_head_turn_id == expected_head
                    and stored_head_sequence == expected_sequence
                    and stored_turn_count == len(normalized_expected)
                ):
                    return False
                delete_result = connection.execute(
                    "DELETE FROM context_view_turns WHERE view_id = ?",
                    (view_id,),
                )
                if delete_result.rowcount != len(current):
                    raise RuntimeError(
                        f"active view repair 删除 Turn 行数不一致: {view_id}"
                    )
                insert_result = connection.executemany(
                    "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence) VALUES (?, ?, ?, ?, ?)",
                    (
                        (view_id, turn_id, ordinal, user_sequence, final_sequence)
                        for ordinal, (
                            turn_id,
                            _first,
                            user_sequence,
                            final_sequence,
                        ) in enumerate(expected, start=1)
                    ),
                )
                if insert_result.rowcount != len(normalized_expected):
                    raise RuntimeError(
                        f"active view repair 插入 Turn 行数不一致: {view_id}"
                    )
                header_result = connection.execute(
                    "UPDATE context_views SET head_turn_id = ?, head_message_sequence = ?, logical_turn_count = ? WHERE view_id = ?",
                    (
                        expected_head,
                        expected_sequence,
                        len(normalized_expected),
                        view_id,
                    ),
                )
                if header_result.rowcount != 1:
                    raise RuntimeError(f"active view repair header 更新失败: {view_id}")
                connection.commit()
                return True

    def ensure_active_view_contains_turn_root(
        self,
        thread_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把已接受 Turn root 幂等补入当前 active view 的 item 索引。

        acceptance 可能先于 LangGraph 创建初始化 view；因此 acceptance 事务
        本身没有可更新的 view。provider dispatch 前再次执行这个 owner-side
        同步，避免首个 assembly 在 active view 已建立后仍看不到 root。只更新
        SQLite view membership，不改变 canonical JSONL 或 view head。
        """
        thread_id = strict_text(thread_id, field="thread_id")
        turn_id = strict_text(turn_id, field="turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        if not thread_id or not turn_id:
            raise ValueError("ensure active view root 缺少 thread_id/turn_id")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                if branch_row is None:
                    raise RuntimeError(
                        f"rollout namespace 缺少 active branch: {checkpoint_ns!r}"
                    )
                active_branch_id = strict_text(
                    branch_row[0],
                    field="checkpoint_namespace_state.active_branch_id",
                )
                view_row = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (active_branch_id,),
                ).fetchone()
                if view_row is None:
                    raise RuntimeError(
                        f"rollout active branch 不存在: {active_branch_id}"
                    )
                view_id = strict_optional_text(
                    view_row[0], field="branches.head_view_id"
                )
                if view_id is None:
                    return False
                root_row = connection.execute(
                    "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if root_row is None:
                    raise KeyError(f"Turn root 不存在: {turn_id}")
                item_id = strict_text(
                    root_row[0], field=f"turn_records.root_input_item_id:{turn_id}"
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM item_catalog WHERE item_id = ?",
                        (item_id,),
                    ).fetchone()
                    is None
                ):
                    raise RuntimeError(f"Turn root catalog 缺失: {item_id}")
                item_added = self._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=(item_id,),
                )
                if not item_added:
                    return False
                view_turn = connection.execute(
                    "SELECT 1 FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
                    (view_id, turn_id),
                ).fetchone()
                if view_turn is None:
                    ordinal_row = connection.execute(
                        "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 FROM context_view_turns WHERE view_id = ?",
                        (view_id,),
                    ).fetchone()
                    if ordinal_row is None:
                        raise RuntimeError(
                            f"active view Turn ordinal 无法读取: {view_id}"
                        )
                    logical_turn_ordinal = strict_non_negative_int(
                        ordinal_row[0],
                        field="context_view_turns.next_logical_turn_ordinal",
                    )
                    if logical_turn_ordinal == 0:
                        raise RuntimeError(
                            f"active view Turn ordinal 不能为 0: {view_id}"
                        )
                    turn_result = connection.execute(
                        "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                        (
                            view_id,
                            turn_id,
                            logical_turn_ordinal,
                            item_id,
                            _json({"source": "turn_root_reconciliation"}),
                        ),
                    )
                    if turn_result.rowcount != 1:
                        raise RuntimeError(
                            f"active view Turn root 写入失败: {view_id}/{turn_id}"
                        )
                    header_result = connection.execute(
                        "UPDATE context_views SET head_turn_id = ?, logical_turn_count = MAX(logical_turn_count, ?) WHERE view_id = ?",
                        (turn_id, logical_turn_ordinal, view_id),
                    )
                    if header_result.rowcount != 1:
                        raise RuntimeError(f"active view header 更新失败: {view_id}")
                connection.commit()
                return True
