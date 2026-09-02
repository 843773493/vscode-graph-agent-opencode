"""MessageService：从 LangGraph checkpoint 读取会话历史。"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from app.abstractions.session_context import AgentContextState
from app.agents.cache_preserving_summarization import apply_summarization_event
from app.core.checkpoint_config import build_checkpoint_config
from app.core.identifier import create_prefixed_id
from app.prompting.validation import internal_prompt_metadata, validate_internal_message
from app.schemas.internal_v2.common import CursorPage, MessageRole
from app.schemas.internal_v2.message import (
    AgentStateMessagesDTO,
    AttachmentRef,
    MessageCreateRequest,
    MessageDTO,
)
from app.services.business.message_display import project_message_for_display
from app.services.business.system_reminder_checkpoint_service import (
    append_system_reminder_checkpoint,
)
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.mapping.user_message_content_projection import user_content_projection


class MessageService:
    def __init__(
        self,
        checkpointer: BaseCheckpointSaver | None = None,
        attachment_store: SessionAttachmentStore | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._attachment_store = attachment_store

    @staticmethod
    def _message_to_dto(
        session_id: str,
        index: int,
        message: BaseMessage,
    ) -> MessageDTO:
        role = MessageService._persisted_role(message)
        extracted = MessageService._extract_content(message)
        content = extracted["content"]
        response_metadata = message.response_metadata or {}
        structured_metadata = internal_prompt_metadata(response_metadata)
        display_projection = None
        if isinstance(message.content, str):
            if structured_metadata is not None or role != MessageRole.user:
                display_projection = project_message_for_display(
                    message.content,
                    response_metadata,
                )
                content = display_projection.content
        elif structured_metadata is not None:
            raise TypeError("内部结构消息 content 必须是字符串")
        else:
            if role == MessageRole.user:
                user_projection = user_content_projection(
                    message.content,
                    response_metadata,
                )
                content = user_projection.visible_text
            else:
                display_content = response_metadata.get("display_content")
                if display_content is not None:
                    if not isinstance(display_content, str):
                        raise TypeError("message metadata.display_content 必须是字符串")
                    content = display_content
        message_id = response_metadata.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError(
                "用户可见消息缺少持久化 message_id: "
                f"checkpoint_index={index} message_type={message.type}"
            )
        created_at = MessageService._metadata_datetime(response_metadata, "created_at")
        updated_at = MessageService._metadata_datetime(response_metadata, "updated_at")
        metadata: dict[str, object] = {
            "langchain_type": message.type,
            "tool_calls": getattr(message, "tool_calls", None) or [],
            "tool_call_id": getattr(message, "tool_call_id", None),
        }
        display_blocks = MessageService._display_content_blocks(
            extracted["content_blocks"]
        )
        if display_blocks:
            metadata["content_blocks"] = display_blocks
        if extracted["reasoning_id"] is not None:
            metadata["reasoning_id"] = extracted["reasoning_id"]
        message_metadata = response_metadata.get("message_metadata")
        if message_metadata is not None:
            if not isinstance(message_metadata, Mapping):
                raise TypeError("checkpoint message_metadata 必须是对象")
            metadata.update(
                {str(key): value for key, value in message_metadata.items()}
            )
        metadata.update(
            {
                key: value
                for key, value in response_metadata.items()
                if key
                not in {
                    "attachments",
                    "content_blocks",
                    "message_metadata",
                    "message_role",
                }
            }
        )
        if structured_metadata is not None:
            if display_projection is None:
                raise RuntimeError("内部结构消息缺少展示投影")
            metadata = {
                "langchain_type": message.type,
                **display_projection.metadata,
            }
        return MessageDTO(
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            attachments=MessageService._attachments_for_message(
                message,
                response_metadata,
            ),
            metadata=metadata,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _metadata_datetime(metadata: Mapping[object, object], key: str) -> datetime:
        value = metadata.get(key)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value)
        else:
            raise RuntimeError(f"用户可见消息缺少持久化 {key}")
        if parsed.tzinfo is None:
            raise RuntimeError(f"用户可见消息的持久化 {key} 必须包含时区")
        return parsed

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
    def _persisted_role(message: BaseMessage) -> MessageRole:
        """模型消息类型决定 role，业务来源只保存在 response_metadata。"""
        return MessageService._detect_role(message)

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
                attachments.append(item.model_copy(update={"data_url": None}))
                continue
            if isinstance(item, Mapping):
                attachments.append(AttachmentRef.model_validate({
                    str(key): value
                    for key, value in item.items()
                    if str(key) != "data_url"
                }))
                continue
            raise TypeError(
                f"message.response_metadata.attachments 中出现不支持的元素类型: {type(item).__name__}"
            )
        return attachments

    @staticmethod
    def _attachments_for_message(
        message: BaseMessage,
        response_metadata: Mapping[object, object],
    ) -> list[AttachmentRef]:
        attachments = MessageService._attachments_from_metadata(response_metadata)
        if attachments or not isinstance(message, HumanMessage):
            return attachments
        projection = user_content_projection(message.content, response_metadata)
        return [
            AttachmentRef.model_validate(
                {
                    str(key): value
                    for key, value in item.items()
                    if str(key) != "data_url"
                }
            )
            for item in projection.attachments
        ]

    @staticmethod
    def _is_system_reminder_only_message(message: BaseMessage) -> bool:
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            return False
        metadata = message.response_metadata or {}
        if internal_prompt_metadata(metadata) is None:
            return False
        validate_internal_message(content, metadata)
        return True

    @staticmethod
    def _is_user_visible_message(message: BaseMessage) -> bool:
        if MessageService._is_system_reminder_only_message(message):
            content = message.content
            if not isinstance(content, str):
                raise TypeError("内部结构消息 content 必须是字符串")
            return project_message_for_display(
                content,
                message.response_metadata or {},
            ).visible
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
            "role": MessageService._persisted_role(message).value,
            "type": message.type,
            "content": MessageService._json_safe(
                raw_content
                if isinstance(message, HumanMessage)
                else content_blocks if content_blocks else raw_content
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
    def _agent_state_record_key(record: Mapping[str, object]) -> str:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _dedupe_consecutive_agent_state_records(
        records: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        deduped: list[dict[str, object]] = []
        previous_key: str | None = None
        for record in records:
            current_key = MessageService._agent_state_record_key(record)
            if current_key == previous_key:
                continue
            deduped.append(record)
            previous_key = current_key
        return deduped

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
                    text_block: dict[str, object] = {"type": "text", "text": text}
                    if isinstance(part.get("id"), str):
                        text_block["id"] = part["id"]
                    if isinstance(part.get("index"), int):
                        text_block["index"] = part["index"]
                    content_blocks.append(text_block)
            elif part_type in {"reasoning", "thinking"}:
                reasoning_text = (
                    part.get("thinking") or part.get("text")
                    if part_type == "thinking"
                    else part.get("reasoning")
                )
                if not isinstance(reasoning_text, str):
                    raw_content = part.get("content")
                    if isinstance(raw_content, list):
                        reasoning_text = "".join(
                            item.get("text", "")
                            for item in raw_content
                            if isinstance(item, Mapping)
                            and item.get("type") in {"reasoning_text", "text"}
                            and isinstance(item.get("text"), str)
                        )
                    else:
                        reasoning_text = ""
                if not reasoning_text:
                    reasoning_text = extract_reasoning_summary(part.get("summary"))
                reasoning_block: dict[str, object] = {
                    "type": "reasoning",
                    "reasoning": reasoning_text,
                }
                if reasoning_id is None:
                    rid = part.get("id")
                    if isinstance(rid, str):
                        reasoning_id = rid
                if isinstance(part.get("id"), str):
                    reasoning_block["id"] = part["id"]
                if isinstance(part.get("index"), int):
                    reasoning_block["index"] = part["index"]
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

    @staticmethod
    def _display_content_blocks(value: object) -> list[dict[str, object]]:
        """只保留历史 UI 所需的轻量文本/推理块，媒体正文由附件接口读取。"""
        if not isinstance(value, list):
            return []
        result: list[dict[str, object]] = []
        for block in value:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            block_metadata = block.get("metadata")
            if (
                isinstance(block_metadata, Mapping)
                and block_metadata.get("origin") == "generated"
                and block_metadata.get("kind") in {
                    "attachment_manifest",
                    "attachment_preview",
                }
            ):
                continue
            if block_type == "text" and isinstance(block.get("text"), str):
                normalized: dict[str, object] = {
                    "type": "text",
                    "text": block["text"],
                }
            elif block_type == "reasoning" and isinstance(
                block.get("reasoning"), str
            ):
                normalized = {
                    "type": "reasoning",
                    "reasoning": block["reasoning"],
                }
            else:
                continue
            if isinstance(block.get("id"), str):
                normalized["id"] = block["id"]
            if isinstance(block.get("index"), int):
                normalized["index"] = block["index"]
            result.append(normalized)
        return result

    async def list(
        self,
        session_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[MessageDTO]:
        checkpoint_tuple = (
            await self._checkpointer.aget_tuple(build_checkpoint_config(session_id))
            if self._checkpointer is not None
            else None
        )
        if checkpoint_tuple is None:
            return CursorPage(items=[], next_cursor=None, has_more=False)
        checkpoint_id = str(checkpoint_tuple.checkpoint.get("id") or "")
        if not checkpoint_id:
            raise RuntimeError(f"checkpoint 缺少有效 id: session_id={session_id}")
        if limit < 1:
            raise ValueError("消息分页 limit 必须大于 0")
        messages = self._visible_messages_from_checkpoint(
            session_id,
            checkpoint_tuple,
        )
        end = (
            len(messages)
            if cursor is None
            else self._decode_visible_cursor(cursor, session_id, checkpoint_id)
        )
        if end < 0 or end > len(messages):
            raise ValueError("消息历史游标位置无效")
        start = max(0, end - limit)
        # 消息分页不能把一轮截成孤立的 assistant 回复。
        while start > 0 and messages[start].role != MessageRole.user:
            start -= 1
        return CursorPage(
            items=messages[start:end],
            next_cursor=(
                self._encode_visible_cursor(session_id, checkpoint_id, start)
                if start > 0
                else None
            ),
            has_more=start > 0,
        )

    @staticmethod
    def _encode_visible_cursor(
        session_id: str,
        checkpoint_id: str,
        before: int,
    ) -> str:
        payload = json.dumps(
            {
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
                "before": before,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_visible_cursor(
        cursor: str,
        session_id: str,
        checkpoint_id: str,
    ) -> int:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("消息历史游标格式无效") from error
        if not isinstance(payload, dict):
            raise TypeError("消息历史游标内容无效")
        if (
            payload.get("session_id") != session_id
            or payload.get("checkpoint_id") != checkpoint_id
        ):
            raise ValueError("消息历史已更新，请重新加载最新消息")
        before = payload.get("before")
        if isinstance(before, bool) or not isinstance(before, int):
            raise TypeError("消息历史游标缺少 before")
        return before

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
        if message_create.role != MessageRole.user:
            raise ValueError(
                "创建并执行新一轮消息时 role 必须为 user；"
                "委派、跨会话和团队消息的来源请写入 metadata"
            )
        attachments = message_create.attachments
        if self._attachment_store is not None:
            attachments = self._attachment_store.persist_inline(session_id, attachments)
        now = datetime.now(UTC)
        return MessageDTO(
            message_id=create_prefixed_id("msg"),
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            attachments=attachments,
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
        return self._dedupe_consecutive_agent_state_records(records)

    async def get_agent_context_state(self, session_id: str) -> AgentContextState:
        """读取应用压缩事件后，模型当前实际使用的消息上下文。"""
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 checkpointer，无法读取 Agent Context")

        checkpoint_tuple = await self._checkpointer.aget_tuple(
            build_checkpoint_config(session_id)
        )
        if checkpoint_tuple is None:
            return {
                "records": [],
                "checkpoint_id": "",
                "raw_message_count": 0,
                "compacted": False,
                "compaction_cutoff": None,
                "history_file_path": None,
            }

        checkpoint = checkpoint_tuple.checkpoint
        channel_values = checkpoint.get("channel_values", {})
        if not isinstance(channel_values, Mapping):
            raise TypeError(
                "LangGraph checkpoint channel_values 应为 mapping，"
                f"实际类型: {type(channel_values).__name__}"
            )
        raw_messages = channel_values.get("messages", [])
        if not isinstance(raw_messages, list):
            raise TypeError(
                f"Agent Context messages 应为 list，实际类型: {type(raw_messages).__name__}"
            )

        event = channel_values.get("_summarization_event")
        compacted = event is not None
        compaction_cutoff: int | None = None
        history_file_path: str | None = None
        effective_messages = list(raw_messages)
        if event is not None:
            if not isinstance(event, Mapping):
                raise TypeError(
                    "_summarization_event 应为 mapping，"
                    f"实际类型: {type(event).__name__}"
                )
            summary_message = event.get("summary_message")
            compaction_cutoff = event.get("cutoff_index")
            if not isinstance(compaction_cutoff, int) or compaction_cutoff < 0:
                raise TypeError(
                    "_summarization_event.cutoff_index 应为非负整数，"
                    f"实际值: {compaction_cutoff!r}"
                )
            if summary_message is None:
                raise ValueError("_summarization_event 缺少 summary_message")
            raw_history_file_path = event.get("file_path")
            if raw_history_file_path is not None and not isinstance(
                raw_history_file_path,
                str,
            ):
                raise TypeError("_summarization_event.file_path 应为字符串或 null")
            history_file_path = raw_history_file_path
            effective_messages = apply_summarization_event(raw_messages, event)

        records: list[dict[str, object]] = []
        for message in effective_messages:
            if isinstance(message, BaseMessage):
                records.append(self._message_to_agent_state_record(message))
            elif isinstance(message, Mapping):
                records.append(self._mapping_to_agent_state_record(message))
            else:
                raise TypeError(
                    "Agent Context messages 中出现不支持的消息类型: "
                    f"{type(message).__name__}"
                )

        return {
            "records": self._dedupe_consecutive_agent_state_records(records),
            "checkpoint_id": str(checkpoint.get("id") or ""),
            "raw_message_count": len(raw_messages),
            "compacted": compacted,
            "compaction_cutoff": compaction_cutoff,
            "history_file_path": history_file_path,
        }

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
        messages, _ = await self._load_messages_with_checkpoint_id(session_id)
        return messages

    async def _load_messages_with_checkpoint_id(
        self,
        session_id: str,
    ) -> tuple[list[MessageDTO], str]:
        if self._checkpointer is None:
            return [], ""
        checkpoint_tuple = await self._checkpointer.aget_tuple(
            build_checkpoint_config(session_id)
        )
        if checkpoint_tuple is None:
            return [], ""
        raw_messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get(
            "messages", []
        )
        if not isinstance(raw_messages, list):
            return [], str(checkpoint_tuple.checkpoint.get("id") or "")

        return self._visible_messages_from_checkpoint(session_id, checkpoint_tuple), str(
            checkpoint_tuple.checkpoint.get("id") or ""
        )

    def _visible_messages_from_checkpoint(
        self,
        session_id: str,
        checkpoint_tuple: CheckpointTuple,
    ) -> list[MessageDTO]:
        raw_messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get(
            "messages", []
        )
        if not isinstance(raw_messages, list):
            return []

        result: list[MessageDTO] = []
        seen_visible_messages: set[tuple[str, str]] = set()
        for index, message in enumerate(raw_messages):
            if not isinstance(message, BaseMessage):
                continue
            # 普通消息列表只暴露用户可见的输入和最终回复；工具调用、
            # system_reminder 与空 assistant 仍可通过 Agent State 调试视图查看。
            if not self._is_user_visible_message(message):
                continue
            dto = self._message_to_dto(session_id, index, message)
            visible_key = (dto.role.value, dto.message_id)
            if visible_key in seen_visible_messages:
                continue
            seen_visible_messages.add(visible_key)
            result.append(dto)
        return result
