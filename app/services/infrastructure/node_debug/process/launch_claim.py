"""Node 调试 launch claim 的登记、状态迁移与崩溃恢复决策。

claim 是 thread 节点下 durable 的启动登记（``debug/node/launch-claim.json``），独立于
任何短期工具调用，跨 Turn 保留。它只表达“这个 owner 曾经启动过哪个 process instance”：

- spawn 前登记 ``launch_pending`` + 一次性 nonce；
- spawn 后记录 OS 起始身份（``spawned``），供崩溃后定点恢复；
- 起始身份核对 + Inspector 握手成功后才进入 ``running`` 并登记 PID/端口；
- 停止/退出核实终结后 ``settled``；
- 任何无法核实的情形保持 ``reconcile_required``，绝不虚报终态。
- ``reconcile_required`` 只有在再次核实“登记的那一个实例已终结”（PID 已不存在，或该
  PID 起始身份已变化＝原实例已终结并被回收）时才结清解除；仍存活且身份匹配的实例不会被
  自动接管、也不会在此被自动终止。

本模块只做状态迁移与恢复判定，不直接操作进程；真正的终止动作由服务层在核实身份后执行。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import NodeDebugLaunchClaimDTO
from app.services.infrastructure.node_debug.process.process_identity import (
    NodeDebugProcessIdentity,
    probe_process_identity,
)

#: 仍会阻断新启动/方案切换的 claim 阶段。
ACTIVE_CLAIM_PHASES: frozenset[str] = frozenset(
    {"launch_pending", "spawned", "running", "stopping", "reconcile_required"}
)

ClaimRecoveryOutcome = Literal["settle", "terminate_then_settle", "reconcile_required"]

IdentityProbe = Callable[[int], NodeDebugProcessIdentity | None]


@dataclass(frozen=True, slots=True)
class NodeDebugClaimRecoveryDecision:
    """按 claim + 当前 OS 事实得出的恢复决策。"""

    outcome: ClaimRecoveryOutcome
    reason: str
    identity: NodeDebugProcessIdentity | None = None


def new_launch_claim(
    *,
    session_id: str,
    thread_id: str,
    configuration_id: str,
    inspector_host: str,
    inspector_port: int,
) -> NodeDebugLaunchClaimDTO:
    """spawn 前登记：唯一 process_instance_id + 一次性 nonce。"""
    now = datetime.now(UTC)
    # TODO: nonce 目前只作为“本次启动尝试”的 durable 代际指纹登记，尚无消费方；
    # 跨 Turn 的 typed lease 与 spawn 侧 nonce 回读核验属后续轮次（任务 3.5 完整版）。
    # 当前“是否同一实例”的权威依据是 OS 进程起始身份 + Inspector 握手成功。
    return NodeDebugLaunchClaimDTO(
        session_id=session_id,
        thread_id=thread_id,
        process_instance_id=create_prefixed_id("node-debug-proc"),
        nonce=secrets.token_hex(16),
        phase="launch_pending",
        configuration_id=configuration_id,
        inspector_host=inspector_host,
        inspector_port=inspector_port,
        created_at=now,
        updated_at=now,
    )


def claim_with_spawn_identity(
    claim: NodeDebugLaunchClaimDTO,
    *,
    pid: int,
    identity: NodeDebugProcessIdentity,
) -> NodeDebugLaunchClaimDTO:
    """spawn 后记录 PID 与 OS 起始身份，等待握手才进入 ``running``。"""
    if claim.phase != "launch_pending":
        raise RuntimeError(
            "只有 launch_pending claim 可以登记 spawn 身份: "
            f"process_instance_id={claim.process_instance_id}, phase={claim.phase}"
        )
    return claim.model_copy(
        update={
            "phase": "spawned",
            "pid": pid,
            "process_identity_source": identity.source,
            "process_start_marker": identity.start_marker,
            "updated_at": datetime.now(UTC),
        }
    )


def claim_running(
    claim: NodeDebugLaunchClaimDTO,
    *,
    inspector_port: int,
) -> NodeDebugLaunchClaimDTO:
    """身份核对 + Inspector 握手成功后才把 PID/端口登记为权威运行属性。"""
    if claim.phase != "spawned":
        raise RuntimeError(
            "只有 spawned claim 可以在握手成功后进入 running: "
            f"process_instance_id={claim.process_instance_id}, phase={claim.phase}"
        )
    if claim.pid is None:
        raise RuntimeError(
            "running claim 必须已记录 PID: "
            f"process_instance_id={claim.process_instance_id}"
        )
    return claim.model_copy(
        update={
            "phase": "running",
            "inspector_port": inspector_port,
            "reconcile_reason": None,
            "updated_at": datetime.now(UTC),
        }
    )


def claim_marked(
    claim: NodeDebugLaunchClaimDTO,
    *,
    phase: Literal["stopping", "reconcile_required", "settled"],
    reason: str | None = None,
) -> NodeDebugLaunchClaimDTO:
    """通用阶段迁移；``reconcile_required`` 必须带原因。"""
    if phase == "reconcile_required" and not reason:
        raise ValueError("reconcile_required 必须记录无法核实的原因")
    return claim.model_copy(
        update={
            "phase": phase,
            "reconcile_reason": reason if phase == "reconcile_required" else None,
            "updated_at": datetime.now(UTC),
        }
    )


def _incomparable_reason(
    claim: NodeDebugLaunchClaimDTO,
    identity: NodeDebugProcessIdentity,
) -> str:
    """身份不可比对时的诊断文本：事实不足，既不能认领/停止，也不能判为已终结。"""
    return (
        "PID 当前实例的起始身份与登记不可比对（来源缺失或不同构、或标记缺失），"
        "不能认领、不能停止，也不能判定原实例已终结: "
        f"pid={claim.pid}, recorded_source={claim.process_identity_source}, "
        f"actual_source={identity.source}, recorded_marker="
        f"{claim.process_start_marker}, actual_marker={identity.start_marker}"
    )


def _verified_termination_reason(
    claim: NodeDebugLaunchClaimDTO,
    identity_probe: IdentityProbe,
) -> str | None:
    """能核实“登记的那一个实例已经终结”时返回结清原因，否则返回 ``None``。

    只用于已经处于 ``reconcile_required`` 的登记：被标记过的实例绝不在此自动接管或
    自动终止；只有再次核实它确实不在了（PID 条目消失，或**同来源**起始身份已变化＝原实例
    已终结且该 PID 被回收复用）才允许解除阻断。跨来源/缺标记属事实不足，继续阻断。
    """
    if claim.pid is None or claim.process_start_marker is None:
        return None
    identity = identity_probe(claim.pid)
    if identity is None:
        return f"已核实登记的进程实例不再存在: pid={claim.pid}"
    comparison = identity.compare(
        recorded_source=claim.process_identity_source,
        recorded_start_marker=claim.process_start_marker,
    )
    if comparison == "match":
        # 仍是登记的那个实例：继续阻断，由调用方按 owner 策略决定是否停止。
        return None
    if comparison == "incomparable":
        # 事实不足：不能把“比不出来”当成原实例已终结，否则就是虚报终态。
        return None
    return (
        "同来源起始身份已变化，判定原实例已终结且不得触碰新进程: "
        f"pid={claim.pid}, recorded={claim.process_start_marker}, "
        f"actual={identity.start_marker}"
    )


def decide_claim_recovery(
    claim: NodeDebugLaunchClaimDTO,
    *,
    identity_probe: IdentityProbe = probe_process_identity,
) -> NodeDebugClaimRecoveryDecision:
    """按持久 claim 判定如何处理旧实例，绝不猜测。"""
    if claim.phase == "settled":
        return NodeDebugClaimRecoveryDecision(
            outcome="settle",
            reason="claim 已结清",
        )
    if claim.phase == "reconcile_required":
        released = _verified_termination_reason(claim, identity_probe)
        if released is not None:
            prefix = f"{claim.reconcile_reason}；" if claim.reconcile_reason else ""
            return NodeDebugClaimRecoveryDecision(
                outcome="settle",
                reason=f"{prefix}{released}",
            )
        return NodeDebugClaimRecoveryDecision(
            outcome="reconcile_required",
            reason=claim.reconcile_reason or "claim 处于 reconcile_required",
        )
    if claim.pid is None:
        # launch_pending 崩溃：从未记录 PID，无法证明进程不存在。
        return NodeDebugClaimRecoveryDecision(
            outcome="reconcile_required",
            reason=(
                "启动登记停留在 launch_pending 且没有 PID，无法证明进程不存在: "
                f"process_instance_id={claim.process_instance_id}"
            ),
        )
    identity = identity_probe(claim.pid)
    if identity is None:
        return NodeDebugClaimRecoveryDecision(
            outcome="settle",
            reason=f"登记的进程实例已不存在: pid={claim.pid}",
        )
    comparison = identity.compare(
        recorded_source=claim.process_identity_source,
        recorded_start_marker=claim.process_start_marker,
    )
    if comparison == "match":
        return NodeDebugClaimRecoveryDecision(
            outcome="terminate_then_settle",
            reason=(
                "核实为登记的同一实例，按 owner 策略停止后结清: "
                f"process_instance_id={claim.process_instance_id}, pid={claim.pid}"
            ),
            identity=identity,
        )
    if comparison == "incomparable":
        # 事实不足（缺标记或跨来源）：不能认领、不能停止，也**不能**当成原实例已终结。
        return NodeDebugClaimRecoveryDecision(
            outcome="reconcile_required",
            reason=_incomparable_reason(claim, identity),
            identity=identity,
        )
    # 同来源但起始身份不同：PID 已被回收复用，原实例必已终结，绝不停止新进程。
    return NodeDebugClaimRecoveryDecision(
        outcome="settle",
        reason=(
            "PID 起始身份不匹配，判定为回收后复用，未认领也未停止新进程: "
            f"pid={claim.pid}, recorded={claim.process_start_marker}, "
            f"actual={identity.start_marker}"
        ),
        identity=identity,
    )


__all__ = [
    "ACTIVE_CLAIM_PHASES",
    "IdentityProbe",
    "NodeDebugClaimRecoveryDecision",
    "claim_marked",
    "claim_running",
    "claim_with_spawn_identity",
    "decide_claim_recovery",
    "new_launch_claim",
]
