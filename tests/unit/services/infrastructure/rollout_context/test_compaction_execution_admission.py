"""摘要派生输入沿原 execution 准入持久化，不能伪装聊天或普通 Job。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    InternalExecutionIntent,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.prompting import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION = "ses_019c3dbcb2f6749b85bed7b51dfe889d"


@pytest.fixture
def saver(tmp_path: Path, session_bundle_factory: Callable[[Path, str], Path]):
    session_bundle_factory(tmp_path, SESSION)
    with RolloutCheckpointSaver(tmp_path) as owner:
        yield owner


@pytest.fixture
def intent() -> InternalExecutionIntent:
    return InternalExecutionIntent(
        message_id="msg-compaction", execution_id="exe-compaction",
        admission_idempotency_key="admission-compaction",
        derived_reference=DerivedExecutionReference(
            task_id="task-summary", plan_id="plan-frozen-summary",
        ),
    )


def test_compaction_admission_is_durable_without_chat_side_effects(
    saver: RolloutCheckpointSaver, intent: InternalExecutionIntent,
) -> None:
    thread = saver._storage.storage.main_thread_id(SESSION)
    accepted = saver.accept_turn(
        SESSION, thread_id=thread, accepted_ingress_id="user-ingress",
        acceptance_idempotency_key="user-acceptance", job_id="user-job",
        admission_idempotency_key="user-admission", payload="仍在运行的真实用户输入",
    )
    with saver._storage.storage.read_connection(SESSION, thread) as db:
        before = db.execute("SELECT * FROM turn_records").fetchall()
        view = db.execute("SELECT * FROM context_view_items").fetchall()
    first = saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="job-compaction", intent=intent, message=None,
    )
    assert saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="job-compaction", intent=intent, message=None,
    ) == first
    assert first.execution_origin == "internal" and first.turn_id is None
    assert first.execution_id != accepted["initial_execution_id"]
    with saver._storage.storage.read_connection(SESSION, thread) as db:
        assert db.execute("SELECT * FROM turn_records").fetchall() == before
        assert db.execute("SELECT * FROM context_view_items").fetchall() == view
        assert db.execute("SELECT COUNT(*) FROM item_catalog WHERE execution_id=?", (first.execution_id,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM execution_queue WHERE execution_id=?", (first.execution_id,)).fetchone() == (0,)
        commit, = db.execute(
            "SELECT metadata_json FROM storage_commits WHERE subject_id=? AND commit_kind='execution_admission'",
            (first.execution_id,),
        ).fetchall()
        assert json.loads(commit[0])["request_input"] == {
            "kind": "derived_reference", "reference": asdict(intent.derived_reference),
        }
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold.derived_execution_reference(
            SESSION, thread_id=thread, execution_id=first.execution_id,
        ) == intent.derived_reference
        assert cold.execution_admission(SESSION, thread_id=thread, execution_id=first.execution_id) == first
        assert cold.internal_seed_source(SESSION, thread, first.execution_id) is None
        with pytest.raises(ValueError, match="不是普通 Agent 聊天输入"):
            cold.accepted_internal_message(
                SESSION, thread_id=thread, job_id=first.job_id,
                message_id=intent.message_id, intent=intent,
            )
        # 不能删掉调用方的 typed reference，把相同 durable execution 当通知读。
        with pytest.raises(ValueError, match="不是普通 Agent 聊天输入"):
            cold.accepted_internal_message(
                SESSION, thread_id=thread, job_id=first.job_id,
                message_id=intent.message_id, intent=replace(intent, derived_reference=None),
            )


def test_compaction_admission_rejects_changed_input_and_forged_notice(
    saver: RolloutCheckpointSaver, intent: InternalExecutionIntent,
) -> None:
    thread = saver._storage.storage.main_thread_id(SESSION)
    with pytest.raises(ValueError, match="不得伪造 runtime notice"):
        saver.admit_internal_execution(
            SESSION, thread_id=thread, job_id="job-compaction", intent=intent,
            message=HumanMessage(id=intent.message_id, content="假用户消息"),
        )
    saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="job-compaction", intent=intent, message=None,
    )
    changed = replace(intent, derived_reference=replace(intent.derived_reference, plan_id="changed-plan"))
    with pytest.raises(ValueError, match="幂等键已绑定不同"):
        saver.admit_internal_execution(
            SESSION, thread_id=thread, job_id="job-compaction", intent=changed, message=None,
        )
    assert saver.derived_execution_reference(
        SESSION, thread_id=thread, execution_id=intent.execution_id,
    ) == intent.derived_reference


@pytest.mark.parametrize("ordinary_kind", ["runtime_notice", "seed_reference"])
def test_derived_admission_does_not_require_ordinary_queue_input(
    saver: RolloutCheckpointSaver, intent: InternalExecutionIntent, ordinary_kind: str,
) -> None:
    thread = saver._storage.storage.main_thread_id(SESSION)
    saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="job-compaction", intent=intent, message=None,
    )
    saver.execution_queue_snapshot(SESSION, thread, validate_inputs=True)
    ordinary = InternalExecutionIntent(
        message_id="ordinary-notice", execution_id="ordinary-execution",
        admission_idempotency_key="ordinary-admission",
        input_reference_json=json.dumps({
            "creation_idempotency_key": "creation", "task_seed_hash": "seed",
            "reference_binding_hash": "binding",
        }) if ordinary_kind == "seed_reference" else None,
    )
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理内部结果",
        sections=(PromptSection("control_context", {"source": "test"}),
                  PromptSection("generated_session_result", "必须由普通队列调度")),
        display_content="内部结果等待调度",
    )
    saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="ordinary-job", intent=ordinary,
        message=None if ordinary_kind == "seed_reference" else HumanMessage(id=ordinary.message_id, content=prepared.content, response_metadata={
            "message_id": ordinary.message_id, "created_at": "2026-11-24T12:00:00+00:00",
            "message_metadata": prepared.metadata,
        }),
    )
    with pytest.raises(RuntimeError, match="job_id=ordinary-job"):
        saver.execution_queue_snapshot(SESSION, thread, validate_inputs=True)


def test_compaction_cannot_enter_ordinary_execution_queue(intent: InternalExecutionIntent) -> None:
    with pytest.raises(ValueError, match="不得进入普通 Agent Job 队列"):
        ExecutionQueueInput(
            execution_binding=intent, message_id=intent.message_id, agent_id="default",
            model=None, delivery_policy="after_turn", gateway_id=None, request_id=None,
            created_at="2026-11-24T12:00:00+00:00",
        )
    with pytest.raises(ValueError, match="不得同时绑定"):
        replace(intent, input_reference_json=json.dumps({
            "creation_idempotency_key": "creation", "task_seed_hash": "seed",
            "reference_binding_hash": "binding",
        }))


def test_low_level_admission_cannot_hide_compaction_behind_notice_queue_binding(
    saver: RolloutCheckpointSaver, intent: InternalExecutionIntent,
) -> None:
    thread = saver._storage.storage.main_thread_id(SESSION)
    ordinary = ExecutionQueueInput(
        execution_binding=replace(intent, derived_reference=None),
        message_id=intent.message_id, agent_id="default", model=None,
        delivery_policy="after_turn", gateway_id=None, request_id=None,
        created_at="2026-11-24T12:00:00+00:00",
    )
    with pytest.raises(ValueError, match="不得进入普通 Agent Job 队列"):
        saver.admit_internal_execution(
            SESSION, thread_id=thread, job_id="job-compaction", intent=intent,
            message=None, queue_input=ordinary,
        )
    with saver._storage.storage.read_connection(SESSION, thread) as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM storage_commits").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM execution_queue").fetchone() == (0,)


def test_compaction_cold_reader_rejects_tampered_admission(
    saver: RolloutCheckpointSaver, intent: InternalExecutionIntent,
) -> None:
    thread = saver._storage.storage.main_thread_id(SESSION)
    saver.admit_internal_execution(
        SESSION, thread_id=thread, job_id="job-compaction", intent=intent, message=None,
    )
    with saver._storage.storage.write_connection(SESSION, thread) as db:
        db.execute(
            "UPDATE storage_commits SET metadata_json=json_set(metadata_json,"
            "'$.request_input.reference.plan_id','another-plan') WHERE subject_id=?",
            (intent.execution_id,),
        )
        db.commit()
    with (
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold,
        pytest.raises(RuntimeError, match="hash 与冻结 request 不一致"),
    ):
        cold.derived_execution_reference(
            SESSION, thread_id=thread, execution_id=intent.execution_id,
        )


@pytest.mark.parametrize("field,value", [
    ("task_id", ""), ("task_id", " "), ("plan_id", None),
])
def test_derived_reference_rejects_ambiguous_coordinates(
    intent: InternalExecutionIntent, field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(intent.derived_reference, **{field: value})
