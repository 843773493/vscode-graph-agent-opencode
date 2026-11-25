"""真实 Saver 创建来源后，候选冻结不得改变视图、吞掉追加或混用身份。"""

from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.model_call_execution import ModelCallExecution
from app.agents.summarization.generation import (
    CompactionGenerator,
    SummaryCallBudget,
    _Sources,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
    CanonicalTextRange,
    FrozenCompactionRange,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.durable.compaction_candidates import (
    _normalize_coverage,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    generation_case as generation_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_compaction_generation import (
    summary_model as summary_model,  # noqa: PLC0414
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 原真实来源准备装具
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_sibling as production_sibling,  # noqa: PLC0414 - 真实独立 Thread
)
from tests.support.rollout.context_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 显式 fixture 注入
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def candidate_case(production_context):
    saver, session, thread, connection, manager, prepare = production_context
    prepared = prepare("candidate-sources")
    refs = [entry.ref for entry in prepared["snapshot"].selection
            if entry.included and entry.ref.ref_type == "canonical_item"]
    assert refs
    frozen = saver._storage.compactions.freeze(
        session, thread, candidate_id="candidate-1", task_id="task-1",
        item_ids=[ref.ref_id for ref in refs], model_config_hash=sha256_jcs({"provider": "frozen"}),
    )
    return saver, session, thread, connection, manager, prepared, frozen


def test_discarded_candidate_cannot_accept_manual_application_intent(candidate_case):
    saver, session, thread, connection, _manager, _prepared, frozen = candidate_case
    candidates = saver._storage.compactions
    candidates.discard(session, thread, frozen.candidate_id, reason="task-failed")
    with pytest.raises(ValueError, match="compaction-candidate-unavailable.*task-failed"):
        candidates.request_application(session, thread, frozen.task_id)
    assert connection.execute("SELECT apply_on_next_request FROM context_compaction_candidates WHERE candidate_id=?",
                              (frozen.candidate_id,)).fetchone() == (0,)


def test_frozen_candidate_is_idempotent_and_cold_readable_without_changing_current_context(candidate_case):
    saver, session, thread, connection, _manager, prepared, frozen = candidate_case
    owner = saver._storage.compactions
    before = connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall()
    rows = connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()
    assert owner.freeze(session, thread, candidate_id=frozen.candidate_id, task_id=frozen.task_id,
                        item_ids=[ref.ref_id for ref in frozen.refs], model_config_hash=frozen.model_config_hash) == frozen
    assert FrozenCompactionRange.from_dict(frozen.to_dict()) == frozen
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)._storage.compactions
    assert cold.read(session, thread, frozen.candidate_id) == frozen
    assert cold.applicability(session, thread, frozen.candidate_id) is None
    assert cold.read_result(session, thread, frozen.candidate_id) is None
    assert connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone() == rows
    assert connection.execute("SELECT head_view_id,head_checkpoint_id FROM branches WHERE status='active'").fetchall() == before
    assert connection.execute("SELECT target_view_id,status FROM retention_refs WHERE reference_id=?", (frozen.candidate_id,)).fetchone() == (prepared["snapshot"].active_view_id, "active")


def test_append_and_unread_source_changes_preserve_frozen_candidate(candidate_case):
    saver, session, thread, _connection, manager, _prepared, frozen = candidate_case
    saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="candidate-tail", acceptance_idempotency_key="candidate-tail-accept",
        job_id="candidate-tail-job", admission_idempotency_key="candidate-tail-admit", payload="追加的新输入", payload_kind="text",
    )
    manager.observe("instruction:R01", "候选没有读取的规则第二版", revision="instructions:2")
    assert saver._storage.compactions.applicability(session, thread, frozen.candidate_id) is None


def test_candidate_identity_cannot_be_reused_for_another_model_or_task(candidate_case):
    saver, session, thread, _connection, _manager, _prepared, frozen = candidate_case
    for task, model in (("another-task", frozen.model_config_hash), (frozen.task_id, sha256_jcs({"provider": "other"}))):
        with pytest.raises(ValueError, match="compaction-candidate-conflict"):
            saver._storage.compactions.freeze(session, thread, candidate_id=frozen.candidate_id, task_id=task,
                                              item_ids=[ref.ref_id for ref in frozen.refs], model_config_hash=model)


