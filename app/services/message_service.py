from __future__ import annotations
from datetime import datetime
from typing import Optional

from app.schemas.message import MessageDTO, MessageCreate, MessageRunRequest, MessageRunAccepted
from app.schemas.common import MessageRole, JobStatus


class MessageService:
    async def list(self, session_id: str) -> list[MessageDTO]:
        now = datetime.now()
        return [
            MessageDTO(
                message_id="msg_001",
                session_id=session_id,
                role=MessageRole.user,
                content="请帮我实现用户认证模块",
                attachments=[],
                metadata={"source": "user_input"},
                created_at=now
            ),
            MessageDTO(
                message_id="msg_002",
                session_id=session_id,
                role=MessageRole.assistant,
                content="好的，我将为您实现用户认证模块。首先我会创建用户模型、认证路由和JWT中间件。",
                attachments=[],
                metadata={"agent": "planner"},
                created_at=now
            ),
            MessageDTO(
                message_id="msg_003",
                session_id=session_id,
                role=MessageRole.assistant,
                content="已完成用户模型创建，正在创建认证路由...",
                attachments=[],
                metadata={"agent": "executor"},
                created_at=now
            )
        ]

    async def get(self, message_id: str) -> MessageDTO:
        return MessageDTO(
            message_id=message_id,
            session_id="ses_123",
            role=MessageRole.user,
            content="请帮我实现用户认证模块",
            attachments=[],
            metadata={"source": "user_input"},
            created_at=datetime.now()
        )

    async def create(self, session_id: str, message_create: MessageCreate) -> MessageDTO:
        return MessageDTO(
            message_id=f"msg_{datetime.now().timestamp()}",
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            attachments=message_create.attachments,
            metadata=message_create.metadata,
            created_at=datetime.now()
        )

    async def run(self, session_id: str, run_request: MessageRunRequest) -> MessageRunAccepted:
        message = await self.create(session_id, run_request.message)
        return MessageRunAccepted(
            message_id=message.message_id,
            job_id=f"job_{datetime.now().timestamp()}",
            status=JobStatus.accepted
        )
