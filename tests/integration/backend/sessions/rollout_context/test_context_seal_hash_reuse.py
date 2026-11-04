"""真实 prepare 的同次摘要复用与 seal 输入首次错误合同。"""

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import rfc8785
from langchain_core.messages import AIMessage, ToolMessage

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.root_compilation import CompiledRootSourceProvenance
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans import contributions as contribution_domain
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly import (
    manifest as assembly_manifest,
    sealing as assembly_writer,
)
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.assembly.plans import manifest
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import (
    assembly as assembly_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import lifecycle, retry
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import DetailUnavailableError
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前文件隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实 prepare fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.mark.parametrize("source_count", [1, 8])
def test_prepare_matches_plan_contributions_with_linear_identity_work(
    production_context, monkeypatch, request, source_count,
):
    saver, session, thread, _connection, manager, prepare = production_context
    for index in range(1, source_count):
        source_id = f"instruction:binding-{index}"
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name=source_id,
            description="贡献匹配装具", internal_locator=f"runtime:binding-{index}", root_placement="root_eligible",
        ), tracking_status="tracked")
        manager.observe(source_id, f"冻结来源 {index}", revision="binding:1")
    post_init = ContextRequestPlan.__post_init__
    attribute = ContextContribution.__getattribute__
    calls, active = [], []

    def observe_plan(plan):
        call = {"sources": len(plan.contributions), "selections": len(plan.selection), "identity_reads": 0}
        calls.append(call)
        active.append(call)
        try:
            return post_init(plan)
        finally:
            active.pop()

    def observe_attribute(contribution, name):
        if name == "contribution_id" and active:
            caller = sys._getframe(1).f_code
            if caller.co_filename == contribution_domain.__file__ and caller.co_name in {
                "resolve_contribution_for_ref", "__init__", "resolve", "<dictcomp>",
            }:
                active[-1]["identity_reads"] += 1
        return attribute(contribution, name)

    monkeypatch.setattr(ContextRequestPlan, "__post_init__", observe_plan)
    monkeypatch.setattr(ContextContribution, "__getattribute__", observe_attribute)
    first = prepare("binding-linear-first")["snapshot"]
    second = prepare("binding-linear-second")["snapshot"]
    assert len(first.contributions) == len(second.contributions) == source_count
    for snapshot in (first, second):
        assert canonical_json_bytes(saver.get_context_assembly(
            session, thread, assembly_id=snapshot.assembly_id,
        ).to_dict()) == canonical_json_bytes(snapshot.to_dict())
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"contribution-binding-{source_count}.json").write_text(
        json.dumps(calls, indent=2), encoding="utf-8",
    )
    print(f"plan contribution binding work ({source_count} sources): {calls}")
    assert any(call["sources"] == source_count and call["selections"] for call in calls)
    # 只观察真实领域校验；每批来源建一次索引，不为每个 selection 再扫全部贡献。
    assert all(call["identity_reads"] <= 2 * call["sources"] for call in calls)


