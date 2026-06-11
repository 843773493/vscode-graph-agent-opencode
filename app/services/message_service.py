from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.schemas.public_v2.message import MessageDTO, MessageCreateRequest, MessageRunRequest, MessageRunAccepted
from app.schemas.public_v2.common import MessageRole, CursorPage
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
        now = datetime.now()
        message = MessageDTO(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=MessageRole.assistant,
            content=content,
            attachments=[],
            metadata=metadata or {},
            created_at=now,
            updated_at=now,
        )
        return self._append_message(message)
    
    async def list(self, session_id: str, limit: int = 50, cursor: str | None = None) -> CursorPage[MessageDTO]:
        import logging

        logger = logging.getLogger(__name__)
        try:
            items = self._read_messages(session_id)
            return CursorPage(items=items[:limit], next_cursor=None, has_more=len(items) > limit)
        except Exception as e:
            logger.exception("读取消息列表失败: session_id=%s error=%s", session_id, e)
            raise

    async def get(self, session_id: str, message_id: str) -> MessageDTO:
        for message in self._read_messages(session_id):
            if message.message_id == message_id:
                return message

        raise ValueError(f"Message {message_id} not found in session {session_id}")

    async def create(self, session_id: str, message_create: MessageCreateRequest) -> MessageDTO:
        now = datetime.now()
        message = MessageDTO(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            attachments=message_create.attachments,
            metadata=message_create.metadata,
            created_at=now,
            updated_at=now,
        )
        return self._append_message(message)