def test_discard_releases_only_its_retention_and_cannot_be_resurrected(candidate_case):
    saver, session, thread, connection, _manager, _prepared, frozen = candidate_case
    owner = saver._storage.compactions
    owner.discard(session, thread, frozen.candidate_id, reason="user-cancelled")
    owner.discard(session, thread, frozen.candidate_id, reason="later-reason")
    assert owner.applicability(session, thread, frozen.candidate_id) == "user-cancelled"
    assert connection.execute("SELECT status FROM retention_refs WHERE reference_id=?", (frozen.candidate_id,)).fetchone() == ("released",)
    assert owner.read_result(session, thread, frozen.candidate_id) is None
    assert owner.read(session, thread, frozen.candidate_id) == frozen
    status = owner.current_status(session, thread)
    assert status["candidate_id"] == frozen.candidate_id and status["task_id"] == frozen.task_id
    assert status["status"] == "discarded" and status["reason"] == "user-cancelled"


def test_unknown_candidate_does_not_materialize_a_second_thread(candidate_case):
    saver, session, thread, _connection, _manager, _prepared, _frozen = candidate_case
    with pytest.raises(KeyError, match="compaction-candidate-unavailable"):
        saver._storage.compactions.read(session, thread, "does-not-exist")


def test_status_of_new_session_has_no_rollout_and_does_not_read_another_owner(candidate_case, session_bundle_factory, production_sibling):
    saver, session, thread, _connection, _manager, _prepared, frozen = candidate_case
    other = f"ses_{create_uuid_hex()}"
    session_bundle_factory(saver._storage.storage.sessions_dir, other)
    other_thread = saver._storage.storage.main_thread_id(other)
    assert saver._storage.compactions.current_status(other, other_thread) is None
    assert saver._storage.compactions.current_status(session, production_sibling[1]) is None
    assert saver._storage.compactions.current_status(session, thread)["candidate_id"] == frozen.candidate_id


def test_only_actually_read_source_revision_invalidates_candidate(candidate_case):
    saver, session, thread, _connection, manager, _prepared, frozen = candidate_case
    owner = saver._storage.compactions
    source = owner.freeze(
        session, thread, candidate_id="source-reader", task_id="source-task",
        item_ids=[ref.ref_id for ref in frozen.refs], model_config_hash=frozen.model_config_hash,
        source_versions=(("instruction:R01", "instructions:1"),),
    )
    assert owner.applicability(session, thread, source.candidate_id) is None
    manager.observe("instruction:R01", "实际读取的来源已更新", revision="instructions:2")
    assert owner.applicability(session, thread, source.candidate_id) == "source-version-changed"
    assert owner.applicability(session, thread, frozen.candidate_id) is None


