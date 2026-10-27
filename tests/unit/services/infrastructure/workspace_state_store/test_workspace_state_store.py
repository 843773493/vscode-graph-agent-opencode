from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from app.core.resource_uri import thread_resource_uri
from app.core.sqlite_state import SQLiteStateDatabase
from app.domain.itemized.execution_queue import (
    ExecutionCompletion,
    ExecutionCompletionPage,
)
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskCreateRequest,
    TaskDecisionDTO,
    TaskEvaluationDTO,
    TaskTriggerDTO,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import (
    _WORKSPACE_MIGRATIONS,
    WorkspaceStateStore,
)


def test_workspace_state_uses_workspace_boundary_and_activity_cursor(workspace_root):
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        assert store.path == workspace_root / ".boxteam" / "state" / "workspace.sqlite"
        store.sync_config_source(
            config_key="workspace",
            uri=None,
            config_version=1,
            payload={"jobs": {"max_concurrency": 2}},
            presence="present",
            layer_digest="workspace-digest",
        )
        first = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        second = store.append_activity(
            event_id="event-2",
            session_id="session-2",
            status="failed",
            summary="任务失败",
        )
        duplicate = store.append_activity(
            event_id="event-1",
            session_id="session-1",
            status="completed",
            summary="任务完成",
        )
        assert first.event_seq == 1
        assert duplicate.event_seq == first.event_seq
        assert [
            item.event_id
            for item in store.read_activity_page(after=first.event_seq).records
        ] == [second.event_id]
        assert store.diagnostics().schema_version == 22
    finally:
        store.close()


def test_task_decision_and_lifecycle_roll_back_together(workspace_root):
    now = datetime(2026, 10, 27, tzinfo=UTC)
    request = PersistentTaskCreateRequest.model_validate({
        "idempotency_key": "atomic-check", "configuration": {
            "name": "事务检查", "trigger": {"type_id": "builtin.manual", "version": "1"},
            "condition": {"type_id": "builtin.always", "version": "1"},
            "context": {"type_id": "builtin.literal", "version": "1", "content": "冻结输入"},
            "action": {"type_id": "builtin.runtime_source", "version": "1", "delivery_mode": "context_only",
                       "target_uri": thread_resource_uri("ws_atomic", "ses_atomic", "thr_atomic")},
        },
    })
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        tasks = PersistentTaskStore(store, workspace_id="ws_atomic")
        task = tasks.create(request, now)
        run = tasks.trigger(task.task_id, PersistentTaskCheckRequest(idempotency_key="complete", expected_revision=task.revision),
                            now, request_id="req_atomic")
        tasks.advance(run, phase="checking", now=now)
        run, = tasks.runs(task.task_id)
        decision = TaskDecisionDTO(decision="complete", reason="完成")
        with closing(store.connection()) as connection:
            connection.execute("CREATE TRIGGER stop_lifecycle BEFORE UPDATE ON persistent_tasks BEGIN SELECT RAISE(ABORT,'lifecycle-write-fault'); END")
        with pytest.raises(sqlite3.IntegrityError, match="lifecycle-write-fault"):
            tasks.finish_check(run, decision, now)
        assert tasks.get(task.task_id) == task and tasks.runs(task.task_id) == [run]
        with closing(store.connection()) as connection:
            connection.execute("DROP TRIGGER stop_lifecycle")
        actual = tasks.finish_check(run, decision, now)
        assert actual.phase == "checked" and actual.decision == decision
        assert tasks.get(task.task_id).lifecycle == "complete"
    finally:
        store.close()


