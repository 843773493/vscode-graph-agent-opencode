from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.services.config_service import ConfigService
from app.services.job_service import JobService
from app.services.session_service import SessionService
from app.core.job_event_bus import EventType, JobEventBus
from app.schemas.message import MessageDTO, MessageCreateRequest, MessageRunRequest, MessageRunAccepted
from app.schemas.job import JobStatus
from app.schemas.common import MessageRole, CursorPage
from app.core.path_utils import get_session_path


class MessageService:
    def __init__(self):
        pass

    def _message_file(self, session_id: str) -> Path:
        return get_session_path(session_id) / "messages.jsonl"

    def _read_messages(self, session_id: str) -> list[MessageDTO]:
        message_file = self._message_file(session_id)
        if not message_file.exists():
            return []

        messages: list[MessageDTO] = []
        with open(message_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                data = json.loads(line)
                messages.append(MessageDTO.model_validate(data))

        messages.sort(key=lambda item: item.created_at)
        return messages

    def _append_message(self, message: MessageDTO) -> MessageDTO:
        message_file = self._message_file(message.session_id)
        message_file.parent.mkdir(exist_ok=True, parents=True)

        with open(message_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.model_dump(mode="json"), ensure_ascii=False) + "\n")

        return message

    async def append_assistant_message(self, session_id: str, content: str, metadata: dict | None = None) -> MessageDTO:
        message = MessageDTO(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=MessageRole.assistant,
            content=content,
            attachments=[],
            metadata=metadata or {},
            created_at=datetime.now(),
        )
        return self._append_message(message)
    
    async def list(self, session_id: str, limit: int = 50, cursor: str | None = None) -> CursorPage[MessageDTO]:
        items = self._read_messages(session_id)
        return CursorPage(items=items[:limit], next_cursor=None, has_more=len(items) > limit)

    async def get(self, session_id: str, message_id: str) -> MessageDTO:
        for message in self._read_messages(session_id):
            if message.message_id == message_id:
                return message

        raise ValueError(f"Message {message_id} not found in session {session_id}")

    async def create(self, session_id: str, message_create: MessageCreateRequest) -> MessageDTO:
        message = MessageDTO(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            attachments=message_create.attachments,
            metadata=message_create.metadata,
            created_at=datetime.now(),
        )
        return self._append_message(message)

    async def create_and_run(
        self,
        session_id: str,
        run_request: MessageRunRequest,
        *,
        session_service: SessionService,
        config_service: ConfigService,
        job_service: JobService,
        job_event_bus: JobEventBus,
    ) -> MessageRunAccepted:
        """
        创建消息并启动异步Job执行
        供路由层调用的实例方法
        """
        message = await self.create(session_id, run_request.message)

        # 消息创建事件：此时 job_id 还未生成，使用 session_id 作为关联ID
        await job_event_bus.publish(
            session_id,  # ⚠️ 暂时使用 session_id，因为 job_id 尚未创建
            EventType.MESSAGE_CREATED,
            {
                "message_id": message.message_id,
                "session_id": message.session_id,
                "role": message.role,
                "content": message.content,
                "attachments": [a.model_dump() for a in message.attachments],
                "metadata": message.metadata,
                "created_at": message.created_at,  # datetime对象，Pydantic会处理
            }
        )

        session = await session_service.get(session_id)

        requested_agent_id = run_request.run.agent_id if run_request.run else None
        if requested_agent_id is None:
            requested_agent_id = session.current_agent_id
        effective_agent_id = config_service.resolve_agent_id(requested_agent_id)

        try:
            config_service.validate_agent_id(effective_agent_id)
        except ValueError:
            effective_agent_id = config_service.get_default_agent_id()

        job_id = await job_service.start_job(session_id, message.content, effective_agent_id)

        return MessageRunAccepted(
            message_id=message.message_id,
            job_id=job_id,
            status=JobStatus.accepted,
        )
