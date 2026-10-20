"""CSM 只读准备、真实 assembly seal 与 receipt 确认的原子合同。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import content_hash, sha256_jcs
from app.domain.itemized.identity.item_projection import ItemSourceManifest
from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    ContextMutationIntent,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.seal import preparation
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    CommittedContextSourceBatch,
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    SourceObservation,
    build_source_lifecycle_decision,
)


def _descriptor(source_id: str, name: str) -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id=source_id, source_kind="skill", name=name,
        description=name + " 工作流",
        internal_locator="/.boxteam/skills/" + name + "/SKILL.md",
    )


def _activate(manager: ContextSourceManager, source_id: str, name: str, content: str) -> None:
    manager.register(_descriptor(source_id, name))
    manager.activate_skill_content(name, content)


def _source_owner_snapshot(saver, owner):
    control = saver.load_context_source_control_states(owner)
    items = tuple(saver._storage.storage.read_items(owner.session_id, owner.thread_id))
    with closing(
        saver._storage.storage._connect(owner.thread_id, session_id=owner.session_id),
    ) as connection:
        commits = tuple(
            tuple(row) for row in connection.execute(
                "SELECT * FROM storage_commits ORDER BY commit_id",
            )
        )
    return control, items, commits


def test_prepare_pending_is_readonly_and_sealed_receipt_is_idempotent(source_seal_case):
    events = []
    manager = source_seal_case.manager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    batch = manager.prepare_pending()
    assert batch is not None
    assert manager.prepare_pending() == batch
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == before
    assert before[0].latest_visible_committed_revision is None
    assert events == []

    receipt, binding = source_seal_case.seal(manager)
    assert isinstance(receipt, CommittedContextSourceBatch)
    assert receipt.deltas == batch.deltas
    assert receipt.assembly_id == binding.prefix.assembly_id
    assert [event.kind for event in events] == ["committed"]
    assert manager.prepare_pending() is None
    assert manager.acknowledge_sealed_pending(batch, binding) == receipt
    assert [event.kind for event in events] == ["committed"]


def test_failed_real_seal_never_advances_memory_applied_or_events(source_seal_case):
    events = []
    manager = source_seal_case.manager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    owner, saver = source_seal_case.owner, source_seal_case.saver
    before = saver.load_context_source_control_states(owner)
    with closing(saver._storage.storage._connect(owner.thread_id, session_id=owner.session_id)) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_source_seal BEFORE INSERT ON context_assemblies "
            "BEGIN SELECT RAISE(ABORT, 'injected assembly failure'); END"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected assembly failure"):
            source_seal_case.seal(manager)
        assert events == []
        assert manager.prepare_pending() == batch
        assert saver.load_context_source_control_states(owner) == before
        assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (0,)
        connection.execute("DROP TRIGGER refuse_source_seal")
        connection.commit()

    receipt, _ = source_seal_case.seal(manager)
    assert receipt.deltas == batch.deltas
    assert [event.kind for event in events] == ["committed"]
    (stored,) = saver.load_context_source_control_states(owner)
    assert stored.latest_visible_committed_revision == stored.latest_revision


def test_activation_is_applied_by_one_seal_without_source_mutation_intent(source_seal_case, monkeypatch):
    consume = source_seal_case.saver.consume_mutation_intent

    def refuse_source_intent(
        intent: ContextMutationIntent, connection: sqlite3.Connection | None = None,
    ) -> None:
        if isinstance(intent, ApplySourceLifecycleDecision):
            pytest.fail("model-call 来源只能由 seal 原子应用，不能再提交 source intent")
        consume(intent, connection)

    monkeypatch.setattr(source_seal_case.saver, "consume_mutation_intent", refuse_source_intent)
    manager = source_seal_case.manager()
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch.deltas[0].kind == "activation"
    _, binding = source_seal_case.seal(manager)
    (applied,) = binding.source_revisions
    assert applied.source_id == "src_a"
    assert applied.revision == batch.deltas[0].revision
    assert applied.previous_revision is None


@pytest.mark.parametrize(
    "root_placement,policy_key",
    [("root_eligible", "filesystem_rules"), ("tail_only", None)],
)
def test_real_lifecycle_preserves_descriptor_root_policy(source_seal_case, root_placement, policy_key):
    manager = source_seal_case.manager()
    manager.register(
        ContextSourceDescriptor(
            source_id="instruction:R06", source_kind="filesystem_rules",
            name="Filesystem 规则", description="文件访问规则",
            internal_locator="instruction-producer://R06",
            root_placement=root_placement, policy_key=policy_key,
        ), tracking_status="tracked",
    )
    manager.observe("instruction:R06", "遵守工作区路径规则")
    selected = ToolSetSnapshot(
        selection_revision=sha256_jcs({"read_file": True}),
        tools=({"name": "read_file", "parameters": {"type": "object"}},),
        policy_keys=("filesystem_rules",),
    )
    _, binding = source_seal_case.seal(manager, tool_set=selected)
    if root_placement == "root_eligible":
        assert binding.root is not None
        assert [source.source_identity for source in binding.root.sources] == ["instruction:R06"]
    else:
        assert any(frame.item_ref.source_identity == "instruction:R06" for frame in binding.prefix.state.frames)


def test_first_observation_preserves_semantic_revision_through_prepare_and_seal(source_seal_case):
    manager = source_seal_case.manager()
    manager.register(
        ContextSourceDescriptor(
            source_id="instruction:R01", source_kind="agent_instructions", name="Agent 说明",
            description="Agent 说明", internal_locator="instruction-producer://R01",
            root_placement="root_eligible",
        ), tracking_status="tracked",
    )
    assert manager.observe("instruction:R01", "第一版说明", revision="agent-config-revision:7")
    batch = manager.prepare_pending()
    assert batch.deltas[0].revision == "agent-config-revision:7"
    assert batch.deltas[0].content_hash != batch.deltas[0].revision
    assert batch.deltas[0].observation_provenance == ("agent-config-revision:7",)
    (stored,) = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)
    assert stored.latest_revision == "agent-config-revision:7"
    assert stored.latest_visible_committed_revision is None
    _, binding = source_seal_case.seal(manager)
    assert binding.source_revisions[0].revision == "agent-config-revision:7"
    assert manager.observe("instruction:R01", "第二版说明", revision="agent-config-revision:8")
    second = manager.prepare_pending().deltas[0]
    assert second.revision == "agent-config-revision:8"
    assert second.previous_revision == "agent-config-revision:7"
    assert second.observation_provenance == ("agent-config-revision:8",)


@pytest.mark.parametrize("decision_kind", ["base", "delta", "rebuild"])
def test_ordinary_lifecycle_intent_keeps_visible_revision_until_real_seal(
    source_seal_case, tmp_path, decision_kind,
):
    manager = source_seal_case.manager()
    _activate(manager, "src_a", "a", "已封存正文")
    source_seal_case.seal(manager)
    saver, owner = source_seal_case.saver, source_seal_case.owner
    (before_state,) = saver.load_context_source_control_states(owner)
    prior_visible_revision = before_state.latest_visible_committed_revision
    assert prior_visible_revision == before_state.latest_revision

    prior_items = saver._storage.storage.read_items(owner.session_id, owner.thread_id)
    prior_last_sequence = max(
        (item.item_sequence for item in prior_items), default=0,
    )
    content = f"{decision_kind} 生命周期 intent 正文"
    observation = SourceObservation(
        owner=owner,
        source_id="src_a",
        source_kind="skill",
        name="a",
        revision=f"ordinary-{decision_kind}-revision",
        tracking_mode="tracked",
        from_revision=(
            prior_visible_revision if decision_kind == "delta" else None
        ),
        content=content,
    )
    decision = build_source_lifecycle_decision(
        observation, decision_kind=decision_kind,
    )

    saver.consume_mutation_intent(decision)
    (after_state,) = saver.load_context_source_control_states(owner)
    assert after_state.latest_visible_committed_revision == prior_visible_revision
    assert after_state.latest_revision == decision.revision
    assert after_state.tracking_status == "tracked"
    assert after_state.state_revision == before_state.state_revision + 1

    (item,) = saver._storage.storage.read_items(
        owner.session_id, owner.thread_id, item_ids=(decision.item_id,),
    )
    assert item.payload == content
    assert item.source_manifest == ItemSourceManifest(
        decision.revision, "internal", "public",
    )
    assert item.item_sequence == prior_last_sequence + 1
    assert item.content_hash == content_hash("text", content)

    saver.consume_mutation_intent(decision)
    (replayed_state,) = saver.load_context_source_control_states(owner)
    (replayed_item,) = saver._storage.storage.read_items(
        owner.session_id, owner.thread_id, item_ids=(decision.item_id,),
    )
    assert replayed_state == after_state
    assert replayed_item == item

    restored_saver = RolloutCheckpointSaver(
        tmp_path / ".boxteam" / "sessions",
    )
    (restored_state,) = restored_saver.load_context_source_control_states(owner)
    (restored_item,) = restored_saver._storage.storage.read_items(
        owner.session_id, owner.thread_id, item_ids=(decision.item_id,),
    )
    assert restored_state == after_state
    assert restored_item == item


@pytest.mark.parametrize(
    "decision_kind", ["base", "delta", "rebuild", "observe_pending"],
)
def test_owner_rejects_source_decision_without_canonical_item(
    source_seal_case, decision_kind,
):
    saver, owner = source_seal_case.saver, source_seal_case.owner
    observation = SourceObservation(
        owner=owner,
        source_id=f"missing-item:{decision_kind}",
        source_kind="skill",
        name=f"missing-item-{decision_kind}",
        revision=f"revision-{decision_kind}",
        tracking_mode="tracked",
        from_revision="previous-revision" if decision_kind == "delta" else None,
        content="冻结来源正文",
    )
    decision = build_source_lifecycle_decision(
        observation,
        decision_kind=decision_kind,
        pending_only=(decision_kind == "observe_pending"),
    )
    before = _source_owner_snapshot(saver, owner)
    with pytest.raises(ValueError, match="必须携带对应 CanonicalItemRecord"):
        saver.apply_source_lifecycle_decision(decision, None)
    assert _source_owner_snapshot(saver, owner) == before


@pytest.mark.parametrize(
    "decision_kind,tracking_mode,tracking_status",
    [("track", "tracked", "tracked"), ("untrack", "untrack", "untracked")],
)
def test_control_decision_keeps_itemless_contract_at_owner(
    source_seal_case, decision_kind, tracking_mode, tracking_status,
):
    saver, owner = source_seal_case.saver, source_seal_case.owner
    source_observation = SourceObservation(
        owner=owner, source_id="control-source", source_kind="skill",
        name="control-source", revision="source-revision",
        tracking_mode="tracked", content="source item",
    )
    source_decision = build_source_lifecycle_decision(
        source_observation, decision_kind="base",
    )
    saver.consume_mutation_intent(source_decision)
    (item,) = saver._storage.storage.read_items(
        owner.session_id, owner.thread_id, item_ids=(source_decision.item_id,),
    )

    observation = SourceObservation(
        owner=owner, source_id=f"{decision_kind}-source", source_kind="skill",
        name=f"{decision_kind}-source", revision="control-revision",
        tracking_mode=tracking_mode, content=None,
    )
    decision = build_source_lifecycle_decision(
        observation, decision_kind=decision_kind,
    )
    before = _source_owner_snapshot(saver, owner)
    assert saver.apply_source_lifecycle_decision(decision, None) == ()
    after_control, after_items, after_commits = _source_owner_snapshot(saver, owner)
    assert after_items == before[1]
    assert after_commits == before[2]
    (control_state,) = tuple(
        state for state in after_control if state.source_id == decision.source_id
    )
    assert control_state.tracking_status == tracking_status

    before_invalid_item = _source_owner_snapshot(saver, owner)
    with pytest.raises(ValueError, match="不得携带.*CanonicalItemRecord"):
        saver.apply_source_lifecycle_decision(decision, item)
    assert _source_owner_snapshot(saver, owner) == before_invalid_item


@pytest.mark.parametrize("revision", ["", 7])
def test_invalid_observation_revision_is_rejected_without_hash_fallback(revision):
    manager = ContextSourceManager()
    manager.register(_descriptor("src_a", "a"), tracking_status="tracked")
    with pytest.raises(ValueError, match="revision 必须是非空字符串"):
        manager.observe("src_a", "beta", revision=revision)


def test_frozen_batch_keeps_source_arriving_after_seal_pending(source_seal_case, monkeypatch):
    events = []
    manager = source_seal_case.manager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    original_batch = manager.prepare_pending()
    original = preparation.seal_registered_plan

    def seal_then_activate(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        _activate(manager, "src_b", "b", "beta")
        return snapshot

    monkeypatch.setattr(preparation, "seal_registered_plan", seal_then_activate)
    receipt, _ = source_seal_case.seal(manager)
    assert receipt.deltas == original_batch.deltas
    assert [event.source_id for event in events] == ["src_a"]
    pending = manager.prepare_pending()
    assert [delta.source_id for delta in pending.deltas] == ["src_b"]
    stored = {state.source_id: state for state in source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)}
    assert stored["src_a"].latest_visible_committed_revision == stored["src_a"].latest_revision
    assert stored["src_b"].latest_visible_committed_revision is None

    monkeypatch.setattr(preparation, "seal_registered_plan", original)
    source_seal_case.seal(manager)
    assert [event.source_id for event in events] == ["src_a", "src_b"]
    assert manager.prepare_pending() is None


def test_acknowledgment_rejects_drifted_batch_without_memory_or_event_advance(source_seal_case):
    events = []
    manager = source_seal_case.manager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    # 另一真实 CSM 使用同一 durable desired 封存，当前待确认 CSM 尚未推进内存。
    sealing_manager = source_seal_case.manager()
    _activate(sealing_manager, "src_a", "a", "alpha")
    _, binding = source_seal_case.seal(sealing_manager)
    assert events == []
    changed = replace(batch.deltas[0], revision="semantic-revision:drifted")
    with pytest.raises(ValueError, match="冻结 delta 与完整 source snapshot 不一致"):
        replace(batch, deltas=(changed,))
    drifted = replace(
        batch, deltas=(changed,),
        sources=(replace(batch.sources[0], revision=changed.revision),),
    )
    with pytest.raises(RuntimeError, match="seal-receipt-conflict"):
        manager.acknowledge_sealed_pending(drifted, binding)
    assert events == []
    assert manager.prepare_pending() == batch
    receipt = manager.acknowledge_sealed_pending(batch, binding)
    assert receipt.deltas == batch.deltas
    assert [event.kind for event in events] == ["committed"]


def test_pure_memory_acknowledgment_reuses_real_seal_receipt(source_seal_case):
    events = []
    manager = ContextSourceManager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    sealing_manager = source_seal_case.manager()
    _activate(sealing_manager, "src_a", "a", "alpha")
    _, binding = source_seal_case.seal(sealing_manager)
    first = manager.acknowledge_sealed_pending(batch, binding)
    assert manager.acknowledge_sealed_pending(batch, binding) == first
    assert manager.prepare_pending() is None
    assert len(events) == 1


def test_acknowledgment_reads_real_port_without_writing_or_consuming_intent(source_seal_case, monkeypatch):
    manager = source_seal_case.manager()
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    sealing_manager = source_seal_case.manager()
    _activate(sealing_manager, "src_a", "a", "alpha")
    _, binding = source_seal_case.seal(sealing_manager)
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)

    def refuse_write(*args, **kwargs):
        raise AssertionError("receipt 确认不得调用 source control writer")

    monkeypatch.setattr(source_seal_case.saver, "apply_context_source_control_mutation", refuse_write)
    monkeypatch.setattr(source_seal_case.saver, "consume_mutation_intent", refuse_write)
    receipt = manager.acknowledge_sealed_pending(batch, binding)
    assert receipt.deltas == batch.deltas
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == before
