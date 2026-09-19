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
            "为复杂、可独立执行的工作在当前会话内创建一个持久化 child thread。"
            "本工具返回 child_thread_id 与委派 admission 状态（pending），"
            "不会把子 Agent 最终文本作为隐藏返回值带回；"
            "不要为寒暄、简单问题或单步操作创建 child thread。"
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
            "child_thread_id": accepted.child_thread_id,
            "owner_session_id": accepted.owner_session_id,
            "delegation_id": accepted.delegation_id,
            "admission_state": accepted.admission_state,
            "status": "accepted",
            "message": (
                "child thread 已在当前会话内创建，初始执行 admission 处于 "
                "pending，由后端执行面绑定后开始运行。task 不会等待或回传 "
                "child Agent 的最终文本；与 child thread 的直接通信能力由 "
                "后续 child-thread 执行面提供。"
            ),
        }

    return task
