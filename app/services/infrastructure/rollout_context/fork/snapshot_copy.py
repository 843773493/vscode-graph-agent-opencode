"""冻结 rollout 的原字节安装；历史 scope 与目标存储 owner 分开。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.resource_uri import parse_resource_uri
from app.core.sqlite_state import load_sql, utc_now_text
from app.services.infrastructure.rollout_context.fork.copy_items import (
    prune_view_prefix,
    select_view_prefix,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    HistoricalNamespaceBinding,
    bind_captured_namespace,
    current_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    clone_members,
    replace_members,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    HISTORY_NAMESPACE_SCHEMA_SQL,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.fork.source_snapshot import (
        ForkSourceSnapshot,
    )
    from app.services.infrastructure.rollout_context.fork.store import RolloutForkStore
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )

_COPY_BUFFER_BYTES = 1024 * 1024


def stage_captured_rollout(
    snapshot: ForkSourceSnapshot, *, target_root: Path, materialization_id: str,
) -> Path:
    """逐文件验证并复制已捕获产物，内存不随文件正文长度增长。"""
    stage = target_root.with_name(f".fork-{materialization_id}.rollout")
    stage.mkdir(mode=0o700, exist_ok=False)
    try:
        source_root = snapshot.rollout.captured_root / "rollout"
        for captured in snapshot.rollout.files:
            relative = Path(captured.relative_path)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("source-mismatch: captured rollout 文件 locator 非法")
            source = source_root / relative
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"source-mismatch: captured rollout 不是普通文件: {relative}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            length = 0
            with source.open("rb") as reader, destination.open("xb") as writer:
                while chunk := reader.read(_COPY_BUFFER_BYTES):
                    writer.write(chunk)
                    digest.update(chunk)
                    length += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if (length, digest.hexdigest()) != (captured.size_bytes, captured.sha256):
                raise RuntimeError(f"source-mismatch: captured rollout 文件 hash/length 不一致: {relative}")
        return stage
    except BaseException:
        shutil.rmtree(stage)
        raise


def prepare_target_snapshot(
    storage: RolloutStorage, snapshot: ForkSourceSnapshot, *, stage: Path,
    fork_store: RolloutForkStore,
    target_session_id: str, target_thread_id: str, source_uri: str,
    source_view_id: str, checkpoint_ns: str, materialization_id: str,
    prefix: bool, anchor_item_id: str | None, anchor_mode: str,
    source_view_revision: int | None,
    source_checkpoint_id: str | None,
) -> str:
    """只创建目标协调事实与 view；历史业务行、封存 hash 和 bytes 不重写。"""
    parsed = parse_resource_uri(source_uri)
    if parsed.scope != "workspace" or parsed.kind != "session" or parsed.tail_segments != (
        snapshot.source_session_id, "threads", snapshot.source_thread_id,
    ):
        raise ValueError("source-mismatch: namespace binding 必须使用精确来源 Thread URI")
    with storage._connect(target_thread_id, checkpoint_ns,
                          session_id=target_session_id, read_only=True) as target:
        journal = target.execute(
            "SELECT * FROM fork_materializations WHERE materialization_id = ? "
            "AND target_session_id = ? AND target_thread_id = ? AND checkpoint_ns = ? AND status = 'prepared'",
            (materialization_id, target_session_id, target_thread_id, checkpoint_ns),
        ).fetchone()
        if journal is None:
            raise RuntimeError("snapshot 安装缺少目标 prepared materialization journal")
        if target.execute("SELECT 1 FROM item_catalog LIMIT 1").fetchone() is not None or (
            target.execute("SELECT 1 FROM checkpoints LIMIT 1").fetchone() is not None
        ):
            raise ValueError("fork target Thread 必须是本次创建的空目标")
        target_database_created_at = target.execute(
            "SELECT created_at FROM database_meta WHERE singleton_id = 1",
        ).fetchone()[0]
        columns = tuple(row[1] for row in target.execute("PRAGMA table_info(fork_materializations)"))
    with closing(sqlite3.connect(stage / "index.sqlite")) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(HISTORY_NAMESPACE_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(load_sql(__package__, "sql/drop_context_epoch_pending_namespace.sql"))
        connection.execute(
            load_sql(__package__, "sql/context_epoch_pending_namespace.sql"),
        )
        if current_scope(connection) != (snapshot.source_session_id, snapshot.source_thread_id):
            raise ValueError("source-mismatch: captured SQLite 与精确 source Thread 不一致")
        source_rollout_id = connection.execute(
            "SELECT rollout_id FROM database_meta WHERE singleton_id = 1",
        ).fetchone()[0]
        bind_captured_namespace(connection, HistoricalNamespaceBinding(
            session_id=snapshot.source_session_id, thread_id=snapshot.source_thread_id,
            source_uri=parsed.display_uri, source_rollout_id=source_rollout_id,
            source_snapshot_sha256=snapshot.snapshot_sha256,
        ))
        if prefix:
            select_view_prefix(
                storage, connection, stage=stage, source_view_id=source_view_id,
                anchor_item_id=anchor_item_id, anchor_mode=anchor_mode,
                source_view_revision=source_view_revision,
                source_checkpoint_id=source_checkpoint_id,
            )
        # now 是目标 fork 安装时新建 view/branch 的记账时刻，不是来源执行事件时间。
        # 来源 item/message 的业务时间由复制的历史事实保留。
        now = utc_now_text()
        connection.execute(
            "UPDATE database_meta SET session_id = ?, thread_id = ?, rollout_id = ?, "
            "created_at = ?, updated_at = ?, history_view_revision = history_view_revision + 1 "
            "WHERE singleton_id = 1",
            (target_session_id, target_thread_id, uuid4().hex, target_database_created_at, now),
        )
        connection.execute(
            f"INSERT INTO fork_materializations ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})", tuple(journal),
        )
        namespaces = connection.execute(
            "SELECT n.checkpoint_ns, b.head_view_id FROM checkpoint_namespace_state n "
            "JOIN branches b ON b.branch_id = n.active_branch_id ORDER BY n.checkpoint_ns",
        )
        requested_view = None
        for namespace, old_view in namespaces:
            frozen_view = source_view_id if namespace == checkpoint_ns else old_view
            view_id = _new_target_view(
                connection, source_view_id=frozen_view, now=now,
                selected_prefix=prefix and namespace == checkpoint_ns,
            )
            branch_id = connection.execute(
                "SELECT branch_id FROM context_views WHERE view_id = ?", (view_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE checkpoint_namespace_state SET active_branch_id = ?, "
                "projection_epoch = projection_epoch + 1, updated_at = ? WHERE checkpoint_ns = ?",
                (branch_id, now, namespace),
            )
            if namespace == checkpoint_ns:
                requested_view = view_id
                connection.execute(
                    "UPDATE database_meta SET active_branch_id = ?, projection_epoch = projection_epoch + 1 "
                    "WHERE singleton_id = 1", (branch_id,),
                )
        if requested_view is None:
            raise ValueError("source-mismatch: captured SQLite 缺少请求的 checkpoint namespace")
        if prefix:
            # 原 checkpoint 仍完整时由唯一 owner 复制非消息状态；新 head 的
            # messages 只引用目标 selection。随后只保留依赖齐全的历史 checkpoint。
            target_checkpoint_id = fork_store.materialize_fork_checkpoint(
                materialization_id=materialization_id, target_session_id=target_session_id,
                target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns,
                view_id=requested_view, staging_connection=connection,
            )
            if target_checkpoint_id is not None:
                connection.execute(
                    "INSERT INTO fork_selected_checkpoints(checkpoint_id) VALUES (?)",
                    (target_checkpoint_id,),
                )
            prune_view_prefix(connection, stage=stage)
        connection.commit()
    _fsync_tree(stage)
    return requested_view


def _new_target_view(
    connection: sqlite3.Connection, *, source_view_id: str, now: str,
    selected_prefix: bool,
) -> str:
    if connection.execute(
        "SELECT 1 FROM context_views WHERE view_id = ?", (source_view_id,),
    ).fetchone() is None:
        raise ValueError("source-mismatch: captured SQLite 缺少冻结 source view")
    branch_id, view_id = uuid4().hex, uuid4().hex
    connection.execute(
        "INSERT INTO branches (branch_id, branch_kind, status, head_view_id, "
        "head_checkpoint_id, parent_branch_id, created_at, updated_at) "
        "VALUES (?, 'fork', 'active', ?, NULL, NULL, ?, ?)",
        (branch_id, view_id, now, now),
    )
    if selected_prefix:
        header = connection.execute(
            "SELECT (SELECT item.turn_id FROM fork_selected_items selected "
            "JOIN item_catalog item ON item.item_id=selected.item_id "
            "WHERE selected.visible_ordinal IS NOT NULL ORDER BY selected.visible_ordinal DESC LIMIT 1),"
            "COALESCE((SELECT MAX(message_sequence) FROM fork_visible_messages),0),"
            "(SELECT COUNT(DISTINCT item.turn_id) FROM fork_selected_items selected "
            "JOIN item_catalog item ON item.item_id=selected.item_id WHERE selected.visible_ordinal IS NOT NULL)",
        ).fetchone()
    else:
        header = connection.execute(
            "SELECT head_turn_id,head_message_sequence,logical_turn_count FROM context_views WHERE view_id=?",
            (source_view_id,),
        ).fetchone()
    connection.execute(
        "INSERT INTO context_views (view_id, branch_id, parent_view_id, view_kind, "
        "head_turn_id, head_message_sequence, logical_turn_count, control_sequence, created_at) "
        "VALUES (?, ?, NULL, 'fork', ?, ?, ?, NULL, ?)", (view_id, branch_id, *header, now),
    )
    if selected_prefix:
        members = connection.execute(
            "SELECT selected.item_id,selected.visible_ordinal,1,member.source_kind,member.context_selected,"
            "CASE WHEN member.context_selected=1 THEN SUM(member.context_selected) OVER "
            "(ORDER BY CASE WHEN member.context_selected=1 THEN member.context_ordinal END NULLS LAST)-1 END "
            "FROM fork_selected_items selected JOIN context_view_items member "
            "ON member.item_id=selected.item_id AND member.view_id=? "
            "WHERE selected.visible_ordinal IS NOT NULL", (source_view_id,),
        ).fetchall()
        members += connection.execute(
            "SELECT selected.item_id,member.logical_item_ordinal,0,member.source_kind,0,NULL "
            "FROM fork_selected_items selected JOIN context_view_items member "
            "ON member.item_id=selected.item_id AND member.view_id=? "
            "WHERE selected.visible_ordinal IS NULL", (source_view_id,),
        ).fetchall()
        replace_members(connection, view_id, members)
        connection.execute(
            "INSERT INTO context_view_turns(view_id,turn_id,logical_turn_ordinal,user_message_sequence,"
            "final_message_sequence,root_input_item_id,fork_lineage_json) "
            "SELECT ?,turn_id,logical_turn_ordinal,user_message_sequence,"
            "CASE WHEN final_message_sequence IN (SELECT message_sequence FROM fork_visible_messages) "
            "THEN final_message_sequence END,root_input_item_id,fork_lineage_json "
            "FROM context_view_turns old WHERE view_id=? AND EXISTS "
            "(SELECT 1 FROM fork_selected_items selected JOIN item_catalog item "
            "ON item.item_id=selected.item_id WHERE item.turn_id=old.turn_id "
            "AND selected.visible_ordinal IS NOT NULL)", (view_id, source_view_id),
        )
        connection.execute(
            "INSERT INTO context_view_ranges(view_id,range_index,source_kind,start_message_sequence,end_message_sequence) "
            "SELECT ?,ROW_NUMBER() OVER (ORDER BY MIN(message_sequence))-1,'messages',"
            "MIN(message_sequence),MAX(message_sequence) FROM "
            "(SELECT message_sequence,message_sequence-ROW_NUMBER() OVER "
            "(ORDER BY message_sequence) AS island FROM fork_visible_messages) GROUP BY island",
            (view_id,),
        )
    else:
        clone_members(connection, source_view_id, view_id)
        for table, columns in (
            ("context_view_turns", ("turn_id,logical_turn_ordinal,user_message_sequence,"
                                   "final_message_sequence,root_input_item_id,fork_lineage_json")),
        ):
            connection.execute(
                f"INSERT INTO {table}(view_id,{columns}) SELECT ?,{columns} "
                f"FROM {table} WHERE view_id=?", (view_id, source_view_id),
            )
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(context_view_ranges)") if row[1] != "view_id")
        names = ",".join(columns)
        connection.execute(
            f"INSERT INTO context_view_ranges(view_id,{names}) SELECT ?,{names} "
            "FROM context_view_ranges WHERE view_id=?", (view_id, source_view_id),
        )
    return view_id


def installed_target_view(
    storage: RolloutStorage, *, target_session_id: str, target_thread_id: str,
    checkpoint_ns: str, materialization_id: str, source_snapshot_sha256: str,
) -> str | None:
    """硬退出后只核当前目标和既有 operation，不把继承 journal 当新操作。"""
    with storage._connect(target_thread_id, checkpoint_ns,
                          session_id=target_session_id, read_only=True) as connection:
        if current_scope(connection) != (target_session_id, target_thread_id):
            raise RuntimeError("snapshot 恢复当前目标 owner 不一致")
        journal = connection.execute(
            "SELECT source_snapshot_sha256,status FROM fork_materializations "
            "WHERE materialization_id=? AND target_session_id=? AND target_thread_id=? AND checkpoint_ns=?",
            (materialization_id, target_session_id, target_thread_id, checkpoint_ns),
        ).fetchone()
        if journal is None or journal[0] != source_snapshot_sha256 or journal[1] == "aborted":
            raise RuntimeError("snapshot 恢复与冻结目标 journal 不一致")
        row = connection.execute(
            "SELECT b.branch_kind,b.head_view_id FROM checkpoint_namespace_state n "
            "JOIN branches b ON b.branch_id=n.active_branch_id WHERE n.checkpoint_ns=?",
            (checkpoint_ns,),
        ).fetchone()
        return row[1] if row is not None and row[0] == "fork" else None


def recover_target_snapshot_publication(
    storage: RolloutStorage, *, target_session_id: str, target_thread_id: str,
    checkpoint_ns: str, materialization_id: str,
) -> None:
    """两次 rename 之间退出时按 intent 定点还原空目标，随后重放冻结 capture。"""
    target = storage.root(target_session_id, checkpoint_ns, thread_id=target_thread_id)
    stage = target.with_name(f".fork-{materialization_id}.rollout")
    rollback = target.with_name(f".fork-{materialization_id}.rollback")
    with storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id):
        if rollback.exists():
            if not target.exists():
                rollback.rename(target)
                if stage.exists():
                    shutil.rmtree(stage)
                storage._fsync_directory(target.parent)
            elif stage.exists():
                raise RuntimeError("fork 发布恢复出现冲突的 target/stage/rollback")
        elif stage.exists():
            shutil.rmtree(stage)
            storage._fsync_directory(target.parent)


def publish_target_snapshot(
    storage: RolloutStorage, *, stage: Path, target_session_id: str,
    target_thread_id: str, checkpoint_ns: str, materialization_id: str,
) -> None:
    """目标排他锁内安装私有目录；rollback 保留至本次 journal 完成。"""
    target = storage.root(target_session_id, checkpoint_ns, thread_id=target_thread_id)
    rollback = target.with_name(f".fork-{materialization_id}.rollback")
    with storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id):
        target.rename(rollback)
        storage._fsync_directory(target.parent)
        try:
            stage.rename(target)
            storage._fsync_directory(target.parent)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            rollback.rename(target)
            storage._fsync_directory(target.parent)
            raise


def finalize_target_snapshot_publication(
    storage: RolloutStorage, *, target_session_id: str, target_thread_id: str,
    checkpoint_ns: str, materialization_id: str,
) -> None:
    target = storage.root(target_session_id, checkpoint_ns, thread_id=target_thread_id)
    rollback = target.with_name(f".fork-{materialization_id}.rollback")
    with storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id):
        if rollback.exists():
            shutil.rmtree(rollback)
            storage._fsync_directory(target.parent)


def _fsync_tree(root: Path) -> None:
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            with (Path(directory) / name).open("rb") as stream:
                os.fsync(stream.fileno())
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
