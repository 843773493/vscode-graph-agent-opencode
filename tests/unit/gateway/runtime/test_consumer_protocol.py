from __future__ import annotations

import pytest

from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeConsumerTransaction,
    GatewayRuntimeHealthProof,
    runtime_fencing_token_digest,
)


def _proof(consumer_id: str, generation: str) -> GatewayRuntimeHealthProof:
    return GatewayRuntimeHealthProof(
        consumer_id=consumer_id,
        generation=generation,
        state="healthy",
        details={},
    )


@pytest.mark.asyncio
async def test_consumer_transaction_runs_protocol_phases_and_promotes() -> None:
    calls: list[str] = []
    stages = tuple(
        GatewayRuntimeConsumerStage(
            consumer_id=consumer_id,
            generation="generation-1",
            prepare=lambda consumer_id=consumer_id: calls.append(
                f"prepare:{consumer_id}"
            ),
            apply=lambda consumer_id=consumer_id: calls.append(
                f"apply:{consumer_id}"
            ),
            health=lambda consumer_id=consumer_id: (
                calls.append(f"health:{consumer_id}")
                or _proof(consumer_id, "generation-1")
            ),
            promote=lambda consumer_id=consumer_id: calls.append(
                f"promote:{consumer_id}"
            ),
            rollback=lambda consumer_id=consumer_id: calls.append(
                f"rollback:{consumer_id}"
            ),
        )
        for consumer_id in ("catalog", "scheduler")
    )

    result = await GatewayRuntimeConsumerTransaction(stages).apply()
    assert [proof.consumer_id for proof in result.proofs] == [
        "catalog",
        "scheduler",
    ]
    await result.promote()

    assert calls == [
        "prepare:catalog",
        "prepare:scheduler",
        "apply:catalog",
        "apply:scheduler",
        "health:catalog",
        "health:scheduler",
        "promote:catalog",
        "promote:scheduler",
    ]


@pytest.mark.asyncio
async def test_consumer_transaction_rolls_back_applied_stages_on_failure() -> None:
    calls: list[str] = []

    def fail_apply() -> None:
        calls.append("apply:failed")
        raise RuntimeError("consumer apply failed")

    stages = (
        GatewayRuntimeConsumerStage(
            consumer_id="first",
            generation="generation-1",
            prepare=lambda: calls.append("prepare:first"),
            apply=lambda: calls.append("apply:first"),
            health=lambda: _proof("first", "generation-1"),
            promote=lambda: calls.append("promote:first"),
            rollback=lambda: calls.append("rollback:first"),
        ),
        GatewayRuntimeConsumerStage(
            consumer_id="failed",
            generation="generation-1",
            prepare=lambda: calls.append("prepare:failed"),
            apply=fail_apply,
            health=lambda: _proof("failed", "generation-1"),
            promote=lambda: calls.append("promote:failed"),
            rollback=lambda: calls.append("rollback:failed"),
        ),
    )

    with pytest.raises(RuntimeError, match="consumer apply failed"):
        await GatewayRuntimeConsumerTransaction(stages).apply()

    assert calls == [
        "prepare:first",
        "prepare:failed",
        "apply:first",
        "apply:failed",
        "rollback:failed",
        "rollback:first",
    ]


@pytest.mark.asyncio
async def test_consumer_transaction_rolls_back_all_applied_stages_on_health_failure() -> None:
    calls: list[str] = []

    def fail_health() -> GatewayRuntimeHealthProof:
        calls.append("health:failed")
        raise RuntimeError("consumer health failed")

    stages = (
        GatewayRuntimeConsumerStage(
            consumer_id="healthy",
            generation="generation-1",
            prepare=lambda: calls.append("prepare:healthy"),
            apply=lambda: calls.append("apply:healthy"),
            health=lambda: (
                calls.append("health:healthy")
                or _proof("healthy", "generation-1")
            ),
            promote=lambda: calls.append("promote:healthy"),
            rollback=lambda: calls.append("rollback:healthy"),
        ),
        GatewayRuntimeConsumerStage(
            consumer_id="failed",
            generation="generation-1",
            prepare=lambda: calls.append("prepare:failed"),
            apply=lambda: calls.append("apply:failed"),
            health=fail_health,
            promote=lambda: calls.append("promote:failed"),
            rollback=lambda: calls.append("rollback:failed"),
        ),
    )

    with pytest.raises(RuntimeError, match="consumer health failed"):
        await GatewayRuntimeConsumerTransaction(stages).apply()

    assert calls == [
        "prepare:healthy",
        "prepare:failed",
        "apply:healthy",
        "apply:failed",
        "health:healthy",
        "health:failed",
        "rollback:failed",
        "rollback:healthy",
    ]


