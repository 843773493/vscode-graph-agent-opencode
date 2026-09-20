"""B4 第二阶段最小合同：commit_model_call_pending 的原子提交行为。"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    CommittedContextSourceBatch,
    ContextSourceDescriptor,
    ContextSourceManager,
)

OWNER = ContextSourceOwnerKey(session_id="ses_b4csm", thread_id="main")


class _FakeControlStatePort:
    """实现现有 ContextSourceControlStatePort 协议的内存桩（CAS 语义）。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str, str], ContextSourceControlState] = {}
        self.save_count = 0
        self.fail_on_save_number: int | None = None

    def load_context_source_control_states(
        self, owner: ContextSourceOwnerKey
    ) -> tuple[ContextSourceControlState, ...]:
        return tuple(
            state
            for key, state in self._states.items()
            if key[:2] == (owner.session_id, owner.thread_id)
        )

    def save_context_source_control_state(
        self, snapshot: ContextSourceControlState
    ) -> ContextSourceControlState:
        self.save_count += 1
        if (
            self.fail_on_save_number is not None
            and self.save_count >= self.fail_on_save_number
        ):
            raise RuntimeError("control state storage unavailable")
        key = (
            snapshot.owner.session_id,
            snapshot.owner.thread_id,
            snapshot.source_id,
        )
        existing = self._states.get(key)
        if existing is not None and existing.durable_fields() == snapshot.durable_fields():
            return existing
        if existing is not None and snapshot.state_revision != existing.state_revision:
            raise RuntimeError("测试端口拒绝过期控制状态")
        stored = dataclasses.replace(
            snapshot,
            state_revision=1 if existing is None else existing.state_revision + 1,
        )
        self._states[key] = stored
        return stored


class _RecordingMutationIntentPort:
    """记录 CSM 提交的 source lifecycle intent。"""

    def __init__(self) -> None:
        self.intents: list[object] = []

    def consume_mutation_intent(self, intent: object) -> None:
        self.intents.append(intent)


def _descriptor(source_id: str, name: str) -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id=source_id,
        source_kind="skill",
        name=name,
        description=name + " 工作流",
        internal_locator="/.boxteam/skills/" + name + "/SKILL.md",
    )


def _activate(
    manager: ContextSourceManager,
    source_id: str,
    name: str,
    content: str,
) -> None:
    manager.register(_descriptor(source_id, name))
    manager.activate_skill_content(name, content)


def test_prepare_pending_is_readonly_and_commit_is_idempotent() -> None:
    """prepare 不推进 applied；重复提交同一批次复用同一结果且不再发事件。"""
    events: list[ContextSourceEvent] = []
    manager = ContextSourceManager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None
    again = manager.prepare_pending()
    assert again is not None and again.deltas == batch.deltas

    receipt = manager.commit_model_call_pending(batch)
    assert isinstance(receipt, CommittedContextSourceBatch)
    assert receipt.deltas == batch.deltas
    assert [event.kind for event in events] == ["committed"]
    assert manager.prepare_pending() is None

    replay = manager.commit_model_call_pending(batch)
    assert replay == receipt
    assert [event.kind for event in events] == ["committed"]


def test_commit_persists_before_memory_advance_and_events() -> None:
    """durable 持久化成功后才推进内存 applied 并发布事件；失败零内存变化。"""
    port = _FakeControlStatePort()
    events: list[ContextSourceEvent] = []
    manager = ContextSourceManager(
        owner=OWNER,
        control_state_port=port,
        lifecycle_event_sink=events.append,
    )
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None

    port.fail_on_save_number = port.save_count + 1
    with pytest.raises(RuntimeError, match="control state storage unavailable"):
        manager.commit_model_call_pending(batch)
    assert events == []
    (stored,) = port.load_context_source_control_states(OWNER)
    assert stored.latest_visible_committed_revision is None

    port.fail_on_save_number = None
    receipt = manager.commit_model_call_pending(batch)
    assert receipt.deltas == batch.deltas
    assert [event.kind for event in events] == ["committed"]
    (stored,) = port.load_context_source_control_states(OWNER)
    assert stored.latest_visible_committed_revision == stored.latest_revision


def test_activation_delta_maps_to_base_source_lifecycle_intent() -> None:
    """首次 activation 保留内存 delta 合同，但 intent 使用合法 base 语义。"""
    mutation_port = _RecordingMutationIntentPort()
    manager = ContextSourceManager(
        owner=OWNER,
        control_state_port=_FakeControlStatePort(),
        mutation_intent_port=mutation_port,
    )
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None
    assert batch.deltas[0].kind == "activation"

    manager.commit_model_call_pending(batch)

    assert len(mutation_port.intents) == 1
    intent = mutation_port.intents[0]
    assert isinstance(intent, ApplySourceLifecycleDecision)
    assert intent.decision_kind == "base"
    assert intent.from_revision is None


def test_commit_rejects_pending_drift_fail_closed() -> None:
    """提交前 pending 变化时 fail closed：零持久化推进、零事件、可重试。"""
    port = _FakeControlStatePort()
    events: list[ContextSourceEvent] = []
    manager = ContextSourceManager(
        owner=OWNER,
        control_state_port=port,
        lifecycle_event_sink=events.append,
    )
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None

    _activate(manager, "src_b", "b", "beta")
    with pytest.raises(RuntimeError, match="提交前发生变化"):
        manager.commit_model_call_pending(batch)
    assert events == []
    stored = {
        state.source_id: state
        for state in port.load_context_source_control_states(OWNER)
    }
    assert stored["src_a"].latest_visible_committed_revision is None
    assert stored["src_b"].latest_visible_committed_revision is None

    fresh = manager.prepare_pending()
    assert fresh is not None and len(fresh.deltas) == 2
    manager.commit_model_call_pending(fresh)
    assert [event.kind for event in events] == ["committed", "committed"]


def test_commit_rejects_drifted_batch_fail_closed() -> None:
    """batch 漂移（revision 被篡改）时 fail closed：零事件、pending 保持可重试。"""
    events: list[ContextSourceEvent] = []
    manager = ContextSourceManager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None
    drifted = dataclasses.replace(batch.deltas[0], revision="sha256:drifted")
    with pytest.raises(RuntimeError):
        manager.commit_model_call_pending(
            type(batch)(deltas=(drifted,))
        )
    assert events == []
    # 内存真值未被推进：原始批次仍可原样重试提交。
    receipt = manager.commit_model_call_pending(batch)
    assert receipt.deltas == batch.deltas
    assert [event.kind for event in events] == ["committed"]


def test_commit_pure_memory_receipt_reuse() -> None:
    """纯内存 CSM：提交后 receipt 复用且 pending 清空。"""
    events: list[ContextSourceEvent] = []
    manager = ContextSourceManager(lifecycle_event_sink=events.append)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None
    first = manager.commit_model_call_pending(batch)
    second = manager.commit_model_call_pending(batch)
    assert second == first
    assert len(events) == 1


def test_commit_reuses_existing_port_contract() -> None:
    """复用现有 durable 端口：提交走既有 save 协议且 CAS 防过期写入。"""
    port = _FakeControlStatePort()
    manager = ContextSourceManager(owner=OWNER, control_state_port=port)
    _activate(manager, "src_a", "a", "alpha")
    batch = manager.prepare_pending()
    assert batch is not None
    saves_before = port.save_count
    manager.commit_model_call_pending(batch)
    assert port.save_count == saves_before + 1
    (stored,) = port.load_context_source_control_states(OWNER)
    assert stored.latest_visible_committed_revision == stored.latest_revision
