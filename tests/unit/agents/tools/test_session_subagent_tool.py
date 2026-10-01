from __future__ import annotations

import pytest

from app.abstractions.session_subagent import SessionSubagentAccepted
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.session_subagent import create_session_subagent_tool
from app.core.job_context import reset_current_job_id, set_current_job_id


class _SessionSubagentService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def delegate(self, **kwargs):
        self.calls.append(kwargs)
        return SessionSubagentAccepted(
            owner_session_id="ses_parent",
            child_thread_id="thr_" + "a" * 32,
            delegation_id="del_" + "b" * 32,
            admission_idempotency_key="del_" + "b" * 32,
            admission_state="pending",
            execution_binding_id="tbind_" + "c" * 32,
            frozen_job_id="job_" + "d" * 32,
        )


@pytest.mark.asyncio
async def test_task_returns_child_thread_identity_not_subagent_final_text():
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

    # 模型可见 schema：child thread 术语，无 child_session_id/job 绑定。
    assert result["child_thread_id"] == "thr_" + "a" * 32
    assert result["owner_session_id"] == "ses_parent"
    assert result["delegation_id"] == "del_" + "b" * 32
    assert result["admission_state"] == "pending"
    assert result["status"] == "accepted"
    assert "child_session_id" not in result
    assert "child_job_id" not in result
    assert "child_message_id" not in result
    assert "最终文本" in result["message"]
    assert service.calls[0]["parent_job_id"] == "job_parent"
    assert service.calls[0]["parent_tool_call_id"] == "call_task"
    assert "runtime" not in task.get_input_schema().model_fields