@pytest.mark.parametrize("source_count", [1, 8])
def test_prepare_restores_source_details_in_one_snapshot(
    production_context, monkeypatch, request, source_count,
):
    saver, session, thread, connection, manager, prepare = production_context
    for index in range(1, source_count):
        source_id = f"instruction:snapshot-{index}"
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name=source_id,
            description="快照复用装具", internal_locator=f"instruction-producer://snapshot-{index}",
            root_placement="root_eligible",
        ), tracking_status="tracked")
        manager.observe(source_id, f"冻结来源正文 {index}", revision=f"snapshot-{index}:1")
    storage = saver._storage.storage
    request_content = saver._request_content_for_plan
    open_snapshot = storage.open_read_snapshot
    read_manifest = saver._storage.assemblies.get_context_plan_detail
    read_body = saver._detail_store.read
    read_prepared = saver._detail_store.read_prepared
    contribution_attribute = ContextContribution.__getattribute__
    calls, active = [], []

    def observe_content(*args, **kwargs):
        call = {"owned": [], "borrowed": kwargs.get("snapshot"), "manifests": [], "bodies": [], "prepared": 0, "contribution_id_reads": 0}
        calls.append(call)
        active.append(call)
        try:
            return request_content(*args, **kwargs)
        finally:
            active.pop()

    def observe_open(*args, **kwargs):
        snapshot = open_snapshot(*args, **kwargs)
        if active:
            active[-1]["owned"].append(snapshot)
        return snapshot

    def observe_manifest(*args, **kwargs):
        if active:
            active[-1]["manifests"].append(kwargs.get("snapshot"))
        return read_manifest(*args, **kwargs)

    def observe_body(*args, **kwargs):
        if active:
            active[-1]["bodies"].append(kwargs.get("snapshot"))
        return read_body(*args, **kwargs)

    def observe_prepared(*args, **kwargs):
        if active:
            active[-1]["prepared"] += 1
        return read_prepared(*args, **kwargs)

    def observe_contribution_attribute(contribution, name):
        if name == "contribution_id" and active:
            active[-1]["contribution_id_reads"] += 1
        return contribution_attribute(contribution, name)

    monkeypatch.setattr(saver, "_request_content_for_plan", observe_content)
    monkeypatch.setattr(storage, "open_read_snapshot", observe_open)
    monkeypatch.setattr(saver._storage.assemblies, "get_context_plan_detail", observe_manifest)
    monkeypatch.setattr(saver._detail_store, "read", observe_body)
    monkeypatch.setattr(saver._detail_store, "read_prepared", observe_prepared)
    monkeypatch.setattr(ContextContribution, "__getattribute__", observe_contribution_attribute)
    prepare("source-snapshot-first")
    second = prepare("source-snapshot-second")["snapshot"]
    second.validate_hashes()
    selected = [entry for entry in second.selection if entry.included and entry.ref.ref_type == "request_only"]
    assert len(selected) == source_count
    second_bytes = canonical_json_bytes(second.to_dict())
    before = connection.execute("SELECT assembly_id,request_hash FROM context_assemblies ORDER BY rowid").fetchall()
    target = saver._detail_store._files.path(selected[-1].detail_ref)
    assert target is not None
    original = target.read_bytes()
    try:
        target.write_bytes(b"{}")
        with pytest.raises(DetailUnavailableError, match="typed v3"):
            prepare("source-snapshot-failed")
        assert connection.execute("SELECT assembly_id,request_hash FROM context_assemblies ORDER BY rowid").fetchall() == before
    finally:
        target.write_bytes(original)
    recovered = prepare("source-snapshot-recovered")["snapshot"]
    recovered.validate_hashes()
    assert canonical_json_bytes(saver.get_context_assembly(session, thread, assembly_id=second.assembly_id).to_dict()) == second_bytes
    measurements = [
        {"opened": len(call["owned"]), "manifests": len(call["manifests"]), "bodies": len(call["bodies"]), "prepared": call["prepared"], "contribution_id_reads": call["contribution_id_reads"]}
        for call in calls
    ]
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"source-detail-snapshots-{source_count}.json").write_text(
        json.dumps(measurements, indent=2), encoding="utf-8",
    )
    print(f"source detail snapshot calls ({source_count} sources): {measurements}")
    disk_reads = [call for call in calls if call["manifests"]]
    assert len(disk_reads) == 3
    for call in disk_reads:
        assert call["borrowed"] is None
        assert len(call["owned"]) == 1
        snapshot, = call["owned"]
        assert snapshot.closed
        assert call["manifests"] == [snapshot] * source_count
        assert call["bodies"] == [snapshot] * source_count
    prepared_reads = [call for call in calls if call["prepared"]]
    assert prepared_reads
    assert all(call["prepared"] == source_count and call["owned"] == [] for call in prepared_reads)
    # 同时约束真实 prepare 的身份查找工作量，避免来源增多时退回逐份全表扫描。
    assert all(call["contribution_id_reads"] <= 3 * source_count for call in calls)


