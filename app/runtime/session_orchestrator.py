from __future__ import annotations

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.job_event_bus import EventType
from app.schemas.public_v2.common import MessageRole, RunMode
from app.schemas.public_v2.message import (
    MessageCreateRequest,
    MessageDTO,
    MessageRunAccepted,
    MessageRunRequest,
    RunOptions,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService


class SessionOrchestrator:
    def __init__(
        self,
        *,
        message_service: MessageService,
        session_service: SessionService,
        config_service: ConfigService,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
    ) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._config_service = config_service
        self._job_service = job_service
        self._job_event_bus = job_event_bus

    async def create_and_run(
        self,
        session_id: str,
        content: str,
        *,
        message_role: MessageRole = MessageRole.user,
        metadata: dict[str, object] | None = None,
    ) -> MessageRunAccepted:
        session = await self._session_service.get(session_id)
        run_request = MessageRunRequest(
            message=MessageCreateRequest(
                role=message_role,
                content=content,
                metadata=metadata or {},
            ),
            run=RunOptions(mode=RunMode.single_agent, agent_id=session.current_agent_id),
        )
        return await self.create_message(session_id, run_request)

    async def create_message(self, session_id: str, payload: MessageRunRequest) -> MessageRunAccepted:
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[session_orchestrator] create_message begin: session_id=%s", session_id)
        requested_agent_id = payload.run.agent_id if payload.run else None
        message = await self._message_service.create(session_id, payload.message)
        return await self.dispatch_prepared_message(
            session_id,
            message,
            requested_agent_id=requested_agent_id,
        )

    async def dispatch_prepared_message(
        self,
        session_id: str,
        message: MessageDTO,
        *,
        requested_agent_id: str | None,
    ) -> MessageRunAccepted:
        """调度一条已生成稳定 message_id 的用户消息。"""
        import logging
        logger = logging.getLogger(__name__)
        session = await self._session_service.get(session_id)
        if requested_agent_id is None:
            requested_agent_id = session.current_agent_id
        effective_agent_id = self._config_service.resolve_agent_id(requested_agent_id)
        try:
            self._config_service.validate_agent_id(effective_agent_id)
        except ValueError:
            effective_agent_id = self._config_service.get_default_agent_id()

        logger.info("[session_orchestrator] message created: session_id=%s message_id=%s", session_id, message.message_id)
        job_id = await self._job_service.start_job(
            session_id,
            message.content,
            agent_id=effective_agent_id,
            message_id=message.message_id,
            attachments=message.attachments,
            message_created_at=message.created_at.isoformat(),
            message_role=message.role,
            message_metadata=message.metadata,
        )
        logger.info("[session_orchestrator] start_job returned: session_id=%s job_id=%s", session_id, job_id)
        await self._job_event_bus.publish(
            job_id=job_id,
            event_type=EventType.MESSAGE_CREATED,
            payload={
                "message_id": message.message_id,
                "session_id": message.session_id,
                "role": message.role,
                "content": message.content,
                "attachments": [
                    a.model_dump(mode="json", exclude={"data_url"})
                    for a in message.attachments
                ],
                "metadata": message.metadata,
                "created_at": message.created_at,
            },
            agent_id=effective_agent_id,
        )
        logger.info("[session_orchestrator] message_created event published: session_id=%s message_id=%s job_id=%s", session_id, message.message_id, job_id)
        return MessageRunAccepted(message_id=message.message_id, job_id=job_id, status="accepted")
