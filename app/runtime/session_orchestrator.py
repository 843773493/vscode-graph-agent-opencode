from __future__ import annotations

from dataclasses import replace

from app.abstractions.internal_message import (
    PreparedInternalMessage,
    RuntimeContextMode,
    RuntimeContextReceipt,
)
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.job_event_bus import EventType
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueueRecord,
)
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageDTO,
    MessageRunAccepted,
    MessageRunRequest,
    MessageSubmissionResultDTO,
)
from app.schemas.internal_v2.pending_request import DeliveryPolicy
from app.services.business.message_display import project_message_for_display
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

_RESERVED_INTERNAL_METADATA_KEYS = frozenset(
    {
        "internal",
        "structured_prompt_kind",
        "structured_prompt_schema_version",
        "internal_display_kind",
    }
)


class SessionOrchestrator:
    def __init__(
        self,
        *,
        message_service: MessageService,
        session_service: SessionService,
        config_service: ConfigService,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
        attachment_holder: OperationHolderGate,
        runtime_source_dispatcher: RuntimeSourceDispatcher | None = None,
    ) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._config_service = config_service
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._attachment_holder = attachment_holder
        self._runtime_source_dispatcher = runtime_source_dispatcher

    async def resolve_main_thread(self, session_id: str) -> str:
        return await self._session_service.resolve_main_thread(session_id)

    async def publish_runtime_context(
        self, session_id: str, message: PreparedInternalMessage, *,
        thread_id: str, source_id: str, idempotency_key: str,
        context_mode: RuntimeContextMode = "event",
    ) -> RuntimeContextReceipt:
        if self._runtime_source_dispatcher is None:
            raise RuntimeError("runtime source dispatcher unavailable")
        return await self._runtime_source_dispatcher.publish_context(
            session_id, thread_id, message, source_id=source_id,
            idempotency_key=idempotency_key, context_mode=context_mode,
        )

    async def submit_runtime_event(
        self, session_id: str, message: PreparedInternalMessage, *,
        thread_id: str, source_id: str, idempotency_key: str,
        delivery_policy: DeliveryPolicy = "after_turn",
        context_mode: RuntimeContextMode = "event",
    ) -> MessageRunAccepted:
        if self._runtime_source_dispatcher is None:
            raise RuntimeError("runtime source dispatcher unavailable")
        return await self._runtime_source_dispatcher.submit(
            session_id, thread_id, message, source_id=source_id,
            idempotency_key=idempotency_key, delivery_policy=delivery_policy, context_mode=context_mode,
        )

    def reserve_user_execution_identity(self) -> UserExecutionIdentity:
        return self._job_service.reserve_user_execution_identity()

    async def prepare_user_message(
        self,
        session_id: str,
        content: str,
        *,
        thread_id: str,
        metadata: dict[str, object] | None = None,
    ) -> MessageDTO:
        """生成稳定消息 DTO；调用方必须先写入业务账本再派发。"""
        self._reject_internal_metadata_on_user_path(metadata)
        self._session_service.path_resolver.resolve_thread_node(session_id, thread_id)
        return await self._job_service.run_session_preparation(
            session_id,
            lambda: self._message_service.create(
                session_id,
                MessageCreateRequest(
                    role=MessageRole.user,
                    content=content,
                    metadata=metadata or {},
                ),
                thread_id=thread_id,
            ),
        )

    @staticmethod
    def _reject_internal_metadata_on_user_path(
        metadata: dict[str, object] | None,
    ) -> None:
        if metadata is not None and not _RESERVED_INTERNAL_METADATA_KEYS.isdisjoint(
            metadata
        ):
            raise ValueError(
                "普通用户消息不得携带保留的内部结构 metadata"
            )

    async def dispatch_existing_message(
        self,
        session_id: str,
        message: MessageDTO,
        *,
        execution_binding: ExecutionBinding,
        reserved_job_id: str | None = None,
    ) -> MessageRunAccepted:
        """派发已经持久化的消息；重启恢复时保持同一个 message_id。"""
        async def dispatch() -> MessageRunAccepted:
            session = await self._session_service.get(session_id)
            return await self.dispatch_prepared_message(
                session_id,
                message,
                requested_agent_id=session.current_agent_id,
                execution_binding=execution_binding,
                reserved_job_id=reserved_job_id,
            )

        return await self._job_service.run_session_preparation(
            session_id,
            dispatch,
        )

    async def create_message(self, session_id: str, payload: MessageRunRequest, *, thread_id: str | None = None) -> MessageRunAccepted:
        self._reject_internal_metadata_on_user_path(payload.message.metadata)
        submission_hash = sha256_jcs(payload.model_dump(mode="json", by_alias=True))

        async def submit() -> MessageRunAccepted:
            target = thread_id if thread_id is not None else await self.resolve_main_thread(session_id)
            self._session_service.path_resolver.resolve_thread_node(session_id, target)
            async with (self._job_service.thread_input_admission(session_id, target),
                        self._attachment_holder.lock_for(session_id, exclusive=False)):
                record = await run_sync_shielded(
                    self._message_service.message_submission, session_id, target, payload.operation_id,
                    submission_hash=submission_hash,
                )
                if record is not None:
                    return await self._accepted_submission(record)
                message = await self._message_service.create(session_id, payload.message, thread_id=target)
                identity = replace(self.reserve_user_execution_identity(), admission_idempotency_key=payload.operation_id)
                return await self.dispatch_prepared_message(
                    session_id, message, requested_agent_id=payload.run.agent_id,
                    delivery_policy=payload.run.delivery_policy, execution_binding=identity,
                    thread_id=target, submission_hash=submission_hash,
                )

        return await self._job_service.run_session_preparation(session_id, submit)

    async def _accepted_submission(self, record: ExecutionQueueRecord) -> MessageRunAccepted:
        identity = record.input.execution_binding
        if not isinstance(identity, UserExecutionIdentity):
            raise TypeError("普通消息操作不能引用内部执行")
        dispatch = await self._job_service.submission_dispatch(record)
        return MessageRunAccepted(
            operation_id=identity.admission_idempotency_key,
            message_id=record.input.message_id, job_id=identity.job_id, turn_id=identity.turn_id,
            thread_id=record.cursor.admission.thread_id, status=dispatch.job_status, dispatch=dispatch,
        )

    async def message_submission(self, session_id: str, thread_id: str, operation_id: str) -> MessageSubmissionResultDTO:
        async with self._session_service.session_read_admission(session_id):
            self._session_service.path_resolver.resolve_thread_node(session_id, thread_id)
            record = await run_sync_shielded(self._message_service.message_submission, session_id, thread_id, operation_id)
            accepted = None if record is None else await self._accepted_submission(record)
            return MessageSubmissionResultDTO(
                session_id=session_id, thread_id=thread_id, operation_id=operation_id,
                status="unknown" if accepted is None else "accepted", accepted=accepted,
            )

    async def dispatch_prepared_message(
        self,
        session_id: str,
        message: MessageDTO,
        *,
        requested_agent_id: str | None,
        delivery_policy: DeliveryPolicy = "after_turn",
        execution_binding: ExecutionBinding | None = None,
        reserved_job_id: str | None = None,
        thread_id: str | None = None,
        submission_hash: str | None = None,
    ) -> MessageRunAccepted:
        """调度一条已生成稳定 message_id 的用户消息。"""
        import logging
        logger = logging.getLogger(__name__)
        async with (
            self._attachment_holder.lock_for(session_id, exclusive=False),
            self._job_service.thread_input_admission(session_id, message.thread_id),
        ):
            session = await self._session_service.get(session_id)
            if message.session_id != session_id:
                raise ValueError(
                    f"消息派发 Session 与持久 owner 不一致: requested={session_id} "
                    f"message={message.session_id} message_id={message.message_id}"
                )
            if thread_id is not None and thread_id != message.thread_id:
                raise ValueError(
                    f"消息派发 Thread 与持久 owner 不一致: requested={thread_id} "
                    f"message={message.thread_id} message_id={message.message_id}"
                )
            # prepare 阶段已冻结消息 owner；恢复和生成器派发只消费该身份。
            thread_id = message.thread_id
            self._session_service.path_resolver.resolve_thread_node(session_id, thread_id)
            if requested_agent_id is None:
                requested_agent_id = session.current_agent_id
            effective_agent_id = self._config_service.validate_agent_id(requested_agent_id)

            logger.info("[session_orchestrator] message created: session_id=%s message_id=%s", session_id, message.message_id)
            job_message_metadata = {
                **message.metadata,
                "boxteam_session_provider_id": session.current_provider_id,
            }
            if execution_binding is None:
                if reserved_job_id is not None:
                    raise ValueError("未预留 execution binding 时不得单独预留 Job ID")
                execution_binding = self._job_service.reserve_user_execution_identity()
            if isinstance(execution_binding, UserExecutionIdentity):
                record = await run_sync_shielded(
                    self._message_service.accept_user_message,
                    message,
                    execution_identity=execution_binding,
                    queue_input=ExecutionQueueInput(
                        execution_binding=execution_binding, message_id=message.message_id,
                        agent_id=effective_agent_id, provider_id=session.current_provider_id,
                        delivery_policy=delivery_policy, gateway_id=get_current_gateway_id(),
                        request_id=get_current_request_id(), created_at=message.created_at.isoformat(),
                        submission_hash=submission_hash,
                    ),
                )
                if submission_hash is not None and record.input.message_id != message.message_id:
                    return await self._accepted_submission(record)
            dispatch = await self._job_service.start_job(
                session_id,
                message.content,
                thread_id=thread_id,
                execution_binding=execution_binding,
                reserved_job_id=reserved_job_id,
                agent_id=effective_agent_id,
                message_id=message.message_id,
                attachments=message.attachments,
                message_created_at=message.created_at.isoformat(),
                message_metadata=job_message_metadata,
                delivery_policy=delivery_policy,
            )
            job_id = dispatch.job_id
            logger.info("[session_orchestrator] start_job returned: session_id=%s job_id=%s", session_id, job_id)
            display_projection = project_message_for_display(
                message.content,
                message.metadata,
            )
            await self._job_event_bus.publish(
                job_id=job_id,
                event_type=EventType.MESSAGE_CREATED,
                payload={
                    "message_id": message.message_id,
                    "session_id": message.session_id,
                    "thread_id": thread_id,
                    "role": message.role,
                    "content": display_projection.content,
                    "attachments": [
                        a.model_dump(mode="json", exclude={"data_url"})
                        for a in message.attachments
                    ],
                    "metadata": display_projection.metadata,
                    "created_at": message.created_at,
                },
                agent_id=effective_agent_id,
            )
            logger.info("[session_orchestrator] message_created event published: session_id=%s message_id=%s job_id=%s", session_id, message.message_id, job_id)
            return MessageRunAccepted(
                operation_id=execution_binding.admission_idempotency_key if submission_hash is not None else None,
                message_id=message.message_id,
                job_id=job_id,
                turn_id=(execution_binding.turn_id if isinstance(execution_binding, UserExecutionIdentity) else None),
                status=dispatch.job_status,
                dispatch=dispatch,
                thread_id=thread_id,
            )
