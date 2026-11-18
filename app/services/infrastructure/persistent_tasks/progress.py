"""任务执行归属与用量水位，复用原 application_json，不复制执行正文或终态。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING

from app.schemas.internal_v2.task_usage import (
    TaskDispatchDTO,
    TaskExecutionReferenceDTO,
    TaskJobAccountingDTO,
    TaskUsageStateDTO,
)
from app.services.business.persistent_tasks.usage import account_usage, budget_reached
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


def task_application(row: sqlite3.Row) -> dict[str, object]:
    # automation 原本不需要应用状态，SQL NULL 明确表示尚无用量或投递水位。
    return {} if row["application_json"] is None else json.loads(row["application_json"])


class TaskProgress:
    _store: WorkspaceStateStore
    workspace_id: str

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def _progress_row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM persistent_tasks WHERE task_id=? AND workspace_id=? AND retired_at IS NULL",
                                 (task_id, self.workspace_id)).fetchone()
        if row is None:
            raise KeyError(f"持久任务不存在: {task_id}")
        return row

    def usage_state(self, task_id: str) -> TaskUsageStateDTO:
        with closing(self._store.connection()) as connection:
            return TaskUsageStateDTO.model_validate(task_application(self._progress_row(connection, task_id)))

    @staticmethod
    def _write_progress(connection: sqlite3.Connection, row: sqlite3.Row, state: TaskUsageStateDTO, now: datetime) -> None:
        # Goal 的特定规则与通用水位共处原 owner，不能用通用模型覆盖其它应用字段。
        application = {**task_application(row), **state.model_dump(mode="json"),
                       "sync_version": state.sync_version + 1}
        connection.execute("UPDATE persistent_tasks SET application_json=?,updated_at=? WHERE task_id=?",
                           (json.dumps(application, ensure_ascii=False), now.isoformat(), row["task_id"]))

    def bind_execution(self, task_id: str, job_id: str, execution: TaskExecutionReferenceDTO, now: datetime) -> None:
        with self._transaction() as connection:
            row = self._progress_row(connection, task_id)
            state = TaskUsageStateDTO.model_validate(task_application(row))
            previous = state.accounted_jobs.get(job_id, TaskJobAccountingDTO())
            if previous.execution is not None:
                if previous.execution != execution:
                    raise RuntimeError("任务执行归属与原准入引用冲突")
                return
            state.accounted_jobs[job_id] = previous.model_copy(update={"execution": execution})
            self._write_progress(connection, row, state, now)

    def execution_owners(self, job_id: str) -> list[str]:
        with closing(self._store.connection()) as connection:
            return [row[0] for row in connection.execute(
                "SELECT t.task_id FROM persistent_tasks t,json_each(t.application_json,'$.accounted_jobs') job "
                "WHERE t.workspace_id=? AND t.retired_at IS NULL AND job.key=? "
                "AND json_extract(job.value,'$.execution') IS NOT NULL", (self.workspace_id, job_id))]

    def record_dispatch(self, task_id: str, dispatch: TaskDispatchDTO, now: datetime) -> None:
        with self._transaction() as connection:
            row = self._progress_row(connection, task_id)
            state = TaskUsageStateDTO.model_validate(task_application(row))
            state.dispatch = dispatch
            self._write_progress(connection, row, state, now)

    def account_execution(self, task_id: str, *, job_id: str, tokens: int, elapsed_seconds: int,
                          settled: bool, now: datetime) -> bool:
        with self._transaction() as connection:
            row = self._progress_row(connection, task_id)
            if row["application_type"] != "automation":
                raise ValueError("Goal 结算必须应用原 Goal 状态规则")
            state = TaskUsageStateDTO.model_validate(task_application(row))
            if job_id not in state.accounted_jobs or state.accounted_jobs[job_id].execution is None:
                raise RuntimeError("任务用量缺少原执行归属")
            current = account_usage(state, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds,
                                    close_time=settled, settled=settled)
            if current == state:
                return False
            self._write_progress(connection, row, current, now)
            definition = connection.execute("SELECT configuration_json FROM persistent_task_definitions WHERE task_id=? AND definition_revision=?",
                                            (task_id, row["definition_revision"])).fetchone()
            token_budget = json.loads(definition[0]).get("token_budget")
            # 只阻止下一次准入；暂停、终态及已接受执行不被迟到用量改写。
            if row["lifecycle"] == "active" and budget_reached(current.tokens_used, token_budget):
                connection.execute("UPDATE persistent_tasks SET lifecycle='needs_attention',revision=revision+1,reason=? WHERE task_id=?",
                                   ("任务累计 Token 预算已耗尽", task_id))
            return True
