"""真实 Saver prepare → seal → CSM 确认 → Provider request 的纵向验证。"""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - fixture 根据当前测试文件隔离产物
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)

@pytest.fixture
def production_context(frozen_provider_activation, frozen_provider_wire, registry_db):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.main_thread_id(session)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(session, thread), control_state_port=saver,
    )
    manager.register(ContextSourceDescriptor(
        source_id="instruction:R01", source_kind="agent_instructions", name="Agent 基础说明",
        description="Agent 基础说明", internal_locator="instruction-producer://R01",
        root_placement="root_eligible",
    ), tracking_status="tracked")
    manager.observe("instruction:R01", "基础规则第一版", revision="instructions:1")
    selected = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )

    def prepare(key, *, supplier=None, target_format="chat_completions"):
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session, thread), langgraph_thread_id=thread,
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            return saver.prepare_context_for_provider(
                session, thread, turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"],
                plan_creation_idempotency_key="create:" + key,
                seal_idempotency_key="seal:" + key,
                context_source_manager=manager,
                tool_set_supplier=supplier or (lambda: selected),
                **frozen_provider_wire("production-provider", target_format),
                **frozen_provider_activation(saver, plan_creation_idempotency_key='create:' + key),
            )

    return saver, session, thread, connection, manager, prepare


def test_initial_and_delta_use_one_seal_and_exact_prefix(production_context):
    saver, session, thread, connection, manager, prepare = production_context
    first = prepare("first")
    assert [type(message) for message in first["messages"]] == [SystemMessage, HumanMessage]
    assert first["messages"][0].content == "基础规则第一版"
    assert first["tools"] == []
    first_snapshot = saver._storage.get_context_assembly(
        session, thread, assembly_id=first["assembly_id"],
    )
    assert len(first_snapshot.tool_set_refs) == 1
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1",)
    manager.observe("instruction:R01", "基础规则第二版", revision="instructions:2")
    second = prepare("second")
    second_snapshot = saver._storage.get_context_assembly(
        session, thread, assembly_id=second["assembly_id"],
    )
    assert second_snapshot.seal_binding.root == first_snapshot.seal_binding.root
    assert second_snapshot.seal_binding.prefix.state.frames[:2] == first_snapshot.seal_binding.prefix.state.frames
    assert second["messages"][0].content == "基础规则第一版"
    assert isinstance(second["messages"][-1], HumanMessage)
    assert "基础规则第二版" in second["messages"][-1].content
    assert manager.prepare_pending() is None
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'"
    ).fetchone() == (2,)


def test_failed_seal_never_acknowledges_source(production_context):
    _saver, _session, _thread, connection, manager, prepare = production_context
    batch = manager.prepare_pending()
    connection.execute(
        "CREATE TRIGGER refuse_assembly BEFORE INSERT ON context_assemblies "
        "BEGIN SELECT RAISE(ABORT, 'injected assembly failure'); END"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected assembly failure"):
        prepare("failure")
    assert manager.prepare_pending() == batch
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states"
    ).fetchone() == (None,)
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)


def test_sealed_retry_does_not_read_live_desired(production_context, monkeypatch):
    saver, session, thread, _connection, manager, prepare = production_context
    first = prepare("retry")
    manager.observe("instruction:R01", "尚未发送的新 desired", revision="instructions:2")
    pending = manager.prepare_pending()

    def refuse_desired():
        raise AssertionError("sealed retry 不得重新读取 desired")

    monkeypatch.setattr(manager, "prepare_sources", refuse_desired)
    retry = prepare("retry", supplier=refuse_desired)
    assert retry["assembly_id"] == first["assembly_id"]
    assert retry["messages"] == first["messages"]
    assert pending is not None
    assert tuple(manager._pending.values()) == pending.deltas
    restored = saver._storage.get_context_assembly(session, thread, assembly_id=retry["assembly_id"])
    assert restored.seal_binding.source_revisions[0].revision == "instructions:1"


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_late_desired_after_seal_is_preserved_and_next_call_applies_exact_base(
    production_context, monkeypatch, target_format,
):
    saver, _session, _thread, connection, manager, prepare = production_context
    original_seal = saver.seal_context_plan

    def seal_then_observe(*args, **kwargs):
        snapshot = original_seal(*args, **kwargs)
        manager.observe("instruction:R01", "封存后到达的第二版", revision="instructions:2")
        return snapshot

    monkeypatch.setattr(saver, "seal_context_plan", seal_then_observe)
    first = prepare("late-first", target_format=target_format)
    assert first["messages"][0].content == "基础规则第一版"
    pending = manager.prepare_pending()
    assert pending.deltas[0].previous_revision == "instructions:1"
    assert pending.deltas[0].revision == "instructions:2"
    assert connection.execute(
        "SELECT latest_visible_committed_revision, latest_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:1", "instructions:2")
    monkeypatch.setattr(saver, "seal_context_plan", original_seal)
    second = prepare("late-second", target_format=target_format)
    assert second["messages"][0].content == first["messages"][0].content
    assert "封存后到达的第二版" in second["messages"][-1].content
    assert manager.prepare_pending() is None


def test_same_body_new_semantic_revision_is_not_discarded(production_context):
    _saver, _session, _thread, connection, manager, prepare = production_context
    prepare("same-body-first")
    manager.observe("instruction:R01", "基础规则第一版", revision="instructions:2")
    pending = manager.prepare_pending()
    assert pending.deltas[0].revision == "instructions:2"
    second = prepare("same-body-second")
    assert second["messages"][-1].content == "基础规则第一版"
    assert connection.execute(
        "SELECT latest_visible_committed_revision, latest_revision FROM context_source_control_states"
    ).fetchone() == ("instructions:2", "instructions:2")


def test_final_request_is_rechecked_against_sealed_wire(production_context, monkeypatch):
    saver, _session, _thread, _connection, _manager, prepare = production_context
    original = saver.project_context_plan_to_provider

    def drifted_projection(*args, **kwargs):
        messages, tools, losses = original(*args, **kwargs)
        return [*messages, HumanMessage(content="未封存的额外正文")], tools, losses

    monkeypatch.setattr(saver, "project_context_plan_to_provider", drifted_projection)
    with pytest.raises(ValueError, match="最终 Provider request"):
        prepare("wire-drift")
