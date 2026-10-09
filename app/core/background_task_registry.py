"""typed 后台工作注册、恢复与运行实体；持久 authority 仅在控制库。"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.background_task_contracts import (
    ACTIVE_TASK_STATUSES,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskHandler,
    BackgroundTaskStatus,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    OperationHolderGate,
    SessionDeletionPendingError,
)
from app.core.thread_residency_contracts import (
    BackgroundResidencyPort,
    ResidencyBlocker,
)
from app.domain.itemized.hashing import sha256_jcs


class BackgroundWorkStoreProtocol(Protocol):
    sessions_dir: Path

    def session_ids(self) -> list[str]: ...

    def resolve_thread(self, session_id: str, thread_id: str) -> None: ...

    def control(self, session_id: str, *, read_only: bool = False) -> AbstractContextManager[SessionControlStore]: ...

    def admission(self, session_id: str) -> AbstractAsyncContextManager[None]: ...


@dataclass(slots=True)
class _BackgroundTaskRecord:
    handle: BackgroundTaskHandle
    task: asyncio.Task[None]


class BackgroundTaskRegistry:
    def __init__(
        self, *, store: BackgroundWorkStoreProtocol,
        residency_tracker: BackgroundResidencyPort,
        handlers: tuple[BackgroundTaskHandler, ...],
    ) -> None:
        self._store = store
        self._residency = residency_tracker
        self._handlers = {(handler.kind, handler.version): handler for handler in handlers}
        if len(self._handlers) != len(handlers):
            raise ValueError("后台恢复 handler kind/version 重复")
        self._holders = OperationHolderGate(store.sessions_dir, namespace="background_tasks")
        self._tasks: dict[tuple[str, str], _BackgroundTaskRecord] = {}
        self._stopping = False
        residency_tracker.add_blocker_source(self)

    def _handler(self, descriptor: BackgroundTaskDescriptor) -> BackgroundTaskHandler:
        handler = self._handlers.get((descriptor.kind, descriptor.version))
        if handler is None:
            raise ValueError(f"后台任务不具备已注册恢复 handler: {descriptor.kind}@{descriptor.version}")
        return handler

    async def spawn(
        self, *, session_id: str, thread_id: str, agent_id: str, task_id: str,
        descriptor: BackgroundTaskDescriptor,
    ) -> BackgroundTaskHandle:
        if self._stopping:
            raise RuntimeError("后台任务注册表已停止准入")
        if not task_id or not agent_id:
            raise ValueError("后台 task_id 与 agent selector 必须显式提供")
        handler = self._handler(descriptor)
        checkpoint = handler.initial_checkpoint(descriptor)
        handler.validate_checkpoint(checkpoint)
        preimage_hash = sha256_jcs({
            "session_id": session_id, "thread_id": thread_id, "agent_id": agent_id,
            "descriptor": descriptor.model_dump(mode="json"),
        }).split(":")[-1]
        async with self._store.admission(session_id):
            self._store.resolve_thread(session_id, thread_id)
            self._residency.assert_blocker_admission(session_id, thread_id)
            with self._store.control(session_id) as control:
                handle = control.create_background_task(
                    task_id=task_id, session_id=session_id, thread_id=thread_id, agent_id=agent_id,
                    descriptor=descriptor, checkpoint=checkpoint, preimage_hash=preimage_hash,
                )
            if handle.status in {"pending", "running"}:
                self._launch(handle)
            return handle

    def _launch(self, handle: BackgroundTaskHandle) -> None:
        key = (handle.session_id, handle.task_id)
        existing = self._tasks.get(key)
        if existing is not None and not existing.task.done():
            return
        task = asyncio.create_task(self._run(handle), name=f"{handle.task_name}:{handle.task_id}")
        self._tasks[key] = _BackgroundTaskRecord(handle, task)
        task.add_done_callback(self._observe_failure)

    @staticmethod
    def _observe_failure(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            asyncio.get_running_loop().call_exception_handler({
                "message": "后台任务执行失败，详细错误已持久化", "exception": error, "task": task,
            })

    async def _run(self, original: BackgroundTaskHandle) -> None:
        handle = original
        acquired = False
        try:
            async with self._holders.lock_for(f"{handle.session_id}:{handle.task_id}", wait=True) as holder:
                if not holder.acquired:
                    return
                handler = self._handler(handle.descriptor)
                handler.validate_checkpoint(handle.checkpoint)
                async with self._store.admission(handle.session_id):
                    generation = self._residency.snapshot(handle.session_id, handle.thread_id).generation
                    with self._store.control(handle.session_id) as control:
                        current = control.get_background_task(handle.task_id)
                        if current is None:
                            raise RuntimeError("后台任务 descriptor 在领取前丢失")
                        if current.status not in {"pending", "running"}:
                            return
                        if current.external_effect_pending:
                            self._finish(current, "reconcile_required", "外部效果未确认，需要显式对账；禁止自动重放")
                            return
                        lease = control.get_lease(current.lease_id)
                        control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                        handle = control.claim_background_task(
                            handle.task_id, expected_token=current.fencing_token, runtime_generation=generation,
                        )
                    self._tasks[(handle.session_id, handle.task_id)].handle = handle
                    acquired = True
                while handle.status == "running":
                    self._assert_generation(handle)
                    if handler.effect == "external":
                        async with self._store.admission(handle.session_id):
                            with self._store.control(handle.session_id) as control:
                                control.mark_background_external_effect(handle.task_id, token=handle.fencing_token)
                        handle.external_effect_pending = True
                    # 外部执行/等待只持独立 holder 锁，不跨 await 持事务或 Session gate。
                    step = await handler.step(handle)
                    handler.validate_checkpoint(step.checkpoint)
                    async with self._store.admission(handle.session_id):
                        self._assert_generation(handle)
                        with self._store.control(handle.session_id) as control:
                            handle = control.commit_background_step(handle.task_id, token=handle.fencing_token, step=step)
                        self._tasks[(handle.session_id, handle.task_id)].handle = handle
                self._settle_lease(handle)
        except asyncio.CancelledError:
            if acquired:
                status = "reconcile_required" if handle.external_effect_pending else ("pending" if self._stopping else "cancelled")
                handle = self._finish(handle, status, "后端停止，等待恢复" if self._stopping else "任务已取消")
            raise
        except SessionDeletionPendingError:
            if acquired:
                handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "cancelled", "Session 删除已关闭后台回调准入")
        except Exception as error:
            if acquired:
                handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "failed", f"{type(error).__name__}: {error}")
            raise

    def _assert_generation(self, handle: BackgroundTaskHandle) -> None:
        if handle.runtime_generation and not self._residency.is_current_generation(
            handle.session_id, handle.thread_id, handle.runtime_generation,
        ):
            raise RuntimeError("后台回调来自已失效的 Thread runtime generation")

    def _settle_lease(self, handle: BackgroundTaskHandle) -> None:
        if handle.status in ACTIVE_TASK_STATUSES:
            return
        with self._store.control(handle.session_id) as control:
            lease = control.get_lease(handle.lease_id)
            if lease.state in {"completed", "cancelled", "failed"}:
                return
            if lease.state == "active":
                lease = control.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
            control.settle_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token,
                                 outcome=handle.status if handle.status in {"completed", "cancelled"} else "failed")

    def _finish(self, handle: BackgroundTaskHandle, status: BackgroundTaskStatus, note: str) -> BackgroundTaskHandle:
        with self._store.control(handle.session_id) as control:
            handle = control.finish_background_task(handle.task_id, token=handle.fencing_token, status=status, note=note)
        record = self._tasks.get((handle.session_id, handle.task_id))
        if record is not None:
            record.handle = handle
        self._settle_lease(handle)
        return handle

    async def start(self) -> None:
        for session_id in self._store.session_ids():
            with self._store.control(session_id) as control:
                handles = control.list_background_tasks()
            for handle in handles:
                if handle.status not in ACTIVE_TASK_STATUSES:
                    self._settle_lease(handle)
                elif handle.status != "reconcile_required":
                    self._handler(handle.descriptor).validate_checkpoint(handle.checkpoint)
                    self._launch(handle)

    async def stop(self) -> None:
        self._stopping = True
        tasks = [record.task for record in self._tasks.values() if not record.task.done()]
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("后台任务关闭失败", failures)

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None:
        with self._store.control(session_id, read_only=True) as control:
            return control.get_background_task(task_id)

    def get_task(self, session_id: str, task_id: str) -> asyncio.Task[None] | None:
        record = self._tasks.get((session_id, task_id))
        return record.task if record is not None else None

    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]:
        with self._store.control(session_id, read_only=True) as control:
            return [handle for handle in control.list_background_tasks(active_only=True) if handle.status in ACTIVE_TASK_STATUSES]

    def list_closed_handles(self, session_id: str) -> list[BackgroundTaskHandle]:
        with self._store.control(session_id, read_only=True) as control:
            return [handle for handle in control.list_background_tasks() if handle.status not in ACTIVE_TASK_STATUSES]

    def list_active_handles(self) -> list[BackgroundTaskHandle]:
        return [handle for session_id in self._store.session_ids() for handle in self.list_handles(session_id)]

    def residency_blockers(self, session_id: str, thread_id: str) -> tuple[ResidencyBlocker, ...]:
        with self._store.control(session_id, read_only=True) as control:
            active = any(handle.status in ACTIVE_TASK_STATUSES for handle in control.list_background_tasks(thread_id=thread_id, active_only=True))
        return (ResidencyBlocker("background_task", "后台任务仍在运行或等待恢复对账"),) if active else ()

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        handle = self.get_handle(session_id, task_id)
        if handle is None:
            raise ValueError(f"后台任务不存在: session_id={session_id}, task_id={task_id}")
        record = self._tasks.get((session_id, task_id))
        if record is not None and not record.task.done():
            record.task.cancel()
            await asyncio.gather(record.task, return_exceptions=True)
            handle = self.get_handle(session_id, task_id)
        if handle is None:
            raise RuntimeError("后台任务在取消期间丢失")
        if handle.status in ACTIVE_TASK_STATUSES:
            handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "cancelled", "任务已取消")
        return handle

    async def cancel_all_active(self, *, reason: str) -> int:
        handles = self.list_active_handles()
        for handle in handles:
            await self.cancel(handle.session_id, handle.task_id)
        return len(handles)

    async def reconcile(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        """已注册 handler 核实未知外部效果；没有确认事实就保留原恢复证据。"""
        async with self._holders.lock_for(f"{session_id}:{task_id}", wait=False) as holder:
            if not holder.acquired:
                raise ValueError("后台任务仍有活跃 holder，不能并发对账")
            async with self._store.admission(session_id):
                with self._store.control(session_id) as control:
                    handle = control.get_background_task(task_id)
                    if handle is None:
                        raise ValueError(f"后台任务不存在: {task_id}")
                    if handle.status not in ACTIVE_TASK_STATUSES:
                        return handle
                    if handle.status != "reconcile_required":
                        raise ValueError("后台任务不处于外部效果待对账状态")
                    handler = self._handler(handle.descriptor)
                    if handler.effect != "external":
                        raise ValueError("原子 mailbox handler 没有外部效果需要对账")
                    lease = control.get_lease(handle.lease_id)
                    control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                    handle = control.claim_background_task(task_id, expected_token=handle.fencing_token,
                        runtime_generation=self._residency.snapshot(session_id, handle.thread_id).generation,
                        reconcile=True)
            step = await handler.reconcile(handle)
            if step is None:
                raise ValueError("后台 handler 尚未确认外部效果，恢复证据保留；禁止盲目重放")
            handler.validate_checkpoint(step.checkpoint)
            self._assert_generation(handle)
            async with self._store.admission(session_id):
                with self._store.control(session_id) as control:
                    handle = control.commit_background_step(task_id, token=handle.fencing_token, step=step, reconcile=True)
            self._settle_lease(handle)
        if handle.status == "running":
            self._launch(handle)
        return handle

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        handle = await self.cancel(session_id, task_id)
        if handle.status == "reconcile_required":
            raise RuntimeError("后台任务外部效果未确认，不能删除恢复证据")
        return self._finish(handle, "deleted", "任务记录已删除")

    async def delete_session(self, session_id: str) -> int:
        with self._store.control(session_id, read_only=True) as control:
            handles = control.list_background_tasks()
        for handle in handles:
            if handle.status != "deleted":
                await self.delete(session_id, handle.task_id)
        return len(handles)
