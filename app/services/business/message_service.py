"""MessageService：从 LangGraph checkpoint 读取会话历史。"""
from __future__ import annotations

import uuid
from datetime import datetime

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.public_v2.common import CursorPage, MessageRole
from app.schemas.public_v2.message import MessageCreateRequest, MessageDTO


class MessageService:
    def __init__(self, checkpointer: BaseCheckpointSaver | None = None) -> None:
        self._checkpointer = checkpointer

    @staticmethod
    def _message_to_dto(
        session_id: str,
        index: int,
        message: BaseMessage,
    ) -> MessageDTO:
        role = MessageService._detect_role(message)
        extracted = MessageService._extract_content(message)
        content = extracted["content"]
        response_metadata = message.response_metadata or {}
        message_id = response_metadata.get("message_id") or f"msg_{index:06d}"
        metadata: dict[str, object] = {
            "langchain_type": message.type,
            "tool_calls": getattr(message, "tool_calls", None) or [],
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        if extracted["reasoning_blocks"]:
            metadata["reasoning_blocks"] = extracted["reasoning_blocks"]
        if extracted["reasoning_id"] is not None:
            metadata["reasoning_id"] = extracted["reasoning_id"]
        metadata.update(response_metadata)
        return MessageDTO(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            attachments=[],
            metadata=metadata,
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
    def _format_reasoning_summary(summary: object) -> str:
        """把 reasoning.summary 块拼成可读字符串。"""
        if not isinstance(summary, list):
            return str(summary)
        parts: list[str] = []
        for entry in summary:
            if isinstance(entry, dict):
                text = entry.get("text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(entry, str):
                parts.append(entry)
            else:
                parts.append(str(entry))
        return "".join(parts)

    @staticmethod
    def _extract_content(message: BaseMessage) -> dict[str, object]:
        """从 BaseMessage 提取可读 content，并把结构化 reasoning 块单独保存。

        返回:
        - content: 拼装好的字符串（reasoning 摘要以 <think>...</think> 前缀放在最前）
        - reasoning_blocks: 原始 reasoning 块列表（responses API / opencode_zen 都可能产生）
        - reasoning_id: 首个 reasoning 块的 id（用于关联）
        """
        content = getattr(message, "content", "")
        if isinstance(content, str):
            # 兼容 opencode_zen：字符串 content + additional_kwargs["kind"]="reasoning"
            # 这里认为整个字符串就是 reasoning 内容，提取为 <think>...</think>
            if content.strip() and not getattr(message, "tool_calls", None):
                additional = getattr(message, "additional_kwargs", {}) or {}
                kind = additional.get("kind")
                if kind == "reasoning":
                    return {
                        "content": f"<think>\n{content}\n</think>",
                        "reasoning_blocks": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": content}],
                            }
                        ],
                        "reasoning_id": None,
                    }
            return {
                "content": content,
                "reasoning_blocks": [],
                "reasoning_id": None,
            }

        if not isinstance(content, list):
            return {
                "content": str(content),
                "reasoning_blocks": [],
                "reasoning_id": None,
            }

        text_parts: list[str] = []
        reasoning_blocks: list[dict] = []
        reasoning_id: str | None = None

        for part in content:
            if not isinstance(part, dict):
                text_parts.append(str(part))
                continue
            part_type = part.get("type")
            if part_type in ("text", "output_text"):
                text = part.get("text", "")
                if isinstance(text, str):
                    text_parts.append(text)
            elif part_type == "reasoning":
                summary_text = MessageService._format_reasoning_summary(part.get("summary"))
                if summary_text:
                    text_parts.insert(0, f"<think>\n{summary_text}\n</think>")
                reasoning_blocks.append(part)
                if reasoning_id is None:
                    rid = part.get("id")
                    if isinstance(rid, str):
                        reasoning_id = rid
            elif part_type == "refusal":
                refusal_text = part.get("refusal", "")
                text_parts.append(f"[拒绝]{refusal_text}")
            elif part_type == "function_call":
                name = part.get("name", "unknown_tool")
                args = part.get("arguments", "")
                text_parts.append(f"[调用工具 {name}，参数：{args}]")
            else:
                # 其它未知块类型：尝试提取常见字段，避免直接丢弃
                fallback = part.get("text")
                if isinstance(fallback, str):
                    text_parts.append(fallback)

        return {
            "content": "".join(text_parts),
            "reasoning_blocks": reasoning_blocks,
            "reasoning_id": reasoning_id,
        }

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

        config = build_checkpoint_config(session_id)
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
