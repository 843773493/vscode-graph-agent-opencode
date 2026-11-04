"""真实 Skill 目录冻结、批量显式选择和冷恢复的线性工作量。"""

import asyncio
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.agents.tools.skill_loading import create_skill_load_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.checkpoint.durable import source_activation
from app.services.infrastructure.rollout_context.runtime.context_sources import skill_catalog
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import ContextSourceOwnerKey
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import ContextSourceManager
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 当前测试路径隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_skill_catalog_shadow_binding import (
    skill_catalog_owner as skill_catalog_owner,  # noqa: PLC0414 - 真实文件发布与冻结
)


@pytest.mark.parametrize("source_count", [1, 8])
@pytest.mark.parametrize("cold", [False, True], ids=["resident", "cold"])
async def test_prepare_restores_pending_skills_with_linear_name_work(
    skill_catalog_owner, frozen_provider_wire, frozen_provider_activation, monkeypatch, request,
    source_count, cold,
):
    saver, session, thread, accepted, registry, files, manager = skill_catalog_owner
    names = [f"batch-{index:02d}" for index in range(source_count)]
    for name in names:
        source = files.workspace_root / ".boxteam/skills" / name / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text(f"---\nname: {name}\ndescription: 批量选择\n---\n冻结正文 {name}\n", encoding="utf-8")
    async with asyncio.timeout(10):
        while len(registry.snapshot("skill-entry:workspace:catalog:catalog").payload["entries"]) != source_count + 1:
            await asyncio.sleep(0.01)
    owner = ContextSourceOwnerKey(session, thread)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
    tools = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())
    policy = ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={"skill": "model_call"})

    def prepare(key):
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            return saver.prepare_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                context_source_manager=manager, tool_set_supplier=lambda: tools,
                **frozen_provider_wire("skill-batch", "chat_completions"),
                **frozen_provider_activation(saver, plan_creation_idempotency_key=key, registry=registry, policy=policy),
            )["snapshot"]

    prepare("skill-batch-initial")
    frozen = manager.frozen_skill_catalog()
    tool = create_skill_load_tool(manager, control_admission=lambda _name, _mode: nullcontext(),
        activation_snapshot_resolver=lambda: frozen)
    for name in names:
        receipt = json.loads(await tool.ainvoke({"name": name}))
        assert receipt["status"] == "loaded" and receipt["queued"] and not receipt["tracked"]
    before = saver.load_context_source_control_states(owner)
    selected_ids = {"skill:" + frozen.entries[name].entry_identity for name in names}
    assert {state.source_id for state in before if state.selection_sequence > state.latest_visible_selection_sequence} == selected_ids
    if cold:
        manager = ContextSourceManager(owner=owner, control_state_port=saver)
    restore = ContextSourceManager.restore_frozen_skill_selections
    normalize = skill_catalog._normalize_skill_name
    calls, active = [], []
    seals, active_seals = [], []
    apply_sources = source_activation.ContextSourceActivationMixin._apply_sealed_context_sources
    from_row = source_activation._state_from_row

    def observe_apply(context_store, connection, snapshot, *, previous):
        call = {"skill_scans": 0, "state_decodes": 0}
        seals.append(call)
        active_seals.append(call)

        def trace(sql):
            if "FROM context_source_control_states " in sql and "AND source_kind='skill'" in sql:
                call["skill_scans"] += 1

        connection.set_trace_callback(trace)
        try:
            return apply_sources(context_store, connection, snapshot, previous=previous)
        finally:
            connection.set_trace_callback(None)
            active_seals.pop()

    def observe_row(row):
        if active_seals:
            active_seals[-1]["state_decodes"] += 1
        return from_row(row)

    def observe_restore(context_sources, snapshot):
        call = {"skills": sum(state.source_kind == "skill" for state in context_sources._sources.values()), "normalizations": 0}
        calls.append(call)
        active.append(call)
        try:
            return restore(context_sources, snapshot)
        finally:
            active.pop()

    def observe_name(name):
        if active:
            active[-1]["normalizations"] += 1
        return normalize(name)

    monkeypatch.setattr(ContextSourceManager, "restore_frozen_skill_selections", observe_restore)
    monkeypatch.setattr(skill_catalog, "_normalize_skill_name", observe_name)
    monkeypatch.setattr(source_activation.ContextSourceActivationMixin, "_apply_sealed_context_sources", observe_apply)
    monkeypatch.setattr(source_activation, "_state_from_row", observe_row)
    selected = prepare("skill-batch-selected")
    unchanged = prepare("skill-batch-unchanged")
    blocks = [block for frame in selected.seal_binding.prefix.state.frames if frame.source_protocol is not None
              for block in parse_source_blocks(frame.content) if block.source_identity in selected_ids]
    assert [(block.source_identity, block.revision, block.body) for block in blocks] == [
        ("skill:" + frozen.entries[name].entry_identity, frozen.entries[name].activation_revision, frozen.entries[name].body)
        for name in names
    ]
    assert unchanged.seal_binding.prefix.state.frames == selected.seal_binding.prefix.state.frames
    after = {state.source_id: state for state in saver.load_context_source_control_states(owner)}
    for state in before:
        if state.source_id in selected_ids:
            assert after[state.source_id].selection_sequence == after[state.source_id].latest_visible_selection_sequence == state.selection_sequence
            assert after[state.source_id].latest_visible_committed_revision == state.latest_revision
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as reader:
        for snapshot in (selected, unchanged):
            assert canonical_json_bytes(reader.get_context_assembly(
                session, thread, assembly_id=snapshot.assembly_id,
            ).to_dict()) == canonical_json_bytes(snapshot.to_dict())
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"skill-batch-{source_count}-{cold}.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    print(f"skill selection work ({source_count} sources, cold={cold}): {calls}")
    (context.artifacts_dir / f"skill-seal-{source_count}-{cold}.json").write_text(json.dumps(seals, indent=2), encoding="utf-8")
    print(f"skill seal work ({source_count} sources, cold={cold}): {seals}")
    assert len(calls) == 2 and calls[0]["skills"] >= source_count
    assert calls[0]["normalizations"] <= 3 * calls[0]["skills"]
    assert calls[1]["normalizations"] == 0
    # 同一事务只扫描一次 Skill sibling；各待提交行仍逐个读取并执行 CAS。
    assert [seal["skill_scans"] for seal in seals] == [1, 0]
    assert seals[0]["state_decodes"] == source_count + sum(state.source_kind == "skill" for state in before)
    assert seals[1]["state_decodes"] == 0
