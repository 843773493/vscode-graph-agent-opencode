"""同 Session initial intent 的真实 admission/bind/dispatch 编排。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import replace

from app.abstractions.job_service import JobServiceProtocol
from app.core.job_event_bus import EventType
from app.core.key_lock_pool import KeyLockPool
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.prompting import PromptSection, internal_message_factory
from app.schemas.event import Event
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.itemized.thread_seed import (
    FrozenThreadSeed,
    frozen_thread_seed,
)
from app.services.orchestration.initial_execution_binding_worker import (
    InitialExecutionBinder,
    InitialExecutionBindingTarget,
    InitialExecutionBindingWorker,
    InitialExecutionBindOutcome,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

logger = logging.getLogger(__name__)


class ThreadInitialExecutionService:
    def __init__(self, *, factory: OwnerThreadCreationFactory, sessions: SessionService,
                 saver: RolloutCheckpointSaver, jobs: JobServiceProtocol,
                 dispatcher: RuntimeSourceDispatcher) -> None:
        self._factory = factory
        self._sessions = sessions
        self._saver = saver
        self._jobs = jobs
        self._dispatcher = dispatcher
        self._locks = KeyLockPool()
        self._tasks: set[asyncio.Task[None]] = set()
        self._error: BaseException | None = None
        self._closed = False

    @property
    def binder(self) -> InitialExecutionBinder:
        return self._bind

    def _binding(self, target: InitialExecutionBindingTarget) -> tuple[InternalExecutionIntent, FrozenThreadSeed]:
        intent = target.intent
        control = self._factory.for_owner_session(intent.session_id).control_store
        record = control.get_thread_creation_record(intent.creation_idempotency_key)
        frozen = frozen_thread_seed(record)
        binding = InternalExecutionIntent(
            message_id=f"seed-{intent.execution_binding_id}", execution_id=intent.execution_binding_id,
            admission_idempotency_key=intent.admission_idempotency_key,
            input_reference_json=json.dumps(frozen.reference, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )
        return binding, frozen

    def _bind(self, target: InitialExecutionBindingTarget) -> InitialExecutionBindOutcome:
        binding, _frozen = self._binding(target)
        self._saver.admit_internal_execution(
            target.intent.session_id, thread_id=target.intent.thread_id,
            job_id=target.job_id, intent=binding, message=None,
        )
        return InitialExecutionBindOutcome(execution_binding_id=binding.execution_id, job_id=target.job_id)

    async def bind_owner(self, session_id: str) -> None:
        async with self._locks.lock_for(session_id):
            async with self._sessions.session_write_admission(session_id):
                control = self._factory.for_owner_session(session_id).control_store
                worker = InitialExecutionBindingWorker(store=control, claim_owner="initial-execution", binder=self.binder)
                worker.start()
                try:
                    worker.bind_pending_once()
                finally:
                    worker.stop()
                intents = control.list_initial_execution_intents()
            for intent in intents:
                if intent.initial_state == "idle":
                    continue
                if intent.state != "bound":
                    raise RuntimeError(f"child initial execution 未绑定: {intent.admission_idempotency_key}")
                target = InitialExecutionBindingTarget(intent=intent, execution_binding_id=intent.execution_binding_id, job_id=intent.job_id)
                outcome = self._saver.runtime_execution_outcome(session_id, intent.thread_id, intent.execution_binding_id)
                if outcome != "unknown":
                    await self._report(intent.session_id, intent.job_id)
                    continue
                binding, frozen = self._binding(target)
                record = control.get_thread_creation_record(intent.creation_idempotency_key)
                frozen_agent_id = json.loads(record.task_seed)["agent_id"]
                gateway_token = set_current_gateway_id(frozen.gateway_id)
                try:
                    await self._jobs.start_job(
                        session_id, frozen.message.content, thread_id=intent.thread_id,
                        execution_binding=binding, reserved_job_id=intent.job_id,
                        message_id=binding.message_id, message_created_at=intent.intent_created_at,
                        message_metadata=dict(frozen.message.metadata), agent_id=frozen_agent_id,
                    )
                finally:
                    reset_current_gateway_id(gateway_token)

    async def start(self) -> None:
        _revision, nodes = self._sessions.path_resolver.catalog_store.read_nodes_snapshot()
        for node in nodes:
            if node.kind == "session" and node.state == "active":
                await self.bind_owner(node.node_id)

    async def on_event(self, event: Event) -> None:
        if event.type not in {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED} or self._closed:
            return
        payload = event.payload.model_dump()
        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            self._spawn(self._report(session_id, event.job_id))

    def _spawn(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._error = task.exception()
            logger.error("child initial execution 回报失败", exc_info=(type(self._error), self._error, self._error.__traceback__))

    async def _report(self, session_id: str, job_id: str) -> None:
        control = self._factory.for_owner_session(session_id).control_store
        intent = next((item for item in control.list_initial_execution_intents() if item.job_id == job_id), None)
        if intent is None or intent.initial_state != "running":
            return
        record = control.get_thread_creation_record(intent.creation_idempotency_key)
        frozen = frozen_thread_seed(record)
        if record.task_reference is None:
            raise RuntimeError("delegated child 缺少冻结 parent thread address")
        parent = json.loads(record.task_reference)
        outcome = self._saver.runtime_execution_outcome(session_id, intent.thread_id, intent.execution_binding_id)
        if outcome == "unknown":
            return
        content, provenance = self._saver.execution_output(session_id, intent.thread_id, intent.execution_binding_id)
        message = internal_message_factory.build(
            kind="generated_session_result", control="这是当前会话内 child thread 的执行结果。按任务结果继续处理。",
            sections=(PromptSection("control_context", {"outcome": outcome}),
                      PromptSection("generated_session_result", content)),
            display_content="委派任务已结束，正在处理返回结果。",
            display_provenance=replace(provenance, delegation_id=record.delegation_id),
        )
        gateway_token = set_current_gateway_id(frozen.gateway_id)
        try:
            await self._dispatcher.submit(
                session_id, parent["parent_thread_id"], message,
                source_id=f"subagent:{record.delegation_id}:result",
                idempotency_key=f"subagent-result:{intent.admission_idempotency_key}",
            )
        finally:
            reset_current_gateway_id(gateway_token)

    async def stop(self) -> None:
        self._closed = True
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._error is not None:
            raise BaseExceptionGroup("child execution 回报失败", [self._error])
