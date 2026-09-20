"""MessageService：从 LangGraph checkpoint 读取会话历史。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from langchain_core.messages import (
    BaseMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from app.abstractions.session_context import AgentContextState
from app.agents.cache_preserving_summarization import apply_summarization_event
from app.core.checkpoint_config import build_checkpoint_config
from app.core.identifier import create_prefixed_id
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.common import CursorPage, MessageRole
from app.schemas.internal_v2.message import (
    AgentStateMessagesDTO,
    MessageCreateRequest,
    MessageDTO,
)
from app.services.business.session_turn_history.visible_page import visible_message_page
from app.services.business.system_reminder_checkpoint_service import (
    submit_checkpoint_reminder,
)
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore
from app.services.mapping.itemized.message_reasoning_merge import (
    merge_canonical_reasoning,
)
from app.services.mapping.message_content import MessageContentProjectionMixin


class MessageService(MessageContentProjectionMixin):
    def __init__(
        self,
        checkpointer: BaseCheckpointSaver | None = None,
        attachment_store: SessionAttachmentStore | None = None,
        canonical_item_reader: Callable[[str], Sequence[CanonicalItemRecord]]
        | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._attachment_store = attachment_store
        self._canonical_item_reader = canonical_item_reader


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
        return visible_message_page(
            self._visible_messages_from_checkpoint(session_id, checkpoint_tuple),
            session_id=session_id,
            checkpoint_id=str(checkpoint_tuple.checkpoint.get("id") or ""),
            limit=limit,
            cursor=cursor,
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
        if self._canonical_item_reader is not None:
            canonical_items = await asyncio.to_thread(
                self._canonical_item_reader,
                session_id,
            )
            raw_messages = merge_canonical_reasoning(
                raw_messages,
                canonical_items,
            )
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

    def submit_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        event_identity: str | None = None,
    ) -> bool:
        if self._checkpointer is None:
            raise RuntimeError("MessageService 未配置 checkpointer，无法写入 system_reminder")
        return submit_checkpoint_reminder(
            checkpointer=self._checkpointer,
            session_id=session_id,
            reminder=reminder,
            response_metadata=response_metadata,
            checkpoint_source=checkpoint_source,
            event_identity=event_identity,
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
