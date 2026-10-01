"""通信 ledger 只读路径:取行 helper 与状态索引/单条读取方法族。"""

from __future__ import annotations

import sqlite3

from .records import CommunicationInboxRecord, _communication_inbox_from_row
from .schema import _COMMUNICATION_INBOX_COLUMNS, _COMMUNICATION_OUTBOX_COLUMNS


def _fetch_inbox_row(
    connection: sqlite3.Connection, communication_id: str
) -> sqlite3.Row | None:
    """按 communication_id 取 inbox 行投影；缺失返回 None。

    inbox 的十余处判态/回读共用本查询，避免同一 SELECT 在多处复制；
    各调用点仍各自决定缺失时的错误文案与分支。
    """
    return connection.execute(
        f"SELECT {_COMMUNICATION_INBOX_COLUMNS} "
        "FROM communication_inbox WHERE communication_id = ?",
        (communication_id,),
    ).fetchone()


def _fetch_outbox_row_by_operation(
    connection: sqlite3.Connection, send_operation_id: str
) -> sqlite3.Row | None:
    """按 send_operation_id（operation 层 PK）取 outbox 行；缺失返回 None。"""
    return connection.execute(
        f"SELECT {_COMMUNICATION_OUTBOX_COLUMNS} "
        "FROM communication_outbox WHERE send_operation_id = ?",
        (send_operation_id,),
    ).fetchone()


def _fetch_outbox_row_by_communication(
    connection: sqlite3.Connection, communication_id: str
) -> sqlite3.Row | None:
    """按 communication_id（communication 层 UNIQUE）取 outbox 行；缺失返回 None。"""
    return connection.execute(
        f"SELECT {_COMMUNICATION_OUTBOX_COLUMNS} "
        "FROM communication_outbox WHERE communication_id = ?",
        (communication_id,),
    ).fetchone()



# reply 因果证明字段表：(本库行取值的列, 本次对应端点)。被回复行方向必须相反。
_OUTBOX_REPLY_DIRECTION_FIELDS = (
    ("source_gateway_id", "target_gateway_id"),
    ("source_workspace_id", "target_workspace_id"),
    ("source_thread_id", "target_thread_id"),
    ("session_id", "session_id"),
    ("target_thread_id", "source_thread_id"),
)
_INBOX_REPLY_DIRECTION_FIELDS = (
    ("target_gateway_id", "source_gateway_id"),
    ("target_workspace_id", "source_workspace_id"),
    ("target_session_id", "source_session_id"),
    ("target_thread_id", "source_thread_id"),
    ("session_id", "session_id"),
    ("source_thread_id", "target_thread_id"),
)


def _ensure_reply_direction(
    row: sqlite3.Row,
    fields: tuple[tuple[str, str], ...],
    endpoints: dict[str, str],
    *,
    session_id: str,
    reply_to_communication_id: str,
    mismatch_message: str,
) -> None:
    """逐字段核对被回复行方向必须与本次相反；任一不符 fail closed。"""
    if not all(str(row[column]) == endpoints[expected] for column, expected in fields):
        raise RuntimeError(
            f"{mismatch_message}: session_id={session_id!r}, "
            f"reply_to={reply_to_communication_id!r}"
        )


class ReadsMixin:
    """通信 ledger 只读路径:取行 helper 与状态索引/单条读取方法族。"""

    def list_target_accepted_communication_inboxes(
        self,
    ) -> tuple[CommunicationInboxRecord, ...]:
        """worker 恢复用状态索引：只查 target_accepted（不扫目录）。"""
        self._ensure_open()
        rows = self._connection.execute(
            f"SELECT {_COMMUNICATION_INBOX_COLUMNS} "
            "FROM communication_inbox WHERE state = 'target_accepted' "
            "ORDER BY created_at, communication_id"
        ).fetchall()
        return tuple(_communication_inbox_from_row(row) for row in rows)

    def get_communication_inbox(
        self,
        communication_id: str,
    ) -> CommunicationInboxRecord:
        """读取单条 inbox 投影；不存在抛 KeyError。"""
        self._ensure_open()
        row = _fetch_inbox_row(self._connection, communication_id)
        if row is None:
            raise KeyError(
                "communication inbox 不存在: "
                f"communication_id={communication_id!r}"
            )
        return _communication_inbox_from_row(row)
