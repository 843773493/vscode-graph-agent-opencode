from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.session_subagent import SessionSubagentAccepted
from app.core.session_control_store.collaboration_fanout import (
    CollaborationFanoutRecord,
)
from app.schemas.internal_v2.team import TeamBoardDTO
from app.services.business.message_display import project_message_for_display
from app.services.business.team.service import TeamCoordinationService


class BoardStore:
    def __init__(self):
        self.boards = {}
        self.events = []
        self.commits = []

    async def commit(self, board, event, *, wakeup=None):
        board = board.model_copy(update={"members": [], "recent_events": []})
        self.boards[(board.session_id, board.team_id)] = board
        self.events.append(event)
        self.commits.append((board, wakeup))
        return len(self.commits)

    def get(self, session_id, team_id):
        return self.boards[(session_id, team_id)]

    def list(self, session_id):
        return [board for (owner, _key), board in self.boards.items() if owner == session_id]

    def recent_events(self, session_id, team_id, *, limit=20):
        return [event for event in self.events if (event.session_id, event.team_id) == (session_id, team_id)][-limit:]

    def latest_task_event(self, session_id, team_id, task_id):
        return next((event for event in reversed(self.events)
                     if (event.session_id, event.team_id, event.payload.get("task_id")) == (session_id, team_id, task_id)), None)


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

    def read_transaction(self):
        return nullcontext()

    def get_initial_execution_intent(self, creation_idempotency_key):
        member = next(
            row
            for row in self.members
            if row.creation_idempotency_key == creation_idempotency_key
        )
        return SimpleNamespace(
            state="bound",
            thread_id=member.child_thread_id,
            intent_updated_at=datetime.now(UTC),
        )

    def validate_initial_execution_readiness(self, intent):
        assert any(
            row.state == "published" and row.child_thread_id == intent.thread_id
            for row in self.members
        )


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
            creation_idempotency_key=delegation_id,
            coordinator_thread_id=kwargs["parent_thread_id"], child_thread_id=child_thread_id,
            role=kwargs["collaboration_member"]["role"], state="published", registered_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            task_seed=json.dumps({"trusted_context": kwargs["trusted_context"], "untrusted_instructions": kwargs["untrusted_instructions"]}),
        ))
        return SessionSubagentAccepted(owner_session_id=kwargs["parent_session_id"], child_thread_id=child_thread_id,
                                       delegation_id=delegation_id, admission_idempotency_key=delegation_id,
                                       admission_state="bound", execution_binding_id=f"binding_{index}", frozen_job_id=f"job_{index}")


class Fanout:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.finalizer = None

    async def dispatch(self, session_id, revision):
        board, wakeup = self.store.commits[revision - 1]
        if wakeup is None:
            return None
        self.calls.append((session_id, wakeup["target_thread_id"], PreparedInternalMessage(wakeup["content"], wakeup["metadata"]),
                           wakeup["source_id"], wakeup["idempotency_key"]))
        job_id = f"notice_{len(self.calls)}"
        record = CollaborationFanoutRecord(revision, session_id, "hash", json.dumps({"boards": [board.model_dump(mode="json")]}),
                                           "lease", json.dumps(wakeup), None, "pending", ())
        await self.finalizer(record, job_id)
        return job_id


@pytest.fixture
def team_case():
    store = BoardStore()
    factory = Factory()
    subagents = Subagents(factory)
    fanout = Fanout(store)
    service = TeamCoordinationService(store=store, session_service=Sessions(), fanout=fanout,
                                      session_subagent_service=subagents, thread_creation_factory=factory)
    fanout.finalizer = service.finalize_wakeup
    return SimpleNamespace(service=service, store=store, factory=factory, subagents=subagents, fanout=fanout)


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
    assert team_case.fanout.calls[0][:2] == ("ses_owner", member.thread_id)
    assert task.task.assigned_job_id == task.dispatched_job_id == "notice_1"
    updated = await team_case.service.update_task(requester_session_id="ses_owner", requester_thread_id=member.thread_id,
                                                  team_id=board.team_id, task_id=task.task.task_id, status="completed", summary="审查通过")
    assert updated.task.status == "completed"
    assert team_case.fanout.calls[1][:2] == ("ses_owner", "thr_main")
    assert "审查通过" in team_case.fanout.calls[1][2].content
    assert "审查通过" not in project_message_for_display(team_case.fanout.calls[1][2].content, team_case.fanout.calls[1][2].metadata).content
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
    fresh = TeamCoordinationService(store=team_case.store, session_service=Sessions(), fanout=team_case.fanout,
                                    session_subagent_service=team_case.subagents, thread_creation_factory=team_case.factory)
    restored: TeamBoardDTO = await fresh.get_board(requester_session_id="ses_owner", requester_thread_id=member.member.thread_id, team_id=board.team_id)
    assert [item.thread_id for item in restored.members] == ["thr_main", member.member.thread_id]
    team_case.factory.members[0].state = "cancelled"
    with pytest.raises(PermissionError):
        await fresh.get_board(requester_session_id="ses_owner", requester_thread_id=member.member.thread_id, team_id=board.team_id)


