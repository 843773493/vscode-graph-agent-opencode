"""schema4 terminal convergence 的 JSONL/SQLite/恢复边界验收。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
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
    ContextSourceManager,
)
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True, slots=True)
class TerminalCase:
    sessions: Path
    session_id: str
    thread_id: str
    turn_id: str | None
    execution_id: str
    root: Path
    saver: RolloutCheckpointSaver

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def jsonl(self) -> Path:
        return self.root / "rollout.jsonl"


@pytest.fixture
def terminal_case(
    request: pytest.FixtureRequest,
    session_bundle_factory,
) -> Iterator[TerminalCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    saver = RolloutCheckpointSaver(sessions)
    accepted = saver.accept_turn(
        session_id,
        thread_id=saver._storage.storage.main_thread_id(session_id), accepted_ingress_id=f"ingress-{session_id}",
        acceptance_idempotency_key=f"acceptance-{session_id}",
        job_id=f"job-{session_id}",
        admission_idempotency_key=f"admission-{session_id}",
        payload="terminal convergence 用户输入",
        payload_kind=PayloadKind.TEXT,
        turn_id=f"turn-{session_id}",
        root_item_id=f"item-root-{session_id}",
        initial_execution_id=f"execution-{session_id}",
    )
    thread_id = saver._storage.storage.fork_paths.thread_identity(session_id, None)
    case = TerminalCase(
        sessions=sessions,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=str(accepted["turn_id"]),
        execution_id=str(accepted["initial_execution_id"]),
        root=saver._storage.storage.root(session_id, thread_id=thread_id),
        saver=saver,
    )
    admission = saver.execution_admission(
        session_id, thread_id=thread_id, execution_id=case.execution_id,
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        yield case


@pytest.fixture(params=("user_turn", "internal"))
def terminal_execution_case(
    request: pytest.FixtureRequest,
    terminal_case: TerminalCase,
) -> Iterator[TerminalCase]:
    case = terminal_case
    if request.param == "user_turn":
        yield case
        return
    execution_id = f"internal-{case.session_id}"
    message_id = f"message-{execution_id}"
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control="独立执行用于终态验收。",
        sections=(
            PromptSection("control_context", {"source": "terminal-test"}),
            PromptSection("generated_session_result", "独立执行用于终态验收。"),
        ),
        display_content="独立执行用于终态验收。",
    )
    case.saver.admit_internal_execution(
        case.session_id, thread_id=case.thread_id, job_id=f"job-{execution_id}",
        intent=InternalExecutionIntent(
            message_id=message_id, execution_id=execution_id,
            admission_idempotency_key=f"admission-{execution_id}",
        ),
        message=HumanMessage(
            content=prepared.content, id=message_id,
            response_metadata={
                "message_id": message_id,
                "created_at": "2026-10-04T12:00:00+00:00",
                "message_metadata": prepared.metadata,
            },
        ),
    )
    admission = case.saver.execution_admission(
        case.session_id, thread_id=case.thread_id, execution_id=execution_id,
    )
    with execution_admission_scope(admission, langgraph_thread_id=case.thread_id):
        yield replace(case, turn_id=None, execution_id=execution_id)


def _assistant_item(case: TerminalCase, item_id: str, payload: str) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=999,
        item_id=item_id,
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind=PayloadKind.TEXT,
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "provider",
            "producer_id": "terminal-provider",
            "invocation_id": case.execution_id,
        },
        payload=payload,
        projection_identity=ItemProjectionIdentity(projection_message_id=item_id.removeprefix("item-")),
        turn_id=case.turn_id,
        turn_scope=TurnScope.TURN_MEMBER if case.turn_id is not None else None,
        message_group_id=f"group-{case.execution_id}",
        wire_role="assistant",
    )


def _assemble_and_register_model_call(frozen_provider_activation, frozen_provider_wire, case: TerminalCase) -> str:
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(case.session_id, case.thread_id),
        control_state_port=case.saver,
    )
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    prepared = case.saver.prepare_context_for_provider(
        case.session_id, case.thread_id,
        turn_id=case.turn_id, execution_id=case.execution_id,
        plan_creation_idempotency_key=f"terminal-create:{case.execution_id}",
        seal_idempotency_key=f"terminal-seal:{case.execution_id}",
        context_source_manager=manager, tool_set_supplier=lambda: selected,
        **frozen_provider_wire("schema4-terminal-test", "chat_completions"),
        **frozen_provider_activation(case.saver, plan_creation_idempotency_key=f'terminal-create:{case.execution_id}'),
    )
    assembly_id = str(prepared["assembly_id"])
    case.saver.register_model_call(
        case.session_id,
        thread_id=case.thread_id,
        execution_id=case.execution_id,
        model_call_id=case.saver.get_context_assembly(
            case.session_id, case.thread_id, assembly_id=assembly_id,
        ).model_call_id,
        attempt=1,
        provider="terminal-test-provider",
        assembly_id=assembly_id,
        dispatch_state="dispatched",
    )
    return assembly_id


@pytest.fixture
def terminal_active_case(
    terminal_case: TerminalCase,
    frozen_provider_activation,
    frozen_provider_wire,
) -> TerminalCase:
    _assemble_and_register_model_call(
        frozen_provider_activation, frozen_provider_wire, terminal_case,
    )
    return terminal_case


def _terminal_rows(
    connection: sqlite3.Connection, case: TerminalCase
) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT commit_id, commit_mode, outcome, jsonl_offset_before, "
        "jsonl_offset_after, jsonl_record_count, status "
        "FROM storage_commits WHERE commit_kind = 'terminal_convergence' "
        "AND subject_id = ? ORDER BY commit_id",
        (case.turn_id,),
    ).fetchall()


def test_legacy_terminal_materialization_cannot_bypass_runtime_transition(
    terminal_case: TerminalCase,
) -> None:
    case = terminal_case
    final = _assistant_item(case, f"item-unstarted-final-{case.execution_id}", "尚未派发的回答")
    before = case.jsonl.read_bytes()
    with pytest.raises(ValueError, match="非法 Turn.status 转移: open->completed"):
        case.saver.converge_execution(
            case.session_id, thread_id=case.thread_id, turn_id=case.turn_id,
            execution_id=case.execution_id, outcome="completed", turn_status="completed",
            items=(final,), final_item_id=final.item_id,
        )
    with pytest.raises(TypeError, match="历史终态物化只允许 importer 私有 staging"):
        case.saver._storage.executions.materialize_legacy_turn(
            case.session_id, thread_id=case.thread_id, turn_id=case.turn_id,
            execution_id=case.execution_id, source_session_id="legacy-source-session",
            source_thread_id="legacy-source-thread", turn_status="completed",
            finalization_evidence="explicit_legacy_final_marker", items=(final,),
            final_item_id=final.item_id, checkpoint_ns="",
        )
    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT status,final_item_id FROM turn_records").fetchall() == [
            ("open", None)
        ]
        assert connection.execute("SELECT outcome FROM executions").fetchall() == [("unknown",)]
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind='terminal_convergence'",
        ).fetchone()[0] == 0


def test_acceptance_root_binding_and_turnless_internal_terminal_are_atomic(
    terminal_case: TerminalCase,
) -> None:
    case = terminal_case
    with sqlite3.connect(case.index) as connection:
        root_binding = connection.execute(
            "SELECT ic.execution_id, m.execution_id, m.turn_id "
            "FROM item_catalog AS ic JOIN messages AS m ON m.message_id = "
            "substr(ic.item_id, 6) "
            "WHERE ic.item_id = (SELECT root_input_item_id FROM turn_records "
            "WHERE session_id = ? AND turn_id = ?)",
            (case.session_id, case.turn_id),
        ).fetchone()
    assert root_binding == (case.execution_id, case.execution_id, case.turn_id)

    thread_id = case.saver._storage.storage.fork_paths.thread_identity(case.session_id, None)
    internal_execution_id = f"internal-{case.session_id}"
    internal_job_id = f"job-{internal_execution_id}"
    internal_message_id = f"message-{internal_execution_id}"
    prepared_notice = internal_message_factory.build(
        kind="generated_session_result",
        control="独立任务已经结束。",
        sections=(
            PromptSection("control_context", {"source": "test"}),
            PromptSection("generated_session_result", "独立执行结果"),
        ),
        display_content="独立任务已经结束。",
    )
    case.saver.admit_internal_execution(
        case.session_id,
        thread_id=thread_id,
        job_id=internal_job_id,
        intent=InternalExecutionIntent(
            message_id=internal_message_id,
            execution_id=internal_execution_id,
            admission_idempotency_key=f"internal-admission-{case.session_id}",
        ),
        message=HumanMessage(
            content=prepared_notice.content,
            id=internal_message_id,
            response_metadata={
                "message_id": internal_message_id,
                "created_at": "2026-10-04T12:00:00+00:00",
                "message_metadata": prepared_notice.metadata,
            },
        ),
    )
    pending = case.saver.cold_execution_observations(
        case.session_id, thread_id, job_ids=[internal_job_id],
    )
    assert len(pending) == 1
    assert (pending[0]["job_id"], pending[0]["execution_id"], pending[0]["state"]) == (
        internal_job_id, internal_execution_id, "pending",
    )
    output = CanonicalItemRecord.create(
        item_sequence=999,
        item_id=f"item-{internal_execution_id}-output",
        semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
        payload_kind="text",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={
            "producer_kind": "runtime",
            "producer_id": "internal-agent",
            "invocation_id": internal_execution_id,
        },
        payload="独立 execution 的终态输出",
        projection_identity=ItemProjectionIdentity(projection_message_id=f"{internal_execution_id}-output"),
        wire_role="assistant",
    )

    commit_id = case.saver.converge_execution(
        case.session_id,
        thread_id=thread_id,
        turn_id=None,
        execution_id=internal_execution_id,
        outcome="completed",
        turn_status=None,
        items=(output,),
    )
    jsonl_after_first_commit = case.jsonl.read_bytes()
    replayed_commit_id = case.saver.converge_execution(
        case.session_id,
        thread_id=thread_id,
        turn_id=None,
        execution_id=internal_execution_id,
        outcome="completed",
        turn_status=None,
        items=(output,),
    )

    assert replayed_commit_id == commit_id
    assert case.jsonl.read_bytes() == jsonl_after_first_commit
    with sqlite3.connect(case.index) as connection:
        internal_item = connection.execute(
            "SELECT turn_id, execution_id FROM item_catalog WHERE item_id = ?",
            (output.item_id,),
        ).fetchone()
        internal_message = connection.execute(
            "SELECT turn_id, execution_id, role FROM messages WHERE message_id = ?",
            (output.projection_identity.projection_message_id,),
        ).fetchone()
        internal_execution = connection.execute(
            "SELECT turn_id, execution_origin, outcome FROM executions "
            "WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
            (case.session_id, thread_id, internal_execution_id),
        ).fetchone()
        terminal_commits = connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = "
            "'terminal_convergence' AND subject_id = ? AND commit_id = ?",
            (internal_execution_id, commit_id),
        ).fetchone()

    assert internal_item == (None, internal_execution_id)
    assert internal_message == (None, internal_execution_id, "assistant")
    assert internal_execution == (None, "internal", "completed")
    assert terminal_commits == (1,)
    completed = case.saver.cold_execution_observations(
        case.session_id, thread_id, job_ids=[internal_job_id],
    )
    assert len(completed) == 1
    assert (completed[0]["execution_id"], completed[0]["state"]) == (
        internal_execution_id, "completed",
    )


def test_cold_wait_uses_direct_typed_legacy_notice_origin(
    terminal_case: TerminalCase,
) -> None:
    case = terminal_case
    legacy_item_id = f"item-legacy-notice-{case.session_id}"
    live_item_id = f"item-live-notice-{case.session_id}"
    fake_annotations = {
        "boxteam.legacy.import": {"extension_version": "1", "protection": "public", "value": {}},
        "boxteam.legacy.annotations": {
            "extension_version": "1", "protection": "public",
            "value": {"legacy_provenance": "legacy/unknown_source"},
        },
    }
    for label, item_id, relations, extensions in (
        ("legacy", legacy_item_id, (ItemProvenanceRelation("derived_from", "legacy_message", "legacy-notice"),), {}),
        ("live", live_item_id, (ItemProvenanceRelation(
            "derived_from", "canonical_item", legacy_item_id,
            target_session_id=case.session_id, target_thread_id=case.thread_id,
        ),), fake_annotations),
    ):
        item = CanonicalItemRecord.create(
            item_sequence=1, item_id=item_id, semantic_kind="runtime_notice", payload_kind="text",
            status="completed", producer_ref={"producer_kind": "runtime", "producer_id": label},
            payload=f"{label} notice", turn_scope="pending_next_turn", wire_role="user",
            provenance_relations=relations, extensions=extensions,
        )
        case.saver._storage.executions.admit_internal_execution(
            case.session_id, thread_id=case.thread_id,
            execution_id=f"execution-{label}-{case.session_id}", job_id=f"job-{label}-{case.session_id}",
            admission_idempotency_key=f"admission-{label}-{case.session_id}", runtime_notice_item=item,
            request_input={"kind": "runtime_notice", "payload": item.payload},
        )
    cold = RolloutCheckpointSaver(case.sessions)
    assert cold.cold_execution_observations(
        case.session_id, case.thread_id, job_ids=[f"job-legacy-{case.session_id}"],
    ) == []
    live = cold.cold_execution_observations(
        case.session_id, case.thread_id, job_ids=[f"job-live-{case.session_id}"],
    )
    assert len(live) == 1
    assert (live[0]["job_id"], live[0]["execution_id"], live[0]["state"]) == (
        f"job-live-{case.session_id}", f"execution-live-{case.session_id}", "pending",
    )


def test_pre_model_failure_keeps_accepted_user_turn_visible(
    terminal_case: TerminalCase,
) -> None:
    case = terminal_case

    case.saver.mark_execution_lost(
        case.session_id,
        thread_id=case.thread_id,
        turn_id=case.turn_id,
        execution_id=case.execution_id,
        reason="provider_configuration_error",
    )

    latest, _older_cursor, _projection_epoch = case.saver.bootstrap_history(
        case.session_id, case.thread_id,
    )
    assert latest is not None
    assert latest.turn_id == case.turn_id
    assert latest.status == "failed"
    assert latest.user_message_count == 1
    assert latest.user_messages[0].preview == "terminal convergence 用户输入"

    with sqlite3.connect(case.index) as connection:
        assert connection.execute(
            "SELECT role, turn_id FROM messages ORDER BY message_sequence"
        ).fetchall() == [("user", case.turn_id)]
        assert connection.execute(
            "SELECT status, user_message_sequence, final_message_sequence "
            "FROM turns WHERE turn_id = ?",
            (case.turn_id,),
        ).fetchone() == ("failed", 1, None)
        assert connection.execute(
            "SELECT user_message_sequence, final_message_sequence "
            "FROM context_view_turns WHERE turn_id = ?",
            (case.turn_id,),
        ).fetchone() == (1, None)


def test_item_bearing_terminal_is_one_barrier_and_one_transaction(frozen_provider_activation, frozen_provider_wire,
    terminal_case: TerminalCase,
) -> None:
    case = terminal_case
    assembly_id = _assemble_and_register_model_call(frozen_provider_activation, frozen_provider_wire, case)
    output = _assistant_item(case, f"item-output-{case.execution_id}", "终态正文")
    before = case.jsonl.read_bytes()

    commit_id = case.saver.converge_execution(
        case.session_id,
        thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
        execution_id=case.execution_id,
        outcome="completed",
        turn_status="completed",
        items=(output,),
        final_item_id=output.item_id,
        assembly_id=assembly_id,
    )

    after = case.jsonl.read_bytes()
    assert after.startswith(before)
    with sqlite3.connect(case.index) as connection:
        terminal = _terminal_rows(connection, case)
        item = connection.execute(
            "SELECT commit_id, item_sequence, semantic_kind, status "
            "FROM item_catalog WHERE item_id = ?",
            (output.item_id,),
        ).fetchone()
        state = connection.execute(
            "SELECT tr.status, tr.final_item_id, e.outcome, "
            "ca.status, ca.outcome, t.status, t.final_message_id, "
            "cvt.final_message_sequence "
            "FROM turn_records tr "
            "JOIN executions e ON e.execution_id = tr.last_execution_id "
            "JOIN context_assemblies ca ON ca.assembly_id = ? "
            "JOIN turns t ON t.turn_id = tr.turn_id "
            "JOIN context_view_turns cvt ON cvt.turn_id = tr.turn_id "
            "WHERE tr.turn_id = ?",
            (assembly_id, case.turn_id),
        ).fetchone()
        view_membership = connection.execute(
            "SELECT COUNT(*) FROM context_view_items cvi "
            "JOIN branches b ON b.head_view_id = cvi.view_id "
            "WHERE b.status = 'active' AND cvi.item_id = ? AND cvi.visible = 1",
            (output.item_id,),
        ).fetchone()
    assert len(terminal) == 1
    terminal_row = terminal[0]
    assert terminal_row == (
        commit_id,
        "item_bearing",
        "completed",
        len(before),
        len(after),
        1,
        "committed",
    )
    assert item == (commit_id, 2, "assistant_output", "completed")
    assert state == (
        "completed",
        output.item_id,
        "completed",
        "terminal",
        "completed",
        "completed",
        output.item_id.removeprefix("item-"),
        2,
    )
    assert view_membership == (1,)


@pytest.mark.parametrize(
    ("outcome", "turn_status"),
    [
        ("completed_empty", "completed_empty"),
        ("failed", "failed"),
        ("interrupted", "interrupted"),
        ("cancelled", "cancelled"),
        ("execution_lost", "unknown"),
    ],
)
def test_metadata_only_terminal_does_not_move_offset_and_replays_idempotently(frozen_provider_activation, frozen_provider_wire,
    terminal_case: TerminalCase,
    outcome: str,
    turn_status: str,
) -> None:
    case = terminal_case
    assembly_id = _assemble_and_register_model_call(frozen_provider_activation, frozen_provider_wire, case)
    before = case.jsonl.read_bytes()
    first = case.saver.converge_execution(
        case.session_id,
        thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
        execution_id=case.execution_id,
        outcome=outcome,
        turn_status=turn_status,
        assembly_id=assembly_id,
    )
    after_first = case.jsonl.read_bytes()
    second = case.saver.converge_execution(
        case.session_id,
        thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
        execution_id=case.execution_id,
        outcome=outcome,
        turn_status=turn_status,
        assembly_id=assembly_id,
    )

    assert after_first == before
    assert case.jsonl.read_bytes() == before
    assert second == first
    with sqlite3.connect(case.index) as connection:
        terminal = _terminal_rows(connection, case)
        meta = connection.execute(
            "SELECT committed_jsonl_offset FROM database_meta"
        ).fetchone()
        control = connection.execute(
            "SELECT tr.status, tr.final_item_id, e.outcome, ca.status, ca.outcome "
            "FROM turn_records tr JOIN executions e ON e.execution_id = tr.last_execution_id "
            "JOIN context_assemblies ca ON ca.assembly_id = ? "
            "WHERE tr.turn_id = ?",
            (assembly_id, case.turn_id),
        ).fetchone()
    assert terminal == [
        (first, "metadata_only", outcome, len(before), len(before), 0, "committed")
    ]
    assert meta == (len(before),)
    assert control == (turn_status, None, outcome, "terminal", outcome)


@pytest.mark.parametrize(
    ("provider_outcome", "execution_outcome"),
    [
        ("completed", "cancelled"),
        ("completed", "interrupted"),
        ("completed", "failed"),
        ("completed", "execution_lost"),
        ("failed", "execution_lost"),
    ],
)
def test_provider_result_survives_later_execution_abort_and_restart(
    terminal_execution_case: TerminalCase,
    frozen_provider_activation,
    frozen_provider_wire,
    provider_outcome: str,
    execution_outcome: str,
) -> None:
    case = terminal_execution_case
    assembly_id = _assemble_and_register_model_call(
        frozen_provider_activation, frozen_provider_wire, case,
    )
    model_call_id = case.saver.get_context_assembly(
        case.session_id, case.thread_id, assembly_id=assembly_id,
    ).model_call_id
    dispatch_state = "completed" if provider_outcome == "completed" else "failed"
    case.saver.update_model_call_outcome(
        case.session_id, thread_id=case.thread_id, model_call_id=model_call_id,
        outcome=provider_outcome, dispatch_state=dispatch_state,
    )
    # Provider 已返回，工具或下一请求仍未完成；此时不能把调用结果当执行结果。
    turn_status = (
        ("unknown" if execution_outcome == "execution_lost" else execution_outcome)
        if case.turn_id is not None else None
    )
    loss_reason = (
        "terminal-test-execution-lost"
        if execution_outcome == "execution_lost" and case.turn_id is not None else None
    )
    before = case.jsonl.read_bytes()
    first = case.saver.converge_execution(
        case.session_id, thread_id=case.thread_id, turn_id=case.turn_id,
        execution_id=case.execution_id, outcome=execution_outcome,
        turn_status=turn_status, assembly_id=assembly_id, loss_reason=loss_reason,
    )
    restarted = RolloutCheckpointSaver(case.sessions)
    second = restarted.converge_execution(
        case.session_id, thread_id=case.thread_id, turn_id=case.turn_id,
        execution_id=case.execution_id, outcome=execution_outcome,
        turn_status=turn_status, assembly_id=assembly_id, loss_reason=loss_reason,
    )
    assert first == second
    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT outcome FROM executions WHERE execution_id=?", (case.execution_id,),
        ).fetchone() == (execution_outcome,)
        assert connection.execute(
            "SELECT outcome,dispatch_state FROM model_calls WHERE model_call_id=?",
            (model_call_id,),
        ).fetchone() == (provider_outcome, dispatch_state)
        assert connection.execute(
            "SELECT status,outcome FROM context_assemblies WHERE assembly_id=?",
            (assembly_id,),
        ).fetchone() == ("terminal", provider_outcome)
        assert connection.execute(
            "SELECT COUNT(*),MIN(commit_id),MAX(commit_id) FROM storage_commits "
            "WHERE commit_kind='terminal_convergence' AND subject_id=?",
            (case.turn_id or case.execution_id,),
        ).fetchone() == (1, first, first)
        if case.turn_id is not None:
            assert connection.execute(
                "SELECT status,final_item_id FROM turn_records WHERE turn_id=?",
                (case.turn_id,),
            ).fetchone() == (turn_status, None)


def test_failed_provider_call_cannot_be_promoted_by_execution_success(
    terminal_execution_case: TerminalCase,
    frozen_provider_activation,
    frozen_provider_wire,
) -> None:
    case = terminal_execution_case
    assembly_id = _assemble_and_register_model_call(
        frozen_provider_activation, frozen_provider_wire, case,
    )
    model_call_id = case.saver.get_context_assembly(
        case.session_id, case.thread_id, assembly_id=assembly_id,
    ).model_call_id
    case.saver.update_model_call_outcome(
        case.session_id, thread_id=case.thread_id, model_call_id=model_call_id,
        outcome="failed", dispatch_state="failed",
    )
    output = _assistant_item(case, f"item-invalid-success-{case.execution_id}", "不能提升失败调用")
    before = case.jsonl.read_bytes()
    with pytest.raises(ValueError, match="与最新 model_call outcome 冲突"):
        case.saver.converge_execution(
            case.session_id, thread_id=case.thread_id, turn_id=case.turn_id,
            execution_id=case.execution_id, outcome="completed",
            turn_status="completed" if case.turn_id is not None else None,
            items=(output,), final_item_id=output.item_id if case.turn_id is not None else None,
            assembly_id=assembly_id,
        )
    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT outcome FROM executions WHERE execution_id=?", (case.execution_id,),
        ).fetchone() == ("unknown",)
        assert connection.execute(
            "SELECT outcome,dispatch_state FROM model_calls WHERE model_call_id=?",
            (model_call_id,),
        ).fetchone() == ("failed", "failed")
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind='terminal_convergence' "
            "AND subject_id=?", (case.turn_id or case.execution_id,),
        ).fetchone() == (0,)


def test_terminal_idempotency_conflict_preserves_committed_prefix(
    terminal_active_case: TerminalCase,
) -> None:
    case = terminal_active_case
    output = _assistant_item(case, f"item-output-{case.execution_id}", "首个终态正文")
    case.saver.converge_execution(
        case.session_id,
        thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
        execution_id=case.execution_id,
        outcome="completed",
        turn_status="completed",
        items=(output,),
        final_item_id=output.item_id,
    )
    before = case.jsonl.read_bytes()
    with sqlite3.connect(case.index) as connection:
        committed_row = _terminal_rows(connection, case)
    conflicting = _assistant_item(case, output.item_id, "冲突终态正文")

    with pytest.raises(ValueError, match="幂等键冲突"):
        case.saver.converge_execution(
            case.session_id,
            thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
            execution_id=case.execution_id,
            outcome="completed",
            turn_status="completed",
            items=(conflicting,),
            final_item_id=conflicting.item_id,
        )

    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index) as connection:
        assert _terminal_rows(connection, case) == committed_row
        assert connection.execute(
            "SELECT content_hash FROM item_catalog "
            "WHERE item_catalog.item_id = ?",
            (output.item_id,),
        ).fetchone() == (output.content_hash,)


def test_completed_terminal_cannot_split_precommitted_final_item(
    terminal_active_case: TerminalCase,
) -> None:
    case = terminal_active_case
    output = _assistant_item(case, f"item-precommitted-{case.execution_id}", "不能拆分")
    case.saver.append_items(case.session_id, (output,), thread_id=case.thread_id)
    before = case.jsonl.read_bytes()

    with pytest.raises(ValueError, match="同一提交中包含 final canonical item"):
        case.saver.converge_execution(
            case.session_id,
            thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
            execution_id=case.execution_id,
            outcome="completed",
            turn_status="completed",
            final_item_id=output.item_id,
        )

    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index) as connection:
        assert _terminal_rows(connection, case) == []
        assert connection.execute(
            "SELECT status, final_item_id FROM turn_records WHERE turn_id = ?",
            (case.turn_id,),
        ).fetchone() == ("active", None)


def test_terminal_projection_failure_rolls_back_jsonl_and_all_control_state(
    terminal_active_case: TerminalCase,
) -> None:
    case = terminal_active_case
    before = case.jsonl.read_bytes()
    output = _assistant_item(case, f"item-invalid-{case.execution_id}", "不会提交")
    invalid = replace(output, wire_role="user")

    with pytest.raises(ValueError, match="wire_role 与 message projection 不匹配"):
        case.saver.converge_execution(
            case.session_id,
            thread_id=case.saver._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
            execution_id=case.execution_id,
            outcome="completed",
            turn_status="completed",
            items=(invalid,),
            final_item_id=invalid.item_id,
        )

    assert case.jsonl.read_bytes() == before
    with sqlite3.connect(case.index) as connection:
        assert connection.execute(
            "SELECT status, final_item_id FROM turn_records WHERE turn_id = ?",
            (case.turn_id,),
        ).fetchone() == ("active", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_commits WHERE commit_kind = 'terminal_convergence'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE item_id = ?",
            (invalid.item_id,),
        ).fetchone() == (0,)


def test_real_process_exit_after_jsonl_barrier_is_reclaimed_and_retried(
    terminal_active_case: TerminalCase,
) -> None:
    case = terminal_active_case
    output = _assistant_item(case, f"item-crash-{case.execution_id}", "崩溃后重试正文")
    before = case.jsonl.read_bytes()
    child = """
