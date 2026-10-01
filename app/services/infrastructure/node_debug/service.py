from __future__ import annotations

import asyncio
import os
import shutil
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugStateDTO,
)
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoint_mutations import (
    NodeDebugBreakpointMutations,
)
from app.services.infrastructure.node_debug.breakpoint.source_reconciliation import (
    NodeDebugSourceReconciliation,
)
from app.services.infrastructure.node_debug.configuration.configuration_control import (
    NodeDebugConfigurationControlMixin,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.configuration_registry import (
    NodeDebugConfigurationRegistry,
)
from app.services.infrastructure.node_debug.configuration.runtime_config import (
    NodeDebugRuntimeConfig,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
)
from app.services.infrastructure.node_debug.process.evaluation import (
    NodeDebugEvaluation,
)
from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.process.observation import (
    NodeDebugRuntimeObserver,
)
from app.services.infrastructure.node_debug.process.process_lifecycle import (
    NodeDebugProcessLifecycle,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.action_dispatch import (
    NodeDebugActionDispatchMixin,
)
from app.services.infrastructure.node_debug.session.launch_entry import (
    NodeDebugLaunchEntryMixin,
)
from app.services.infrastructure.node_debug.session.lifecycle_entry import (
    NodeDebugLifecycleEntryMixin,
)
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_state import (
    NodeDebugSessionState,
)
from app.services.infrastructure.node_debug.session.state_reads import (
    NodeDebugStateReadMixin,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugOwner,
    normalize_node_debug_owner,
    resolve_node_debug_owner,
)
from app.services.infrastructure.node_debug.session.tool_actions import (
    NodeDebugToolActionMixin,
)
from app.services.orchestration.thread_residency import (
    ResidencyBlocker,
    ThreadResidencyTracker,
)

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
    append_runtime_debug_action,
)


class NodeDebugService(
    NodeDebugActionDispatchMixin,
    NodeDebugToolActionMixin,
    NodeDebugLaunchEntryMixin,
    NodeDebugStateReadMixin,
    NodeDebugLifecycleEntryMixin,
    NodeDebugConfigurationControlMixin,
):
    """通过 Node Inspector 提供 SessionThread 级 JavaScript 源码调试。

    运行时、断点、活动方案和动作时间线全部以精确 ``(session_id, thread_id)``
    owner key 隔离。所有产品入口都要求显式 ``thread_id``；main thread 使用值
    ``"main"``，owner 别名折叠只由 :mod:`node_debug_thread_owner` 定义。
    同一实体的别名地址（``(parent_session, child_session)`` 与
    ``(child_session, main)``）在入口折叠为同一个 owner key。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        config_service: ConfigService,
        session_store: NodeDebugSessionStore | None = None,
        session_admission: NodeDebugSessionAdmission,
        external_resource_leases: ExternalResourceLeaseLedger,
        residency_tracker: ThreadResidencyTracker | None = None,
        state_events: ResourceStateEventPublisher | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._config_service = config_service
        self._session_store = session_store
        #: 唯一 external_resource_leases 账本。只登记/结清 typed ``node_debug_process``
        #: 占用，不参与任何进程状态判断（见 claim_runtime 的职责说明）。
        self._external_resource_leases = external_resource_leases
        #: ThreadResidency 的 blocker 上报目标（R5b）：把已核实的 claim 相位变化单向
        #: 推送为 thread 的 idle blocker；本服务绝不反向读取 residency 推断进程状态。
        self._residency_tracker = residency_tracker
        #: resource.state/{owner_domain} 轻量通知出口：只在 owner 已核实的
        #: 释放失败（进入 reconcile_required）时发布 release_failed；成功终态
        #: 由账本 settle 发布 released。通知失败不改变 durable claim 事实。
        self._state_events = state_events
        self._append_action = partial(
            append_runtime_debug_action, max_actions=MAX_NODE_DEBUG_ACTIONS
        )
        self._runtimes: dict[NodeDebugOwner, NodeDebugRuntime] = {}
        #: per-owner 启动临界区：串行化“核实旧 claim → durable 登记 → spawn → 握手”。
        #: 条目数与 ``_runtimes`` 同量级（每个被触达过的 owner 一把锁）；不做回收，
        #: 以免丢弃仍被并发任务持有的锁。
        self._owner_locks: dict[NodeDebugOwner, asyncio.Lock] = {}
        self._configuration_factory = NodeDebugConfigurationFactory(
            workspace_root=self._workspace_root
        )
        self._configuration_registry = NodeDebugConfigurationRegistry(
            store=session_store,
            configuration_factory=self._configuration_factory,
        )
        self._session_state = NodeDebugSessionState(
            configuration_registry=self._configuration_registry,
            store=session_store,
        )
        self._session_admission = session_admission
        # 入口别名折叠需要目录索引；无持久化会话树场景（嵌入式/单测）没有可折叠的
        # thread 节点，只做 owner 归一。
        self._thread_path_resolver = (
            session_store.path_resolver if session_store is not None else None
        )
        self._runtimes_lock = asyncio.Lock()
        self._node_bin = os.environ.get("BOXTEAM_NODE_BIN") or shutil.which("node")
        self._inspector = NodeDebugInspector(
            workspace_root=self._workspace_root,
            append_action=self._append_action,
            clear_stop_snapshot=self._clear_stop_snapshot,
        )
        self._breakpoint_mutations = NodeDebugBreakpointMutations(
            workspace_root=self._workspace_root,
            configuration_factory=self._configuration_factory,
            session_state=self._session_state,
            command=self._inspector.command,
            append_action=self._append_action,
            append_pending_action=self._session_state.append_pending_action,
        )
        self._source_reconciliation = NodeDebugSourceReconciliation(
            workspace_root=self._workspace_root,
            session_state=self._session_state,
            command=self._inspector.command,
            append_action=self._append_action,
        )
        self._evaluation = NodeDebugEvaluation(
            inspector=self._inspector,
            append_action=self._append_action,
        )
        self._claim_runtime = NodeDebugClaimRuntime(
            session_store=self._session_store,
            external_resource_leases=self._external_resource_leases,
            runtimes=self._runtimes,
            residency_tracker=self._residency_tracker,
            state_events=self._state_events,
        )
        self._lifecycle = NodeDebugProcessLifecycle(
            runtimes=self._runtimes,
            read_launch_claim=self._claim_runtime.read_launch_claim,
            write_launch_claim=self._claim_runtime.write_launch_claim,
            mark_claim_phase=self._claim_runtime.mark_claim_phase,
            settle_process_lease=self._claim_runtime.settle_process_lease,
            notify_release_failed=self._claim_runtime.notify_release_failed,
            append_action=self._append_action,
            append_pending_action=self._session_state.append_pending_action,
            write_session_manifest=self._session_state.write_session_manifest,
            clear_stop_snapshot=self._clear_stop_snapshot,
        )
        self._observer = NodeDebugRuntimeObserver(
            mark_claim_phase=self._claim_runtime.mark_claim_phase,
            clear_stop_snapshot=self._clear_stop_snapshot,
            append_action=self._append_action,
        )


    def _owner_lock(self, owner: NodeDebugOwner) -> asyncio.Lock:
        """取（或懒建）该 owner 的临界区：串行化 start/stop/restart/close 的整段序列。

        asyncio 单线程事件循环内 check-then-set 之间没有 await，不存在竞态。
        临界区内的 await 全部有界：旧实例停止的 terminate 核实 3s + kill 核实 3s
        超时、socket.close、已 cancel 任务的 gather；不存在无界等待，也不会与
        ``_runtimes_lock`` 形成反向持锁（``close()`` 先释放 ``_runtimes_lock`` 再取本锁）。
        """
        lock = self._owner_locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._owner_locks[owner] = lock
        return lock


    async def clear_all_breakpoints(
        self,
        session_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        owner = self._owner_key(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is None:
            removed = self._session_state.clear_pending_breakpoints(owner)
            self._session_state.append_pending_action(
                session_id,
                thread_id,
                "clear_all_breakpoints",
                f"已清除全部源码断点（{removed} 个）",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._session_state.persist_runtime_state(
                session_id, thread_id, None
            )
            return await self.get_state(session_id, thread_id)
        async with runtime.state_lock:
            breakpoint_ids = tuple(runtime.inspector_breakpoint_ids.values())
        for inspector_id in breakpoint_ids:
            if runtime.inspector.socket is not None:
                await self._inspector.command(
                    runtime,
                    "Debugger.removeBreakpoint",
                    {"breakpointId": inspector_id},
                )
        async with runtime.state_lock:
            runtime.breakpoints.clear()
            runtime.inspector_breakpoint_ids.clear()
            self._append_action(
                runtime,
                "clear_all_breakpoints",
                "已清除全部源码断点",
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        self._session_state.persist_runtime_state(
            session_id, thread_id, runtime
        )
        return await self.get_state(session_id, thread_id)


    @staticmethod
    def _owner_key(session_id: str, thread_id: str) -> NodeDebugOwner:
        """纯 owner key 归一，只用于精确 owner 参数，不触碰目录索引。"""
        return normalize_node_debug_owner(session_id, thread_id)

    def _resolve_owner(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugOwner:
        """读入口的 owner 归一：有目录索引时按受检 thread 节点折叠别名地址。"""
        if self._thread_path_resolver is None:
            return self._owner_key(session_id, thread_id)
        return resolve_node_debug_owner(
            self._thread_path_resolver,
            session_id=session_id,
            thread_id=thread_id,
        ).key

    async def _admit_mutation(
        self,
        session_id: str,
        thread_id: str,
    ) -> NodeDebugOwner:
        """调试 mutation 的 Session 生命周期准入，并返回受检的精确 owner。

        准入同时收口持久 launch claim：能核实旧实例已终结的当场结清，无法核实的保持
        ``reconcile_required`` 并在后续断言中阻断。
        """
        owner = (await self._session_admission.admit(session_id, thread_id)).key
        await self._lifecycle.reconcile_persisted_claim(owner)
        return owner

    def residency_blockers(
        self, session_id: str, thread_id: str
    ) -> list[ResidencyBlocker]:
        """从 claim/runtime 投影读取该 owner 当前仍活跃的占用。"""
        owner = self._owner_key(session_id, thread_id)
        return self._claim_runtime.residency_blockers(*owner)


    def _assert_no_unsettled_claim(
        self, owner: NodeDebugOwner, *, operation: str
    ) -> None:
        """未结清的 durable claim（含 reconcile_required）必须阻断 owner 级操作。"""
        claim = self._claim_runtime.active_claim(*owner)
        if claim is None:
            return
        raise RuntimeError(
            f"{operation}被未结清的调试实例登记阻断: "
            f"phase={claim.phase}, session_id={owner[0]}, thread_id={owner[1]}, "
            f"reason={claim.reconcile_reason or '等待核实旧实例终态'}"
        )

    def _source_digests_for_runtime(
        self,
        runtime: NodeDebugRuntime,
    ) -> dict[str, str | None]:
        return self._source_reconciliation.source_digests(runtime)

    async def _reconcile_session_sources(
        self,
        session_id: str,
        thread_id: str,
        runtime: NodeDebugRuntime | None,
    ) -> None:
        await self._source_reconciliation.reconcile(session_id, thread_id, runtime)

    def _get_typed_debug_runtime_config(self) -> NodeDebugRuntimeConfig:
        return NodeDebugRuntimeConfig.from_mapping(
            self._config_service.get_debug_runtime_config()
        )

    def _clear_stop_snapshot(self, runtime: NodeDebugRuntime) -> None:
        """清空 Inspector 暂停快照并把断点回退为未安装态。"""
        self._inspector.clear_paused_snapshot(runtime)
        runtime.inspector_breakpoint_ids.clear()
        runtime.breakpoints = {
            breakpoint_id: breakpoint.model_copy(
                update={
                    "verified": False,
                    "actual_line": None,
                    "inspector_id": None,
                }
            )
            for breakpoint_id, breakpoint in runtime.breakpoints.items()
        }

