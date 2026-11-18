"""精确 Thread 邮箱消息与消费 checkpoint/ack 的唯一 SQLite owner。"""

from __future__ import annotations

import sqlite3
from time import monotonic

from app.core.session_control_thread_catalog.thread_catalog import read_fence_row
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.sqlite_state import load_sql
from app.domain.background_work import (
    BackgroundConsumptionState,
    BackgroundMessageBatchDTO,
    BackgroundMessageBatchMetadata,
    BackgroundMessageDTO,
    BackgroundToolResultIdentity,
)
from app.domain.itemized.hashing import sha256_jcs

BACKGROUND_MAILBOX_DDL = load_sql(__package__, "migrations/background_mailbox.sql")
BACKGROUND_MAILBOX_OWNER_DDL = load_sql(__package__, "migrations/idx_background_mailbox_owner.sql")
BACKGROUND_CONSUMPTIONS_DDL = load_sql(__package__, "migrations/background_consumptions.sql")


class BackgroundMailboxMixin:
    """只依赖宿主连接与唯一 _write_transaction，不保存进程内消息镜像。"""

    @staticmethod
    def _insert_background_message(
        connection: sqlite3.Connection, message: BackgroundMessageDTO,
    ) -> BackgroundMessageDTO:
        preimage = message.model_dump(mode="json", exclude={"timestamp"})
        digest = sha256_jcs(preimage)
        row = connection.execute(
            "SELECT * FROM background_mailbox WHERE message_id=?", (message.message_id,),
        ).fetchone()
        if row is not None:
            if row["preimage_hash"] != digest:
                raise RuntimeError(f"邮箱 message_id 内容冲突: {message.message_id}")
            return BackgroundMessageDTO.model_validate_json(row["message_json"])
        connection.execute(
            "INSERT INTO background_mailbox "
            "(message_id,session_id,thread_id,agent_id,source_id,preimage_hash,message_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (message.message_id, message.session_id, message.thread_id, message.agent_id,
             message.source_id, digest, message.model_dump_json()),
        )
        return message

    def emit_background_message(self, message: BackgroundMessageDTO) -> BackgroundMessageDTO:
        with self._write_transaction() as connection:
            fence = read_fence_row(connection, database_path=self.database_path)
            if fence["state"] != "active":
                raise SessionDeletionPendingError("Session 删除中，拒绝邮箱写入")
            return self._insert_background_message(connection, message)

    def list_background_messages(
        self, thread_id: str, agent_id: str, *,
        source_id: str | None = None, after_message_id: str | None = None, limit: int = 1000,
    ) -> list[BackgroundMessageDTO]:
        self._ensure_open()
        if not 1 <= limit <= 1000:
            raise ValueError("邮箱 limit 必须位于 1..1000")
        cursor = _message_sequence(self._connection, thread_id, agent_id, after_message_id)
        rows = self._connection.execute(
            "SELECT message_json FROM background_mailbox "
            "WHERE thread_id=? AND agent_id=? AND sequence>? "
            + ("AND source_id=? " if source_id is not None else "")
            + "ORDER BY sequence LIMIT ?",
            (thread_id, agent_id, cursor, *([source_id] if source_id is not None else []), limit),
        ).fetchall()
        return [BackgroundMessageDTO.model_validate_json(row["message_json"]) for row in rows]

    def open_background_consumption(
        self, batch: BackgroundMessageBatchMetadata, *, preimage_hash: str,
    ) -> BackgroundConsumptionState:
        with self._write_transaction() as connection:
            existing = _consumption_state(connection, batch.consumption_id, preimage_hash)
            if existing is not None:
                return existing
            cursor = _message_sequence(connection, batch.thread_id, batch.agent_id, batch.last_message_id)
            connection.execute(
                "INSERT INTO background_consumptions "
                "(consumption_id,session_id,thread_id,agent_id,preimage_hash,source_id,"
                "after_sequence,through_sequence,message_count,interrupted,timed_out,collected_at,tool_result_identity_json) "
                "VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?)",
                (batch.consumption_id, batch.session_id, batch.thread_id, batch.agent_id,
                 preimage_hash, batch.source_id, cursor, cursor, batch.collected_at.isoformat(),
                 batch.tool_result_identity.model_dump_json() if batch.tool_result_identity else None),
            )
            return BackgroundConsumptionState(**batch.model_dump(), after_sequence=cursor,
                                              through_sequence=cursor, message_count=0)

    def advance_background_consumption(
        self, consumption_id: str, *, preimage_hash: str, deadline_at: float, stop_on_interrupt: bool,
    ) -> tuple[BackgroundConsumptionState, bool]:
        """按原邮箱有界推进消费范围；没有新消息或终态变化时不写盘。"""
        with self._write_transaction() as connection:
            state = _consumption_state(connection, consumption_id, preimage_hash)
            if state is None:
                raise KeyError(consumption_id)
            if state.interrupted or state.timed_out:
                return state, True
            rows = connection.execute(
                "SELECT sequence,message_json FROM background_mailbox "
                "WHERE thread_id=? AND agent_id=? AND sequence>? "
                + ("AND source_id=? " if state.source_id is not None else "")
                + "ORDER BY sequence LIMIT 1000",
                (state.thread_id, state.agent_id, state.through_sequence,
                 *([state.source_id] if state.source_id is not None else [])),
            ).fetchall()
            through = state.through_sequence
            last_id = state.last_message_id
            count = state.message_count
            interrupted = False
            for row in rows:
                message = BackgroundMessageDTO.model_validate_json(row["message_json"])
                through, last_id, count = row["sequence"], message.message_id, count + 1
                if stop_on_interrupt and message.kind.value == "interrupt":
                    interrupted = True
                    break
            timed_out = monotonic() >= deadline_at and not interrupted
            if through != state.through_sequence or interrupted or timed_out:
                connection.execute(
                    "UPDATE background_consumptions SET through_sequence=?,message_count=?,interrupted=?,timed_out=? "
                    "WHERE consumption_id=?",
                    (through, count, int(interrupted), int(timed_out), consumption_id),
                )
            return state.model_copy(update={"through_sequence": through, "last_message_id": last_id,
                "message_count": count, "interrupted": interrupted, "timed_out": timed_out}), len(rows) < 1000

    def get_background_consumption(
        self, consumption_id: str, *, preimage_hash: str,
    ) -> BackgroundMessageBatchDTO:
        self._ensure_open()
        state = _consumption_state(self._connection, consumption_id, preimage_hash)
        if state is None:
            raise KeyError(consumption_id)
        rows = self._connection.execute(
            "SELECT message_json FROM background_mailbox "
            "WHERE thread_id=? AND agent_id=? AND sequence>? AND sequence<=? "
            + ("AND source_id=? " if state.source_id is not None else "")
            + "ORDER BY sequence",
            (state.thread_id, state.agent_id, state.after_sequence, state.through_sequence,
             *([state.source_id] if state.source_id is not None else [])),
        ).fetchall()
        if len(rows) != state.message_count:
            raise RuntimeError("邮箱消费冻结范围内的消息数量不一致")
        return BackgroundMessageBatchDTO(
            **state.model_dump(exclude={"after_sequence", "through_sequence", "message_count"}),
            messages=[BackgroundMessageDTO.model_validate_json(row["message_json"]) for row in rows],
        )

    def unacknowledged_background_consumptions(self) -> list[tuple[str, str, BackgroundToolResultIdentity]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT consumption_id,thread_id,tool_result_identity_json "
            "FROM background_consumptions WHERE acknowledged=0 AND tool_result_identity_json IS NOT NULL "
            "AND (interrupted=1 OR timed_out=1)"
        ).fetchall()
        return [(row[0], row[1], BackgroundToolResultIdentity.model_validate_json(row[2])) for row in rows]

    def acknowledge_background_consumption(self, consumption_id: str, *, if_present: bool = False) -> None:
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT interrupted,timed_out FROM background_consumptions WHERE consumption_id=?",
                (consumption_id,),
            ).fetchone()
            if row is None:
                if if_present:
                    return
                raise KeyError(consumption_id)
            if not (row["interrupted"] or row["timed_out"]):
                raise RuntimeError("邮箱消费尚未 durable 完成，拒绝 ack")
            connection.execute(
                "UPDATE background_consumptions SET acknowledged=1 WHERE consumption_id=?",
                (consumption_id,),
            )


