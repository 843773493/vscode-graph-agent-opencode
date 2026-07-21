from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import Field, StrictBool, create_model

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.core.identifier import create_prefixed_id


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
        kind=(
            Literal["question", "reply", "progress", "result"],
            Field(
                default="result",
                description="跨 Agent 消息语义：提问、回复、进度或最终结果",
            ),
        ),
        reply_to_communication_id=(
            str | None,
            Field(
                default=None,
                description="kind=reply 时必填，使用收到问题中的 communication_id",
            ),
        ),
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
        kind: Literal["question", "reply", "progress", "result"] = "result",
        reply_to_communication_id: str | None = None,
        simulate_user: bool = False,
    ) -> dict[str, Any]:
        """向目标 session 发送消息并启动任务。

        默认发送带可信来源的跨会话提醒。返回 target_session_state 原子调度快照，
        包含目标 job 是运行还是排队、当前活跃 job、阻塞关系和队列数量。
        """
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if not content.strip():
            raise ValueError("content 不能为空")
        if kind == "reply" and not reply_to_communication_id:
            raise ValueError("kind=reply 时必须提供 reply_to_communication_id")
        if kind != "reply" and reply_to_communication_id is not None:
            raise ValueError("只有 kind=reply 可以提供 reply_to_communication_id")
        sent_at = datetime.now(timezone.utc).isoformat()
        communication_id = create_prefixed_id("comm")
        reminder_payload = {
            "communication_id": communication_id,
            "sender_session_id": sender_session_id,
            "sender_agent_id": sender_agent_id,
            "target_session_id": target_session_id,
            "sent_at": sent_at,
            "kind": kind,
            "reply_required": kind == "question",
            "reply_to_communication_id": reply_to_communication_id,
            "reply_via": (
                {
                    "tool": "send_message_to_session",
                    "target_session_id": sender_session_id,
                    "simulate_user": False,
                    "kind": "reply",
                    "reply_to_communication_id": communication_id,
                }
                if kind == "question"
                else None
            ),
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
            "target_session_state": result.dispatch.model_dump(mode="json"),
            "sent_at": sent_at,
            "communication_id": communication_id,
            "kind": kind,
            "reply_required": kind == "question",
            "reply_to_communication_id": reply_to_communication_id,
        }

    return send_message_to_session
