"""v2 checkpoint context view message projection owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


class RolloutMessageViewMixin:
    """只按已提交 context view 查询消息 locator，不实现 projection。"""

    def _messages_for_view(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        view_id: str,
        *,
        connection: sqlite3.Connection | None = None,
        selection_view_id: str | None = None,
        execution_view_id: str | None = None,
        additional_message_sequences: Collection[int] = (),
        execution_fenced: bool = False,
    ) -> list[object]:
        view_id = strict_text(view_id, field="context_views.view_id")
        sequences = self._storage._view_message_sequences(
            thread_id,
            checkpoint_ns,
            view_id,
            set(),
            session_id=session_id,
            connection=connection,
        )
        if selection_view_id is not None:
            selected = set(self._storage._view_message_sequences(
                thread_id, checkpoint_ns, selection_view_id, set(),
                session_id=session_id, connection=connection,
            ))
            sequences = [sequence for sequence in sequences if sequence in selected]
        additional = set(additional_message_sequences) - set(sequences)
        if additional:
            if connection is None or execution_view_id is None:
                raise RuntimeError("执行消息合并必须复用当前 active context snapshot")
            ordered = list(dict.fromkeys(row[0] for row in connection.execute(
                "SELECT message.message_sequence FROM context_view_items AS member "
                "JOIN item_catalog AS item ON item.item_id=member.item_id "
                "JOIN messages AS message ON message.jsonl_offset=item.jsonl_offset "
                "AND message.jsonl_length=item.jsonl_length "
                "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 "
                "ORDER BY member.context_ordinal", (execution_view_id,),
            )))
            positions = {sequence: index for index, sequence in enumerate(ordered)}
            if additional - positions.keys():
                raise RuntimeError("执行消息缺少 active context 顺序")
            pending = iter(sequence for sequence in ordered if sequence in additional)
            next_sequence = next(pending, None)
            merged: list[int] = []
            # frozen raw 保持原子序列；只把合法补充项插入其 active context 锚点前。
            # 被压缩的 middle 不在当前 selection 中，仍留在 raw event 的原坐标。
            for sequence in sequences:
                while (next_sequence is not None and sequence in positions
                       and positions[next_sequence] < positions[sequence]):
                    merged.append(next_sequence)
                    next_sequence = next(pending, None)
                merged.append(sequence)
            if next_sequence is not None:
                merged.extend((next_sequence, *pending))
            sequences = merged
        if execution_fenced:
            if connection is None:
                raise RuntimeError("执行上下文过滤必须复用当前 checkpoint snapshot")
            excluded_ids = self.execution_context_excluded_item_ids(
                connection, session_id=session_id, thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
            )
            if excluded_ids:
                placeholders = ",".join("?" for _ in excluded_ids)
                excluded_sequences = {row[0] for row in connection.execute(
                    "SELECT message.message_sequence FROM messages AS message JOIN item_catalog AS item "
                    "ON item.jsonl_offset=message.jsonl_offset AND item.jsonl_length=message.jsonl_length "
                    f"WHERE item.item_id IN ({placeholders})", tuple(excluded_ids),
                )}
                sequences = [sequence for sequence in sequences if sequence not in excluded_sequences]
        if not sequences:
            return []
        values = self._read_messages(
            session_id,
            thread_id,
            checkpoint_ns,
            sequences,
            connection=connection,
        )
        return [self._storage._codec().from_dict(value) for value in values]
