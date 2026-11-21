"""异步留言与删除墓碑共用 Thread 原 SQLite；不从工具历史重建列表。"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from app.core.sqlite_state import utc_now_text
from app.schemas.internal_v2.user_question import (
    UserQuestionDTO,
    UserQuestionPageDTO,
    UserQuestionSummaryDTO,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


class UserQuestionStore:
    def __init__(self, storage: RolloutStorage) -> None:
        self._storage = storage

    def create(self, session_id: str, thread_id: str, *, question_id: str, markdown: str,
               model_call_id: str, turn_id: str | None) -> str:
        with self._storage.write_connection(session_id, thread_id) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT markdown,model_call_id,turn_id FROM user_questions WHERE question_id=?", (question_id,),
            ).fetchone()
            if existing is not None:
                if existing != (markdown, model_call_id, turn_id):
                    raise ValueError("同一工具操作不能保存不同留言")
                return question_id
            connection.execute(
                "INSERT INTO user_questions(question_id,markdown,model_call_id,turn_id,created_at) VALUES(?,?,?,?,?)",
                (question_id, markdown, model_call_id, turn_id, utc_now_text()),
            )
            self._storage._commit_connection(connection)
        return question_id

    @staticmethod
    def _summary(connection: sqlite3.Connection, session_id: str, thread_id: str) -> UserQuestionSummaryDTO:
        count = connection.execute("SELECT COUNT(*) FROM user_questions WHERE deleted_at IS NULL").fetchone()[0]
        revision = connection.execute("SELECT revision FROM user_question_state WHERE singleton_id=1").fetchone()[0]
        return UserQuestionSummaryDTO(session_id=session_id, thread_id=thread_id, count=count, revision=revision)

    def summary(self, session_id: str, thread_id: str) -> UserQuestionSummaryDTO:
        try:
            with self._storage.read_connection(session_id, thread_id) as connection:
                return self._summary(connection, session_id, thread_id)
        except RolloutNotMaterializedError:
            return UserQuestionSummaryDTO(session_id=session_id, thread_id=thread_id, count=0, revision=0)

    def page(self, session_id: str, thread_id: str, *, before: int | None, limit: int) -> UserQuestionPageDTO:
        if not 1 <= limit <= 50:
            raise ValueError("留言页大小必须为 1..50")
        try:
            with self._storage.read_connection(session_id, thread_id) as connection:
                summary = self._summary(connection, session_id, thread_id)
                rows = connection.execute(
                    "SELECT question_id,sequence,markdown,turn_id,created_at FROM user_questions "
                    "WHERE deleted_at IS NULL" + (" AND sequence<?" if before is not None else "")
                    + " ORDER BY sequence DESC LIMIT ?", (before, limit + 1) if before is not None else (limit + 1,),
                ).fetchall()
                items = [UserQuestionDTO(question_id=row[0], sequence=row[1], markdown=row[2], turn_id=row[3], created_at=row[4]) for row in rows[:limit]]
                return UserQuestionPageDTO(**summary.model_dump(), items=items, next_before=items[-1].sequence if len(rows) > limit else None)
        except RolloutNotMaterializedError:
            return UserQuestionPageDTO(session_id=session_id, thread_id=thread_id, count=0, revision=0, items=[], next_before=None)

    def delete(self, session_id: str, thread_id: str, question_id: str) -> UserQuestionSummaryDTO:
        with self._storage.write_connection(session_id, thread_id) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT deleted_at FROM user_questions WHERE question_id=?", (question_id,)).fetchone()
            if row is None:
                raise KeyError(question_id)
            if row[0] is None:
                connection.execute("UPDATE user_questions SET deleted_at=? WHERE question_id=?", (utc_now_text(), question_id))
            summary = self._summary(connection, session_id, thread_id)
            self._storage._commit_connection(connection)
            return summary
