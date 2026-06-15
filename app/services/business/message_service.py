"""MessageService：从 LangGraph checkpoint 读取会话历史。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.schemas.public_v2.common import CursorPage, MessageRole
from app.schemas.public_v2.message import MessageCreateRequest, MessageDTO


class MessageService:
    def __init__(self, checkpointer: BaseCheckpointSaver | None = None) -> None:
        self._checkpointer = checkpointer

    def _checkpoint_config(self, session_id: str) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": session_id,
                "checkpoint_ns": "",
            }
        }

    @staticmethod
    def _message_to_dto(
        session_id: str,
        index: int,
        message: BaseMessage,
    ) -> MessageDTO:
        role = MessageService._detect_role(message)
        content = MessageService._extract_content(message)
        response_metadata = message.response_metadata or {}
        message_id = response_metadata.get("message_id") or f"msg_{index:06d}"
        return MessageDTO(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            attachments=[],
            metadata={
                "langchain_type": message.type,
                "tool_calls": getattr(message, "tool_calls", None) or [],
                "tool_call_id": getattr(message, "tool_call_id", None),
                **response_metadata,
            },
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    @staticmethod
    def _detect_role(message: BaseMessage) -> MessageRole:
        if isinstance(message, HumanMessage):
            return MessageRole.user
        if isinstance(message, AIMessage):
            return MessageRole.assistant
        if isinstance(message, ToolMessage):
            return MessageRole.tool
        return MessageRole.system

    @staticmethod
    def _extract_content(message: BaseMessage) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    part_type = part.get("type")
                    if part_type == "text":
                        text = part.get("text", "")
                        if isinstance(text, str):
                            text_parts.append(text)
                    elif part_type == "function_call":
                        name = part.get("name", "unknown_tool")
                        args = part.get("arguments", "")
                        text_parts.append(f"[调用工具 {name}，参数：{args}]")
                else:
                    text_parts.append(str(part))
            return "".join(text_parts)
        return str(content)

    async def list(
        self,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[MessageDTO]:
        messages = await self._load_messages(session_id)
        return CursorPage(
            items=messages[:limit],
            next_cursor=None,
            has_more=len(messages) > limit,
        )

    async def get(self, session_id: str, message_id: str) -> MessageDTO:
        messages = await self._load_messages(session_id)
        for message in messages:
            if message.message_id == message_id:
                return message
        raise ValueError(f"Message {message_id} not found in session {session_id}")

    async def create(self, session_id: str, message_create: MessageCreateRequest) -> MessageDTO:
        """创建一条用户消息 DTO。

        注意：实际的持久化由 LangGraph checkpoint 负责；此方法只生成 message_id
        并返回 DTO，供 API 响应和事件发布使用。
        """
        now = datetime.now()
        return MessageDTO(
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            attachments=message_create.attachments,
            metadata=message_create.metadata,
            created_at=now,
            updated_at=now,
        )

    async def _load_messages(self, session_id: str) -> list[MessageDTO]:
        if self._checkpointer is None:
            return []

        config = self._checkpoint_config(session_id)
        tup = await self._checkpointer.aget_tuple(config)
        if tup is None:
            return []

        raw_messages = tup.checkpoint.get("channel_values", {}).get("messages", [])
        if not isinstance(raw_messages, list):
            return []

        result: list[MessageDTO] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                continue
            # 与旧的 messages.jsonl 保持一致：只返回用户可见的 user/assistant 消息
            if isinstance(message, ToolMessage):
                continue
            result.append(self._message_to_dto(session_id, index, message))
        return result
