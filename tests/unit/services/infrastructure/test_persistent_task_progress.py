"""同一任务 owner 的用量、控制版本、预算与恢复水位。"""

from datetime import UTC, datetime

import pytest

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.state_conflict import ClientStateConflictError
from app.core.identifier import create_prefixed_id
from app.core.resource_uri import thread_resource_uri
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    PersistentTaskCreateRequest,
)
from app.schemas.internal_v2.task_usage import (
    TaskDispatchDTO,
    TaskExecutionReferenceDTO,
)
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


@pytest.fixture
def progress_case(empty_workspace_scope):
    workspace_id, root = empty_workspace_scope
    now = datetime(2026, 11, 17, tzinfo=UTC)
    session_id, thread_id = create_prefixed_id("ses"), create_prefixed_id("thr")
    configuration = PersistentTaskConfigurationDTO.model_validate({
        "name": "通用预算", "trigger": {"version": "1", "type_id": "builtin.manual"},
        "condition": {"version": "1", "type_id": "builtin.always"},
        "context": {"version": "1", "type_id": "builtin.literal", "content": "继续工作"},
        "action": {"version": "1", "type_id": "builtin.runtime_source", "delivery_mode": "context_and_run",
                   "target_uri": thread_resource_uri(workspace_id, session_id, thread_id)},
    })
    reference = TaskExecutionReferenceDTO(session_id=session_id, thread_id=thread_id,
        execution_id="execution-original", started_at=now, control_revision=1, run_id="run-original")
    with WorkspaceStateStore(workspace_root=root) as state:
        store = PersistentTaskStore(state, workspace_id=workspace_id)
        task = store.create(PersistentTaskCreateRequest(idempotency_key="create", configuration=configuration), now)
        store.bind_execution(task.task_id, "job-original", reference, now)
        yield store, task, reference, now


def test_unlimited_default_and_duplicate_or_late_usage_do_not_invalidate_control(progress_case):
    store, task, reference, now = progress_case
    assert task.configuration.token_budget is None
    before = store.get(task.task_id)
    assert store.account_execution(task.task_id, job_id="job-original", tokens=100_000,
        elapsed_seconds=7, settled=True, now=now)
    used = store.get(task.task_id)
    assert (used.tokens_used, used.time_used_seconds, used.lifecycle, used.revision) == (100_000, 7, "active", task.revision)
    assert used.sync_version > before.sync_version
    assert not store.account_execution(task.task_id, job_id="job-original", tokens=90_000,
        elapsed_seconds=7, settled=True, now=now)
    assert store.get(task.task_id) == used
    assert store.account_execution(task.task_id, job_id="job-original", tokens=100_001,
        elapsed_seconds=40, settled=True, now=now)
    late = store.get(task.task_id)
    assert (late.tokens_used, late.time_used_seconds, late.revision) == (100_001, 7, task.revision)
    assert store.usage_state(task.task_id).accounted_jobs["job-original"].execution == reference
    updated = store.update(task.task_id, expected_revision=task.revision,
        configuration=task.configuration.model_copy(update={"name": "用量变化期间仍可编辑"}), now=now)
    assert updated.tokens_used == 100_001


def test_budget_stops_only_new_admissions_and_requires_explicit_budget_change(progress_case):
    store, task, _reference, now = progress_case
    limited = task.configuration.model_copy(update={"token_budget": 10})
    task = store.update(task.task_id, expected_revision=task.revision, configuration=limited, now=now)
    store.account_execution(task.task_id, job_id="job-original", tokens=11, elapsed_seconds=3, settled=True, now=now)
    exhausted = store.get(task.task_id)
    assert (exhausted.lifecycle, exhausted.tokens_used) == ("needs_attention", 11)
    with pytest.raises(ClientStateConflictError, match="预算已耗尽"):
        store.control(task.task_id, expected_revision=exhausted.revision, lifecycle="active", now=now)
    edited = store.update(task.task_id, expected_revision=exhausted.revision,
        configuration=limited.model_copy(update={"token_budget": 30}), now=now)
    assert edited.lifecycle == "needs_attention"
    resumed = store.control(task.task_id, expected_revision=edited.revision, lifecycle="active", now=now)
    assert resumed.tokens_used == 11 and resumed.lifecycle == "active"


def test_lowering_budget_below_existing_usage_blocks_new_admissions_immediately(progress_case):
    store, task, _reference, now = progress_case
    store.account_execution(task.task_id, job_id="job-original", tokens=11, elapsed_seconds=3, settled=True, now=now)
    limited = store.update(task.task_id, expected_revision=task.revision,
        configuration=task.configuration.model_copy(update={"token_budget": 10}), now=now)
    assert limited.lifecycle == "needs_attention" and limited.tokens_used == 11
    assert limited.revision == task.revision + 1
    with pytest.raises(ClientStateConflictError, match="预算已耗尽"):
        store.control(task.task_id, expected_revision=limited.revision, lifecycle="active", now=now)


@pytest.mark.parametrize("lifecycle", ["paused", "terminated"])
def test_late_usage_keeps_explicit_control_and_survives_reopening(progress_case, lifecycle):
    store, task, reference, now = progress_case
    task = store.update(task.task_id, expected_revision=task.revision,
        configuration=task.configuration.model_copy(update={"token_budget": 1}), now=now)
    controlled = store.control(task.task_id, expected_revision=task.revision, lifecycle=lifecycle, now=now)
    store.account_execution(task.task_id, job_id="job-original", tokens=50, elapsed_seconds=4, settled=True, now=now)
    store._store.close()
    with WorkspaceStateStore(workspace_root=store._store.workspace_root) as reopened:
        restored = PersistentTaskStore(reopened, workspace_id=store.workspace_id)
        current = restored.get(task.task_id)
        assert (current.lifecycle, current.revision, current.tokens_used) == (lifecycle, controlled.revision, 50)
        assert restored.usage_state(task.task_id).accounted_jobs["job-original"].execution == reference
        assert restored.execution_owners("job-original") == [task.task_id]


def test_execution_identity_and_dispatch_intent_use_the_same_owner(progress_case):
    store, task, reference, now = progress_case
    original = store.get(task.task_id)
    store.bind_execution(task.task_id, "job-original", reference, now)
    assert store.get(task.task_id) == original
    with pytest.raises(RuntimeError, match="原准入引用冲突"):
        store.bind_execution(task.task_id, "job-original", reference.model_copy(update={"execution_id": "other"}), now)
    with pytest.raises(RuntimeError, match="缺少原执行归属"):
        store.account_execution(task.task_id, job_id="unknown", tokens=1, elapsed_seconds=1, settled=True, now=now)
    intent = TaskDispatchDTO(trigger_id="trigger", idempotency_key="original-key", source_id="original-source",
        revision=1, control_revision=1, message=PreparedInternalMessage("冻结的原提示", {"kind": "test"}))
    store.record_dispatch(task.task_id, intent, now)
    assert store.usage_state(task.task_id).dispatch == intent
    accepted = intent.model_copy(update={"job_id": "job-original", "message": None})
    store.record_dispatch(task.task_id, accepted, now)
    state = store.usage_state(task.task_id)
    assert state.dispatch == accepted and state.dispatch.message is None
    assert state.accounted_jobs["job-original"].execution == reference
