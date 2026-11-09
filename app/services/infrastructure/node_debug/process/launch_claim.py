"""Node Debug owner 唯一冻结 run 记录的状态迁移。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugBindingSnapshotDTO,
    NodeDebugBreakpointDTO,
    NodeDebugLaunchClaimDTO,
)

ACTIVE_CLAIM_PHASES: frozenset[str] = frozenset({"launch_pending", "running", "stopping"})
UNSETTLED_CLAIM_PHASES: frozenset[str] = ACTIVE_CLAIM_PHASES | {"execution_lost"}


@dataclass(frozen=True, slots=True)
class NodeDebugClaimRecoveryDecision:
    outcome: Literal["settled", "running", "execution_lost"]
    reason: str


def new_launch_claim(
    *,
    session_id: str,
    thread_id: str,
    run_id: str,
    configuration_id: str,
    debugger_binding: NodeDebugBindingSnapshotDTO,
    script_path: str,
    args: list[str],
    node_executable: str,
    working_directory: str,
    launch_profile_name: str | None,
    breakpoints: list[NodeDebugBreakpointDTO],
) -> NodeDebugLaunchClaimDTO:
    now = datetime.now(UTC)
    return NodeDebugLaunchClaimDTO(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        process_instance_id=create_prefixed_id("node-debug-proc"),
        phase="launch_pending",
        configuration_id=configuration_id,
        debugger_binding=debugger_binding,
        script_path=script_path,
        args=list(args),
        node_executable=node_executable,
        working_directory=working_directory,
        launch_profile_name=launch_profile_name,
        breakpoints=[item.model_copy(deep=True) for item in breakpoints],
        created_at=now,
        updated_at=now,
    )


def claim_with_target_pid(claim: NodeDebugLaunchClaimDTO, *, pid: int) -> NodeDebugLaunchClaimDTO:
    if claim.phase != "launch_pending":
        raise RuntimeError(f"只有 launch_pending claim 可以登记目标 PID: phase={claim.phase}")
    return claim.model_copy(update={"pid": pid, "updated_at": datetime.now(UTC)})


def claim_running(claim: NodeDebugLaunchClaimDTO) -> NodeDebugLaunchClaimDTO:
    if claim.phase != "launch_pending" or claim.pid is None:
        raise RuntimeError("Debug run 进入 running 前必须收到插件进程身份回执")
    return claim.model_copy(update={"phase": "running", "reconcile_reason": None,
                                    "updated_at": datetime.now(UTC)})


def claim_marked(
    claim: NodeDebugLaunchClaimDTO,
    *,
    phase: Literal["stopping", "execution_lost", "settled"],
    reason: str | None = None,
) -> NodeDebugLaunchClaimDTO:
    if phase == "execution_lost" and not reason:
        raise ValueError("execution_lost 必须记录插件运行事实查询结果")
    return claim.model_copy(update={
        "phase": phase,
        "reconcile_reason": reason if phase == "execution_lost" else None,
        "updated_at": datetime.now(UTC),
    })


__all__ = [
    "ACTIVE_CLAIM_PHASES",
    "UNSETTLED_CLAIM_PHASES",
    "NodeDebugClaimRecoveryDecision",
    "claim_marked",
    "claim_running",
    "claim_with_target_pid",
    "new_launch_claim",
]