def test_execution_event_and_source_cursor_roll_back_together(workspace_root):
    now = datetime(2026, 10, 27, tzinfo=UTC)
    uri = thread_resource_uri("ws_atomic", "ses_atomic", "thr_atomic")
    request = PersistentTaskCreateRequest.model_validate({
        "idempotency_key": "atomic-end", "configuration": {
            "name": "终态接受事务", "trigger": {"type_id": "builtin.execution_end", "version": "1", "source_uri": uri},
            "condition": {"type_id": "builtin.always", "version": "1"},
            "context": {"type_id": "builtin.literal", "version": "1", "content": "原终态引用"},
            "action": {"type_id": "builtin.runtime_source", "version": "1", "target_uri": uri, "delivery_mode": "context_only"},
        },
    })
    with WorkspaceStateStore(workspace_root=workspace_root) as store:
        tasks = PersistentTaskStore(store, workspace_id="ws_atomic")
        task = tasks.create(request, now, source_cursor=10)
        page = ExecutionCompletionPage((ExecutionCompletion(12, "exe_original", "job_original", "completed", "req_original", 12),), 15)
        with closing(store.connection()) as connection:
            connection.execute("CREATE TRIGGER stop_event BEFORE INSERT ON persistent_task_runs BEGIN SELECT RAISE(ABORT,'event-write-fault'); END")
        with pytest.raises(sqlite3.IntegrityError, match="event-write-fault"):
            tasks.accept_execution_completions(task.task_id, expected_revision=1, after_commit_id=10, page=page, now=now)
        assert tasks.execution_cursor(task.task_id) == 10 and tasks.runs(task.task_id) == []
        with closing(store.connection()) as connection:
            connection.execute("DROP TRIGGER stop_event")
        run_id, = tasks.accept_execution_completions(task.task_id, expected_revision=1, after_commit_id=10, page=page, now=now)
        run, = tasks.runs(task.task_id)
        assert run.run_id == run_id and run.request_id == "req_original" and tasks.execution_cursor(task.task_id) == 15
        assert tasks.accept_execution_completions(task.task_id, expected_revision=1, after_commit_id=15,
                                                 page=ExecutionCompletionPage((), 15), now=now) == []
        assert tasks.runs(task.task_id) == [run]
        tasks.control(task.task_id, expected_revision=1, lifecycle="paused", now=now)
        with pytest.raises(RuntimeError, match="历史已回退"):
            tasks.control(task.task_id, expected_revision=2, lifecycle="active", now=now, source_cursor=4)
        assert tasks.get(task.task_id).lifecycle == "paused" and tasks.execution_cursor(task.task_id) == 15


def test_model_evaluation_reference_is_immutable_and_does_not_publish_a_decision(workspace_root):
    now = datetime(2026, 10, 27, tzinfo=UTC)
    request = PersistentTaskCreateRequest.model_validate({
        "idempotency_key": "evaluation", "configuration": {
            "name": "明确判断来源", "trigger": {"type_id": "builtin.manual", "version": "1"},
            "condition": {"type_id": "builtin.always", "version": "1"},
            "context": {"type_id": "builtin.literal", "version": "1", "content": "原判断输入"},
            "action": {"type_id": "builtin.runtime_source", "version": "1", "target_uri": thread_resource_uri("ws_eval", "ses_work", "thr_work"),
                       "delivery_mode": "context_only"},
        },
    })
    with WorkspaceStateStore(workspace_root=workspace_root) as store:
        tasks = PersistentTaskStore(store, workspace_id="ws_eval")
        task = tasks.create(request, now)
        run = tasks.trigger(task.task_id, PersistentTaskCheckRequest(expected_revision=1, idempotency_key="check"), now, request_id="req_evaluation")
        tasks.advance(run, phase="checking", now=now)
        run, = tasks.runs(task.task_id)
        evaluation = TaskEvaluationDTO(thread_uri=thread_resource_uri("ws_eval", "ses_judge", "thr_judge"),
                                       source_item_id="item_original", job_id="job_original", execution_id="exe_original")
        tasks.record_evaluation(run, evaluation, now)
        tasks.record_evaluation(run, evaluation, now)
        with pytest.raises(RuntimeError, match="原准入身份冲突"):
            tasks.record_evaluation(run, evaluation.model_copy(update={"execution_id": "exe_other"}), now)
        recorded, = tasks.runs(task.task_id)
        assert recorded.phase == "checking" and recorded.evaluation == evaluation
        assert recorded.decision is None and recorded.delivery is None and tasks.get(task.task_id).revision == 1