def _message_sequence(connection: sqlite3.Connection, thread_id: str, agent_id: str,
                      message_id: str | None) -> int:
    if message_id is None:
        return 0
    row = connection.execute(
        "SELECT sequence FROM background_mailbox WHERE message_id=? AND thread_id=? AND agent_id=?",
        (message_id, thread_id, agent_id),
    ).fetchone()
    if row is None:
        raise ValueError("after_message_id 不属于指定 Thread 邮箱")
    return row["sequence"]


def _consumption_state(connection: sqlite3.Connection, consumption_id: str,
                       preimage_hash: str) -> BackgroundConsumptionState | None:
    row = connection.execute(
        "SELECT c.*,m.message_id AS last_message_id FROM background_consumptions c "
        "LEFT JOIN background_mailbox m ON m.sequence=c.through_sequence "
        "AND m.thread_id=c.thread_id AND m.agent_id=c.agent_id WHERE c.consumption_id=?",
        (consumption_id,),
    ).fetchone()
    if row is None:
        return None
    if row["preimage_hash"] != preimage_hash:
        raise RuntimeError("邮箱 consumption_id 请求内容冲突")
    if row["through_sequence"] and row["last_message_id"] is None:
        raise RuntimeError("邮箱消费的冻结消息边界缺失或归属不一致")
    return BackgroundConsumptionState(
        session_id=row["session_id"], thread_id=row["thread_id"], agent_id=row["agent_id"],
        consumption_id=consumption_id, source_id=row["source_id"], last_message_id=row["last_message_id"],
        interrupted=bool(row["interrupted"]), timed_out=bool(row["timed_out"]), collected_at=row["collected_at"],
        tool_result_identity=BackgroundToolResultIdentity.model_validate_json(row["tool_result_identity_json"])
        if row["tool_result_identity_json"] is not None else None,
        after_sequence=row["after_sequence"], through_sequence=row["through_sequence"], message_count=row["message_count"],
    )
