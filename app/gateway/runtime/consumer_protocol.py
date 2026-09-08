from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

RuntimeAction = Callable[[], Awaitable[None] | None]
RuntimeFenceCheck = Callable[[], None]


def runtime_fencing_token_digest(fencing_token: str | None) -> str | None:
    """生成运行时协议使用的 fencing token 摘要，不在 proof 中保存原 token。"""

    if fencing_token is None:
        return None
    if not fencing_token:
        raise ValueError("Gateway runtime fencing token 不能为空")
    return sha256(fencing_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GatewayRuntimeHealthProof:
    """单个 Gateway 运行时消费者对当前 generation 的健康证明。"""

    consumer_id: str
    generation: str
    state: Literal["healthy"]
    details: dict[str, object]
    fencing_token_digest: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayRuntimeConsumerStage:
    """一个消费者的一次 prepare/apply/health/promotion/rollback 协议。"""

    consumer_id: str
    generation: str
    prepare: RuntimeAction
    apply: RuntimeAction
    health: Callable[[], GatewayRuntimeHealthProof]
    promote: RuntimeAction
    rollback: RuntimeAction
    fencing_token_digest: str | None = None
    fence_check: RuntimeFenceCheck | None = None


@dataclass(slots=True)
class GatewayRuntimeConsumerApplyResult:
    """外部消费者 apply 成功后的暂存结果。"""

    stages: tuple[GatewayRuntimeConsumerStage, ...]
    proofs: tuple[GatewayRuntimeHealthProof, ...]

    async def promote(self) -> None:
        """执行消费者本地 promotion hook；SQLite promotion 由调用方负责。"""

        for stage in self.stages:
            GatewayRuntimeConsumerTransaction._assert_stage_fence(stage)
            await _run_action(stage.promote)

    async def rollback(self) -> None:
        """按逆序补偿所有已经 apply 的消费者。"""

        errors: list[str] = []
        for stage in reversed(self.stages):
            try:
                GatewayRuntimeConsumerTransaction._assert_stage_fence(stage)
                await _run_action(stage.rollback)
            except Exception as error:  # noqa: BLE001 - 必须汇总全部补偿失败
                errors.append(f"{stage.consumer_id}: {error}")
        if errors:
            raise RuntimeError(
                "Gateway runtime consumer 补偿不完整: " + "; ".join(errors)
            )


class GatewayRuntimeConsumerTransaction:
    """协调一组消费者，保证 apply 失败不会留下部分热更新。"""

    def __init__(self, stages: Sequence[GatewayRuntimeConsumerStage]) -> None:
        self._stages = tuple(stages)
        consumer_ids = [stage.consumer_id for stage in self._stages]
        if len(set(consumer_ids)) != len(consumer_ids):
            raise ValueError("Gateway runtime consumer_id 不能重复")
        generations = {stage.generation for stage in self._stages}
        if len(generations) > 1:
            raise ValueError("Gateway runtime consumer 必须绑定同一 generation")
        fencing_tokens = {stage.fencing_token_digest for stage in self._stages}
        if len(fencing_tokens) > 1:
            raise ValueError("Gateway runtime consumer 必须绑定同一 fencing token")

    async def apply(self) -> GatewayRuntimeConsumerApplyResult:
        prepared: list[GatewayRuntimeConsumerStage] = []
        applied: list[GatewayRuntimeConsumerStage] = []
        try:
            for stage in self._stages:
                self._assert_stage_fence(stage)
                await _run_action(stage.prepare)
                prepared.append(stage)
            for stage in prepared:
                # apply 可能已经产生部分外部副作用后才抛错；必须先登记阶段，
                # 让该阶段自己的 rollback 也参与补偿。rollback 需要幂等。
                self._assert_stage_fence(stage)
                applied.append(stage)
                await _run_action(stage.apply)
            proofs = tuple(
                self._validated_health_proof(stage)
                for stage in applied
            )
        except BaseException as error:
            # prepare 允许建立尚未暴露给外部请求的候选资源；即使后续
            # prepare 或 apply 尚未开始，也必须调用该阶段的幂等回退释放它。
            await self._rollback_prepared(prepared, error)
            raise
        return GatewayRuntimeConsumerApplyResult(
            stages=tuple(applied),
            proofs=proofs,
        )

    @staticmethod
    def _validated_health_proof(
        stage: GatewayRuntimeConsumerStage,
    ) -> GatewayRuntimeHealthProof:
        GatewayRuntimeConsumerTransaction._assert_stage_fence(stage)
        proof = stage.health()
        if proof.consumer_id != stage.consumer_id:
            raise RuntimeError(
                "Gateway runtime consumer health proof consumer_id 不匹配: "
                f"expected={stage.consumer_id}, actual={proof.consumer_id}"
            )
        if proof.generation != stage.generation:
            raise RuntimeError(
                "Gateway runtime consumer health proof generation 不匹配: "
                f"consumer_id={stage.consumer_id}, expected={stage.generation}, "
                f"actual={proof.generation}"
            )
        if proof.state != "healthy":
            raise RuntimeError(
                "Gateway runtime consumer health proof 不是 healthy: "
                f"consumer_id={stage.consumer_id}, state={proof.state}"
            )
        if proof.fencing_token_digest != stage.fencing_token_digest:
            raise RuntimeError(
                "Gateway runtime consumer health proof fencing token 不匹配: "
                f"consumer_id={stage.consumer_id}"
            )
        return proof

    @staticmethod
    def _assert_stage_fence(stage: GatewayRuntimeConsumerStage) -> None:
        if stage.fence_check is not None:
            stage.fence_check()

    @staticmethod
    async def _rollback_prepared(
        prepared: Sequence[GatewayRuntimeConsumerStage],
        original_error: BaseException,
    ) -> None:
        errors: list[str] = []
        for stage in reversed(prepared):
            try:
                GatewayRuntimeConsumerTransaction._assert_stage_fence(stage)
                await _run_action(stage.rollback)
            except Exception as rollback_error:  # noqa: BLE001
                errors.append(f"{stage.consumer_id}: {rollback_error}")
        if errors:
            raise RuntimeError(
                "Gateway runtime consumer apply 失败且补偿不完整: "
                + "; ".join(errors)
            ) from original_error


async def _run_action(action: RuntimeAction) -> None:
    result = action()
    if inspect.isawaitable(result):
        await result
