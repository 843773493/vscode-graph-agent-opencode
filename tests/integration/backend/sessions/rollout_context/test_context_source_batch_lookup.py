"""真实 prepare 中冻结来源批次的线性解析及持久化合同。"""

import json
from pathlib import Path

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash, payload_content_length
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
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