@pytest.mark.parametrize("source_count", [1, 8])
def test_prepare_promotes_source_frontiers_with_linear_identity_lookup(
    production_context, monkeypatch, request, source_count,
):
    saver, session, thread, connection, manager, prepare = production_context
    prepare("frontier-root-initial")
    source_ids = {f"instruction:frontier-{index}" for index in range(source_count)}
    for source_id in sorted(source_ids):
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name=source_id,
            description="来源前沿装具", internal_locator=f"instruction-producer://{source_id}",
            root_placement="root_eligible",
        ), tracking_status="tracked")
        manager.observe(source_id, f"冻结来源 {source_id}", revision="frontier:1")
    original_frozen = assembly_manifest._frozen_source_refs
    original_attribute = CompiledRootSourceProvenance.__getattribute__
    calls, active = [], []

    class ObservedSources(dict):
        def __init__(self, sources, call):
            super().__init__(sources)
            self.call = call

        def items(self):
            for item in super().items():
                self.call["item_visits"] += 1
                yield item

    def observe_attribute(provenance, name):
        if name == "source_identity" and active:
            active[-1]["identity_reads"] += 1
        return original_attribute(provenance, name)

    def observe_frozen(snapshot):
        call = {"identity_reads": 0, "item_visits": 0}
        calls.append(call)
        active.append(call)
        try:
            sources, roots = original_frozen(snapshot)
        finally:
            active.pop()
        call.update(sources=len(sources), roots=len(roots))
        return ObservedSources(sources, call), roots

    monkeypatch.setattr(CompiledRootSourceProvenance, "__getattribute__", observe_attribute)
    monkeypatch.setattr(assembly_manifest, "_frozen_source_refs", observe_frozen)
    injected = prepare("frontier-injected")["snapshot"]
    assert source_ids <= {
        ref.source_identity for frame in injected.seal_binding.prefix.state.frames if frame.role == "user"
        for ref in frame.item_refs if ref.ref_type == "source_item"
    }
    tools = ToolSetSnapshot(selection_revision=sha256_jcs("frontier-epoch-change"), tools=({
        "type": "function", "function": {"name": "frontier_epoch", "parameters": {"type": "object"}},
    },))
    promoted = prepare(
        "frontier-promoted", supplier=lambda: tools,
        request_messages=(
            AIMessage(
                content="来源激活之后的 canonical 回答", id="frontier-later-answer",
                response_metadata={"model_call_id": "frontier-later-model"},
                tool_calls=[{"id": "frontier-later-tool", "name": "frontier_epoch", "args": {}}],
            ),
            ToolMessage(content="后续工具结果", id="frontier-later-result",
                        tool_call_id="frontier-later-tool", name="frontier_epoch"),
        ),
    )["snapshot"]
    assert source_ids <= {source.source_identity for source in promoted.seal_binding.root.sources}

    def frontiers(snapshot):
        contributions = {source.contribution_id: source for source in snapshot.contributions}
        return {
            contributions[entry.contribution_id].source_binding.source_ref: connection.execute(
                "SELECT source_cut_position_kind,source_cut_item_id FROM context_assembly_selections "
                "WHERE assembly_id=? AND ref_id=?", (snapshot.assembly_id, entry.ref.ref_id),
            ).fetchone()
            for entry in snapshot.selection if entry.included and entry.contribution_id in contributions
            and contributions[entry.contribution_id].source_binding.source_ref in source_ids
        }

    root_item, = connection.execute(
        "SELECT root_input_item_id FROM turn_records WHERE session_id=? AND thread_id=? AND turn_id=?",
        (session, thread, injected.turn_id),
    ).fetchone()
    assert any(
        entry.included and entry.ref.ref_type == "canonical_item"
        and entry.ref.semantic_kind == "tool_result" and entry.ref.ref_id != root_item
        for entry in promoted.selection
    )
    assert frontiers(injected) == frontiers(promoted) == dict.fromkeys(source_ids, ("after_canonical", root_item))
    frozen = tuple(canonical_json_bytes(snapshot.to_dict()) for snapshot in (injected, promoted))
    assert tuple(canonical_json_bytes(saver.get_context_assembly(
        session, thread, assembly_id=snapshot.assembly_id,
    ).to_dict()) for snapshot in (injected, promoted)) == frozen
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"source-frontier-lookups-{source_count}.json").write_text(
        json.dumps(calls, indent=2), encoding="utf-8",
    )
    print(f"source frontier lookup work ({source_count} promoted): {calls}")
    assert any(call["roots"] >= source_count for call in calls)
    # 只观察真实计算，不替换来源事实；身份匹配与前驱遍历均应随来源数线性增长。
    assert all(call["identity_reads"] <= call["sources"] + call["roots"] for call in calls)
    assert all(call["item_visits"] <= 2 * call["sources"] for call in calls)


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


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_prepare_persists_the_once_encoded_snapshot_and_cold_retry_revalidates(
    production_context, monkeypatch, target_format,
):
    saver, session, thread, connection, _manager, prepare = production_context
    selected = ToolSetSnapshot(
        selection_revision=sha256_jcs("canonical-seal-tools"),
        tools=({
            "name": "seal_bytes", "parameters": {"type": "object", "properties": {
                "value": {"default": -0.0, "minimum": 1e-7,
                          "examples": [{"\ue000": "尾", "𐀀": "引号\"\\\n"}]},
            }},
        },),
    )
    original_encode = rfc8785.dumps
    original_preflight = assembly_writer.preflight_seal
    writing = False
    encodings = []

    def encode(value):
        encoded = original_encode(value)
        if writing and isinstance(value, dict) and (
            (value.get("schema") == "context-plan-seal:v1" and "snapshot" in value)
            or "request_hash_preimage" in value
        ):
            encodings.append((value, encoded))
        return encoded

    def preflight(*args, **kwargs):
        nonlocal writing
        writing = True
        try:
            return original_preflight(*args, **kwargs)
        finally:
            writing = False

    monkeypatch.setattr(rfc8785, "dumps", encode)
    monkeypatch.setattr(assembly_writer, "preflight_seal", preflight)
    prepared = prepare("canonical-seal", supplier=lambda: selected, target_format=target_format)
    snapshot = prepared["snapshot"]
    # 真实 writer 对这份大 snapshot 只编码一次；同次 seal 与持久文本共用该结果。
    assert len(encodings) == 1
    seal_preimage, encoded_seal = encodings[0]
    assert seal_preimage["schema"] == "context-plan-seal:v1"
    persisted, detail_key = connection.execute(
        "SELECT snapshot_json,detail_ref FROM context_assemblies WHERE assembly_id=?",
        (snapshot.assembly_id,),
    ).fetchone()
    restored = load_snapshot(connection, persisted)
    assert original_encode(restored) == original_encode(seal_preimage["snapshot"])
    assert ContextAssemblySnapshot.from_dict(restored).to_dict() == snapshot.to_dict()
    registration = saver.get_context_plan_registration(session, thread_id=thread, plan_id=snapshot.plan_id)
    expected_hash = "sha256:jcs:v1:" + hashlib.sha256(original_encode({
        "schema": "context-plan-runtime-seal:v1",
        "seal_input_hash": registration.seal_input_hash,
        "sealed_hash": "sha256:jcs:v1:" + hashlib.sha256(encoded_seal).hexdigest(),
    })).hexdigest()
    assert registration.seal_hash == expected_hash
    assert seal_preimage["detail_ref"] == detail_key

    def forbid_snapshot_projection(_snapshot):
        raise AssertionError("cold registry 不得重新投影大 snapshot 来计算 seal hash")

    # 经过真实 registry 入口证明 cold hash 消费当次 SQL 文本；typed parse、
    # draft/plan/request 与派生索引仍完整验证，两次读取不共享对象或校验结果。
    with (
        monkeypatch.context() as cold_patch,
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold,
    ):
        cold_patch.setattr(ContextAssemblySnapshot, "to_dict", forbid_snapshot_projection)
        cold_first = cold.get_context_plan_registration(session, thread_id=thread, plan_id=snapshot.plan_id)
        cold_second = cold.get_context_plan_registration(session, thread_id=thread, plan_id=snapshot.plan_id)
        assert cold_first.sealed_snapshot is not cold_second.sealed_snapshot
        assert cold_first.seal_hash == cold_second.seal_hash == registration.seal_hash
    retried = prepare("canonical-seal", supplier=lambda: selected, target_format=target_format)
    assert retried["snapshot"].to_dict() == snapshot.to_dict()
    assert len(encodings) == 1
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        first = cold.read_context_for_dispatch(prepared["dispatch_ref"])
        second = cold.read_context_for_dispatch(prepared["dispatch_ref"])
    assert first["snapshot"] is not second["snapshot"]
    assert first["snapshot"].to_dict() == second["snapshot"].to_dict() == snapshot.to_dict()
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'",
    ).fetchone() == (1,)


