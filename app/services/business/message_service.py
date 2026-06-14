from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
import logging

from app.schemas.public_v2.message import MessageDTO, MessageCreateRequest
from app.schemas.public_v2.common import MessageRole, CursorPage
from app.core.path_utils import get_session_path


logger = logging.getLogger(__name__)


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

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("跳过无法解析的消息记录: session_id=%s line=%s", session_id, line)
                    continue

                try:
                    message = MessageDTO.model_validate(data)
                except Exception:
                    logger.exception("跳过无效消息记录: session_id=%s data=%s", session_id, data)
                    continue

                if not message.content and message.role == MessageRole.assistant:
                    logger.warning("跳过空内容的助手消息: session_id=%s message_id=%s", session_id, message.message_id)
                    continue

                messages.append(message)

        messages.sort(key=lambda item: item.created_at)
        return messages

    def _append_message(self, message: MessageDTO) -> MessageDTO:
        message_file = self._message_file(message.session_id)
        message_file.parent.mkdir(exist_ok=True, parents=True)

        with open(message_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.model_dump(mode="json"), ensure_ascii=False) + "\n")

        return message

    def _write_messages(self, session_id: str, messages: list[MessageDTO]) -> None:
        message_file = self._message_file(session_id)
        message_file.parent.mkdir(exist_ok=True, parents=True)
        with open(message_file, "w", encoding="utf-8") as f:
            for message in messages:
                f.write(json.dumps(message.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def _build_system_reminder(
        self,
        *,
        phase: str,
        tool_name: str | None,
        interrupted_at: datetime,
    ) -> str:
        phase_desc = "文本生成" if phase == "text" else "工具调用"
        lines = [
            "",
            "<system_reminder>",
            f"用户在 {phase_desc} 过程中于 {interrupted_at.isoformat()} 打断。",
        ]
        if phase == "tool" and tool_name:
            lines.append(f"当前工具调用：{tool_name} 已被取消。")
        lines.append("请停止当前操作，根据已有信息回应用户最新请求。")
        lines.append("</system_reminder>")
        return "\n".join(lines)

    async def append_system_reminder_to_last_message(
        self,
        session_id: str,
        *,
        phase: str,
        tool_name: str | None = None,
        interrupted_at: datetime | None = None,
        base_content: str | None = None,
    ) -> MessageDTO:
        if interrupted_at is None:
            interrupted_at = datetime.now()

        messages = self._read_messages(session_id)
        if not messages:
            raise ValueError(f"Session {session_id} 没有可追加提醒的消息")

        # tool/text 打断都定位到 assistant 消息：text 是正在生成的回复，
        # tool 是包含 tool_calls 的那条 assistant 消息（若尚未持久化则创建）。
        target_role = MessageRole.assistant
        target_message: MessageDTO | None = None
        for message in reversed(messages):
            if message.role == target_role:
                target_message = message
                break

        reminder = self._build_system_reminder(
            phase=phase,
            tool_name=tool_name,
            interrupted_at=interrupted_at,
        )

        if target_message is None and phase == "text":
            target_message = MessageDTO(
                message_id=f"msg_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                role=MessageRole.assistant,
                content=base_content or "",
                attachments=[],
                metadata={"interrupted": True, "phase": phase},
                created_at=interrupted_at,
                updated_at=interrupted_at,
            )
            messages.append(target_message)
        elif target_message is None and phase == "tool":
            target_message = MessageDTO(
                message_id=f"msg_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                role=MessageRole.assistant,
                content=base_content or f"我将调用工具：{tool_name or 'unknown'}。",
                attachments=[],
                metadata={"interrupted": True, "phase": phase, "tool_name": tool_name},
                created_at=interrupted_at,
                updated_at=interrupted_at,
            )
            messages.append(target_message)
        elif target_message is None:
            target_message = messages[-1]

        target_message.content = target_message.content + reminder
        target_message.updated_at = interrupted_at

        self._write_messages(session_id, messages)
        return target_message

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