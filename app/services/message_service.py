from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.schemas.message import MessageDTO, MessageCreate, MessageRunRequest, MessageRunAccepted
from app.schemas.common import MessageRole, JobStatus, CursorPage
from app.core.path_utils import get_session_path


class MessageService:

    _instance: Optional["MessageService"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self):
        pass

    @classmethod
    def get_instance(cls) -> "MessageService":
        return cls()

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

    async def create(self, session_id: str, message_create: MessageCreate) -> MessageDTO:
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

    async def run(self, session_id: str, run_request: MessageRunRequest) -> MessageRunAccepted:
        message = await self.create(session_id, run_request.message)
        return MessageRunAccepted(
            message_id=message.message_id,
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            status=JobStatus.accepted
        )
    
    async def create_and_run(self, session_id: str, run_request: MessageRunRequest) -> MessageRunAccepted:
        """
        创建消息并启动异步Job执行
        供路由层调用的实例方法
        """
        message = await self.create(session_id, run_request.message)

        # 无论前端HTTP调用还是后端内部调用，创建消息后统一广播事件到SSE流
        from app.core.job_event_bus import JobEventBus, EventType
        await JobEventBus.get_instance().publish(
            session_id,
            EventType.MESSAGE_CREATED,
            message.dict()
        )

        from app.services.session_service import SessionService
        session = await SessionService.get(session_id)
        
        from app.services.config_service import ConfigService
        config_service = ConfigService.get_instance()
        
        requested_agent_id = run_request.run.agent_id if run_request.run else None
        effective_agent_id = config_service.resolve_agent_id(requested_agent_id)
        
        try:
            config_service.validate_agent_id(effective_agent_id)
        except ValueError:
            effective_agent_id = config_service.get_default_agent_id()
        
        from app.services.job_service import JobService
        job_service = JobService.get_instance()
        job_id = await job_service.start_job(session_id, message.content, effective_agent_id)
        
        return MessageRunAccepted(
            message_id=message.message_id,
            job_id=job_id,
            status=JobStatus.accepted
        )
