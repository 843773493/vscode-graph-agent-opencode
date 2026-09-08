"""v2 fork completion/clone owner。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.enums import ControlOutcome, TurnStatus
from app.services.infrastructure.rollout_context.fork.cloning import ForkCloneMixin
from app.services.infrastructure.rollout_context.fork.identity import (
    target_local_acceptance_identity,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    json_mapping,
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ForkCompletionMixin(ForkCloneMixin):
    """fork 后 Turn finalization、v2 turn lineage 与 clone owner。"""

    def _fork_completed_turn_finalizations(
        self,
        source_thread_id: str,
        source_checkpoint_id: str | None,
        checkpoint_ns: str,
    ) -> list[tuple[str, str]]:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        source_thread_id = required_text(
            source_thread_id, field="completed_finalizations.source_thread_id"
        )
        source_checkpoint_id = optional_text(
            source_checkpoint_id,
            field="completed_finalizations.source_checkpoint_id",
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("completed_finalizations.checkpoint_ns 必须是字符串")
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            return []
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(source_thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                if source_checkpoint_id is not None:
                    source_view_row = connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_view_row is None:
                        raise KeyError(source_checkpoint_id)
                    source_view_id = required_text(
                        source_view_row[0],
                        field="completed_finalizations.source_view_id",
                    )
                else:
                    source_view_row = connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                    source_view_id = (
                        required_text(
                            source_view_row[0],
                            field="completed_finalizations.source_view_id",
                        )
                        if source_view_row is not None
                        and source_view_row[0] is not None
                        else None
                    )
                if source_view_id is None:
                    return []
                rows = connection.execute(
                    """
                    SELECT DISTINCT t.turn_id, t.final_message_id
                    FROM context_view_turns AS cvt
                    JOIN turns AS t ON t.turn_id = cvt.turn_id
                    WHERE cvt.view_id = ?
                      AND t.status IN ('completed', 'succeeded')
                      AND t.final_message_id IS NOT NULL
                    ORDER BY t.turn_ordinal
                    """,
                    (source_view_id,),
                ).fetchall()
                return [
                    (
                        required_text(
                            turn_id,
                            field="completed_finalizations.turn_id",
                        ),
                        required_text(
                            message_id,
                            field="completed_finalizations.final_message_id",
                        ),
                    )
                    for turn_id, message_id in rows
                ]
        finally:
            source_lock.release()

    def _fork_v2_turn_records(
        self,
        source_thread_id: str,
        checkpoint_ns: str,
    ) -> tuple[tuple[object, ...], ...]:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        """读取 source v2 Turn/acceptance 的只读 lineage 输入。"""
        source_root = self.root(source_thread_id, checkpoint_ns)
        if not source_root.is_dir():
            return ()
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(
                source_thread_id, checkpoint_ns, read_only=True
            ) as connection:
                self._require_v2_runtime(connection)
                rows = tuple(
                    connection.execute(
                        """
                        SELECT tr.turn_id, tr.turn_ordinal, tr.source_branch_id,
                               tr.root_input_item_id, tr.accepted_ingress_id,
                               tr.acceptance_idempotency_key, tr.initial_execution_id,
                               tr.status, tr.final_item_id, tr.replay_of_turn_id,
                               ta.payload_hash, e.outcome
                        FROM turn_records AS tr
                        JOIN turn_acceptances AS ta
                          ON ta.turn_id = tr.turn_id
                        LEFT JOIN executions AS e
                          ON e.execution_id = tr.initial_execution_id
                        ORDER BY tr.turn_ordinal
                        """
                    ).fetchall()
                )
                for row in rows:
                    if len(row) != 12:
                        raise RuntimeError("fork source Turn manifest 字段数量不一致")
                    required_text(row[0], field="source.turn_id")
                    non_negative_int(row[1], field="source.turn_ordinal")
                    required_text(row[2], field="source.source_branch_id")
                    required_text(row[3], field="source.root_input_item_id")
                    required_text(row[4], field="source.accepted_ingress_id")
                    required_text(row[5], field="source.acceptance_idempotency_key")
                    required_text(row[6], field="source.initial_execution_id")
                    one_of_text(
                        row[7],
                        {item.value for item in TurnStatus},
                        field="source.status",
                    )
                    optional_text(row[8], field="source.final_item_id")
                    optional_text(row[9], field="source.replay_of_turn_id")
                    required_text(row[10], field="source.payload_hash")
                    optional_text(row[11], field="source.execution_outcome")
                return rows
        finally:
            source_lock.release()

    def _ensure_fork_v2_turn_records(
        self,
        connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        checkpoint_ns: str,
        source_turn_rows: Sequence[tuple[object, ...]],
        timestamp: str,
    ) -> None:
        """为非 full-copy fork 建立 target-local v2 Turn/acceptance/execution。

        context/history fork 的 checkpoint channel 只会经过普通 message
        writer，不会调用 acceptance API。这里在 fork 的最终事务中补齐 v2
        的业务身份；item copy 已经把 source identity 写入 metadata，下面
        用它把 target item/root 映射到新的 target Turn，并保留 source lineage。
        """
        source_by_turn: dict[str, tuple[object, ...]] = {}
        for row in source_turn_rows:
            if len(row) != 12:
                raise RuntimeError("fork source Turn manifest 字段数量不一致")
            turn_id = required_text(row[0], field="source.turn_id")
            if turn_id in source_by_turn:
                raise RuntimeError(f"fork source Turn manifest 重复: {turn_id}")
            source_by_turn[turn_id] = row
        roots = connection.execute(
            "SELECT turn_id, item_id, item_sequence, content_hash, metadata_json FROM item_catalog WHERE turn_scope = 'turn_root' AND semantic_kind = 'user_input' AND turn_id IS NOT NULL ORDER BY item_sequence"
        ).fetchall()
        all_items = connection.execute(
            "SELECT item_id, turn_id, metadata_json FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
        active_branch_row = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if active_branch_row is None:
            raise RuntimeError("fork target active branch 缺失")
        target_branch = required_text(
            active_branch_row[0], field="target.active_branch_id"
        )
        ordinal_row = connection.execute(
            "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turn_records"
        ).fetchone()
        if ordinal_row is None:
            raise RuntimeError("fork target Turn ordinal 无法解析")
        next_ordinal = non_negative_int(
            ordinal_row[0], field="target.next_turn_ordinal"
        )
        if next_ordinal == 0:
            raise RuntimeError("fork target Turn ordinal 不能为 0")
        target_turn_by_source: dict[str, str] = {}
        target_item_by_source: dict[str, str] = {}
        for target_item_id, target_turn_id, metadata_json in all_items:
            target_item_id = required_text(target_item_id, field="target.item_id")
            if target_turn_id is not None:
                target_turn_id = required_text(target_turn_id, field="target.turn_id")
            metadata = json_mapping(metadata_json, field="target.item.metadata_json")
            source_item_id = metadata.get("fork_source_item_id")
            source_turn_id = metadata.get("fork_source_turn_id")
            if source_item_id is not None and (
                not isinstance(source_item_id, str) or not source_item_id
            ):
                raise RuntimeError(
                    f"fork copied item source item identity 非法: {target_item_id}"
                )
            if source_turn_id is not None and (
                not isinstance(source_turn_id, str) or not source_turn_id
            ):
                raise RuntimeError(
                    f"fork copied item source Turn identity 非法: {target_item_id}"
                )
            if isinstance(source_item_id, str) and source_item_id:
                if source_item_id in target_item_by_source:
                    raise RuntimeError(
                        f"fork source item 映射到多个 target item: {source_item_id}"
                    )
                target_item_by_source[source_item_id] = target_item_id
            if isinstance(source_turn_id, str) and source_turn_id:
                if target_turn_id is None:
                    raise RuntimeError(
                        "fork copied item 带 source Turn lineage 但缺少 target turn_id: "
                        f"{target_item_id}"
                    )
                previous_target_turn_id = target_turn_by_source.get(source_turn_id)
                if (
                    previous_target_turn_id is not None
                    and previous_target_turn_id != target_turn_id
                ):
                    raise RuntimeError(
                        f"fork source Turn 映射到多个 target Turn: {source_turn_id}"
                    )
                target_turn_by_source.setdefault(source_turn_id, target_turn_id)

        for raw_turn_id, root_item_id, root_sequence, root_hash, metadata_json in roots:
            turn_id = required_text(raw_turn_id, field="target.root.turn_id")
            root_item_id = required_text(root_item_id, field="target.root.item_id")
            root_sequence = non_negative_int(
                root_sequence, field="target.root.item_sequence"
            )
            root_hash = required_text(root_hash, field="target.root.content_hash")
            metadata = json_mapping(metadata_json, field="target.root.metadata_json")
            # _copy_fork_checkpoint 产生的 LangChain projection 仍会暂时带有
            # source turn_id。它只是 checkpoint 的临时 view，不是 target 的
            # canonical Turn root；只有 copy_v2_items_for_fork 写入的、带有
            # fork_source_* lineage 的 item 才能进入 target TurnRecord。
            source_item_id = metadata.get("fork_source_item_id")
            source_turn_id_value = metadata.get("fork_source_turn_id")
            if source_item_id is None and source_turn_id_value is None:
                continue
            source_item_id = required_text(
                source_item_id, field="target.root.fork_source_item_id"
            )
            source_turn_id = required_text(
                source_turn_id_value, field="target.root.fork_source_turn_id"
            )
            source = source_by_turn.get(source_turn_id)
            if source is None:
                # 旧 source 可能只有 message/index projection，没有 v2
                # acceptance；不能用猜测值伪造一个 v2 Turn。
                continue
            if (
                connection.execute(
                    "SELECT 1 FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                is not None
            ):
                continue
            expected_source_turn_id = source_turn_id
            (
                raw_source_turn_id,
                source_turn_ordinal,
                source_branch_id,
                source_root_item_id,
                source_ingress,
                source_key,
                source_execution,
                source_status,
                source_final_item,
                source_replay_of,
                _source_payload_hash,
                source_execution_outcome,
            ) = source
            source_turn_id = required_text(raw_source_turn_id, field="source.turn_id")
            if source_turn_id != expected_source_turn_id:
                raise RuntimeError("fork source Turn identity 读取不一致")
            non_negative_int(source_turn_ordinal, field="source.turn_ordinal")
            source_branch_id = required_text(
                source_branch_id, field="source.source_branch_id"
            )
            source_root_item_id = required_text(
                source_root_item_id, field="source.root_input_item_id"
            )
            source_ingress = required_text(
                source_ingress, field="source.accepted_ingress_id"
            )
            source_key = required_text(
                source_key, field="source.acceptance_idempotency_key"
            )
            source_execution = required_text(
                source_execution, field="source.initial_execution_id"
            )
            source_status = one_of_text(
                source_status,
                {item.value for item in TurnStatus},
                field="source.status",
            )
            source_final_item = optional_text(
                source_final_item, field="source.final_item_id"
            )
            source_replay_of = optional_text(
                source_replay_of, field="source.replay_of_turn_id"
            )
            source_payload_hash = required_text(
                _source_payload_hash, field="source.payload_hash"
            )
            source_execution_outcome = optional_text(
                source_execution_outcome, field="source.execution_outcome"
            )
            if source_execution_outcome is not None:
                source_execution_outcome = one_of_text(
                    source_execution_outcome,
                    {item.value for item in ControlOutcome},
                    field="source.execution_outcome",
                )
            if source_item_id != source_root_item_id:
                raise RuntimeError(
                    "fork target root 与 source Turn root identity 不一致: "
                    f"turn_id={turn_id}"
                )
            if root_hash != source_payload_hash:
                raise RuntimeError(
                    "fork target root content hash 与 source acceptance 不一致: "
                    f"turn_id={turn_id}"
                )
            target_ingress, target_key = target_local_acceptance_identity(
                target_session_id=target_session_id,
                fork_id=fork_id,
                source_accepted_ingress_id=source_ingress,
                source_acceptance_key=source_key,
            )
            execution_seed = hashlib.sha256(
                f"{target_session_id}|{fork_id}|{turn_id}|{target_ingress}".encode()
            ).hexdigest()[:32]
            target_execution = f"fork-execution:{target_session_id}:{execution_seed}"
            if (
                connection.execute(
                    "SELECT 1 FROM turn_acceptances WHERE accepted_ingress_id = ? OR acceptance_idempotency_key = ?",
                    (target_ingress, target_key),
                ).fetchone()
                is not None
            ):
                raise ValueError(
                    f"fork target acceptance identity 冲突: turn_id={turn_id}"
                )
            target_status = one_of_text(
                source_status,
                {item.value for item in TurnStatus},
                field="source.turn.status",
            )
            target_final_item = (
                target_item_by_source.get(source_final_item)
                if source_final_item is not None
                and connection.execute(
                    "SELECT 1 FROM item_catalog WHERE item_id = ? AND turn_id = ? AND semantic_kind = 'assistant_output' AND status = 'completed'",
                    (target_item_by_source.get(source_final_item), turn_id),
                ).fetchone()
                is not None
                and target_status == TurnStatus.COMPLETED.value
                else None
            )
            if (
                target_status == TurnStatus.COMPLETED.value
                and target_final_item is None
            ):
                # source final item 不在本次 prefix 中时，不能留下违反
                # TurnRecord invariant 的 completed/null 组合。
                target_status = TurnStatus.UNKNOWN.value
            target_execution_outcome = (
                source_execution_outcome or ControlOutcome.UNKNOWN.value
            )
            result = connection.execute(
                "INSERT INTO turn_acceptances(accepted_ingress_id, acceptance_idempotency_key, session_id, turn_id, payload_hash, source_session_id, source_accepted_ingress_id, source_acceptance_idempotency_key, identity_origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fork_copied', ?)",
                (
                    target_ingress,
                    target_key,
                    target_session_id,
                    turn_id,
                    root_hash,
                    source_session_id,
                    source_ingress,
                    source_key,
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"fork target acceptance 未写入: {turn_id}")
            result = connection.execute(
                "INSERT INTO turn_records(turn_id, turn_ordinal, source_branch_id, root_input_item_id, root_input_item_sequence, accepted_ingress_id, acceptance_idempotency_key, initial_execution_id, last_execution_id, status, final_item_id, replay_of_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    next_ordinal,
                    target_branch,
                    root_item_id,
                    root_sequence,
                    target_ingress,
                    target_key,
                    target_execution,
                    target_execution,
                    target_status,
                    target_final_item,
                    target_turn_by_source.get(source_replay_of)
                    if source_replay_of is not None
                    else None,
                    timestamp,
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"fork target Turn 未写入: {turn_id}")
            result = connection.execute(
                "INSERT INTO executions(execution_id, turn_id, attempt, execution_ordinal, accepted_ingress_id, outcome, resumed_from_execution_id, replay_of_execution_id, created_at) VALUES (?, ?, 1, 1, ?, ?, NULL, NULL, ?)",
                (
                    target_execution,
                    turn_id,
                    target_ingress,
                    target_execution_outcome,
                    timestamp,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"fork target execution 未写入: {turn_id}")
            result = connection.execute(
                "INSERT INTO turn_execution_links(turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, 'fork_copied', 1, ?, ?)",
                (turn_id, target_execution, f"{turn_id}:fork:{fork_id}", timestamp),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"fork target execution link 未写入: {turn_id}")
            lineage = {
                "identity_mode": "remapped_target_local",
                "fork_id": fork_id,
                "source": {
                    "session_id": source_session_id,
                    "turn_id": source_turn_id,
                    "source_branch_id": source_branch_id,
                    "root_input_item_id": source_root_item_id,
                    "accepted_ingress_id": source_ingress,
                    "acceptance_idempotency_key": source_key,
                    "initial_execution_id": source_execution,
                },
                "target": {
                    "session_id": target_session_id,
                    "turn_id": turn_id,
                    "source_branch_id": target_branch,
                    "root_input_item_id": root_item_id,
                    "accepted_ingress_id": target_ingress,
                    "acceptance_idempotency_key": target_key,
                    "initial_execution_id": target_execution,
                },
            }
            for entity_type, source_id, target_id in (
                ("turn", source_turn_id, turn_id),
                ("accepted_ingress", source_ingress, target_ingress),
                ("acceptance_idempotency_key", source_key, target_key),
                ("item", source_root_item_id, root_item_id),
                ("execution", source_execution, target_execution),
            ):
                result = connection.execute(
                    "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        source_session_id,
                        target_session_id,
                        entity_type,
                        source_id,
                        target_id,
                        _json(lineage),
                        timestamp,
                    ),
                )
            for target_item_id, target_item_turn_id, metadata_json in all_items:
                target_item_id = required_text(target_item_id, field="target.item_id")
                if target_item_turn_id is not None:
                    target_item_turn_id = required_text(
                        target_item_turn_id, field="target.turn_id"
                    )
                item_metadata = json_mapping(
                    metadata_json, field="target.item.metadata_json"
                )
                source_item_id = item_metadata.get("fork_source_item_id")
                source_item_turn_id = item_metadata.get("fork_source_turn_id")
                if source_item_id is not None and (
                    not isinstance(source_item_id, str) or not source_item_id
                ):
                    raise RuntimeError(
                        f"fork copied item source item identity 非法: {target_item_id}"
                    )
                if source_item_turn_id is not None and (
                    not isinstance(source_item_turn_id, str) or not source_item_turn_id
                ):
                    raise RuntimeError(
                        f"fork copied item source Turn identity 非法: {target_item_id}"
                    )
                if source_item_id is None:
                    continue
                if source_item_turn_id is None:
                    raise RuntimeError(
                        f"fork copied item 缺少 source Turn lineage: {target_item_id}"
                    )
                if source_item_turn_id != source_turn_id:
                    continue
                if target_item_id == root_item_id:
                    continue
                item_lineage = {
                    **lineage,
                    "source": {
                        **lineage["source"],
                        "item_id": source_item_id,
                    },
                    "target": {
                        **lineage["target"],
                        "item_id": target_item_id,
                    },
                }
                result = connection.execute(
                    "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, target_session_id, entity_type, source_local_id, target_local_id, source_offset, target_offset, lineage_json, created_at) VALUES (?, ?, ?, ?, 'item', ?, ?, NULL, NULL, ?, ?)",
                    (
                        uuid4().hex,
                        fork_id,
                        source_session_id,
                        target_session_id,
                        source_item_id,
                        target_item_id,
                        _json(item_lineage),
                        timestamp,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"fork item identity mapping 未写入: {target_item_id}"
                    )
            next_ordinal += 1
