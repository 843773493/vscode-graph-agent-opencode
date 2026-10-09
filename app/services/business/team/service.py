from __future__ import annotations

import json

from app.abstractions.session_subagent import (
    OwnerThreadCreationFactoryProtocol,
    SessionReaderProtocol,
    SessionSubagentProtocol,
)
from app.abstractions.team import TeamFanoutProtocol, TeamStoreProtocol
from app.core.session_control_store.collaboration_fanout import (
    CollaborationFanoutRecord,
)
from app.schemas.internal_v2.team import (
    TeamBoardDTO,
    TeamListDTO,
    TeamMemberOperationDTO,
    TeamTaskOperationDTO,
    TeamTaskPhase,
    TeamTaskStatus,
    TeamWorkMode,
)

from .board_manager import TeamBoardManager
from .messages import trusted_team_context
from .rules import find_task, require_active_member, required_text


class TeamCoordinationService:
    """同一 Session 的成员 publication、任务面板与精确线程通知。"""

    def __init__(self, *, store: TeamStoreProtocol, session_service: SessionReaderProtocol,
                 fanout: TeamFanoutProtocol,
                 session_subagent_service: SessionSubagentProtocol,
                 thread_creation_factory: OwnerThreadCreationFactoryProtocol) -> None:
        self._boards = TeamBoardManager(store=store, factory=thread_creation_factory)
        self._sessions = session_service
        self._fanout = fanout
        self._subagents = session_subagent_service
        self._factory = thread_creation_factory

    async def create_team(self, *, requester_session_id: str, requester_thread_id: str, name: str) -> TeamBoardDTO:
        session = await self._sessions.get(requester_session_id)
        if requester_thread_id != session.thread_id:
            raise PermissionError("只有当前 Session 的 main thread 可以创建团队")
        self._factory.validate_owner_thread(requester_session_id, requester_thread_id)
        return await self._boards.create(session_id=requester_session_id, coordinator_thread_id=requester_thread_id, name=name)

    async def list_teams(self, *, requester_session_id: str, requester_thread_id: str) -> TeamListDTO:
        self._factory.validate_owner_thread(requester_session_id, requester_thread_id)
        return self._boards.list_for_member(requester_session_id, requester_thread_id)

    async def get_board(self, *, requester_session_id: str, requester_thread_id: str, team_id: str) -> TeamBoardDTO:
        return self._boards.get_for_member(session_id=requester_session_id, thread_id=requester_thread_id, team_id=team_id)

    async def create_member(self, *, requester_session_id: str, requester_thread_id: str,
                            requester_agent_id: str, requester_job_id: str, requester_tool_call_id: str,
                            team_id: str, role: str, startup_prompt: str, instructions: str,
                            work_mode: TeamWorkMode) -> TeamMemberOperationDTO:
        board = self._boards.get_for_coordinator(session_id=requester_session_id, thread_id=requester_thread_id, team_id=team_id)
        normalized_role = required_text(role, "role")
        accepted = await self._subagents.delegate(
            parent_session_id=requester_session_id, parent_thread_id=requester_thread_id,
            parent_agent_id=requester_agent_id, parent_job_id=requester_job_id,
            parent_tool_call_id=requester_tool_call_id, description=required_text(startup_prompt, "startup_prompt"),
            subagent_type="general-purpose", title=f"{normalized_role} · {board.name}",
            trusted_context=trusted_team_context(team_id=team_id, coordinator_thread_id=board.coordinator_thread_id,
                                                role=normalized_role, work_mode=work_mode),
            untrusted_instructions=instructions,
            collaboration_member={"role": normalized_role},
        )
        board = self._boards.get_for_coordinator(session_id=requester_session_id, thread_id=requester_thread_id, team_id=team_id)
        return TeamMemberOperationDTO(
            board=board, member=require_active_member(board, accepted.child_thread_id),
            child_thread_id=accepted.child_thread_id, child_delegation_id=accepted.delegation_id,
            child_admission_state=accepted.admission_state,
        )

    async def assign_task(self, *, requester_session_id: str, requester_thread_id: str, team_id: str,
                          assignee_thread_id: str, title: str, description: str, phase: TeamTaskPhase,
                          cycle: int, depends_on_task_ids: list[str], start_assignee: bool) -> TeamTaskOperationDTO:
        if start_assignee and assignee_thread_id == requester_thread_id:
            raise ValueError("当前线程登记自行执行的任务时必须使用 start_assignee=false")
        board, task, revision = await self._boards.assign_task(
            requester_session_id=requester_session_id, requester_thread_id=requester_thread_id,
            team_id=team_id, assignee_thread_id=assignee_thread_id, title=title, description=description,
            phase=phase, cycle=cycle, depends_on_task_ids=depends_on_task_ids, start_assignee=start_assignee,
        )
        job_id = None
        if start_assignee:
            try:
                job_id = await self._fanout.dispatch(requester_session_id, revision)
            except Exception as error:
                await self._boards.set_task_dispatch_failure(
                    session_id=requester_session_id, team_id=team_id, task_id=task.task_id,
                    actor_thread_id=requester_thread_id, error=str(error),
                )
                raise RuntimeError(f"团队任务已记录，目标线程调度失败: team_id={team_id} task_id={task.task_id}: {error}") from error
            board = self._boards.get_for_coordinator(session_id=requester_session_id, thread_id=requester_thread_id, team_id=team_id)
            task = find_task(board, task.task_id)
        return TeamTaskOperationDTO(board=board, task=task, dispatched_job_id=job_id)

    async def update_task(self, *, requester_session_id: str, requester_thread_id: str, team_id: str,
                          task_id: str, status: TeamTaskStatus, summary: str) -> TeamTaskOperationDTO:
        board, task, revision = await self._boards.update_task(
            requester_session_id=requester_session_id, requester_thread_id=requester_thread_id,
            team_id=team_id, task_id=task_id, status=status, summary=summary,
        )
        job_id = await self._fanout.dispatch(requester_session_id, revision)
        return TeamTaskOperationDTO(board=board, task=task, dispatched_job_id=job_id)

    async def finalize_wakeup(self, record: CollaborationFanoutRecord, job_id: str) -> None:
        """消费原 fanout 回执；恢复不得把任务换绑到另一 Job。"""
        wakeup = json.loads(record.wakeup_json)
        metadata = wakeup["metadata"]
        if metadata["source"] == "team_task_update":
            return
        if metadata["source"] != "team_task_assignment":
            raise RuntimeError("collaboration wakeup 不是既有团队任务通知")
        board = next(board for board in json.loads(record.payload_json)["boards"]
                     if board["team_id"] == metadata["team_id"])
        await self._boards.set_task_dispatched(
            session_id=record.session_id, team_id=metadata["team_id"], task_id=metadata["team_task_id"],
            actor_thread_id=board["coordinator_thread_id"], job_id=job_id,
        )
