from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from typing import Literal

from app.services.infrastructure.node_debug.launch_claim import (
    NodeDebugClaimRecoveryDecision,
    claim_marked,
    decide_claim_recovery,
)
from app.services.infrastructure.node_debug.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.runtime_state import (
    NodeDebugActionAppender,
    NodeDebugClaimPhaseMarker,
    NodeDebugLaunchClaimReader,
    NodeDebugLaunchClaimWriter,
    NodeDebugPendingActionAppender,
    NodeDebugProcessLeaseSettler,
    NodeDebugReleaseFailureNotifier,
    NodeDebugRuntime,
    NodeDebugSessionManifestWriter,
    NodeDebugStopSnapshotClearer,
)
from app.services.infrastructure.node_debug.thread_owner import NodeDebugOwner

_TERMINATE_TIMEOUT_SECONDS = 3.0
_KILL_TIMEOUT_SECONDS = 3.0
_RECONCILE_TERMINATE_TIMEOUT_SECONDS = 5.0


class NodeDebugProcessLifecycle:
    """集中处理 launch claim 恢复、进程终止核实和 runtime 停止收口。"""

    def __init__(
        self,
        *,
        runtimes: Mapping[NodeDebugOwner, NodeDebugRuntime],
        read_launch_claim: NodeDebugLaunchClaimReader,
        write_launch_claim: NodeDebugLaunchClaimWriter,
        mark_claim_phase: NodeDebugClaimPhaseMarker,
        settle_process_lease: NodeDebugProcessLeaseSettler,
        notify_release_failed: NodeDebugReleaseFailureNotifier,
        append_action: NodeDebugActionAppender,
        append_pending_action: NodeDebugPendingActionAppender,
        write_session_manifest: NodeDebugSessionManifestWriter,
        clear_stop_snapshot: NodeDebugStopSnapshotClearer,
    ) -> None:
        self._runtimes = runtimes
        self._read_launch_claim = read_launch_claim
        self._write_launch_claim = write_launch_claim
        self._mark_claim_phase = mark_claim_phase
        self._settle_process_lease = settle_process_lease
        self._notify_release_failed = notify_release_failed
        self._append_action = append_action
        self._append_pending_action = append_pending_action
        self._write_session_manifest = write_session_manifest
        self._clear_stop_snapshot = clear_stop_snapshot

    async def assert_claim_recoverable(self, owner: NodeDebugOwner) -> None:
        """启动前必须先核实并结清旧 claim；无法核实则拒绝启动新实例。"""
        decision = await self.reconcile_persisted_claim(owner)
        if decision is not None and decision.outcome == "reconcile_required":
            raise RuntimeError(
                "存在无法核实的旧调试实例登记，保持 reconcile_required；"
                f"拒绝启动新实例: session_id={owner[0]}, thread_id={owner[1]}, "
                f"reason={decision.reason}"
            )

    async def reconcile_persisted_claim(
        self, owner: NodeDebugOwner
    ) -> NodeDebugClaimRecoveryDecision | None:
        """按持久 claim 核实旧实例：能结清的定点结清，无法核实的保持阻断。"""
        if self._runtimes.get(owner) is not None:
            return None
        session_id, thread_id = owner
        claim = self._read_launch_claim(session_id, thread_id)
        if claim is None or claim.phase == "settled":
            return None
        decision = decide_claim_recovery(claim)
        if decision.outcome == "reconcile_required":
            if claim.phase != "reconcile_required":
                # 只在“进入”该状态时写登记并留一条审计动作；状态本身可反复查询，
                # 但读接口是轮询入口，绝不能每次轮询都追加动作并重写 manifest。
                claim = claim_marked(
                    claim, phase="reconcile_required", reason=decision.reason
                )
                self._write_launch_claim(claim)
                self._record_claim_action(
                    owner,
                    "reconcile_required",
                    f"调试实例无法核实，需人工核实后才能继续: {decision.reason}",
                    result="error",
                )
                self._notify_release_failed(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
            return decision
        if decision.outcome == "terminate_then_settle":
            terminated = await self.terminate_verified_instance(
                pid=claim.pid,
                recorded_source=claim.process_identity_source,
                recorded_start_marker=claim.process_start_marker,
            )
            if not terminated:
                failure = (
                    "已核实为登记的同一实例但停止失败，保持 reconcile_required: "
                    f"pid={claim.pid}"
                )
                self._write_launch_claim(
                    claim_marked(
                        claim,
                        phase="reconcile_required",
                        reason=failure,
                    )
                )
                self._record_claim_action(
                    owner, "reconcile_required", failure, result="error"
                )
                self._notify_release_failed(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
                return NodeDebugClaimRecoveryDecision(
                    outcome="reconcile_required",
                    reason=failure,
                    identity=decision.identity,
                )
        # 已核实旧实例不存在（或已按 owner 策略定点停止）：结清账本占用后才写
        # claim 终态；没有任何登记时账本保持原样、不重复 acquire。
        self._settle_process_lease(
            session_id=claim.session_id,
            thread_id=claim.thread_id,
            process_instance_id=claim.process_instance_id,
        )
        self._write_launch_claim(
            claim_marked(claim, phase="settled", reason=decision.reason)
        )
        self._record_claim_action(
            owner,
            "reconcile_settled",
            f"已结清遗留调试实例登记: {decision.reason}",
        )
        return NodeDebugClaimRecoveryDecision(
            outcome="settle",
            reason=decision.reason,
            identity=decision.identity,
        )

    async def wait_for_recorded_instance(
        self,
        *,
        pid: int,
        recorded_source: str | None,
        recorded_start_marker: str,
        timeout_seconds: float,
    ) -> Literal["terminated", "reused", "incomparable", "same"]:
        """轮询该 PID，直到身份事实可判定或超时。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            identity = probe_process_identity(pid)
            if identity is None:
                return "terminated"
            comparison = identity.compare(
                recorded_source=recorded_source,
                recorded_start_marker=recorded_start_marker,
            )
            if comparison != "match":
                return "reused" if comparison == "mismatch" else "incomparable"
            if loop.time() >= deadline:
                return "same"
            await asyncio.sleep(0.01)

    async def terminate_verified_instance(
        self,
        *,
        pid: int | None,
        recorded_source: str | None,
        recorded_start_marker: str | None,
    ) -> bool:
        """只终止再次核实为同一实例的进程；PID 复用和事实不足一律不碰。"""
        if pid is None or recorded_start_marker is None:
            return False
        state = await self.wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=0.0,
        )
        if state == "terminated":
            return True
        if state == "reused":
            return True
        if state == "incomparable":
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        state = await self.wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=_RECONCILE_TERMINATE_TIMEOUT_SECONDS,
        )
        if state != "same":
            return state in {"terminated", "reused"}
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        state = await self.wait_for_recorded_instance(
            pid=pid,
            recorded_source=recorded_source,
            recorded_start_marker=recorded_start_marker,
            timeout_seconds=_RECONCILE_TERMINATE_TIMEOUT_SECONDS,
        )
        return state in {"terminated", "reused"}

    def _record_claim_action(
        self,
        owner: NodeDebugOwner,
        action: str,
        message: str,
        *,
        result: Literal["success", "error"] = "success",
    ) -> None:
        session_id, thread_id = owner
        self._append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor="system",
            tool_name=None,
            tool_call_id=None,
            result=result,
        )
        self._write_session_manifest(session_id, thread_id)

    async def stop_runtime(
        self,
        runtime: NodeDebugRuntime,
        *,
        clear_error: bool = True,
    ) -> Literal["stopped", "reconcile_required"]:
        """停止并核实进程终结；未核实终结时保持 reconcile_required 阻断。"""
        async with runtime.state_lock:
            if runtime.status in {"starting", "running", "paused"}:
                runtime.status = "stopping"
            runtime.closing = True
        self._mark_claim_phase(runtime, "stopping", "收到停止请求，等待进程终结")
        socket = runtime.inspector.socket
        if socket is not None:
            await socket.close()
            runtime.inspector.socket = None
        failure_reason = await self.terminate_and_verify(runtime)
        tasks = (
            runtime.inspector.receiver_task,
            runtime.stderr_task,
            runtime.stdout_task,
            runtime.process_task,
        )
        current_task = asyncio.current_task()
        for task in tasks:
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        pending = [
            task for task in tasks if task is not None and task is not current_task
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with runtime.state_lock:
            self._clear_stop_snapshot(runtime)
        if failure_reason is None:
            self._mark_claim_phase(runtime, "settled", "已核实进程终结并结清")
            async with runtime.state_lock:
                if clear_error:
                    runtime.error_message = None
            return "stopped"
        self._mark_claim_phase(runtime, "reconcile_required", failure_reason)
        async with runtime.state_lock:
            runtime.status = "reconcile_required"
            runtime.error_message = (
                f"停止调试进程失败且无法核实终态: {failure_reason}"
            )
            self._append_action(
                runtime,
                "stop_reconcile_required",
                f"停止调试进程失败且无法核实终态: {failure_reason}",
                actor="system",
                result="error",
            )
        return "reconcile_required"

    async def terminate_and_verify(
        self, runtime: NodeDebugRuntime
    ) -> str | None:
        """终止 runtime 进程并核实终结；None 表示已核实不存在。"""
        process = runtime.process
        if process is None:
            return None
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except OSError as error:
                self._append_action(
                    runtime,
                    "stop_signal_failed",
                    f"发送终止信号失败: {error}",
                    actor="system",
                    result="error",
                )
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=_TERMINATE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    self._append_action(
                        runtime,
                        "stop_kill_failed",
                        f"强制终止调试进程失败: {error}",
                        actor="system",
                        result="error",
                    )
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_KILL_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    pass
            except OSError as error:
                self._append_action(
                    runtime,
                    "stop_wait_failed",
                    f"等待调试进程退出失败: {error}",
                    actor="system",
                    result="error",
                )
        if process.returncode is not None:
            return None
        return self.verify_process_gone(runtime)

    def verify_process_gone(
        self, runtime: NodeDebugRuntime
    ) -> str | None:
        """按已登记的 OS 起始身份核实进程是否终结。"""
        process = runtime.process
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "进程句柄未报告终态且缺少可用 PID"
        identity = probe_process_identity(pid)
        if identity is None:
            return None
        if runtime.process_start_marker is None:
            return (
                "缺少可核实的 OS 进程起始身份，不能判定终结: "
                f"pid={pid}, source={identity.source}"
            )
        comparison = identity.compare(
            recorded_source=runtime.process_identity_source,
            recorded_start_marker=runtime.process_start_marker,
        )
        if comparison == "match":
            return f"进程仍存活且起始身份匹配: pid={pid}"
        if comparison == "incomparable":
            return (
                "PID 当前实例的起始身份与登记不可比对，无法核实是否同一实例: "
                f"pid={pid}, recorded_source={runtime.process_identity_source}, "
                f"actual_source={identity.source}"
            )
        return None
