"""执行 admission 的 SQLite owner 合同。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    internal_execution_request_preimage,
)
from app.domain.itemized.hashing import content_hash, sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ProjectionPartRef,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord, InternalDisplayProvenance
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.domain.itemized.serde.runtime import (
    execution_record_from_dict,
    execution_record_to_dict,
)
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


@pytest.mark.parametrize("timestamps", [
    ("2026-10-04T12:00:02+00:00", "2026-10-04T12:00:01+00:00"),
    ("2026-10-04T12:00:01+00:00", "2026-10-04T12:00:01+00:00"),
])
def test_pending_runtime_wakeups_keep_admission_order_after_restart(
    saver: RolloutCheckpointSaver, timestamps: tuple[str, str],
) -> None:
    resolver = saver._storage.storage._path_resolver
    thread_id = resolver.main_thread_id(SESSION_ID)
    accepted = []
    with saver, closing(SessionControlStore(resolver.resolve_session_node(SESSION_ID) / "session-control.sqlite")) as control:
        for suffix, created_at in zip(("z-first", "a-second"), timestamps, strict=True):
            intent = InternalExecutionIntent(
                message_id=f"msg-{suffix}", execution_id=f"execution-{suffix}",
                admission_idempotency_key=f"admission-{suffix}",
            )
            lease = control.create_or_get_lease(
                operation_kind="context_control", operation_identity=intent.admission_idempotency_key,
                preimage_hash=sha256_jcs({"identity": suffix}).rsplit(":", 1)[1],
                recovery_ref=intent.execution_id,
            )
            lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
            wakeup = RuntimeExecutionWakeup(
                session_id=SESSION_ID, thread_id=thread_id, source_id=f"source-{suffix}",
                source_item_id=intent.notice_item_id, execution_id=intent.execution_id, job_id=f"job-{suffix}",
                admission_idempotency_key=intent.admission_idempotency_key, agent_id="default",
                message_created_at=created_at, gateway_id=None, request_id=None, lease_id=lease.lease_id,
                lifecycle_generation=lease.captured_lifecycle_generation, lease_fencing_token=lease.fencing_token,
            )
            message = _internal_notice(intent.message_id)
            message.response_metadata["created_at"] = created_at
            accepted.append(saver.commit_runtime_source_wakeup(
                wakeup, source_kind="generated_session_result", intent=intent, message=message,
            ))
            control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token, outcome="completed")
    sessions_dir = saver._storage.storage.sessions_dir
    with RolloutCheckpointSaver(sessions_dir=sessions_dir) as restarted:
        assert restarted.pending_runtime_wakeups(SESSION_ID, thread_id) == tuple(accepted)
        with restarted._storage.storage.read_connection(SESSION_ID, thread_id, "") as connection:
            cursors = [restarted._storage.executions.execution_admission_cursor(
                restarted.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=wakeup.execution_id),
                connection=connection,
            ).commit_id for wakeup in accepted]
            assert cursors[0] < cursors[1]
        for wakeup in accepted:
            generation = restarted.claim_runtime_wakeup(wakeup)
            restarted.finish_runtime_wakeup(wakeup, dispatch_generation=generation, receipt={"started": True})
    with RolloutCheckpointSaver(sessions_dir=sessions_dir) as restarted:
        # dispatch generation/CAS 与 Job started 不能释放 canonical unknown owner。
        assert restarted.pending_runtime_wakeups(SESSION_ID, thread_id) == tuple(accepted)
        cancelled = accepted[0]
        restarted.converge_execution(
            SESSION_ID, thread_id=thread_id, turn_id=None, execution_id=cancelled.execution_id,
            outcome="cancelled", turn_status=None,
        )
    with RolloutCheckpointSaver(sessions_dir=sessions_dir) as restarted:
        assert restarted.pending_runtime_wakeups(SESSION_ID, thread_id) == (accepted[1],)
        assert restarted.runtime_wakeup_before_provider(
            SESSION_ID, thread_id, execution_id=cancelled.execution_id, job_id=cancelled.job_id,
            admission_idempotency_key=cancelled.admission_idempotency_key,
        ) is None


def test_internal_admission_creates_turnless_execution_and_reuses_exact_identity(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    message = _internal_notice("msg-internal-admission")
    intent = InternalExecutionIntent(
        message_id="msg-internal-admission",
        execution_id="execution-internal-1",
        admission_idempotency_key="team-task:event-1",
    )
    expected_notice = saver._storage.storage._codec().items_for_message(
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
    assert asdict(first) == {
        "session_id": SESSION_ID, "thread_id": thread_id,
        "execution_id": expected.execution_id, "job_id": expected.job_id,
        "admission_idempotency_key": intent.admission_idempotency_key,
        "execution_origin": "internal", "turn_id": None,
    }
    assert saver.execution_admission(
        SESSION_ID,
        thread_id=thread_id,
        execution_id=expected.execution_id,
    ) == expected
    assert saver.execution_admission_for_job(
        SESSION_ID, thread_id=thread_id, job_id=expected.job_id
    ) == expected
    with sqlite3.connect(
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
    ) as db:
        row = db.execute(
            "SELECT turn_id, accepted_ingress_id, execution_origin, job_id, "
            "admission_idempotency_key, admission_payload_hash "
            "FROM executions WHERE execution_id = ?",
            (expected.execution_id,),
        ).fetchone()
        db.row_factory = sqlite3.Row
        execution_row = dict(db.execute(
            "SELECT session_id, thread_id, execution_id, turn_id, attempt, outcome, "
            "resumed_from_execution_id FROM executions WHERE execution_id = ?",
            (expected.execution_id,),
        ).fetchone())
    execution = execution_record_from_dict(execution_row)
    assert execution.turn_id is None
    assert execution_record_to_dict(execution) == execution_row
    assert row == (
        None,
        None,
        "internal",
        expected.job_id,
        expected.admission_idempotency_key,
        sha256_jcs(internal_execution_request_preimage(
            session_id=SESSION_ID,
            thread_id=thread_id,
            execution_id=expected.execution_id,
            job_id=expected.job_id,
            admission_idempotency_key=expected.admission_idempotency_key,
            request_input={"kind": "runtime_notice", "payload": expected_notice.payload},
        )),
    )
    item = saver._storage.storage.read_items(
        SESSION_ID,
        thread_id,
        item_ids=(intent.notice_item_id,),
    )[0]
    assert item.item_id == intent.notice_item_id
    with sqlite3.connect(
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
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
        notice_projection = db.execute(
            "SELECT message.message_id, message.turn_id, message.execution_id, "
            "message.visibility, message.message_sequence, commit_record.first_message_sequence, "
            "commit_record.last_message_sequence, meta.last_message_sequence "
            "FROM item_catalog AS item JOIN messages AS message "
            "ON message.jsonl_offset = item.jsonl_offset AND message.jsonl_length = item.jsonl_length "
            "JOIN storage_commits AS commit_record "
            "ON commit_record.commit_id = item.commit_id AND message.commit_id = commit_record.commit_id "
            "CROSS JOIN database_meta AS meta WHERE item.item_id = ?",
            (intent.notice_item_id,),
        ).fetchall()
    assert item_row == (None, "pending_next_turn", expected.execution_id)
    assert membership is not None and membership[0] >= 0
    assert membership[1:] == (1, "display_only")
    assert len(notice_projection) == 1
    projection = notice_projection[0]
    assert projection[:4] == (intent.message_id, None, expected.execution_id, "internal")
    assert projection[4] > 0
    assert projection[4:] == (projection[4],) * 4


def test_internal_admission_rejects_key_replay_with_changed_preimage(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(
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


@pytest.mark.parametrize("late_shadow", [False, True])
@pytest.mark.parametrize("shadow_matches_final", [False, True])
def test_execution_output_excludes_typed_shadows_after_restart(
    saver: RolloutCheckpointSaver, late_shadow: bool, shadow_matches_final: bool,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(SESSION_ID)
    intent = InternalExecutionIntent(
        message_id="msg-output", execution_id="execution-output",
        admission_idempotency_key="generated-session:output",
    )
    content = "不同模型调用可以产生相同正文。"
    items: list[CanonicalItemRecord] = []
    for model_call_id in ("model-first", "model-second"):
        checkpoint_id = f"checkpoint-{model_call_id}"
        carriers = (
            ("stream", ItemProjectionIdentity(
                model_call_id=model_call_id, block_id=f"{model_call_id}:block:part-0", block_index=0,
            ), ()),
            ("checkpoint", ItemProjectionIdentity(
                model_call_id=model_call_id, projection_message_id=checkpoint_id,
            ), ()),
            ("final", ItemProjectionIdentity(
                model_call_id=model_call_id, projection_message_id=f"final-{model_call_id}",
                content_part_refs=(ProjectionPartRef("part-0", 0, "text"),),
            ), (ItemProvenanceRelation(
                relation="replaces", target_kind="projection_message", target_id=checkpoint_id,
                target_model_call_id=model_call_id,
            ),)),
        )
        items.extend(CanonicalItemRecord.create(
            item_sequence=1, item_id=f"item-{kind}-{model_call_id}",
            semantic_kind="assistant_output", payload_kind="text", status="completed",
            producer_ref={"producer_kind": "provider", "producer_id": model_call_id,
                          "invocation_id": model_call_id},
            payload=content if kind == "final" or shadow_matches_final else f"不得进入输出的 {kind} shadow",
            created_at="2026-10-16T00:00:00+00:00", wire_role="assistant",
            projection_identity=identity, provenance_relations=relations,
        ) for kind, identity, relations in carriers)
    if late_shadow:
        # 最后落盘的 shadow 不能覆盖最终保留结果的来源引用。
        items.append(items.pop(0))
    expected = (f"{content}\n{content}", InternalDisplayProvenance(
        source_session_id=SESSION_ID, source_thread_id=thread_id,
        source_execution_id=intent.execution_id, source_item_id="item-final-model-second",
    ))
    sessions_dir = saver._storage.storage.sessions_dir
    with saver:
        admission = saver.admit_internal_execution(
            SESSION_ID, thread_id=thread_id, job_id="job-output", intent=intent,
            message=_internal_notice(intent.message_id),
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            saver.append_items(SESSION_ID, items, thread_id=thread_id)
        assert saver.execution_output(SESSION_ID, thread_id, intent.execution_id) == expected
        assert len(saver._storage.storage.read_items(
            SESSION_ID, thread_id, item_ids=tuple(item.item_id for item in items),
        )) == 6
    with RolloutCheckpointSaver(sessions_dir=sessions_dir) as restarted:
        assert restarted.execution_output(SESSION_ID, thread_id, intent.execution_id) == expected
        assert restarted.execution_admission(
            SESSION_ID, thread_id=thread_id, execution_id=intent.execution_id,
        ) == admission


def test_internal_execution_lost_converges_without_creating_a_turn(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(
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
        saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
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
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(
        SESSION_ID
    )
    payload = "accepted user payload"
    accepted = saver.accept_turn(
        SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(SESSION_ID), accepted_ingress_id="ingress-user-admission",
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
    thread_id = get_session_path_resolver(saver._storage.storage.sessions_dir).main_thread_id(
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
    session_bundle_factory(saver._storage.storage.sessions_dir, other_session)
    resolver = get_session_path_resolver(saver._storage.storage.sessions_dir)
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
