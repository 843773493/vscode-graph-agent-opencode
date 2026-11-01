from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.core.thread_residency_contracts import ResidencyBlocker
from app.schemas.internal_v2.node_debug import (
    NodeDebugLaunchClaimDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.launch_claim import (
    ACTIVE_CLAIM_PHASES,
    claim_marked,
    claim_running,
)
from app.services.infrastructure.node_debug.runtime_state import (
    ACTIVE_NODE_DEBUG_RUNTIME_STATUSES,
    NodeDebugRuntime,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import NodeDebugOwner
from app.services.orchestration.thread_residency import (
    ThreadResidencyTracker,
)

logger = logging.getLogger(__name__)

_NODE_DEBUG_BLOCKER_REASON: dict[str, str] = {
    "launch_pending": "Node 调试进程已登记启动，等待 spawn 与握手核实",
    "spawned": "Node 调试进程已启动，等待 Inspector 握手核实",
    "running": "Node 调试进程运行中",
    "stopping": "Node 调试进程停止中，等待核实终结",
    "reconcile_required": "Node 调试实例无法核实终态，需核实后才能解除占用",
}


@dataclass(frozen=True, slots=True)
class NodeDebugProcessLeaseIdentity:
    """typed ``node_debug_process`` 账本中的 owner 与实例身份。"""

    resource_id: str
    holder_id: str
    lease_id: str
    operation_id: str

    @classmethod
    def for_process_instance(
        cls,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> NodeDebugProcessLeaseIdentity:
        resource_id = f"node_debug_process:{session_id}:{thread_id}"
        return cls(
            resource_id=resource_id,
            holder_id=f"node-debug-owner:{session_id}:{thread_id}",
            lease_id=f"{resource_id}:{process_instance_id}",
            operation_id=process_instance_id,
        )


class NodeDebugClaimRuntime:
    """集中维护 launch claim、process lease 与 residency blocker 的事实投影。"""

    def __init__(
        self,
        *,
        session_store: NodeDebugSessionStore | None,
        external_resource_leases: ExternalResourceLeaseLedger,
        runtimes: Mapping[NodeDebugOwner, NodeDebugRuntime],
        residency_tracker: ThreadResidencyTracker | None,
        state_events: ResourceStateEventPublisher | None,
    ) -> None:
        self._session_store = session_store
        self._external_resource_leases = external_resource_leases
        self._runtimes = runtimes
        self._residency_tracker = residency_tracker
        self._state_events = state_events

    async def settle_process_lease(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None:
        identity = NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=session_id,
            thread_id=thread_id,
            process_instance_id=process_instance_id,
        )
        if self._external_resource_leases.get_lease(identity.lease_id) is None:
            return
        await self._external_resource_leases.settle(identity.lease_id)

    async def write_launch_claim(self, claim: NodeDebugLaunchClaimDTO) -> None:
        store = self._session_store
        if store is None:
            return
        tracker = self._residency_tracker
        blocker_key = f"node_debug_claim_write:{claim.process_instance_id}"
        active = claim.phase in _NODE_DEBUG_BLOCKER_REASON
        if tracker is not None and active:
            # 原 blocker 机制覆盖排队及写盘；先撤销卸载资格，再让出事件循环。
            # 真实 claim 的投影只在提交后更新，写入失败不能遗留虚假的持久占用。
            tracker.register_blocker(
                claim.session_id, claim.thread_id, blocker_key=blocker_key,
                kind="node_debug_process", reason="Node 调试实例登记正在持久提交",
            )

        async def publish() -> None:
            await run_sync_shielded(store.write_launch_claim, claim)
            self._sync_residency_blocker(claim)

        pending = asyncio.create_task(publish())
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as original:
            try:
                await run_cleanup_shielded(lambda: pending)
            except BaseException as failure:
                raise original from failure
            raise
        finally:
            if tracker is not None and active:
                tracker.release_blocker(
                    claim.session_id, claim.thread_id, blocker_key=blocker_key,
                )

    def residency_blockers(
        self,
        session_id: str,
        thread_id: str,
    ) -> list[ResidencyBlocker]:
        owner = (session_id, thread_id)
        blockers: list[ResidencyBlocker] = []
        runtime = self._runtimes.get(owner)
        if runtime is not None and runtime.status in ACTIVE_NODE_DEBUG_RUNTIME_STATUSES:
            blockers.append(
                ResidencyBlocker(
                    kind="node_debug_process",
                    reason="Node 调试运行时在册且未核实终态",
                )
            )
        claim = self.active_claim(*owner)
        if claim is not None:
            blockers.append(
                ResidencyBlocker(
                    kind="node_debug_process",
                    reason=_NODE_DEBUG_BLOCKER_REASON.get(
                        claim.phase,
                        "Node 调试进程占用该 thread",
                    ),
                )
            )
        return blockers

    def read_launch_claim(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugLaunchClaimDTO | None:
        if self._session_store is None:
            return None
        return self._session_store.read_launch_claim(session_id, thread_id)

    def active_claim(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugLaunchClaimDTO | None:
        claim = self.read_launch_claim(session_id, thread_id)
        if claim is None or claim.phase not in ACTIVE_CLAIM_PHASES:
            return None
        return claim

    def claim_for_runtime(
        self,
        runtime: NodeDebugRuntime,
    ) -> NodeDebugLaunchClaimDTO | None:
        claim = self.read_launch_claim(runtime.session_id, runtime.thread_id)
        if claim is None or runtime.process_instance_id is None:
            return None
        if claim.process_instance_id != runtime.process_instance_id:
            return None
        return claim

    async def mark_claim_running(self, runtime: NodeDebugRuntime) -> None:
        # 进程monitor也通过原state_lock结清，不能越过尚未发布的同实例占用。
        async with runtime.state_lock:
            claim = self.claim_for_runtime(runtime)
            if claim is None:
                return
            if claim.phase != "running":
                await self.write_launch_claim(
                    claim_running(
                        claim,
                        inspector_port=self._authoritative_inspector_port(runtime),
                    )
                )
            identity = NodeDebugProcessLeaseIdentity.for_process_instance(
                session_id=claim.session_id, thread_id=claim.thread_id,
                process_instance_id=claim.process_instance_id,
            )

            def register() -> None:
                self._external_resource_leases.register_external(
                    resource_id=identity.resource_id, kind="node_debug_process", lifetime_scope="session",
                )
                self._external_resource_leases.acquire(
                    resource_id=identity.resource_id, turn_stream_id=identity.holder_id,
                    lease_id=identity.lease_id, operation_id=identity.operation_id,
                )

            # 原owner准入与锁持续覆盖worker结算；取消不能留下晚于停止发布的活跃占用。
            # TODO(5.4)：同步读取、原生I/O抢占与整个启动/关闭期限仍须分别收口。
            pending = asyncio.create_task(run_sync_shielded(register))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError as original:
                try:
                    await run_cleanup_shielded(lambda: pending)
                except BaseException as failure:
                    raise original from failure
                raise

    @staticmethod
    def _authoritative_inspector_port(runtime: NodeDebugRuntime) -> int:
        inspector_url = runtime.inspector.inspector_url
        if inspector_url is not None:
            port = urlparse(inspector_url).port
            if isinstance(port, int) and port > 0:
                return port
        return runtime.inspector_port

    async def mark_claim_phase(
        self,
        runtime: NodeDebugRuntime,
        phase: Literal["stopping", "reconcile_required", "settled"],
        reason: str | None = None,
    ) -> None:
        async with runtime.state_lock:
            claim = self.claim_for_runtime(runtime)
            if claim is None or claim.phase == "settled":
                return
            if phase == "reconcile_required":
                self.notify_release_failed(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
            if phase == "settled":
                await self.settle_process_lease(
                    session_id=claim.session_id,
                    thread_id=claim.thread_id,
                    process_instance_id=claim.process_instance_id,
                )
            await self.write_launch_claim(claim_marked(claim, phase=phase, reason=reason))

    def notify_release_failed(
        self,
        *,
        session_id: str,
        thread_id: str,
        process_instance_id: str,
    ) -> None:
        if self._state_events is None:
            return
        resource_id = NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=session_id,
            thread_id=thread_id,
            process_instance_id=process_instance_id,
        ).resource_id
        try:
            self._state_events.publish(resource_id=resource_id, state="release_failed")
        except (RuntimeError, ValueError):
            logger.exception(
                "resource.state release_failed 事件发布失败: resource_id=%s",
                resource_id,
            )

    def _sync_residency_blocker(self, claim: NodeDebugLaunchClaimDTO) -> None:
        if self._residency_tracker is None:
            return
        blocker_key = f"node_debug_claim:{claim.process_instance_id}"
        reason = _NODE_DEBUG_BLOCKER_REASON.get(claim.phase)
        if reason is None:
            self._residency_tracker.release_blocker(
                claim.session_id,
                claim.thread_id,
                blocker_key=blocker_key,
            )
            return
        self._residency_tracker.register_blocker(
            claim.session_id,
            claim.thread_id,
            blocker_key=blocker_key,
            kind="node_debug_process",
            reason=reason,
        )


__all__ = [
    "NodeDebugClaimRuntime",
    "NodeDebugProcessLeaseIdentity",
]