def test_seal_encoding_preserves_envelope_bytes_and_observes_each_snapshot(production_context):
    _saver, _session, _thread, _connection, _manager, prepare = production_context
    snapshot = prepare("seal-envelope-vectors")["snapshot"]
    input_hash = sha256_jcs("seal-envelope-input")
    for detail_key in (None, '引号"\\\n𐀀\ue000,"snapshot":'):
        encoded, digest = manifest.runtime_seal_bytes_and_hash(snapshot, detail_key, input_hash)
        assert encoded == canonical_json_bytes({
            "schema": "context-plan-seal:v1", "snapshot": snapshot.to_dict(),
            "detail_ref": detail_key,
        })
        assert digest == sha256_jcs({
            "schema": "context-plan-runtime-seal:v1", "seal_input_hash": input_hash,
            "sealed_hash": sha256_jcs({
                "schema": "context-plan-seal:v1", "snapshot": snapshot.to_dict(),
                "detail_ref": detail_key,
            }),
        })
        assert manifest.persisted_runtime_seal_hash(
            canonical_json_bytes(snapshot.to_dict()).decode(), detail_key, input_hash,
        ) == digest
    damaged = ContextAssemblySnapshot.from_dict(snapshot.to_dict())
    object.__setattr__(damaged, "request_hash_preimage", {"changed": "新正文"})
    changed_bytes, changed_hash = manifest.runtime_seal_bytes_and_hash(damaged, detail_key, input_hash)
    assert changed_bytes != encoded and changed_hash != digest
    with pytest.raises(ItemSchemaError, match="request_hash"):
        damaged.validate_hashes()
    object.__setattr__(damaged, "request_hash_preimage", {"invalid": object()})
    with pytest.raises(ValueError, match="seal_input_hash"):
        manifest.runtime_seal_bytes_and_hash(damaged, detail_key, "not-a-hash")
    with pytest.raises(ItemSchemaError, match="value.snapshot.request_hash_preimage.invalid"):
        manifest.runtime_seal_bytes_and_hash(damaged, detail_key, input_hash)


@pytest.mark.parametrize("damage", ["whitespace", "unknown-field"])
def test_cold_registry_hash_binds_actual_snapshot_text(production_context, damage):
    saver, session, thread, connection, _manager, prepare = production_context
    snapshot = prepare("cold-text-integrity")["snapshot"]
    original = connection.execute(
        "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (snapshot.assembly_id,),
    ).fetchone()[0]
    altered = " " + original if damage == "whitespace" else original[:-1] + ',"unknown":null}'
    with connection:
        connection.execute(
            "UPDATE context_assemblies SET snapshot_json=? WHERE assembly_id=?",
            (altered, snapshot.assembly_id),
        )
    expected_error = "source-mismatch: assembly content manifest 非canonical JSON" if damage == "whitespace" else "source-mismatch: sealed plan preimage 与 assembly 不一致"
    with (
        RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold,
        pytest.raises(ValueError, match=expected_error),
    ):
        cold.get_context_plan_registration(session, thread_id=thread, plan_id=snapshot.plan_id)
    assert connection.execute(
        "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (snapshot.assembly_id,),
    ).fetchone() == (altered,)
