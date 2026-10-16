"""真实 prepare 的同次摘要复用与 seal 输入首次错误合同。"""

from dataclasses import replace

import pytest

from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.seal import (
    assembly as assembly_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import lifecycle, retry
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前文件隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实 prepare fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def seal_registration(registry_db):
    saver, session, _connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    plan = ContextRequestPlan(
        session_id=session, thread_id=thread, plan_id="reuse-draft", refs=(),
        tool_set_refs=(ToolSetRef.from_tool_snapshot(
            session_id=session, thread_id=thread, plan_id="reuse-draft",
            snapshot_id="reuse-tools", source_revision="reuse-tools-v1",
            tools=({"name": "read_file", "parameters": {"type": "object"}},),
        ),),
        plan_creation_idempotency_key="reuse-create",
    )
    return saver.create_context_plan(session, plan)


@pytest.mark.parametrize(
    "case,error,steps",
    [
        ("same", None, ["manifest"]),
        ("equal", None, ["manifest", "manifest"]),
        ("different", "seal 输入与当前注册 draft 不一致", ["manifest", "manifest"]),
        ("owner", "context plan session/thread owner 不一致", []),
        ("state", "seal 输入必须是已注册 unsealed plan", []),
        ("lookup", "lookup-before-manifest", ["lookup"]),
        ("registration-owner", "seal registration owner/plan 不一致", []),
        ("creation", "创建 draft 必须有显式幂等键", ["manifest"]),
        ("registered-creation", "创建 draft 必须有显式幂等键", ["manifest", "manifest"]),
    ],
)
def test_registered_draft_reuse_keeps_first_error_and_distinct_comparison(
    registry_db, seal_registration, monkeypatch, case, error, steps,
):
    saver, session, connection, _accepted = registry_db
    registered = seal_registration
    plan = registered.draft
    if case in {"owner", "state", "lookup", "registration-owner", "creation"}:
        plan = replace(plan, plan_creation_idempotency_key=None)
        registered = replace(registered, draft=plan)
    if case == "equal":
        plan = replace(plan)
    elif case == "different":
        plan = replace(plan, compiler_version="different-compiler")
    elif case == "state":
        plan = plan.seal_for_assembly("already-sealed", selection=())
    elif case == "registration-owner":
        registered = replace(registered, thread_id="foreign-thread")
    elif case == "registered-creation":
        registered = replace(
            registered, draft=replace(plan, plan_creation_idempotency_key=None),
        )
    events, projected = [], []
    original_manifest = retry.draft_manifest

    def manifest(value):
        events.append("manifest")
        projected.append(value)
        return original_manifest(value)

    def lookup(*args, **kwargs):
        events.append("lookup")
        raise ValueError("lookup-before-manifest")

    monkeypatch.setattr(retry, "draft_manifest", manifest)
    monkeypatch.setattr(saver, "get_context_plan_registration", lookup)

    def require():
        return retry.require_registered_draft(
            saver, "foreign-session" if case == "owner" else session,
            plan.thread_id, plan, "", registered=None if case == "lookup" else registered,
        )

    if error is None:
        actual, verified = require()
        assert actual is registered
        assert verified == original_manifest(registered.draft)
        assert verified["tool_set_refs"] == [
            plan.tool_set_refs[0].to_dict()
        ]
    else:
        with pytest.raises(ValueError, match=error):
            require()
    assert events == steps
    if projected:
        assert projected[0] is plan
    if len(projected) == 2:
        assert projected[1] is registered.draft
        assert projected[0] is not projected[1]
    assert connection.execute("SELECT plan_state FROM context_plans").fetchall() == [("unsealed",)]
    assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_prepare_reuses_only_current_draft_and_final_wire_plan_digest(
    production_context, monkeypatch, target_format,
):
    _saver, _session, _thread, _connection, manager, prepare = production_context
    original_manifest = retry.draft_manifest
    original_input_hash = lifecycle.seal_input_hash
    original_plan_hash = ContextRequestPlan.plan_hash
    original_assembly = ContextPlanComposer.assembly
    original_wire_hash = assembly_seal._context_request_hash_preimage
    original_seal = lifecycle.seal_snapshot
    phase = {"name": "prepare"}
    manifests, consumed, composed, hashes, finalized = [], [], [], [], []

    def manifest(plan):
        value = original_manifest(plan)
        manifests.append(value)
        return value

    def input_hash(value, *args, **kwargs):
        consumed.append(value)
        return original_input_hash(value, *args, **kwargs)

    def plan_hash(plan):
        hashes.append((phase["name"], plan.plan_state, plan.assembly_id))
        return original_plan_hash(plan)

    def assembly(composer, *args, **kwargs):
        previous = phase["name"]
        phase["name"] = "composer"
        try:
            snapshot = original_assembly(composer, *args, **kwargs)
            composed.append(snapshot)
            return snapshot
        finally:
            phase["name"] = previous

    def wire_hash(plan, *args, **kwargs):
        previous = phase["name"]
        phase["name"] = "final-wire"
        try:
            finalized.append(plan.assembly_id)
            return original_wire_hash(plan, *args, **kwargs)
        finally:
            phase["name"] = previous

    def seal(*args, **kwargs):
        phase["name"] = "seal"
        result = original_seal(*args, **kwargs)
        phase["name"] = "prepare"
        return result

    monkeypatch.setattr(retry, "draft_manifest", manifest)
    monkeypatch.setattr(lifecycle, "seal_input_hash", input_hash)
    monkeypatch.setattr(ContextRequestPlan, "plan_hash", plan_hash)
    monkeypatch.setattr(ContextPlanComposer, "assembly", assembly)
    monkeypatch.setattr(assembly_seal, "_context_request_hash_preimage", wire_hash)
    monkeypatch.setattr(lifecycle, "seal_snapshot", seal)
    first = prepare("reuse-first", target_format=target_format)
    manager.observe("instruction:R01", "基础规则第二版", revision="instructions:2")
    second = prepare("reuse-second", target_format=target_format)
    assert len(manifests) == len(consumed) == len(composed) == 2
    assert all(value is consumed[index] for index, value in enumerate(manifests))
    assert finalized == [first["assembly_id"], second["assembly_id"]]
    assert not [event for event in hashes if event[0] == "final-wire"]
    assert [event[2] for event in hashes if event[:2] == ("composer", "sealed")] == [
        first["assembly_id"], second["assembly_id"],
    ]
    for provisional, result in zip(composed, (first, second), strict=True):
        snapshot = result["snapshot"]
        assert snapshot.plan_hash == provisional.plan_hash
        assert snapshot.request_hash_preimage != provisional.request_hash_preimage
        assert snapshot.request_hash != provisional.request_hash
        assert snapshot.request_hash == context_request_hash(
            snapshot.as_sealed_plan(), snapshot.provider_version,
            projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
            target_format=snapshot.target_format, wire_request=snapshot.request_hash_preimage,
        )
