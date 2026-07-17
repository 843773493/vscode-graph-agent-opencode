from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.abstractions.session_subagent import SessionSubagentAccepted
from app.schemas.public_v2.session import SessionDTO
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.team.store import TeamStore


def _session(session_id: str, *, title: str = "会话") -> SessionDTO:
    now = datetime.now(timezone.utc)
    return SessionDTO(
        session_id=session_id,
        workspace_id="ws_test",
        title=title,
        current_agent_id="default",
        kind="normal",
        created_at=now,
        updated_at=now,
    )


class _SessionService:
    def __init__(self, sessions: list[SessionDTO]) -> None:
        self.sessions = {session.session_id: session for session in sessions}

    async def get(self, session_id: str) -> SessionDTO:
        return self.sessions[session_id]


class _Orchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_and_run(self, session_id: str, content: str, **kwargs):
        self.calls.append({"session_id": session_id, "content": content, **kwargs})
        index = len(self.calls)
        return SimpleNamespace(message_id=f"msg_{index}", job_id=f"job_{index}")


class _SubagentService:
    def __init__(self, session_service: _SessionService) -> None:
        self.session_service = session_service
        self.calls: list[dict[str, object]] = []

    async def delegate(self, **kwargs) -> SessionSubagentAccepted:
        self.calls.append(kwargs)
        child = _session(f"ses_child_{len(self.calls)}", title="团队审查员")
        self.session_service.sessions[child.session_id] = child
        before_start = kwargs["before_start"]
        await before_start(child)
        return SessionSubagentAccepted(
            child_session=child,
            message_id="msg_child_start",
            job_id="job_child_start",
        )


def _service(tmp_path, sessions: list[SessionDTO]):
    session_service = _SessionService(sessions)
    orchestrator = _Orchestrator()
    subagents = _SubagentService(session_service)
    service = TeamCoordinationService(
        store=TeamStore(workspace_root=tmp_path),
        session_service=session_service,
        session_orchestrator=orchestrator,
        session_subagent_service=subagents,
    )
    return service, session_service, orchestrator, subagents


@pytest.mark.asyncio
async def test_delegated_reviewer_uses_shared_board_and_reports_to_parent(tmp_path):
    parent = _session("ses_parent", title="开发主会话")
    service, _, orchestrator, subagents = _service(tmp_path, [parent])

    team = await service.create_team(
        requester_session_id=parent.session_id,
        name="交付循环",
    )
    member_result = await service.create_member(
        requester_session_id=parent.session_id,
        requester_agent_id="default",
        requester_job_id="job_parent",
        requester_tool_call_id="call_member",
        team_id=team.team_id,
        role="reviewer",
        startup_prompt="确认审查职责并等待任务。",
        instructions="只读审查，不修改文件。",
        work_mode="read_only",
    )

    reviewer_id = member_result.member.session_id
    assert member_result.member.source == "delegated"
    assert member_result.member.activation_job_id == "job_child_start"
    assert subagents.calls[0]["trusted_context"]["team_id"] == team.team_id
    assert subagents.calls[0]["title"] == "reviewer · 交付循环"

    assigned = await service.assign_task(
        requester_session_id=parent.session_id,
        team_id=team.team_id,
        assignee_session_id=reviewer_id,
        title="审查父会话修改",
        description="读取结果并报告问题。",
        phase="review",
        cycle=1,
        depends_on_task_ids=[],
        start_assignee=True,
    )
    assert assigned.dispatched_job_id == "job_1"
    assert orchestrator.calls[0]["session_id"] == reviewer_id

    completed = await service.update_task(
        requester_session_id=reviewer_id,
        team_id=team.team_id,
        task_id=assigned.task.task_id,
        status="completed",
        summary="REVIEW_OK：修改符合要求。",
    )

    assert completed.task.status == "completed"
    assert completed.dispatched_job_id == "job_2"
    assert orchestrator.calls[1]["session_id"] == parent.session_id
    assert '"board_update_persisted": true' in orchestrator.calls[1]["content"]
    assert "不得声称面板尚未同步" in orchestrator.calls[1]["content"]
    reviewer_board = await service.get_board(
        requester_session_id=reviewer_id,
        team_id=team.team_id,
    )
    assert {member.session_id for member in reviewer_board.members} == {
        parent.session_id,
        reviewer_id,
    }


@pytest.mark.asyncio
async def test_existing_review_session_is_attached_without_replacement(tmp_path):
    parent = _session("ses_parent", title="开发主会话")
    reviewer = _session("ses_manual_review", title="已调校三轮的审查会话")
    service, sessions, orchestrator, subagents = _service(
        tmp_path,
        [parent, reviewer],
    )
    original_session_count = len(sessions.sessions)
    original_reviewer = sessions.sessions[reviewer.session_id]
    team = await service.create_team(
        requester_session_id=parent.session_id,
        name="复用人工审查会话",
    )

    attached = await service.attach_session(
        requester_session_id=parent.session_id,
        team_id=team.team_id,
        target_session_id=reviewer.session_id,
        role="reviewer",
        instructions="沿用该会话已经形成的严格审查方案。",
        work_mode="read_only",
        notify=True,
    )

    assert len(sessions.sessions) == original_session_count
    assert sessions.sessions[reviewer.session_id] is original_reviewer
    assert attached.member.source == "attached"
    assert attached.member.session_id == reviewer.session_id
    assert subagents.calls == []
    assert orchestrator.calls[0]["session_id"] == reviewer.session_id
    assert "保留当前全部上下文和既有审查方案" in orchestrator.calls[0]["content"]

    task = await service.assign_task(
        requester_session_id=parent.session_id,
        team_id=team.team_id,
        assignee_session_id=reviewer.session_id,
        title="使用既有规则复审",
        description="按此前人工确认的审查方案检查父会话修改。",
        phase="review",
        cycle=2,
        depends_on_task_ids=[],
        start_assignee=True,
    )
    board = await service.get_board(
        requester_session_id=reviewer.session_id,
        team_id=team.team_id,
    )
    assert board.tasks[0].task_id == task.task.task_id
    assert board.members[1].source == "attached"
