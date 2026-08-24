from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.core.sqlite_state import utc_now_text
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessContext


@dataclass(frozen=True, slots=True)
class UserViewStateRecord:
    user_id: str
    workspace_id: str
    session_id: str
    turn_anchor: str | None
    scroll_offset: float
    follow_latest: bool
    projection_version: int
    tool_details_expanded: bool
    updated_at: str


class UserViewStateStore:
    def __init__(self, *, state: GatewayStateStore) -> None:
        self._state = state

    @staticmethod
    def _require_user(context: UserAccessContext) -> str:
        if context.kind != "user" or context.user_id is None:
            raise PermissionError("guest_view_state_not_persistent")
        return context.user_id

    @staticmethod
    def _assert_active_lease(
        connection: sqlite3.Connection,
        context: UserAccessContext,
    ) -> str:
        user_id = UserViewStateStore._require_user(context)
        row = connection.execute(
            """
            SELECT 1
            FROM user_access_lease
            WHERE user_id = ?
              AND access_session_id = ?
              AND lease_generation = ?
              AND expires_at > ?
            """,
            (
                user_id,
                context.access_session_id,
                context.lease_generation,
                utc_now_text(),
            ),
        ).fetchone()
        if row is None:
            context.invalidated.set()
            raise PermissionError("user_session_taken_over")
        return user_id

    @staticmethod
    def _record(row: sqlite3.Row) -> UserViewStateRecord:
        return UserViewStateRecord(
            user_id=str(row[0]),
            workspace_id=str(row[1]),
            session_id=str(row[2]),
            turn_anchor=str(row[3]) if row[3] is not None else None,
            scroll_offset=float(row[4]),
            follow_latest=bool(row[5]),
            projection_version=int(row[6]),
            tool_details_expanded=bool(row[7]),
            updated_at=str(row[8]),
        )

    def get(
        self,
        *,
        context: UserAccessContext,
        workspace_id: str,
        session_id: str,
    ) -> UserViewStateRecord | None:
        connection = self._state.connection()
        try:
            user_id = self._assert_active_lease(connection, context)
            row = connection.execute(
                """
                SELECT user_id, workspace_id, session_id, turn_anchor,
                       scroll_offset, follow_latest, projection_version,
                       tool_details_expanded, updated_at
                FROM user_view_state
                WHERE user_id = ? AND workspace_id = ? AND session_id = ?
                """,
                (user_id, workspace_id, session_id),
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            connection.close()

    def get_latest(self, *, context: UserAccessContext) -> UserViewStateRecord | None:
        connection = self._state.connection()
        try:
            user_id = self._assert_active_lease(connection, context)
            row = connection.execute(
                """
                SELECT user_id, workspace_id, session_id, turn_anchor,
                       scroll_offset, follow_latest, projection_version,
                       tool_details_expanded, updated_at
                FROM user_view_state
                WHERE user_id = ?
                ORDER BY updated_at DESC, workspace_id ASC, session_id ASC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            connection.close()

    def put(
        self,
        *,
        context: UserAccessContext,
        workspace_id: str,
        session_id: str,
        turn_anchor: str | None,
        scroll_offset: float,
        follow_latest: bool,
        projection_version: int,
        tool_details_expanded: bool,
    ) -> UserViewStateRecord:
        if scroll_offset < 0:
            raise ValueError("视图滚动偏移不能小于 0")
        if projection_version < 1:
            raise ValueError("视图投影版本必须大于 0")
        updated_at = utc_now_text()
        connection = self._state.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            user_id = self._assert_active_lease(connection, context)
            connection.execute(
                """
                INSERT INTO user_view_state(
                    user_id, workspace_id, session_id, turn_anchor, scroll_offset,
                    follow_latest, projection_version, tool_details_expanded, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, workspace_id, session_id) DO UPDATE SET
                    turn_anchor=excluded.turn_anchor,
                    scroll_offset=excluded.scroll_offset,
                    follow_latest=excluded.follow_latest,
                    projection_version=excluded.projection_version,
                    tool_details_expanded=excluded.tool_details_expanded,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    workspace_id,
                    session_id,
                    turn_anchor,
                    scroll_offset,
                    int(follow_latest),
                    projection_version,
                    int(tool_details_expanded),
                    updated_at,
                ),
            )
            row = connection.execute(
                """
                SELECT user_id, workspace_id, session_id, turn_anchor,
                       scroll_offset, follow_latest, projection_version,
                       tool_details_expanded, updated_at
                FROM user_view_state
                WHERE user_id = ? AND workspace_id = ? AND session_id = ?
                """,
                (user_id, workspace_id, session_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("用户视图状态写入后无法读取")
            connection.execute("COMMIT")
            return self._record(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