def test_freeze_rejects_reordered_or_noncontiguous_real_selection(candidate_case):
    saver, session, thread, _connection, _manager, _prepared, frozen = candidate_case
    ids = [ref.ref_id for ref in frozen.refs]
    for number in range(2):
        accepted = saver.accept_turn(
            session, thread_id=thread, accepted_ingress_id=f"range-tail-{number}",
            acceptance_idempotency_key=f"range-tail-accept-{number}", job_id=f"range-tail-job-{number}",
            admission_idempotency_key=f"range-tail-admit-{number}", payload=f"追加{number}", payload_kind="text",
        )
        ids.append(accepted["root_input_item_id"])
    for number, selection in enumerate((list(reversed(ids)), [ids[0], ids[-1]])):
        with pytest.raises(ValueError, match="source-selection-changed"):
            saver._storage.compactions.freeze(
                session, thread, candidate_id=f"bad-range-{number}", task_id=f"bad-task-{number}",
                item_ids=selection, model_config_hash=frozen.model_config_hash,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
@pytest.mark.parametrize("late_checkpoint", [False, True], ids=["checkpoint-before-freeze", "checkpoint-after-ready"])
async def test_stream_and_checkpoint_carriers_share_one_frozen_range_and_atomic_adoption(
    generation_case, production_context, frozen_provider_wire, frozen_provider_activation, late_checkpoint,
):
    saver, session, thread, connection, model, controller, activation, _freeze = generation_case
    admission = saver.execution_admission(session, thread_id=thread, execution_id=connection.execute(
        "SELECT execution_id FROM executions WHERE job_id='registry-job'",
    ).fetchone()[0])
    owner = MutationIntentOwner(session, thread)
    text = "必须保留的历史目标与边界。" * 100
    stream = CanonicalBlockAccumulator(turn_id=admission.turn_id, execution_id=admission.execution_id,
        producer_id="carrier-model", model_call_id="carrier-model")
    stream.accept({"block_id": "carrier-model:block:part-0", "block_index": 0, "carrier_type": "text", "text": text})
    shadow, = stream.finalize(first_item_sequence=2)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        root, = saver.read_canonical_items(session, thread)
        saver.append_items(session, (shadow,), thread_id=thread)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "dual-carrier-checkpoint"
        checkpoint["channel_values"] = {"messages": [
            saver._storage.storage._codec().from_dict(saver._storage.storage._codec().project_message((root,))),
            AIMessage(id="checkpoint-carrier", content=text, response_metadata={
                "model_call_id": "carrier-model", "content_part_refs": [{"id": "part-0", "index": 0}],
            }),
        ]}
        checkpoint["channel_versions"] = {"messages": "1"}
        if not late_checkpoint:
            config = saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
        with saver._context_reader.open_snapshot(session, thread) as read:
            items = saver._storage.checkpoints.committed_context_items(read, predecessor=None, include_pending_notices=False)
            view = saver._storage.checkpoints.active_view_id(read)
        assert len(items) == 2 and (shadow.item_id in {item.item_id for item in items}) == late_checkpoint
        assert connection.execute("SELECT visible,context_selected FROM context_view_items WHERE view_id=? AND item_id=?",
            (view, shadow.item_id)).fetchone() == (1, 1)
        candidate = saver._storage.compactions.freeze(session, thread, candidate_id="dual-carrier-range", task_id="dual-carrier-task",
            item_ids=tuple(item.item_id for item in items), model_config_hash=sha256_jcs({"model": "frozen"}))
    result = await CompactionGenerator(saver=saver, model=model, activation=activation).generate(
        candidate, budget=SummaryCallBudget(time.time() + 60),
    )
    assert result.coverage == candidate.refs and controller.transport.hit_counts == {0: 1}
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        if late_checkpoint:
            saver.put(build_checkpoint_config(thread), checkpoint, {"source": "loop", "step": 0}, {"messages": "1"})
            selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
            prepared = saver.freeze_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key="late-carrier:create", seal_idempotency_key="late-carrier:seal",
                context_source_manager=production_context[4], tool_set_supplier=lambda: selected,
                **frozen_provider_wire("production-provider", "chat_completions"),
                **frozen_provider_activation(saver, plan_creation_idempotency_key="late-carrier:create"),
            )
            with pytest.raises(ValueError, match="compaction-coverage-mismatch"):
                prepared.simulate_candidate(candidate, result)
            assert saver._storage.compactions.applicability(session, thread, candidate.candidate_id) == "source-selection-changed"
            assert saver._storage.compactions.latest_ready(session, thread) is None
            assert controller.transport.hit_counts == {0: 1}
            return
        view = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        adopted = saver.adopt_candidate(config, ApplyCompactionCandidateIntent(candidate, view))
    assert adopted.checkpoint["channel_values"]["messages"][0].id == f"summary-{candidate.candidate_id}"
    applied = connection.execute("SELECT applied_view_id FROM context_compaction_candidates WHERE candidate_id=?", (candidate.candidate_id,)).fetchone()[0]
    assert connection.execute("SELECT item_id FROM context_view_items WHERE view_id=? AND context_selected=1 ORDER BY context_ordinal",
        (applied,)).fetchall() == [(f"item-summary-{candidate.candidate_id}",)]
    assert connection.execute("SELECT visible FROM context_view_items WHERE view_id=? AND item_id=?", (applied, shadow.item_id)).fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_model", [("basic-text", "chat_completions", "openai", "/chat/completions")], indirect=True)
