from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from app.core.shielded_cleanup import run_sync_shielded
from app.services.infrastructure.node_debug.process.launch_claim import (
    NodeDebugClaimRecoveryDecision,
    claim_marked,
)
from app.services.infrastructure.node_debug.runtime_state import (
    LIVE_NODE_DEBUG_RUNTIME_STATUSES,
    NodeDebugActionAppender,
    NodeDebugClaimPhaseMarker,
    NodeDebugLaunchClaimReader,
    NodeDebugLaunchClaimWriter,
    NodeDebugPendingActionAppender,
    NodeDebugProcessLeaseSettler,
    NodeDebugRuntime,
    NodeDebugSessionManifestWriter,
    NodeDebugStopSnapshotClearer,
)
from app.services.infrastructure.node_debug.session.thread_owner import NodeDebugOwner

RecoverNodeDebugRun = Callable[[object], Awaitable[NodeDebugClaimRecoveryDecision]]


class NodeDebugProcessLifecycle:
    """由原 Debug 插件 run owner 停止与恢复目标进程。"""

    def __init__(
        self,
        *,
        runtimes: Mapping[NodeDebugOwner, NodeDebugRuntime],
        read_launch_claim: NodeDebugLaunchClaimReader,
        write_launch_claim: NodeDebugLaunchClaimWriter,
        mark_claim_phase: NodeDebugClaimPhaseMarker,
        settle_process_lease: NodeDebugProcessLeaseSettler,
        append_action: NodeDebugActionAppender,
        append_pending_action: NodeDebugPendingActionAppender,
        write_session_manifest: NodeDebugSessionManifestWriter,
        clear_stop_snapshot: NodeDebugStopSnapshotClearer,
        recover_run: RecoverNodeDebugRun,
    ) -> None:
        self._runtimes = runtimes
        self._read_launch_claim = read_launch_claim
        self._write_launch_claim = write_launch_claim
        self._mark_claim_phase = mark_claim_phase
        self._settle_process_lease = settle_process_lease
        self._append_action = append_action
        self._append_pending_action = append_pending_action
        self._write_session_manifest = write_session_manifest
        self._clear_stop_snapshot = clear_stop_snapshot
        self._recover_run = recover_run

    async def assert_claim_recoverable(self, owner: NodeDebugOwner) -> None:
        decision = await self.reconcile_persisted_claim(owner)
        if decision is not None and decision.outcome == "execution_lost":
            raise RuntimeError(
                "原调试实例结果未知，结清前拒绝启动新实例: "
                f"session_id={owner[0]}, thread_id={owner[1]}, reason={decision.reason}"
            )

    async def reconcile_persisted_claim(
        self, owner: NodeDebugOwner,
    ) -> NodeDebugClaimRecoveryDecision | None:
        if self._runtimes.get(owner) is not None:
            return None
        session_id, thread_id = owner
        claim = await run_sync_shielded(self._read_launch_claim, session_id, thread_id)
        if claim is None or claim.phase == "settled":
            return None
        if claim.phase == "execution_lost":
            return NodeDebugClaimRecoveryDecision("execution_lost", claim.reconcile_reason or "原 Debug run 已失效")
        try:
            decision = await self._recover_run(claim)
        except Exception as error:  # noqa: BLE001 - 冷恢复失败必须结算为明确 execution_lost
            decision = NodeDebugClaimRecoveryDecision(
                "execution_lost",
                f"无法连接原 Debug run 查询状态: {type(error).__name__}: {error}",
            )
        if decision.outcome == "running":
            return decision
        if decision.outcome == "settled":
            await self._settle_process_lease(
                session_id=claim.session_id,
                thread_id=claim.thread_id,
                process_instance_id=claim.process_instance_id,
            )
            await self._write_launch_claim(claim_marked(claim, phase="settled"))
            await self._record_claim_action(owner, "execution_settled", decision.reason)
            return decision
        await self._write_launch_claim(
            claim_marked(claim, phase="execution_lost", reason=decision.reason)
        )
        await self._record_claim_action(
            owner, "execution_lost", decision.reason, result="error",
        )
        return decision

    async def _record_claim_action(
        self,
        owner: NodeDebugOwner,
        action: str,
        message: str,
        *,
        result: Literal["success", "error"] = "success",
    ) -> None:
        session_id, thread_id = owner
        await run_sync_shielded(
            self._append_pending_action,
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
        already_settled = runtime.plugin_runtime is None and runtime.target_exited
        if not already_settled and runtime.plugin_runtime is None:
            claim = await run_sync_shielded(
                self._read_launch_claim, runtime.session_id, runtime.thread_id,
            )
            already_settled = (
                claim is not None
                and claim.phase == "settled"
                and claim.run_id == runtime.run_id
                and claim.process_instance_id == runtime.process_instance_id
            )
        async with runtime.state_lock:
            if runtime.status in LIVE_NODE_DEBUG_RUNTIME_STATUSES:
                runtime.status = "stopping"
            runtime.closing = True
        if already_settled:
            failure_reason = None
        else:
            await self._mark_claim_phase(runtime, "stopping", "等待 Debug 插件核实目标进程退出")
            failure_reason = await self.terminate_and_verify(runtime)
        if failure_reason is None:
            if runtime.plugin_runtime is not None:
                await runtime.plugin_runtime.close()
                runtime.plugin_runtime = None
            runtime.inspector.socket = None
        tasks = (runtime.inspector.receiver_task, runtime.process_task)
        current_task = asyncio.current_task()
        for task in tasks:
            if task is not None and task is not current_task and not task.done():
                task.cancel()
        pending = [task for task in tasks if task is not None and task is not current_task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with runtime.state_lock:
            self._clear_stop_snapshot(runtime)
        if failure_reason is None:
            await self._mark_claim_phase(runtime, "settled", "Debug 插件已确认目标进程退出")
            async with runtime.state_lock:
                if clear_error:
                    runtime.error_message = None
            return "stopped"
        await self._mark_claim_phase(runtime, "stopping", failure_reason)
        async with runtime.state_lock:
            runtime.status = "reconcile_required"
            runtime.error_message = f"停止调试进程失败，目标状态仍可通过原插件查询: {failure_reason}"
            self._append_action(
                runtime, "stop_result_unknown", runtime.error_message,
                actor="system", result="error",
            )
        return "reconcile_required"

    async def terminate_and_verify(self, runtime: NodeDebugRuntime) -> str | None:
        plugin = runtime.plugin_runtime
        if plugin is None:
            return "当前Backend没有原Debug插件run连接；拒绝按本机PID停止目标"
        try:
            result = await plugin.stop()
        except (ConnectionError, TimeoutError, OSError) as error:
            try:
                result = await plugin.status()
            except (ConnectionError, TimeoutError, OSError) as status_error:
                return (
                    "停止结果未知，且无法通过原Debug插件查询run状态: "
                    f"stop={type(error).__name__}, status={type(status_error).__name__}"
                )
        if result.get("run_id") != runtime.run_id:
            return "Debug插件状态回执不属于原run"
        if result.get("status") != "exited":
            return f"Debug插件未确认目标进程退出: status={result.get('status')!r}"
        code = result.get("exit_code")
        runtime.target_exit_code = code if type(code) is int else None
        runtime.target_exited = True
        return None
