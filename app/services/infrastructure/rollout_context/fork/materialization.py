"""fork 目标协调 owner；历史业务事实在冻结快照中保持不变。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from langgraph.checkpoint.base import empty_checkpoint

from app.core.sqlite_state import utc_now_text as _now
from app.services.infrastructure.rollout_context.fork.validation import (
    one_of_text,
    optional_text,
    required_sha256,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_storage_commit,
)


class ForkMaterializationMixin:
    """只创建目标当前 checkpoint、copy-origin 和物化状态。"""

    @contextmanager
    def _checkpoint_transaction(
        self, connection: sqlite3.Connection | None, *, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str,
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("fork staging checkpoint 必须属于安装事务")
            yield connection
            return
        with (
            self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
            self._storage._connect(target_thread_id, checkpoint_ns, session_id=target_session_id) as owned,
        ):
            owned.execute("BEGIN IMMEDIATE")
            yield owned
            self._storage._commit_connection(owned)

    def materialize_fork_checkpoint(
        self, *, materialization_id: str, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str, view_id: str,
        staging_connection: sqlite3.Connection | None = None,
    ) -> str | None:
        """新 head 只引用目标已选上下文，不继承源任务、准入或 pending state。"""
        staging = staging_connection is not None
        with self._checkpoint_transaction(
            staging_connection, target_session_id=target_session_id,
            target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns,
        ) as connection:
            journal = connection.execute(
                "SELECT status, source_checkpoint_id, fork_mode FROM fork_materializations WHERE materialization_id = ? "
                "AND target_session_id = ? AND target_thread_id = ? AND checkpoint_ns = ?",
                (materialization_id, target_session_id, target_thread_id, checkpoint_ns),
            ).fetchone()
            if journal is None or journal[0] != "prepared":
                raise RuntimeError("fork checkpoint 缺少精确目标 prepared journal")
            if current_scope(connection) != (target_session_id, target_thread_id):
                raise RuntimeError("fork checkpoint 当前存储 owner 不一致")
            head = connection.execute(
                "SELECT b.branch_id, b.head_checkpoint_id, v.head_turn_id, "
                "v.head_message_sequence, v.logical_turn_count "
                "FROM checkpoint_namespace_state n JOIN branches b ON b.branch_id = n.active_branch_id "
                "JOIN context_views v ON v.view_id = b.head_view_id "
                "WHERE n.checkpoint_ns = ? AND v.view_id = ?",
                (checkpoint_ns, view_id),
            ).fetchone()
            if head is None:
                raise RuntimeError("fork checkpoint 必须引用已安装的目标 active view")
            if head[1] is not None:
                existing = connection.execute(
                    "SELECT metadata_json FROM checkpoints WHERE checkpoint_id = ?",
                    (head[1],),
                ).fetchone()
                if existing is None or json.loads(existing[0]).get("fork_materialization_id") != materialization_id:
                    raise RuntimeError("fork 当前 branch 已有其它操作的执行 head")
                return head[1]
            commit = connection.execute(
                "SELECT last_commit_id FROM database_meta WHERE singleton_id = 1",
            ).fetchone()[0]
            if staging and journal[2] != "full_rollout_copy":
                commit = connection.execute(
                    "SELECT MAX(commit_id) FROM storage_commits commit_record WHERE commit_mode<>'item_bearing' "
                    "OR EXISTS(SELECT 1 FROM item_catalog item JOIN fork_selected_items selected USING(item_id) "
                    "WHERE item.commit_id=commit_record.commit_id)",
                ).fetchone()[0]
            if commit is None:
                if connection.execute("SELECT 1 FROM item_catalog LIMIT 1").fetchone() is not None:
                    raise RuntimeError("fork 正文缺少已提交 storage commit")
                return None
            load_committed_storage_commit(connection, commit_id=commit)
            seed_checkpoint_id = journal[1]
            seed = connection.execute(
                "SELECT checkpoint_version, checkpoint_json, versions_seen_type, versions_seen_blob, "
                "versions_seen_length, versions_seen_hash FROM checkpoints "
                "WHERE checkpoint_id=? AND checkpoint_ns=? AND status='active'",
                (seed_checkpoint_id, checkpoint_ns),
            ).fetchone() if seed_checkpoint_id is not None else None
            if seed_checkpoint_id is not None and seed is None:
                raise RuntimeError("fork 冻结上下文 checkpoint 未保留在目标历史中")
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"messages": []}
            checkpoint["channel_versions"] = {"messages": "1"}
            checkpoint["updated_channels"] = ["messages"]
            checkpoint_id = checkpoint["id"]
            # 标记目标侧 fork checkpoint/view/control 元数据的 UTC 记账时刻，不是源 item.created_at。
            copy_recorded_at = _now()
            message_view = uuid4().hex
            metadata = {
                "source": "fork", "step": -1, "parents": {},
                "fork_materialization_id": materialization_id,
            }
            # 原 checkpoint/event 保留为历史；新执行只读取目标 selection，
            # 不把源 raw 坐标下的压缩事件套到重排后的消息上。
            versions = self._storage._encode({}) if seed is None else tuple(seed[2:])
            pending = self._storage._encode([])
            core = {key: value for key, value in checkpoint.items() if key not in {
                "channel_values", "channel_versions", "updated_channels",
                "versions_seen", "pending_sends",
            }}
            # 新 head 的 ts 来自本次 empty_checkpoint()；seed 只复用状态并以该 ts 覆盖来源 checkpoint 时间。
            # 该 ts 与 copy_recorded_at 分别用于 checkpoint 和目标元数据记账；来源 item/message 时间仍保留，不合并两种时钟。
            if seed is not None:
                checkpoint["v"] = seed[0]
                core = {**json.loads(seed[1]), "id": checkpoint_id, "ts": checkpoint["ts"]}
                metadata["fork_context_checkpoint_id"] = seed_checkpoint_id
            connection.execute(
                "INSERT INTO context_views(view_id, branch_id, parent_view_id, view_kind, "
                "head_turn_id, head_message_sequence, logical_turn_count, control_sequence, created_at) "
                "VALUES (?, ?, NULL, 'checkpoint_messages', ?, ?, ?, NULL, ?)",
                (message_view, head[0], *head[2:], copy_recorded_at),
            )
            connection.execute(
                "INSERT INTO context_view_items(view_id,item_id,logical_item_ordinal,visible,source_kind,context_selected,context_ordinal) "
                "SELECT ?,item_id,context_ordinal,1,source_kind,1,context_ordinal "
                "FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1",
                (message_view, view_id),
            )
            connection.execute(
                "INSERT INTO context_view_turns(view_id,turn_id,logical_turn_ordinal,user_message_sequence,"
                "final_message_sequence,root_input_item_id,fork_lineage_json) "
                "SELECT ?,turn_id,logical_turn_ordinal,user_message_sequence,final_message_sequence,root_input_item_id,fork_lineage_json "
                "FROM context_view_turns AS turn WHERE view_id=? AND EXISTS ("
                "SELECT 1 FROM context_view_items AS member JOIN item_catalog AS item ON item.item_id=member.item_id "
                "WHERE member.view_id=? AND item.turn_id=turn.turn_id)", (message_view, view_id, message_view),
            )
            connection.execute(
                "UPDATE context_views SET logical_turn_count=(SELECT COUNT(*) FROM context_view_turns WHERE view_id=?), "
                "head_turn_id=(SELECT item.turn_id FROM context_view_items member JOIN item_catalog item ON item.item_id=member.item_id "
                "WHERE member.view_id=? ORDER BY member.logical_item_ordinal DESC LIMIT 1), "
                "head_message_sequence=(SELECT MAX(message.message_sequence) FROM context_view_items member "
                "JOIN item_catalog item ON item.item_id=member.item_id JOIN messages message ON message.jsonl_offset=item.jsonl_offset "
                "AND message.jsonl_length=item.jsonl_length WHERE member.view_id=?) "
                "WHERE view_id=?", (message_view, message_view, message_view, message_view),
            )
            count = connection.execute(
                "SELECT COUNT(DISTINCT message.message_sequence) FROM messages message "
                "JOIN item_catalog anchor ON anchor.jsonl_offset=message.jsonl_offset "
                "AND anchor.jsonl_length=message.jsonl_length JOIN context_view_items member "
                "ON member.item_id=anchor.item_id WHERE member.view_id=? AND member.visible=1",
                (message_view,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO checkpoints(checkpoint_id,checkpoint_ns,commit_id,message_sequence,"
                "message_count,parent_checkpoint_id,view_id,branch_id,checkpoint_version,"
                "checkpoint_timestamp,checkpoint_kind,status,checkpoint_json,metadata_json,"
                "envelope_serializer_name,versions_seen_type,versions_seen_blob,versions_seen_length,"
                "versions_seen_hash,pending_sends_type,pending_sends_blob,pending_sends_length,pending_sends_hash) "
                "VALUES (?,?,?,?,?,NULL,?,?,?,?,'normal','active',?,?,'json',?,?,?,?,?,?,?,?)",
                (checkpoint_id, checkpoint_ns, commit, head[3] or 0, count, view_id,
                 head[0], checkpoint["v"], checkpoint["ts"], _json(core), _json(metadata),
                 *versions, *pending),
            )
            if seed_checkpoint_id is None:
                self._storage._insert_checkpoint_channels(
                    connection, checkpoint_id, checkpoint, message_view, copy_recorded_at,
                )
            else:
                connection.execute(
                    "INSERT INTO checkpoint_channels(checkpoint_id,channel_name,storage_kind,value_state,"
                    "channel_version,serializer_name,value_blob,value_length,value_hash,context_view_id,"
                    "updated_index,created_at) SELECT ?,channel_name,storage_kind,value_state,channel_version,"
                    "serializer_name,value_blob,value_length,value_hash,"
                    "CASE WHEN channel_name='messages' THEN ? ELSE context_view_id END,updated_index,? "
                    "FROM checkpoint_channels WHERE checkpoint_id=? "
                    "AND channel_name NOT IN ('_summarization_event','_force_cache_compaction','_compaction_continuation')",
                    (checkpoint_id, message_view, copy_recorded_at, seed_checkpoint_id),
                )
            self._context.checkpoints._capture_checkpoint_context_sources(
                connection, checkpoint_id=checkpoint_id, checkpoint_ns=checkpoint_ns,
                session_id=target_session_id, thread_id=target_thread_id, timestamp=copy_recorded_at,
                source_view_id=view_id,
            )
            control = self._storage._insert_control(
                connection, "checkpoint_created", "checkpoint", checkpoint_id,
                head[0], view_id, checkpoint_id, {"message_count": count},
                f"fork-checkpoint:{materialization_id}", copy_recorded_at,
            )
            connection.execute(
                "UPDATE context_views SET control_sequence = ? WHERE view_id IN (?, ?)",
                (control, view_id, message_view),
            )
            connection.execute(
                "UPDATE branches SET head_checkpoint_id = ?, updated_at = ? WHERE branch_id = ?",
                (checkpoint_id, copy_recorded_at, head[0]),
            )
            connection.execute(
                "UPDATE database_meta SET last_control_sequence = ?, history_view_revision = "
                "history_view_revision + 1, updated_at = ? WHERE singleton_id = 1",
                (control, copy_recorded_at),
            )
            return checkpoint_id

    def commit_fork_materialization(
        self, materialization_id: str, *, target_session_id: str,
        source_session_id: str, source_checkpoint_id: str | None,
        source_view_id: str | None, fork_mode: str, relationship: str,
        checkpoint_ns: str, source_thread_id: str, target_thread_id: str,
        source_snapshot_sha256: str, defer_completion: bool = False,
    ) -> str:
        """核对本次冻结 intent，只提交新 copy-origin 与目标协调状态。

        ``target_committed_at`` 是目标内容事务的 UTC 记账时刻；``fork_origins.created_at`` 与其共用该值，
        表示目标侧 origin 行创建时刻，不是来源 ``item.created_at``，也不表示最终 journal/retention 完成时间。
        """
        materialization_id = required_text(materialization_id, field="materialization_id")
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        if (source_session_id, source_thread_id) == (target_session_id, target_thread_id):
            raise ValueError("fork 不能覆盖来源 Thread")
        source_checkpoint_id = optional_text(source_checkpoint_id, field="source_checkpoint_id")
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_mode = one_of_text(fork_mode, {"context_fork", "view_prefix_fork", "full_rollout_copy"},
                                field="fork_mode")
        relationship = one_of_text(relationship, {"detached", "pinned"}, field="relationship")
        source_snapshot_sha256 = required_sha256(source_snapshot_sha256, field="source_snapshot_sha256")
        expected = (
            source_session_id, source_thread_id, source_checkpoint_id, source_view_id,
            fork_mode, relationship, source_snapshot_sha256,
        )
        with (
            self._storage._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
            self._storage._connect(target_thread_id, checkpoint_ns, session_id=target_session_id) as connection,
        ):
            if current_scope(connection) != (target_session_id, target_thread_id):
                raise RuntimeError("fork commit 当前存储 owner 不一致")
            journal = connection.execute(
                "SELECT fork_id,status,source_session_id,source_thread_id,source_checkpoint_id,"
                "source_view_id,fork_mode,relationship,source_snapshot_sha256 "
                "FROM fork_materializations WHERE materialization_id = ? "
                "AND target_session_id = ? AND target_thread_id = ? AND checkpoint_ns = ?",
                (materialization_id, target_session_id, target_thread_id, checkpoint_ns),
            ).fetchone()
            if journal is None or journal[2:] != expected:
                raise RuntimeError("fork commit 与精确目标冻结 journal 不一致")
            self._assert_fork_debug_snapshot_published(connection, materialization_id)
            fork_id = required_text(journal[0], field="fork_id")
            if journal[1] == "prepared":
                target_commit_recorded_at = _now()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO fork_origins(fork_id,child_session_id,source_session_id,"
                    "source_checkpoint_id,source_view_id,fork_mode,relationship,copied_message_count,created_at) "
                    "VALUES (?,?,?,?,?,?,?,(SELECT COUNT(*) FROM messages),?)",
                    (fork_id, target_session_id, source_session_id, source_checkpoint_id,
                     source_view_id, fork_mode, relationship, target_commit_recorded_at),
                )
                connection.execute(
                    "UPDATE fork_materializations SET status = 'target_committed', "
                    "copied_message_count = (SELECT COUNT(*) FROM messages), target_committed_at = ?, "
                    "error_message = NULL WHERE materialization_id = ? AND target_session_id = ? "
                    "AND target_thread_id = ? AND checkpoint_ns = ? AND status = 'prepared'",
                    (target_commit_recorded_at, materialization_id, target_session_id, target_thread_id, checkpoint_ns),
                )
                connection.commit()
            elif journal[1] not in {"target_committed", "committed"}:
                raise RuntimeError(f"fork journal 状态不可提交: {journal[1]}")
        if defer_completion:
            return fork_id
        return self.complete_fork_materialization(
            materialization_id=materialization_id, target_session_id=target_session_id,
            target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns,
        )
