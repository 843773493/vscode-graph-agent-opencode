from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.abstractions.team import TeamStoreProtocol
from app.core.identifier import create_prefixed_id
from app.schemas.public_v2.team import (
    TeamBoardDTO,
    TeamEventDTO,
    TeamListDTO,
    TeamMemberDTO,
    TeamMemberSource,
    TeamMemberStatus,
    TeamTaskDTO,
    TeamTaskPhase,
    TeamTaskStatus,
    TeamWorkMode,
)

from .rules import (
    SUMMARY_REQUIRED_STATUSES,
    TASK_TRANSITIONS,
    find_member,
    find_task,
    require_active_member,
    require_coordinator,
    required_text,
)


class TeamBoardManager:
    """串行化并持久化团队任务板；不启动 Session Job。"""

    def __init__(self, *, store: TeamStoreProtocol) -> None:
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}

    def create(self, *, coordinator_session_id: str, name: str) -> TeamBoardDTO:
        now = self._now()
        board = TeamBoardDTO(
            team_id=create_prefixed_id("team"),
            name=required_text(name, "name"),
            coordinator_session_id=coordinator_session_id,
            version=1,
            members=[
                TeamMemberDTO(
                    session_id=coordinator_session_id,
                    role="coordinator",
                    source="coordinator",
                    work_mode="write",
                    instructions="负责拆分工作、修改代码并协调团队成员。",
                    status="active",
                    joined_at=now,
                    updated_at=now,
                )
            ],
            created_at=now,
            updated_at=now,
        )
        self._store.create(board)
        self._store.append_event(
            self._event(
                board,
                event_type="team.created",
                actor_session_id=coordinator_session_id,
                payload={"name": board.name},
            )
        )
        return self.with_recent_events(board)

    def list_for_session(self, session_id: str) -> TeamListDTO:
        return TeamListDTO(
            items=[
                self.with_recent_events(board)
                for board in self._store.list()
                if find_member(board, session_id, required=False) is not None
            ]
        )

    def get_for_member(self, *, team_id: str, session_id: str) -> TeamBoardDTO:
        board = self._store.get(team_id)
        require_active_member(board, session_id)
        return self.with_recent_events(board)

    def get_for_coordinator(self, *, team_id: str, session_id: str) -> TeamBoardDTO:
        board = self._store.get(team_id)
        require_coordinator(board, session_id)
        return board

    async def add_member(
        self,
        *,
        team_id: str,
        actor_session_id: str,
        target_session_id: str,
        role: str,
        source: TeamMemberSource,
        work_mode: TeamWorkMode,
        instructions: str,
    ) -> TeamBoardDTO:
        async with self._lock(team_id):
            board = self.get_for_coordinator(
                team_id=team_id,
                session_id=actor_session_id,
            )
            if find_member(board, target_session_id, required=False) is not None:
                raise ValueError(f"Session 已是团队成员: {target_session_id}")
            now = self._now()
            member = TeamMemberDTO(
                session_id=target_session_id,
                role=required_text(role, "role"),
                source=source,
                work_mode=work_mode,
                instructions=instructions.strip(),
                status="active",
                joined_at=now,
                updated_at=now,
            )
            board = self._replace_board(board, members=[*board.members, member])
            return self._persist(
                board,
                self._event(
                    board,
                    event_type="member.added",
                    actor_session_id=actor_session_id,
                    payload={
                        "session_id": target_session_id,
                        "role": member.role,
                        "source": source,
                        "work_mode": work_mode,
                    },
                ),
            )

    async def set_member_activation(
        self,
        *,
        team_id: str,
        actor_session_id: str,
        target_session_id: str,
        status: TeamMemberStatus,
        activation_job_id: str | None = None,
        activation_error: str | None = None,
    ) -> TeamBoardDTO:
        async with self._lock(team_id):
            board = self._store.get(team_id)
            member = find_member(board, target_session_id)
            assert member is not None
            updated = member.model_copy(
                update={
                    "status": status,
                    "activation_job_id": activation_job_id,
                    "activation_error": activation_error,
                    "updated_at": self._now(),
                }
            )
            board = self._replace_board(
                board,
                members=[
                    updated if item.session_id == target_session_id else item
                    for item in board.members
                ],
            )
            return self._persist(
                board,
                self._event(
                    board,
                    event_type="member.activation_updated",
                    actor_session_id=actor_session_id,
                    payload={
                        "session_id": target_session_id,
                        "status": status,
                        "activation_job_id": activation_job_id,
                        "activation_error": activation_error,
                    },
                ),
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
    ) -> tuple[TeamBoardDTO, TeamTaskDTO]:
        if cycle < 1:
            raise ValueError("cycle 必须大于等于 1")
        async with self._lock(team_id):
            board = self.get_for_coordinator(
                team_id=team_id,
                session_id=requester_session_id,
            )
            require_active_member(board, assignee_session_id)
            tasks = {task.task_id: task for task in board.tasks}
            missing = [task_id for task_id in depends_on_task_ids if task_id not in tasks]
            if missing:
                raise ValueError(f"依赖任务不存在: {missing}")
            incomplete = [
                task_id
                for task_id in depends_on_task_ids
                if tasks[task_id].status != "completed"
            ]
            if incomplete:
                raise ValueError(f"依赖任务尚未完成: {incomplete}")
            now = self._now()
            task = TeamTaskDTO(
                task_id=create_prefixed_id("ttask"),
                title=required_text(title, "title"),
                description=required_text(description, "description"),
                phase=phase,
                cycle=cycle,
                assignee_session_id=assignee_session_id,
                status="in_progress",
                depends_on_task_ids=list(dict.fromkeys(depends_on_task_ids)),
                updated_by_session_id=requester_session_id,
                created_at=now,
                updated_at=now,
            )
            board = self._replace_board(board, tasks=[*board.tasks, task])
            board = self._persist(
                board,
                self._event(
                    board,
                    event_type="task.assigned",
                    actor_session_id=requester_session_id,
                    payload={
                        "task_id": task.task_id,
                        "assignee_session_id": assignee_session_id,
                        "phase": phase,
                        "cycle": cycle,
                        "start_assignee": start_assignee,
                    },
                ),
            )
            return board, task

    async def update_task(
        self,
        *,
        requester_session_id: str,
        team_id: str,
        task_id: str,
        status: TeamTaskStatus,
        summary: str,
    ) -> tuple[TeamBoardDTO, TeamTaskDTO]:
        normalized_summary = summary.strip()
        if status in SUMMARY_REQUIRED_STATUSES and not normalized_summary:
            raise ValueError(f"status={status} 时 summary 不能为空")
        async with self._lock(team_id):
            board = self.get_for_member(
                team_id=team_id,
                session_id=requester_session_id,
            )
            task = find_task(board, task_id)
            if requester_session_id not in {
                board.coordinator_session_id,
                task.assignee_session_id,
            }:
                raise PermissionError("只能由协调者或任务负责人更新团队任务")
            if status not in TASK_TRANSITIONS[task.status]:
                raise ValueError(f"不允许的任务状态流转: {task.status} -> {status}")
            task = task.model_copy(
                update={
                    "status": status,
                    "summary": normalized_summary or task.summary,
                    "updated_by_session_id": requester_session_id,
                    "updated_at": self._now(),
                }
            )
            board = self._replace_task(board, task)
            board = self._persist(
                board,
                self._event(
                    board,
                    event_type="task.updated",
                    actor_session_id=requester_session_id,
                    payload={
                        "task_id": task_id,
                        "status": status,
                        "summary": normalized_summary,
                    },
                ),
            )
            return board, task

    async def set_task_dispatched(
        self,
        *,
        team_id: str,
        task_id: str,
        actor_session_id: str,
        job_id: str,
    ) -> tuple[TeamBoardDTO, TeamTaskDTO]:
        async with self._lock(team_id):
            board = self._store.get(team_id)
            task = find_task(board, task_id).model_copy(
                update={
                    "assigned_job_id": job_id,
                    "updated_at": self._now(),
                    "updated_by_session_id": actor_session_id,
                }
            )
            board = self._replace_task(board, task)
            board = self._persist(
                board,
                self._event(
                    board,
                    event_type="task.dispatched",
                    actor_session_id=actor_session_id,
                    payload={"task_id": task_id, "job_id": job_id},
                ),
            )
            return board, task

    async def set_task_dispatch_failure(
        self,
        *,
        team_id: str,
        task_id: str,
        actor_session_id: str,
        error: str,
    ) -> None:
        async with self._lock(team_id):
            board = self._store.get(team_id)
            task = find_task(board, task_id).model_copy(
                update={
                    "status": "failed",
                    "error": error,
                    "summary": "目标 Session Job 启动失败",
                    "updated_at": self._now(),
                    "updated_by_session_id": actor_session_id,
                }
            )
            board = self._replace_task(board, task)
            self._persist(
                board,
                self._event(
                    board,
                    event_type="task.dispatch_failed",
                    actor_session_id=actor_session_id,
                    payload={"task_id": task_id, "error": error},
                ),
            )

    def with_recent_events(self, board: TeamBoardDTO) -> TeamBoardDTO:
        return board.model_copy(
            update={"recent_events": self._store.recent_events(board.team_id)}
        )

    def _persist(self, board: TeamBoardDTO, event: TeamEventDTO) -> TeamBoardDTO:
        self._store.save(board.model_copy(update={"recent_events": []}))
        self._store.append_event(event)
        return board

    def _replace_board(self, board: TeamBoardDTO, **updates: object) -> TeamBoardDTO:
        return board.model_copy(
            update={
                **updates,
                "version": board.version + 1,
                "updated_at": self._now(),
                "recent_events": [],
            }
        )

    def _replace_task(self, board: TeamBoardDTO, task: TeamTaskDTO) -> TeamBoardDTO:
        return self._replace_board(
            board,
            tasks=[task if item.task_id == task.task_id else item for item in board.tasks],
        )

    def _event(
        self,
        board: TeamBoardDTO,
        *,
        event_type: str,
        actor_session_id: str,
        payload: dict[str, object],
    ) -> TeamEventDTO:
        return TeamEventDTO(
            event_id=create_prefixed_id("tevt"),
            team_id=board.team_id,
            type=event_type,
            actor_session_id=actor_session_id,
            created_at=self._now(),
            payload=payload,
        )

    def _lock(self, team_id: str) -> asyncio.Lock:
        return self._locks.setdefault(team_id, asyncio.Lock())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
