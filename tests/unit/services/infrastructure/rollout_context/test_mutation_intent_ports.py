"""B4 第一阶段：SessionThread mutation intent 端口的 typed 合同测试。"""

from __future__ import annotations

import pytest

from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    ApplySourceLifecycleDecision,
    ContextMutationIntent,
    MutationIntentOwner,
    RebuildContextEpochIntent,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentConsumptionReceipt,
    MutationIntentConsumptionRejected,
    MutationIntentDuplicateConsumption,
    MutationIntentOwnerMismatch,
    MutationIntentPortError,
    SessionThreadMutationIntents,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.saver_mutation_intents import (
    RolloutMutationIntentPortMixin,
)

OWNER = MutationIntentOwner(session_id="session-1", thread_id="MAIN")


class _RecordingPort:
    """记录 consume 调用的最小端口实现；失败脚本由测试注入。"""

    def __init__(self) -> None:
        self.consumed: list[ContextMutationIntent] = []
        self.error: Exception | None = None

    def consume_mutation_intent(self, intent: ContextMutationIntent) -> None:
        self.consumed.append(intent)
        if self.error is not None:
            raise self.error


def _three_branch_intents() -> tuple[ContextMutationIntent, ...]:
    """三类 intent 各一支：覆盖 Append/SourceLifecycle/EpochRebuild。"""
    return (
        AppendCanonicalItemIntent(
            owner=OWNER,
            item_id="item-1",
            item_kind="user_message",
            origin_turn_id="turn-1",
            execution_id="execution-1",
        ),
        ApplySourceLifecycleDecision(
            owner=OWNER,
            source_id="skill:demo",
            source_kind="skill",
            name="demo",
            decision_kind="base",
            revision="rev-1",
            content="base",
            item_id="item-src-1",
        ),
        RebuildContextEpochIntent(
            owner=OWNER,
            epoch_reason="rewind",
            view_revision=1,
            control_revision=1,
            checkpoint_ns="", source_checkpoint_id="checkpoint-1", target_view_id="view-1",
        ),
    )


@pytest.mark.parametrize(
    "intent",
    _three_branch_intents()[:-1],
    ids=[type(intent).__name__ for intent in _three_branch_intents()[:-1]],
)
def test_receipt_matches_domain_contract(intent: ContextMutationIntent) -> None:
    port = _RecordingPort()
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    receipt = facade.consume(intent)
    assert isinstance(receipt, MutationIntentConsumptionReceipt)
    assert receipt.intent_kind != ""
    assert receipt.idempotency_key == intent.idempotency_key
    assert receipt.failure_outcome == intent.failure_outcome
    assert port.consumed == [intent]
    assert facade.consumed_keys == (intent.idempotency_key,)


@pytest.fixture
def saver_epoch_port():
    return RolloutMutationIntentPortMixin()


def test_epoch_rebuild_requires_real_boundary_connection(saver_epoch_port) -> None:
    intent = _three_branch_intents()[-1]
    with pytest.raises(MutationIntentPortError, match="epoch-rebuild-transaction-required"):
        saver_epoch_port.consume_mutation_intent(intent)


def test_epoch_rebuild_never_enters_facade_success_cache() -> None:
    port = _RecordingPort()
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    intent = _three_branch_intents()[-1]
    for _attempt in range(2):
        with pytest.raises(MutationIntentPortError, match="epoch-rebuild-transaction-required"):
            facade.consume(intent)
        assert port.consumed == []
        assert facade.consumed_keys == ()


def test_wrong_owner_rejected_without_port_call() -> None:
    port = _RecordingPort()
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    other = MutationIntentOwner(session_id="session-1", thread_id="child-1")
    intent = AppendCanonicalItemIntent(
        owner=other,
        item_id="item-x",
        item_kind="user_message",
        origin_turn_id="turn-1",
        execution_id="execution-1",
    )
    with pytest.raises(MutationIntentOwnerMismatch) as exc_info:
        facade.consume(intent)
    assert exc_info.value.code == "mutation-intent-owner-mismatch"
    assert port.consumed == []
    assert facade.consumed_keys == ()


def test_duplicate_intent_rejected_and_port_called_once() -> None:
    port = _RecordingPort()
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    intent = _three_branch_intents()[0]
    facade.consume(intent)
    duplicate = AppendCanonicalItemIntent(
        owner=OWNER,
        item_id="item-1",
        item_kind="user_message",
        origin_turn_id="turn-1",
        execution_id="execution-1",
    )
    with pytest.raises(MutationIntentDuplicateConsumption) as exc_info:
        facade.consume(duplicate)
    assert exc_info.value.code == "mutation-intent-duplicate"
    assert exc_info.value.idempotency_key == intent.idempotency_key
    assert len(port.consumed) == 1
    assert facade.consumed_keys == (intent.idempotency_key,)


def test_port_failure_exposes_outcome_and_keeps_retryable() -> None:
    port = _RecordingPort()
    port.error = RuntimeError("owner transaction 失败")
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    intent = _three_branch_intents()[1]
    with pytest.raises(MutationIntentConsumptionRejected) as exc_info:
        facade.consume(intent)
    assert exc_info.value.code == "mutation-intent-rejected"
    assert exc_info.value.idempotency_key == intent.idempotency_key
    assert exc_info.value.failure_outcome == "keep_pending"
    # 失败不记录幂等键：同一 intent 按原 identity 重试，不产生半状态。
    assert facade.consumed_keys == ()
    port.error = None
    receipt = facade.consume(intent)
    assert receipt.failure_outcome == "keep_pending"
    assert len(port.consumed) == 2


def test_unknown_intent_branch_rejected() -> None:
    port = _RecordingPort()
    facade = SessionThreadMutationIntents(owner=OWNER, port=port)
    with pytest.raises(TypeError):
        facade.consume("not-an-intent")  # type: ignore[arg-type]
    assert port.consumed == []
    assert facade.consumed_keys == ()
