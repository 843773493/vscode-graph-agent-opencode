from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.abstractions.session_subagent import SessionSubagentAccepted
from app.schemas.internal_v2.team import TeamBoardDTO
from app.services.business.message_display import project_message_for_display
from app.services.business.team.service import TeamCoordinationService


class BoardStore:
    def __init__(self):
        self.boards = {}
        self.events = []

    def create(self, board):
        return self.save(board)

    def save(self, board):
        self.boards[(board.session_id, board.team_id)] = board
        return board

    def get(self, session_id, team_id):
        return self.boards[(session_id, team_id)]

    def list(self, session_id):
        return [board for (owner, _key), board in self.boards.items() if owner == session_id]

    def append_event(self, event):
        self.events.append(event)

    def recent_events(self, session_id, team_id, *, limit=20):
        return [event for event in self.events if (event.session_id, event.team_id) == (session_id, team_id)][-limit:]


class Factory:
    def __init__(self):
        self.members = []
        self.control_store = self

    def for_owner_session(self, session_id):
        assert session_id == "ses_owner"
        return self

    def validate_owner_thread(self, session_id, thread_id):
        assert session_id == "ses_owner"
        assert thread_id == "thr_main" or any(row.child_thread_id == thread_id for row in self.members)

    def list_collaboration_members(self, *, coordinator_session_id):
        assert coordinator_session_id == "ses_owner"
        return tuple(self.members)

    def get_initial_execution_intent(self, delegation_id):
        assert any(row.delegation_id == delegation_id for row in self.members)
        return SimpleNamespace(state="bound", intent_updated_at=datetime.now(UTC))


class Sessions:
    async def get(self, session_id):
        assert session_id == "ses_owner"
        return SimpleNamespace(thread_id="thr_main")


class Subagents:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    async def delegate(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        delegation_id = f"del_{index}"
        child_thread_id = f"thr_child_{index}"
        self.factory.members.append(SimpleNamespace(
            delegation_id=delegation_id, coordinator_session_id=kwargs["parent_session_id"],
            coordinator_thread_id=kwargs["parent_thread_id"], child_thread_id=child_thread_id,
            role=kwargs["collaboration_member"]["role"], state="published", registered_at=datetime.now(UTC),
            task_seed=json.dumps({"trusted_context": kwargs["trusted_context"], "untrusted_instructions": kwargs["untrusted_instructions"]}),
        ))
        return SessionSubagentAccepted(owner_session_id=kwargs["parent_session_id"], child_thread_id=child_thread_id,
                                       delegation_id=delegation_id, admission_idempotency_key=delegation_id,
                                       admission_state="bound", execution_binding_id=f"binding_{index}", frozen_job_id=f"job_{index}")


class Orchestrator:
    def __init__(self):
        self.calls = []

    async def submit_runtime_event(self, session_id, message, *, thread_id, source_id, idempotency_key, delivery_policy="after_turn"):
        self.calls.append((session_id, thread_id, message, source_id, idempotency_key))
        return SimpleNamespace(job_id=f"notice_{len(self.calls)}")


@pytest.fixture
def team_case():
    store = BoardStore()
    factory = Factory()
    subagents = Subagents(factory)
    orchestrator = Orchestrator()
    service = TeamCoordinationService(store=store, session_service=Sessions(), session_orchestrator=orchestrator,
                                      session_subagent_service=subagents, thread_creation_factory=factory)
    return SimpleNamespace(service=service, store=store, factory=factory, subagents=subagents, orchestrator=orchestrator)


async def create_reviewer(case):
    board = await case.service.create_team(requester_session_id="ses_owner", requester_thread_id="thr_main", name="交付循环")
    member = await case.service.create_member(requester_session_id="ses_owner", requester_thread_id="thr_main",
                                              requester_agent_id="default", requester_job_id="job_parent", requester_tool_call_id="call_member",
                                              team_id=board.team_id, role="reviewer", startup_prompt="确认审查职责并等待任务。",
                                              instructions="只读审查，不修改文件。", work_mode="read_only")
    return board, member


@pytest.mark.asyncio
async def test_member_projection_and_task_notifications_use_same_session_exact_threads(team_case):
    board, accepted = await create_reviewer(team_case)
    member = accepted.member
    assert member.thread_id == "thr_child_1"
    assert member.status == "active"
    assert accepted.child_admission_state == "bound"
    frozen = team_case.subagents.calls[0]
    assert frozen["parent_thread_id"] == "thr_main"
    assert frozen["trusted_context"]["coordinator_thread_id"] == "thr_main"
    assert frozen["collaboration_member"] == {"role": "reviewer"}
    stored = team_case.store.get("ses_owner", board.team_id)
    assert stored.members == []
    task = await team_case.service.assign_task(requester_session_id="ses_owner", requester_thread_id="thr_main",
                                                team_id=board.team_id, assignee_thread_id=member.thread_id, title="审查",
                                                description="检查实现并报告。", phase="review", cycle=1,
                                                depends_on_task_ids=[], start_assignee=True)
    assert team_case.orchestrator.calls[0][:2] == ("ses_owner", member.thread_id)
    updated = await team_case.service.update_task(requester_session_id="ses_owner", requester_thread_id=member.thread_id,
                                                  team_id=board.team_id, task_id=task.task.task_id, status="completed", summary="审查通过")
    assert updated.task.status == "completed"
    assert team_case.orchestrator.calls[1][:2] == ("ses_owner", "thr_main")
    assert "审查通过" in team_case.orchestrator.calls[1][2].content
    assert "审查通过" not in project_message_for_display(team_case.orchestrator.calls[1][2].content, team_case.orchestrator.calls[1][2].metadata).content
    assert not hasattr(team_case.service, "attach_session")


@pytest.mark.asyncio
async def test_only_main_coordinator_can_create_or_assign_and_dependencies_must_complete(team_case):
    with pytest.raises(PermissionError, match="main thread"):
        await team_case.service.create_team(requester_session_id="ses_owner", requester_thread_id="thr_child", name="非法团队")
    board, member = await create_reviewer(team_case)
    with pytest.raises(PermissionError, match="协调者"):
        await team_case.service.create_member(requester_session_id="ses_owner", requester_thread_id=member.member.thread_id,
                                              requester_agent_id="default", requester_job_id="job_member", requester_tool_call_id="call_other",
                                              team_id=board.team_id, role="tester", startup_prompt="测试", instructions="", work_mode="read_only")
    with pytest.raises(ValueError, match="依赖任务不存在"):
        await team_case.service.assign_task(requester_session_id="ses_owner", requester_thread_id="thr_main",
                                             team_id=board.team_id, assignee_thread_id=member.member.thread_id, title="检查", description="测试",
                                             phase="test", cycle=1, depends_on_task_ids=["missing"], start_assignee=True)


@pytest.mark.asyncio
async def test_restart_projection_uses_sqlite_readiness_and_never_json_activation(team_case):
    board, member = await create_reviewer(team_case)
    fresh = TeamCoordinationService(store=team_case.store, session_service=Sessions(), session_orchestrator=team_case.orchestrator,
                                    session_subagent_service=team_case.subagents, thread_creation_factory=team_case.factory)
    restored: TeamBoardDTO = await fresh.get_board(requester_session_id="ses_owner", requester_thread_id=member.member.thread_id, team_id=board.team_id)
    assert [item.thread_id for item in restored.members] == ["thr_main", member.member.thread_id]
    team_case.factory.members[0].state = "cancelled"
    with pytest.raises(PermissionError):
        await fresh.get_board(requester_session_id="ses_owner", requester_thread_id=member.member.thread_id, team_id=board.team_id)
