"""真实 prepare 中冻结来源批次的线性解析及持久化合同。"""

import json
import sys
from pathlib import Path

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash, payload_content_length
from app.domain.itemized.epoch.prefix_epoch import AppendedItemRef
from app.domain.itemized.epoch.root_compilation import CompiledRootSourceProvenance
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
from app.services.infrastructure.rollout_context.checkpoint.durable import source_activation
from app.services.infrastructure.rollout_context.runtime.context_sources.models import ContextSourceDescriptor
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前文件隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实 prepare fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.mark.parametrize("source_count", [1, 8])
@pytest.mark.parametrize("history_updates", [0, 4])
def test_seal_matches_applied_sources_with_one_prefix_pass(
    production_context, monkeypatch, request, source_count, history_updates,
):
    saver, session, thread, _connection, manager, prepare = production_context
    source_ids = ["instruction:R01"]
    for index in range(1, source_count):
        source_id = f"instruction:seal-{index}"
        source_ids.append(source_id)
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name=source_id,
            description="封存选集装具", internal_locator=f"runtime:seal-{index}",
            root_placement="root_eligible" if index % 2 == 0 else "tail_only",
        ), tracking_status="tracked")
        manager.observe(source_id, f"初始正文 {source_id}", revision="seal:0")
    prepare("seal-lookup-initial")
    for revision in range(1, history_updates + 1):
        for source_id in source_ids:
            manager.observe(source_id, f"历史正文 {source_id}:{revision}", revision=f"seal:{revision}")
        prepare(f"seal-lookup-history-{revision}")
    apply_sources = source_activation.ContextSourceActivationMixin._apply_sealed_context_sources
    calls, active = [], []

    def observe_apply(context_store, connection, snapshot, *, previous):
        binding = snapshot.seal_binding
        call = {"identity_reads": 0, "applied": len(binding.source_revisions),
                "root_sources": len(binding.root.sources) if binding.root else 0,
                "prefix_refs": sum(len(frame.item_refs) for frame in binding.prefix.state.frames)}
        calls.append(call)
        active.append(call)
        try:
            return apply_sources(context_store, connection, snapshot, previous=previous)
        finally:
            active.pop()

    def observe_attribute(original):
        def read(value, name):
            caller = sys._getframe(1).f_code
            if (active and name == "source_identity" and caller.co_filename == source_activation.__file__
                    and caller.co_qualname.startswith("ContextSourceActivationMixin._apply_sealed_context_sources")):
                active[-1]["identity_reads"] += 1
            return original(value, name)
        return read

    monkeypatch.setattr(source_activation.ContextSourceActivationMixin, "_apply_sealed_context_sources", observe_apply)
    for value_type in (AppendedItemRef, CompiledRootSourceProvenance):
        monkeypatch.setattr(value_type, "__getattribute__", observe_attribute(value_type.__getattribute__))
    for source_id in source_ids:
        manager.observe(source_id, f"本次正文 {source_id}", revision="seal:current")
    selected = prepare("seal-lookup-selected")["snapshot"]
    unchanged = prepare("seal-lookup-unchanged")["snapshot"]
    assert unchanged.seal_binding.prefix.state.frames == selected.seal_binding.prefix.state.frames
    assert {source.source_id: source.revision for source in selected.seal_binding.source_revisions} == {
        source_id: "seal:current" for source_id in source_ids
    }
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        assert cold.get_context_assembly(session, thread, assembly_id=selected.assembly_id).to_dict() == selected.to_dict()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"seal-prefix-{source_count}-{history_updates}.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    print(f"seal prefix work ({source_count=}, {history_updates=}): {calls}")
    assert [call["applied"] for call in calls] == [source_count, 0]
    # 同次 seal 最多遍历一次完整前缀；空回执不建立选集索引。
    assert calls[0]["identity_reads"] <= calls[0]["root_sources"] + calls[0]["prefix_refs"]
    assert calls[1]["identity_reads"] == 0


