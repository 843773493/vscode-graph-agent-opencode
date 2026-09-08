"""v2 checkpoint/view/anchor durable owner。

这里只调用 RolloutStorage 提供的 SQLite、JSONL 和 domain ports，不创建
LangChain message，也不读取 v1 数据。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.services.infrastructure.rollout_context.checkpoint.message_commit.prepare import (
    prepare_messages,
)
from app.services.infrastructure.rollout_context.storage.guards import (
    register_jsonl_guard,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_storage_commit,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _v2_json_line(value: object) -> bytes:
    return canonical_json_line(value)


class RolloutCheckpointPersistenceMixin:
    """LangGraph checkpoint 的 v2 durable commit owner。"""

    def append_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        parent_checkpoint_id: str | None,
        current_messages: list[object],
        branch_id: str | None = None,
    ) -> None:
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            jsonl = self.jsonl_path(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                # append_checkpoint 的 JSONL 与 SQLite 共享一个失败回滚边界。
                # _RolloutSQLiteConnection.__exit__ 会在任意异常退出时把已
                # fsync 但尚未提交到 catalog/commit 的尾部截回原坐标。
                register_jsonl_guard(
                    connection,
                    jsonl,
                    original_size=jsonl.stat().st_size,
                )
                meta = connection.execute(
                    "SELECT last_commit_id, last_message_sequence, "
                    "committed_jsonl_offset, last_item_sequence "
                    "FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    raise RuntimeError("rollout database_meta 缺失")
                self._require_v2_runtime(connection)
                checkpoint_id = checkpoint.get("id")
                if not isinstance(checkpoint_id, str) or not checkpoint_id:
                    raise ValueError("checkpoint 缺少字符串 id")
                checkpoint_core = {
                    key: value
                    for key, value in checkpoint.items()
                    if key
                    not in {
                        "channel_values",
                        "channel_versions",
                        "updated_channels",
                        "versions_seen",
                        "pending_sends",
                    }
                }
                encoded_checkpoint_core = _json(checkpoint_core)
                encoded_metadata = _json(metadata)
                checkpoint_version_value = checkpoint_core.get("v", 2)
                if (
                    not isinstance(checkpoint_version_value, int)
                    or isinstance(checkpoint_version_value, bool)
                    or checkpoint_version_value < 0
                ):
                    raise ValueError("checkpoint.v 必须是非负整数")
                checkpoint_timestamp_value = checkpoint_core.get("ts", _now())
                if not isinstance(checkpoint_timestamp_value, str) or not checkpoint_timestamp_value:
                    raise ValueError("checkpoint.ts 必须是非空字符串")
                existing_checkpoint = connection.execute(
                    "SELECT commit_id, checkpoint_json, metadata_json, status FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                    (checkpoint_id, checkpoint_ns),
                ).fetchone()
                repeated_checkpoint = False
                if existing_checkpoint is not None:
                    existing_commit_id = strict_non_negative_int(
                        existing_checkpoint[0], field="checkpoints.commit_id"
                    )
                    existing_status = strict_text(
                        existing_checkpoint[3], field="checkpoints.status"
                    )
                    if (
                        existing_status == "active"
                        and existing_checkpoint[1] == encoded_checkpoint_core
                        and existing_checkpoint[2] == encoded_metadata
                    ):
                        load_committed_storage_commit(
                            connection,
                            commit_id=existing_commit_id,
                        )
                        repeated_checkpoint = True
                    else:
                        raise ValueError(
                            f"checkpoint_id 已存在但内容不一致: {checkpoint_id}"
                        )
                active_branch, _projection_epoch = self._namespace_state(
                    connection, checkpoint_ns
                )
                if branch_id is not None and branch_id != active_branch:
                    raise RuntimeError(
                        f"checkpoint branch 不是 active branch: {branch_id}"
                    )
                parent = self._checkpoint_row(
                    connection,
                    checkpoint_ns,
                    parent_checkpoint_id,
                )
                jsonl_offset_before = strict_non_negative_int(meta[2], field="database_meta.committed_jsonl_offset")
                if jsonl.stat().st_size != jsonl_offset_before:
                    raise RuntimeError("checkpoint append 前 JSONL 与 committed offset 不一致")
                batch = prepare_messages(
                    connection=connection, jsonl=jsonl, codec=self._codec(),
                    read_item=self._read_v2_item_at, messages=current_messages,
                    last_message_sequence=strict_non_negative_int(meta[1], field="database_meta.last_message_sequence"),
                    next_item_sequence=strict_non_negative_int(meta[3], field="database_meta.last_item_sequence") + 1,
                    offset=jsonl_offset_before,
                )
                prepared = batch.prepared
                first_sequence = batch.first_message_sequence
                last_sequence = batch.last_message_sequence
                next_item_sequence = batch.next_item_sequence
                visible_sequences = batch.visible_sequences
                if repeated_checkpoint:
                    if prepared:
                        raise ValueError("重复 checkpoint 的 canonical message group 发生变化")
                    old_view = connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ?",
                        (checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    ranges = connection.execute(
                        "SELECT start_message_sequence, end_message_sequence FROM context_view_ranges "
                        "WHERE view_id = ? AND source_kind = 'messages' ORDER BY range_index",
                        (old_view[0],),
                    ).fetchall()
                    original_sequences = [
                        sequence for start, end in ranges
                        for sequence in range(
                            strict_non_negative_int(start, field="context_view_ranges.start_message_sequence"),
                            strict_non_negative_int(end, field="context_view_ranges.end_message_sequence") + 1,
                        )
                    ]
                    if original_sequences != visible_sequences:
                        raise ValueError("重复 checkpoint 的消息顺序或成员发生变化")
                    return
                # SQLite 事务必须先建立，再写 JSONL。这样 JSONL append、catalog、
                # checkpoint/view 和 database_meta 才共享同一个 rollback guard；
                # 不能先写文件再 BEGIN，否则失败窗口会留下无法由事务回滚的尾部。
                connection.execute("BEGIN IMMEDIATE")
                if prepared:
                    with jsonl.open("ab") as stream:
                        for row in prepared:
                            stream.write(row[7])
                        stream.flush()
                        os.fsync(stream.fileno())
                transaction_id = uuid4().hex
                timestamp = _now()
                jsonl_offset_after = jsonl.stat().st_size
                new_jsonl_records = sum(len(row[10]) for row in prepared)
                if new_jsonl_records:
                    commit_cursor = connection.execute(
                        "INSERT INTO storage_commits(transaction_id, first_message_sequence, last_message_sequence, jsonl_start_offset, jsonl_end_offset, jsonl_fsync_at, status, created_at, commit_kind, commit_mode, jsonl_offset_before, jsonl_offset_after, jsonl_record_count, subject_id, idempotency_key, outcome, metadata_json) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, 'item_convergence', 'item_bearing', ?, ?, ?, ?, ?, NULL, '{}')",
                        (
                            transaction_id,
                            first_sequence,
                            last_sequence if first_sequence is not None else None,
                            jsonl_offset_before,
                            jsonl_offset_after,
                            timestamp,
                            timestamp,
                            jsonl_offset_before,
                            jsonl_offset_after,
                            new_jsonl_records,
                            checkpoint_id,
                            f"checkpoint-view:{checkpoint_id}",
                        ),
                    )
                    commit_id = strict_non_negative_int(
                        commit_cursor.lastrowid, field="storage_commits.commit_id"
                    )
                else:
                    if meta[0] is None:
                        raise RuntimeError(
                            "空 checkpoint 没有可引用的已提交 storage commit"
                        )
                    previous_commit = strict_non_negative_int(
                        meta[0], field="database_meta.last_commit_id"
                    )
                    load_committed_storage_commit(
                        connection,
                        commit_id=previous_commit,
                    )
                    commit_id = previous_commit
                for (
                    sequence,
                    message_id,
                    turn_id,
                    role,
                    serialized_json,
                    message_offset,
                    byte_length,
                    _raw,
                    visibility,
                    _envelope,
                    canonical_locations,
                ) in prepared:
                    message_value = json.loads(serialized_json)
                    content = (
                        message_value.get("data", {}).get("content")
                        if isinstance(message_value, Mapping)
                        else None
                    )
                    content_bytes = _json(content).encode("utf-8")
                    connection.execute(
                        "INSERT INTO messages(message_sequence, message_id, turn_id, role, jsonl_offset, jsonl_length, content_length, content_hash, visibility, commit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sequence,
                            message_id,
                            turn_id,
                            role,
                            message_offset,
                            byte_length,
                            len(content_bytes),
                            _hash_bytes(content_bytes),
                            visibility,
                            commit_id,
                            timestamp,
                        ),
                    )
                    self._insert_message_projection(
                        connection, sequence, message_value, timestamp
                    )
                    self._upsert_turn(
                        connection,
                        turn_id,
                        sequence,
                        message_id,
                        role,
                        active_branch,
                        timestamp,
                    )
                    for canonical_item, item_offset, item_length in canonical_locations:
                        self._insert_canonical_item(
                            connection,
                            canonical_item,
                            commit_id=commit_id,
                            jsonl_offset=item_offset,
                            jsonl_length=item_length,
                            created_at=timestamp,
                        )
                        # fork checkpoint 只是把 source 的临时 LangChain view
                        # 写入 target，真实 Turn identity 要等 canonical item
                        # remap 和 fork completion 一起建立。若在这里按 source
                        # turn_id 建 checkpoint-origin TurnRecord，completion
                        # 只能把它取消，最终留下 source/target 两套 Turn。
                        if metadata.get("source") != "fork":
                            self._ensure_v2_turn_record_from_root(
                                connection,
                                thread_id=thread_id,
                                item=canonical_item,
                                branch_id=active_branch,
                                timestamp=timestamp,
                            )
                active_view_row = connection.execute(
                    "SELECT head_view_id, head_checkpoint_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (active_branch,),
                ).fetchone()
                active_view_id = (
                    strict_optional_text(
                        active_view_row[0], field="branches.head_view_id"
                    )
                    if active_view_row is not None
                    else None
                )
                active_head_checkpoint_id = (
                    strict_optional_text(
                        active_view_row[1], field="branches.head_checkpoint_id"
                    )
                    if active_view_row is not None
                    else None
                )
                if (
                    active_view_row is not None
                    and active_view_id is None
                    and active_head_checkpoint_id is not None
                ):
                    raise RuntimeError(
                        "active branch 的 head_checkpoint_id 存在但 head_view_id 缺失"
                    )
                parent_view_id = (
                    active_view_id
                    if active_view_id is not None
                    else strict_optional_text(
                        parent[6], field="checkpoints.view_id"
                    )
                    if parent
                    else None
                )
                if active_view_id is not None:
                    active_view_sequences = set(
                        self._view_message_sequences_from_connection(
                            connection,
                            active_view_id,
                        )
                    )
                    # rewind 创建的 view 是一次上下文边界。重放 checkpoint
                    # 通常同时携带边界内的旧前缀和新的 assistant 消息；此时
                    # 父 view 中被替换的 canonical suffix 不能再次继承。只在
                    # 已有前缀与新消息同时出现时切换为 replacement，避免
                    # 增量 ToolMessage/新用户消息丢失 rewind 前缀。
                    active_view_kind = connection.execute(
                        "SELECT view_kind FROM context_views WHERE view_id = ?",
                        (active_view_id,),
                    ).fetchone()
                    if active_view_kind is None:
                        raise RuntimeError(
                            f"branches.head_view_id 引用了不存在的 context view: {active_view_id}"
                        )
                    active_view_kind_value = strict_text(
                        active_view_kind[0], field="context_views.view_kind"
                    )
                    is_rewind_replacement = (
                        active_view_kind_value == "rewind"
                        and any(
                            sequence in active_view_sequences
                            for sequence in visible_sequences
                        )
                        and any(
                            sequence not in active_view_sequences
                            for sequence in visible_sequences
                        )
                    )
                    if is_rewind_replacement:
                        parent_view_id = None
                channel_values = checkpoint.get("channel_values")
                has_compaction_event = isinstance(channel_values, Mapping) and (
                    "_summarization_event" in channel_values
                )
                view_kind = (
                    "compaction"
                    if has_compaction_event
                    or checkpoint.get("checkpoint_kind") == "compaction"
                    or metadata.get("source") == "compaction"
                    else "checkpoint"
                )
                checkpoint_kind = (
                    "compaction" if view_kind == "compaction" else "normal"
                )
                view_id = self._create_view(
                    connection,
                    active_branch,
                    parent_view_id,
                    visible_sequences,
                    timestamp,
                    view_kind=view_kind,
                )
                (
                    versions_seen_type,
                    versions_seen_blob,
                    versions_seen_length,
                    versions_seen_hash,
                ) = self._encode(checkpoint.get("versions_seen", {}))
                pending_type, pending_blob, pending_length, pending_hash = self._encode(
                    checkpoint.get("pending_sends", [])
                )
                message_sequence = max(visible_sequences, default=last_sequence)
                connection.execute(
                    "INSERT INTO checkpoints(checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_kind, status, checkpoint_json, metadata_json, envelope_serializer_name, versions_seen_type, versions_seen_blob, versions_seen_length, versions_seen_hash, pending_sends_type, pending_sends_blob, pending_sends_length, pending_sends_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint_id,
                        checkpoint_ns,
                        commit_id,
                        message_sequence,
                        len(current_messages),
                        parent_checkpoint_id,
                        view_id,
                        active_branch,
                        checkpoint_version_value,
                        checkpoint_timestamp_value,
                        checkpoint_kind,
                        encoded_checkpoint_core,
                        encoded_metadata,
                        "json",
                        versions_seen_type,
                        versions_seen_blob,
                        versions_seen_length,
                        versions_seen_hash,
                        pending_type,
                        pending_blob,
                        pending_length,
                        pending_hash,
                    ),
                )
                self._insert_checkpoint_channels(
                    connection, checkpoint_id, checkpoint, view_id, timestamp
                )
                branch_cursor = connection.execute(
                    "UPDATE branches SET head_view_id = ?, head_checkpoint_id = ?, updated_at = ? WHERE branch_id = ?",
                    (view_id, checkpoint_id, timestamp, active_branch),
                )
                if branch_cursor.rowcount != 1:
                    raise RuntimeError(
                        "checkpoint branch head 更新行数异常: "
                        f"branch_id={active_branch}, rowcount={branch_cursor.rowcount}"
                    )
                control_sequence = self._insert_control(
                    connection,
                    "checkpoint_created",
                    "checkpoint",
                    checkpoint_id,
                    active_branch,
                    view_id,
                    checkpoint_id,
                    self._checkpoint_control_payload(
                        connection,
                        checkpoint=checkpoint,
                        message_count=len(current_messages),
                    ),
                    transaction_id,
                    timestamp,
                )
                view_cursor = connection.execute(
                    "UPDATE context_views SET control_sequence = ? WHERE view_id = ?",
                    (control_sequence, view_id),
                )
                if view_cursor.rowcount != 1:
                    raise RuntimeError(
                        "checkpoint view control_sequence 更新行数异常: "
                        f"view_id={view_id}, rowcount={view_cursor.rowcount}"
                    )
                if new_jsonl_records:
                    commit_cursor = connection.execute(
                        "UPDATE storage_commits SET status = 'committed', committed_at = ? WHERE commit_id = ?",
                        (timestamp, commit_id),
                    )
                    if commit_cursor.rowcount != 1:
                        raise RuntimeError(
                            "checkpoint storage commit 收敛更新行数异常: "
                            f"commit_id={commit_id}, rowcount={commit_cursor.rowcount}"
                        )
                meta_cursor = connection.execute(
                    "UPDATE database_meta SET last_commit_id = ?, last_message_sequence = ?, last_control_sequence = ?, committed_jsonl_offset = ?, last_item_sequence = ?, history_view_revision = history_view_revision + 1, updated_at = ? WHERE singleton_id = 1",
                    (
                        commit_id,
                        last_sequence,
                        control_sequence,
                        jsonl_offset_after,
                        next_item_sequence - 1,
                        timestamp,
                    ),
                )
                if meta_cursor.rowcount != 1:
                    raise RuntimeError(
                        "checkpoint database_meta 更新行数异常: "
                        f"checkpoint_id={checkpoint_id}, rowcount={meta_cursor.rowcount}"
                    )
                self._commit_connection(connection)

    @staticmethod
    def _checkpoint_control_payload(
        connection: sqlite3.Connection,
        *,
        checkpoint: Checkpoint,
        message_count: int,
    ) -> dict[str, object]:
        """保存 checkpoint 控制信息，并保留 compaction 的 message cutoff。"""
        payload: dict[str, object] = {"message_count": message_count}
        channel_values = checkpoint.get("channel_values")
        event = (
            channel_values.get("_summarization_event")
            if isinstance(channel_values, Mapping)
            else None
        )
        if not isinstance(event, Mapping):
            return payload
        cutoff_index = event.get("cutoff_index")
        if isinstance(cutoff_index, int) and cutoff_index >= 0:
            payload["cutoff_index"] = cutoff_index
        cutoff_message_id = event.get("cutoff_message_id")
        if not isinstance(cutoff_message_id, str) or not cutoff_message_id:
            return payload
        row = connection.execute(
            "SELECT message_sequence FROM messages WHERE message_id = ?",
            (cutoff_message_id,),
        ).fetchone()
        if row is not None:
            payload["cutoff_message_id"] = cutoff_message_id
            payload["cutoff_message_sequence"] = strict_non_negative_int(
                row[0], field="messages.message_sequence"
            )
        return payload

    @staticmethod
    def _checkpoint_row(
        connection: sqlite3.Connection,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> tuple[object, ...] | None:
        if checkpoint_id is None:
            return connection.execute(
                "SELECT c.checkpoint_id, c.checkpoint_ns, c.commit_id, c.message_sequence, c.message_count, c.parent_checkpoint_id, c.view_id, c.branch_id, c.checkpoint_version, c.checkpoint_timestamp, c.checkpoint_json, c.metadata_json, c.versions_seen_type, c.versions_seen_blob, c.pending_sends_type, c.pending_sends_blob FROM checkpoints c JOIN branches b ON b.head_checkpoint_id = c.checkpoint_id WHERE b.branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?) AND c.checkpoint_ns = ? AND c.status = 'active' LIMIT 1",
                (checkpoint_ns, checkpoint_ns),
            ).fetchone()
        return connection.execute(
            "SELECT checkpoint_id, checkpoint_ns, commit_id, message_sequence, message_count, parent_checkpoint_id, view_id, branch_id, checkpoint_version, checkpoint_timestamp, checkpoint_json, metadata_json, versions_seen_type, versions_seen_blob, pending_sends_type, pending_sends_blob FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
            (checkpoint_id, checkpoint_ns),
        ).fetchone()
