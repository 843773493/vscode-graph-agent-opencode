from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import Field

from app.abstractions.session_subagent import (
    GENERAL_PURPOSE_SUBAGENT,
    SessionSubagentProtocol,
)
from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.job_context import get_current_job_id


def create_session_subagent_tool(
    *,
    parent_session_id: str,
    parent_agent_id: str,
    session_subagent_service: SessionSubagentProtocol,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    @tool(
        "task",
        description=(
            "为复杂、可独立执行的工作创建一个持久化子会话并立即启动。"
            "本工具只返回 child_session_id/job_id，不会把子 Agent 最终文本作为隐藏返回值带回。"
            "父子 Agent 的问题、进度和最终结果必须通过 send_message_to_session 继续通信；"
            "不要为寒暄、简单问题或单步操作创建子会话。"
        ),
    )
    async def task(
        description: Annotated[
            str,
            Field(description="完整、可独立执行的任务说明；必须包含必要背景、边界和预期产物"),
        ],
        subagent_type: Annotated[
            Literal["general-purpose"],
            Field(description=f"子 Agent 类型；当前仅支持 {GENERAL_PURPOSE_SUBAGENT}"),
        ] = GENERAL_PURPOSE_SUBAGENT,
    ) -> dict[str, Any]:
        tool_call_id = invocation_context.require_tool_call_id()
        parent_job_id = get_current_job_id()
        if not parent_job_id:
            raise RuntimeError("task 工具调用缺少当前 job_id")

        accepted = await session_subagent_service.delegate(
            parent_session_id=parent_session_id,
            parent_agent_id=parent_agent_id,
            parent_job_id=parent_job_id,
            parent_tool_call_id=tool_call_id,
            description=description,
            subagent_type=subagent_type,
        )
        return {
            "child_session_id": accepted.child_session.session_id,
            "child_job_id": accepted.job_id,
            "child_message_id": accepted.message_id,
            "status": "accepted",
            "communication_tool": "send_message_to_session",
            "message": (
                "子会话已启动。task 不会等待或回传子 Agent 的最终文本；"
                "父子双方必须通过 send_message_to_session 进行后续通信。"
            ),
        }

    return task
