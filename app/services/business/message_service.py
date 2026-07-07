"""MessageService：从 LangGraph checkpoint 读取会话历史。"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
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
from app.schemas.public_v2.message import (
    AgentStateMessagesDTO,
    AttachmentRef,
    MessageCreateRequest,
    MessageDTO,
)
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.business.system_reminder_checkpoint_service import (
    append_system_reminder_checkpoint,
)


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
        display_content = response_metadata.get("display_content")
        if role == MessageRole.user and isinstance(display_content, str):
            content = display_content
        message_id = response_metadata.get("message_id") or f"msg_{index:06d}"
        created_at = MessageService._metadata_datetime(response_metadata, "created_at")
        updated_at = MessageService._metadata_datetime(response_metadata, "updated_at")
        metadata: dict[str, object] = {
            "langchain_type": message.type,
            "tool_calls": getattr(message, "tool_calls", None) or [],
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        if extracted["content_blocks"]:
            metadata["content_blocks"] = extracted["content_blocks"]
        if extracted["reasoning_id"] is not None:
            metadata["reasoning_id"] = extracted["reasoning_id"]
        metadata.update(response_metadata)
        return MessageDTO(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            attachments=MessageService._attachments_from_metadata(response_metadata),
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _metadata_datetime(metadata: Mapping[object, object], key: str) -> datetime:
        value = metadata.get(key)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        # TODO: 旧 checkpoint 没有消息时间戳；清理完旧数据后改为严格要求该字段。
        return datetime.now()

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
    def _json_safe(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {
                str(key): MessageService._json_safe(item) for key, item in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [MessageService._json_safe(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return MessageService._json_safe(model_dump(mode="json"))
        return str(value)

    @staticmethod
    def _attachments_from_metadata(metadata: Mapping[object, object]) -> list[AttachmentRef]:
        raw_attachments = metadata.get("attachments")
        if not isinstance(raw_attachments, Sequence) or isinstance(
            raw_attachments, (str, bytes, bytearray)
        ):
            return []

        attachments: list[AttachmentRef] = []
        for item in raw_attachments:
            if isinstance(item, AttachmentRef):
                attachments.append(item)
                continue
            if isinstance(item, Mapping):
                attachments.append(
                    AttachmentRef.model_validate(
                        {str(key): value for key, value in item.items()}
                    )
                )
                continue
            raise TypeError(
                f"message.response_metadata.attachments 中出现不支持的元素类型: {type(item).__name__}"
            )
        return attachments

    @staticmethod
    def _is_system_reminder_only_message(message: BaseMessage) -> bool:
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            return False
        stripped = content.strip()
        return (
            stripped.startswith("<system_reminder>")
            and stripped.endswith("</system_reminder>")
        )

    @staticmethod
    def _is_user_visible_message(message: BaseMessage) -> bool:
        if MessageService._is_system_reminder_only_message(message):
            return False
        if isinstance(message, ToolMessage):
            return False
        if isinstance(message, AIMessage):
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                return False
            extracted = MessageService._extract_content(message)
            content = extracted["content"]
            return isinstance(content, str) and bool(content.strip())
        return True

    @staticmethod
    def _message_to_agent_state_record(message: BaseMessage) -> dict[str, object]:
        extracted = MessageService._extract_content(message)
        content_blocks = extracted["content_blocks"]
        raw_content = getattr(message, "content", "")
        record: dict[str, object] = {
            "role": MessageService._detect_role(message).value,
            "type": message.type,
            "content": MessageService._json_safe(
                content_blocks if content_blocks else raw_content
            ),
        }

        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            record["tool_calls"] = MessageService._json_safe(tool_calls)

        tool_call_id = getattr(message, "tool_call_id", None)
        if isinstance(tool_call_id, str) and tool_call_id:
            record["tool_call_id"] = tool_call_id

        name = getattr(message, "name", None)
        if isinstance(name, str) and name:
            record["name"] = name

        response_metadata = dict(message.response_metadata or {})
        response_metadata.pop("display_content", None)
        phase = response_metadata.get("phase")
        if not isinstance(phase, str) and isinstance(message, AIMessage):
            content = extracted["content"]
            if tool_calls:
                response_metadata["phase"] = "commentary"
            elif isinstance(content, str) and content:
                response_metadata["phase"] = "final_answer"
        if response_metadata:
            record["response_metadata"] = MessageService._json_safe(response_metadata)

        usage_metadata = getattr(message, "usage_metadata", None)
        if usage_metadata:
            record["usage_metadata"] = MessageService._json_safe(usage_metadata)

        additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
        if additional_kwargs:
            record["additional_kwargs"] = MessageService._json_safe(additional_kwargs)

        return record

    @staticmethod
    def _mapping_to_agent_state_record(
        message: Mapping[object, object],
    ) -> dict[str, object]:
        allowed_keys = (
            "role",
            "type",
            "content",
            "tool_calls",
            "tool_call_id",
            "name",
            "response_metadata",
            "usage_metadata",
            "additional_kwargs",
        )
        record: dict[str, object] = {}
        for key in allowed_keys:
            if key not in message:
                continue
            value = message[key]
            if value is None or value == "" or value == []:
                continue
            record[key] = MessageService._json_safe(value)
        if record:
            return record

        ignored_keys = {
            "additional_kwargs",
            "id",
            "metadata",
            "response_metadata",
            "usage_metadata",
        }
        return {
            str(key): MessageService._json_safe(value)
            for key, value in message.items()
            if str(key) not in ignored_keys
        }

    @staticmethod
    def _extract_content(message: BaseMessage) -> dict[str, object]:
        """从 BaseMessage 提取可读 content，并把结构化 reasoning 块单独保存。

        返回:
        - content: 用户可见正文，不包含 reasoning
        - content_blocks: LangChain 标准 content block 列表
        - reasoning_id: 首个 reasoning 块的 id（用于关联）
        """
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return {
                "content": content,
                "content_blocks": [],
                "reasoning_id": None,
            }

        if not isinstance(content, list):
            return {
                "content": str(content),
                "content_blocks": [],
                "reasoning_id": None,
            }

        text_parts: list[str] = []
        content_blocks: list[dict[str, object]] = []
        reasoning_id: str | None = None

        for part in content:
            if not isinstance(part, dict):
                text = str(part)
                text_parts.append(text)
                content_blocks.append({"type": "text", "text": text})
                continue
            part_type = part.get("type")
            if part_type in ("text", "output_text"):
                text = part.get("text", "")
                if isinstance(text, str):
                    text_parts.append(text)
                    content_blocks.append({"type": "text", "text": text})
            elif part_type == "reasoning":
                reasoning_text = part.get("reasoning")
                if not isinstance(reasoning_text, str):
                    reasoning_text = extract_reasoning_summary(part.get("summary"))
                reasoning_block: dict[str, object] = {
                    "type": "reasoning",
                    "reasoning": reasoning_text,
                }
                extras: dict[str, object] = {}
                if reasoning_id is None:
                    rid = part.get("id")
                    if isinstance(rid, str):
                        reasoning_id = rid
                        extras["id"] = rid
                if "extras" in part and isinstance(part["extras"], dict):
                    extras.update(part["extras"])
                if extras:
                    reasoning_block["extras"] = extras
                content_blocks.append(reasoning_block)
            elif part_type == "refusal":
                refusal_text = part.get("refusal", "")
                text_parts.append(f"[拒绝]{refusal_text}")
                content_blocks.append({"type": "text", "text": f"[拒绝]{refusal_text}"})
            elif part_type == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, Mapping):
                    content_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": MessageService._json_safe(image_url),
                        }
                    )
                elif isinstance(image_url, str):
                    content_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        }
                    )
            elif part_type == "image":
                content_blocks.append(
                    {
                        str(key): MessageService._json_safe(value)
                        for key, value in part.items()
                    }
                )
            elif part_type == "function_call":
                name = part.get("name", "unknown_tool")
                args = part.get("arguments", "")
                text_parts.append(f"[调用工具 {name}，参数：{args}]")
            else:
                # 其它未知块类型：尝试提取常见字段，避免直接丢弃
                fallback = part.get("text")
                if isinstance(fallback, str):
                    text_parts.append(fallback)
                    content_blocks.append({"type": "text", "text": fallback})

        return {
            "content": "".join(text_parts),
            "content_blocks": content_blocks,
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

    async def list_agent_state_records(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[dict[str, object]]:
        raw_messages = await self._load_raw_messages(session_id, strict=strict)
        records: list[dict[str, object]] = []
        for message in raw_messages:
            if isinstance(message, BaseMessage):
                records.append(self._message_to_agent_state_record(message))
                continue
            if isinstance(message, Mapping):
                records.append(self._mapping_to_agent_state_record(message))
                continue
            raise TypeError(
                f"Agent State messages 中出现不支持的消息类型: {type(message).__name__}"
            )
        return records

    async def get_agent_state_messages(self, session_id: str) -> AgentStateMessagesDTO:
        records = await self.list_agent_state_records(session_id, strict=True)
        return AgentStateMessagesDTO(
            session_id=session_id,
            message_count=len(records),
            jsonl="\n".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                for record in records
            ),
        )

    def append_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        assistant_text: str = "",
        assistant_response_metadata: dict[str, object] | None = None,
    ) -> bool:
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 checkpointer，无法写入 system_reminder")
        return append_system_reminder_checkpoint(
            checkpointer=self._checkpointer,
            session_id=session_id,
            reminder=reminder,
            response_metadata=response_metadata,
            assistant_text=assistant_text,
            assistant_response_metadata=assistant_response_metadata,
            checkpoint_source=checkpoint_source,
        )

    async def _load_raw_messages(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[object]:
        if self._checkpointer is None:
            if strict:
                raise RuntimeError("MessageService 未配置 checkpointer，无法读取 Agent State")
            return []

        config = build_checkpoint_config(session_id)
        tup = await self._checkpointer.aget_tuple(config)
        if tup is None:
            return []

        raw_messages = tup.checkpoint.get("channel_values", {}).get("messages", [])
        if not isinstance(raw_messages, list):
            if not strict:
                return []
            raise TypeError(
                f"Agent State messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        return raw_messages

    async def _load_messages(self, session_id: str) -> list[MessageDTO]:
        raw_messages = await self._load_raw_messages(session_id)

        result: list[MessageDTO] = []
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                continue
            # 普通消息列表只暴露用户可见的输入和最终回复；工具调用、
            # system_reminder 与空 assistant 仍可通过 Agent State 调试视图查看。
            if not self._is_user_visible_message(message):
                continue
            result.append(self._message_to_dto(session_id, index, message))
        return result
