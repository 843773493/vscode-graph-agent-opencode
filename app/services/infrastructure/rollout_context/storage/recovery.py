"""v2 JSONL/SQLite 提交边界的恢复工具。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.services.infrastructure.node_debug.session.thread_owner import MAIN_THREAD_ID
from app.services.infrastructure.rollout_context.fork.node_debug_materialization import (
    publish_target_snapshot,
    remove_target_snapshot,
    verify_published_target_snapshot,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    one_of_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

class RolloutRecoveryMixin:
    """负责启动阶段 fork/compaction journal 的恢复，不读业务 projection。"""
    def _recover_fork_materialization(
        self,
        thread_id: str,
        checkpoint_ns: str,
        connection: sqlite3.Connection,
        jsonl_path: Path,
    ) -> None:
        """恢复子 rollout 的 fork 两阶段提交日志。

        ``prepared`` 只可能指向一个尚未对外可见的目标副本，直接清空目标
        rollout；``target_committed`` 已经拥有完整的目标数据，只需补做父库
        pinned retention。这里不从 JSONL 推断任何 checkpoint 或上下文状态。
        """
        rows = connection.execute(
            """
            SELECT materialization_id, fork_id, source_session_id,
                   source_checkpoint_id, source_view_id, relationship, status
            FROM fork_materializations
            WHERE status IN ('prepared', 'target_committed')
            ORDER BY created_at
            """
        ).fetchall()
        if not rows:
            return
        if len(rows) != 1:
            raise RuntimeError(
                "fork materialization 存在多条未完成 journal，拒绝猜测恢复目标"
            )
        row = rows[0]

        (
            materialization_id,
            fork_id,
            source_session_id,
            source_checkpoint_id,
            source_view_id,
            relationship,
            status,
        ) = row
        materialization_id = required_text(
            materialization_id, field="fork_materializations.materialization_id"
        )
        fork_id = required_text(fork_id, field="fork_materializations.fork_id")
        source_session_id = required_text(
            source_session_id,
            field="fork_materializations.source_session_id",
        )
        source_checkpoint_id = optional_text(
            source_checkpoint_id,
            field="fork_materializations.source_checkpoint_id",
        )
        source_view_id = optional_text(
            source_view_id, field="fork_materializations.source_view_id"
        )
        relationship = one_of_text(
            relationship,
            {"detached", "pinned"},
            field="fork_materializations.relationship",
        )
        status = one_of_text(
            status,
            {"prepared", "target_committed"},
            field="fork_materializations.status",
        )
        if status == "target_committed":
            debug_row = connection.execute(
                "SELECT lineage_json FROM fork_identity_mappings "
                "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
                (fork_id,),
            ).fetchone()
            if debug_row is not None:
                lineage = json.loads(
                    required_text(debug_row[0], field="debug_snapshot.lineage")
                )
                if not isinstance(lineage, dict) or lineage.get("state") not in {
                    "ready",
                    "published",
                }:
                    raise RuntimeError("fork target_committed 的 debug snapshot 状态非法")
                target_node = self._path_resolver.resolve_session_node_for_runtime(
                    thread_id
                )
                if lineage["state"] == "ready":
                    relative = Path(
                        required_text(
                            lineage.get("target_debug_staging_path"),
                            field="debug_snapshot.target_debug_staging_path",
                        )
                    )
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RuntimeError("fork debug staging journal 路径非法")
                    staging_root = target_node / relative
                    published_root = target_node / "debug" / "node"
                    if staging_root.is_dir() and not staging_root.is_symlink():
                        publish_target_snapshot(staging_root, target_node)
                    elif not published_root.is_dir() or published_root.is_symlink():
                        raise RuntimeError(
                            "fork target_committed 缺少 ready debug staging/published artifact"
                        )
                verify_published_target_snapshot(
                    target_node,
                    target_session_id=thread_id,
                    target_thread_id=MAIN_THREAD_ID,
                    manifest_sha256=required_text(
                        lineage.get("target_manifest_sha256"),
                        field="debug_snapshot.target_manifest_sha256",
                    ),
                    source_configurations_json=json.dumps(
                        lineage.get("source_configurations")
                    ),
                    configuration_id_map_json=json.dumps(
                        lineage.get("target_configuration_id_map")
                    ),
                )
                if lineage["state"] == "ready":
                    lineage["state"] = "published"
                    lineage["published_at"] = _now()
                    result = connection.execute(
                        "UPDATE fork_identity_mappings SET lineage_json = ? "
                        "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
                        (
                            json.dumps(lineage, ensure_ascii=False, sort_keys=True),
                            fork_id,
                        ),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("fork recovery 未收敛 debug published journal")
            if relationship == "pinned":
                source_root = self.root(source_session_id, checkpoint_ns)
                if not source_root.is_dir() or not self.index_path(
                    source_session_id, checkpoint_ns
                ).is_file():
                    error_message = (
                        "fork 已提交目标 rollout，但 pinned 父 rollout 不存在，"
                        "无法安全恢复 retention"
                    )
                    connection.execute(
                        "UPDATE database_meta SET database_state = 'recovery_required', updated_at = ? WHERE singleton_id = 1",
                        (_now(),),
                    )
                    connection.execute(
                        "UPDATE fork_materializations SET error_message = ? WHERE materialization_id = ?",
                        (error_message, materialization_id),
                    )
                    connection.commit()
                    raise RuntimeError(error_message)
                self._retain_fork_source(
                    source_session_id=source_session_id,
                    source_checkpoint_id=source_checkpoint_id,
                    source_view_id=source_view_id,
                    fork_id=fork_id,
                    owner_session_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                )
            result = connection.execute(
                "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ?",
                (_now(), materialization_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork recovery 未完成 committed 状态收敛: {materialization_id}"
                )
            connection.commit()
            return

        connection.execute("BEGIN IMMEDIATE")
        debug_row = connection.execute(
            "SELECT lineage_json FROM fork_identity_mappings "
            "WHERE fork_id = ? AND entity_type = 'debug_snapshot'",
            (fork_id,),
        ).fetchone()
        if debug_row is not None:
            lineage = json.loads(
                required_text(debug_row[0], field="debug_snapshot.lineage")
            )
            if not isinstance(lineage, dict) or lineage.get("state") not in {
                "prepared",
                "ready",
                "published",
            }:
                raise RuntimeError("prepared fork 的 debug snapshot journal 损坏")
            target_node = self._path_resolver.resolve_session_node_for_runtime(thread_id)
            relative = Path(
                required_text(
                    lineage.get("target_debug_staging_path"),
                    field="debug_snapshot.target_debug_staging_path",
                )
            )
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("fork debug staging journal 路径非法")
            staging_root = target_node / relative
            published_root = target_node / "debug" / "node"
            has_staging = staging_root.exists() or staging_root.is_symlink()
            has_published = published_root.exists() or published_root.is_symlink()
            if lineage["state"] == "prepared" and not has_staging:
                raise RuntimeError("prepared fork 缺少 debug staging")
            if lineage["state"] == "ready" and has_staging == has_published:
                raise RuntimeError("ready fork 的 debug staging/published 状态不唯一")
            if lineage["state"] == "published" and not has_published:
                raise RuntimeError("published fork 缺少 debug artifact")
            if has_published:
                verify_published_target_snapshot(
                    target_node,
                    target_session_id=thread_id,
                    target_thread_id=MAIN_THREAD_ID,
                    manifest_sha256=required_text(
                        lineage.get("target_manifest_sha256"),
                        field="debug_snapshot.target_manifest_sha256",
                    ),
                    source_configurations_json=json.dumps(
                        lineage.get("source_configurations")
                    ),
                    configuration_id_map_json=json.dumps(
                        lineage.get("target_configuration_id_map")
                    ),
                )
            remove_target_snapshot(
                staging_root,
                target_node,
                remove_published=has_published,
            )
        if not jsonl_path.is_file() or jsonl_path.is_symlink():
            raise RuntimeError(
                f"prepared fork recovery 缺少安全的 JSONL 文件: {jsonl_path}"
            )
        with jsonl_path.open("r+b") as stream:
            stream.truncate(0)
            stream.flush()
            os.fsync(stream.fileno())
        for table in (
            "control_events",
            "messages",
            "message_projections",
            "item_projections",
            "tool_calls",
            "reasoning_blocks",
            "turns",
            "context_view_turns",
            "context_view_ranges",
            "context_view_jumps",
            "context_views",
            "checkpoint_channels",
            "pending_writes",
            "checkpoints",
            "branches",
            "checkpoint_namespace_state",
            "storage_commits",
            "fork_origins",
            "retention_refs",
            "item_catalog",
            "item_parts",
            "operation_anchors",
            "turn_acceptances",
            "fork_identity_mappings",
            "turn_records",
            "executions",
            "model_calls",
            "turn_execution_links",
            "context_assemblies",
            "assembly_item_refs",
            "tool_set_snapshots",
            "context_assembly_contributions",
            "context_assembly_selections",
            "context_contributions",
            "context_view_items",
            "source_overlays",
            "context_plan_details",
            "legacy_migration_reports",
        ):
            connection.execute(f"DELETE FROM {table}")
        timestamp = _now()
        result = connection.execute(
            "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, created_at, updated_at) VALUES ('branch-001', 'root', 'active', NULL, NULL, ?, ?)",
            (timestamp, timestamp),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重建 root branch")
        result = connection.execute(
            "INSERT INTO checkpoint_namespace_state(checkpoint_ns, active_branch_id, projection_epoch, created_at, updated_at) VALUES (?, 'branch-001', 1, ?, ?)",
            (checkpoint_ns, timestamp, timestamp),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重建 checkpoint namespace state")
        result = connection.execute(
            "UPDATE database_meta SET database_state = 'active', last_commit_id = NULL, last_message_sequence = 0, last_control_sequence = 0, committed_jsonl_offset = 0, active_branch_id = 'branch-001', projection_epoch = 1, history_view_revision = 0, source_overlay_epoch = 0, updated_at = ? WHERE singleton_id = 1",
            (timestamp,),
        )
        if result.rowcount != 1:
            raise RuntimeError("fork recovery 未重置 database_meta")
        result = connection.execute(
            "UPDATE fork_materializations SET status = 'aborted', error_message = ?, committed_at = NULL WHERE materialization_id = ?",
            ("fork 物化中断，已回滚未提交的目标 rollout", materialization_id),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"fork recovery 未标记 aborted: {materialization_id}"
            )
        connection.commit()
