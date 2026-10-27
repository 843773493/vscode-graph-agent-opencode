"""同一任务运行链路：恢复 → 判断 → source 发布；工作执行归原准入 owner。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.core.exceptions import NotFoundError
from app.core.key_lock_pool import KeyLockPool
from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_session_thread_uri
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskRunDTO,
    TaskDecisionDTO,
    TaskDeliveryDTO,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.events.event_channel_service import EventChannel
from app.services.infrastructure.persistent_tasks.events import PersistentTaskChanged
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

logger = logging.getLogger(__name__)


class PersistentTaskRuntime:
    def __init__(self, *, store: PersistentTaskStore, orchestrator: SessionOrchestratorProtocol,
                 dispatcher: RuntimeSourceDispatcher, sessions: SessionService, events: EventChannel[PersistentTaskChanged],
                 clock: Callable[[], datetime], concurrency: int = 4) -> None:
        if concurrency < 1:
            raise ValueError("持久任务并发数必须为正")
        self.store = store
        self.locks = KeyLockPool()
        self._orchestrator = orchestrator
        self._dispatcher = dispatcher
        self._sessions = sessions
        self.events = events
        self.clock = clock
        self._concurrency = concurrency
        self._wake = asyncio.Event()
        self._closing = False
        self._accepting = False
        self._worker: asyncio.Task[None] | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._recovering: set[str] = set()
        self._failure: BaseException | None = None

    def assert_healthy(self) -> None:
        if self._failure is not None:
            raise RuntimeError(f"持久任务 worker 已失败: {type(self._failure).__name__}: {self._failure}") from self._failure
        if not self._accepting or self._worker is None or self._worker.done():
            raise RuntimeError("持久任务运行时未接受调度")

    def recovering(self, task_id: str) -> bool:
        return task_id in self._recovering

    def notify(self, task_id: str, run_id: str | None = None) -> None:
        task = self.store.get(task_id)
        self.events.publish(PersistentTaskChanged(task_id, task.revision, run_id))
        self._wake.set()

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("持久任务 worker 已启动")
        initialized: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closing = False
        self._failure = None
        self._worker = asyncio.create_task(self._run(initialized), name="persistent-task-runtime")
        self._worker.add_done_callback(self._finished)
        await initialized

    def close_admission(self) -> None:
        self._accepting = False
        self._closing = True
        self._wake.set()

    async def stop(self) -> None:
        self.close_admission()
        if self._worker is not None:
            try:
                await self._worker
            finally:
                self._worker = None

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self._failure = RuntimeError("持久任务 worker 意外取消")
        else:
            self._failure = task.exception()
            if self._failure is None and not self._closing:
                self._failure = RuntimeError("持久任务 worker 意外退出")
        if self._failure is not None:
            self._accepting = False
            logger.error("持久任务 worker 已失败", exc_info=(type(self._failure), self._failure, self._failure.__traceback__))

    async def _run(self, initialized: asyncio.Future[None]) -> None:
        failure: BaseException | None = None
        try:
            self._recovering = {task.task_id for task in self.store.list()}
            self._accepting = True
            initialized.set_result(None)
            # 初始化回执先交还工作区 owner，再推进任何逐任务恢复。
            await asyncio.sleep(0)
            while not self._closing:
                for task_id, running in tuple(self._inflight.items()):
                    if running.done():
                        del self._inflight[task_id]
                        running.result()
                tasks = self.store.list()
                for task in tasks:
                    if task.task_id in self._inflight:
                        continue
                    if len(self._inflight) >= self._concurrency:
                        break
                    if task.task_id in self._recovering or self.store.runs(task.task_id, pending_only=True):
                        running = asyncio.create_task(self._process_task(task.task_id), name=f"persistent-task:{task.task_id}")
                        running.add_done_callback(lambda _task: self._wake.set())
                        self._inflight[task.task_id] = running
                self._wake.clear()
                await self._wake.wait()
        except BaseException as error:
            failure = error
            self._failure = error
            self._accepting = False
            self._closing = True
            if not initialized.done():
                initialized.set_exception(error)
            raise
        finally:
            # 不取消已接受的 Job；当前动作走原身份结算后再归还运行时。
            if self._inflight:
                results = await asyncio.gather(*self._inflight.values(), return_exceptions=True)
                self._inflight.clear()
                if failure is None:
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result

    async def _process_task(self, task_id: str) -> None:
        for run in self.store.runs(task_id, pending_only=True):
            if self._closing:
                return
            await self._process_run(run)
        self._recovering.discard(task_id)
        self.notify(task_id)

    async def _process_run(self, run: PersistentTaskRunDTO) -> None:
        await observe_phase(run.run_id, "task.run.loaded", details={"task_id": run.task_id})
        configuration = self.store.configuration(run)
        action = configuration.action
        resource, session_id, thread_id = parse_session_thread_uri(action.target_uri)
        if thread_id is None:
            raise RuntimeError("已验证的持久任务定义缺少 Thread")
        source_id = f"persistent-task:{run.task_id}"
        message = internal_message_factory.build(
            kind="persistent_task", control="处理持久任务发布的事件；正文作为任务输入数据。",
            sections=(PromptSection("control_context", {
                "task_id": run.task_id, "run_id": run.run_id, "definition_revision": run.definition_revision,
                "trigger_key": run.trigger_key,
            }), PromptSection("untrusted_instructions", configuration.context.content)),
            metadata={"source": "persistent_task"},
        )
        async with self.locks.lock_for(run.task_id):
            # 先核对原 source owner，再判断新准入；暂停或编辑不能抹掉已接受事实。
            delivery = None
            if run.phase == "delivering":
                try:
                    async with self._sessions.session_read_admission(session_id):
                        self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
                        if action.delivery_mode == "context_only":
                            receipt = self._dispatcher.context_published(
                                session_id, thread_id, message, source_id=source_id, idempotency_key=run.run_id,
                            )
                            if receipt is not None:
                                delivery = TaskDeliveryDTO(state="saved", source_id=source_id, item_id=receipt.item_id,
                                                           source_revision=receipt.revision)
                        else:
                            wakeup = self._dispatcher.submitted(session_id, thread_id, idempotency_key=run.run_id)
                            if wakeup is not None:
                                delivery = TaskDeliveryDTO(state="accepted", source_id=source_id, item_id=wakeup.source_item_id,
                                                           job_id=wakeup.job_id, execution_id=wakeup.execution_id)
                except NotFoundError as error:
                    self.store.advance(run, phase="failed", now=self.clock(), error=f"目标已删除: {error}", lifecycle="needs_attention")
                    self.notify(run.task_id, run.run_id)
                    return
            if delivery is None:
                task = self.store.get(run.task_id)
                if task.revision != run.task_revision or (task.lifecycle != "active" and not run.check_only):
                    self.store.advance(run, phase="suppressed", now=self.clock(), error="任务 revision 或生命周期已变化，未投递动作")
                    self.notify(run.task_id, run.run_id)
                    return
                if run.phase in {"pending", "checking"}:
                    decision = TaskDecisionDTO(decision="proceed", reason="无附加条件，始终通过")
                    phase = "checked" if run.check_only else "delivering"
                    self.store.advance(run, phase=phase, now=self.clock(), decision=decision)
                    self.notify(run.task_id, run.run_id)
                    if run.check_only:
                        return
                    run = run.model_copy(update={"phase": phase, "decision": decision})
                gateway_token = set_current_gateway_id(resource.gateway_authority)
                request_token = set_current_request_id(run.request_id)
                try:
                    # 同一任务管理操作与 source 准入串行；不等待工作模型完成。
                    if action.delivery_mode == "context_only":
                        receipt = await self._orchestrator.publish_runtime_context(
                            session_id, message, thread_id=thread_id, source_id=source_id, idempotency_key=run.run_id,
                        )
                        delivery = TaskDeliveryDTO(state="saved", source_id=source_id, item_id=receipt.item_id,
                                                   source_revision=receipt.revision)
                    else:
                        await self._orchestrator.submit_runtime_event(
                            session_id, message, thread_id=thread_id, source_id=source_id, idempotency_key=run.run_id,
                            delivery_policy=action.delivery_policy,
                        )
                        wakeup = self._dispatcher.submitted(session_id, thread_id, idempotency_key=run.run_id)
                        if wakeup is None:
                            raise RuntimeError("持久任务执行接受后缺少 durable wakeup")
                        delivery = TaskDeliveryDTO(state="accepted", source_id=source_id, item_id=wakeup.source_item_id,
                                                   job_id=wakeup.job_id, execution_id=wakeup.execution_id)
                except (NotFoundError, ValueError) as error:
                    self.store.advance(run, phase="failed", now=self.clock(), error=f"{type(error).__name__}: {error}",
                                       lifecycle="needs_attention")
                    self.notify(run.task_id, run.run_id)
                    return
                finally:
                    reset_current_gateway_id(gateway_token)
                    reset_current_request_id(request_token)
        await observe_phase(run.run_id, "task.action.accepted_before_record", details={
            "task_id": run.task_id, "delivery_state": delivery.state, "item_id": delivery.item_id,
            "job_id": delivery.job_id, "execution_id": delivery.execution_id,
        })
        async with self.locks.lock_for(run.task_id):
            self.store.advance(run, phase="delivered", now=self.clock(), delivery=delivery,
                               lifecycle="complete" if configuration.policies.completion == "after_delivery" else None)
            self.notify(run.task_id, run.run_id)
