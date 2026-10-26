"""Gateway 单元测试通过 fixture 显式装配完整 Backend 合同。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import pytest

from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerApplyResult,
    GatewayRuntimeConsumerStage,
    GatewayRuntimeHealthProof,
)

if TYPE_CHECKING:
    from tests.support.gateway_managed_backends import ManagedBackendGraph


@pytest.fixture
def managed_backend_factory() -> Callable[..., ManagedBackendGraph]:
    from tests.support.gateway_managed_backends import backend_graph

    return backend_graph


@pytest.fixture
def runtime_apply_result_factory():
    def create(rollback: Callable[[], Awaitable[None]]) -> GatewayRuntimeConsumerApplyResult:
        proof = GatewayRuntimeHealthProof("fixture-consumer", "fixture-generation", "healthy", {})
        return GatewayRuntimeConsumerApplyResult(
            stages=(GatewayRuntimeConsumerStage(
                consumer_id=proof.consumer_id, generation=proof.generation,
                prepare=lambda: None, apply=lambda: None, health=lambda: proof,
                promote=lambda: None, rollback=rollback,
            ),),
            proofs=(proof,),
        )
    return create
