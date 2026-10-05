"""跨 session fork 的 canonical item 复制与 identity remap owner。

本模块只消费 v2 item/catalog，并在 target rollout 重新分配本地 identity；不提供
v1 fork fallback。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from app.core.sqlite_state import utc_now_text
from app.domain.itemized.enums import CommitKind, SemanticKind, TurnScope
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.fork.identity import (
    target_local_acceptance_identity,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


class ForkItemCopyMixin:
    def copy_v2_items_for_fork(
        self,
        *,
        source_session_id: str,
        source_thread_id: str,
        target_session_id: str,
        target_thread_id: str,
        checkpoint_ns: str = "",
        fork_id: str,
        materialization_id: str,
        source_message_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """由 prepared journal 从冻结 source 物化 canonical 与真实执行 lineage。

        不经过普通 admission/append_items。canonical、Turn、execution 和 mapping
        在同一目标事务中准备，checkpoint 随后只引用已物化 item。
        """
        source_thread_id = strict_text(
            source_thread_id,
            field="fork.source_thread_id",
        )
        target_thread_id = strict_text(
            target_thread_id,
            field="fork.target_thread_id",
        )
        if source_thread_id == target_thread_id:
            raise ValueError("fork target_thread_id 不能与 source_thread_id 相同")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        fork_id = strict_text(fork_id, field="fork.fork_id")
        materialization_id = strict_text(materialization_id, field="fork.materialization_id")
        if self._fork_source_snapshot_for_session(source_session_id) is None:
            raise RuntimeError("fork canonical materialization 必须消费冻结 source snapshot")
        source_items = self.read_items(
            source_session_id, source_thread_id, checkpoint_ns=checkpoint_ns,
        )
        with self._connect(source_thread_id, checkpoint_ns, session_id=source_session_id,
                           read_only=True) as source_connection:
            source_message_rows = source_connection.execute(
                "SELECT m.message_id, m.turn_id, m.message_sequence, i.item_sequence, i.item_id FROM messages AS m "
                "JOIN item_catalog AS i ON i.jsonl_offset = m.jsonl_offset AND i.jsonl_length = m.jsonl_length "
                "ORDER BY m.message_sequence"
            ).fetchall()
            source_execution_by_item = dict(source_connection.execute(
                "SELECT item_id, execution_id FROM item_catalog"
            ).fetchall())
            source_executions = source_connection.execute(
                "SELECT execution_id, turn_id, execution_ordinal FROM executions"
            ).fetchall()
            source_model_calls = source_connection.execute(
                "SELECT model_call_id, execution_id FROM model_calls"
            ).fetchall()
        requested_messages = set(source_message_ids)
        source_messages = {row[0]: row for row in source_message_rows if row[0] in requested_messages}
        if set(source_messages) != requested_messages:
            raise RuntimeError("fork checkpoint 引用了不存在的 source canonical message")
        source_turn_ids = {row[1] for row in source_messages.values() if row[1] is not None}
        source_item_by_id = {item.item_id: item for item in source_items}
        source_group_messages = {
            source_item_by_id[row[4]].message_group_id: row[0]
            for row in source_messages.values() if source_item_by_id[row[4]].message_group_id is not None
        }
        cutoff = max(
            max((row[3] for row in source_messages.values()), default=0),
            max((item.item_sequence for item in source_items
                 if item.metadata.get("projection_message_id") in requested_messages
                 or (item.message_group_id is not None
                     and source_group_messages.get(item.message_group_id) in requested_messages)),
                default=0),
        )
        source_message_map = {
            value: self._full_copy_identity(fork_id, target_session_id, "message", value)
            for value in source_message_ids
        }
        selected = tuple(item for item in source_items if item.item_sequence <= cutoff and (
            (item.turn_id in source_turn_ids
             and (item.metadata.get("projection_message_id") is None
                  or item.metadata.get("projection_message_id") in requested_messages)
             and (item.message_group_id is None
                  or source_group_messages.get(item.message_group_id) in requested_messages))
            or item.metadata.get("projection_message_id") in requested_messages
            or item.turn_scope == TurnScope.AMBIENT
        ))
        if not selected:
            return ()
        fork_token = fork_id
        source_turn_rows = self._fork_v2_turn_records(source_session_id, checkpoint_ns)
        source_by_turn = {row[2]: row for row in source_turn_rows}
        for turn_id in source_turn_ids:
            if turn_id not in source_by_turn:
                raise RuntimeError(f"fork source 缺少真实 Turn/admission lineage: {turn_id}")
        source_turn_ids = {
            item.turn_id for item in selected if item.turn_id is not None
        }
        target_turn_map = {
            source_turn_id: (
                f"fork-turn:{target_thread_id}:"
                f"{hashlib.sha256((fork_token + ':' + source_turn_id).encode('utf-8')).hexdigest()[:32]}"
            )
            for source_turn_id in sorted(source_turn_ids)
        }
        used_executions = {source_execution_by_item[item.item_id] for item in selected}
        selected_ordinals = {turn_id: max((ordinal for execution_id, execution_turn, ordinal in source_executions
                                         if execution_turn == turn_id and execution_id in used_executions), default=1)
                             for turn_id in source_turn_ids}
        source_execution_map = {
            execution_id: self._full_copy_identity(fork_id, target_session_id, "execution", execution_id)
            for execution_id, turn_id, ordinal in source_executions
            if (turn_id in selected_ordinals and ordinal <= selected_ordinals[turn_id])
            or execution_id in used_executions
        }
        source_model_call_map = {
            model_call_id: self._full_copy_identity(fork_id, target_session_id, "model_call", model_call_id)
            for model_call_id, execution_id in source_model_calls if execution_id in source_execution_map
        }

        item_id_map = {item.item_id: self._full_copy_identity(fork_id, target_session_id, "item", item.item_id)
                       for item in selected}
        for source_message_id, row in source_messages.items():
            anchor = source_item_by_id[row[4]]
            group = (tuple(item for item in selected if item.message_group_id == anchor.message_group_id)
                     if anchor.message_group_id is not None else (anchor,))
            projected = self._codec().from_dict(self._codec().project_message(group))
            target_group = self._codec().items_for_message(
                projected, item_sequence=group[0].item_sequence,
                message_id=source_message_map[source_message_id], turn_id=target_turn_map.get(anchor.turn_id),
                execution_id=source_execution_map[source_execution_by_item[anchor.item_id]], timestamp=anchor.created_at,
            )
            if len(group) != len(target_group):
                raise RuntimeError("fork source canonical group 与 target projection identity 数量不一致")
            for source_item, target_item in zip(group, target_group, strict=True):
                if source_item.semantic_kind != target_item.semantic_kind:
                    raise RuntimeError("fork target projection identity 改变了 canonical semantic kind")
                item_id_map[source_item.item_id] = target_item.item_id
        remapped: list[CanonicalItemRecord] = []

        def remap_fork_identity(entity_type: str, value: object) -> object:
            if not isinstance(value, str) or not value:
                return value
            return (
                f"fork-{entity_type}:{target_thread_id}:"
                f"{hashlib.sha256((fork_token + ':' + value).encode('utf-8')).hexdigest()[:32]}"
            )

        def remap_payload(value: object) -> object:
            if isinstance(value, Mapping):
                result: dict[str, object] = {}
                for key, child in value.items():
                    if not isinstance(key, str) or not key:
                        raise ValueError("fork payload object key 必须是非空字符串")
                    key_text = key
                    if key_text in {"tool_call_id", "call_id"}:
                        child = remap_fork_identity("tool-call", child)
                    elif key_text in {"tool_invocation_id", "tool_attempt_id"}:
                        child = remap_fork_identity(key_text.removesuffix("_id"), child)
                    elif key_text == "result_id":
                        child = (source_message_map[child] if isinstance(child, str) and child in source_message_map
                                 else remap_fork_identity("tool-result", child))
                    elif (
                        key_text == "id"
                        and isinstance(child, str)
                        and "name" in value
                        and "args" in value
                    ):
                        # 只在工具 call/result payload 的已知位置重写；其它
                        # 业务 payload 的通用 id 不是 rollout identity。
                        child = remap_fork_identity("tool-call", child)
                    result[key_text] = remap_payload(child)
                return result
            if isinstance(value, list):
                return [remap_payload(child) for child in value]
            if isinstance(value, tuple):
                return [remap_payload(child) for child in value]
            return value

        for sequence, item in enumerate(selected, 1):
            target_item_id = item_id_map[item.item_id]
            metadata = {
                **dict(item.metadata),
                "fork_source_session_id": source_session_id,
                "fork_source_item_id": item.item_id,
                "fork_source_turn_id": item.turn_id,
            }
            source_message_id = item.metadata.get("projection_message_id")
            if source_message_id not in source_message_map:
                source_message_id = next(
                    (message_id for message_id in source_message_map
                     if item.item_id == f"item-{message_id}"),
                    None,
                )
            if source_message_id in source_message_map:
                metadata["projection_message_id"] = source_message_map[source_message_id]
            source_group_message_id = (
                source_group_messages.get(item.message_group_id)
                if item.message_group_id is not None
                else source_message_id
            )
            target_message_group_id = (
                f"message-{source_message_map[source_group_message_id]}"
                if source_group_message_id in source_message_map
                else remap_fork_identity("message-group", item.message_group_id)
            )
            metadata["context_fork_source_session_id"] = source_session_id
            for key, entity_type in (("tool_call_id", "tool-call"),
                                     ("tool_invocation_id", "tool_invocation"),
                                     ("tool_attempt_id", "tool_attempt"),
                                     ("result_id", "tool-result")):
                if key in metadata:
                    value = metadata[key]
                    metadata[key] = (source_message_map[value] if key == "result_id"
                                     and isinstance(value, str) and value in source_message_map
                                     else remap_fork_identity(entity_type, value))
            source_execution = strict_text(source_execution_by_item[item.item_id], field="source.item.execution_id")
            if source_execution not in source_execution_map:
                raise RuntimeError("fork canonical item 缺少真实 source execution")
            metadata["fork_source_execution_id"] = source_execution
            metadata["execution_id"] = source_execution_map[source_execution]
            if item.turn_id is not None:
                metadata["turn_id"] = target_turn_map[item.turn_id]
                if "accepted_ingress_id" in metadata:
                    metadata["accepted_ingress_id"] = target_local_acceptance_identity(
                        target_session_id=target_session_id, fork_id=fork_id,
                        source_accepted_ingress_id=source_by_turn[item.turn_id][6],
                        source_acceptance_key=source_by_turn[item.turn_id][7],
                    )[0]
            if "model_call_id" in metadata:
                source_model = metadata["model_call_id"]
                if source_model not in source_model_call_map:
                    raise RuntimeError("fork canonical item 的 source model call 缺少真实 lineage")
                metadata["model_call_id"] = source_model_call_map[source_model]
            producer = dict(item.producer_ref)
            producer_id = producer.get("producer_id")
            if isinstance(producer_id, str) and producer_id:
                producer["producer_id"] = remap_fork_identity("tool-call", producer_id) if producer.get("producer_kind") == "tool" else source_model_call_map.get(producer_id) or source_message_map.get(producer_id) or (
                    f"fork-producer:{target_thread_id}:"
                    f"{hashlib.sha256((fork_token + ':' + producer_id).encode('utf-8')).hexdigest()[:32]}"
                )
            producer["invocation_id"] = source_execution_map[source_execution]
            remapped_payload = (
                remap_payload(item.payload)
                if item.semantic_kind
                in {SemanticKind.TOOL_CALL, SemanticKind.TOOL_RESULT}
                else item.payload
            )
            remapped.append(
                CanonicalItemRecord.create(
                    item_sequence=sequence,
                    item_id=target_item_id,
                    semantic_kind=item.semantic_kind,
                    payload_kind=item.payload_kind,
                    status=item.status,
                    producer_ref=producer,
                    payload=remapped_payload,
                    created_at=item.created_at,
                    metadata=metadata,
                    turn_id=(
                        target_turn_map[item.turn_id]
                        if item.turn_id is not None
                        else None
                    ),
                    turn_scope=item.turn_scope,
                    message_group_id=target_message_group_id,
                    wire_role=item.wire_role,
                )
            )
        with (self._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
              self._connect(target_thread_id, checkpoint_ns, session_id=target_session_id) as connection):
            journal = connection.execute(
                "SELECT fork_id, source_session_id, source_thread_id, target_thread_id, checkpoint_ns, status "
                "FROM fork_materializations WHERE materialization_id = ? AND target_session_id = ?",
                (materialization_id, target_session_id),
            ).fetchone()
            if journal != (fork_id, source_session_id, source_thread_id, target_thread_id, checkpoint_ns, "prepared"):
                raise RuntimeError("fork canonical materialization 与 prepared journal scope 不一致")
            connection.execute("BEGIN IMMEDIATE")
            self._append_v2_records_transaction(
                connection, target_thread_id, checkpoint_ns, tuple(remapped),
                session_id=target_session_id, commit_kind=CommitKind.ITEM_CONVERGENCE,
                subject_id=materialization_id, idempotency_key=f"fork-items:{materialization_id}",
                begin_transaction=False,
            )
            timestamp = utc_now_text()
            self._ensure_fork_v2_turn_records(
                connection, source_session_id=source_session_id, target_session_id=target_session_id,
                target_thread_id=target_thread_id, fork_id=fork_id, checkpoint_ns=checkpoint_ns,
                source_turn_rows=source_turn_rows, timestamp=timestamp,
            )
            for source_item, target_item in zip(selected, remapped, strict=True):
                source_execution = strict_text(
                    source_execution_by_item[source_item.item_id], field="fork.source.item.execution_id"
                )
                target_execution = connection.execute(
                    "SELECT target_local_id FROM fork_identity_mappings "
                    "WHERE fork_id = ? AND entity_type = 'execution' AND source_local_id = ?",
                    (fork_id, source_execution),
                ).fetchone()
                if target_execution is None:
                    raise RuntimeError(f"fork canonical item 缺少真实 execution mapping: {source_execution}")
                self._bind_canonical_item_execution(
                    connection, item_id=target_item.item_id, execution_id=target_execution[0],
                )
            for source_message, target_message in source_message_map.items():
                self._ensure_fork_identity_mapping(
                    connection, source_session_id=source_session_id, target_session_id=target_session_id,
                    fork_id=fork_id, entity_type="message", source_id=source_message, target_id=target_message,
                    lineage={"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                        "message_id": source_message},
                             "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                        "message_id": target_message}}, timestamp=timestamp,
                )
            self._commit_connection(connection)
        return tuple(item.item_id for item in remapped)
