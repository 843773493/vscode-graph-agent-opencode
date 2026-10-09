"""v2 checkpoint/context query owner。

本模块只提供已提交 SQLite view/catalog 的查询，不扫描 v1、不生成 LangChain
message，也不拥有 checkpoint 写入事实。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.core.execution_admission_context import current_execution_admission
from app.core.hashing import sha256_hex as _hash_bytes
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutCheckpointIndex,
        RolloutReadSnapshot,
    )


class RolloutCheckpointQueriesMixin:
    """checkpoint header、active view 和已提交 context query owner。"""

    @staticmethod
    def _require_checkpoint_messages_view(
        connection: sqlite3.Connection, *, checkpoint_id: str,
        history_view_id: str, branch_id: str, messages_view_id: str,
    ) -> str:
        """读取与继承都必须证明 messages selection 属于精确冻结 checkpoint。"""
        owner = connection.execute(
            "SELECT messages.branch_id, messages.view_kind, messages.control_sequence, "
            "history.branch_id, history.control_sequence, event.checkpoint_id, "
            "event.view_id, event.branch_id, event.control_kind "
            "FROM context_views AS messages "
            "JOIN context_views AS history ON history.view_id = ? "
            "JOIN control_events AS event ON event.control_sequence = history.control_sequence "
            "WHERE messages.view_id = ?",
            (history_view_id, messages_view_id),
        ).fetchone()
        if (
            owner is None
            or owner[0] != branch_id
            or owner[1] != "checkpoint_messages"
            or owner[2] != owner[4]
            or owner[3] != branch_id
            or owner[5:] != (checkpoint_id, history_view_id, branch_id, "checkpoint_created")
        ):
            raise RuntimeError(f"checkpoint messages selection 与冻结 checkpoint 不一致: checkpoint_id={checkpoint_id}, messages_view_id={messages_view_id}, owner={owner!r}")
        strict_non_negative_int(owner[2], field="checkpoint messages context_views.control_sequence")
        return messages_view_id

    def latest_checkpoint(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
        *,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> RolloutCheckpointIndex | None:
        session_id = strict_text(session_id, field="latest_checkpoint.session_id")
        thread_id = strict_text(thread_id, field="latest_checkpoint.thread_id")
        strict_text(
            checkpoint_ns, field="latest_checkpoint.checkpoint_ns", allow_empty=True
        )
        checkpoint_id = strict_optional_text(
            checkpoint_id, field="latest_checkpoint.checkpoint_id"
        )
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("latest_checkpoint snapshot 与目标 rollout 不一致")

        def resolve_target_checkpoint_id(
            connection: sqlite3.Connection,
            requested_id: str | None,
        ) -> str | None:
            if requested_id is None:
                return None
            direct = connection.execute(
                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                (requested_id, checkpoint_ns),
            ).fetchone()
            if direct is not None:
                return requested_id
            # full_rollout_copy 的物理 checkpoint identity 是 target-local；
            # 这里只提供 source checkpoint 坐标的只读 alias，绝不把 source
            # ID 写回 target 表或作为新的幂等 identity。
            alias = connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE target_session_id = ? AND entity_type = 'checkpoint' AND source_local_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id, requested_id),
            ).fetchone()
            return (
                strict_text(alias[0], field="fork_identity_mappings.target_local_id")
                if alias is not None
                else requested_id
            )

        try:
            with self._storage.read_connection(
                session_id, thread_id, checkpoint_ns, snapshot=snapshot,
            ) as connection:
                row = self._checkpoint_row(
                    connection, checkpoint_ns, resolve_target_checkpoint_id(connection, checkpoint_id),
                )
                return self._storage._checkpoint_index(row) if row else None
        except RolloutNotMaterializedError:
            return None

    def active_view_id(
        self,
        snapshot: RolloutReadSnapshot,
    ) -> str | None:
        connection = self._storage._snapshot_connection(snapshot)
        self._storage._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ?",
            (snapshot.manifest.active_branch_id,),
        ).fetchone()
        return (
            strict_optional_text(row[0], field="branches.head_view_id")
            if row is not None
            else None
        )

    def committed_context_items(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        include_pending_notices: bool = True,
    ) -> tuple[CanonicalItemRecord, ...]:
        """返回当前已提交 view 的 item；不从 JSONL 物理顺序推导上下文。

        active view 是历史 selection 的权威来源。pending runtime notice 不属于
        view membership，但可以作为下一次 assembly 的显式 pending reference，
        因此在调用方选择 ``include_pending_notices`` 时按 catalog 顺序追加。
        """
        view_id = self.active_view_id(snapshot)
        if view_id is None:
            # v2 初始化会先建立 active view，acceptance/stream sink 只会补充
            # 该 view 的 membership。没有 active view 就无法知道当前 branch 的
            # selection；回退到 item_catalog 的物理顺序会把其它 branch/ambient
            # item 冒充当前上下文，属于第二套事实源，必须 fail-closed。
            raise RuntimeError(
                "rollout active view 缺失，拒绝从 item_catalog 物理顺序推导 context"
            )
        items = self.read_items_for_view(snapshot, view_id)
        if not include_pending_notices:
            return tuple(items)
        connection = self._storage._snapshot_connection(snapshot)
        pending_rows = connection.execute(
            "SELECT item_id FROM item_catalog WHERE turn_scope = 'pending_next_turn' AND turn_id IS NULL ORDER BY item_sequence"
        ).fetchall()
        known = {item.item_id for item in items}
        pending_ids = []
        for row in pending_rows:
            item_id = strict_text(row[0], field="item_catalog.item_id")
            if item_id not in known:
                pending_ids.append(item_id)
        if pending_ids:
            items.extend(
                self._storage.read_items(
                    snapshot.session_id,
                    snapshot.thread_id,
                    checkpoint_ns=snapshot.checkpoint_ns,
                    item_ids=pending_ids,
                    snapshot=snapshot,
                )
            )
        return tuple(items)

    def committed_context_refs(
        self,
        snapshot: RolloutReadSnapshot,
        *,
        include_pending_notices: bool = True,
    ) -> tuple[ContextRef, ...]:
        """从已提交 item 生成 Saver-owned canonical refs。"""
        items = self.committed_context_items(
            snapshot,
            include_pending_notices=include_pending_notices,
        )
        by_message_id = {
            message_id: item
            for item in items
            if isinstance(
                message_id := (
                    item.projection_identity.projection_message_id
                    if item.projection_identity is not None
                    else None
                ),
                str,
            )
            and message_id
        }
        superseded_message_ids: set[str] = set()
        for item in items:
            superseded = next(
                (
                    relation.target_id
                    for relation in item.provenance_relations
                    if relation.relation == "replaces"
                    and relation.target_kind == "projection_message"
                ),
                None,
            )
            if superseded is None:
                continue
            if not isinstance(superseded, str) or not superseded:
                raise RuntimeError(
                    f"canonical item typed replaces relation 非法: {item.item_id}"
                )
            previous = by_message_id.get(superseded)
            if previous is None:
                continue
            if previous.turn_id != item.turn_id:
                raise RuntimeError(
                    "canonical assistant supersedes 跨 Turn: "
                    f"{previous.item_id}->{item.item_id}"
                )
            if previous.item_sequence >= item.item_sequence:
                raise RuntimeError(
                    "canonical assistant supersedes 顺序非法: "
                    f"{previous.item_id}->{item.item_id}"
                )
            if superseded in superseded_message_ids:
                raise RuntimeError(f"canonical assistant 被多个消息替代: {superseded}")
            superseded_message_ids.add(superseded)
        session_id = snapshot.session_id
        thread_id = snapshot.thread_id
        return tuple(
            ContextRef.canonical_item(
                item,
                session_id=session_id,
                thread_id=thread_id,
            )
            for item in items
            if (
                item.projection_identity.projection_message_id
                if item.projection_identity is not None
                else None
            )
            not in superseded_message_ids
        )

    def list_checkpoints(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str | None,
        *,
        before_checkpoint_id: str | None,
        limit: int | None,
    ) -> list[RolloutCheckpointIndex]:
        session_id = strict_text(session_id, field="list_checkpoints.session_id")
        thread_id = strict_text(thread_id, field="list_checkpoints.thread_id")
        if checkpoint_ns is not None and not isinstance(checkpoint_ns, str):
            raise TypeError("list_checkpoints.checkpoint_ns 必须是字符串或 null")
        checkpoint_ns = checkpoint_ns or ""
        before_checkpoint_id = strict_optional_text(
            before_checkpoint_id, field="list_checkpoints.before_checkpoint_id"
        )
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("checkpoint limit 必须是正整数或 null")
        try:
            with self._storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                self._storage._require_v2_runtime(connection)
                before = (
                    connection.execute(
                        "SELECT cv.control_sequence FROM checkpoints AS c "
                        "LEFT JOIN context_views AS cv ON cv.view_id = c.view_id "
                        "WHERE c.checkpoint_id = ? AND c.checkpoint_ns = ?",
                        (before_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if before_checkpoint_id
                    else None
                )
                query = (
                    "SELECT c.checkpoint_id, c.checkpoint_ns, c.commit_id, c.message_sequence, "
                    "c.message_count, c.parent_checkpoint_id, c.view_id, c.branch_id, "
                    "c.checkpoint_version, c.checkpoint_timestamp, c.checkpoint_json, "
                    "c.metadata_json, c.versions_seen_type, c.versions_seen_blob, "
                    "c.pending_sends_type, c.pending_sends_blob, cv.control_sequence "
                    "FROM checkpoints AS c LEFT JOIN context_views AS cv ON cv.view_id = c.view_id "
                    "WHERE c.checkpoint_ns = ? AND c.status = 'active' "
                    "AND (? IS NULL OR cv.control_sequence < ?) ORDER BY cv.control_sequence DESC"
                    + (" LIMIT ?" if limit is not None else "")
                )
                before_control_sequence = (
                    strict_non_negative_int(before[0], field="checkpoint context_views.control_sequence")
                    if before is not None
                    else None
                )
                params: tuple[object, ...] = (
                    checkpoint_ns,
                    before_control_sequence,
                    before_control_sequence,
                ) + ((limit,) if limit is not None else ())
                rows = connection.execute(query, params).fetchall()
        except RolloutNotMaterializedError:
            return []
        # 仅 channel/metadata 变化的 checkpoint 可以共用 item commit，
        # 已有 checkpoint_created 的控制序才是其唯一创建顺序。
        for row in rows:
            strict_non_negative_int(
                row[-1], field="checkpoint context_views.control_sequence"
            )
        return [self._storage._checkpoint_index(row[:-1]) for row in rows]

    def checkpoint_values(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
        include_accepted_inputs: bool = False,
    ) -> dict[str, object]:
        session_id = strict_text(session_id, field="checkpoint_values.session_id")
        strict_text(thread_id, field="checkpoint_values.thread_id")
        strict_text(
            checkpoint_ns, field="checkpoint_values.checkpoint_ns", allow_empty=True
        )
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("checkpoint_values snapshot 与目标 rollout 不一致")
        context_view_id_override = strict_optional_text(
            context_view_id_override, field="checkpoint_values.context_view_id_override"
        )

        def read(connection: sqlite3.Connection) -> dict[str, object]:
            self._storage._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT channel_name, storage_kind, value_state, serializer_name, value_blob, value_hash, context_view_id FROM checkpoint_channels WHERE checkpoint_id = ?",
                (index.checkpoint_id,),
            ).fetchall()
            values: dict[str, object] = {}
            has_messages = False
            for raw_row in rows:
                if len(raw_row) != 7:
                    raise RuntimeError("checkpoint_channels 行字段数非法")
                channel, storage_kind, state, serializer, blob, digest, view_id = (
                    raw_row
                )
                channel = strict_text(channel, field="checkpoint_channels.channel_name")
                storage_kind = strict_text(
                    storage_kind, field="checkpoint_channels.storage_kind"
                )
                state = strict_text(state, field="checkpoint_channels.value_state")
                serializer = strict_optional_text(
                    serializer, field="checkpoint_channels.serializer_name"
                )
                digest = strict_optional_text(
                    digest, field="checkpoint_channels.value_hash"
                )
                view_id = strict_optional_text(
                    view_id, field="checkpoint_channels.context_view_id"
                )
                if channel == "messages":
                    context_view_id = context_view_id_override or view_id
                    if (
                        storage_kind != "rollout_view"
                        or state != "view"
                        or view_id is None
                    ):
                        raise RuntimeError(
                            f"checkpoint messages view 指针非法: {index.checkpoint_id}"
                        )
                    self._require_checkpoint_messages_view(
                        connection, checkpoint_id=index.checkpoint_id,
                        history_view_id=index.view_id, branch_id=index.branch_id,
                        messages_view_id=view_id,
                    )
                    selection_view_id = None
                    if include_accepted_inputs:
                        selection = connection.execute(
                            "SELECT branch.branch_id, branch.head_view_id "
                            "FROM checkpoint_namespace_state AS namespace "
                            "JOIN branches AS branch ON branch.branch_id = namespace.active_branch_id "
                            "WHERE namespace.checkpoint_ns = ?",
                            (checkpoint_ns,),
                        ).fetchone()
                        if selection is None:
                            raise RuntimeError("最新 checkpoint 缺少权威 active branch selection")
                        if selection[0] != index.branch_id:
                            # rewind 等显式分支选择可继续引用旧 checkpoint；只截断
                            # 其冻结 channel，不把 active view 的 stream 消息重新注入。
                            selection_view_id = strict_text(selection[1], field="branches.head_view_id")
                    accepted_sequences: list[int] = []
                    admission = current_execution_admission(langgraph_thread_id=thread_id) if include_accepted_inputs else None
                    if include_accepted_inputs and (admission is None or admission.execution_origin == "user_turn"):
                        # 仅 acceptance/Turn owner 证明的根输入可进入最新执行状态；
                        # active history 中的 live assistant/tool item 不属于 checkpoint channel。
                        cutoff_ordinal = None
                        if admission is not None:
                            cutoff = connection.execute(
                                "SELECT turn_ordinal FROM turn_records WHERE turn_id = ? AND session_id = ? AND thread_id = ?",
                                (admission.turn_id, session_id, thread_id),
                            ).fetchone()
                            if cutoff is None:
                                raise RuntimeError("checkpoint 执行 cutoff 缺少同 owner 的真实 Turn")
                            cutoff_ordinal = strict_non_negative_int(cutoff[0], field="execution cutoff turn_records.turn_ordinal")
                        roots = connection.execute(
                            "SELECT message.message_sequence, turn.session_id, turn.thread_id, "
                            "acceptance.turn_id, item.turn_id, acceptance.session_id, acceptance.thread_id, turn.turn_ordinal "
                            "FROM checkpoint_namespace_state AS namespace "
                            "JOIN branches AS branch ON branch.branch_id = namespace.active_branch_id "
                            "JOIN context_view_items AS member ON member.view_id = branch.head_view_id AND member.visible = 1 "
                            "JOIN item_catalog AS item ON item.item_id = member.item_id "
                            "LEFT JOIN turn_records AS turn ON turn.root_input_item_id = item.item_id "
                            "LEFT JOIN turn_acceptances AS acceptance ON acceptance.accepted_ingress_id = turn.accepted_ingress_id "
                            "LEFT JOIN messages AS message ON message.jsonl_offset = item.jsonl_offset AND message.jsonl_length = item.jsonl_length "
                            "WHERE namespace.checkpoint_ns = ? AND item.semantic_kind = 'user_input' AND item.turn_scope = 'turn_root' "
                            "ORDER BY member.logical_item_ordinal",
                            (checkpoint_ns,),
                        ).fetchall()
                        for root in roots:
                            if root[4] is None or root[1:7] != (session_id, thread_id, root[4], root[4], session_id, thread_id):
                                raise RuntimeError(f"最新 checkpoint 根输入缺少同 owner 的权威 acceptance: {root!r}")
                            ordinal = strict_non_negative_int(root[7], field="accepted input turn_records.turn_ordinal")
                            if cutoff_ordinal is not None and ordinal > cutoff_ordinal:
                                continue
                            accepted_sequences.append(strict_non_negative_int(root[0], field="accepted input messages.message_sequence"))
                    values[channel] = self._messages_for_view(
                        session_id,
                        thread_id,
                        checkpoint_ns,
                        context_view_id,
                        connection=connection,
                        selection_view_id=selection_view_id,
                        additional_message_sequences=accepted_sequences,
                    )
                    has_messages = True
                elif state == "present":
                    if (
                        not isinstance(blob, bytes)
                        or serializer is None
                        or digest is None
                        or _hash_bytes(blob) != digest
                    ):
                        raise RuntimeError(
                            f"checkpoint channel BLOB 校验失败: {index.checkpoint_id}/{channel}"
                        )
                    values[channel] = self._storage.decode_value((serializer, blob))
                elif state != "absent":
                    raise RuntimeError(
                        f"checkpoint channel value_state 未知: {index.checkpoint_id}/{channel}"
                    )
            if not has_messages:
                raise RuntimeError(
                    f"checkpoint 缺少 messages channel: {index.checkpoint_id}"
                )
            return values

        with self._storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            return read(connection)

    def load_checkpoint(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        index: RolloutCheckpointIndex,
        *,
        snapshot: RolloutReadSnapshot | None = None,
        context_view_id_override: str | None = None,
        include_accepted_inputs: bool = False,
    ) -> Checkpoint:
        session_id = strict_text(session_id, field="load_checkpoint.session_id")
        strict_text(thread_id, field="load_checkpoint.thread_id")
        if snapshot is not None and (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.checkpoint_ns != checkpoint_ns
        ):
            raise ValueError("load_checkpoint snapshot 与目标 rollout 不一致")
        if snapshot is None:
            with self._storage.open_read_snapshot(session_id, thread_id, checkpoint_ns) as owned:
                return self.load_checkpoint(
                    session_id, thread_id, checkpoint_ns, index, snapshot=owned,
                    context_view_id_override=context_view_id_override,
                    include_accepted_inputs=include_accepted_inputs,
                )
        checkpoint = json.loads(index.checkpoint_json)
        if not isinstance(checkpoint, dict):
            raise TypeError("checkpoint core JSON 必须是对象")
        checkpoint["channel_values"] = self.checkpoint_values(
            session_id,
            thread_id,
            checkpoint_ns,
            index,
            snapshot=snapshot,
            context_view_id_override=context_view_id_override,
            include_accepted_inputs=include_accepted_inputs,
        )
        checkpoint["channel_versions"] = {}
        checkpoint["updated_channels"] = []
        connection = self._storage._snapshot_connection(snapshot)
        rows = connection.execute(
            "SELECT channel_name, channel_version, updated_index FROM checkpoint_channels WHERE checkpoint_id = ? ORDER BY updated_index",
            (index.checkpoint_id,),
        ).fetchall()
        for raw_row in rows:
            if len(raw_row) != 3:
                raise RuntimeError("checkpoint_channels version 行字段数非法")
            name, version, updated_index = raw_row
            name = strict_text(name, field="checkpoint_channels.channel_name")
            version = strict_optional_text(
                version, field="checkpoint_channels.channel_version"
            )
            updated_index = strict_optional_non_negative_int(
                updated_index, field="checkpoint_channels.updated_index"
            )
            if version is not None:
                checkpoint["channel_versions"][name] = version
            if updated_index is not None:
                checkpoint["updated_channels"].append(name)
        checkpoint["versions_seen"] = self._storage.decode_value(
            (index.versions_seen_type, index.versions_seen_blob)
        )
        checkpoint["pending_sends"] = self._storage.decode_value(
            (index.pending_sends_type, index.pending_sends_blob)
        )
        return checkpoint

    def metadata(self, index: RolloutCheckpointIndex) -> CheckpointMetadata:
        value = json.loads(index.metadata_json)
        if not isinstance(value, dict):
            raise TypeError("checkpoint metadata JSON 必须是对象")
        return value
