"""精确 Thread 邮箱消息与消费 checkpoint/ack 的唯一 SQLite owner。"""

from __future__ import annotations

import json
import sqlite3

from app.core.background_task_contracts import (
    BackgroundMessageBatchDTO,
    BackgroundMessageDTO,
    BackgroundToolResultIdentity,
)
from app.core.session_control_thread_catalog.thread_catalog import read_fence_row
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.domain.itemized.hashing import sha256_jcs

BACKGROUND_MAILBOX_DDL = """
CREATE TABLE IF NOT EXISTS background_mailbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    message_json TEXT NOT NULL
)
"""
BACKGROUND_MAILBOX_OWNER_DDL = """
CREATE INDEX IF NOT EXISTS idx_background_mailbox_owner
ON background_mailbox(thread_id,agent_id,sequence)
"""
BACKGROUND_CONSUMPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS background_consumptions (
    consumption_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES thread_catalog(thread_id),
    agent_id TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    batch_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1))
)
"""


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
        cursor = 0
        if after_message_id is not None:
            row = self._connection.execute(
                "SELECT sequence FROM background_mailbox "
                "WHERE message_id=? AND thread_id=? AND agent_id=?",
                (after_message_id, thread_id, agent_id),
            ).fetchone()
            if row is None:
                raise ValueError("after_message_id 不属于指定 Thread 邮箱")
            cursor = row["sequence"]
        rows = self._connection.execute(
            "SELECT message_json FROM background_mailbox "
            "WHERE thread_id=? AND agent_id=? AND sequence>? "
            + ("AND source_id=? " if source_id is not None else "")
            + "ORDER BY sequence LIMIT ?",
            (thread_id, agent_id, cursor, *([source_id] if source_id is not None else []), limit),
        ).fetchall()
        return [BackgroundMessageDTO.model_validate_json(row["message_json"]) for row in rows]

    def checkpoint_background_consumption(
        self, batch: BackgroundMessageBatchDTO, *, preimage_hash: str,
    ) -> BackgroundMessageBatchDTO:
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM background_consumptions WHERE consumption_id=?",
                (batch.consumption_id,),
            ).fetchone()
            if row is not None:
                if row["preimage_hash"] != preimage_hash or (row["thread_id"], row["agent_id"]) != (batch.thread_id, batch.agent_id):
                    raise RuntimeError("邮箱 consumption_id 归属或请求内容冲突")
                previous = BackgroundMessageBatchDTO.model_validate_json(row["batch_json"])
                if previous.interrupted or previous.timed_out or row["acknowledged"]:
                    return previous
                if [message.message_id for message in batch.messages[:len(previous.messages)]] != [message.message_id for message in previous.messages]:
                    raise RuntimeError("邮箱消费 checkpoint 不能回退或替换已收集消息")
                connection.execute(
                    "UPDATE background_consumptions SET batch_json=? WHERE consumption_id=?",
                    (batch.model_dump_json(), batch.consumption_id),
                )
            else:
                connection.execute(
                    "INSERT INTO background_consumptions "
                    "(consumption_id,thread_id,agent_id,preimage_hash,batch_json) VALUES (?,?,?,?,?)",
                    (batch.consumption_id, batch.thread_id, batch.agent_id, preimage_hash, batch.model_dump_json()),
                )
            return batch

    def get_background_consumption(
        self, consumption_id: str, *, preimage_hash: str,
    ) -> BackgroundMessageBatchDTO | None:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM background_consumptions WHERE consumption_id=?", (consumption_id,),
        ).fetchone()
        if row is None:
            return None
        if row["preimage_hash"] != preimage_hash:
            raise RuntimeError("邮箱 consumption_id 请求内容冲突")
        return BackgroundMessageBatchDTO.model_validate_json(row["batch_json"])

    def unacknowledged_background_consumptions(self) -> list[tuple[str, str, BackgroundToolResultIdentity]]:
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT consumption_id,thread_id,json_extract(batch_json,'$.tool_result_identity') "
            "FROM background_consumptions WHERE acknowledged=0 "
            "AND json_extract(batch_json,'$.tool_result_identity') IS NOT NULL "
            "AND (json_extract(batch_json,'$.interrupted')=1 OR json_extract(batch_json,'$.timed_out')=1)"
        ).fetchall()
        return [(row[0], row[1], BackgroundToolResultIdentity.model_validate_json(row[2])) for row in rows]

    def acknowledge_background_consumption(self, consumption_id: str, *, if_present: bool = False) -> None:
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT batch_json FROM background_consumptions WHERE consumption_id=?",
                (consumption_id,),
            ).fetchone()
            if row is None:
                if if_present:
                    return
                raise KeyError(consumption_id)
            batch = json.loads(row["batch_json"])
            if not (batch["interrupted"] or batch["timed_out"]):
                raise RuntimeError("邮箱消费尚未 durable 完成，拒绝 ack")
            connection.execute(
                "UPDATE background_consumptions SET acknowledged=1 WHERE consumption_id=?",
                (consumption_id,),
            )
