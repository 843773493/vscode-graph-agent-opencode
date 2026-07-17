from __future__ import annotations

from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_subagent import SessionStoreProtocol, SessionSubagentProtocol
from app.abstractions.team import TeamStoreProtocol
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.session import SessionDTO
from app.schemas.public_v2.team import (
    TeamBoardDTO,
    TeamListDTO,
    TeamMemberOperationDTO,
    TeamTaskOperationDTO,
    TeamTaskPhase,
    TeamTaskStatus,
    TeamWorkMode,
)

from .board_manager import TeamBoardManager
from .messages import (
    membership_message,
    task_assignment_message,
    task_update_message,
    trusted_team_context,
)
from .rules import require_active_member, required_text


COORDINATOR_NOTIFICATION_STATUSES = {"blocked", "completed", "failed"}


class TeamCoordinationService:
    """把团队任务板操作转换为真实的持久化 Session Job。"""

    def __init__(
        self,
        *,
        store: TeamStoreProtocol,
        session_service: SessionStoreProtocol,
        session_orchestrator: SessionOrchestratorProtocol,
        session_subagent_service: SessionSubagentProtocol,
    ) -> None:
        self._boards = TeamBoardManager(store=store)
        self._session_service = session_service
        self._session_orchestrator = session_orchestrator
        self._session_subagent_service = session_subagent_service

    async def create_team(
        self,
        *,
        requester_session_id: str,
        name: str,
    ) -> TeamBoardDTO:
        await self._session_service.get(requester_session_id)
        return self._boards.create(
            coordinator_session_id=requester_session_id,
            name=name,
        )

    async def list_teams(self, *, requester_session_id: str) -> TeamListDTO:
        await self._session_service.get(requester_session_id)
        return self._boards.list_for_session(requester_session_id)

    async def get_board(
        self,
        *,
        requester_session_id: str,
        team_id: str,
    ) -> TeamBoardDTO:
        return self._boards.get_for_member(
            team_id=team_id,
            session_id=requester_session_id,
        )

    async def create_member(
        self,
        *,
        requester_session_id: str,
        requester_agent_id: str,
        requester_job_id: str,
        requester_tool_call_id: str,
        team_id: str,
        role: str,
        startup_prompt: str,
        instructions: str,
        work_mode: TeamWorkMode,
    ) -> TeamMemberOperationDTO:
        board = self._boards.get_for_coordinator(
            team_id=team_id,
            session_id=requester_session_id,
        )
        normalized_role = required_text(role, "role")
        normalized_startup_prompt = required_text(
            startup_prompt,
            "startup_prompt",
        )
        member_session_id: str | None = None

        async def add_member_before_start(child_session: SessionDTO) -> None:
            nonlocal member_session_id
            await self._boards.add_member(
                team_id=team_id,
                actor_session_id=requester_session_id,
                target_session_id=child_session.session_id,
                role=normalized_role,
                source="delegated",
                work_mode=work_mode,
                instructions=instructions,
            )
            member_session_id = child_session.session_id

        try:
            accepted = await self._session_subagent_service.delegate(
                parent_session_id=requester_session_id,
                parent_agent_id=requester_agent_id,
                parent_job_id=requester_job_id,
                parent_tool_call_id=requester_tool_call_id,
                description=normalized_startup_prompt,
                subagent_type="general-purpose",
                title=f"{normalized_role} · {board.name}",
                trusted_context=trusted_team_context(
                    team_id=team_id,
                    coordinator_session_id=board.coordinator_session_id,
                    role=normalized_role,
                    work_mode=work_mode,
                    instructions=instructions,
                ),
                before_start=add_member_before_start,
            )
        except Exception as error:
            if member_session_id is not None:
                await self._boards.set_member_activation(
                    team_id=team_id,
                    actor_session_id=requester_session_id,
                    target_session_id=member_session_id,
                    status="activation_failed",
                    activation_error=str(error),
                )
            raise

        board = await self._boards.set_member_activation(
            team_id=team_id,
            actor_session_id=requester_session_id,
            target_session_id=accepted.child_session.session_id,
            status="active",
            activation_job_id=accepted.job_id,
        )
        member = require_active_member(
            board,
            accepted.child_session.session_id,
        )
        return TeamMemberOperationDTO(
            board=self._boards.with_recent_events(board),
            member=member,
            child_session_id=accepted.child_session.session_id,
            child_message_id=accepted.message_id,
            child_job_id=accepted.job_id,
        )

    async def attach_session(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        target_session_id: str,
        role: str,
        instructions: str,
        work_mode: TeamWorkMode,
        notify: bool,
    ) -> TeamMemberOperationDTO:
        self._boards.get_for_coordinator(
            team_id=team_id,
            session_id=requester_session_id,
        )
        await self._session_service.get(target_session_id)
        board = await self._boards.add_member(
            team_id=team_id,
            actor_session_id=requester_session_id,
            target_session_id=target_session_id,
            role=role,
            source="attached",
            work_mode=work_mode,
            instructions=instructions,
        )
        accepted = None
        if notify:
            member = require_active_member(board, target_session_id)
            try:
                accepted = await self._session_orchestrator.create_and_run(
                    target_session_id,
                    membership_message(board=board, member=member),
                    message_role=MessageRole.system,
                    metadata={
                        "source": "team_membership_attached",
                        "team_id": team_id,
                        "coordinator_session_id": requester_session_id,
                        "role": member.role,
                    },
                )
            except Exception as error:
                await self._boards.set_member_activation(
                    team_id=team_id,
                    actor_session_id=requester_session_id,
                    target_session_id=target_session_id,
                    status="activation_failed",
                    activation_error=str(error),
                )
                raise RuntimeError(
                    "会话已加入团队，但成员激活 Job 启动失败: "
                    f"team_id={team_id} session_id={target_session_id} error={error}"
                ) from error
            board = await self._boards.set_member_activation(
                team_id=team_id,
                actor_session_id=requester_session_id,
                target_session_id=target_session_id,
                status="active",
                activation_job_id=accepted.job_id,
            )
        member = require_active_member(board, target_session_id)
        return TeamMemberOperationDTO(
            board=self._boards.with_recent_events(board),
            member=member,
            child_session_id=target_session_id,
            child_message_id=accepted.message_id if accepted is not None else None,
            child_job_id=accepted.job_id if accepted is not None else None,
        )

    async def assign_task(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        assignee_session_id: str,
        title: str,
        description: str,
        phase: TeamTaskPhase,
        cycle: int,
        depends_on_task_ids: list[str],
        start_assignee: bool,
    ) -> TeamTaskOperationDTO:
        if start_assignee and assignee_session_id == requester_session_id:
            raise ValueError(
                "不能在当前 Job 中再次启动当前 Session；"
                "主会话自行执行的开发任务请使用 start_assignee=false"
            )
        board, task = await self._boards.assign_task(
            requester_session_id=requester_session_id,
            team_id=team_id,
            assignee_session_id=assignee_session_id,
            title=title,
            description=description,
            phase=phase,
            cycle=cycle,
            depends_on_task_ids=depends_on_task_ids,
            start_assignee=start_assignee,
        )

        accepted = None
        if start_assignee:
            try:
                accepted = await self._session_orchestrator.create_and_run(
                    assignee_session_id,
                    task_assignment_message(board=board, task=task),
                    message_role=MessageRole.system,
                    metadata={
                        "source": "team_task_assignment",
                        "team_id": team_id,
                        "team_task_id": task.task_id,
                        "coordinator_session_id": board.coordinator_session_id,
                    },
                )
            except Exception as error:
                await self._boards.set_task_dispatch_failure(
                    team_id=team_id,
                    task_id=task.task_id,
                    actor_session_id=requester_session_id,
                    error=str(error),
                )
                raise RuntimeError(
                    "团队任务已记录，但目标 Session Job 启动失败: "
                    f"team_id={team_id} task_id={task.task_id} error={error}"
                ) from error
            board, task = await self._boards.set_task_dispatched(
                team_id=team_id,
                task_id=task.task_id,
                actor_session_id=requester_session_id,
                job_id=accepted.job_id,
            )
        return TeamTaskOperationDTO(
            board=self._boards.with_recent_events(board),
            task=task,
            dispatched_job_id=accepted.job_id if accepted is not None else None,
        )

    async def update_task(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        task_id: str,
        status: TeamTaskStatus,
        summary: str,
    ) -> TeamTaskOperationDTO:
        board, task = await self._boards.update_task(
            requester_session_id=requester_session_id,
            team_id=team_id,
            task_id=task_id,
            status=status,
            summary=summary,
        )
        notification = None
        if (
            requester_session_id != board.coordinator_session_id
            and status in COORDINATOR_NOTIFICATION_STATUSES
        ):
            try:
                notification = await self._session_orchestrator.create_and_run(
                    board.coordinator_session_id,
                    task_update_message(board=board, task=task),
                    message_role=MessageRole.system,
                    metadata={
                        "source": "team_task_update",
                        "team_id": team_id,
                        "team_task_id": task_id,
                        "member_session_id": requester_session_id,
                        "status": status,
                    },
                )
            except Exception as error:
                raise RuntimeError(
                    "团队任务状态已保存，但协调者通知 Job 启动失败: "
                    f"team_id={team_id} task_id={task_id} error={error}"
                ) from error
        return TeamTaskOperationDTO(
            board=self._boards.with_recent_events(board),
            task=task,
            dispatched_job_id=(
                notification.job_id if notification is not None else None
            ),
        )
