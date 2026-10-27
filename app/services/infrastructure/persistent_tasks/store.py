"""工作区持久任务账本；定义版本不可变，运行不复制定义或上下文事实。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime

from app.abstractions.state_conflict import ClientStateConflictError
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.time_schedule import evaluate_schedule
from app.domain.itemized.execution_queue import ExecutionCompletionPage
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.persistent_task import (
    AutomationTaskDTO,
    PersistentTaskCheckRequest,
    PersistentTaskConfigurationDTO,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskNotificationRequest,
    PersistentTaskRunDTO,
    TaskDecisionDTO,
    TaskDeliveryDTO,
    TaskEvaluationDTO,
    TaskLifecycle,
    TaskRunPhase,
)
from app.schemas.internal_v2.session_navigation.generation_state import (
    GENERATION_STATE,
    GenerationActionState,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

from .goals import TaskGoals

_INSERT_RUN = (
    "INSERT INTO persistent_task_runs (run_id,task_id,task_revision,definition_revision,trigger_key,trigger_hash,"
    "request_id,check_only,phase,decision_json,delivery_json,error,created_at,updated_at,trigger_type,scheduled_for,trigger_payload_json) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class PersistentTaskStore(TaskGoals):
    def __init__(self, store: WorkspaceStateStore, *, workspace_id: str,
                 paths: SessionCatalogPathResolver | None = None) -> None:
        self._store = store
        self.workspace_id = workspace_id
        self._paths = paths

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._store.connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _task(self, connection: sqlite3.Connection, task_id: str) -> AutomationTaskDTO:
        row = connection.execute(
            "SELECT t.*,d.configuration_json FROM persistent_tasks t LEFT JOIN persistent_task_definitions d "
            "ON d.task_id=t.task_id AND d.definition_revision=t.definition_revision "
            "WHERE t.task_id=? AND t.workspace_id=? AND t.retired_at IS NULL", (task_id, self.workspace_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"持久任务不存在: {task_id}")
        # TODO（6.5）：通用 Goal 管理动作接入既有业务规则，禁止直接修改派生 lifecycle。
        if row["application_type"] != "automation":
            raise ValueError("Goal 任务的修改和执行使用会话 Goal 入口")
        if row["configuration_json"] is None:
            raise RuntimeError("自动化当前任务缺少不可变定义")
        schedule = connection.execute("SELECT next_run_at FROM persistent_task_schedules WHERE task_id=?", (task_id,)).fetchone()
        return AutomationTaskDTO(
            task_id=row["task_id"], workspace_id=row["workspace_id"], revision=row["revision"],
            definition_revision=row["definition_revision"], lifecycle=row["lifecycle"],
            configuration=PersistentTaskConfigurationDTO.model_validate_json(row["configuration_json"]),
            phase="waiting_trigger" if row["lifecycle"] == "active" else row["lifecycle"],
            reason=row["reason"], created_at=row["created_at"], updated_at=row["updated_at"],
            next_run_at=schedule[0] if schedule else None,
        )

    def get_automation(self, task_id: str) -> AutomationTaskDTO:
        with closing(self._store.connection()) as connection:
            return self._task(connection, task_id)

    def list_automations(self) -> list[AutomationTaskDTO]:
        with closing(self._store.connection()) as connection:
            return [self._task(connection, row[0]) for row in connection.execute(
                "SELECT task_id FROM persistent_tasks WHERE workspace_id=? AND application_type='automation' ORDER BY created_at,task_id", (self.workspace_id,),
            ).fetchall()]

    def _view(self, connection: sqlite3.Connection, task_id: str) -> PersistentTaskDTO:
        row = connection.execute("SELECT * FROM persistent_tasks WHERE task_id=? AND workspace_id=? AND retired_at IS NULL",
                                 (task_id, self.workspace_id)).fetchone()
        if row is None:
            raise KeyError(f"持久任务不存在: {task_id}")
        if row["application_type"] == "automation":
            return self._task(connection, task_id)
        paths = self._goal_paths()
        node = paths.catalog_store.get_node(row["owner_session_id"])
        if node.kind != "session" or node.state != "active":
            raise KeyError(f"持久任务尚未发布或所属会话不可用: {task_id}")
        goal = self._goal(connection, self.workspace_id, node.node_id, paths.main_thread_id(node.node_id))
        if goal is None or goal.goal_id != task_id:
            raise RuntimeError("Goal 管理投影与当前任务身份不一致")
        return PersistentTaskDTO(
            task_id=task_id, workspace_id=self.workspace_id, revision=row["revision"],
            definition_revision=row["definition_revision"], application_type="goal", goal=goal,
            lifecycle=row["lifecycle"], phase=goal.status.value,
            created_at=goal.created_at, updated_at=goal.updated_at,
        )

    def get(self, task_id: str) -> PersistentTaskDTO:
        with closing(self._store.connection()) as connection:
            return self._view(connection, task_id)

    def list(self) -> list[PersistentTaskDTO]:
        # preparing/deleting 的可见性只取 catalog，不让任务存储另存发布状态。
        with closing(self._store.connection()) as connection:
            rows = connection.execute("SELECT task_id,application_type,owner_session_id FROM persistent_tasks "
                                      "WHERE workspace_id=? AND retired_at IS NULL ORDER BY created_at,task_id", (self.workspace_id,)).fetchall()
            active = {node.node_id for node in self._goal_paths().list_nodes() if node.kind == "session" and node.state == "active"} if any(row["application_type"] == "goal" for row in rows) else set()
            return [self._view(connection, row["task_id"]) for row in rows
                    if row["application_type"] == "automation" or row["owner_session_id"] in active]

    def create(self, request: PersistentTaskCreateRequest, now: datetime, *, source_cursor: int | None = None) -> PersistentTaskDTO:
        payload = request.model_dump(mode="json")
        if request.configuration.context.mode == "event":
            payload["configuration"]["context"].pop("mode")
        if request.configuration.trigger.source_uri is None:
            payload["configuration"]["trigger"].pop("source_uri")
        # 缺省时间配置不改变既有手动创建请求的幂等身份。
        if request.configuration.trigger.type_id == "builtin.manual":
            for field in ("interval_seconds", "expression", "timezone"):
                payload["configuration"]["trigger"].pop(field)
        if request.configuration.policies.misfire == "skip":
            payload["configuration"]["policies"].pop("misfire")
        if request.configuration.policies.match_mode == "each_match":
            payload["configuration"]["policies"].pop("match_mode")
        if request.configuration.condition.type_id == "builtin.always":
            payload["configuration"]["condition"].pop("script")
        if request.configuration.condition.rule is None:
            payload["configuration"]["condition"].pop("rule")
        if request.configuration.condition.model is None:
            payload["configuration"]["condition"].pop("model")
        preimage = sha256_jcs(payload)
        with self._transaction() as connection:
            row = connection.execute("SELECT task_id,creation_hash FROM persistent_tasks WHERE creation_key=?",
                                     (request.idempotency_key,)).fetchone()
            if row is not None:
                if row[1] != preimage:
                    raise ClientStateConflictError("持久任务创建幂等键对应不同内容")
                return self._task(connection, row[0])
            task_id = create_prefixed_id("ptask")
            connection.execute("INSERT INTO persistent_tasks "
                               "(task_id,workspace_id,creation_key,creation_hash,revision,definition_revision,lifecycle,reason,created_at,updated_at) "
                               "VALUES (?,?,?,?,1,1,?,NULL,?,?)", (
                task_id, self.workspace_id, request.idempotency_key, preimage,
                "active" if request.enabled else "paused", now.isoformat(), now.isoformat(),
            ))
            connection.execute("INSERT INTO persistent_task_definitions VALUES (?,1,?)",
                               (task_id, request.configuration.model_dump_json()))
            self._set_schedule(connection, task_id, request.configuration, 1, now)
            self._set_execution_source(connection, task_id, request.configuration, 1, source_cursor)
            return self._task(connection, task_id)

    @staticmethod
    def _require_revision(task: PersistentTaskDTO, expected_revision: int) -> None:
        if task.revision != expected_revision:
            raise ClientStateConflictError(f"持久任务 revision 已变化: expected={expected_revision}, actual={task.revision}")
        if task.lifecycle in {"terminated", "complete"}:
            raise ClientStateConflictError("持久任务已终结")

    def update(self, task_id: str, *, expected_revision: int, configuration: PersistentTaskConfigurationDTO,
               now: datetime, source_cursor: int | None = None) -> PersistentTaskDTO:
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            self._require_revision(task, expected_revision)
            revision = task.definition_revision + 1
            connection.execute("INSERT INTO persistent_task_definitions VALUES (?,?,?)",
                               (task_id, revision, configuration.model_dump_json()))
            connection.execute("UPDATE persistent_tasks SET revision=revision+1,definition_revision=?,updated_at=? WHERE task_id=?",
                               (revision, now.isoformat(), task_id))
            if task.configuration.trigger != configuration.trigger:
                self._set_schedule(connection, task_id, configuration, revision, now)
            else:
                connection.execute("UPDATE persistent_task_schedules SET definition_revision=? WHERE task_id=?", (revision, task_id))
            if task.configuration.trigger.source_uri != configuration.trigger.source_uri:
                self._set_execution_source(connection, task_id, configuration, revision, source_cursor)
            else:
                connection.execute("UPDATE persistent_task_execution_sources SET definition_revision=? WHERE task_id=?", (revision, task_id))
            return self._task(connection, task_id)

    @staticmethod
    def _set_execution_source(connection: sqlite3.Connection, task_id: str, configuration: PersistentTaskConfigurationDTO,
                              revision: int, cursor: int | None) -> None:
        if configuration.trigger.source_uri is None:
            connection.execute("DELETE FROM persistent_task_execution_sources WHERE task_id=?", (task_id,))
            return
        if cursor is None or cursor < 0:
            raise ValueError("执行结束触发需要明确的来源提交游标")
        previous = connection.execute("SELECT source_uri,after_commit_id FROM persistent_task_execution_sources WHERE task_id=?", (task_id,)).fetchone()
        if previous is not None and previous[0] == configuration.trigger.source_uri and cursor < previous[1]:
            raise RuntimeError("执行结束来源历史已回退，拒绝重新订阅")
        connection.execute("INSERT INTO persistent_task_execution_sources VALUES (?,?,?,?) "
                           "ON CONFLICT(task_id) DO UPDATE SET definition_revision=excluded.definition_revision,"
                           "source_uri=excluded.source_uri,after_commit_id=excluded.after_commit_id",
                           (task_id, revision, configuration.trigger.source_uri, cursor))

    def execution_cursor(self, task_id: str) -> int:
        with closing(self._store.connection()) as connection:
            task = self._task(connection, task_id)
            row = connection.execute("SELECT definition_revision,source_uri,after_commit_id FROM persistent_task_execution_sources WHERE task_id=?",
                                     (task_id,)).fetchone()
            if row is None or tuple(row[:2]) != (task.definition_revision, task.configuration.trigger.source_uri):
                raise RuntimeError("执行结束触发游标与原定义不一致")
            return row[2]

    def accept_execution_completions(self, task_id: str, *, expected_revision: int, after_commit_id: int,
                                     page: ExecutionCompletionPage, now: datetime) -> list[str]:
        """原终态引用与游标一次提交；不复制执行正文或另建事件账本。"""
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            if task.revision != expected_revision or task.lifecycle != "active":
                return []
            source_uri = task.configuration.trigger.source_uri
            changed = connection.execute("UPDATE persistent_task_execution_sources SET after_commit_id=? "
                                         "WHERE task_id=? AND after_commit_id=? AND definition_revision=? AND source_uri=?",
                                         (page.through_commit_id, task_id, after_commit_id, task.definition_revision, source_uri)).rowcount
            if changed != 1 or page.through_commit_id < after_commit_id:
                raise RuntimeError("执行结束来源游标冲突")
            run_ids = []
            for event in page.records:
                if not after_commit_id < event.cursor_commit_id <= page.through_commit_id:
                    raise ValueError("执行结束引用不属于当前来源页")
                key = f"execution-end:{source_uri}:{event.execution_id}"
                payload = {"source_uri": source_uri, "commit_id": event.commit_id, "execution_id": event.execution_id,
                           "job_id": event.job_id, "outcome": event.outcome}
                digest = sha256_jcs(payload)
                row = connection.execute("SELECT trigger_hash FROM persistent_task_runs WHERE task_id=? AND trigger_key=?",
                                         (task_id, key)).fetchone()
                if row is not None:
                    if row[0] != digest:
                        raise RuntimeError("原执行终态引用发生冲突")
                    continue
                run_id = create_prefixed_id("ptrun")
                connection.execute(_INSERT_RUN, (run_id, task_id, task.revision, task.definition_revision,
                    key, digest, event.request_id or create_prefixed_id("req"), 0, "pending", None, None, None,
                    now.isoformat(), now.isoformat(), "execution_end", None, json.dumps(payload, ensure_ascii=False)))
                run_ids.append(run_id)
            return run_ids

    @staticmethod
    def _set_schedule(connection: sqlite3.Connection, task_id: str, configuration: PersistentTaskConfigurationDTO,
                      definition_revision: int, now: datetime) -> None:
        schedule = configuration.trigger.schedule()
        if schedule is None:
            connection.execute("DELETE FROM persistent_task_schedules WHERE task_id=?", (task_id,))
        else:
            connection.execute("INSERT INTO persistent_task_schedules VALUES (?,?,?,?) "
                               "ON CONFLICT(task_id) DO UPDATE SET definition_revision=excluded.definition_revision,"
                               "anchor_at=excluded.anchor_at,next_run_at=excluded.next_run_at",
                               (task_id, definition_revision, now.isoformat(), schedule.next_time(now).isoformat()))

    def control(self, task_id: str, *, expected_revision: int, lifecycle: TaskLifecycle,
                now: datetime, reason: str | None = None, source_cursor: int | None = None) -> PersistentTaskDTO:
        with self._transaction() as connection:
            task = self._task(connection, task_id)
            self._require_revision(task, expected_revision)
            if lifecycle == "active" and task.lifecycle != "active" and task.configuration.trigger.source_uri is not None:
                self._set_execution_source(connection, task_id, task.configuration, task.definition_revision, source_cursor)
            connection.execute("UPDATE persistent_tasks SET revision=revision+1,lifecycle=?,reason=?,updated_at=? WHERE task_id=?",
                               (lifecycle, reason, now.isoformat(), task_id))
            return self._task(connection, task_id)

    def _run(self, row: sqlite3.Row) -> PersistentTaskRunDTO:
        return PersistentTaskRunDTO(
            run_id=row["run_id"], task_id=row["task_id"], task_revision=row["task_revision"],
            definition_revision=row["definition_revision"], trigger_key=row["trigger_key"], check_only=bool(row["check_only"]),
            request_id=row["request_id"],
            trigger_type=row["trigger_type"], scheduled_for=row["scheduled_for"],
            trigger_payload=json.loads(row["trigger_payload_json"]) if row["trigger_payload_json"] is not None else None,
            evaluation=TaskEvaluationDTO.model_validate_json(row["evaluation_json"]) if row["evaluation_json"] else None,
            generation=GENERATION_STATE.validate_json(row["generation_json"]).result(
                run_id=row["run_id"], workspace_id=self.workspace_id, error=row["error"],
            ) if row["generation_json"] else None,
            phase=row["phase"], decision=TaskDecisionDTO.model_validate_json(row["decision_json"]) if row["decision_json"] else None,
            delivery=TaskDeliveryDTO.model_validate_json(row["delivery_json"]) if row["delivery_json"] else None,
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def runs(self, task_id: str, *, pending_only: bool = False) -> list[PersistentTaskRunDTO]:
        with closing(self._store.connection()) as connection:
            self._task(connection, task_id)
            condition = "AND phase IN ('pending','checking','delivering')" if pending_only else ""
            rows = connection.execute(f"SELECT * FROM persistent_task_runs WHERE task_id=? {condition} ORDER BY run_sequence",
                                      (task_id,)).fetchall()
            return [self._run(row) for row in rows]

    def generation_state(self, run: PersistentTaskRunDTO) -> GenerationActionState | None:
        with closing(self._store.connection()) as connection:
            self._task(connection, run.task_id)
            row = connection.execute("SELECT generation_json FROM persistent_task_runs WHERE run_id=? AND task_id=?",
                                     (run.run_id, run.task_id)).fetchone()
            if row is None:
                raise KeyError(f"持久任务运行不存在: {run.run_id}")
            state = GENERATION_STATE.validate_json(row[0]) if row[0] else None
            if state is not None and not isinstance(state, GenerationActionState):
                raise RuntimeError("不可推进迁移的历史终态")
            return state

    def record_generation(self, run: PersistentTaskRunDTO, state: GenerationActionState, now: datetime, *,
                          phase: TaskRunPhase = "delivering", error: str | None = None,
                          delivery: TaskDeliveryDTO | None = None, lifecycle: TaskLifecycle | None = None) -> None:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE persistent_task_runs SET generation_json=?,phase=?,error=?,updated_at=?,delivery_json=COALESCE(?,delivery_json) "
                "WHERE run_id=? AND task_id=? AND phase=?",
                (state.model_dump_json(), phase, error, now.isoformat(), delivery.model_dump_json() if delivery is not None else None,
                 run.run_id, run.task_id, run.phase),
            ).rowcount
            if changed != 1:
                raise RuntimeError("会话动作运行阶段发生并发冲突")
            if lifecycle is not None:
                connection.execute("UPDATE persistent_tasks SET lifecycle=?,revision=revision+1,reason=?,updated_at=? "
                                   "WHERE task_id=? AND revision=? AND lifecycle='active'",
                                   (lifecycle, error, now.isoformat(), run.task_id, run.task_revision))

    def trigger(self, task_id: str, request: PersistentTaskCheckRequest | PersistentTaskNotificationRequest, now: datetime, *,
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
            external = isinstance(request, PersistentTaskNotificationRequest)
            check_only = request.check_only if isinstance(request, PersistentTaskCheckRequest) else False
            if isinstance(request, PersistentTaskCheckRequest):
                self._require_revision(task, request.expected_revision)
            elif task.configuration.trigger.type_id != "builtin.external":
                raise ValueError("只有外部通知触发的任务接受notifications")
            if task.lifecycle != "active" and not check_only:
                raise ClientStateConflictError("只有 active 持久任务可接受新动作")
            run_id = create_prefixed_id("ptrun")
            connection.execute(_INSERT_RUN, (
                run_id, task_id, task.revision, task.definition_revision, request.idempotency_key, preimage,
                request_id, int(check_only), "pending", None, None, None, now.isoformat(), now.isoformat(),
                "external" if external else "manual", None,
                json.dumps(request.payload, ensure_ascii=False) if isinstance(request, PersistentTaskNotificationRequest) else None,
            ))
            return self._run(connection.execute("SELECT * FROM persistent_task_runs WHERE run_id=?", (run_id,)).fetchone())

    def schedule_due(self, now: datetime) -> list[str]:
        """计划触发和游标一次提交；中断只会保留两者或两者均不保留。"""
        changed: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT s.* FROM persistent_task_schedules s JOIN persistent_tasks t ON t.task_id=s.task_id "
                "WHERE t.workspace_id=? AND t.lifecycle='active' ORDER BY s.next_run_at,s.task_id", (self.workspace_id,),
            ).fetchall()
            for row in rows:
                task_id = row["task_id"]
                task = self._task(connection, task_id)
                schedule = task.configuration.trigger.schedule()
                if schedule is None or row["definition_revision"] != task.definition_revision:
                    raise RuntimeError(f"持久任务计划与定义不一致: {task_id}")
                due, skipped, following = evaluate_schedule(
                    schedule, anchor=datetime.fromisoformat(row["anchor_at"]),
                    candidate=datetime.fromisoformat(row["next_run_at"]), now=now,
                    misfire=task.configuration.policies.misfire,
                )
                if not due and not skipped:
                    continue
                for scheduled_for in [*due, *skipped]:
                    trigger_key = f"{task_id}:{schedule.kind}:{row['anchor_at']}:{scheduled_for.isoformat()}"
                    preimage = sha256_jcs({"trigger_key": trigger_key, "definition_revision": task.definition_revision})
                    missed = scheduled_for in skipped
                    connection.execute(_INSERT_RUN, (
                        create_prefixed_id("ptrun"), task_id, task.revision, task.definition_revision,
                        trigger_key, preimage, create_prefixed_id("req"), 0, "skipped" if missed else "pending", None, None,
                        "按 misfire=skip 跳过积压触发" if missed else None, now.isoformat(), now.isoformat(),
                        schedule.kind, scheduled_for.isoformat(), None,
                    ))
                connection.execute("UPDATE persistent_task_schedules SET next_run_at=? WHERE task_id=?",
                                   (following.isoformat(), task_id))
                changed.append(task_id)
        return changed

    def next_schedule_at(self) -> datetime | None:
        tasks = [task for task in self.list_automations() if task.lifecycle == "active"
                 and task.next_run_at is not None]
        return min((task.next_run_at for task in tasks if task.next_run_at is not None), default=None)

    def configuration(self, run: PersistentTaskRunDTO) -> PersistentTaskConfigurationDTO:
        with closing(self._store.connection()) as connection:
            row = connection.execute("SELECT configuration_json FROM persistent_task_definitions WHERE task_id=? AND definition_revision=?",
                                     (run.task_id, run.definition_revision)).fetchone()
        if row is None:
            raise RuntimeError("持久任务运行引用的定义版本缺失")
        return PersistentTaskConfigurationDTO.model_validate_json(row[0])

    def record_evaluation(self, run: PersistentTaskRunDTO, evaluation: TaskEvaluationDTO, now: datetime) -> None:
        """只登记原准入回执；正文仍由canonical owner持有。"""
        with self._transaction() as connection:
            row = connection.execute("SELECT evaluation_json FROM persistent_task_runs WHERE run_id=? AND phase='checking'", (run.run_id,)).fetchone()
            if row is None:
                raise RuntimeError("判断执行引用缺少checking运行")
            if row[0] is not None and TaskEvaluationDTO.model_validate_json(row[0]) != evaluation:
                raise RuntimeError("判断执行引用与原准入身份冲突")
            connection.execute("UPDATE persistent_task_runs SET evaluation_json=?,updated_at=? WHERE run_id=?",
                               (evaluation.model_dump_json(), now.isoformat(), run.run_id))

    def advance(self, run: PersistentTaskRunDTO, *, phase: TaskRunPhase, now: datetime,
                decision: TaskDecisionDTO | None = None, delivery: TaskDeliveryDTO | None = None,
                error: str | None = None, lifecycle: TaskLifecycle | None = None) -> None:
        with self._transaction() as connection:
            self._advance(connection, run, phase=phase, now=now, decision=decision, delivery=delivery,
                          error=error, lifecycle=lifecycle)

    @staticmethod
    def _advance(connection: sqlite3.Connection, run: PersistentTaskRunDTO, *, phase: TaskRunPhase, now: datetime,
                 decision: TaskDecisionDTO | None = None, delivery: TaskDeliveryDTO | None = None,
                 error: str | None = None, lifecycle: TaskLifecycle | None = None) -> None:
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
                               (lifecycle, error or (decision.reason if decision else None), now.isoformat(), run.task_id, run.task_revision))
        elif decision is not None and not run.check_only:
            connection.execute("UPDATE persistent_tasks SET reason=?,updated_at=? WHERE task_id=? AND revision=? AND lifecycle='active'",
                               (decision.reason, now.isoformat(), run.task_id, run.task_revision))

    def finish_check(self, run: PersistentTaskRunDTO, decision: TaskDecisionDTO, now: datetime) -> PersistentTaskRunDTO:
        """判断、策略选择与运行阶段原子提交；已接受动作仍由原回执事实证明。"""
        with self._transaction() as connection:
            task = self._task(connection, run.task_id)
            phase: TaskRunPhase = "checked"
            lifecycle: TaskLifecycle | None = None
            error = None
            if task.revision != run.task_revision or (task.lifecycle != "active" and not run.check_only):
                phase, error = "suppressed", "判断完成时任务 revision 或生命周期已变化，保留结果但未投递"
            elif not run.check_only:
                if decision.decision == "proceed" and self._match_allows(connection, run, task.configuration.policies.match_mode):
                    phase = "delivering"
                elif decision.decision in {"complete", "needs_attention"}:
                    lifecycle = "complete" if decision.decision == "complete" else "needs_attention"
            self._advance(connection, run, phase=phase, now=now, decision=decision, error=error, lifecycle=lifecycle)
            return self._run(connection.execute("SELECT * FROM persistent_task_runs WHERE run_id=?", (run.run_id,)).fetchone())

    @staticmethod
    def _match_allows(connection: sqlite3.Connection, run: PersistentTaskRunDTO, mode: str) -> bool:
        if mode == "each_match":
            return True
        if mode == "first_match":
            return connection.execute("SELECT 1 FROM persistent_task_runs WHERE task_id=? AND delivery_json IS NOT NULL LIMIT 1",
                                      (run.task_id,)).fetchone() is None
        # dry-run、过期结果和错误不改变边沿；只有已生效判断的接受顺序有效。
        previous = connection.execute(
            "SELECT decision_json FROM persistent_task_runs WHERE task_id=? AND check_only=0 "
            "AND phase IN ('checked','delivering','delivered') AND decision_json IS NOT NULL "
            "AND json_extract(decision_json,'$.decision') IN ('wait','proceed') "
            "AND run_sequence < (SELECT run_sequence FROM persistent_task_runs WHERE run_id=?) "
            "ORDER BY run_sequence DESC LIMIT 1", (run.task_id, run.run_id),
        ).fetchone()
        return previous is None or TaskDecisionDTO.model_validate_json(previous[0]).decision != "proceed"
