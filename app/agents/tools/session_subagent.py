from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import Field

from app.abstractions.session_subagent import (
    GENERAL_PURPOSE_SUBAGENT,
    SessionSubagentProtocol,
)
from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.execution_admission_context import require_current_execution_admission
from app.core.job_context import get_current_job_id
from app.schemas.internal_v2.session import SessionTaskContext


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
            "默认空历史；context.mode=context_fork 继承当前 Thread 最近完成的安全上下文。"
            "本工具返回 child_thread_id 与真实委派 admission 状态，"
            "结果稍后由后端自动回报父 Thread。当后端未装配真实 "
            "thread binder 时本工具直接报错（fail closed），不会创建无法执行 "
            "的 child thread；"
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
        context: Annotated[
            SessionTaskContext | None,
            Field(description="上下文初始化选择；省略或 empty 为空历史。仅继承当前调用 Thread，不复制活跃执行或 Goal。"),
        ] = None,
    ) -> dict[str, Any]:
        tool_call_id = invocation_context.require_tool_call_id()
        parent_job_id = get_current_job_id()
        if not parent_job_id:
            raise RuntimeError("task 工具调用缺少当前 job_id")

        accepted = await session_subagent_service.delegate(
            parent_session_id=parent_session_id,
            parent_thread_id=require_current_execution_admission().thread_id,
            parent_agent_id=parent_agent_id,
            parent_job_id=parent_job_id,
            parent_tool_call_id=tool_call_id,
            description=description,
            subagent_type=subagent_type,
            context=context,
        )
        return {
            "child_thread_id": accepted.child_thread_id,
            "owner_session_id": accepted.owner_session_id,
            "delegation_id": accepted.delegation_id,
            "admission_state": accepted.admission_state,
            "status": "accepted",
            "message": (
                "child thread 已在当前会话内创建，初始执行由后端绑定运行。"
                "task 不等待最终文本；子 Agent 结果稍后自动回报创建它的父 Thread。"
            ),
        }

    return task
