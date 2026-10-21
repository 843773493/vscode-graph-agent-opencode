"""后台任务的 durable descriptor/checkpoint 与领取 fencing 唯一 writer。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from app.core.background_task_contracts import (
    ACTIVE_TASK_STATUSES,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStatus,
    BackgroundTaskStep,
)
from app.core.session_control_thread_catalog.thread_catalog import read_fence_row
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.sqlite_state import load_sql

BACKGROUND_TASKS_DDL = load_sql(__package__, "sql/background_tasks.sql")
BACKGROUND_TASKS_ACTIVE_DDL = load_sql(__package__, "sql/idx_background_tasks_active.sql")


def background_task_from_row(row: sqlite3.Row) -> BackgroundTaskHandle:
    return BackgroundTaskHandle(
        task_id=row["task_id"], session_id=row["session_id"],
        thread_id=row["thread_id"], agent_id=row["agent_id"],
        descriptor=BackgroundTaskDescriptor.model_validate_json(row["descriptor_json"]),
        checkpoint=json.loads(row["checkpoint_json"]),
        status=cast(BackgroundTaskStatus, row["status"]),
        lease_id=row["lease_id"], fencing_token=row["fencing_token"],
        runtime_generation=row["runtime_generation"],
        external_effect_pending=bool(row["external_effect_pending"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        metadata=json.loads(row["metadata_json"]),
    )


class BackgroundWorkMixin:
    """仅依赖宿主连接、事务与 mailbox 同事务插入方法；不持有运行实体。"""

    def create_background_task(
        self, *, task_id: str, session_id: str, thread_id: str, agent_id: str,
        descriptor: BackgroundTaskDescriptor, checkpoint: dict[str, JsonValue], preimage_hash: str,
    ) -> BackgroundTaskHandle:
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if existing is not None:
                if existing["preimage_hash"] != preimage_hash:
                    raise RuntimeError(f"后台任务 identity 的 descriptor 冲突: {task_id}")
                return background_task_from_row(existing)
            fence = read_fence_row(connection, database_path=self.database_path)
            if fence["state"] != "active":
                raise SessionDeletionPendingError("Session 删除中，拒绝创建后台任务")
            lease = self.create_or_get_lease(
                operation_kind="runtime_owner", operation_identity=task_id,
                preimage_hash=preimage_hash, recovery_ref=f"background_task:{task_id}",
                transaction_connection=connection,
            )
            if lease.state != "active" or lease.captured_lifecycle_generation != fence["generation"]:
                raise RuntimeError("后台任务创建缺少当前 lifecycle lease")
            connection.execute(
                "INSERT INTO background_tasks "
                "(task_id,session_id,thread_id,agent_id,descriptor_json,preimage_hash,"
                "checkpoint_json,status,lease_id,created_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                (task_id, session_id, thread_id, agent_id,
                 descriptor.model_dump_json(), preimage_hash,
                 json.dumps(checkpoint, ensure_ascii=False), lease.lease_id,
                 lease.created_at, json.dumps(descriptor.payload, ensure_ascii=False)),
            )
            return background_task_from_row(connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone())

    def get_background_task(self, task_id: str) -> BackgroundTaskHandle | None:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        return background_task_from_row(row) if row is not None else None

    def list_background_tasks(self, *, thread_id: str | None = None, active_only: bool = False) -> list[BackgroundTaskHandle]:
        self._ensure_open()
        conditions = []
        if thread_id is not None:
            conditions.append("thread_id=?")
        if active_only:
            conditions.append("status IN ('pending','running','reconcile_required')")
        rows = self._connection.execute(
            "SELECT * FROM background_tasks "
            + ("WHERE " + " AND ".join(conditions) + " " if conditions else "")
            + "ORDER BY created_at,task_id",
            (thread_id,) if thread_id is not None else (),
        ).fetchall()
        return [background_task_from_row(row) for row in rows]

    def claim_background_task(
        self, task_id: str, *, expected_token: int, runtime_generation: int,
        reconcile: bool = False,
    ) -> BackgroundTaskHandle:
        with self._write_transaction() as connection:
            fence = read_fence_row(connection, database_path=self.database_path)
            if fence["state"] != "active":
                raise SessionDeletionPendingError("Session 删除中，拒绝领取后台任务")
            updated = connection.execute(
                "UPDATE background_tasks SET fencing_token=fencing_token+1,"
                "runtime_generation=?,status=?,started_at=COALESCE(started_at,?) "
                "WHERE task_id=? AND fencing_token=? AND status IN (?,?) "
                "AND external_effect_pending=?",
                (runtime_generation, "reconcile_required" if reconcile else "running",
                 datetime.now(UTC).isoformat(), task_id, expected_token,
                 "reconcile_required" if reconcile else "pending",
                 "reconcile_required" if reconcile else "running", int(reconcile)),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"后台任务领取 CAS 失败: {task_id}, token={expected_token}")
            return background_task_from_row(connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone())

    def mark_background_external_effect(self, task_id: str, *, token: int) -> None:
        with self._write_transaction() as connection:
            updated = connection.execute(
                "UPDATE background_tasks SET external_effect_pending=1 "
                "WHERE task_id=? AND fencing_token=? AND status='running' "
                "AND external_effect_pending=0", (task_id, token),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"后台任务外部效果 fencing 失败: {task_id}")

    def commit_background_step(
        self, task_id: str, *, token: int, step: BackgroundTaskStep,
        reconcile: bool = False,
    ) -> BackgroundTaskHandle:
        with self._write_transaction() as connection:
            fence = read_fence_row(connection, database_path=self.database_path)
            if fence["state"] != "active":
                raise SessionDeletionPendingError("Session 删除中，拒绝后台回调写入")
            row = connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=? AND fencing_token=? "
                "AND status=?", (task_id, token, "reconcile_required" if reconcile else "running"),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"后台任务 checkpoint fencing 失败: {task_id}, token={token}")
            for message in step.messages:
                if (message.session_id, message.thread_id, message.agent_id) != (
                    row["session_id"], row["thread_id"], row["agent_id"],
                ):
                    raise RuntimeError("后台任务消息不能越过精确 thread/agent 归属")
                self._insert_background_message(connection, message)
            metadata = json.loads(row["metadata_json"])
            if step.result is not None:
                result = dict(step.result)
                result_messages = []
                for message_id in step.result_message_ids:
                    message = connection.execute(
                        "SELECT message_json FROM background_mailbox WHERE message_id=? "
                        "AND thread_id=? AND agent_id=?",
                        (message_id, row["thread_id"], row["agent_id"]),
                    ).fetchone()
                    if message is None:
                        raise RuntimeError(f"后台完成回执缺少已提交消息: {message_id}")
                    result_messages.append(json.loads(message["message_json"]))
                if step.result_message_ids:
                    result["messages"] = result_messages
                metadata["result"] = result
            connection.execute(
                "UPDATE background_tasks SET checkpoint_json=?,external_effect_pending=0,"
                "status=?,ended_at=?,metadata_json=? WHERE task_id=? AND fencing_token=?",
                (json.dumps(step.checkpoint, ensure_ascii=False),
                 "completed" if step.completed else "running",
                 datetime.now(UTC).isoformat() if step.completed else None,
                 json.dumps(metadata, ensure_ascii=False), task_id, token),
            )
            return background_task_from_row(connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone())

    def finish_background_task(
        self, task_id: str, *, token: int, status: BackgroundTaskStatus,
        note: str | None = None,
    ) -> BackgroundTaskHandle:
        if status not in {"pending", "reconcile_required", "cancelled", "failed", "deleted"}:
            raise ValueError(f"后台任务终结状态非法: {status}")
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["fencing_token"] != token:
                raise RuntimeError(f"后台任务终结 fencing 失败: {task_id}")
            if row["status"] not in ACTIVE_TASK_STATUSES and status != "deleted":
                return background_task_from_row(row)
            metadata = json.loads(row["metadata_json"])
            if note is not None:
                metadata["status_note"] = note
            connection.execute(
                "UPDATE background_tasks SET status=?,ended_at=?,metadata_json=? "
                "WHERE task_id=? AND fencing_token=?",
                (status, None if status in ACTIVE_TASK_STATUSES else datetime.now(UTC).isoformat(),
                 json.dumps(metadata, ensure_ascii=False), task_id, token),
            )
            return background_task_from_row(connection.execute(
                "SELECT * FROM background_tasks WHERE task_id=?", (task_id,),
            ).fetchone())
