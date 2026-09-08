"""正式 fresh schema4 的 Saver seal 生命周期；不安装手工 DDL、不伪造 draft。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    invoke_native_dispatch,
    native_http_server,
    seed_dispatch_overlay,
)

__all__ = ["native_http_server"]
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    draft as draft,  # noqa: PLC0414 - 复用真实 Saver fixture，不运行主文件用例
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - fixture 按 request.node.path 隔离
)


def _state(saver, session, connection):
    return (
        (saver._storage.root(session) / "rollout.jsonl").read_bytes(),
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
    return saver.seal_context_plan(session, plan, **options)


def _no_allocation(*_args, **_kwargs):
    pytest.fail("幂等重试不得分配新的 assembly/detail 或重读 active source")


def test_unregistered_plan_is_rejected_without_allocating(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    before = _state(saver, session, connection)
    with pytest.raises(KeyError, match="context plan 不存在"):
        _seal(saver, session, draft, accepted)
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("omitted", [(), ("tools",), ("request-ref", "tools")])
def test_seal_restart_and_retry_reuse_committed_identity(
    registry_db, draft, monkeypatch, omitted
):
    saver, session, connection, accepted = registry_db
    registered = saver.create_context_plan(session, draft)
    snapshot = _seal(
        saver, session, registered.draft, accepted, omitted_ref_ids=omitted
    )
    sealed = saver.get_context_plan_registration(session, plan_id=draft.plan_id)
    assert sealed.plan_state == "sealed" and sealed.seal_input_hash
    assert sealed.registration_origin == "runtime"
    assert snapshot.plan_id == draft.plan_id
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        monkeypatch.setattr(
            "app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle.uuid4",
            _no_allocation,
        )
        restored = restarted.get_context_plan_registration(
            session, plan_id=draft.plan_id
        )
        retry = _seal(
            restarted, session, restored.draft, accepted, omitted_ref_ids=omitted
        )
        assert retry.to_dict() == snapshot.to_dict()
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize(
    "change",
    [
        {"turn_id": "different-turn"},
        {"execution_id": "different-execution"},
        {"provider_version": "different-provider"},
        {"model_call_id": "different-call"},
        {"target_format": "chat_completions"},
        {"omitted_ref_ids": ("tools",)},
        {"loss": ("different-loss",)},
        {"seal_idempotency_key": "different-key"},
        {"request_input_hash": "different-input"},
    ],
)
def test_retry_input_conflict_precedes_random_allocation(
    registry_db, draft, monkeypatch, change
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    _seal(saver, session, plan, accepted)
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
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
    canonical = (saver._storage.root(session) / "rollout.jsonl").read_bytes()
    original = saver._storage.seal_context_assembly

    def reject(*_args, **_kwargs):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(saver._storage, "seal_context_assembly", reject)
    with pytest.raises(RuntimeError, match="injected before commit"):
        _seal(saver, session, plan, accepted)
    registration = saver.get_context_plan_registration(session, plan_id=plan.plan_id)
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
        "SELECT status,availability FROM context_plan_details"
    ).fetchall() == [("unavailable", "unavailable")]
    assert (saver._storage.root(session) / "rollout.jsonl").read_bytes() == canonical
    monkeypatch.setattr(saver._storage, "seal_context_assembly", original)
    assert _seal(saver, session, plan, accepted).plan_id == plan.plan_id


def test_lost_response_never_removes_committed_source_detail(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    original = saver._storage.seal_context_assembly

    def commit_then_lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected lost response")

    monkeypatch.setattr(
        saver._storage, "seal_context_assembly", commit_then_lose_response
    )
    with pytest.raises(RuntimeError, match="injected lost response"):
        _seal(saver, session, plan, accepted)
    registration = saver.get_context_plan_registration(session, plan_id=plan.plan_id)
    assert registration.plan_state == "sealed"
    assert connection.execute(
        "SELECT count(*) FROM context_plan_seal_failures"
    ).fetchone() == (0,)
    snapshot = saver.get_context_assembly(session, assembly_id=registration.assembly_id)
    detail = next(entry.detail_ref for entry in snapshot.selection if entry.detail_ref)
    assert (
        saver.read_context_plan_detail(session, detail_ref=detail)["detail"]
        == draft.contributions[0].body
    )
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    assert _seal(saver, session, plan, accepted).to_dict() == snapshot.to_dict()
    assert _state(saver, session, connection) == before


def test_prepare_registers_final_filter_and_restarts_without_recompose(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    saver.register_context_contribution(
        session, draft.contributions[0], request_content=draft.contributions[0].body
    )
    seed_dispatch_overlay(saver, session)
    options = {
        "turn_id": accepted["turn_id"],
        "provider_version": "prepare-provider",
        "target_format": "responses",
        "plan_creation_idempotency_key": "prepare-create",
        "seal_idempotency_key": "prepare-seal",
    }
    first = saver.prepare_context_for_provider(session, **options)
    plan = first["plan"]
    registered = saver.get_context_plan_registration(session, plan_id=plan.plan_id)
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
                turn_id=accepted["turn_id"],
                turn_scope="turn_member",
            ),
        ),
    )
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted, "compose_committed_context_plan", _no_allocation)
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        retry = restarted.prepare_context_for_provider(session, **options)
        assert retry["assembly_id"] == first["assembly_id"]
        assert retry["plan"].selection == plan.selection
    assert _state(saver, session, connection) == before


def test_dispatch_model_call_identity_reuses_registered_source(
    registry_db, monkeypatch
):
    saver, session, connection, accepted = registry_db
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        "model_call_id": "schema4-call",
        "provider_version": "schema4-provider",
    }
    first = saver.seal_context_for_dispatch(session, **options)
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver, "compose_committed_context_plan", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    assert saver.seal_context_for_dispatch(session, **options) == first
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver.seal_context_for_dispatch(
            session, **{**options, "provider_version": "changed"}
        )
    assert _state(saver, session, connection) == before


def test_low_level_header_detail_retry_does_not_allocate(registry_db, monkeypatch):
    saver, session, connection, accepted = registry_db
    plan = ContextRequestPlan(
        session_id=session,
        plan_id="header-plan",
        refs=(),
        plan_creation_idempotency_key="header-create",
    )
    plan = saver.create_context_plan(session, plan).draft
    snapshot = ContextPlanComposer().assembly(
        plan=plan,
        session_id=session,
        assembly_id="header-assembly",
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="header-provider",
    )
    options = {
        "seal_idempotency_key": "header-seal",
        "seal_input_hash": sha256_jcs({"request": "header"}),
        "detail": {"audit": "body"},
        "required_detail": True,
    }
    commit = saver.seal_context_assembly(snapshot, **options)
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    assert saver.seal_context_assembly(snapshot, **options) == commit
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver.seal_context_assembly(
            snapshot, **{**options, "detail": {"audit": "changed"}}
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


@pytest.mark.parametrize(
    "change", ["messages", "prompt", "tools", "provider", "target"]
)
def test_prepare_rejects_different_input_without_source_side_effects(
    registry_db, draft, monkeypatch, change
):
    saver, session, connection, accepted = registry_db
    options = {
        "turn_id": accepted["turn_id"],
        "provider_version": "prepare-provider",
        "target_format": "responses",
        "plan_creation_idempotency_key": "prepare-input",
        "seal_idempotency_key": "prepare-input-seal",
    }
    saver.prepare_context_for_provider(session, **options)
    if change == "messages":
        options["request_messages"] = (HumanMessage(content="changed", id="changed"),)
    elif change == "prompt":
        options["prompt_contributions"] = draft.contributions
    elif change == "tools":
        options["tool_snapshot"] = draft.tool_set_refs[0].tools
    elif change == "provider":
        options["provider_version"] = "changed"
    else:
        options["target_format"] = "chat_completions"
    before = _state(saver, session, connection)
    monkeypatch.setattr(saver, "compose_committed_context_plan", _no_allocation)
    monkeypatch.setattr(saver._detail_store, "write", _no_allocation)
    with pytest.raises(ValueError, match="assembly-idempotency-conflict"):
        saver.prepare_context_for_provider(session, **options)
    assert _state(saver, session, connection) == before


@pytest.mark.asyncio
async def test_real_middleware_registers_fresh4_before_native_http(
    registry_db, native_http_server
):
    saver, session, connection, accepted = registry_db
    state, endpoint = native_http_server
    seed_dispatch_overlay(saver, session)
    await invoke_native_dispatch(
        saver, session, accepted["turn_id"], endpoint, state.api_key
    )
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(session)
    registration = saver.get_context_plan_registration(
        session, plan_id=snapshot.plan_id
    )
    assert registration.registration_origin == "runtime"
    assert registration.draft.plan_creation_idempotency_key.startswith(
        "provider-create:"
    )
    assert registration.seal_idempotency_key.startswith("provider-seal:")
    assert registration.seal_input_hash.startswith("sha256:jcs:v1:")
    assert connection.execute(
        "SELECT schema_version FROM database_meta"
    ).fetchone() == (4,)
    native = saver.project_context_plan_to_native(session, snapshot.as_sealed_plan())
    assert state.requests[0]["body"]["input"] == native["request"]["input"]