@pytest.mark.asyncio
async def test_list_teams_reads_member_ledger_once_and_filters_other_teams(
    team_case,
    monkeypatch,
):
    first_board, first_member = await create_reviewer(team_case)
    await create_reviewer(team_case)
    list_members = team_case.factory.list_collaboration_members
    list_member_calls = 0

    def count_member_reads(*, coordinator_session_id):
        nonlocal list_member_calls
        list_member_calls += 1
        return list_members(coordinator_session_id=coordinator_session_id)

    monkeypatch.setattr(team_case.factory, "list_collaboration_members", count_member_reads)

    listed = await team_case.service.list_teams(
        requester_session_id="ses_owner",
        requester_thread_id=first_member.member.thread_id,
    )

    assert [board.team_id for board in listed.items] == [first_board.team_id]
    assert [member.thread_id for member in listed.items[0].members] == [
        "thr_main",
        first_member.member.thread_id,
    ]
    assert list_member_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_update", [False, True])
async def test_original_dispatch_recovery_clears_only_its_failure_after_long_event_history(team_case, monkeypatch, user_update):
    case = team_case
    board, member = await create_reviewer(case)

    async def failed_dispatch(_session_id, _revision):
        raise OSError("原调度尚未绑定")

    monkeypatch.setattr(case.fanout, "dispatch", failed_dispatch)
    with pytest.raises(RuntimeError, match="目标线程调度失败"):
        await case.service.assign_task(requester_session_id="ses_owner", requester_thread_id="thr_main",
            team_id=board.team_id, assignee_thread_id=member.member.thread_id, title="恢复任务", description="原始任务",
            phase="test", cycle=1, depends_on_task_ids=[], start_assignee=True)
    failed = case.store.get("ses_owner", board.team_id)
    task = failed.tasks[0]
    assert task.status == "failed" and task.assigned_job_id is None
    await case.service._boards.set_task_dispatch_failure(session_id="ses_owner", team_id=board.team_id,
        task_id=task.task_id, actor_thread_id="thr_main", error="同一调度第二次失败")
    if user_update:
        current = case.store.get("ses_owner", board.team_id)
        changed = task.model_copy(update={"status": "failed", "summary": "用户已确认任务失败", "error": "用户原因"})
        await case.store.commit(current.model_copy(update={"version": current.version + 1, "tasks": [changed]}),
            case.store.events[-1].model_copy(update={"type": "task.updated",
                "payload": {"task_id": task.task_id, "status": "failed", "summary": changed.summary}}))
    # 无关任务事件超过展示页，不能抹掉原任务的恢复因果。
    for index in range(25):
        current = case.store.get("ses_owner", board.team_id)
        await case.store.commit(current.model_copy(update={"version": current.version + 1}),
            case.store.events[-1].model_copy(update={"type": "task.updated", "payload": {"task_id": f"other-{index}"}}))
    original, wakeup = next((item, wakeup) for item, wakeup in case.store.commits if wakeup is not None)
    record = CollaborationFanoutRecord(2, "ses_owner", "hash", json.dumps({"boards": [original.model_dump(mode="json")]}),
        "lease", json.dumps(wakeup), None, "pending", ())
    fresh = TeamCoordinationService(store=case.store, session_service=Sessions(), fanout=case.fanout,
        session_subagent_service=case.subagents, thread_creation_factory=case.factory)
    await fresh.finalize_wakeup(record, "original-job")
    restored = case.store.get("ses_owner", board.team_id).tasks[0]
    assert restored.assigned_job_id == "original-job"
    assert (restored.status, restored.summary, restored.error) == (
        ("failed", "用户已确认任务失败", "用户原因") if user_update else ("in_progress", None, None)
    )
    version = case.store.get("ses_owner", board.team_id).version
    await fresh.finalize_wakeup(record, "original-job")
    assert case.store.get("ses_owner", board.team_id).version == version
    with pytest.raises(RuntimeError, match="其它 Job"):
        await fresh.finalize_wakeup(record, "replacement-job")