@pytest.mark.asyncio
async def test_consumer_transaction_rejects_health_proof_for_another_generation() -> None:
    calls: list[str] = []
    stage = GatewayRuntimeConsumerStage(
        consumer_id="catalog",
        generation="generation-2",
        prepare=lambda: calls.append("prepare"),
        apply=lambda: calls.append("apply"),
        health=lambda: _proof("catalog", "generation-1"),
        promote=lambda: calls.append("promote"),
        rollback=lambda: calls.append("rollback"),
    )

    with pytest.raises(RuntimeError, match="generation 不匹配"):
        await GatewayRuntimeConsumerTransaction((stage,)).apply()

    assert calls == ["prepare", "apply", "rollback"]


@pytest.mark.asyncio
async def test_consumer_transaction_rejects_health_proof_for_another_fencing_token() -> None:
    token_digest = runtime_fencing_token_digest("fence-1")
    stage = GatewayRuntimeConsumerStage(
        consumer_id="catalog",
        generation="generation-1",
        prepare=lambda: None,
        apply=lambda: None,
        health=lambda: GatewayRuntimeHealthProof(
            consumer_id="catalog",
            generation="generation-1",
            state="healthy",
            details={},
            fencing_token_digest=runtime_fencing_token_digest("fence-2"),
        ),
        promote=lambda: None,
        rollback=lambda: None,
        fencing_token_digest=token_digest,
    )

    with pytest.raises(RuntimeError, match="fencing token 不匹配"):
        await GatewayRuntimeConsumerTransaction((stage,)).apply()


@pytest.mark.asyncio
async def test_consumer_transaction_checks_fence_before_each_mutable_phase() -> None:
    checks: list[str] = []
    stage = GatewayRuntimeConsumerStage(
        consumer_id="catalog",
        generation="generation-1",
        prepare=lambda: checks.append("prepare"),
        apply=lambda: checks.append("apply"),
        health=lambda: (
            checks.append("health")
            or GatewayRuntimeHealthProof(
                consumer_id="catalog",
                generation="generation-1",
                state="healthy",
                details={},
                fencing_token_digest=runtime_fencing_token_digest("fence-1"),
            )
        ),
        promote=lambda: checks.append("promote"),
        rollback=lambda: checks.append("rollback"),
        fencing_token_digest=runtime_fencing_token_digest("fence-1"),
        fence_check=lambda: checks.append("fence"),
    )

    result = await GatewayRuntimeConsumerTransaction((stage,)).apply()
    await result.promote()
    await result.rollback()

    assert checks == [
        "fence",
        "prepare",
        "fence",
        "apply",
        "fence",
        "health",
        "fence",
        "promote",
        "fence",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_consumer_transaction_does_not_rollback_stage_rejected_before_apply() -> None:
    calls: list[str] = []

    def reject_fence() -> None:
        calls.append("fence")
        raise RuntimeError("stale fencing claim")

    stage = GatewayRuntimeConsumerStage(
        consumer_id="catalog",
        generation="generation-1",
        prepare=lambda: calls.append("prepare"),
        apply=lambda: calls.append("apply"),
        health=lambda: _proof("catalog", "generation-1"),
        promote=lambda: None,
        rollback=lambda: calls.append("rollback"),
        fence_check=reject_fence,
    )

    with pytest.raises(RuntimeError, match="stale fencing claim"):
        await GatewayRuntimeConsumerTransaction((stage,)).apply()

    assert calls == ["fence"]