import os
import sys
from pathlib import Path
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from tests.support.session_catalog_owner import fixture_catalog_scope

sessions = Path(sys.argv[1])
session_id = sys.argv[2]
turn_id = sys.argv[3]
execution_id = sys.argv[4]
item = CanonicalItemRecord.from_dict(__import__("json").loads(sys.argv[5]))
jsonl = Path(sys.argv[6]).resolve()
real_fsync = os.fsync

def crash_after_jsonl_barrier(fd):
    real_fsync(fd)
    descriptor_stat = os.fstat(fd)
    jsonl_stat = jsonl.stat()
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
        jsonl_stat.st_dev,
        jsonl_stat.st_ino,
    ):
        os._exit(83)

os.fsync = crash_after_jsonl_barrier
with fixture_catalog_scope():
    RolloutCheckpointSaver(sessions).converge_execution(
        session_id,
        thread_id=sys.argv[7],
        turn_id=turn_id,
        execution_id=execution_id,
        outcome="completed",
        turn_status="completed",
        items=(item,),
        final_item_id=item.item_id,
    )
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(case.sessions),
            case.session_id,
            case.turn_id,
            case.execution_id,
            json.dumps(output.to_dict(), ensure_ascii=False),
            str(case.jsonl),
            case.thread_id,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 83, result.stderr
    crash_tail = case.jsonl.read_bytes()
    assert crash_tail.startswith(before)
    assert len(crash_tail) > len(before)

    restarted = RolloutCheckpointSaver(case.sessions)
    restarted._storage.storage.initialize(case.session_id)
    assert case.jsonl.read_bytes() == before
    commit_id = restarted.converge_execution(
        case.session_id,
        thread_id=restarted._storage.storage.main_thread_id(case.session_id), turn_id=case.turn_id,
        execution_id=case.execution_id,
        outcome="completed",
        turn_status="completed",
        items=(output,),
        final_item_id=output.item_id,
    )
    with sqlite3.connect(case.index) as connection:
        assert connection.execute(
            "SELECT COUNT(*), MIN(commit_id), MAX(commit_id) FROM storage_commits "
            "WHERE commit_kind = 'terminal_convergence' AND subject_id = ?",
            (case.turn_id,),
        ).fetchone() == (1, commit_id, commit_id)
        assert connection.execute(
            "SELECT status, final_item_id FROM turn_records WHERE turn_id = ?",
            (case.turn_id,),
        ).fetchone() == ("completed", output.item_id)
