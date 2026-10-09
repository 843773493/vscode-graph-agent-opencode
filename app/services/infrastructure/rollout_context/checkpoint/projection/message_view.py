"""v2 checkpoint context view message projection owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

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
        additional_message_sequences: Sequence[int] = (),
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
        sequences = list(dict.fromkeys((*sequences, *additional_message_sequences)))
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
