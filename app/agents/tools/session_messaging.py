from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import Field, StrictBool, create_model

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.schemas.public_v2.common import MessageRole


def create_send_message_to_session_tool(
    sender_session_id: str,
    sender_agent_id: str = "default",
    *,
    session_orchestrator: SessionOrchestratorProtocol,
) -> BaseTool:
    """创建向目标 session 发送消息的工具。"""
    input_schema = create_model(
        "SendMessageToSessionInput",
        target_session_id=(str, Field(description="接收消息的目标 session ID")),
        content=(str, Field(description="要发送的消息正文")),
        simulate_user=(
            StrictBool,
            Field(
                default=False,
                description="是否模拟普通用户发送；false 时由系统注入发送方身份并包装跨会话提醒",
            ),
        ),
    )

    @tool("send_message_to_session", args_schema=input_schema)
    async def send_message_to_session(
        target_session_id: str,
        content: str,
        simulate_user: bool = False,
    ) -> dict[str, Any]:
        """向目标 session 发送消息并启动任务；默认发送带可信来源的跨会话提醒。"""
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if not content.strip():
            raise ValueError("content 不能为空")
        sent_at = datetime.now(timezone.utc).isoformat()
        reminder_payload = {
            "sender_session_id": sender_session_id,
            "sender_agent_id": sender_agent_id,
            "target_session_id": target_session_id,
            "sent_at": sent_at,
            "message": content,
        }
        if simulate_user:
            result = await session_orchestrator.create_and_run(
                target_session_id,
                content,
            )
        else:
            submitted_content = (
                "<system_reminder>\n"
                f"{json.dumps(reminder_payload, ensure_ascii=False, indent=2)}\n"
                "</system_reminder>"
            )
            result = await session_orchestrator.create_and_run(
                target_session_id,
                submitted_content,
                message_role=MessageRole.system,
                metadata={
                    "source": "send_message_to_session",
                    "simulate_user": False,
                    **reminder_payload,
                },
            )
        return {
            "job_id": result.job_id,
            "simulate_user": simulate_user,
            "sender_session_id": sender_session_id,
            "sender_agent_id": sender_agent_id,
            "target_session_id": target_session_id,
            "message_id": result.message_id,
            "status": result.status,
            "sent_at": sent_at,
        }

    return send_message_to_session