@pytest.mark.parametrize("old_version", [16, 17, 18, 19, 20, 21])
def test_old_task_migration_preserves_history_and_atomic_schedule_order(workspace_root, monkeypatch, old_version):
    original = {
        "idempotency_key": "old-create", "enabled": True, "configuration": {
            "name": "旧手动任务", "trigger": {"type_id": "builtin.manual", "version": "1"},
            "condition": {"type_id": "builtin.always", "version": "1"},
            "context": {"type_id": "builtin.literal", "version": "1", "content": "冻结正文"},
            "action": {"type_id": "builtin.runtime_source", "version": "1",
                       "target_uri": thread_resource_uri("ws_old", "ses_old", "thr_old"),
                       "delivery_mode": "context_only", "delivery_policy": "after_turn"},
            "policies": {"completion": "continuous"},
        },
    }
    now = datetime(2026, 10, 27, tzinfo=UTC)
    database = SQLiteStateDatabase(path=workspace_root / ".boxteam/state/workspace.sqlite",
                                   schema_version=old_version, migrations=_WORKSPACE_MIGRATIONS[:old_version])
    with closing(database.connection()) as connection, connection:
        connection.execute("INSERT INTO persistent_tasks (task_id,workspace_id,creation_key,creation_hash,revision,definition_revision,lifecycle,reason,created_at,updated_at) VALUES ('ptask_old','ws_old','old-create',?,1,1,'active',NULL,?,?)",
                           (sha256_jcs(original), now.isoformat(), now.isoformat()))
        connection.execute("INSERT INTO persistent_task_definitions VALUES ('ptask_old',1,?)", (json.dumps(original["configuration"]),))
        if old_version == 16:
            connection.execute("INSERT INTO persistent_task_runs VALUES ('ptrun_old','ptask_old',1,1,'old-check','hash','req_original',0,'pending',NULL,NULL,NULL,?,?)",
                               (now.isoformat(), now.isoformat()))
        elif old_version == 17:
            connection.execute("INSERT INTO persistent_task_runs VALUES (37,'ptrun_old','ptask_old',1,1,'old-check','hash','req_original',0,'pending',NULL,NULL,NULL,?,?,'manual',NULL)",
                               (now.isoformat(), now.isoformat()))
        elif old_version < 20:
            connection.execute("INSERT INTO persistent_task_runs VALUES (37,'ptrun_old','ptask_old',1,1,'old-check','hash','req_original',0,'pending',NULL,NULL,NULL,?,?,'manual',NULL,NULL)",
                               (now.isoformat(), now.isoformat()))
        else:
            connection.execute("INSERT INTO persistent_task_runs VALUES (37,'ptrun_old','ptask_old',1,1,'old-check','hash','req_original',0,'pending',NULL,NULL,NULL,?,?,'manual',NULL,NULL,NULL)",
                               (now.isoformat(), now.isoformat()))
    database.close()
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        tasks = PersistentTaskStore(store, workspace_id="ws_old")
        request = PersistentTaskCreateRequest.model_validate(original)
        task = tasks.create(request, now)
        assert task.task_id == "ptask_old" and task.next_run_at is None
        run, = tasks.runs(task.task_id)
        assert run.run_id == "ptrun_old" and run.request_id == "req_original"
        assert run.trigger_type == "manual" and run.scheduled_for is None and run.phase == "pending" and run.trigger_payload is None and run.evaluation is None
        assert tasks.configuration(run) == request.configuration
        with closing(store.connection()) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='persistent_task_runs_v16'").fetchall() == []
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='persistent_task_runs_v17'").fetchall() == []
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='persistent_task_runs_v18'").fetchall() == []
            assert connection.execute("SELECT run_sequence FROM persistent_task_runs WHERE run_id='ptrun_old'").fetchone()[0] == (1 if old_version == 16 else 37)
        scheduled = request.configuration.model_copy(update={"trigger": TaskTriggerDTO(type_id="builtin.interval", version="1", interval_seconds=60)})
        tasks.update(task.task_id, expected_revision=1, configuration=scheduled, now=now)
        due = now + timedelta(seconds=60)
        with closing(store.connection()) as connection:
            connection.execute("CREATE TRIGGER stop_cursor BEFORE UPDATE OF next_run_at ON persistent_task_schedules BEGIN SELECT RAISE(ABORT,'cursor-write-fault'); END")
        with pytest.raises(sqlite3.IntegrityError, match="cursor-write-fault"):
            tasks.schedule_due(due)
        assert tasks.runs(task.task_id) == [run]
        assert tasks.get(task.task_id).next_run_at == due
        with closing(store.connection()) as connection:
            connection.execute("DROP TRIGGER stop_cursor")
        assert tasks.schedule_due(due) == [task.task_id]
        assert tasks.schedule_due(due) == []
        manual, automatic = tasks.runs(task.task_id)
        assert manual == run and automatic.scheduled_for == due and automatic.trigger_type == "interval"
        assert tasks.get(task.task_id).next_run_at == due + timedelta(seconds=60)
        scheduled = scheduled.model_copy(update={"policies": scheduled.policies.model_copy(update={"misfire": "catch_up"})})
        tasks.update(task.task_id, expected_revision=2, configuration=scheduled, now=due)
        from app.services.infrastructure.persistent_tasks import store as task_store

        original_id = task_store.create_prefixed_id
        reversed_ids = sorted((original_id("ptrun") for _ in range(3)), reverse=True)
        identifiers = iter(reversed_ids)
        monkeypatch.setattr(task_store, "create_prefixed_id", lambda prefix: next(identifiers) if prefix == "ptrun" else original_id(prefix))
        tasks.schedule_due(now + timedelta(seconds=240))
        history = tasks.runs(task.task_id)
        assert [entry.run_id for entry in history[-3:]] == reversed_ids
        assert [entry.scheduled_for for entry in history[1:]] == [now + timedelta(seconds=n) for n in (60, 120, 180, 240)]
    finally:
        store.close()
