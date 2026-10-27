from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.abstractions.session_subagent import SessionSubagentAccepted
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.session_subagent import create_session_subagent_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.session import SessionTaskContext
from app.services.orchestration.session_subagent_service import (
    SessionSubagentUnavailableError,
)


@pytest.fixture(autouse=True)
def parent_admission():
    admission = ExecutionAdmission(
        session_id="ses_parent", thread_id="thr_parent", execution_id="exec_parent",
        job_id="job_parent", admission_idempotency_key="admission_parent", execution_origin="internal", turn_id=None,
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(admission.session_id, admission.thread_id), langgraph_thread_id=admission.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=admission.thread_id),
    ):
        yield


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
    assert service.calls[0]["context"] is None
    assert "runtime" not in task.get_input_schema().model_fields


@pytest.mark.parametrize("context", [
    {"mode": "context_fork"},
    {"mode": "empty"},
])
async def test_task_schema_passes_typed_context(context):
    service = _SessionSubagentService()
    invocation = ToolInvocationContext()
    task = create_session_subagent_tool(
        parent_session_id="ses_parent", parent_agent_id="default",
        session_subagent_service=service, invocation_context=invocation,
    )
    job_token = set_current_job_id("job_parent")
    tool_token = invocation.set_tool_call_id("call_context")
    try:
        await task.ainvoke({"description": "检查上下文", "context": context})
    finally:
        invocation.reset_tool_call_id(tool_token)
        reset_current_job_id(job_token)
    assert service.calls[0]["context"] == SessionTaskContext.model_validate(context)


@pytest.mark.parametrize("context", [
    {"mode": "view_prefix_fork"},
    {"mode": "view_prefix_fork", "source_view_id": "v", "source_view_revision": 1},
    {"mode": "empty", "anchor_item_id": "i"},
    {"mode": "context_fork", "anchor_mode": "before"},
    {"mode": "context_fork", "source_view_id": "v"},
    {"mode": "full_rollout_copy"},
    {"mode": "view_prefix_fork", "source_view_id": "v", "source_view_revision": True, "anchor_item_id": "i"},
    {"mode": "empty", "source_uri": "another-thread"},
])
async def test_task_context_rejects_ambiguous_or_foreign_selection(context):
    service = _SessionSubagentService()
    task = create_session_subagent_tool(
        parent_session_id="ses_parent", parent_agent_id="default",
        session_subagent_service=service, invocation_context=ToolInvocationContext(),
    )
    with pytest.raises(ValidationError):
        await task.ainvoke({"description": "非法上下文应在创建前拒绝", "context": context})
    assert service.calls == []


@pytest.mark.asyncio
async def test_task_tool_propagates_fail_closed_when_no_real_binder():
    """后端未装配真实 binder 时 task 工具必须报错，不得返回虚假 accepted。"""

    class _UnavailableService:
        async def delegate(self, **kwargs):
            raise SessionSubagentUnavailableError(
                delegation_id="del_" + "b" * 32,
                parent_session_id=kwargs["parent_session_id"],
                subagent_type=kwargs["subagent_type"],
            )

    invocation_context = ToolInvocationContext()
    task = create_session_subagent_tool(
        parent_session_id="ses_parent",
        parent_agent_id="default",
        session_subagent_service=_UnavailableService(),
        invocation_context=invocation_context,
    )
    job_token = set_current_job_id("job_parent")
    tool_token = invocation_context.set_tool_call_id("call_task")
    try:
        assert task.coroutine is not None
        with pytest.raises(SessionSubagentUnavailableError, match="unavailable"):
            await task.coroutine(
                description="独立检查代码",
                subagent_type="general-purpose",
            )
    finally:
        invocation_context.reset_tool_call_id(tool_token)
        reset_current_job_id(job_token)
