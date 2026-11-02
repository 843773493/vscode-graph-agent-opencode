"""Node Debug 关停/排空入口链路。

承载 close/drain_session 两条停止入口：在 per-owner 临界区内串行停止在册
runtime，并在删除物理隔离前定点核实 durable claim。由 NodeDebugService 继承
（宿主必须提供 _runtimes、_runtimes_lock、_owner_lock、_lifecycle、_claim_runtime、
_session_state、_session_store），不反向依赖顶层 service.py。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.core.shielded_cleanup import run_sync_shielded

if TYPE_CHECKING:
    from app.services.infrastructure.node_debug.process.claim_runtime import (
        NodeDebugClaimRuntime,
    )
    from app.services.infrastructure.node_debug.process.process_lifecycle import (
        NodeDebugProcessLifecycle,
    )
    from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
    from app.services.infrastructure.node_debug.session.session_state import (
        NodeDebugSessionState,
    )
    from app.services.infrastructure.node_debug.session.session_store import (
        NodeDebugSessionStore,
    )
    from app.services.infrastructure.node_debug.session.thread_owner import (
        NodeDebugOwner,
    )

logger = logging.getLogger(__name__)
NODE_DEBUG_CLOSE_WAIT_SECONDS = 30.0


class NodeDebugLifecycleEntryMixin:
    """关停/排空入口链路的方法族（由 NodeDebugService 继承）。"""

    _session_store: NodeDebugSessionStore | None
    _runtimes: dict[NodeDebugOwner, NodeDebugRuntime]
    _runtimes_lock: asyncio.Lock
    _lifecycle: NodeDebugProcessLifecycle
    _claim_runtime: NodeDebugClaimRuntime
    _session_state: NodeDebugSessionState
    _closing: bool
    _admitted_operations: dict[NodeDebugOwner, set[asyncio.Future[None]]]
    _close_tasks: dict[NodeDebugOwner, asyncio.Task[None]]

    if TYPE_CHECKING:
        def _owner_lock(self, owner: NodeDebugOwner) -> asyncio.Lock: ...

    async def close(self) -> None:
        """关闭准入并独立排空各 owner；观察到期不取消原关闭任务。"""
        self._closing = True
        owners = dict.fromkeys((*self._runtimes, *self._admitted_operations, *self._close_tasks))
        for owner in owners:
            task = self._close_tasks.get(owner)
            if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
                task = asyncio.create_task(
                    self._close_owner(owner), name=f"node-debug-close:{owner[0]}:{owner[1]}",
                )
                self._close_tasks[owner] = task
                task.add_done_callback(self._observe_close)
        if not self._close_tasks:
            return
        tasks = tuple(self._close_tasks.values())
        done, pending = await asyncio.wait(tasks, timeout=NODE_DEBUG_CLOSE_WAIT_SECONDS)
        errors: list[BaseException] = []
        for task in tasks:
            if task in done:
                try:
                    task.result()
                except BaseException as error:  # noqa: BLE001 - 整批保留原任务的失败与取消。
                    errors.append(error)
        if pending:
            errors.append(TimeoutError(
                "Node 调试关闭等待超时，原任务及未核实资源仍保留: "
                f"tasks={sorted(task.get_name() for task in pending)}"
            ))
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Node 调试关闭未收敛", errors)

    async def _close_owner(self, owner: NodeDebugOwner) -> None:
        # 已准入操作可能尚未物化 runtime；等待其 Session gate 退出后再读取最终实例。
        operations = tuple(self._admitted_operations.get(owner, ()))
        if operations:
            await asyncio.wait(operations)
        async with self._owner_lock(owner):
            runtime = self._runtimes.get(owner)
            if runtime is not None:
                outcome = await self._lifecycle.stop_runtime(runtime)
                if outcome == "reconcile_required":
                    raise RuntimeError(
                        "关闭 Node 调试服务未能核实进程终态: "
                        f"session_id={owner[0]}, thread_id={owner[1]}"
                    )
        # TODO(5.4)：原生I/O不可抢占；本次预算只限制观察，不保证整个进程退出。

    @staticmethod
    def _observe_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Node 调试关闭任务失败: task=%s", task.get_name(),
                         exc_info=(type(error), error, error.__traceback__))

    async def drain_session(self, session_id: str) -> None:
        """删除物理隔离前排空该 Session 的全部 catalog 调试 owner。

        SessionSubtreeDeleteService 已在对应 SessionLifecycleGate exclusive
        临界区内调用本方法。这里不走普通 mutation admission（catalog 已经
        原子进入 ``deleting``），而是从 Session control thread catalog 读取
        main 与已发布 child owners，逐一验证 catalog 路径、停止在册 runtime，
        再核实并结清各自的 durable launch claim。任何 ``reconcile_required``
        或残留 active claim 都向上抛错，让共享删除流保持源目录和 deleting
        record，禁止伪成功；不扫描物理 thread 目录，也不凭 PID/端口猜测 owner。
        """
        if self._session_store is None:
            raise RuntimeError(
                "删除 Session 前排空 Node 调试 owner 需要 catalog resolver: "
                f"session_id={session_id}"
            )
        # 连接的打开、读取、关闭和路径核对留在同一 worker；取消也先结算再释放删除 gate。
        owners = await run_sync_shielded(self._session_store.session_thread_owners, session_id)
        # TODO(5.4)：原生I/O抢占、不合作取消及整次排空期限仍须单独核实。
        for owner in owners:
            async with self._owner_lock(owner):
                runtime = self._runtimes.get(owner)
                if runtime is not None:
                    outcome = await self._lifecycle.stop_runtime(
                        runtime,
                        clear_error=False,
                    )
                    if outcome == "reconcile_required":
                        raise RuntimeError(
                            "删除 Session 前无法核实 Node 调试进程终态，"
                            "保持 reconcile_required 并阻断删除: "
                            f"session_id={owner[0]}, thread_id={owner[1]}"
                        )
                    async with runtime.state_lock:
                        runtime.status = "exited"
                        runtime.error_message = None
                    # 与 Web/API stop 入口保持同一 authoritative manifest 语义；
                    # 物理 rename 发生在本回调返回之后。
                    # 进程监控已排空；Session删除gate与owner锁覆盖原同步投影及写盘。
                    await run_sync_shielded(
                        self._session_state.persist_runtime_state, owner[0], owner[1], runtime
                    )

                # runtime 缺失时按 durable claim 恢复合同定点核实旧实例；若
                # claim 仍不可核实，必须阻断删除，而不能把内存缺项当成 stopped。
                decision = await self._lifecycle.reconcile_persisted_claim(owner)
                if decision is not None and decision.outcome == "reconcile_required":
                    raise RuntimeError(
                        "删除 Session 前无法核实 Node 调试 claim，"
                        "保持 reconcile_required 并阻断删除: "
                        f"session_id={owner[0]}, thread_id={owner[1]}, "
                        f"reason={decision.reason}"
                    )
                claim = await run_sync_shielded(self._claim_runtime.active_claim, *owner)
                if claim is not None:
                    raise RuntimeError(
                        "删除 Session 前仍存在未结清的 Node 调试 claim，"
                        "阻断物理隔离: "
                        f"session_id={owner[0]}, thread_id={owner[1]}, "
                        f"phase={claim.phase}"
                    )
                if runtime is not None:
                    # 删除后catalog不再定位该节点；只有全部结清才能移除已停止投影。
                    async with self._runtimes_lock:
                        del self._runtimes[owner]
