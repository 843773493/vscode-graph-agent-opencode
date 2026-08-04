from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.agents.tools.session_subagent import create_session_subagent_tool
from app.abstractions.session_subagent import SessionSubagentAccepted
from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.schemas.public_v2.session import SessionDTO, SessionDelegationDTO


class _SessionSubagentService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def delegate(self, **kwargs):
        self.calls.append(kwargs)
        now = datetime.now(timezone.utc)
        return SessionSubagentAccepted(
            child_session=SessionDTO(
                session_id="ses_child",
                workspace_id="ws_local",
                title="委派测试",
                current_agent_id="default",
                parent_session_id="ses_parent",
                kind="delegated",
                delegation=SessionDelegationDTO(
                    parent_session_id="ses_parent",
                    parent_job_id="job_parent",
                    parent_tool_call_id="call_task",
                    subagent_type="general-purpose",
                    start_status="running",
                ),
                created_at=now,
                updated_at=now,
            ),
            message_id="msg_child",
            job_id="job_child",
        )


@pytest.mark.asyncio
async def test_task_returns_identity_not_subagent_final_text():
    service = _SessionSubagentService()
    invocation_context = ToolInvocationContext()
    task = create_session_subagent_tool(
        parent_session_id="ses_parent",
        parent_agent_id="default",
        session_subagent_service=service,
        invocation_context=invocation_context,
    )
    job_token = set_current_job_id("job_parent")
    tool_token = invocation_context.set_tool_call_id("call_task")
    try:
        assert task.coroutine is not None
        result = await task.coroutine(
            description="独立检查代码",
            subagent_type="general-purpose",
        )
    finally:
        invocation_context.reset_tool_call_id(tool_token)
        reset_current_job_id(job_token)

    assert result["child_session_id"] == "ses_child"
    assert result["child_job_id"] == "job_child"
    assert result["communication_tool"] == "send_message_to_session"
    assert "最终文本" in result["message"]
    assert service.calls[0]["parent_job_id"] == "job_parent"
    assert service.calls[0]["parent_tool_call_id"] == "call_task"
    assert "runtime" not in task.get_input_schema().model_fields
