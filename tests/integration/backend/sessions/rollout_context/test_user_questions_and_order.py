"""实际领取顺序、模型上下文与独立留言生命周期的集成验证。"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage

from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    QueueOrderConflictError,
)
from app.prompting import internal_message_factory
from app.services.business.job.service import JobService
from app.services.infrastructure.rollout_context.execution.user_questions import (
    UserQuestionStore,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    EXECUTION_SCHEDULING_MIGRATION_SQL,
    TEAM_OBSERVATIONS_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema26,
)
from tests.integration.backend.sessions.rollout_context.test_runtime_notice_consumption import (
    NoticeCase,
    _wire_ids,
)
from tests.integration.backend.sessions.rollout_context.test_runtime_notice_consumption import (
    notice_case as notice_case,  # noqa: PLC0414
)


@pytest.fixture
def schedule(notice_case: NoticeCase):
    case = notice_case

    def accept(text: str, *, background: bool = False):
        identity = JobService.reserve_user_execution_identity()
        message_id = "message-" + identity.job_id
        frozen = ExecutionQueueInput(identity, message_id, "default", None, "after_turn", None, None,
            datetime.now(UTC).isoformat(), background=background)
        accepted = case.saver.accept_turn(case.session, thread_id=case.thread, accepted_ingress_id=message_id,
            acceptance_idempotency_key=identity.admission_idempotency_key, job_id=identity.job_id,
            admission_idempotency_key=identity.admission_idempotency_key, payload=text,
            turn_id=identity.turn_id, queue_input=frozen)
        return case.saver.execution_queue_record(case.session, case.thread, identity.job_id), accepted["root_input_item_id"]
    return case, accept


def test_reorder_changes_consumption_context_without_mutating_acceptance(schedule):
    case, accept = schedule
    first, first_root = accept("A 后执行")
    second, second_root = accept("B 先执行")
    original = case.saver.execution_queue_snapshot(case.session, case.thread)
    case.saver.reorder_execution_queue(case.session, case.thread,
        message_ids=(second.input.message_id, first.input.message_id), expected_revision=original.revision)
    with pytest.raises(QueueOrderConflictError):
        case.saver.reorder_execution_queue(case.session, case.thread,
            message_ids=(first.input.message_id, second.input.message_id), expected_revision=original.revision)
    case.restart()
    assert [record.input.message_id for record in case.saver.execution_queue_snapshot(case.session, case.thread).records] == [second.input.message_id, first.input.message_id]
    claimed = case.saver.claim_execution_queue(case.session, case.thread, job_id=second.cursor.admission.job_id,
        claim_owner="test", previous_generation=0)
    sealed = case.prepare(claimed.cursor.admission, "B-first")
    assert second_root in _wire_ids(sealed) and first_root not in _wire_ids(sealed)
    case.saver.mark_job_terminal_status(session_id=case.session, thread_id=case.thread,
        job_id=second.cursor.admission.job_id, status="cancelled")
    claimed_first = case.saver.claim_execution_queue(case.session, case.thread, job_id=first.cursor.admission.job_id,
        claim_owner="test", previous_generation=0)
    next_seal = case.prepare(claimed_first.cursor.admission, "A-second")
    assert _wire_ids(next_seal).index(second_root) < _wire_ids(next_seal).index(first_root)
    assert claimed_first.cursor == first.cursor


def test_user_messages_precede_background_but_do_not_preempt_claim(schedule):
    case, accept = schedule
    task, _ = accept("任务续跑", background=True)
    user, _ = accept("用户回答")
    current = case.saver.execution_queue_snapshot(case.session, case.thread)
    assert [record.input.message_id for record in current.records] == [user.input.message_id, task.input.message_id]
    case.saver.claim_execution_queue(case.session, case.thread, job_id=user.cursor.admission.job_id,
        claim_owner="test", previous_generation=0)
    late, _ = accept("稍后消息")
    assert [record.input.message_id for record in case.saver.execution_queue_snapshot(case.session, case.thread).records] == [user.input.message_id, late.input.message_id, task.input.message_id]
    with pytest.raises(QueueOrderConflictError):
        case.saver.reorder_execution_queue(case.session, case.thread, message_ids=(user.input.message_id, late.input.message_id),
            expected_revision=case.saver.execution_queue_snapshot(case.session, case.thread).revision)


def test_internal_task_uses_same_priority_queue(schedule):
    case, accept = schedule
    intent = InternalExecutionIntent("task-message", "task-execution", "task-admission")
    message = internal_message_factory.build(kind="checkpoint_reminder", control="持久任务")
    frozen = ExecutionQueueInput(intent, intent.message_id, "default", None, "after_turn", None, None,
        datetime.now(UTC).isoformat(), background=True)
    case.saver.admit_internal_execution(case.session, thread_id=case.thread, job_id="task-job", intent=intent,
        queue_input=frozen, message=HumanMessage(id=intent.message_id, content=message.content,
            response_metadata={"message_id": intent.message_id, "created_at": frozen.created_at, "message_metadata": message.metadata}))
    user, _ = accept("普通消息")
    assert [record.input.message_id for record in case.saver.execution_queue_snapshot(case.session, case.thread).records] == [user.input.message_id, intent.message_id]
    claimed = case.saver.claim_execution_queue(case.session, case.thread, job_id=user.cursor.admission.job_id,
        claim_owner="test", previous_generation=0)
    sealed = case.prepare(claimed.cursor.admission, "user-before-task")
    assert intent.message_id not in _wire_ids(sealed)


def test_question_delete_is_idempotent_and_replay_does_not_resurrect(schedule):
    case, accept = schedule
    record, _ = accept("给用户留言")
    store = case.saver.user_questions
    kwargs = {"question_id": "ask-operation", "markdown": "**关键问题**\n\n请稍后阅读", "model_call_id": "emitting-call", "turn_id": record.cursor.admission.turn_id}
    store.create(case.session, case.thread, **kwargs)
    assert store.page(case.session, case.thread, before=None, limit=1).items[0].markdown.startswith("**关键问题**")
    store.delete(case.session, case.thread, "ask-operation")
    case.restart()
    cold = UserQuestionStore(case.saver._storage.storage)
    cold.create(case.session, case.thread, **kwargs)
    assert cold.summary(case.session, case.thread).count == 0
    assert cold.delete(case.session, case.thread, "ask-operation").count == 0
    with pytest.raises(ValueError, match="不同留言"):
        cold.create(case.session, case.thread, **{**kwargs, "markdown": "不同正文"})
    assert case.saver.execution_queue_snapshot(case.session, case.thread).records[0].phase == "queued"


def test_questions_are_bounded_and_paginate_without_repeating(schedule):
    case, accept = schedule
    accept("初始化")
    for index in range(23):
        case.saver.user_questions.create(case.session, case.thread, question_id=f"q-{index}", markdown=str(index), model_call_id="call", turn_id=None)
    first = case.saver.user_questions.page(case.session, case.thread, before=None, limit=20)
    last = case.saver.user_questions.page(case.session, case.thread, before=first.next_before, limit=20)
    assert first.count == 23 and len(first.items) == 20 and len(last.items) == 3 and last.next_before is None
    assert len({item.question_id for item in [*first.items, *last.items]}) == 23


@pytest.mark.parametrize("active", [False, True])
def test_schema26_upgrade_preserves_history_and_requires_queue_drain(schedule, active):
    case, accept = schedule
    record, _ = accept("升级前已接受的消息")
    if not active:
        case.saver.mark_job_terminal_status(session_id=case.session, thread_id=case.thread,
            job_id=record.cursor.admission.job_id, status="cancelled")
    storage = case.saver._storage.storage
    original = storage.jsonl_path(case.session, thread_id=case.thread).read_bytes()
    with storage._connect(case.thread, session_id=case.session) as connection:
        restore_schema26(connection)
    def migrate():
        storage.migrate_schema(case.thread, session_id=case.session, to_version=27,
            migration_name="execution_queue_026_027", migration_sql=EXECUTION_SCHEDULING_MIGRATION_SQL)
    if active:
        with pytest.raises(RuntimeError, match="排空执行队列"):
            migrate()
        with storage._connect(case.thread, session_id=case.session) as connection:
            assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (26,)
            assert connection.execute("SELECT phase FROM execution_queue").fetchone() == ("queued",)
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='user_questions'").fetchone() is None
    else:
        migrate()
        storage.migrate_schema(case.thread, session_id=case.session, to_version=28,
            migration_name="team_observations_027_028", migration_sql=TEAM_OBSERVATIONS_MIGRATION_SQL)
        case.restart()
        assert case.saver.execution_queue_snapshot(case.session, case.thread).records == ()
        assert case.saver.user_questions.summary(case.session, case.thread).count == 0
    assert storage.jsonl_path(case.session, thread_id=case.thread).read_bytes() == original