async def test_real_partial_head_and_tail_must_merge_before_candidate_can_be_ready(generation_case):
    saver, session, thread, _connection, model, _controller, activation, freeze = generation_case
    full = freeze(count=1, payload_size=400)
    owner = saver._storage.compactions
    ref = full.refs[-1]
    candidate = owner.freeze(session, thread, candidate_id="partial-coverage", task_id="partial-task",
                             item_ids=(ref.ref_id,), model_config_hash=full.model_config_hash)
    item, = saver.read_canonical_items(session, thread, item_ids=(ref.ref_id,))
    length = len(item.payload)
    generator = CompactionGenerator(saver=saver, model=model, activation=activation)
    execution = ModelCallExecution(checkpointer=saver, session_id=session, thread_id=thread)
    budget = SummaryCallBudget(time.time() + 60)
    results = []
    for ordinal, (start, end) in enumerate(((0, length // 3), (length // 3, 2 * length // 3), (2 * length // 3, length))):
        part = CanonicalTextRange(ref, start, end, length)
        request = generator._plan(candidate, execution, _Sources((item,), (ref,), text_range=part), stage="chunk", level=0, ordinal=ordinal)
        result = await generator._invoke(candidate, execution, request, budget, False)
        assert result.coverage == (ref,) and result.text_ranges == (part,)
        assert set(result.detail()) == {"output_refs", "dispatch_ref", "coverage"}
        with pytest.raises(ValueError, match="实际摘要输入未按顺序完整覆盖"):
            owner.publish_result(candidate, output_refs=result.output_refs, dispatch_ref=result.dispatch_ref)
        assert owner.read_result(session, thread, candidate.candidate_id) is None
        results.append(result)
    request = generator._plan(candidate, execution, _Sources(summaries=tuple(results[:2])), stage="merge", level=1, ordinal=0)
    partial = await generator._invoke(candidate, execution, request, budget, False)
    assert partial.coverage == (ref,) and partial.text_ranges == (CanonicalTextRange(ref, 0, 2 * length // 3, length),)
    with pytest.raises(ValueError, match="实际摘要输入未按顺序完整覆盖"):
        owner.publish_result(candidate, output_refs=partial.output_refs, dispatch_ref=partial.dispatch_ref)
    request = generator._plan(candidate, execution, _Sources(summaries=(partial, results[2])), stage="merge", level=2, ordinal=0)
    merged = await generator._invoke(candidate, execution, request, budget, False)
    assert merged.coverage == (ref,) and merged.text_ranges == ()
    assert owner.publish_result(candidate, output_refs=merged.output_refs, dispatch_ref=merged.dispatch_ref) == merged.result_ref
    assert owner.read_result(session, thread, candidate.candidate_id) == merged
    assert budget.calls_used == 5


@pytest.mark.parametrize("next_start", [4, 6])
def test_partial_coverage_rejects_overlap_and_gap(candidate_case, next_start):
    ref = candidate_case[-1].refs[0]
    with pytest.raises(ValueError, match="有序、无缝且不重叠"):
        _normalize_coverage(((ref, CanonicalTextRange(ref, 0, 5, 10)),
                             (ref, CanonicalTextRange(ref, next_start, 10, 10))))


def test_partial_coverage_cannot_return_to_an_earlier_original(candidate_case):
    refs = candidate_case[-1].refs
    # 单原件 fixture 也能证明完整ref后不得再次声称另一个片段。
    with pytest.raises(ValueError, match="有序、无缝且不重叠"):
        _normalize_coverage(((refs[0], None), (refs[0], CanonicalTextRange(refs[0], 5, 10, 10))))
