import asyncio
from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController


def _controller(tmp_path: Path) -> GatewayWorkspaceRuntimeController:
    return GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json"),
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )


def test_runtime_lock_table_stays_bounded_across_many_workspace_ids(
    tmp_path: Path,
) -> None:
    """长驻单例的锁表不得随历史/探针 workspace_id 无界增长。"""

    controller = _controller(tmp_path)
    for index in range(5000):
        controller._lock(f"gw_probe_{index}")
    assert len(controller._locks) <= 64


@pytest.mark.asyncio
async def test_runtime_lock_is_stable_and_serializes_same_workspace(
    tmp_path: Path,
) -> None:
    """同一 workspace_id 恒得同一把锁，且该锁对同工作区仍是真互斥。"""

    controller = _controller(tmp_path)
    assert controller._lock("gw_x") is controller._lock("gw_x")
    lock = controller._lock("gw_x")
    peak = 0
    active = 0

    async def worker() -> None:
        nonlocal peak, active
        async with lock:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
