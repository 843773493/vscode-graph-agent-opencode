"""CSM 只读准备、真实 assembly seal 与 receipt 确认的原子合同。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    CommittedContextSourceBatch,
    ContextSourceDescriptor,
    ContextSourceManager,
)


class RecordingMutationIntentPort:
    """确认 CSM acknowledgment 不再消费第二条 source lifecycle 写入链。"""

    def __init__(self) -> None:
        self.intents: list[object] = []

    def consume_mutation_intent(self, intent: object) -> None:
        self.intents.append(intent)


def _descriptor(source_id: str, name: str) -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id=source_id, source_kind="skill", name=name,
        description=name + " 工作流",
        internal_locator="/.boxteam/skills/" + name + "/SKILL.md",
    )


def _activate(manager: ContextSourceManager, source_id: str, name: str, content: str) -> None:
    manager.register(_descriptor(source_id, name))
    manager.activate_skill_content(name, content)


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


def test_activation_is_applied_by_one_seal_without_source_mutation_intent(source_seal_case):
    mutation_port = RecordingMutationIntentPort()
    manager = source_seal_case.manager(mutation_intent_port=mutation_port)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch.deltas[0].kind == "activation"
    _, binding = source_seal_case.seal(manager)
    (applied,) = binding.source_revisions
    assert applied.source_id == "src_a"
    assert applied.revision == batch.deltas[0].revision
    assert applied.previous_revision is None
    assert mutation_port.intents == []


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
    original = source_seal_case.saver.seal_context_plan

    def seal_then_activate(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        _activate(manager, "src_b", "b", "beta")
        return snapshot

    monkeypatch.setattr(source_seal_case.saver, "seal_context_plan", seal_then_activate)
    receipt, _ = source_seal_case.seal(manager)
    assert receipt.deltas == original_batch.deltas
    assert [event.source_id for event in events] == ["src_a"]
    pending = manager.prepare_pending()
    assert [delta.source_id for delta in pending.deltas] == ["src_b"]
    stored = {state.source_id: state for state in source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)}
    assert stored["src_a"].latest_visible_committed_revision == stored["src_a"].latest_revision
    assert stored["src_b"].latest_visible_committed_revision is None

    monkeypatch.setattr(source_seal_case.saver, "seal_context_plan", original)
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
    mutation_port = RecordingMutationIntentPort()
    manager = source_seal_case.manager(mutation_intent_port=mutation_port)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    sealing_manager = source_seal_case.manager()
    _activate(sealing_manager, "src_a", "a", "alpha")
    _, binding = source_seal_case.seal(sealing_manager)
    before = source_seal_case.saver.load_context_source_control_states(source_seal_case.owner)

    def refuse_write(*args, **kwargs):
        raise AssertionError("receipt 确认不得调用 source control writer")

    monkeypatch.setattr(source_seal_case.saver, "save_context_source_control_state", refuse_write)
    receipt = manager.acknowledge_sealed_pending(batch, binding)
    assert receipt.deltas == batch.deltas
    assert source_seal_case.saver.load_context_source_control_states(source_seal_case.owner) == before
    assert mutation_port.intents == []
