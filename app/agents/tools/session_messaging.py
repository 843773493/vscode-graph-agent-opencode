from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol


def create_send_message_to_session_tool(
    sender_agent_id: str = "default",
    *,
    session_orchestrator: SessionOrchestratorProtocol,
) -> BaseTool:
    """创建向目标 session 发送消息的工具。"""
    @tool("send_message_to_session")
    async def send_message_to_session(
        target_session_id: str,
        content: str,
    ) -> dict[str, Any]:
        """模拟用户向目标 session 发送消息，并立即启动目标 session 的新任务。"""
        if not target_session_id:
            raise ValueError("target_session_id 不能为空")
        if not content.strip():
            raise ValueError("content 不能为空")

        result = await session_orchestrator.create_and_run(target_session_id, content)
        return {
            "job_id": result.job_id,
            "sender_agent_id": sender_agent_id,
            "target_session_id": target_session_id,
            "message_id": result.message_id,
            "status": result.status,
        }

    return send_message_to_session
