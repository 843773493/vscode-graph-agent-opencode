"""2.3-B4 append 分支生产接线：saver.append_items 经 typed intent 的 focused 测试。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.execution_admission_context import (
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import CanonicalItemStatus, TurnScope
from app.domain.itemized.identity.item_projection import (
    ToolResultEvidence,
)
from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    MutationIntentOwner,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentConsumptionRejected,
    MutationIntentDuplicateConsumption,
    MutationIntentOwnerMismatch,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION_ID = "ses_019be3a6514379e7a28a7ad83396e698"


@pytest.fixture
def saver(tmp_path: Path, session_bundle_factory) -> Iterator[RolloutCheckpointSaver]:
    session_bundle_factory(tmp_path, SESSION_ID)
    with RolloutCheckpointSaver(sessions_dir=tmp_path) as owner:
        thread = owner._storage.storage.main_thread_id(SESSION_ID)
        accepted = owner.accept_turn(
            SESSION_ID, thread_id=thread, accepted_ingress_id="append-ingress",
            acceptance_idempotency_key="append-accept", job_id="append-job",
            admission_idempotency_key="append-admission", payload="追加合同",
            turn_id="turn-1", root_item_id="item-ingress-1",
        )
        admission = owner.execution_admission(
            SESSION_ID, thread_id=thread, execution_id=accepted["initial_execution_id"],
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            yield owner


def _tool_call_item(item_id: str) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=1,
        item_id=item_id,
        semantic_kind="tool_call",
        wire_role="assistant",
        payload_kind="tool_call",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "model-call-1"},
        payload={
            "tool_call_id": "model-call-1:tool-call:call-1",
            "name": "get_goal",
            "args": {"path": "goal.json"},
        },
        turn_id="turn-1",
        turn_scope=TurnScope.TURN_MEMBER,
    )


def _tool_result_item(item_id: str) -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=2,
        item_id=item_id,
        semantic_kind="tool_result",
        wire_role="tool",
        payload_kind="tool_result",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "tool", "producer_id": "tool-exec-1"},
        payload={
            "tool_call_id": "model-call-1:tool-call:call-1",
            "result_id": "result-1",
            "name": "get_goal",
            "content": "{}",
            "tool_outcome": "success"
        , "execution_confirmed": True},
        extensions={},
        turn_id="turn-1",
        turn_scope=TurnScope.TURN_MEMBER,
    )


def test_append_items_consumes_intents_and_commits_batch(
    saver: RolloutCheckpointSaver,
) -> None:
    call = _tool_call_item("item-call-1")
    result = _tool_result_item("item-result-1")
    commit_ids = saver.append_items(SESSION_ID, (call, result), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    # 整批落在同一个 item-bearing commit，保持 storage 批提交语义。
    assert len(commit_ids) == 1
    assert isinstance(commit_ids[0], int)
    stored = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    assert [item.item_id for item in stored] == ["item-ingress-1", "item-call-1", "item-result-1"]


def test_consume_mutation_intent_rejects_owner_without_active_batch(
    saver: RolloutCheckpointSaver,
) -> None:
    before = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    other = MutationIntentOwner(session_id=SESSION_ID, thread_id="thr_01a100c4a1897a2d8d40b568d23fcbe6")
    intent = AppendCanonicalItemIntent(
        owner=other,
        item_id="item-x",
        item_kind="assistant_message",
        execution_id=require_current_execution_admission().execution_id,
        origin_turn_id="turn-1",
    )
    with pytest.raises(MutationIntentOwnerMismatch) as exc_info:
        saver.consume_mutation_intent(intent)
    assert exc_info.value.code == "mutation-intent-owner-mismatch"
    assert saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)) == before


def test_append_items_duplicate_replay_fails_loudly(
    saver: RolloutCheckpointSaver,
) -> None:
    call = _tool_call_item("item-call-1")
    saver.append_items(SESSION_ID, (call,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    with pytest.raises(MutationIntentDuplicateConsumption) as exc_info:
        saver.append_items(SESSION_ID, (call,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert exc_info.value.code == "mutation-intent-duplicate"
    assert exc_info.value.idempotency_key == (
        f"append:{SESSION_ID}:{saver._storage.storage.main_thread_id(SESSION_ID)}:item-call-1"
    )
    stored = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    assert [item.item_id for item in stored] == ["item-ingress-1", "item-call-1"]


def test_append_items_port_failure_keeps_zero_half_state_and_retry(
    saver: RolloutCheckpointSaver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    call = _tool_call_item("item-call-1")

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("owner 事务失败")

    monkeypatch.setattr(saver._storage.executions, "append_items", _fail)
    with pytest.raises(MutationIntentConsumptionRejected) as exc_info:
        saver.append_items(SESSION_ID, (call,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert exc_info.value.code == "mutation-intent-rejected"
    assert exc_info.value.failure_outcome == "reject_atomic"
    monkeypatch.undo()
    # 失败零半状态：存储未被触碰。
    assert saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)) == before
    commit_ids = saver.append_items(SESSION_ID, (call,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert len(commit_ids) == 1
    stored = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    assert [item.item_id for item in stored] == ["item-ingress-1", "item-call-1"]


def test_append_items_rejects_kind_outside_intent_closure(
    saver: RolloutCheckpointSaver,
) -> None:
    before = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    notice = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-notice-1",
        semantic_kind="runtime_notice",
        payload_kind="text",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "runtime", "producer_id": "runtime-1"},
        payload="ambient notice",
        turn_scope=TurnScope.AMBIENT,
    )
    with pytest.raises(ValueError, match="append intent 闭合集"):
        _append_intent_for(
            MutationIntentOwner(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)),
            notice, execution_id=require_current_execution_admission().execution_id,
        )
    with pytest.raises(RuntimeError, match="Turn attribution 与 execution admission 不一致"):
        saver.append_items(SESSION_ID, (notice,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)) == before


def test_append_items_supports_shadow_tool_call_single_call_list(
    saver: RolloutCheckpointSaver,
) -> None:
    shadow = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-shadow-call-1",
        semantic_kind="tool_call",
        wire_role="assistant",
        payload_kind="tool_call",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "lc_run-1"},
        payload={"tool_calls": [{"id": "call-1", "name": "get_goal", "args": {}}]},
        turn_id="turn-1",
        turn_scope=TurnScope.TURN_MEMBER,
    )
    commit_ids = saver.append_items(SESSION_ID, (shadow,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert len(commit_ids) == 1
    stored = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    assert [item.item_id for item in stored] == ["item-ingress-1", "item-shadow-call-1"]


def test_append_items_rejects_multi_call_tool_call(
    saver: RolloutCheckpointSaver,
) -> None:
    before = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    call = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-multi-call-1",
        semantic_kind="tool_call",
        wire_role="assistant",
        payload_kind="tool_call",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "model-call-1"},
        payload={
            "tool_calls": [
                {"id": "call-1", "name": "get_goal", "args": {}},
                {"id": "call-2", "name": "get_goal", "args": {}},
            ]
        },
        turn_id="turn-1",
        turn_scope=TurnScope.TURN_MEMBER,
    )
    with pytest.raises(ValueError, match="多 call 批式 tool_call"):
        saver.append_items(SESSION_ID, (call,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)) == before


def test_append_items_rejects_item_without_origin_turn(
    saver: RolloutCheckpointSaver,
) -> None:
    before = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    output = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-ambient-1",
        semantic_kind="assistant_output",
        wire_role="assistant",
        payload_kind="text",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "provider", "producer_id": "model-call-1"},
        payload="ambient 输出",
    )
    with pytest.raises(RuntimeError, match="Turn attribution 与 execution admission 不一致"):
        saver.append_items(SESSION_ID, (output,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)) == before


def test_append_items_supports_text_tool_result_identity_from_metadata(
    saver: RolloutCheckpointSaver,
) -> None:
    result = CanonicalItemRecord.create(
        item_sequence=1,
        item_id="item-text-result-1",
        semantic_kind="tool_result",
        wire_role="tool",
        payload_kind="text",
        status=CanonicalItemStatus.COMPLETED,
        producer_ref={"producer_kind": "tool", "producer_id": "tool-exec-1"},
        payload="{}",
        tool_result_evidence=ToolResultEvidence(tool_call_id="model-call-1:tool-call:call-1", result_id="result-1", execution_confirmed=True),
        turn_id="turn-1",
        turn_scope=TurnScope.TURN_MEMBER,
    )
    commit_ids = saver.append_items(SESSION_ID, (result,), thread_id=saver._storage.storage.main_thread_id(SESSION_ID))
    assert len(commit_ids) == 1
    stored = saver.read_canonical_items(SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID))
    assert [item.item_id for item in stored] == ["item-ingress-1", "item-text-result-1"]

from app.services.infrastructure.rollout_context.checkpoint.mutation.saver_mutation_intents import (
    _append_intent_for,
)
