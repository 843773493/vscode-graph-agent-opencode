"""回报是一次新执行；预算停止新准入，但不撤销已经接受的回报。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.identifier import create_prefixed_id
from app.core.key_lock_pool import KeyLockPool
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.runtime_wakeup import RuntimeExecutionProgress
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskConfigurationDTO,
    PersistentTaskCreateRequest,
    TaskDeliveryDTO,
)
from app.schemas.internal_v2.session_navigation.generation_state import (
    GenerationActionState,
)
from app.schemas.internal_v2.task_usage import TaskExecutionReferenceDTO
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from app.services.orchestration.persistent_tasks.generation import advance_generation
from app.services.orchestration.persistent_tasks.usage import settle_task


@pytest.fixture
def report_case(empty_workspace_scope, monkeypatch):
    workspace_id, root = empty_workspace_scope
    now = datetime(2026, 11, 17, tzinfo=UTC)
    session_id, thread_id = create_prefixed_id("ses"), create_prefixed_id("thr")
    target_session, target_thread = create_prefixed_id("ses"), create_prefixed_id("thr")
    identity = UserExecutionIdentity(job_id="child-job", turn_id="child-turn", admission_idempotency_key="child-key")

    @asynccontextmanager
    async def admission(_session_id):
        yield

    with WorkspaceStateStore(workspace_root=root) as state:
        runtime = SimpleNamespace(
            store=PersistentTaskStore(state, workspace_id=workspace_id), clock=lambda: now + timedelta(seconds=3),
            locks=KeyLockPool(), _waiting_actions=set(), notify=Mock(),
            _sessions=SimpleNamespace(session_read_admission=admission, path_resolver=Mock()),
            _saver=Mock(), _dispatcher=Mock(), _orchestrator=SimpleNamespace(submit_runtime_event=AsyncMock()),
            messages=Mock(), jobs=SimpleNamespace(list=AsyncMock()),
        )
        runtime._saver.execution_admission_for_job.return_value = SimpleNamespace(
            execution_id="child-execution", turn_id=identity.turn_id,
            admission_idempotency_key=identity.admission_idempotency_key,
        )
        runtime._saver.runtime_execution_outcome.return_value = "completed"
        runtime._saver.execution_progress.return_value = RuntimeExecutionProgress("completed", runtime.clock().isoformat(), 10)
        runtime._saver.execution_queue_record.side_effect = lambda _session, _thread, job_id: SimpleNamespace(
            input=SimpleNamespace(created_at=now.isoformat()),
            cursor=SimpleNamespace(admission=SimpleNamespace(execution_id="child-execution" if job_id == identity.job_id else "report-execution")),
        )
        runtime.messages.execution_output_for_job.return_value = ("保留的子会话结果", None)
        monkeypatch.setattr("app.services.orchestration.persistent_tasks.generation.generation_report_message",
            Mock(return_value=PreparedInternalMessage("生成结果回报", {})))
        configuration = PersistentTaskConfigurationDTO.model_validate({
            "name": "预算回报", "trigger": {"type_id": "builtin.manual", "version": "1"},
            "condition": {"type_id": "builtin.always", "version": "1"},
            "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
            "action": {"type_id": "builtin.session_generation", "version": "1", "generation": {
                "generator_type": {"type_id": "builtin.agent_prompt", "version": "1"}, "config": {"prompt": "执行"},
                "placement": {"kind": "workspace", "workspace_id": workspace_id}, "context_source": {"kind": "fresh"},
                "session_strategy": {"mode": "fork_new_and_report_back", "report_back": "summary_and_link",
                    "target": {"workspace_id": workspace_id, "session_id": target_session, "thread_id": target_thread}},
            }},
        })
        yield runtime, configuration, GenerationActionState(title="子会话", session_id=session_id,
            thread_id=thread_id, message_id="child-message", execution_identity=identity)


@pytest.mark.parametrize(("budget", "already_accepted"), [(10, False), (20, False), (None, False), (10, True)])
async def test_report_budget_keeps_generated_result_and_original_acceptance(report_case, budget, already_accepted):
    runtime, configuration, state = report_case
    configuration = configuration.model_copy(update={"token_budget": budget})
    store, now = runtime.store, runtime.clock()
    task = store.create(PersistentTaskCreateRequest(idempotency_key="task", configuration=configuration), now)
    run = store.trigger(task.task_id, PersistentTaskCheckRequest(expected_revision=task.revision, idempotency_key="run"), now, request_id=None)
    store.advance(run, phase="delivering", now=now, delivery=TaskDeliveryDTO(state="accepted", source_id="generation",
        job_id=state.execution_identity.job_id, execution_id="child-execution"))
    run = store.run(task.task_id, run.run_id)
    store.record_generation(run, state, now)
    report = SimpleNamespace(job_id="original-report", execution_id="report-execution")
    runtime._dispatcher.submitted.return_value = report if already_accepted else None

    async def accept(*args, **kwargs):
        runtime._dispatcher.submitted.return_value = report

    runtime._orchestrator.submit_runtime_event.side_effect = accept
    await advance_generation(runtime, configuration, run)

    suppressed = budget == 10 and not already_accepted
    result = store.run(task.task_id, run.run_id)
    actual = store.generation_state(result)
    assert result.phase == ("suppressed" if suppressed else "delivered")
    assert actual.status == "completed" and actual.session_id == state.session_id
    assert actual.execution_identity == state.execution_identity
    assert actual.report_back_job_id == (None if suppressed else report.job_id)
    assert store.get(task.task_id).tokens_used == (10 if suppressed else 20)
    assert store.get(task.task_id).lifecycle == ("active" if budget is None else "needs_attention")
    assert run.run_id not in runtime._waiting_actions
    assert runtime._orchestrator.submit_runtime_event.await_count == (0 if suppressed or already_accepted else 1)
    if suppressed:
        assert "未接受回报执行" in result.error


async def test_usage_recovery_skips_closed_history_and_still_accepts_late_notifications(report_case):
    runtime, configuration, state = report_case
    store, now = runtime.store, runtime.clock()
    task = store.create(PersistentTaskCreateRequest(idempotency_key="task", configuration=configuration), now)
    reference = TaskExecutionReferenceDTO(session_id=state.session_id, thread_id=state.thread_id,
        execution_id="child-execution", started_at=now, control_revision=task.revision)
    for job_id in ("closed", "current"):
        store.bind_execution(task.task_id, job_id, reference, now)
    store.account_execution(task.task_id, job_id="closed", tokens=10, elapsed_seconds=3, settled=True, now=now)

    await settle_task(runtime, task.task_id, set())

    runtime._saver.execution_progress.assert_called_once_with(state.session_id, state.thread_id,
        job_id="current", execution_id="child-execution")
    runtime._saver.execution_progress.reset_mock()
    runtime._saver.execution_progress.return_value = RuntimeExecutionProgress("completed", now.isoformat(), 11)
    await settle_task(runtime, task.task_id, {"closed"})
    runtime._saver.execution_progress.assert_called_once_with(state.session_id, state.thread_id,
        job_id="closed", execution_id="child-execution")
    assert store.get(task.task_id).tokens_used == 21
