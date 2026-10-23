"""v2 checkpoint/view/anchor durable owner。

这里只调用 RolloutContextStore 提供的 SQLite、JSONL 和 domain ports，不创建
LangChain message，也不读取 v1 数据。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from uuid import uuid4

from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata

from app.core.execution_admission_context import current_execution_admission
from app.core.hashing import sha256_hex as _hash_bytes
from app.core.session_thread_context import require_current_session_thread_owner
from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
    RebuildContextEpochIntent,
)
from app.services.infrastructure.rollout_context.activation.reads import (
    read_activation_refs_for_view,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.message_writes import (
    PendingMessageReferences,
    publish_message_write_references,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.writes import (
    _pending_write_row,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.compaction import (
    new_compaction_event,
    prepare_compaction,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.prepare import (
    prepare_messages,
)
from app.services.infrastructure.rollout_context.storage.catalog.unreferenced_content import (
    collect_unreferenced_content,
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
        messages_state: str = "view",
        new_versions: ChannelVersions,
        branch_id: str | None = None,
        consume_epoch_intent: Callable[[RebuildContextEpochIntent, sqlite3.Connection], None] | None = None,
    ) -> None:
        owner = require_current_session_thread_owner(
            langgraph_thread_id=thread_id
        )
        with self._storage._lock(
            owner.thread_id,
            checkpoint_ns,
            session_id=owner.session_id,
        ):
            self._storage.initialize(
                owner.session_id,
                checkpoint_ns,
                thread_id=owner.thread_id,
            )
            jsonl = self._storage.jsonl_path(
                owner.session_id,
                checkpoint_ns,
                thread_id=owner.thread_id,
            )
            with self._storage._connect(
                owner.thread_id,
                checkpoint_ns,
                session_id=owner.session_id,
            ) as connection:
                meta = connection.execute(
                    "SELECT last_commit_id, last_message_sequence, "
                    "committed_jsonl_offset, last_item_sequence "
                    "FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if meta is None:
                    raise RuntimeError("rollout database_meta 缺失")
                self._storage._require_v2_runtime(connection)
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
                admission = current_execution_admission(langgraph_thread_id=thread_id)
                if admission is not None and admission.execution_origin == "internal":
                    binding = connection.execute(
                        "SELECT admission_payload_hash FROM executions WHERE session_id=? AND thread_id=? "
                        "AND execution_id=? AND job_id=? AND admission_idempotency_key=? "
                        "AND execution_origin='internal' AND turn_id IS NULL",
                        (owner.session_id, owner.thread_id, admission.execution_id, admission.job_id,
                         admission.admission_idempotency_key),
                    ).fetchone()
                    if binding is None:
                        raise RuntimeError("internal checkpoint 缺少同 owner 的真实 execution admission")
                    metadata = {**metadata, "boxteam_execution_binding": {
                        "execution_id": admission.execution_id,
                        "job_id": admission.job_id,
                        "admission_idempotency_key": admission.admission_idempotency_key,
                        "admission_payload_hash": binding[0],
                    }}
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
                active_branch, _projection_epoch = self._storage._namespace_state(
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
                locator_checkpoint = checkpoint_id if repeated_checkpoint else parent_checkpoint_id
                locator_view = connection.execute(
                    "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
                    (locator_checkpoint,),
                ).fetchone() if locator_checkpoint is not None else None
                message_version = new_versions.get("messages")
                parent_message_version = connection.execute(
                    "SELECT channel_version FROM checkpoint_channels WHERE checkpoint_id=? AND channel_name='messages'",
                    (parent_checkpoint_id,),
                ).fetchone()
                # 版本递增属于当前父链；其它 branch 可以合法地产生相同版本号。
                new_message_version = message_version is not None and (
                    parent_message_version is None or parent_message_version[0] != message_version
                )
                batch = prepare_messages(
                    connection=connection, jsonl=jsonl, codec=self._storage._codec(),
                    read_item=self._storage._read_v2_item_at, thread_id=thread_id,
                    messages=current_messages,
                    last_message_sequence=strict_non_negative_int(meta[1], field="database_meta.last_message_sequence"),
                    next_item_sequence=strict_non_negative_int(meta[3], field="database_meta.last_item_sequence") + 1,
                    offset=jsonl_offset_before,
                    locator_sequences=self._storage._view_message_sequences_from_connection(connection, locator_view[0]) if locator_view else None,
                    allow_revisions=new_message_version and not repeated_checkpoint,
                )
                compaction_event = new_compaction_event(
                    connection, checkpoint, parent_checkpoint_id=parent_checkpoint_id,
                )
                compaction_selection = None
                compaction_summary_sequence = None
                if compaction_event is not None:
                    compaction_selection, compaction_summary_sequence = prepare_compaction(
                        connection=connection, codec=self._storage._codec(), batch=batch,
                        event=compaction_event, messages=current_messages, thread_id=thread_id,
                        checkpoint_id=checkpoint_id, timestamp=checkpoint_timestamp_value,
                        offset=jsonl_offset_before + sum(location[2] for row in batch.prepared for location in row[8]),
                    )
                prepared = batch.prepared
                first_sequence = batch.first_message_sequence
                last_sequence = batch.last_message_sequence
                visible_sequences = batch.visible_sequences
                if repeated_checkpoint:
                    if prepared:
                        raise ValueError("重复 checkpoint 的 canonical message group 发生变化")
                    old_view = connection.execute(
                        "SELECT context_view_id FROM checkpoint_channels WHERE checkpoint_id = ? AND channel_name='messages'",
                        (checkpoint_id,),
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
                transaction_id = uuid4().hex
                timestamp = _now()
                canonical_items = tuple(
                    item for row in prepared for item, _offset, _length in row[8]
                )
                if canonical_items:
                    # 唯一 storage writer 在本事务内完成 durability/catalog/offset；
                    # checkpoint/view 失败时，连接的 guard 回滚同一 JSONL 尾部。
                    commit_id, _offset = self._storage._append_v2_records_transaction(
                        connection, owner.thread_id, checkpoint_ns, canonical_items,
                        session_id=owner.session_id, commit_kind="item_convergence",
                        subject_id=checkpoint_id,
                        idempotency_key=f"checkpoint-view:{checkpoint_id}",
                        begin_transaction=False, transaction_id=transaction_id,
                        timestamp=timestamp, first_message_sequence=first_sequence,
                        last_message_sequence=last_sequence if first_sequence is not None else None,
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
                    visibility,
                    canonical_locations,
                    execution_id,
                ) in prepared:
                    message_value = json.loads(serialized_json)
                    content = (
                        message_value.get("data", {}).get("content")
                        if isinstance(message_value, Mapping)
                        else None
                    )
                    content_bytes = _json(content).encode("utf-8")
                    connection.execute(
                        "INSERT INTO messages(message_sequence, message_id, turn_id, execution_id, role, jsonl_offset, jsonl_length, content_length, content_hash, visibility, commit_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sequence,
                            message_id,
                            turn_id,
                            execution_id,
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
                    if turn_id is not None:
                        self._storage._upsert_turn(
                            connection,
                            turn_id,
                            sequence,
                            message_id,
                            role,
                            active_branch,
                            timestamp,
                        )
                    for canonical_item, _item_offset, _item_length in canonical_locations:
                        self._storage._bind_canonical_item_execution(
                            connection, item_id=canonical_item.item_id,
                            execution_id=execution_id,
                        )
                        # fork 的历史 Turn 与 canonical item 由冻结快照保留；
                        # checkpoint 只登记目标读取投影，不新建执行身份。
                        if (
                            canonical_item.turn_id is not None
                            and metadata.get("source") != "fork"
                        ):
                            self._context.executions._ensure_v2_turn_record_from_root(
                                connection,
                                session_id=owner.session_id,
                                thread_id=owner.thread_id,
                                item=canonical_item,
                                branch_id=active_branch,
                                timestamp=timestamp,
                            )
                    self._insert_message_projection(
                        connection, sequence, message_value, timestamp,
                        canonical_items=tuple(item for item, _offset, _length in canonical_locations),
                    )
                publish_message_write_references(
                    PendingMessageReferences(
                        self._storage, connection, session_id=owner.session_id,
                        thread_id=owner.thread_id, checkpoint_ns=checkpoint_ns,
                    ),
                    validate_row=_pending_write_row,
                    parent_checkpoint_id=parent_checkpoint_id,
                    item_ids={message_id: members[-1] for message_id, members in batch.message_item_ids.items()},
                )
                # Turn-less 输出与 typed display notice 在同一事务登记；fork 的
                # target-local item 也从已校验的 canonical group 恢复 membership。
                self._storage._append_context_view_items(
                    connection, checkpoint_ns=checkpoint_ns,
                    item_ids=batch.display_only_item_ids, branch_id=active_branch,
                    require_view=True, source_kind="display_only",
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
                view_kind = "compaction" if compaction_event is not None else "checkpoint"
                checkpoint_kind = (
                    "compaction" if view_kind == "compaction" else "normal"
                )
                if view_kind == "compaction":
                    # raw channel 仍保存被压缩的声明；协议闭合必须核新的
                    # prefix/summary/tail，不能用历史完整性掩盖尾部孤立结果。
                    selected_messages = dict(connection.execute(
                        "SELECT item.item_id,message.message_sequence FROM item_catalog item "
                        "JOIN messages message ON message.jsonl_offset=item.jsonl_offset "
                        "AND message.jsonl_length=item.jsonl_length",
                    ).fetchall())
                    selected_sequences = list(dict.fromkeys(
                        selected_messages[item_id] for item_id in compaction_selection if item_id in selected_messages
                    ))
                    validate_tool_protocol_closure(connection, selected_sequences, len(selected_sequences))
                # 完整 Turn 只补展示历史；Provider 选择继承冻结成员和本次新消息。
                parent_members = connection.execute(
                    "SELECT item_id, context_selected, context_ordinal FROM context_view_items "
                    "WHERE view_id=? AND visible=1", (parent_view_id,),
                ).fetchall() if parent_view_id is not None else []
                parent_item_ids = {row[0] for row in parent_members}
                inherited = [row[0] for row in sorted(
                    (row for row in parent_members if row[1] == 1),
                    key=lambda row: strict_non_negative_int(row[2], field="context_view_items.context_ordinal"),
                )]
                if messages_state != "view" and locator_view is not None:
                    previous_items = {row[0] for row in connection.execute(
                        "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1",
                        (locator_view[0],),
                    )}
                    removed_items = previous_items - set(batch.item_ids)
                    inherited = [item_id for item_id in inherited if item_id not in removed_items]
                context_selection = compaction_selection if compaction_selection is not None else (
                    *inherited, *(item_id for item_id in batch.item_ids if item_id not in parent_item_ids)
                )
                view_id = self._storage._create_view(
                    connection,
                    active_branch,
                    parent_view_id,
                    [*visible_sequences, compaction_summary_sequence] if compaction_summary_sequence is not None else visible_sequences,
                    timestamp,
                    view_kind=view_kind,
                    ordered_context_item_ids=context_selection,
                )
                # canonical history 保留两个 sink 的全部事实；LangGraph messages
                # channel 只冻结本次提供的消息和声明的父 channel，不能夹带尚未
                # 进入 checkpoint 的 stream result。两种 selection 均由同一
                # context view owner 提交，不从 Turn 的物理全量记录重建执行状态。
                parent_messages_view = None
                messages_sequences = visible_sequences
                if parent is not None and view_kind == "checkpoint" and messages_state == "view":
                    parent_channel = connection.execute(
                        "SELECT context_view_id FROM checkpoint_channels "
                        "WHERE checkpoint_id = ? AND channel_name = 'messages'",
                        (parent[0],),
                    ).fetchone()
                    if parent_channel is None:
                        raise RuntimeError("父 checkpoint 缺少 messages channel selection")
                    parent_messages_view = self._require_checkpoint_messages_view(
                        connection, checkpoint_id=strict_text(parent[0], field="checkpoints.checkpoint_id"),
                        history_view_id=strict_text(parent[6], field="checkpoints.view_id"),
                        branch_id=strict_text(parent[7], field="checkpoints.branch_id"),
                        messages_view_id=strict_text(parent_channel[0], field="checkpoint_channels.context_view_id"),
                    )
                    if parent[7] != active_branch:
                        if active_view_id is None:
                            raise RuntimeError("跨分支 checkpoint 缺少权威 active view selection")
                        # rewind 后只保留父 channel 中仍被当前分支选中的前缀；
                        # 本次写入可以是 delta，不能丢掉前缀或带回已移除消息。
                        selected = set(self._storage._view_message_sequences_from_connection(connection, active_view_id))
                        messages_sequences = [
                            sequence for sequence in self._storage._view_message_sequences_from_connection(connection, parent_messages_view)
                            if sequence in selected
                        ] + visible_sequences
                        parent_messages_view = None
                messages_view_id = self._storage._create_view(
                    connection, active_branch, parent_messages_view, messages_sequences,
                    timestamp, view_kind="checkpoint_messages",
                )
                (
                    versions_seen_type,
                    versions_seen_blob,
                    versions_seen_length,
                    versions_seen_hash,
                ) = self._storage._encode(checkpoint.get("versions_seen", {}))
                pending_type, pending_blob, pending_length, pending_hash = self._storage._encode(
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
                self._storage._insert_checkpoint_channels(
                    connection, checkpoint_id, checkpoint, messages_view_id, timestamp,
                    session_id=owner.session_id, thread_id=owner.thread_id,
                    checkpoint_ns=checkpoint_ns, parent_checkpoint_id=parent_checkpoint_id,
                    new_versions=new_versions, messages_state=messages_state,
                )
                self._capture_checkpoint_context_sources(
                    connection,
                    checkpoint_id=checkpoint_id,
                    checkpoint_ns=checkpoint_ns,
                    session_id=owner.session_id,
                    thread_id=owner.thread_id,
                    timestamp=timestamp,
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
                control_sequence = self._storage._insert_control(
                    connection,
                    "checkpoint_created",
                    "checkpoint",
                    checkpoint_id,
                    active_branch,
                    view_id,
                    checkpoint_id,
                    self._checkpoint_control_payload(
                        connection,
                        session_id=owner.session_id,
                        thread_id=owner.thread_id,
                        view_id=view_id,
                        message_count=len(current_messages),
                        message_sequences=batch.visible_sequences,
                        compaction_event=compaction_event,
                    ),
                    transaction_id,
                    timestamp,
                )
                view_cursor = connection.execute(
                    "UPDATE context_views SET control_sequence = ? WHERE view_id IN (?, ?)",
                    (control_sequence, view_id, messages_view_id),
                )
                if view_cursor.rowcount != 2:
                    raise RuntimeError(
                        "checkpoint view control_sequence 更新行数异常: "
                        f"view_id={view_id}, messages_view_id={messages_view_id}, rowcount={view_cursor.rowcount}"
                    )
                meta_cursor = connection.execute(
                    "UPDATE database_meta SET last_message_sequence = ?, last_control_sequence = ?, history_view_revision = history_view_revision + 1, updated_at = ? WHERE singleton_id = 1",
                    (
                        last_sequence,
                        control_sequence,
                        timestamp,
                    ),
                )
                if meta_cursor.rowcount != 1:
                    raise RuntimeError(
                        "checkpoint database_meta 更新行数异常: "
                        f"checkpoint_id={checkpoint_id}, rowcount={meta_cursor.rowcount}"
                    )
                if view_kind == "compaction":
                    revision = connection.execute(
                        "SELECT history_view_revision FROM database_meta WHERE singleton_id=1"
                    ).fetchone()
                    intent = RebuildContextEpochIntent(
                        owner=MutationIntentOwner(owner.session_id, owner.thread_id),
                        epoch_reason="compaction",
                        view_revision=strict_non_negative_int(
                            revision[0], field="database_meta.history_view_revision",
                        ),
                        control_revision=control_sequence,
                        checkpoint_ns=checkpoint_ns,
                        source_checkpoint_id=checkpoint_id,
                        target_view_id=view_id,
                    )
                    consumer = self.consume_context_epoch_rebuild_intent if consume_epoch_intent is None else consume_epoch_intent
                    consumer(intent, connection)
                    collect_unreferenced_content(connection)
                self._storage._commit_connection(connection)

    @staticmethod
    def _checkpoint_control_payload(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        view_id: str,
        message_count: int,
        message_sequences: list[int],
        compaction_event: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """同一提交保存 compaction cutoff 与两条历史 lane 的冻结资源引用。"""
        payload: dict[str, object] = {"message_count": message_count}
        event = compaction_event
        if not isinstance(event, Mapping):
            return payload
        activation_refs = read_activation_refs_for_view(
            connection, session_id=session_id, thread_id=thread_id, view_id=view_id,
        )
        if activation_refs:
            payload["resource_activation_refs"] = list(activation_refs)
        cutoff_index = event.get("cutoff_index")
        if isinstance(cutoff_index, int) and cutoff_index >= 0:
            payload["cutoff_index"] = cutoff_index
        cutoff_message_id = event.get("cutoff_message_id")
        if not isinstance(cutoff_message_id, str) or not cutoff_message_id:
            return payload
        if type(cutoff_index) is not int or not 0 < cutoff_index <= len(message_sequences):
            raise ValueError("compaction cutoff identity 缺少已校验的消息位置")
        payload["cutoff_message_id"] = cutoff_message_id
        payload["cutoff_message_sequence"] = message_sequences[cutoff_index - 1]
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
