"""Node Debug 关停/排空入口链路。

承载 close/drain_session 两条停止入口：在 per-owner 临界区内串行停止在册
runtime，并在删除物理隔离前定点核实 durable claim。由 NodeDebugService 继承
（宿主必须提供 _runtimes、_runtimes_lock、_owner_lock、_lifecycle、_claim_runtime、
_session_state），不反向依赖顶层 service.py。
"""

from __future__ import annotations

from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore


class NodeDebugLifecycleEntryMixin:
    """关停/排空入口链路的方法族（由 NodeDebugService 继承）。"""

    async def close(self) -> None:
        async with self._runtimes_lock:
            runtimes = tuple(self._runtimes.values())
        for runtime in runtimes:
            # 关停也走 per-owner 临界区：与并发 start/stop 串行化，避免关闭期间
            # 仍有启动序列在为同一 owner spawn 新进程。先释放 _runtimes_lock 再取
            # owner 锁，保持"_owner_lock → _runtimes_lock"的单向锁序。
            async with self._owner_lock((runtime.session_id, runtime.thread_id)):
                await self._lifecycle.stop_runtime(runtime)

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
        if self._thread_path_resolver is None:
            raise RuntimeError(
                "删除 Session 前排空 Node 调试 owner 需要 catalog resolver: "
                f"session_id={session_id}"
            )
        resolver = self._thread_path_resolver
        session_node = resolver.resolve_session_node(session_id)
        main_thread_id = resolver.main_thread_id(session_id)
        control_path = session_node / CONTROL_DATABASE_NAME
        if not control_path.is_file() or control_path.is_symlink():
            raise RuntimeError(
                "删除 Session 前 session control catalog 缺失或不是普通文件，"
                "无法核实全部 thread owners: "
                f"session_id={session_id}, main_thread_id={main_thread_id}, "
                "artifact=session-control"
            )
        control = SessionControlStore(control_path)
        try:
            child_thread_ids = tuple(
                row.thread_id for row in control.list_child_thread_rows()
            )
        finally:
            control.close()

        thread_ids = (main_thread_id, *child_thread_ids)
        if len(set(thread_ids)) != len(thread_ids):
            raise RuntimeError(
                "删除 Session 前 thread catalog 出现重复 owner，阻断物理隔离: "
                f"session_id={session_id}, thread_ids={thread_ids!r}"
            )
        for thread_id in thread_ids:
            resolver.resolve_thread_node(session_id, thread_id)

        for thread_id in thread_ids:
            owner = self._owner_key(session_id, thread_id)
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
                    self._session_state.persist_runtime_state(
                        owner[0], owner[1], runtime
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
                claim = self._claim_runtime.active_claim(*owner)
                if claim is not None:
                    raise RuntimeError(
                        "删除 Session 前仍存在未结清的 Node 调试 claim，"
                        "阻断物理隔离: "
                        f"session_id={owner[0]}, thread_id={owner[1]}, "
                        f"phase={claim.phase}"
                    )
