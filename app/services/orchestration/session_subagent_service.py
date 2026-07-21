from __future__ import annotations

import json

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_subagent import (
    BeforeSubagentStart,
    GENERAL_PURPOSE_SUBAGENT,
    SessionStoreProtocol,
    SessionSubagentAccepted,
)


class SessionSubagentService:
    """把 Agent 委派转换为可持久化、可继续对话的子会话。"""

    def __init__(
        self,
        *,
        session_service: SessionStoreProtocol,
        session_orchestrator: SessionOrchestratorProtocol,
    ) -> None:
        self._session_service = session_service
        self._session_orchestrator = session_orchestrator

    async def delegate(
        self,
        *,
        parent_session_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        description: str,
        subagent_type: str,
        title: str | None = None,
        trusted_context: dict[str, object] | None = None,
        before_start: BeforeSubagentStart | None = None,
    ) -> SessionSubagentAccepted:
        normalized_description = description.strip()
        if not normalized_description:
            raise ValueError("description 不能为空")
        if subagent_type != GENERAL_PURPOSE_SUBAGENT:
            raise ValueError(
                "不支持的 subagent_type: "
                f"{subagent_type}；当前仅支持 {GENERAL_PURPOSE_SUBAGENT}"
            )
        if not parent_job_id:
            raise RuntimeError("创建委派子会话时缺少 parent_job_id")
        if not parent_tool_call_id:
            raise RuntimeError("创建委派子会话时缺少 parent_tool_call_id")

        parent_session = await self._session_service.get(parent_session_id)
        if parent_session.current_agent_id != parent_agent_id:
            raise RuntimeError(
                "委派调用的 Agent 与父会话当前 Agent 不一致: "
                f"expected={parent_session.current_agent_id} actual={parent_agent_id}"
            )

        child_session = await self._session_service.create_delegated(
            title=self._build_title(title or normalized_description),
            agent_id=parent_agent_id,
            parent_session_id=parent_session_id,
            parent_job_id=parent_job_id,
            parent_tool_call_id=parent_tool_call_id,
            subagent_type=subagent_type,
        )
        if before_start is not None:
            try:
                await before_start(child_session)
            except Exception as error:
                await self._session_service.set_delegation_start_result(
                    child_session.session_id,
                    status="failed",
                    error=str(error),
                )
                raise RuntimeError(
                    "委派子会话已创建，但启动前准备失败: "
                    f"child_session_id={child_session.session_id} error={error}"
                ) from error
        delegation_content = self._build_delegation_content(
            parent_session_id=parent_session_id,
            parent_agent_id=parent_agent_id,
            parent_job_id=parent_job_id,
            parent_tool_call_id=parent_tool_call_id,
            child_session_id=child_session.session_id,
            subagent_type=subagent_type,
            description=normalized_description,
            trusted_context=dict(trusted_context or {}),
        )
        try:
            accepted = await self._session_orchestrator.create_and_run(
                child_session.session_id,
                delegation_content,
                metadata={
                    "source": "session_subagent_delegation",
                    "parent_session_id": parent_session_id,
                    "parent_job_id": parent_job_id,
                    "parent_tool_call_id": parent_tool_call_id,
                    "subagent_type": subagent_type,
                    "trusted_context": dict(trusted_context or {}),
                },
            )
        except Exception as error:
            await self._session_service.set_delegation_start_result(
                child_session.session_id,
                status="failed",
                error=str(error),
            )
            raise RuntimeError(
                "委派子会话已创建，但首个 Job 启动失败: "
                f"child_session_id={child_session.session_id} error={error}"
            ) from error
        child_session = await self._session_service.set_delegation_start_result(
            child_session.session_id,
            status="running",
        )
        return SessionSubagentAccepted(
            child_session=child_session,
            message_id=accepted.message_id,
            job_id=accepted.job_id,
        )

    @staticmethod
    def _build_title(description: str) -> str:
        single_line = " ".join(description.split())
        return f"委派：{single_line[:48]}"

    @staticmethod
    def _build_delegation_content(
        *,
        parent_session_id: str,
        parent_agent_id: str,
        parent_job_id: str,
        parent_tool_call_id: str,
        child_session_id: str,
        subagent_type: str,
        description: str,
        trusted_context: dict[str, object],
    ) -> str:
        metadata = {
            "type": "subagent_delegation",
            "parent_session_id": parent_session_id,
            "parent_agent_id": parent_agent_id,
            "parent_job_id": parent_job_id,
            "parent_tool_call_id": parent_tool_call_id,
            "child_session_id": child_session_id,
            "subagent_type": subagent_type,
            "trusted_context": trusted_context,
            "communication": {
                "tool": "send_message_to_session",
                "target_session_id": parent_session_id,
                "simulate_user": False,
                "kinds": ["question", "progress", "result"],
            },
        }
        return (
            "<system_reminder>\n"
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n"
            "你是由父 Agent 创建的独立子会话。不要假设本会话的普通最终回复会自动返回父 Agent。\n"
            "需要提问、汇报进度或提交最终结果时，必须调用 send_message_to_session，"
            "target_session_id 使用上面的可信值，simulate_user=false；"
            "提问用 kind=question，进度用 kind=progress，最终结果用 kind=result。\n"
            "父子会话采用异步轮次通信；消息可能在目标会话当前任务结束后才开始处理。\n"
            "</system_reminder>\n"
            "<delegated_task>\n"
            f"{description}\n"
            "</delegated_task>"
        )
