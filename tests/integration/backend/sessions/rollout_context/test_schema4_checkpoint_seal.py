"""正式当前 fresh schema 的 Saver seal 生命周期；不安装手工 DDL、不伪造 draft。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextRequestPlan, SourceBinding
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    ROLLOUT_SCHEMA_VERSION,
)
from app.testing.model_stream import build_cassette
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    invoke_provider_dispatch,
    responses_reply_frames,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    draft as draft,  # noqa: PLC0414 - 复用真实 Saver fixture，不运行主文件用例
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - fixture 按 request.node.path 隔离
)

EMPTY_TOOL_SET = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@pytest.fixture(autouse=True)
def execution_scope(registry_db):
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(
        session,
        thread_id=thread,
        execution_id=accepted["initial_execution_id"],
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session, thread), langgraph_thread_id=thread
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        yield


@pytest.fixture
def source_manager(registry_db):
    saver, session, _connection, _accepted = registry_db
    return ContextSourceManager(
        owner=ContextSourceOwnerKey(session, saver._storage.storage.main_thread_id(session)),
        control_state_port=saver,
    )


def _state(saver, session, connection):
    return (
        (saver._storage.storage.root(session) / "rollout.jsonl").read_bytes(),
        tuple(connection.iterdump()),
    )


def _seal(saver, session, plan, accepted, **changes):
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        "provider_version": "schema4-provider",
        "seal_idempotency_key": "schema4-seal",
    }
    options.update(changes)
    return saver.seal_context_plan(
        session, saver._storage.storage.main_thread_id(session), plan, wire_profile=None, wire_params=None, **options
    )


def _no_allocation(*_args, **_kwargs):
    pytest.fail("幂等重试不得分配新的 assembly/detail 或重读 active source")


def test_unregistered_plan_is_rejected_without_allocating(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    before = _state(saver, session, connection)
    with pytest.raises(KeyError, match="context plan 不存在"):
        _seal(saver, session, draft, accepted)
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("source_ref", ["source-A", "source-B"])
def test_overlay_source_binding_matches_before_real_plan_seal_writes(
    registry_db, draft, source_ref
):
    """selection alias 与 source alias 可不同，错误 source 不得进入封存事务。"""
    saver, session, connection, accepted = registry_db
    contribution = replace(
        draft.contributions[0],
        contribution_id="overlay:logical-overlay:base",
        contribution_kind="overlay_base",
        content_hash=contribution_content_hash(
            "overlay_base", draft.contributions[0].body
        ),
        source_binding=SourceBinding(
            source_ref="source-A",
            overlay_ref="selection-overlay-base",
            overlay_id="logical-overlay",
            overlay_role="base",
            source_overlay_epoch=1,
        ),
        source_ordinal=None,
        selection_role="backing_only",
    )
    saver.register_context_contribution(
        session, draft.thread_id, contribution, request_content=contribution.body
    )
    ref = replace(
        draft.refs[0],
        ref_id="selection-overlay-base",
        source_ref=source_ref,
        base_delta_role="base",
        source_overlay_epoch=1,
        content_hash=contribution.content_hash,
    )
    plan = replace(
        saver.compose_context_plan(
            session_id=session,
            thread_id=draft.thread_id,
            plan_id=draft.plan_id,
            refs=(ref,),
            source_overlay_epoch=1,
            tool_set=EMPTY_TOOL_SET,
        ),
        plan_creation_idempotency_key="overlay-source-create",
    )
    registered = saver.create_context_plan(session, plan)
    before = _state(saver, session, connection)
    if source_ref != "source-A":
        with pytest.raises(ValueError, match="overlay ref 缺少唯一 contribution"):
            bound, _records, detail_refs = saver._bind_request_detail_refs(
                session,
                plan.thread_id,
                "",
                registered.draft,
                assembly_id="overlay-source-assembly",
            )
            snapshot = ContextPlanComposer().assembly(
                plan=bound,
                assembly_id="overlay-source-assembly",
                session_id=session,
                thread_id=plan.thread_id,
                turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"],
                provider_version="source-binding-provider",
                request_detail_refs=detail_refs,
            )
            saver._storage.assemblies.seal_context_assembly(
                session,
                plan.thread_id,
                snapshot,
                seal_idempotency_key="overlay-source-seal",
                seal_input_hash=sha256_jcs("overlay-source-seal-input"),
            )
        assert _state(saver, session, connection) == before
        return
    snapshot = saver.seal_context_plan(
        session,
        plan.thread_id,
        registered.draft,
        wire_profile=None, wire_params=None, turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="source-binding-provider",
        seal_idempotency_key="overlay-source-seal",
    )
    assert snapshot.selection[0].ref.ref_id == "selection-overlay-base"
    assert snapshot.selection[0].ref.source_ref == "source-A"
    assert snapshot.contributions[0].source_binding == contribution.source_binding
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (
        1,
    )
    assert (
        saver.get_context_plan_registration(
            session, thread_id=plan.thread_id, plan_id=plan.plan_id
        ).plan_state
        == "sealed"
    )
    assert saver._storage.storage.jsonl_path(session).read_bytes() == before[0]


@pytest.mark.parametrize("omitted_ordinals, tool_ids", [
    ((), ("tools",)),
    ((1,), ("tools",)),
    ((0, 1), ("tools",)),
    ((2,), ("z-tools", "tools", "a-tools")),
])
def test_seal_restart_and_retry_reuse_committed_identity(
    registry_db, draft, monkeypatch, omitted_ordinals, tool_ids
):
    saver, session, connection, accepted = registry_db
    draft = replace(draft, tool_set_refs=tuple(
        replace(draft.tool_set_refs[0], ref_id=ref_id) for ref_id in tool_ids
    ))
    registry = (*draft.refs, *draft.tool_set_refs)
    omitted = tuple(selection_ref_identity(registry[ordinal]) for ordinal in omitted_ordinals)
    registered = saver.create_context_plan(session, draft)
    snapshot = _seal(
        saver, session, registered.draft, accepted, omitted_ref_identities=omitted
    )
    sealed = saver.get_context_plan_registration(
        session, plan_id=draft.plan_id, thread_id=saver._storage.storage.main_thread_id(session)
    )
    assert sealed.plan_state == "sealed" and sealed.seal_input_hash
    assert snapshot.plan_id == draft.plan_id
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        monkeypatch.setattr(restarted._detail_store, "prepare", _no_allocation)
        monkeypatch.setattr(
            "app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle.uuid4",
            _no_allocation,
        )
        restored = restarted.get_context_plan_registration(
            session,
            plan_id=draft.plan_id,
            thread_id=restarted._storage.storage.main_thread_id(session),
        )
        retry = _seal(
            restarted, session, restored.draft, accepted, omitted_ref_identities=omitted
        )
        assert retry.to_dict() == snapshot.to_dict()
        assert retry == snapshot
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize(
    "change",
    [
        {"turn_id": "different-turn"},
        {"execution_id": "different-execution"},
        {"provider_version": "different-provider"},
        {"model_call_id": "different-call"},
        {"target_format": "chat_completions"},
        {"omitted_ref_identities": (("tool_set", "tools"),)},
        {"loss": ("different-loss",)},
        {"seal_idempotency_key": "different-key"},
        {"request_input_hash": "different-input"},
    ],
)
def test_retry_input_conflict_precedes_random_allocation(
    registry_db, draft, monkeypatch, change, source_manager
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    _seal(saver, session, plan, accepted)
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle.uuid4",
        _no_allocation,
    )
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        _seal(saver, session, plan, accepted, **change)
    assert _state(saver, session, connection) == before


def test_failure_records_control_and_keeps_draft_correctable(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    canonical = (saver._storage.storage.root(session) / "rollout.jsonl").read_bytes()
    detail_root = saver._storage.storage.root(session)
    before_detail_rows = connection.execute(
        "SELECT * FROM context_plan_details ORDER BY detail_ref"
    ).fetchall()

    def detail_files():
        return tuple(
            (path.relative_to(detail_root).as_posix(), path.read_bytes())
            for path in sorted(detail_root.rglob("*"))
            if path.is_file() and (
                "context-plan-details" in path.parts
            )
        )

    before_detail_files = detail_files()
    original = saver._storage.assemblies.seal_context_assembly

    def reject(*_args, **_kwargs):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(saver._storage.assemblies, "seal_context_assembly", reject)
    with pytest.raises(RuntimeError, match="injected before commit"):
        _seal(saver, session, plan, accepted)
    registration = saver.get_context_plan_registration(
        session, plan_id=plan.plan_id, thread_id=saver._storage.storage.main_thread_id(session)
    )
    assert (
        registration.plan_state == "unsealed" and registration.seal_input_hash is None
    )
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (
        0,
    )
    assert connection.execute(
        "SELECT error_code FROM context_plan_seal_failures"
    ).fetchall() == [("seal-storage-failure",)]
    assert connection.execute(
        "SELECT * FROM context_plan_details ORDER BY detail_ref"
    ).fetchall() == before_detail_rows
    assert detail_files() == before_detail_files
    assert (saver._storage.storage.root(session) / "rollout.jsonl").read_bytes() == canonical
    monkeypatch.setattr(saver._storage.assemblies, "seal_context_assembly", original)
    assert _seal(saver, session, plan, accepted).plan_id == plan.plan_id


def test_lost_response_never_removes_committed_source_detail(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    original = saver._storage.assemblies.seal_context_assembly

    def commit_then_lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected lost response")

    monkeypatch.setattr(
        saver._storage.assemblies, "seal_context_assembly", commit_then_lose_response
    )
    with pytest.raises(RuntimeError, match="injected lost response"):
        _seal(saver, session, plan, accepted)
    registration = saver.get_context_plan_registration(
        session, plan_id=plan.plan_id, thread_id=saver._storage.storage.main_thread_id(session)
    )
    assert registration.plan_state == "sealed"
    assert connection.execute(
        "SELECT count(*) FROM context_plan_seal_failures"
    ).fetchone() == (0,)
    snapshot = saver.get_context_assembly(
        session,
        saver._storage.storage.main_thread_id(session),
        assembly_id=registration.assembly_id,
    )
    detail = next(entry.detail_ref for entry in snapshot.selection if entry.detail_ref)
    assert (
        saver.read_context_plan_detail(
            session, saver._storage.storage.main_thread_id(session), detail_ref=detail
        )["detail"]
        == draft.contributions[0].body
    )
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    assert _seal(saver, session, plan, accepted).to_dict() == snapshot.to_dict()
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
def test_prepare_registers_final_filter_and_restarts_without_recompose(frozen_provider_activation,
     frozen_provider_wire, registry_db, draft, monkeypatch, source_manager
):
    saver, session, connection, accepted = registry_db
    saver.register_context_contribution(
        session,
        saver._storage.storage.main_thread_id(session),
        draft.contributions[0],
        request_content=draft.contributions[0].body,
    )
    seed_dispatch_overlay(saver, session)
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        "context_source_manager": source_manager,
        "tool_set_supplier": lambda: EMPTY_TOOL_SET,
        **frozen_provider_wire("prepare-provider", "responses"),

        "plan_creation_idempotency_key": "prepare-create",
        "seal_idempotency_key": "prepare-seal",
    }
    first = saver.prepare_context_for_provider(
        session, saver._storage.storage.main_thread_id(session), **options,
        **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']),
    )
    plan = first["plan"]
    registered = saver.get_context_plan_registration(
        session, plan_id=plan.plan_id, thread_id=saver._storage.storage.main_thread_id(session)
    )
    assert {item.contribution_kind for item in registered.draft.contributions} == {
        "overlay_base",
        "overlay_delta",
    }
    items = saver.read_canonical_items(session)
    saver.append_items(
        session,
        (
            CanonicalItemRecord.create(
                item_id="later-output",
                item_sequence=max(item.item_sequence for item in items) + 1,
                semantic_kind="assistant_output",
                payload_kind="text",
                status="completed",
                producer_ref={
                    "producer_kind": "provider",
                    "producer_id": "schema4-test",
                },
                payload="后续 active view 输出不能改变旧请求",
                wire_role="assistant",
                turn_id=accepted["turn_id"],
                turn_scope="turn_member",
            ),
        ),
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(
        saver._detail_store.sessions_dir,
    ) as restarted:
        monkeypatch.setattr(restarted, "compose_committed_context_plan", _no_allocation)
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        monkeypatch.setattr(restarted._detail_store, "prepare", _no_allocation)
        retry = restarted.prepare_context_for_provider(
            session, restarted._storage.storage.main_thread_id(session), **options,
            **frozen_provider_activation(restarted, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']),
        )
        assert retry["assembly_id"] == first["assembly_id"]
        assert retry["plan"].selection == plan.selection
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
def test_dispatch_attempt_identity_reuses_registered_source(frozen_provider_activation,
    frozen_provider_wire, registry_db,
    monkeypatch,
    source_manager,
):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        **frozen_provider_wire("schema4-provider", "responses"),

        "context_source_manager": source_manager,
        "tool_set_supplier": lambda: EMPTY_TOOL_SET,
        "plan_creation_idempotency_key": "schema4-call:create",
        "seal_idempotency_key": "schema4-call:seal",
    }
    first = saver.prepare_context_for_provider(session, thread, **options, **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']))
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver, "compose_committed_context_plan", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    retry = saver.prepare_context_for_provider(session, thread, **options, **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']))
    assert retry["assembly_id"] == first["assembly_id"]
    assert retry["plan"].selection == first["plan"].selection
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver.prepare_context_for_provider(
            session,
            thread,
            **{**options, **frozen_provider_wire("changed", "responses")},
            **frozen_provider_activation(saver, plan_creation_idempotency_key=({**options, **frozen_provider_wire('changed', 'responses')})['plan_creation_idempotency_key']),
        )
    assert _state(saver, session, connection) == before


def test_low_level_header_detail_retry_does_not_allocate(registry_db, monkeypatch):
    saver, session, connection, accepted = registry_db
    plan = ContextRequestPlan(
        session_id=session,
        plan_id="header-plan",
        refs=(),
        plan_creation_idempotency_key="header-create",
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    plan = saver.create_context_plan(session, plan).draft
    snapshot = ContextPlanComposer().assembly(
        plan=plan,
        session_id=session,
        assembly_id="header-assembly",
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="header-provider",
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    options = {
        "seal_idempotency_key": "header-seal",
        "seal_input_hash": sha256_jcs({"request": "header"}),
        "detail": {"audit": "body"},
        "required_detail": True,
    }
    commit = saver.seal_context_assembly(
        session, saver._storage.storage.main_thread_id(session), snapshot, **options
    )
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    assert (
        saver.seal_context_assembly(
            session, saver._storage.storage.main_thread_id(session), snapshot, **options
        )
        == commit
    )
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver.seal_context_assembly(
            session,
            saver._storage.storage.main_thread_id(session),
            snapshot,
            **{**options, "detail": {"audit": "changed"}},
        )
    assert _state(saver, session, connection) == before


def test_changed_registered_manifest_cannot_reuse_seal(registry_db, draft):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    _seal(saver, session, plan, accepted)
    before = _state(saver, session, connection)
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        _seal(saver, session, replace(plan, selection_policy="other"), accepted)
    assert canonical_json_bytes(plan.to_dict()) != canonical_json_bytes(
        replace(plan, selection_policy="other").to_dict()
    )
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
@pytest.mark.parametrize(
    "change", ["messages", "turn", "execution", "provider", "target"]
)
def test_prepare_rejects_different_input_without_source_side_effects(frozen_provider_activation,
    frozen_provider_wire, registry_db, draft, monkeypatch, change, source_manager
):
    saver, session, connection, accepted = registry_db
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        "context_source_manager": source_manager,
        "tool_set_supplier": lambda: EMPTY_TOOL_SET,
        **frozen_provider_wire("prepare-provider", "responses"),

        "plan_creation_idempotency_key": "prepare-input",
        "seal_idempotency_key": "prepare-input-seal",
    }
    saver.prepare_context_for_provider(
        session, saver._storage.storage.main_thread_id(session), **options,
        **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']),
    )
    if change == "messages":
        options["request_messages"] = (HumanMessage(content="changed", id="changed"),)
    elif change == "turn":
        options["turn_id"] = "different-turn"
    elif change == "execution":
        options["execution_id"] = "different-execution"
    elif change == "provider":
        options.update(frozen_provider_wire("changed", "responses"))
    else:
        options.update(frozen_provider_wire("prepare-provider", "chat_completions"))
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver, "compose_committed_context_plan", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "prepare", _no_allocation)
    with pytest.raises(
        (RuntimeError, ValueError), match="admission|assembly-idempotency-conflict"
    ):
        saver.prepare_context_for_provider(
            session, saver._storage.storage.main_thread_id(session), **options,
            **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']),
        )
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
@pytest.mark.parametrize("change", ["source", "tools"])
def test_sealed_prepare_retry_keeps_binding_and_pending_desired(frozen_provider_activation,
    frozen_provider_wire, registry_db,
    source_manager,
    monkeypatch,
    change,
):
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    source_manager.register(
        ContextSourceDescriptor(
            source_id="instructions:retry",
            source_kind="agent_instructions",
            name="retry instruction",
            description="封存后desired变化",
            internal_locator="instruction-producer://schema-retry",
            root_placement="root_eligible",
        ),
        tracking_status="tracked",
    )
    source_manager.observe(
        "instructions:retry", "已发送说明", revision="instructions:1"
    )
    desired_tools = EMPTY_TOOL_SET
    reads = []

    def tool_set():
        reads.append(desired_tools)
        return desired_tools

    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        **frozen_provider_wire("prepare-provider", "responses"),

        "plan_creation_idempotency_key": "desired:create",
        "seal_idempotency_key": "desired:seal",
        "context_source_manager": source_manager,
        "tool_set_supplier": tool_set,
    }
    first = saver.prepare_context_for_provider(session, thread, **options, **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']))
    if change == "source":
        source_manager.observe(
            "instructions:retry", "尚未发送说明", revision="instructions:2"
        )
    else:
        desired_tools = ToolSetSnapshot(
            selection_revision=sha256_jcs({"read_file": True}),
            tools=({"name": "read_file", "parameters": {"type": "object"}},),
        )
    pending = source_manager.prepare_pending()
    before = _state(saver, session, connection)
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(source_manager, "prepare_sources", _no_allocation)
        retry = saver.prepare_context_for_provider(session, thread, **options, **frozen_provider_activation(saver, plan_creation_idempotency_key=(options)['plan_creation_idempotency_key']))
    assert retry["assembly_id"] == first["assembly_id"]
    assert retry["plan"].selection == first["plan"].selection
    assert retry["messages"] == first["messages"] and retry["tools"] == first["tools"]
    assert reads == [EMPTY_TOOL_SET]
    assert source_manager.prepare_pending() == pending
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
@pytest.mark.asyncio
async def test_real_middleware_registers_current_schema_before_dispatch(
    registry_db, inprocess_model_replay,
):
    saver, session, connection, accepted = registry_db
    replay, controller = inprocess_model_replay
    endpoint = "https://schema-seal-replay.invalid/v1"
    thread = saver._storage.storage.main_thread_id(session)
    cassette = build_cassette(
        asset_id="schema-seal", url=endpoint + "/responses", model="gpt-4.1",
        protocol="openai_responses_sse", frames=responses_reply_frames(1),
    )
    (call_id,) = replay.control.plan_model_calls(session, thread, [
        {**cassette.raw["interactions"][0], "protocol": "openai_responses_sse"},
    ])
    seed_dispatch_overlay(saver, session)
    await invoke_provider_dispatch(saver, session, accepted["turn_id"], endpoint, "schema-replay-key", target_format="responses")
    assert controller.transport.call_count == 1
    (snapshot,) = saver.list_context_assemblies(
        session, saver._storage.storage.main_thread_id(session)
    )
    assert snapshot.model_call_id == call_id
    recorded = json.loads((replay.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
    assert recorded["identity"] == {"session_id": session, "thread_id": thread, "model_call_id": call_id}
    registration = saver.get_context_plan_registration(
        session,
        plan_id=snapshot.plan_id,
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    assert registration.draft.plan_creation_idempotency_key.startswith(
        "provider-create:"
    )
    assert registration.seal_idempotency_key.startswith("provider-seal:")
    assert registration.seal_input_hash.startswith("sha256:jcs:v1:")
    assert connection.execute(
        "SELECT schema_version FROM database_meta"
    ).fetchone() == (ROLLOUT_SCHEMA_VERSION,)
    native = saver.project_context_plan_to_native(
        session, snapshot.thread_id, snapshot.as_sealed_plan()
    )
    assert recorded["request"]["input"] == native["request"]["input"]
