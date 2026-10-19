"""同名扩展控制键经过真实 seal、fork 和来源离线后的冷读取仍不生效。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.request_plan import ContextContribution, SourceBinding
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    SourceObservation,
    build_source_lifecycle_decision,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)
from tests.unit.services.infrastructure.rollout_context.conftest import SourceSealCase


@pytest.mark.asyncio
async def test_extensions_preserve_typed_controls_through_seal_fork_and_cold_read(
    source_seal_case: SourceSealCase, session_bundle_factory, fork_deadlines,
):
    case = source_seal_case
    saver, owner = case.saver, case.owner
    session, thread = owner.session_id, owner.thread_id
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "extensions-fork-checkpoint"
    checkpoint["channel_values"] = {"messages": [HumanMessage(
        content="真实 source seal 合同", id=str(case.accepted["root_input_item_id"]).removeprefix("item-"),
        response_metadata={"turn_id": case.accepted["turn_id"]},
    )]}
    checkpoint["channel_versions"] = {"messages": "1"}
    execution = str(case.accepted["initial_execution_id"])
    admission = saver.execution_admission(session, thread_id=thread, execution_id=execution)
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        saver.put(build_checkpoint_config(thread), checkpoint, {"source": "extensions-fork"}, {"messages": "1"})
    annotations = {
        "source_ordinal": 999, "replaceable_source": True,
        "replacement_policy": "replaceable", "selection_only": True,
        "selection_role": "backing_only", "tracking_state": "untracked",
        "tracking_status": "untracked", "role": "delta", "base_delta_role": "delta",
        "source_binding": {"source_ref": "forged-source"}, "root_placement": "tail_only",
    }
    extensions = {"vendor.future/context": {
        "extension_version": "future-19", "protection": "public", "value": annotations,
    }}
    contribution = ContextContribution(
        contribution_id="opaque-root", source_kind="environment", source_revision="root-v1",
        source_binding=SourceBinding(source_ref="typed-root"), root_placement="root_eligible",
        body="唯一 typed 根正文", content_hash=contribution_content_hash("prompt", "唯一 typed 根正文"),
        extensions=extensions, selection_role="direct", replacement_policy="immutable",
    )
    saver.register_context_contribution(session, thread, contribution)
    observation = SourceObservation(
        owner=owner, source_id="opaque-observation", source_kind="workspace_instructions",
        name="扩展来源", revision="source-v1", tracking_mode="tracked", content="来源第一版",
        extensions=extensions,
    )
    base = build_source_lifecycle_decision(observation, decision_kind="base")
    delta = build_source_lifecycle_decision(
        replace(observation, revision="source-v2", from_revision="source-v1", content="来源第二版"),
        decision_kind="delta",
    )
    for decision in (base, delta):
        saver.consume_mutation_intent(decision)
    (control,) = saver.load_context_source_control_states(owner)
    assert control.tracking_status == "tracked"
    assert control.latest_revision == "source-v2"
    assert control.latest_visible_committed_revision is None
    assert (base.decision_kind, base.from_revision, delta.decision_kind, delta.from_revision) == (
        "base", None, "delta", "source-v1",
    )

    draft = saver.compose_committed_context_plan(
        session, thread, plan_id="extensions-fork-plan",
        tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
    )
    assert draft.contributions[0].source_ordinal == 0
    registered = saver.create_context_plan(
        session, replace(draft, plan_creation_idempotency_key="extensions-fork-create"),
    )
    snapshot = saver.seal_context_plan(
        session, thread, registered.draft,
        wire_profile=None, wire_params=None, turn_id=str(case.accepted["turn_id"]),
        execution_id=execution, provider_version="extensions-fork-contract",
        seal_idempotency_key="extensions-fork-seal",
        request_only_content={contribution.contribution_id: contribution.body},
    )
    assert snapshot.sealed
    (sealed_source,) = [value for value in snapshot.contributions if value.contribution_id == contribution.contribution_id]
    assert sealed_source.extensions == extensions
    assert sealed_source.source_binding == SourceBinding(source_ref="typed-root")
    # registry slot 与 assembly 内 ordinal 分属不同 owner，sealed contribution
    # 按正式合同不复制 source_ordinal；最终顺序由 selection 固化。
    assert sealed_source.source_ordinal is None
    assert (sealed_source.selection_role, sealed_source.replacement_policy, sealed_source.root_placement) == (
        "direct", "immutable", "root_eligible",
    )
    (selected,) = [entry for entry in snapshot.selection if entry.contribution_id == contribution.contribution_id]
    assert selected.included and selected.ref.ref_type == "request_only"
    assert selected.contribution_ordinal == 0
    records = saver._storage.storage.read_items(session, thread, item_ids=(base.item_id, delta.item_id))
    assert [record.extensions for record in records] == [extensions, extensions]
    assert [record.source_manifest.source_revision for record in records] == ["source-v1", "source-v2"]

    sessions = saver._storage.storage.sessions_dir
    target = create_prefixed_id("ses")
    session_bundle_factory(sessions, target)
    target_thread = saver._storage.storage.main_thread_id(target)
    await copy_rollout_snapshot(saver, session, target, fork_deadlines=fork_deadlines)
    source_root = saver._storage.storage.root(session, thread_id=thread)
    source_root.rename(source_root.with_name("source-offline"))
    jsonl = saver._storage.storage.jsonl_path(target, "", thread_id=target_thread)
    before = jsonl.read_bytes()
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(target, target_thread, assembly_id=snapshot.assembly_id)
        assert restored == snapshot
        restored_records = restarted._storage.storage.read_items(
            target, target_thread, item_ids=(base.item_id, delta.item_id),
        )
        assert restored_records == records
        assert restored.session_id == session and restored.thread_id == thread
        (registry_row,) = restarted._storage.assemblies.list_context_contributions(target, target_thread)
        assert registry_row["source_ordinal"] == 0
        bodies = restarted._request_content_for_plan(target, target_thread, "", restored.as_sealed_plan(), None)
        assert bodies[contribution.contribution_id] == contribution.body
    assert jsonl.read_bytes() == before
