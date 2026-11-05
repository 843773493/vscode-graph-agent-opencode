"""Gateway 单元测试通过 fixture 显式装配完整 Backend 合同。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.core.logging_contract import get_runtime_log_root, load_runtime_log_contract
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
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


GATEWAY_ID = "gateway_00000000000040008000000000000001"
WORKSPACE_A = "00000000-0000-4000-8000-000000000001"
WORKSPACE_B = "00000000-0000-4000-8000-000000000002"
WORKSPACE_SHARED = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def diagnostic_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GatewayWorkspaceRegistry]:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    launcher = tmp_path / "explicit-launcher.log"
    launcher.write_text("launcher sharedword\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_SERVICE_LOG_PATH", str(launcher))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=GATEWAY_ID)
    for workspace_id, name, port in ((WORKSPACE_A, "A", 41001), (WORKSPACE_B, "B", 41002), (WORKSPACE_SHARED, "C", 41001)):
        registry.upsert(WorkspaceTarget(
            gateway_id=GATEWAY_ID, workspace_id=workspace_id, name=name, root_path=str(tmp_path / name),
            backend_url=f"http://127.0.0.1:{port}", connection_kind="local", managed=True,
        ), activate=False)
    contract, root = load_runtime_log_contract(), get_runtime_log_root()
    for role, port, text in (
        ("backend", 41001, "only-a sharedword 😀needle needle\n"),
        ("backend", 41002, "only-b sharedword\n"),
        ("gateway", None, "only-g sharedword\n"),
    ):
        path = contract.path_for(root, role, port)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    yield registry
    registry.close()