def test_root_only_seal_does_not_scan_prefix_refs(production_context, monkeypatch):
    _saver, _session, _thread, _connection, _manager, prepare = production_context
    original = AppendedItemRef.__getattribute__
    identity_reads = 0

    def observe(ref, name):
        nonlocal identity_reads
        caller = sys._getframe(1).f_code
        if (name == "source_identity" and caller.co_filename == source_activation.__file__
                and caller.co_qualname.startswith("ContextSourceActivationMixin._apply_sealed_context_sources")):
            identity_reads += 1
        return original(ref, name)

    monkeypatch.setattr(AppendedItemRef, "__getattribute__", observe)
    snapshot = prepare("seal-root-only")["snapshot"]
    assert {source.source_identity for source in snapshot.seal_binding.root.sources} == {"instruction:R01"}
    assert [source.source_id for source in snapshot.seal_binding.source_revisions] == ["instruction:R01"]
    assert identity_reads == 0


@pytest.mark.parametrize("source_count", [1, 8])
def test_prepare_resolves_source_delta_placement_with_linear_work(
    production_context, monkeypatch, request, source_count,
):
    saver, session, thread, _connection, manager, prepare = production_context
    source_ids = ["instruction:R01"]
    for index in range(1, source_count):
        source_id = f"instruction:batch-{index}"
        source_ids.append(source_id)
        manager.register(ContextSourceDescriptor(
            source_id=source_id, source_kind="agent_instructions", name=source_id,
            description="批次定位装具", internal_locator=f"runtime:batch-{index}",
            root_placement="root_eligible" if index % 2 == 0 else "tail_only",
        ), tracking_status="tracked")
        manager.observe(source_id, f"冻结来源 {index} 第一版", revision="batch:1")
    original = preparation.source_contributions
    attribute = ContextSourceDescriptor.__getattribute__
    calls, active = [], []

    def observe_attribute(descriptor, name):
        if active and name == "source_id":
            active[-1]["identity_reads"] += 1
        return attribute(descriptor, name)

    def observe_batch(batch):
        call = {"sources": len(batch.sources), "deltas": len(batch.deltas), "identity_reads": 0}
        calls.append(call)
        active.append(call)
        try:
            result = original(batch)
        finally:
            active.pop()
        full, deltas = result[:len(batch.sources)], result[len(batch.sources):]
        assert len(deltas) == len(batch.deltas)
        for contribution, source in zip(full, batch.sources, strict=True):
            assert contribution.contribution_id == f"source-full:{source.descriptor.source_id}:{source.revision}"
        by_id = {source.descriptor.source_id: source for source in batch.sources}
        for contribution, source in zip(result, (*batch.sources, *batch.deltas), strict=True):
            source_id = source.descriptor.source_id if hasattr(source, "descriptor") else source.source_id
            kind = "prompt" if hasattr(source, "descriptor") else "notice"
            assert contribution.source_binding.source_ref == source_id
            assert contribution.source_revision == source.revision
            assert contribution.body == source.content
            assert contribution.content_hash == contribution_content_hash(kind, source.content)
            assert contribution.content_length == payload_content_length("structured_content", source.content)
            assert contribution.root_placement == by_id[source_id].descriptor.root_placement
        return result

    monkeypatch.setattr(preparation, "source_contributions", observe_batch)
    monkeypatch.setattr(ContextSourceDescriptor, "__getattribute__", observe_attribute)
    first = prepare("batch-first")["snapshot"]
    unchanged = prepare("batch-unchanged")["snapshot"]
    for index, source_id in enumerate(source_ids):
        manager.observe(source_id, f"冻结来源 {index} 第二版", revision="batch:2")
    updated = prepare("batch-updated")["snapshot"]
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for snapshot in (first, unchanged, updated):
            restored = cold.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(snapshot.to_dict())
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"source-batch-{source_count}.json").write_text(
        json.dumps(calls, indent=2), encoding="utf-8",
    )
    print(f"source batch lookup work ({source_count} sources): {calls}")
    assert [call["deltas"] for call in calls] == [source_count, 0, source_count]
    # 无 delta 不建额外索引；有 delta 时一次索引消除逐条重扫。
    assert all(call["identity_reads"] <= (3 if call["deltas"] else 2) * call["sources"] for call in calls)
