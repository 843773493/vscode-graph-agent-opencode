"""同一任务运行链路：恢复 → 判断 → source 发布；工作执行归原准入 owner。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.core.exceptions import NotFoundError
from app.core.job_event_bus import EventType
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
from app.schemas.event import Event
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    PersistentTaskRunDTO,
    TaskDecisionDTO,
    TaskDeliveryDTO,
    TaskEvaluationDTO,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.events.event_channel_service import EventChannel
from app.services.infrastructure.persistent_tasks.events import PersistentTaskChanged
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.persistent_tasks.conditions import (
    TaskCheckError,
    condition_input,
    evaluate_condition,
    model_decision,
)
from app.services.orchestration.persistent_tasks.contexts import action_context
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

logger = logging.getLogger(__name__)


class PersistentTaskRuntime:
    def __init__(self, *, store: PersistentTaskStore, orchestrator: SessionOrchestratorProtocol,
                 dispatcher: RuntimeSourceDispatcher, sessions: SessionService, events: EventChannel[PersistentTaskChanged],
                 clock: Callable[[], datetime], workspace_root: Path, saver: RolloutCheckpointSaver,
                 job_events: JobEventBusProtocol, concurrency: int = 4) -> None:
        if concurrency < 1:
            raise ValueError("持久任务并发数必须为正")
        self.store = store
        self.locks = KeyLockPool()
        self._orchestrator = orchestrator
        self._dispatcher = dispatcher
        self._sessions = sessions
        self.events = events
        self.clock = clock
        self._workspace_root = workspace_root
        self._saver = saver
        self._job_events = job_events
        self._execution_pending: set[str] = set()
        self._waiting_checks: set[str] = set()
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

    def reconcile_execution_source(self, task_id: str) -> None:
        self._execution_pending.add(task_id)
        self._wake.set()

    async def execution_source_cursor(self, configuration: PersistentTaskConfigurationDTO) -> int | None:
        uri = configuration.trigger.source_uri
        if uri is None:
            return None
        _resource, session_id, thread_id = parse_session_thread_uri(uri)
        assert thread_id is not None
        async with self._sessions.session_read_admission(session_id):
            self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
            return self._saver.execution_completions(session_id, thread_id, after_commit_id=None).through_commit_id

    async def on_event(self, event: Event) -> None:
        # 事件只唤醒原worker；恢复读取canonical终态提交，不依赖内存通知完整性。
        if event.type in {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED} and not self._closing:
            self._waiting_checks.clear()
            self._execution_pending.update(task.task_id for task in self.store.list()
                                           if task.configuration.trigger.source_uri is not None and task.lifecycle == "active")
            self._wake.set()

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
        self._waiting_checks.clear()
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
            await self._job_events.register_durable_listener(self.on_event)
            self._accepting = True
            initialized.set_result(None)
            # 初始化回执先交还工作区 owner，再推进任何逐任务恢复。
            await asyncio.sleep(0)
            while not self._closing:
                self._wake.clear()
                for task_id, running in tuple(self._inflight.items()):
                    if running.done():
                        del self._inflight[task_id]
                        running.result()
                for task_id in self.store.schedule_due(self.clock()):
                    self.notify(task_id)
                tasks = self.store.list()
                for task in tasks:
                    if task.task_id in self._inflight or task.task_id in self._waiting_checks:
                        continue
                    if len(self._inflight) >= self._concurrency:
                        break
                    if (task.task_id in self._recovering or task.task_id in self._execution_pending
                            or self.store.runs(task.task_id, pending_only=True)):
                        self._execution_pending.discard(task.task_id)
                        running = asyncio.create_task(self._process_task(task.task_id), name=f"persistent-task:{task.task_id}")
                        running.add_done_callback(lambda _task: self._wake.set())
                        self._inflight[task.task_id] = running
                next_run = self.store.next_schedule_at()
                if next_run is None:
                    await self._wake.wait()
                else:
                    delay = max(0, (next_run - self.clock()).total_seconds())
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    except TimeoutError:
                        pass
        except BaseException as error:
            failure = error
            self._failure = error
            self._accepting = False
            self._closing = True
            if not initialized.done():
                initialized.set_exception(error)
            raise
        finally:
            await self._job_events.unregister_durable_listener(self.on_event)
            # 不取消已接受的 Job；当前动作走原身份结算后再归还运行时。
            if self._inflight:
                results = await asyncio.gather(*self._inflight.values(), return_exceptions=True)
                self._inflight.clear()
                if failure is None:
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result

    async def _process_task(self, task_id: str) -> None:
        await self._collect_execution_completions(task_id)
        for run in self.store.runs(task_id, pending_only=True):
            if self._closing:
                return
            await self._process_run(run)
            if task_id in self._waiting_checks:
                return
        self._recovering.discard(task_id)
        self.notify(task_id)

    async def _collect_execution_completions(self, task_id: str) -> None:
        async with self.locks.lock_for(task_id):
            task = self.store.get(task_id)
            uri = task.configuration.trigger.source_uri
            if uri is None or task.lifecycle != "active":
                return
            _resource, session_id, thread_id = parse_session_thread_uri(uri)
            assert thread_id is not None
            try:
                async with self._sessions.session_read_admission(session_id):
                    self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
                    cursor = self.store.execution_cursor(task_id)
                    page = self._saver.execution_completions(session_id, thread_id, after_commit_id=cursor)
            except NotFoundError as error:
                self.store.control(task_id, expected_revision=task.revision, lifecycle="needs_attention",
                                   now=self.clock(), reason=f"执行结束来源已删除: {error}")
                self.notify(task_id)
                return
            if page.records:
                await observe_phase(task_id, "task.execution_source.read_before_accept", details={"task_id": task_id})
            self.store.accept_execution_completions(task_id, expected_revision=task.revision, after_commit_id=cursor,
                                                     page=page, now=self.clock())
            if page.records:
                self.reconcile_execution_source(task_id)
                self.notify(task_id)

    async def _process_run(self, run: PersistentTaskRunDTO) -> None:
        await observe_phase(run.run_id, "task.run.loaded", details={"task_id": run.task_id})
        configuration = self.store.configuration(run)
        if run.phase in {"pending", "checking"}:
            checked = await self._check(run, configuration)
            if checked is None:
                return
            run = checked
        action = configuration.action
        resource, session_id, thread_id = parse_session_thread_uri(action.target_uri)
        if thread_id is None:
            raise RuntimeError("已验证的持久任务定义缺少 Thread")
        source_id = f"persistent-task:{run.task_id}"
        message = action_context(configuration, run)
        context_mode = configuration.context.mode
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
                                context_mode=context_mode,
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
                gateway_token = set_current_gateway_id(resource.gateway_authority)
                request_token = set_current_request_id(run.request_id)
                try:
                    # 同一任务管理操作与 source 准入串行；不等待工作模型完成。
                    if action.delivery_mode == "context_only":
                        receipt = await self._orchestrator.publish_runtime_context(
                            session_id, message, thread_id=thread_id, source_id=source_id, idempotency_key=run.run_id,
                            context_mode=context_mode,
                        )
                        delivery = TaskDeliveryDTO(state="saved", source_id=source_id, item_id=receipt.item_id,
                                                   source_revision=receipt.revision)
                    else:
                        await self._orchestrator.submit_runtime_event(
                            session_id, message, thread_id=thread_id, source_id=source_id, idempotency_key=run.run_id,
                            delivery_policy=action.delivery_policy,
                            context_mode=context_mode,
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
            if context_mode == "state" and delivery.state == "accepted":
                receipt = self._dispatcher.context_published(
                    session_id, thread_id, message, source_id=source_id, idempotency_key=run.run_id, context_mode="state",
                )
                if receipt is None:
                    raise RuntimeError("状态执行接受后缺少同事务的canonical状态发布")
                delivery = delivery.model_copy(update={"item_id": receipt.item_id, "source_revision": receipt.revision})
        await observe_phase(run.run_id, "task.action.accepted_before_record", details={
            "task_id": run.task_id, "delivery_state": delivery.state, "item_id": delivery.item_id,
            "job_id": delivery.job_id, "execution_id": delivery.execution_id,
        })
        async with self.locks.lock_for(run.task_id):
            self.store.advance(run, phase="delivered", now=self.clock(), delivery=delivery,
                               lifecycle="complete" if configuration.policies.completion == "after_delivery" else None)
            self.notify(run.task_id, run.run_id)

    async def _check(self, run: PersistentTaskRunDTO,
                     configuration: PersistentTaskConfigurationDTO) -> PersistentTaskRunDTO | None:
        launching = run.phase == "pending"
        async with self.locks.lock_for(run.task_id):
            task = self.store.get(run.task_id)
            if run.phase == "checking" and configuration.condition.type_id == "builtin.script":
                self.store.advance(run, phase="failed", now=self.clock(),
                                   error="脚本检查中断，结果未知；需要人工确认，不自动重跑",
                                   lifecycle=None if run.check_only else "needs_attention")
                self.notify(run.task_id, run.run_id)
                return None
            # 模型checking先对账原准入；暂停/编辑不丢掉已接受判断的结果。
            reconciling = run.phase == "checking" and configuration.condition.type_id == "builtin.model"
            if not reconciling and (task.revision != run.task_revision or (task.lifecycle != "active" and not run.check_only)):
                self.store.advance(run, phase="suppressed", now=self.clock(), error="任务 revision 或生命周期已变化，未执行判断")
                self.notify(run.task_id, run.run_id)
                return None
            self.store.advance(run, phase="checking", now=self.clock())
            run = run.model_copy(update={"phase": "checking"})
            self.notify(run.task_id, run.run_id)
        # 慢判断不占管理锁；迟到结果必须在下面重新核对revision。
        if launching or configuration.condition.type_id != "builtin.model":
            await observe_phase(run.run_id, "task.check.started_before_launch", details={"task_id": run.task_id})
        try:
            if configuration.condition.type_id == "builtin.model":
                decision = await self._check_model(run, configuration)
                if decision is None:
                    return None
            else:
                decision = await evaluate_condition(configuration, run, workspace=self._workspace_root, workspace_id=self.store.workspace_id)
        except TaskCheckError as error:
            self._waiting_checks.discard(run.task_id)
            async with self.locks.lock_for(run.task_id):
                self.store.advance(run, phase="failed", now=self.clock(), error=str(error),
                                   lifecycle=None if run.check_only else "needs_attention")
                self.notify(run.task_id, run.run_id)
            return None
        async with self.locks.lock_for(run.task_id):
            run = self.store.finish_check(run, decision, self.clock())
            self.notify(run.task_id, run.run_id)
        if run.phase != "delivering":
            return None
        await observe_phase(run.run_id, "task.check.decided_before_delivery", details={"task_id": run.task_id})
        return run

    async def _check_model(self, run: PersistentTaskRunDTO,
                           configuration: PersistentTaskConfigurationDTO) -> TaskDecisionDTO | None:
        model = configuration.condition.model
        assert model is not None
        resource, session_id, thread_id = parse_session_thread_uri(model.thread_uri)
        assert thread_id is not None
        key = f"persistent-task-check:{run.run_id}"
        async with self.locks.lock_for(run.task_id):
            try:
                async with self._sessions.session_read_admission(session_id):
                    self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
                    wakeup = self._dispatcher.submitted(session_id, thread_id, idempotency_key=key)
            except NotFoundError as error:
                raise TaskCheckError(f"判断Thread已删除: {error}") from error
            if wakeup is None:
                if run.evaluation is not None:
                    raise RuntimeError("已记录判断的原准入事实缺失")
                task = self.store.get(run.task_id)
                if task.revision != run.task_revision or (task.lifecycle != "active" and not run.check_only):
                    self.store.advance(run, phase="suppressed", now=self.clock(), error="任务已变化，未接受模型判断")
                    self.notify(run.task_id, run.run_id)
                    return None
                message = internal_message_factory.build(
                    kind="persistent_task_check",
                    control="评估本次冻结输入并只返回一个JSON对象，不直接改变持久任务或投递工作。"
                            "原样回显run_id和definition_revision；decision为wait/proceed/complete/needs_attention之一，"
                            "reason为非空理由，result为JSON对象。历史回复不能代替本次结论。",
                    sections=(PromptSection("control_context", {"run_id": run.run_id, "definition_revision": run.definition_revision}),
                              PromptSection("untrusted_instructions", json.dumps({"condition": model.instruction,
                                  "input": condition_input(configuration, run, workspace_id=self.store.workspace_id)}, ensure_ascii=False))),
                    metadata={"source": "persistent_task_check"},
                )
                gateway_token = set_current_gateway_id(resource.gateway_authority)
                request_token = set_current_request_id(run.request_id)
                try:
                    await self._orchestrator.submit_runtime_event(
                        session_id, message, thread_id=thread_id, source_id=f"persistent-task-check:{run.task_id}",
                        idempotency_key=key, delivery_policy="after_turn",
                    )
                except (NotFoundError, ValueError) as error:
                    raise TaskCheckError(f"判断准入失败: {type(error).__name__}: {error}") from error
                finally:
                    reset_current_gateway_id(gateway_token)
                    reset_current_request_id(request_token)
                wakeup = self._dispatcher.submitted(session_id, thread_id, idempotency_key=key)
                if wakeup is None:
                    raise RuntimeError("模型判断接受后缺少原durable wakeup")
        await observe_phase(run.run_id, "task.check.model_accepted_before_record", details={
            "task_id": run.task_id, "job_id": wakeup.job_id, "execution_id": wakeup.execution_id, "item_id": wakeup.source_item_id,
        })
        self.store.record_evaluation(run, TaskEvaluationDTO(thread_uri=model.thread_uri, source_item_id=wakeup.source_item_id,
                                     job_id=wakeup.job_id, execution_id=wakeup.execution_id), self.clock())
        self.notify(run.task_id, run.run_id)
        # 先登记等待，再读终态，避免读后才登记而吞掉并发完成通知；不占worker槽等待模型。
        self._waiting_checks.add(run.task_id)
        try:
            async with self._sessions.session_read_admission(session_id):
                self._sessions.path_resolver.resolve_thread_node(session_id, thread_id)
                outcome = self._saver.runtime_execution_outcome(session_id, thread_id, wakeup.execution_id)
                if outcome == "unknown":
                    return None
                self._waiting_checks.discard(run.task_id)
                if outcome != "completed":
                    raise TaskCheckError(f"模型判断执行未成功: {outcome}")
                content, _provenance = self._saver.execution_output(session_id, thread_id, wakeup.execution_id, final_only=True)
        except NotFoundError as error:
            raise TaskCheckError(f"判断Thread已删除: {error}") from error
        return model_decision(content, run)
