"""typed 后台工作注册、恢复与运行实体；持久 authority 仅在控制库。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime
from typing import Literal, cast

from app.abstractions.background_task_registry import (
    BackgroundTaskDescriptorFactory,
    BackgroundTaskHandler,
    DerivedBackgroundTaskHandler,
)
from app.core.failures import failure_detail
from app.core.session_lifecycle_gate import (
    OperationHolderGate,
    SessionDeletionPendingError,
)
from app.core.thread_residency_contracts import (
    BackgroundResidencyPort,
    ResidencyBlocker,
)
from app.domain.background_work import (
    ACTIVE_BACKGROUND_TASK_STATUSES,
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStatus,
    BackgroundTaskStep,
    BackgroundTaskSummary,
)
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.background_work_store import BackgroundWorkStore


class BackgroundTaskRegistry:
    def __init__(
        self, *, store: BackgroundWorkStore,
        residency_tracker: BackgroundResidencyPort,
        handlers: tuple[BackgroundTaskHandler, ...],
        admission_guard: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._admission_guard = admission_guard
        self._residency = residency_tracker
        self._handlers = {(handler.kind, handler.version): handler for handler in handlers}
        if len(self._handlers) != len(handlers):
            raise ValueError("后台恢复 handler kind/version 重复")
        self._holders = OperationHolderGate(store.sessions_dir, namespace="background_tasks")
        self._running_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._derived_queue: deque[tuple[str, str]] = deque()
        self._derived_background: tuple[str, str] | None = None
        self._derived_admission = asyncio.Lock()
        self._active_session_ids: set[str] | None = None
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
        if self._handler(descriptor).effect == "derived_request":
            raise ValueError("派生请求必须通过有界 spawn_derived 准入")
        handle = await self._register(session_id=session_id, thread_id=thread_id, agent_id=agent_id,
            task_id=task_id, descriptor=descriptor)
        if handle.status in {"pending", "running"}:
            self._launch(handle)
        return handle

    async def _register(
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
            if self._admission_guard is not None:
                self._admission_guard(session_id)
            self._store.resolve_thread(session_id, thread_id)
            self._residency.assert_blocker_admission(session_id, thread_id)
            with self._store.control(session_id) as control:
                handle = control.create_background_task(
                    task_id=task_id, session_id=session_id, thread_id=thread_id, agent_id=agent_id,
                    descriptor=descriptor, checkpoint=checkpoint, preimage_hash=preimage_hash,
                )
            if self._active_session_ids is not None and handle.status in ACTIVE_BACKGROUND_TASK_STATUSES:
                self._active_session_ids.add(session_id)
            return handle

    async def spawn_derived(
        self, *, session_id: str, thread_id: str, agent_id: str, task_id: str,
        descriptor: BackgroundTaskDescriptor | BackgroundTaskDescriptorFactory, foreground: bool = False,
    ) -> BackgroundTaskHandle | None:
        if self._stopping:
            raise RuntimeError("后台任务注册表已停止准入")
        async with self._derived_admission:
            try:
                await self.recover_derived_thread(session_id, thread_id)
                existing = self.get_handle(session_id, task_id)
                running = self._derived_running_count()
                if existing is None and (running >= 2 if foreground else (
                    (self._derived_background is not None or running >= 2) and len(self._derived_queue) >= 4
                )):
                    return None
                async with AsyncExitStack() as frozen_input:
                    if callable(descriptor):
                        if existing is not None and existing.thread_id != thread_id:
                            raise ValueError("derived-input-conflict: 幂等任务已属于不同 Thread")
                        resolved = await frozen_input.enter_async_context(descriptor(existing))
                        if existing is not None:
                            agent_id = existing.agent_id
                    else:
                        resolved = descriptor
                    if self._handler(resolved).effect != "derived_request":
                        raise ValueError("有界派生准入只接受 derived_request handler")
                    handle = await self._register(session_id=session_id, thread_id=thread_id, agent_id=agent_id,
                        task_id=task_id, descriptor=resolved)
                if handle.status in {"pending", "running"}:
                    key = (session_id, handle.task_id)
                    if foreground:
                        if existing is None:
                            self._launch(handle)
                        else:
                            self._promote_derived(handle)
                    elif existing is None:
                        if self._derived_background is None and self._derived_running_count() < 2:
                            self._derived_background = key
                            self._launch(handle)
                        else:
                            self._derived_queue.append(key)
                return handle
            finally:
                self._start_next_derived()

    def _derived_running_count(self) -> int:
        return sum(
            self._handler(handle.descriptor).effect == "derived_request"
            for (session_id, task_id), task in self._running_tasks.items()
            if not task.done() and (handle := self.get_handle(session_id, task_id)) is not None
        )

    def _derived_deadline(self, handle: BackgroundTaskHandle) -> datetime:
        return cast(DerivedBackgroundTaskHandler, self._handler(handle.descriptor)).deadline(handle)

    def _derived_handles(self, session_id: str, thread_id: str) -> list[BackgroundTaskHandle]:
        with self._store.control(session_id, read_only=True) as control:
            return [handle for handle in control.list_background_tasks(thread_id=thread_id, active_only=True)
                    if self._handler(handle.descriptor).effect == "derived_request"]

    async def promote_derived(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        if self._stopping:
            raise RuntimeError("后台任务注册表已停止准入")
        async with self._derived_admission:
            try:
                handle = self.get_handle(session_id, task_id)
                if handle is None or self._handler(handle.descriptor).effect != "derived_request":
                    raise ValueError("前台提升要求已登记的派生任务")
                await self.recover_derived_thread(session_id, handle.thread_id)
                handle = self.get_handle(session_id, task_id)
                if handle is None:
                    raise RuntimeError("派生任务在提升期间丢失")
                self._promote_derived(handle)
                return handle
            finally:
                self._start_next_derived()

    def _promote_derived(self, handle: BackgroundTaskHandle) -> None:
        # 已运行或总额度满时保留原 task、队列位置与期限。
        key = (handle.session_id, handle.task_id)
        if key in self._derived_queue and handle.status == "pending" and self._derived_running_count() < 2:
            self._derived_queue.remove(key)
            self._launch(handle)

    async def retry_derived(
        self, session_id: str, task_id: str, *, deadline: datetime, validate: Callable[[BackgroundTaskHandle], None],
    ) -> BackgroundTaskHandle:
        """显式前台请求才可补尝试；原 holder 退出后在共同额度和 Session 准入中裁决。"""
        if self._stopping:
            raise RuntimeError("后台任务注册表已停止准入")
        if (running := self.get_task(session_id, task_id)) is not None:
            await asyncio.gather(running, return_exceptions=True)
        async with self._derived_admission:
            try:
                handle = self.get_handle(session_id, task_id)
                if handle is None or self._handler(handle.descriptor).effect != "derived_request":
                    raise ValueError("补尝试要求已登记的派生任务")
                if handle.status != "failed":
                    self._promote_derived(handle)
                    return handle
                if self._derived_running_count() >= 2:
                    raise RuntimeError("derived-capacity-exhausted: 前台补尝试没有可用运行额度")
                async with self._holders.lock_for(f"{session_id}:{task_id}", wait=False) as holder:
                    if not holder.acquired:
                        raise RuntimeError("derived-retry-holder-active: 原任务尚未退出，拒绝重复调用")
                    async with self._store.admission(session_id):
                        self._store.resolve_thread(session_id, handle.thread_id)
                        self._residency.assert_blocker_admission(session_id, handle.thread_id)
                        if self._admission_guard is not None:
                            self._admission_guard(session_id)
                        if deadline.tzinfo is None:
                            raise ValueError("派生补尝试期限必须带时区")
                        if deadline <= datetime.now(UTC):
                            raise TimeoutError("派生补尝试已超过本次前台操作绝对期限")
                        validate(handle)
                        self._settle_lease(handle)
                        with self._store.control(session_id) as control:
                            handle = control.retry_background_task(task_id, expected_token=handle.fencing_token, deadline=deadline)
                        if self._active_session_ids is not None:
                            self._active_session_ids.add(session_id)
                self._launch(handle)
                return handle
            finally:
                self._start_next_derived()

    def _start_next_derived(self) -> None:
        if self._stopping or self._derived_background is not None or self._derived_running_count() >= 2:
            return
        while self._derived_queue:
            key = self._derived_queue.popleft()
            handle = self.get_handle(*key)
            if handle is not None and handle.status == "pending":
                self._derived_background = key
                self._launch(handle)
                return

    @asynccontextmanager
    async def derived_publication(self, handle: BackgroundTaskHandle) -> AsyncIterator[None]:
        """只包住 rollout 最终发布；取消和发布共用原准入，不覆盖网络等待。"""
        if self._handler(handle.descriptor).effect != "derived_request":
            raise ValueError("派生发布 gate 只接受 derived_request")
        async with self._store.admission(handle.session_id):
            self._store.resolve_thread(handle.session_id, handle.thread_id)
            self._assert_generation(handle)
            current = self.get_handle(handle.session_id, handle.task_id)
            if current is None or current.status != "running" or current.fencing_token != handle.fencing_token:
                raise RuntimeError("派生任务已取消或 fencing 失效，拒绝晚到结果")
            if self._derived_deadline(current) <= datetime.now(UTC):
                self._finish(current, "failed", "派生请求已到期，拒绝晚到结果")
                raise TimeoutError("派生请求已超过绝对期限，拒绝发布结果")
            yield

    def _validate_step(self, handle: BackgroundTaskHandle, step: BackgroundTaskStep) -> None:
        handler = self._handler(handle.descriptor)
        handler.validate_checkpoint(step.checkpoint)
        if handler.effect == "derived_request":
            if step.messages or step.result_message_ids:
                raise ValueError("派生请求不能写入普通后台邮箱")
            if step.completed and not step.result:
                raise ValueError("派生任务完成必须引用已提交的完整结果")

    async def _settle_derived(
        self, handle: BackgroundTaskHandle, status: Literal["failed", "cancelled"], note: str,
    ) -> BackgroundTaskHandle:
        try:
            async with self._store.admission(handle.session_id):
                current = self.get_handle(handle.session_id, handle.task_id)
                if current is None:
                    raise RuntimeError("派生任务在结算期间丢失")
                if current.status not in ACTIVE_BACKGROUND_TASK_STATUSES:
                    if current.status == "failed":
                        await cast(DerivedBackgroundTaskHandler, self._handler(current.descriptor)).abandon(
                            current, outcome=status,
                        )
                    self._settle_lease(current)
                    return current
                step = await self._handler(current.descriptor).reconcile(current)
                if step is None:
                    await cast(DerivedBackgroundTaskHandler, self._handler(current.descriptor)).abandon(
                        current, outcome=status,
                    )
                    return self._finish(current, status, note)
                self._validate_step(current, step)
                if not step.completed:
                    raise ValueError("派生恢复只能确认已提交的完整结果")
                with self._store.control(current.session_id) as control:
                    if current.status == "pending":
                        lease = control.get_lease(current.lease_id)
                        control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                        current = control.claim_background_task(current.task_id, expected_token=current.fencing_token,
                            runtime_generation=self._residency.snapshot(current.session_id, current.thread_id).generation)
                    current = control.commit_background_step(current.task_id, token=current.fencing_token, step=step)
                self._settle_lease(current)
                return current
        except SessionDeletionPendingError:
            current = self.get_handle(handle.session_id, handle.task_id)
            if current is None:
                raise RuntimeError("删除期间派生任务恢复证据丢失")
            handler = cast(DerivedBackgroundTaskHandler, self._handler(current.descriptor))
            if current.status in ACTIVE_BACKGROUND_TASK_STATUSES and await handler.reconcile(current) is None:
                await handler.abandon(current, outcome="cancelled")
            return self._finish(current, "cancelled", "Session 删除已关闭派生发布准入")

    async def recover_derived_thread(self, session_id: str, thread_id: str) -> tuple[BackgroundTaskHandle, ...]:
        """只在真实访问该 Thread 时结算旧 holder；没有 ready 结果就终结，不重放。"""
        recovered = []
        for handle in self._derived_handles(session_id, thread_id):
            key = (session_id, handle.task_id)
            if self._derived_deadline(handle) <= datetime.now(UTC):
                recovered.append(await self._settle_derived(handle, "failed", "派生请求已超过本次 attempt 期限"))
                if key in self._derived_queue:
                    self._derived_queue.remove(key)
                if (task := self.get_task(*key)) is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                continue
            if key in self._derived_queue or self.get_task(*key) is not None:
                continue
            async with self._holders.lock_for(f"{session_id}:{handle.task_id}", wait=False) as holder:
                if holder.acquired:
                    async with self._store.admission(session_id):
                        with self._store.control(session_id) as control:
                            current = control.get_background_task(handle.task_id)
                            if current is None or current.status not in {"pending", "running"}:
                                continue
                            lease = control.get_lease(current.lease_id)
                            control.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
                            handle = control.claim_background_task(current.task_id, expected_token=current.fencing_token,
                                runtime_generation=self._residency.snapshot(session_id, thread_id).generation)
                    recovered.append(await self._settle_derived(handle, "failed", "派生请求已失去 holder 且无已提交结果；禁止自动重放"))
        if not self._derived_admission.locked():
            self._start_next_derived()
        return tuple(recovered)

    def _launch(self, handle: BackgroundTaskHandle) -> None:
        key = (handle.session_id, handle.task_id)
        existing = self._running_tasks.get(key)
        if existing is not None and not existing.done():
            return
        # 持久任务独立于原 Job；配置快照、delta sink 和执行身份不能随创建者泄入。
        task = asyncio.create_task(self._run(handle), name=f"{handle.task_name}:{handle.task_id}", context=Context())
        self._running_tasks[key] = task
        task.add_done_callback(lambda completed: self._observe_completion(key, completed))

    def _observe_completion(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._running_tasks.get(key) is task:
            del self._running_tasks[key]
        if self._derived_background == key:
            self._derived_background = None
        # 准入跨 await 时只释放原槽，由准入结束后统一续排，避免抢走已检查的额度。
        if not self._derived_admission.locked():
            self._start_next_derived()
        if not task.cancelled() and (error := task.exception()) is not None:
            asyncio.get_running_loop().call_exception_handler({
                "message": "后台任务执行失败，详细原因见诊断", "exception": error, "task": task,
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
                if handler.effect == "derived_request" and self._derived_deadline(handle) <= datetime.now(UTC):
                    await self._settle_derived(handle, "failed", "派生请求在排队期间已到期，未调用上游")
                    return
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
                    acquired = True
                remaining = None
                if handler.effect == "derived_request":
                    remaining = (self._derived_deadline(handle) - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError("派生请求在开始执行前已到期")
                async with asyncio.timeout(remaining):
                    while handle.status == "running":
                        self._assert_generation(handle)
                        if handler.effect == "derived_request" and self._derived_deadline(handle) <= datetime.now(UTC):
                            raise TimeoutError("派生请求在下一步骤开始前已到期")
                        if handler.effect == "external":
                            async with self._store.admission(handle.session_id):
                                with self._store.control(handle.session_id) as control:
                                    control.mark_background_external_effect(handle.task_id, token=handle.fencing_token)
                            handle.external_effect_pending = True
                        # 外部执行/等待只持独立 holder 锁，不跨 await 持事务或 Session gate。
                        step = await handler.step(handle)
                        self._validate_step(handle, step)
                        async with self._store.admission(handle.session_id):
                            self._assert_generation(handle)
                            with self._store.control(handle.session_id) as control:
                                handle = control.commit_background_step(handle.task_id, token=handle.fencing_token, step=step)
                self._settle_lease(handle)
        except asyncio.CancelledError:
            if acquired:
                if handler.effect == "derived_request":
                    handle = await self._settle_derived(handle, "cancelled", "派生请求已取消；无结果不自动重放")
                else:
                    status = "reconcile_required" if handle.external_effect_pending else ("pending" if self._stopping else "cancelled")
                    handle = self._finish(handle, status, "后端停止，等待恢复" if self._stopping else "任务已取消")
            raise
        except SessionDeletionPendingError:
            if acquired:
                handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "cancelled", "Session 删除已关闭后台回调准入")
        except Exception as error:
            if acquired:
                detail = failure_detail(
                    error, stage="background-task-execution",
                    label=f"后台任务失败 session_id={handle.session_id}, thread_id={handle.thread_id}, task_id={handle.task_id}",
                )
                if handler.effect == "derived_request":
                    handle = await self._settle_derived(handle, "failed", detail)
                else:
                    handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "failed", detail)
            raise

    def _assert_generation(self, handle: BackgroundTaskHandle) -> None:
        if handle.runtime_generation and not self._residency.is_current_generation(
            handle.session_id, handle.thread_id, handle.runtime_generation,
        ):
            raise RuntimeError("后台回调来自已失效的 Thread runtime generation")

    def _settle_lease(self, handle: BackgroundTaskHandle) -> None:
        if handle.status in ACTIVE_BACKGROUND_TASK_STATUSES:
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
        self._settle_lease(handle)
        return handle

    async def start(self) -> None:
        active_session_ids: set[str] = set()
        for session_id in self._store.session_ids():
            with self._store.control(session_id) as control:
                handles = [*control.list_unsettled_background_tasks(), *control.list_background_tasks(active_only=True)]
            for handle in handles:
                if handle.status not in ACTIVE_BACKGROUND_TASK_STATUSES:
                    self._settle_lease(handle)
                else:
                    active_session_ids.add(session_id)
                    if handle.status != "reconcile_required" and self._handler(handle.descriptor).effect != "derived_request":
                        self._handler(handle.descriptor).validate_checkpoint(handle.checkpoint)
                        self._launch(handle)
        self._active_session_ids = active_session_ids

    async def stop(self) -> None:
        self._stopping = True
        while self._derived_queue:
            handle = self.get_handle(*self._derived_queue.popleft())
            if handle is not None:
                await self._settle_derived(handle, "cancelled", "后端停止，取消尚未运行的派生任务")
        tasks = [task for task in self._running_tasks.values() if not task.done()]
        for session_id, task_id in tuple(self._running_tasks):
            handle = self.get_handle(session_id, task_id)
            if handle is not None and self._handler(handle.descriptor).effect == "derived_request":
                await self._settle_derived(handle, "cancelled", "后端停止，关闭派生结果发布准入")
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
        """返回当前仍运行的本地 Task；持久终态通过 get_handle 查询。"""
        task = self._running_tasks.get((session_id, task_id))
        return task if task is not None and not task.done() else None

    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]:
        with self._store.control(session_id, read_only=True) as control:
            return control.list_background_tasks(active_only=True)

    def list_summaries(self, session_id: str, *, include_history: bool) -> list[BackgroundTaskSummary]:
        with self._store.control(session_id, read_only=True) as control:
            return control.list_background_task_summaries(include_history=include_history)

    def list_active_handles(self) -> list[BackgroundTaskHandle]:
        # 启动恢复与本 registry 的准入确定候选；状态正文始终读取控制库。
        # 与当前 catalog 相交，排除任务终结后已被删除的会话。
        candidates = self._active_session_ids
        if candidates is None:
            session_ids = self._store.session_ids()
        elif candidates:
            session_ids = [session_id for session_id in self._store.session_ids() if session_id in candidates]
        else:
            return []
        handles = [handle for session_id in session_ids for handle in self.list_handles(session_id)]
        if candidates is not None:
            self._active_session_ids = {handle.session_id for handle in handles}
        return handles

    def residency_blockers(self, session_id: str, thread_id: str) -> tuple[ResidencyBlocker, ...]:
        with self._store.control(session_id, read_only=True) as control:
            active = any(self._handler(handle.descriptor).effect != "derived_request"
                or (session_id, handle.task_id) in self._derived_queue
                or self.get_task(session_id, handle.task_id) is not None
                for handle in control.list_background_tasks(thread_id=thread_id, active_only=True))
        return (ResidencyBlocker("background_task", "后台任务仍在运行或等待恢复对账"),) if active else ()

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        handle = self.get_handle(session_id, task_id)
        if handle is None:
            raise ValueError(f"后台任务不存在: session_id={session_id}, task_id={task_id}")
        if self._handler(handle.descriptor).effect == "derived_request":
            handle = await self._settle_derived(handle, "cancelled", "任务已取消，关闭派生结果发布准入")
            key = (session_id, task_id)
            if key in self._derived_queue:
                self._derived_queue.remove(key)
        task = self._running_tasks.get((session_id, task_id))
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            handle = self.get_handle(session_id, task_id)
        if handle is None:
            raise RuntimeError("后台任务在取消期间丢失")
        if handle.status in ACTIVE_BACKGROUND_TASK_STATUSES:
            handle = self._finish(handle, "reconcile_required" if handle.external_effect_pending else "cancelled", "任务已取消")
        return handle

    async def cancel_all_active(self, *, reason: str) -> int:
        async with self._derived_admission:
            try:
                handles = self.list_active_handles()
                for handle in handles:
                    await self.cancel(handle.session_id, handle.task_id)
                return len(handles)
            finally:
                self._start_next_derived()

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
                    if handle.status not in ACTIVE_BACKGROUND_TASK_STATUSES:
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

    async def cleanup_session(
        self, session_id: str, *, action: Literal["stop", "delete"], thread_id: str | None = None,
    ) -> int:
        # 整批排空共用原调度门，前项退出不能启动本批尚未取消的 pending。
        async with self._derived_admission:
            try:
                with self._store.control(session_id, read_only=True) as control:
                    handles = [handle for handle in control.list_background_tasks()
                        if handle.status != "deleted" and (thread_id is None or handle.thread_id == thread_id)]
                for handle in handles:
                    result = await (self.delete(session_id, handle.task_id) if action == "delete"
                                    else self.cancel(session_id, handle.task_id))
                    if result.status == "reconcile_required":
                        raise RuntimeError(f"后台任务外部效果未确认，停止未完成: {session_id}/{handle.task_id}")
                return len(handles)
            finally:
                self._start_next_derived()
