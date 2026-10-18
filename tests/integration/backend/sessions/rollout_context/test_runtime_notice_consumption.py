"""真实 admission→prepare→seal→冷恢复的 notice 消费闭包。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import EpochReason
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.hashing import canonical_json_bytes, content_hash, sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ProjectionPartRef,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)


@dataclass
class NoticeCase:
    saver: RolloutCheckpointSaver
    session: str
    thread: str
    sessions: Path
    wire: Callable[..., dict[str, object]]
    activation: Callable[..., dict[str, object]]

    @contextmanager
    def scope(self, admission):
        with (
            session_thread_owner_scope(MutationIntentOwner(self.session, self.thread), langgraph_thread_id=self.thread),
            execution_admission_scope(admission, langgraph_thread_id=self.thread),
        ):
            yield

    def internal(self, key):
        intent = InternalExecutionIntent(message_id=f"msg-{key}", execution_id=f"execution-{key}",
                                         admission_idempotency_key=f"admission-{key}")
        notice = internal_message_factory.build(
            kind="generated_session_result", control=f"私有提示 {key}",
            sections=(PromptSection("control_context", {"source": key}),
                      PromptSection("generated_session_result", f"私有结果 {key}")),
            display_content=f"任务 {key} 已完成。",
        )
        message = HumanMessage(content=notice.content, id=intent.message_id, response_metadata={
            "message_id": intent.message_id, "created_at": "2026-10-16T00:00:00+00:00",
            "message_metadata": notice.metadata,
        })
        return self.saver.admit_internal_execution(self.session, thread_id=self.thread, job_id=f"job-{key}",
                                                   intent=intent, message=message), intent

    def user(self, key):
        accepted = self.saver.accept_turn(self.session, accepted_ingress_id=f"ingress-{key}",
            acceptance_idempotency_key=f"acceptance-{key}", job_id=f"job-{key}",
            admission_idempotency_key=f"admission-{key}", payload="真正的用户输入")
        return accepted, self.saver.execution_admission(self.session, thread_id=self.thread,
                                                       execution_id=accepted["initial_execution_id"])

    def prepare(self, admission, key, *, manager=None) -> ContextAssemblySnapshot:
        with self.scope(admission):
            prepared = self.saver.prepare_context_for_provider(
                self.session, self.thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                context_source_manager=manager if manager is not None else ContextSourceManager(
                    owner=ContextSourceOwnerKey(self.session, self.thread), control_state_port=self.saver),
                tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
                **self.wire("notice-fixture", "chat_completions"),
                **self.activation(self.saver, plan_creation_idempotency_key=key),
            )
            snapshot = prepared["snapshot"]
            with self.saver._storage.storage.read_connection(self.session, self.thread, "") as connection:
                attempt = connection.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM model_calls WHERE execution_id=?",
                                             (admission.execution_id,)).fetchone()[0]
            self.saver.register_model_call(self.session, thread_id=self.thread, execution_id=admission.execution_id,
                model_call_id=snapshot.model_call_id, attempt=attempt, provider="notice-fixture", assembly_id=snapshot.assembly_id)
            return snapshot

    def output(self, admission, key):
        item = CanonicalItemRecord.create(
            item_sequence=1, item_id=f"item-private-{key}", semantic_kind="assistant_output", payload_kind="text",
            status="completed", payload=f"私有输出 {key}", wire_role="assistant",
            producer_ref={"producer_kind": "provider", "producer_id": key, "invocation_id": admission.execution_id},
            projection_identity=ItemProjectionIdentity(projection_message_id=f"private-{key}"),
        )
        with self.scope(admission):
            self.saver.append_items(self.session, (item,), thread_id=self.thread)
        return item.item_id

    def metadata(self, accepted):
        with self.saver._storage.storage.read_connection(self.session, self.thread, "") as connection:
            return json.loads(connection.execute("SELECT metadata_json FROM storage_commits WHERE commit_id=?",
                                                (accepted["commit_id"],)).fetchone()[0])

    def rows(self):
        with self.saver._storage.storage.read_connection(self.session, self.thread, "") as connection:
            return connection.execute("SELECT reason,source_checkpoint_id,control_sequence,canonical_cut_kind,"
                "notice_consumption_acceptance_id,notice_selection_hash,status,consumed_assembly_id "
                "FROM context_epoch_transitions ORDER BY rowid").fetchall()

    def restart(self):
        self.saver = RolloutCheckpointSaver(self.sessions)


@pytest.fixture
def notice_case(request, session_bundle_factory, frozen_provider_wire, frozen_provider_activation):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session)
    saver = RolloutCheckpointSaver(sessions)
    return NoticeCase(saver, session, saver._storage.storage.main_thread_id(session), sessions,
                      frozen_provider_wire, frozen_provider_activation)


def _wire_ids(snapshot):
    return tuple(frame.item_ref.ref_id for frame in snapshot.seal_binding.prefix.state.frames
                 if frame.item_ref.ref_type == "canonical_item")


def test_acceptance_freezes_ordered_notice_outputs_cut_and_replay(notice_case):
    case = notice_case
    internal, intent = case.internal("old")
    output_id = case.output(internal, "old")
    first, admission = case.user("first")
    frozen = case.metadata(first)
    selected = frozen["runtime_notice_selection"]
    assert [item["notice_item_id"] for item in selected["notices"]] == [intent.notice_item_id]
    assert selected["notices"][0]["private_output_item_ids"] == [output_id]
    assert selected["notices"][0]["execution_id"] == internal.execution_id
    assert first["admission_payload_hash"] != selected["selection_hash"]
    assert content_hash("text", "真正的用户输入") != selected["selection_hash"]
    late_output = case.output(internal, "late")
    _late_admission, late_intent = case.internal("late")
    replay, repeated_admission = case.user("first")
    assert replay["idempotent"] and repeated_admission == admission
    assert case.metadata(replay) == frozen
    second, _ = case.user("second")
    later = case.metadata(second)["runtime_notice_selection"]
    assert [item["notice_item_id"] for item in later["notices"]] == [intent.notice_item_id, late_intent.notice_item_id]
    assert later["notices"][0]["private_output_item_ids"] == [output_id, late_output]
    assert case.rows() == []
    sealed = case.prepare(admission, "first-user-with-late-admission")
    assert {intent.notice_item_id, output_id, late_output, late_intent.notice_item_id}.isdisjoint(_wire_ids(sealed))
    assert sealed.seal_binding.notice_acceptance.selection_hash == selected["selection_hash"]
    assert case.metadata(first) == frozen
    case.restart()
    with case.scope(admission), case.saver._storage.storage.read_connection(case.session, case.thread, "") as connection:
        view_id = connection.execute("SELECT head_view_id FROM branches WHERE status='active'").fetchone()[0]
        restored = case.saver._storage.checkpoints._messages_for_view(
            case.session, case.thread, "", view_id, connection=connection, execution_fenced=True,
        )
    assert all(message.id not in {intent.message_id, late_intent.message_id, "private-old", "private-late"} for message in restored)


def test_continuous_internal_prefix_then_first_user_removes_private_wire_once(notice_case):
    case = notice_case
    internal_a, intent_a = case.internal("a")
    first = case.prepare(internal_a, "internal-a-first")
    output_a = case.output(internal_a, "a")
    with_output = case.prepare(internal_a, "internal-a-output")
    internal_b, intent_b = case.internal("b")
    second = case.prepare(internal_b, "internal-b")
    assert second.seal_binding.prefix.state.frames[:len(with_output.seal_binding.prefix.state.frames)] == with_output.seal_binding.prefix.state.frames
    assert {intent_a.notice_item_id, output_a, intent_b.notice_item_id}.issubset(_wire_ids(second))
    output_b = case.output(internal_b, "b")
    last_internal = case.prepare(internal_b, "internal-b-output")
    old_sealed_bytes = canonical_json_bytes(last_internal.to_dict())
    accepted, user = case.user("user")
    frozen = case.metadata(accepted)
    case.restart()
    sealed = case.prepare(user, "user-first")
    assert sealed.seal_binding.prefix.state.epoch.reason == EpochReason.RUNTIME_NOTICE_CONSUMED
    assert sealed.seal_binding.prefix.state.epoch.ordinal == first.seal_binding.prefix.state.epoch.ordinal + 1
    removed = {intent_a.notice_item_id, intent_b.notice_item_id, output_a, output_b}
    assert removed.isdisjoint(_wire_ids(sealed))
    assert removed.issubset(_wire_ids(last_internal))
    assert case.rows() == [("runtime_notice_consumed", None, None, None, accepted["commit_id"],
                           frozen["runtime_notice_selection"]["selection_hash"], "consumed", sealed.assembly_id)]
    again = case.prepare(user, "user-second")
    assert again.seal_binding.prefix.state.epoch == sealed.seal_binding.prefix.state.epoch
    assert len(case.rows()) == 1
    overlapping, next_user = case.user("queued")
    assert case.metadata(overlapping)["runtime_notice_selection"]["notices"] == frozen["runtime_notice_selection"]["notices"]
    next_seal = case.prepare(next_user, "queued-first")
    assert next_seal.seal_binding.prefix.state.epoch == sealed.seal_binding.prefix.state.epoch
    assert next_seal.seal_binding.notice_acceptance.acceptance_commit_id == overlapping["commit_id"]
    assert next_seal.seal_binding.notice_acceptance.selection_hash == case.metadata(overlapping)["runtime_notice_selection"]["selection_hash"]
    assert next_seal.seal_binding.prefix.consumed_transition is None
    assert len(case.rows()) == 1
    with case.saver._storage.storage.read_connection(case.session, case.thread, "") as connection:
        assert {row[0] for row in connection.execute("SELECT item_id FROM item_catalog WHERE item_id IN (?,?,?,?)", tuple(removed))} == removed
        assert all(row[0] == "display_only" for row in connection.execute(
            "SELECT source_kind FROM context_view_items WHERE item_id IN (?,?,?,?)", tuple(removed)))
    assert case.saver.load_context_source_rebuild_state(ContextSourceOwnerKey(case.session, case.thread)) is None
    case.saver.converge_execution(case.session, thread_id=case.thread, turn_id=user.turn_id,
        execution_id=user.execution_id, outcome="cancelled", turn_status="cancelled")
    assert case.rows()[0][-1] == sealed.assembly_id
    assert canonical_json_bytes(case.saver.get_context_assembly(
        case.session, case.thread, assembly_id=last_internal.assembly_id,
    ).to_dict()) == old_sealed_bytes
    with pytest.raises(ValueError, match="turn_not_resumable"):
        case.saver.resume_turn(case.session, thread_id=case.thread, job_id="resume-cancelled", turn_id=user.turn_id)


def test_seal_failure_then_unknown_restart_retains_acceptance_owner(notice_case):
    case = notice_case
    internal, _intent = case.internal("recover")
    case.prepare(internal, "recover-internal")
    accepted, user = case.user("recover-user")
    frozen = case.metadata(accepted)
    with sqlite3.connect(case.saver._storage.storage.index_path(case.session, thread_id=case.thread)) as connection:
        connection.execute("CREATE TRIGGER refuse_notice_consumption BEFORE INSERT ON context_epoch_transitions "
            "WHEN NEW.reason='runtime_notice_consumed' BEGIN SELECT RAISE(ABORT,'notice-seal-rejected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="notice-seal-rejected"):
        case.prepare(user, "recover-user-failed")
    assert case.rows() == []
    assert case.saver.runtime_execution_outcome(case.session, case.thread, user.execution_id) == "unknown"
    case.restart()
    assert case.user("recover-user")[0]["idempotent"]
    assert case.metadata(accepted) == frozen
    with sqlite3.connect(case.saver._storage.storage.index_path(case.session, thread_id=case.thread)) as connection:
        connection.execute("DROP TRIGGER refuse_notice_consumption")
    sealed = case.prepare(user, "recover-user-retry")
    assert case.rows()[0][4] == accepted["commit_id"]
    assert case.rows()[0][-1] == sealed.assembly_id
    with case.saver._storage.storage.read_connection(case.session, case.thread, "") as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies WHERE execution_id=?", (user.execution_id,)).fetchone() == (1,)


@pytest.mark.parametrize("reason", ["rewind", "compaction"])
def test_source_boundary_keeps_primary_reason_and_confirms_notice_in_same_seal(notice_case, reason):
    case = notice_case
    internal, _intent = case.internal("source")
    case.prepare(internal, "source-internal")
    accepted, user = case.user("source-user")
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-source-boundary"
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="真正的用户输入", id=accepted["root_input_item_id"].removeprefix("item-"))]}
    if reason == "compaction":
        checkpoint["channel_values"]["_summarization_event"] = {
            "cutoff_index": 1, "cutoff_message_id": checkpoint["channel_values"]["messages"][-1].id,
            "summary_message": HumanMessage(content="已完成的对话摘要", id="summary-notice-source"),
        }
    checkpoint["channel_versions"] = {key: checkpoint["id"] for key in checkpoint["channel_values"]}
    with case.scope(user):
        config = case.saver.put({"configurable": {"thread_id": case.thread, "checkpoint_ns": ""}}, checkpoint,
            {"source": "compaction" if reason == "compaction" else "loop", "step": 1}, checkpoint["channel_versions"])
        if reason == "rewind":
            case.saver.rewind(config, checkpoint_id=checkpoint["id"])
    sealed = case.prepare(user, "source-user-first")
    assert sealed.seal_binding.prefix.state.epoch.reason.value == reason
    rows = case.rows()
    assert len(rows) == 1
    assert rows[0][0] == reason and rows[0][1] == checkpoint["id"] and rows[0][2] > 0
    assert rows[0][4] == accepted["commit_id"] and rows[0][-1] == sealed.assembly_id
    assert case.saver.load_context_source_rebuild_state(ContextSourceOwnerKey(case.session, case.thread)) is None


def test_v17_database_is_rejected_without_rewrite(notice_case):
    case = notice_case
    case.user("schema")
    path = case.saver._storage.storage.index_path(case.session, thread_id=case.thread)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE database_meta SET schema_version=17")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    case.restart()
    with pytest.raises(RuntimeError, match="schema-incompatible.*current=17, target=18"):
        case.saver.execution_admission_for_job(case.session, thread_id=case.thread, job_id="job-schema")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_three_typed_carriers_freeze_only_selected_private_output(notice_case):
    case = notice_case
    internal, intent = case.internal("carriers")
    initial = case.prepare(internal, "carriers-first")
    call_id = initial.model_call_id
    checkpoint_message_id = "checkpoint-shadow"
    identities = (
        ("stream", ItemProjectionIdentity(model_call_id=call_id, block_id=f"{call_id}:block:part-0", block_index=0), ()),
        ("checkpoint", ItemProjectionIdentity(model_call_id=call_id, projection_message_id=checkpoint_message_id), ()),
        ("final", ItemProjectionIdentity(model_call_id=call_id, projection_message_id="carrier-final",
            content_part_refs=(ProjectionPartRef("part-0", 0, "text"),)),
         (ItemProvenanceRelation(relation="replaces", target_kind="projection_message", target_id=checkpoint_message_id,
                                 target_model_call_id=call_id),)),
    )
    carriers = tuple(CanonicalItemRecord.create(
        item_sequence=1, item_id=f"item-carrier-{kind}", semantic_kind="assistant_output", payload_kind="text",
        status="completed", payload="相同的私有正文", wire_role="assistant",
        producer_ref={"producer_kind": "provider", "producer_id": call_id, "invocation_id": internal.execution_id},
        projection_identity=identity, provenance_relations=relations,
    ) for kind, identity, relations in identities)
    with case.scope(internal):
        case.saver.append_items(case.session, carriers, thread_id=case.thread)
    independent = case.output(internal, "independent")
    internal_seal = case.prepare(internal, "carriers-output")
    assert set(_wire_ids(internal_seal)) & {item.item_id for item in carriers} == {"item-carrier-final"}
    assert independent in _wire_ids(internal_seal)
    accepted, user = case.user("carriers-user")
    selection = case.metadata(accepted)["runtime_notice_selection"]
    assert selection["notices"][0]["private_output_item_ids"] == ["item-carrier-final", independent]
    sealed = case.prepare(user, "carriers-user-first")
    assert {intent.notice_item_id, *(item.item_id for item in carriers)}.isdisjoint(_wire_ids(sealed))
    assert independent not in _wire_ids(sealed)
    assert case.saver.execution_output(case.session, case.thread, internal.execution_id)[0] == "相同的私有正文\n私有输出 independent"


@pytest.mark.parametrize("hidden_column", ["visible", "context_selected"])
def test_acceptance_ignores_output_outside_frozen_head_selection(notice_case, hidden_column):
    case = notice_case
    internal, _intent = case.internal("membership")
    selected_id = case.output(internal, "selected")
    hidden_id = case.output(internal, "hidden")
    with sqlite3.connect(case.saver._storage.storage.index_path(case.session, thread_id=case.thread)) as connection:
        # 显式构造已提交 selection 的反例；仍经真实 acceptance 入口冻结。
        connection.execute(f"UPDATE context_view_items SET {hidden_column}=0 WHERE item_id=?", (hidden_id,))
    accepted, _user = case.user("membership-user")
    assert case.metadata(accepted)["runtime_notice_selection"]["notices"][0]["private_output_item_ids"] == [selected_id]


def test_empty_notice_readback_rejects_detached_source_branch_after_restart(notice_case):
    case = notice_case
    accepted, _admission = case.user("empty")
    metadata = case.metadata(accepted)
    selection = metadata["runtime_notice_selection"]
    assert selection["notices"] == []
    selection["source_branch_id"] = "branch-detached"
    selection["selection_hash"] = sha256_jcs({key: value for key, value in selection.items() if key != "selection_hash"})
    with sqlite3.connect(case.saver._storage.storage.index_path(case.session, thread_id=case.thread)) as connection:
        connection.execute("UPDATE storage_commits SET metadata_json=? WHERE commit_id=?", (canonical_json_bytes(metadata).decode(), accepted["commit_id"]))
    case.restart()
    with pytest.raises(RuntimeError, match="冻结 source view/branch 不一致"):
        case.user("empty")


def test_interrupted_resume_seals_original_acceptance_without_reselection(notice_case):
    case = notice_case
    internal, _intent = case.internal("resume")
    case.prepare(internal, "resume-internal")
    accepted, user = case.user("resume-user")
    frozen = case.metadata(accepted)
    with case.scope(user):
        case.activation(case.saver, plan_creation_idempotency_key="resume-original-preparation")
    case.saver.converge_execution(case.session, thread_id=case.thread, turn_id=user.turn_id,
        execution_id=user.execution_id, outcome="interrupted", turn_status="interrupted")
    case.restart()
    resumed = case.saver.resume_turn(case.session, thread_id=case.thread, job_id="job-resumed", turn_id=user.turn_id)
    resumed_admission = case.saver.execution_admission(case.session, thread_id=case.thread, execution_id=resumed["execution_id"])
    sealed = case.prepare(resumed_admission, "resume-user-seal")
    assert sealed.seal_binding.notice_acceptance.acceptance_commit_id == accepted["commit_id"]
    assert case.metadata(accepted) == frozen
    assert case.rows()[0][4] == accepted["commit_id"]


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
def test_terminal_owner_before_seal_cannot_consume_next_acceptance(notice_case, outcome):
    case = notice_case
    internal, _intent = case.internal("terminal")
    case.prepare(internal, "terminal-internal")
    abandoned, user = case.user("abandoned")
    case.saver.converge_execution(case.session, thread_id=case.thread, turn_id=user.turn_id,
        execution_id=user.execution_id, outcome=outcome, turn_status=outcome)
    case.restart()
    with pytest.raises((RuntimeError, ValueError)):
        case.prepare(user, "terminal-refused")
    assert case.rows() == []
    next_acceptance, next_admission = case.user("new-owner")
    sealed = case.prepare(next_admission, "new-owner-seal")
    assert case.rows()[0][4] == next_acceptance["commit_id"] != abandoned["commit_id"]
    assert sealed.seal_binding.notice_acceptance.acceptance_commit_id == next_acceptance["commit_id"]


def test_notice_rebase_preserves_applied_untracked_root_and_tail_after_restart(notice_case):
    case = notice_case
    internal, _intent = case.internal("sources")
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(case.session, case.thread), control_state_port=case.saver)
    for identity, placement, content in (("snapshot-root", "root_eligible", "已封存根来源"),
                                         ("snapshot-tail", "tail_only", "已封存尾部来源")):
        manager.register(ContextSourceDescriptor(source_id=identity, source_kind="skill", name=identity,
            description=identity, internal_locator=f"runtime:{identity}", root_placement=placement), tracking_status="tracked")
        manager.observe(identity, content, revision="snapshot:1")
    previous = case.prepare(internal, "sources-internal", manager=manager)
    for name in ("snapshot-root", "snapshot-tail"):
        manager.load_skill(name, mode="untrack")
    _accepted, user = case.user("sources-user")
    case.restart()
    sealed = case.prepare(user, "sources-user-first")
    assert sealed.seal_binding.prefix.state.epoch.reason == EpochReason.RUNTIME_NOTICE_CONSUMED
    assert {source.source_identity for source in sealed.seal_binding.root.sources} == {"snapshot-root"}
    assert [frame.content for frame in sealed.seal_binding.prefix.state.frames].count("已封存尾部来源") == 1
    assert sealed.seal_binding.source_revisions == ()
    assert previous.seal_binding.root.content == sealed.seal_binding.root.content


@pytest.mark.asyncio
async def test_fork_copies_notice_epoch_without_source_boundary_and_new_user_binds_target_acceptance(
    notice_case, session_bundle_factory, fork_deadlines,
):
    case = notice_case
    internal, _intent = case.internal("fork")
    case.prepare(internal, "fork-internal")
    accepted, user = case.user("fork-user")
    sealed = case.prepare(user, "fork-user-first")
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "checkpoint-after-notice-consumption"
    checkpoint["channel_values"] = {"messages": [
        HumanMessage(content="真正的用户输入", id=accepted["root_input_item_id"].removeprefix("item-")),
        AIMessage(content="公开回答", id="fork-public-answer", response_metadata={"model_call_id": sealed.model_call_id}),
    ]}
    checkpoint["channel_versions"] = {"messages": checkpoint["id"]}
    with case.scope(user):
        case.saver.put({"configurable": {"thread_id": case.thread, "checkpoint_ns": ""}}, checkpoint,
                       {"source": "loop", "step": 1}, checkpoint["channel_versions"])
    target_session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(case.sessions, target_session)
    await copy_rollout_snapshot(
        case.saver, case.session, target_session, fork_deadlines=fork_deadlines,
    )
    target_saver = RolloutCheckpointSaver(case.sessions)
    target_thread = target_saver._storage.storage.main_thread_id(target_session)
    copied = target_saver.get_context_assembly(target_session, target_thread, assembly_id=sealed.assembly_id)
    assert canonical_json_bytes(copied.to_dict()) == canonical_json_bytes(sealed.to_dict())
    target = NoticeCase(target_saver, target_session, target_thread, case.sessions, case.wire, case.activation)
    target_accepted, target_user = target.user("fork-target")
    target_sealed = target.prepare(target_user, "fork-target-first")
    assert target_sealed.seal_binding.notice_acceptance.acceptance_commit_id == target_accepted["commit_id"]
    assert target_sealed.seal_binding.notice_acceptance != sealed.seal_binding.notice_acceptance
