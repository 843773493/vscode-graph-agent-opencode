"""执行 admission 的 SQLite owner 合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.hashing import content_hash
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION_ID = "ses_019c3dbcb2f6749b85bed7b51dfe889d"


@pytest.fixture
def saver(tmp_path: Path, session_bundle_factory) -> RolloutCheckpointSaver:
    session_bundle_factory(tmp_path, SESSION_ID)
    return RolloutCheckpointSaver(sessions_dir=tmp_path)


def _internal_notice(message_id: str, *, control: str = "任务已完成") -> HumanMessage:
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control=control,
        sections=(
            PromptSection("control_context", {"source": "test"}),
            PromptSection("generated_session_result", "内部结果"),
        ),
        display_content="内部任务已经完成。",
    )
    return HumanMessage(
        content=prepared.content,
        id=message_id,
        response_metadata={
            "message_id": message_id,
            "created_at": "2026-10-04T12:00:00+00:00",
            "message_metadata": prepared.metadata,
        },
    )


def test_internal_admission_creates_turnless_execution_and_reuses_exact_identity(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    message = _internal_notice("msg-internal-admission")
    intent = InternalExecutionIntent(
        message_id="msg-internal-admission",
        execution_id="execution-internal-1",
        admission_idempotency_key="team-task:event-1",
    )
    expected_notice = saver._storage._codec().items_for_message(
        message,
        item_sequence=1,
        message_id=intent.message_id,
        turn_id=None,
        execution_id=intent.execution_id,
        timestamp=message.response_metadata["created_at"],
    )[0]
    expected = ExecutionAdmission(
        session_id=SESSION_ID,
        thread_id=thread_id,
        execution_id="execution-internal-1",
        job_id="job-internal-1",
        admission_idempotency_key=intent.admission_idempotency_key,
        execution_origin="internal",
        turn_id=None,
    )

    first = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id=expected.job_id,
        intent=intent,
        message=message,
    )
    repeated = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id=expected.job_id,
        intent=intent,
        message=message,
    )

    assert first == repeated == expected
    assert saver.execution_admission(
        SESSION_ID,
        thread_id=thread_id,
        execution_id=expected.execution_id,
    ) == expected
    assert saver.execution_admission_for_job(
        SESSION_ID, thread_id=thread_id, job_id=expected.job_id
    ) == expected
    with sqlite3.connect(
        saver._storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as db:
        row = db.execute(
            "SELECT turn_id, accepted_ingress_id, execution_origin, job_id, "
            "admission_idempotency_key, admission_payload_hash "
            "FROM executions WHERE execution_id = ?",
            (expected.execution_id,),
        ).fetchone()
    assert row == (
        None,
        None,
        "internal",
            expected.job_id,
            expected.admission_idempotency_key,
        expected_notice.content_hash,
    )
    item = saver._storage.read_items(
        SESSION_ID,
        thread_id,
        item_ids=(intent.notice_item_id,),
    )[0]
    assert item.item_id == intent.notice_item_id
    with sqlite3.connect(
        saver._storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as db:
        item_row = db.execute(
            "SELECT turn_id, turn_scope, execution_id FROM item_catalog "
            "WHERE item_id = ?",
            (intent.notice_item_id,),
        ).fetchone()
        membership = db.execute(
            "SELECT logical_item_ordinal, visible, source_kind "
            "FROM context_view_items "
            "WHERE item_id = ?",
            (intent.notice_item_id,),
        ).fetchone()
    assert item_row == (None, "pending_next_turn", expected.execution_id)
    assert membership is not None and membership[0] >= 0
    assert membership[1:] == (1, "display_only")


def test_internal_admission_rejects_key_replay_with_changed_preimage(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    kwargs = {
        "thread_id": thread_id,
        "job_id": "job-internal-2",
    }
    intent = InternalExecutionIntent(
        message_id="msg-internal-admission-conflict",
        execution_id="execution-internal-2",
        admission_idempotency_key="team-task:event-2",
    )
    saver.admit_internal_execution(
        SESSION_ID,
        **kwargs,
        intent=intent,
        message=_internal_notice(intent.message_id),
    )

    with pytest.raises(ValueError, match="幂等键已绑定不同"):
        saver.admit_internal_execution(
            SESSION_ID,
            **kwargs,
            intent=intent,
            message=_internal_notice(intent.message_id, control="任务失败"),
        )


def test_internal_execution_lost_converges_without_creating_a_turn(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    intent = InternalExecutionIntent(
        message_id="msg-internal-lost",
        execution_id="execution-internal-lost",
        admission_idempotency_key="team-task:event-lost",
    )
    saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id="job-internal-lost",
        intent=intent,
        message=_internal_notice(intent.message_id),
    )

    lost = saver.mark_execution_lost(
        SESSION_ID,
        thread_id=thread_id,
        turn_id=None,
        execution_id=intent.execution_id,
        reason="provider_result_commit_missing",
    )
    replayed = saver.mark_execution_lost(
        SESSION_ID,
        thread_id=thread_id,
        turn_id=None,
        execution_id=intent.execution_id,
        reason="provider_result_commit_missing",
    )

    assert replayed == lost
    assert lost["status"] == "execution_lost"
    with sqlite3.connect(
        saver._storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as db:
        execution = db.execute(
            "SELECT turn_id, execution_origin, outcome FROM executions "
            "WHERE execution_id = ?",
            (intent.execution_id,),
        ).fetchone()
        turn_count = db.execute("SELECT COUNT(*) FROM turns").fetchone()
        terminal = db.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = "
            "'terminal_convergence' AND subject_id = ? AND outcome = 'execution_lost'",
            (intent.execution_id,),
        ).fetchone()
    assert execution == (None, "internal", "execution_lost")
    assert turn_count == (0,)
    assert terminal == (1,)


def test_user_turn_admission_hash_binds_acceptance_payload_without_conflating_hashes(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    payload = "accepted user payload"
    accepted = saver.accept_turn(
        SESSION_ID,
        accepted_ingress_id="ingress-user-admission",
        acceptance_idempotency_key="acceptance-user-admission",
        job_id="job-user-admission",
        admission_idempotency_key="admission-user-admission",
        payload=payload,
        payload_kind=PayloadKind.TEXT,
        turn_id="turn-user-admission",
        initial_execution_id="execution-user-admission",
    )

    admission = saver.execution_admission(
        SESSION_ID,
        thread_id=thread_id,
        execution_id="execution-user-admission",
    )

    payload_digest = content_hash(PayloadKind.TEXT, payload)
    assert accepted["admission_payload_hash"] != payload_digest
    assert admission == ExecutionAdmission(
        session_id=SESSION_ID,
        thread_id=thread_id,
        execution_id="execution-user-admission",
        job_id="job-user-admission",
        admission_idempotency_key="admission-user-admission",
        execution_origin="user_turn",
        turn_id="turn-user-admission",
    )
    assert saver.execution_admission_for_job(
        SESSION_ID, thread_id=thread_id, job_id="job-user-admission"
    ) == admission


def test_job_admission_lookup_rejects_missing_and_ambiguous_identity(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    with pytest.raises(KeyError, match="Job execution admission 不存在") as missing:
        saver.execution_admission_for_job(
            SESSION_ID, thread_id=thread_id, job_id="job-unresolved"
        )
    assert SESSION_ID in str(missing.value)
    assert thread_id in str(missing.value)
    assert "job-unresolved" in str(missing.value)

    for suffix in ("a", "b"):
        message_id = f"msg-ambiguous-{suffix}"
        saver.admit_internal_execution(
            SESSION_ID,
            thread_id=thread_id,
            job_id="job-ambiguous",
            intent=InternalExecutionIntent(
                message_id=message_id,
                execution_id=f"execution-ambiguous-{suffix}",
                admission_idempotency_key=f"admission-ambiguous-{suffix}",
            ),
            message=_internal_notice(message_id),
        )
    with pytest.raises(RuntimeError, match="Job execution admission 不唯一"):
        saver.execution_admission_for_job(
            SESSION_ID, thread_id=thread_id, job_id="job-ambiguous"
        )


def test_job_admission_lookup_keeps_session_owner_and_nullable_turn(
    saver: RolloutCheckpointSaver,
    session_bundle_factory,
) -> None:
    other_session = "ses_019c3dbcb2f6749b85bed7b51dfe889e"
    session_bundle_factory(saver._storage.sessions_dir, other_session)
    resolver = get_session_path_resolver(saver._storage.sessions_dir)
    expected = []
    for session_id in (SESSION_ID, other_session):
        thread_id = resolver.main_thread_id(session_id)
        message_id = f"msg-owner-{session_id}"
        expected.append(saver.admit_internal_execution(
            session_id,
            thread_id=thread_id,
            job_id="job-reused-across-owner",
            intent=InternalExecutionIntent(
                message_id=message_id,
                execution_id=f"execution-{session_id}",
                admission_idempotency_key=f"admission-{session_id}",
            ),
            message=_internal_notice(message_id),
        ))
    for admission in expected:
        assert admission.turn_id is None
        assert saver.execution_admission_for_job(
            admission.session_id,
            thread_id=admission.thread_id,
            job_id=admission.job_id,
        ) == admission
