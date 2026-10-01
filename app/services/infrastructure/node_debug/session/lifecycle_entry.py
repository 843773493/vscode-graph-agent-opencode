"""Node Debug 关停/排空入口链路。

承载 close/drain_session 两条停止入口：在 per-owner 临界区内串行停止在册
runtime，并在删除物理隔离前定点核实 durable claim。由 NodeDebugService 继承
（宿主必须提供 _runtimes、_runtimes_lock、_owner_lock、_lifecycle、_claim_runtime、
_session_state），不反向依赖顶层 service.py。
"""

from __future__ import annotations


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
        """删除物理隔离前排空该 Session 的精确 main 调试 owner。

        SessionSubtreeDeleteService 已在对应 SessionLifecycleGate exclusive
        临界区内调用本方法。这里不走普通 mutation admission（catalog 已经
        原子进入 ``deleting``），而是直接按稳定 Session ID 取 main owner，
        串行停止在册 runtime，再核实并结清同一 owner 的 durable launch
        claim。任何 ``reconcile_required`` 或残留 active claim 都向上抛错，
        让共享删除流保持源目录和 deleting record，禁止伪成功。

        child Session 会以自己的 Session ID 作为冻结子树中的独立节点再次
        回调，因此这里不扫描父 Session 的 thread 目录，也不凭 PID/端口猜
        测其它 owner。
        """
        owner = self._owner_key(session_id, "main")
        async with self._owner_lock(owner):
            runtime = self._runtimes.get(owner)
            if runtime is not None:
                outcome = await self._lifecycle.stop_runtime(runtime, clear_error=False)
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
