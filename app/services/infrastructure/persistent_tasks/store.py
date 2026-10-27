"""工作区持久任务账本；定义版本不可变，运行不复制定义或上下文事实。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime

from app.abstractions.state_conflict import ClientStateConflictError
from app.core.identifier import create_prefixed_id
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskConfigurationDTO,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskRunDTO,
    TaskDecisionDTO,
    TaskDeliveryDTO,
    TaskLifecycle,
    TaskRunPhase,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class PersistentTaskStore:
    def __init__(self, store: WorkspaceStateStore, *, workspace_id: str) -> None:
        self._store = store
        self.workspace_id = workspace_id

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._store.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _task(self, connection: sqlite3.Connection, task_id: str) -> PersistentTaskDTO:
        row = connection.execute(
            "SELECT t.*,d.configuration_json FROM persistent_tasks t JOIN persistent_task_definitions d "
            "ON d.task_id=t.task_id AND d.definition_revision=t.definition_revision "
            "WHERE t.task_id=? AND t.workspace_id=?", (task_id, self.workspace_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"持久任务不存在: {task_id}")
        return PersistentTaskDTO(
            task_id=row["task_id"], workspace_id=row["workspace_id"], revision=row["revision"],
            definition_revision=row["definition_revision"], lifecycle=row["lifecycle"],
            configuration=PersistentTaskConfigurationDTO.model_validate_json(row["configuration_json"]),
            phase="waiting_trigger" if row["lifecycle"] == "active" else row["lifecycle"],
            reason=row["reason"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get(self, task_id: str) -> PersistentTaskDTO:
        with closing(self._store.connection()) as connection:
            return self._task(connection, task_id)

    def list(self) -> list[PersistentTaskDTO]:
        with closing(self._store.connection()) as connection:
            return [self._task(connection, row[0]) for row in connection.execute(
                "SELECT task_id FROM persistent_tasks WHERE workspace_id=? ORDER BY created_at,task_id", (self.workspace_id,),
            ).fetchall()]

    def create(self, request: PersistentTaskCreateRequest, now: datetime) -> PersistentTaskDTO:
        preimage = sha256_jcs(request.model_dump(mode="json"))
        with self._transaction() as connection:
            row = connection.execute("SELECT task_id,creation_hash FROM persistent_tasks WHERE creation_key=?",
                                     (request.idempotency_key,)).fetchone()
            if row is not None:
                if row[1] != preimage:
                    raise ClientStateConflictError("持久任务创建幂等键对应不同内容")
                return self._task(connection, row[0])
            task_id = create_prefixed_id("ptask")
            connection.execute("INSERT INTO persistent_tasks VALUES (?,?,?,?,1,1,?,NULL,?,?)", (
                task_id, self.workspace_id, request.idempotency_key, preimage,
                "active" if request.enabled else "paused", now.isoformat(), now.isoformat(),
            ))
            connection.execute("INSERT INTO persistent_task_definitions VALUES (?,1,?)",
                               (task_id, request.configuration.model_dump_json()))
            return self._task(connection, task_id)

    @staticmethod
    def _require_revision(task: PersistentTaskDTO, expected_revision: int) -> None:
        if task.revision != expected_revision:
            raise ClientStateConflictError(f"持久任务 revision 已变化: expected={expected_revision}, actual={task.revision}")
        if task.lifecycle in {"terminated", "complete"}:
            raise ClientStateConflictError("持久任务已终结")

    def update(self, task_id: str, *, expected_revision: int, configuration: PersistentTaskConfigurationDTO,
               now: datetime) -> PersistentTaskDTO:
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            self._require_revision(task, expected_revision)
            revision = task.definition_revision + 1
            connection.execute("INSERT INTO persistent_task_definitions VALUES (?,?,?)",
                               (task_id, revision, configuration.model_dump_json()))
            connection.execute("UPDATE persistent_tasks SET revision=revision+1,definition_revision=?,updated_at=? WHERE task_id=?",
                               (revision, now.isoformat(), task_id))
            return self._task(connection, task_id)

    def control(self, task_id: str, *, expected_revision: int, lifecycle: TaskLifecycle,
                now: datetime, reason: str | None = None) -> PersistentTaskDTO:
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            self._require_revision(task, expected_revision)
            connection.execute("UPDATE persistent_tasks SET revision=revision+1,lifecycle=?,reason=?,updated_at=? WHERE task_id=?",
                               (lifecycle, reason, now.isoformat(), task_id))
            return self._task(connection, task_id)

    @staticmethod
    def _run(row: sqlite3.Row) -> PersistentTaskRunDTO:
        return PersistentTaskRunDTO(
            run_id=row["run_id"], task_id=row["task_id"], task_revision=row["task_revision"],
            definition_revision=row["definition_revision"], trigger_key=row["trigger_key"], check_only=bool(row["check_only"]),
            request_id=row["request_id"],
            phase=row["phase"], decision=TaskDecisionDTO.model_validate_json(row["decision_json"]) if row["decision_json"] else None,
            delivery=TaskDeliveryDTO.model_validate_json(row["delivery_json"]) if row["delivery_json"] else None,
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def runs(self, task_id: str, *, pending_only: bool = False) -> list[PersistentTaskRunDTO]:
        with closing(self._store.connection()) as connection:
            self._task(connection, task_id)
            condition = "AND phase IN ('pending','checking','delivering')" if pending_only else ""
            rows = connection.execute(f"SELECT * FROM persistent_task_runs WHERE task_id=? {condition} ORDER BY created_at,run_id",
                                      (task_id,)).fetchall()
            return [self._run(row) for row in rows]

    def trigger(self, task_id: str, request: PersistentTaskCheckRequest, now: datetime, *,
                request_id: str | None) -> PersistentTaskRunDTO:
        preimage = sha256_jcs(request.model_dump(mode="json"))
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            row = connection.execute("SELECT * FROM persistent_task_runs WHERE task_id=? AND trigger_key=?",
                                     (task_id, request.idempotency_key)).fetchone()
            if row is not None:
                if row["trigger_hash"] != preimage:
                    raise ClientStateConflictError("持久任务触发幂等键对应不同输入")
                return self._run(row)
            self._require_revision(task, request.expected_revision)
            if task.lifecycle != "active" and not request.check_only:
                raise ClientStateConflictError("只有 active 持久任务可接受新动作")
            run_id = create_prefixed_id("ptrun")
            connection.execute("INSERT INTO persistent_task_runs VALUES (?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,?,?)", (
                run_id, task_id, task.revision, task.definition_revision, request.idempotency_key, preimage,
                request_id, int(request.check_only), now.isoformat(), now.isoformat(),
            ))
            return self._run(connection.execute("SELECT * FROM persistent_task_runs WHERE run_id=?", (run_id,)).fetchone())

    def configuration(self, run: PersistentTaskRunDTO) -> PersistentTaskConfigurationDTO:
        with closing(self._store.connection()) as connection:
            row = connection.execute("SELECT configuration_json FROM persistent_task_definitions WHERE task_id=? AND definition_revision=?",
                                     (run.task_id, run.definition_revision)).fetchone()
        if row is None:
            raise RuntimeError("持久任务运行引用的定义版本缺失")
        return PersistentTaskConfigurationDTO.model_validate_json(row[0])

    def advance(self, run: PersistentTaskRunDTO, *, phase: TaskRunPhase, now: datetime,
                decision: TaskDecisionDTO | None = None, delivery: TaskDeliveryDTO | None = None,
                error: str | None = None, lifecycle: TaskLifecycle | None = None) -> None:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE persistent_task_runs SET phase=?,decision_json=COALESCE(?,decision_json),delivery_json=COALESCE(?,delivery_json),"
                "error=?,updated_at=? WHERE run_id=? AND phase=?", (
                    phase, decision.model_dump_json() if decision else None, delivery.model_dump_json() if delivery else None,
                    error, now.isoformat(), run.run_id, run.phase,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("持久任务运行阶段发生并发冲突")
            if lifecycle is not None:
                connection.execute("UPDATE persistent_tasks SET lifecycle=?,revision=revision+1,reason=?,updated_at=? "
                                   "WHERE task_id=? AND revision=? AND lifecycle='active'",
                                   (lifecycle, error, now.isoformat(), run.task_id, run.task_revision))
