"""v2 checkpoint boundary 与 rewind 操作 owner。

这里维护 active view/anchor 的 durable 操作；只调用 RolloutStorage 的
transaction/domain port，不提供旧 v1 runtime fallback。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from app.services.infrastructure.rollout_context.checkpoint.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_reads import (
    read_activation_refs_for_turns,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutManifest,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RolloutCheckpointOperationsMixin:
    """checkpoint boundary、rewind 与 view revision owner。"""

    def create_context_boundary(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        boundary: str,
        source_checkpoint_id: str | None,
        source_anchor: str | None,
        source_view_id: str | None = None,
        source_turn_id: str | None = None,
        base_message_sequence: int | None = None,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        thread_id = strict_text(thread_id, field="context_boundary.thread_id")
        strict_text(
            checkpoint_ns, field="context_boundary.checkpoint_ns", allow_empty=True
        )
        boundary = strict_text(boundary, field="context_boundary.boundary")
        source_checkpoint_id = strict_optional_text(
            source_checkpoint_id, field="context_boundary.source_checkpoint_id"
        )
        source_anchor = strict_optional_text(
            source_anchor, field="context_boundary.source_anchor"
        )
        source_view_id = strict_optional_text(
            source_view_id, field="context_boundary.source_view_id"
        )
        source_turn_id = strict_optional_text(
            source_turn_id, field="context_boundary.source_turn_id"
        )
        base_message_sequence = (
            strict_non_negative_int(
                base_message_sequence,
                field="context_boundary.base_message_sequence",
            )
            if base_message_sequence is not None
            else None
        )
        anchor_mode = strict_text(anchor_mode, field="context_boundary.anchor_mode")
        if anchor_mode not in {"inclusive", "before"}:
            raise ValueError("context anchor_mode 必须是 inclusive 或 before")
        if boundary not in {"rewind", "history_replay", "compaction"}:
            raise ValueError(
                "context boundary 必须是 rewind、history_replay 或 compaction"
            )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                if source_turn_id is not None:
                    resolved = self._resolve_turn_anchor_connection(
                        connection,
                        source_turn_id,
                        checkpoint_ns=checkpoint_ns,
                        anchor_mode=anchor_mode,
                    )
                    source_checkpoint_id = resolved.checkpoint_id
                    source_view_id = resolved.view_id
                    base_message_sequence = resolved.cutoff_message_sequence
                    source_anchor = None
                source = (
                    self._checkpoint_row(
                        connection,
                        checkpoint_ns,
                        source_checkpoint_id,
                    )
                    if source_checkpoint_id
                    else self._checkpoint_row(connection, checkpoint_ns, None)
                )
                if source is None:
                    return self._manifest_from_connection(connection, checkpoint_ns)
                source_checkpoint_value = strict_text(
                    source[0], field="checkpoints.checkpoint_id"
                )
                source_view_id = source_view_id or strict_text(
                    source[6], field="checkpoints.view_id"
                )
                source_sequences = self._view_message_sequences(
                    thread_id,
                    checkpoint_ns,
                    source_view_id,
                    set(),
                    connection=connection,
                )
                cutoff = len(source_sequences)
                if base_message_sequence is not None:
                    matching = [
                        index
                        for index, sequence in enumerate(source_sequences)
                        if sequence <= base_message_sequence
                    ]
                    cutoff = matching[-1] + 1 if matching else 0
                if source_anchor is not None:
                    anchor_row = connection.execute(
                        "SELECT message_sequence FROM messages WHERE message_id = ?",
                        (source_anchor,),
                    ).fetchone()
                    if anchor_row is None:
                        raise KeyError(f"rewind anchor 不存在: {source_anchor}")
                    try:
                        anchor_sequence = strict_non_negative_int(
                            anchor_row[0], field="messages.message_sequence"
                        )
                        anchor_index = source_sequences.index(anchor_sequence)
                    except ValueError as error:
                        raise KeyError(
                            f"rewind anchor 不属于 source view: {source_anchor}"
                        ) from error
                    cutoff = anchor_index + (1 if anchor_mode == "inclusive" else 0)
                visible_sequences = source_sequences[:cutoff]
                # rewind/compaction 必须保留被切断/隐藏的 sealed assembly 的精确
                # activation snapshot/ref：这些引用写入同一 control event，后续
                # restore 只读该引用，永不解析当前 URI 或 Registry。schema 未 bootstrap
                # 或没有任何 sealed activation 绑定时不写该键，保持既有 payload 字节。
                source_turn_ids = tuple(
                    dict.fromkeys(
                        strict_text(row[0], field="context_view_turns.turn_id")
                        for row in connection.execute(
                            "SELECT turn_id FROM context_view_turns "
                            "WHERE view_id = ? ORDER BY logical_turn_ordinal",
                            (source_view_id,),
                        ).fetchall()
                    )
                )
                activation_refs = read_activation_refs_for_turns(
                    connection,
                    session_id=thread_id,
                    turn_ids=source_turn_ids,
                )
                # 在创建目标 view/branch 前验证 tool protocol closure：
                # 冲突时旧 active view 与全部状态保持零副作用。
                validate_tool_protocol_closure(
                    connection,
                    source_sequences,
                    cutoff,
                )
                branch_id = "branch-" + uuid4().hex[:12]
                timestamp = _now()
                old_active_branch, _projection_epoch = self._namespace_state(
                    connection, checkpoint_ns
                )
                view_id = self._create_view(
                    connection,
                    branch_id,
                    source_view_id,
                    visible_sequences,
                    timestamp,
                    view_kind=boundary,
                )
                cursor = connection.execute(
                    "INSERT INTO branches(branch_id, branch_kind, status, head_view_id, head_checkpoint_id, parent_branch_id, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
                    (
                        branch_id,
                        boundary,
                        view_id,
                        source_checkpoint_value,
                        old_active_branch,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("context boundary 新 branch 未写入")
                cursor = connection.execute(
                    "UPDATE branches SET status = 'inactive', updated_at = ? WHERE branch_id = ?",
                    (timestamp, old_active_branch),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("context boundary 旧 active branch 更新失败")
                cursor = connection.execute(
                    "UPDATE checkpoint_namespace_state SET active_branch_id = ?, projection_epoch = projection_epoch + 1, updated_at = ? WHERE checkpoint_ns = ?",
                    (branch_id, timestamp, checkpoint_ns),
                )
                control_payload: dict[str, object] = {
                    "source_checkpoint_id": source_checkpoint_id,
                    "source_anchor": source_anchor,
                    "source_turn_id": source_turn_id,
                    "source_view_id": source_view_id,
                    "anchor_mode": anchor_mode,
                    "cutoff_message_sequence": (
                        visible_sequences[-1] if visible_sequences else None
                    ),
                }
                if activation_refs:
                    control_payload["resource_activation_refs"] = [
                        dict(ref) for ref in activation_refs
                    ]
                control_sequence = self._insert_control(
                    connection,
                    boundary,
                    "branch",
                    branch_id,
                    branch_id,
                    view_id,
                    source_checkpoint_value,
                    control_payload,
                    uuid4().hex,
                    timestamp,
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("context boundary namespace state 更新失败")
                cursor = connection.execute(
                    "UPDATE database_meta SET last_control_sequence = ?, history_view_revision = history_view_revision + 1, updated_at = ? WHERE singleton_id = 1",
                    (control_sequence, timestamp),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("context boundary database_meta 更新失败")
                cursor = connection.execute(
                    "UPDATE context_views SET control_sequence = ? WHERE view_id = ?",
                    (control_sequence, view_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("context boundary context view 更新失败")
                connection.commit()
                return self._manifest_from_connection(connection, checkpoint_ns)

    def rewind_to_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        checkpoint_id: str,
        source_anchor: str | None = None,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        return self.create_context_boundary(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            boundary="rewind",
            source_checkpoint_id=checkpoint_id,
            source_anchor=source_anchor,
            anchor_mode=anchor_mode,
        )

    def rewind_to_turn(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        """只接受用户 Turn 锚点，内部解析实际 source view/checkpoint。"""
        return self.create_context_boundary(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            boundary="rewind",
            source_checkpoint_id=None,
            source_anchor=None,
            source_turn_id=turn_id,
            anchor_mode=anchor_mode,
        )

    def history_replay_to_turn(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        turn_id: str,
        anchor_mode: str = "inclusive",
    ) -> RolloutManifest:
        """为历史投影建立新 view，但不创建 execution 或新的 Turn。"""
        return self.create_context_boundary(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            boundary="history_replay",
            source_checkpoint_id=None,
            source_anchor=None,
            source_turn_id=turn_id,
            anchor_mode=anchor_mode,
        )
