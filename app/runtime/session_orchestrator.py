from __future__ import annotations

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.session_lifecycle_gate import OperationHolderGate
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_current_gateway_id, get_current_request_id
from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.prompting.validation import validate_internal_message
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import (
    MessageCreateRequest,
    MessageDTO,
    MessageRunAccepted,
    MessageRunRequest,
)
from app.schemas.internal_v2.pending_request import DeliveryPolicy
from app.services.business.message_display import project_message_for_display
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.session_message_idempotency_store import (
    SessionMessageIdempotencyStore,
)
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
        idempotency_store: SessionMessageIdempotencyStore | None = None,
        runtime_source_dispatcher: RuntimeSourceDispatcher | None = None,
    ) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._config_service = config_service
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._attachment_holder = attachment_holder
        self._idempotency_store = idempotency_store
        self._runtime_source_dispatcher = runtime_source_dispatcher

    async def resolve_main_thread(self, session_id: str) -> str:
        return await self._session_service.resolve_main_thread(session_id)

    async def submit_runtime_event(
        self, session_id: str, message: PreparedInternalMessage, *,
        thread_id: str, source_id: str, idempotency_key: str,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> MessageRunAccepted:
        if self._runtime_source_dispatcher is None:
            raise RuntimeError("runtime source dispatcher unavailable")
        return await self._runtime_source_dispatcher.submit(
            session_id, thread_id, message, source_id=source_id,
            idempotency_key=idempotency_key, delivery_policy=delivery_policy,
        )

    def reserve_user_execution_identity(self) -> UserExecutionIdentity:
        return self._job_service.reserve_user_execution_identity()

    def reserve_internal_job_id(self) -> str:
        return self._job_service.reserve_internal_job_id()

    async def create_and_run(
        self,
        session_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        delivery_policy: DeliveryPolicy = "after_turn",
        idempotency_key: str | None = None,
    ) -> MessageRunAccepted:
        self._reject_internal_metadata_on_user_path(metadata)
        session = await self._session_service.get(session_id)
        message_request = MessageCreateRequest(
            role=MessageRole.user,
            content=content,
            metadata=metadata or {},
        )
        return await self._create_and_dispatch(
            session_id,
            message_request,
            requested_agent_id=session.current_agent_id,
            delivery_policy=delivery_policy,
            idempotency_key=idempotency_key,
            internal=False,
        )

    async def create_and_run_internal(
        self,
        session_id: str,
        message: PreparedInternalMessage,
        *,
        delivery_policy: DeliveryPolicy = "after_turn",
        idempotency_key: str | None = None,
        thread_id: str | None = None,
    ) -> MessageRunAccepted:
        validate_internal_message(message.content, message.metadata)
        session = await self._session_service.get(session_id)
        return await self._create_and_dispatch(
            session_id,
            MessageCreateRequest(
                role=MessageRole.user,
                content=message.content,
                metadata=dict(message.metadata),
            ),
            requested_agent_id=session.current_agent_id,
            delivery_policy=delivery_policy,
            idempotency_key=idempotency_key,
            internal=True,
            thread_id=thread_id,
        )

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

    async def prepare_internal_message(
        self,
        session_id: str,
        message: PreparedInternalMessage,
    ) -> MessageDTO:
        """生成稳定的内部结构消息 DTO；调用方必须先写账本再派发。"""
        validate_internal_message(message.content, message.metadata)
        return await self._job_service.run_session_preparation(
            session_id,
            lambda: self._message_service.create(
                session_id,
                MessageCreateRequest(
                    role=MessageRole.user,
                    content=message.content,
                    metadata=dict(message.metadata),
                ),
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
                "内部结构消息必须通过 create_and_run_internal 或 "
                "prepare_internal_message 注入"
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
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[session_orchestrator] create_message begin: session_id=%s", session_id)
        requested_agent_id = payload.run.agent_id if payload.run else None
        delivery_policy: DeliveryPolicy = payload.run.delivery_policy
        self._reject_internal_metadata_on_user_path(payload.message.metadata)
        return await self._create_and_dispatch(
            session_id,
            payload.message,
            requested_agent_id=requested_agent_id,
            delivery_policy=delivery_policy,
            internal=False,
            thread_id=thread_id,
        )

    async def _create_and_dispatch(
        self,
        session_id: str,
        message_request: MessageCreateRequest,
        *,
        requested_agent_id: str | None,
        delivery_policy: DeliveryPolicy,
        idempotency_key: str | None = None,
        internal: bool,
        thread_id: str | None = None,
    ) -> MessageRunAccepted:
        async def prepare_and_dispatch() -> MessageRunAccepted:
            # 上传和 canonical acceptance 共用一个准备窗口，删除须先排空它。
            async with self._attachment_holder.lock_for(session_id, exclusive=False):
                if idempotency_key is not None and self._idempotency_store is not None:
                    existing = self._idempotency_store.get(session_id, idempotency_key)
                    if existing is not None:
                        return existing
                target_thread_id = thread_id if thread_id is not None else await self._session_service.resolve_main_thread(session_id)
                self._session_service.path_resolver.resolve_thread_node(session_id, target_thread_id)
                message = await self._message_service.create(session_id, message_request, thread_id=target_thread_id)
                execution_binding: ExecutionBinding | None = None
                if internal:
                    execution_binding = InternalExecutionIntent(
                        message_id=message.message_id,
                        execution_id=create_prefixed_id("intr"),
                        admission_idempotency_key=create_prefixed_id("adm"),
                    )
                result = await self.dispatch_prepared_message(
                    session_id,
                    message,
                    requested_agent_id=requested_agent_id,
                    delivery_policy=delivery_policy,
                    execution_binding=execution_binding,
                    thread_id=target_thread_id,
                )
                if idempotency_key is not None and self._idempotency_store is not None:
                    self._idempotency_store.put(session_id, idempotency_key, result)
                return result

        return await self._job_service.run_session_preparation(
            session_id,
            prepare_and_dispatch,
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
    ) -> MessageRunAccepted:
        """调度一条已生成稳定 message_id 的用户消息。"""
        import logging
        logger = logging.getLogger(__name__)
        async with self._attachment_holder.lock_for(session_id, exclusive=False):
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
            effective_agent_id = self._config_service.resolve_agent_id(requested_agent_id)
            try:
                self._config_service.validate_agent_id(effective_agent_id)
            except ValueError:
                effective_agent_id = self._config_service.get_default_agent_id()

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
                await run_sync_shielded(
                    self._message_service.accept_user_message,
                    message,
                    execution_identity=execution_binding,
                    queue_input=ExecutionQueueInput(
                        execution_binding=execution_binding, message_id=message.message_id,
                        agent_id=effective_agent_id, provider_id=session.current_provider_id,
                        delivery_policy=delivery_policy, gateway_id=get_current_gateway_id(),
                        request_id=get_current_request_id(), created_at=message.created_at.isoformat(),
                    ),
                )
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
                message_id=message.message_id,
                job_id=job_id,
                turn_id=(execution_binding.turn_id if isinstance(execution_binding, UserExecutionIdentity) else None),
                status=dispatch.job_status,
                dispatch=dispatch,
                thread_id=thread_id,
            )
