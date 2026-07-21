from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.public_v2.message import MessageRunAccepted
from app.schemas.public_v2.job import JobDispatchSnapshotDTO
from app.schemas.public_v2.session import SessionDTO, SessionDelegationDTO
from app.services.orchestration.session_subagent_service import (
    SessionSubagentService,
)


class _SessionService:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.parent = SessionDTO(
            session_id="ses_parent",
            workspace_id="ws_local",
            title="父会话",
            current_agent_id="default",
            created_at=now,
            updated_at=now,
        )
        self.created_requests: list[dict[str, str]] = []
        self.delegation_updates: list[tuple[str, str, str | None]] = []
        self.child: SessionDTO | None = None

    async def get(self, session_id: str) -> SessionDTO:
        assert session_id == self.parent.session_id
        return self.parent

    async def create_delegated(self, **request: str) -> SessionDTO:
        self.created_requests.append(request)
        now = datetime.now(timezone.utc)
        self.child = SessionDTO(
            session_id="ses_child",
            workspace_id="ws_local",
            title=request["title"],
            title_source="auto",
            current_agent_id=request["agent_id"],
            parent_session_id=request["parent_session_id"],
            kind="delegated",
            delegation=SessionDelegationDTO(
                parent_session_id=request["parent_session_id"],
                parent_job_id=request["parent_job_id"],
                parent_tool_call_id=request["parent_tool_call_id"],
                subagent_type=request["subagent_type"],
            ),
            created_at=now,
            updated_at=now,
        )
        return self.child

    async def set_delegation_start_result(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> SessionDTO:
        self.delegation_updates.append((session_id, status, error))
        child = self.child
        assert child is not None
        assert child.delegation is not None
        child.delegation.start_status = status
        child.delegation.start_error = error
        return child


class _SessionOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def create_and_run(self, session_id: str, content: str, **kwargs):
        self.calls.append((session_id, content, kwargs))
        return MessageRunAccepted(
            message_id="msg_child",
            job_id="job_child",
            status="running",
            dispatch=JobDispatchSnapshotDTO(
                session_id=session_id,
                job_id="job_child",
                job_status="running",
                active_job_id="job_child",
                queued_jobs_ahead=0,
                queued_job_count=0,
                pending_job_count=1,
            ),
        )


class _FailingSessionOrchestrator:
    async def create_and_run(self, session_id: str, content: str, **kwargs):
        raise RuntimeError("调度器不可用")


@pytest.mark.asyncio
async def test_delegate_creates_fresh_child_session_and_starts_independent_job():
    sessions = _SessionService()
    orchestrator = _SessionOrchestrator()
    service = SessionSubagentService(
        session_service=sessions,
        session_orchestrator=orchestrator,
    )

    accepted = await service.delegate(
        parent_session_id="ses_parent",
        parent_agent_id="default",
        parent_job_id="job_parent",
        parent_tool_call_id="call_task",
        description="检查认证模块，并把结论发回父会话。",
        subagent_type="general-purpose",
        title="认证审查员",
    )

    child = accepted.child_session
    assert child.session_id == "ses_child"
    assert child.parent_session_id == "ses_parent"
    assert child.kind == "delegated"
    assert child.delegation is not None
    assert child.delegation.parent_job_id == "job_parent"
    assert child.delegation.parent_tool_call_id == "call_task"
    assert accepted.message_id == "msg_child"
    assert accepted.job_id == "job_child"

    create_request = sessions.created_requests[0]
    assert create_request["parent_session_id"] == "ses_parent"
    assert create_request["title"] == "委派：认证审查员"
    assert sessions.delegation_updates == [("ses_child", "running", None)]
    assert orchestrator.calls[0][0] == "ses_child"
    delegation_content = orchestrator.calls[0][1]
    assert "send_message_to_session" in delegation_content
    assert '"target_session_id": "ses_parent"' in delegation_content
    assert "不要假设本会话的普通最终回复会自动返回父 Agent" in delegation_content
    assert "检查认证模块" in delegation_content
    assert "message_role" not in orchestrator.calls[0][2]
    assert (
        orchestrator.calls[0][2]["metadata"]["source"]
        == "session_subagent_delegation"
    )


@pytest.mark.asyncio
async def test_delegate_rejects_unknown_subagent_type_before_creating_session():
    sessions = _SessionService()
    service = SessionSubagentService(
        session_service=sessions,
        session_orchestrator=_SessionOrchestrator(),
    )

    with pytest.raises(ValueError, match="当前仅支持 general-purpose"):
        await service.delegate(
            parent_session_id="ses_parent",
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="unknown",
        )

    assert sessions.created_requests == []


@pytest.mark.asyncio
async def test_delegate_start_failure_exposes_created_child_session_id():
    sessions = _SessionService()
    service = SessionSubagentService(
        session_service=sessions,
        session_orchestrator=_FailingSessionOrchestrator(),
    )

    with pytest.raises(RuntimeError, match="child_session_id=ses_child"):
        await service.delegate(
            parent_session_id="ses_parent",
            parent_agent_id="default",
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            description="做事",
            subagent_type="general-purpose",
        )
    assert sessions.delegation_updates == [
        ("ses_child", "failed", "调度器不可用")
    ]
