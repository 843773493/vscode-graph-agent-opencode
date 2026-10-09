from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.session_subagent import OwnerThreadCreationFactoryProtocol
from app.abstractions.team import TeamStoreProtocol
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.team import (
    TeamBoardDTO,
    TeamEventDTO,
    TeamListDTO,
    TeamMemberDTO,
    TeamTaskDTO,
    TeamTaskPhase,
    TeamTaskStatus,
)

from .messages import task_assignment_message, task_update_message
from .rules import (
    RESOLUTION_STATUSES,
    TASK_TRANSITIONS,
    find_member,
    find_task,
    require_active_member,
    require_coordinator,
    required_text,
)


class TeamBoardManager:
    """串行化并持久化团队任务板；不启动 Session Job。"""

    def __init__(self, *, store: TeamStoreProtocol, factory: OwnerThreadCreationFactoryProtocol) -> None:
        self._store = store
        self._factory = factory
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, *, session_id: str, coordinator_thread_id: str, name: str) -> TeamBoardDTO:
        now = self._now()
        board = TeamBoardDTO(
            team_id=create_prefixed_id("team"), name=required_text(name, "name"),
            session_id=session_id, coordinator_thread_id=coordinator_thread_id,
            version=1, created_at=now, updated_at=now,
        )
        await self._store.commit(board, self._event(
            board, event_type="team.created", actor_thread_id=coordinator_thread_id,
            payload={"name": board.name},
        ))
        return self.with_recent_events(board)

    def list_for_member(self, session_id: str, thread_id: str) -> TeamListDTO:
        boards = [self.with_recent_events(board) for board in self._store.list(session_id)]
        return TeamListDTO(items=[board for board in boards if find_member(board, thread_id, required=False) is not None])

    def get_for_member(self, *, session_id: str, team_id: str, thread_id: str) -> TeamBoardDTO:
        board = self.with_recent_events(self._store.get(session_id, team_id))
        require_active_member(board, thread_id)
        return board

    def get_for_coordinator(self, *, session_id: str, team_id: str, thread_id: str) -> TeamBoardDTO:
        board = self.get_for_member(session_id=session_id, team_id=team_id, thread_id=thread_id)
        require_coordinator(board, thread_id)
        return board

    async def assign_task(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        team_id: str,
        assignee_thread_id: str,
        title: str,
        description: str,
        phase: TeamTaskPhase,
        cycle: int,
        depends_on_task_ids: list[str],
        start_assignee: bool,
    ) -> tuple[TeamBoardDTO, TeamTaskDTO, int]:
        if cycle < 1:
            raise ValueError("cycle 必须大于等于 1")
        async with self._lock(team_id):
            board = self.get_for_coordinator(
                team_id=team_id,
                session_id=requester_session_id,
                thread_id=requester_thread_id,
            )
            require_active_member(board, assignee_thread_id)
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
                assignee_thread_id=assignee_thread_id,
                status="in_progress",
                depends_on_task_ids=list(dict.fromkeys(depends_on_task_ids)),
                updated_by_thread_id=requester_thread_id,
                created_at=now,
                updated_at=now,
            )
            board = self._replace_board(board, tasks=[*board.tasks, task])
            board, revision = await self._persist(
                board,
                self._event(
                    board,
                    event_type="task.assigned",
                    actor_thread_id=requester_thread_id,
                    payload={
                        "task_id": task.task_id,
                        "assignee_thread_id": assignee_thread_id,
                        "phase": phase,
                        "cycle": cycle,
                        "start_assignee": start_assignee,
                    },
                ),
                wakeup=self._wakeup(
                    task_assignment_message(board=board, task=task), target_thread_id=assignee_thread_id,
                    source_id=f"team:{team_id}:task:{task.task_id}:assignment",
                    idempotency_key=f"team-assignment:{task.task_id}",
                ) if start_assignee else None,
            )
            return board, task, revision

    async def update_task(
        self,
        *,
        requester_session_id: str,
        requester_thread_id: str,
        team_id: str,
        task_id: str,
        status: TeamTaskStatus,
        summary: str,
    ) -> tuple[TeamBoardDTO, TeamTaskDTO, int]:
        normalized_summary = summary.strip()
        if status in RESOLUTION_STATUSES and not normalized_summary:
            raise ValueError(f"status={status} 时 summary 不能为空")
        async with self._lock(team_id):
            board = self.get_for_member(
                team_id=team_id,
                session_id=requester_session_id,
                thread_id=requester_thread_id,
            )
            task = find_task(board, task_id)
            if requester_thread_id not in {
                board.coordinator_thread_id,
                task.assignee_thread_id,
            }:
                raise PermissionError("只能由协调者或任务负责人更新团队任务")
            if status not in TASK_TRANSITIONS[task.status]:
                raise ValueError(f"不允许的任务状态流转: {task.status} -> {status}")
            task = task.model_copy(
                update={
                    "status": status,
                    "summary": normalized_summary or task.summary,
                    "updated_by_thread_id": requester_thread_id,
                    "updated_at": self._now(),
                }
            )
            board = self._replace_task(board, task)
            board, revision = await self._persist(
                board,
                self._event(
                    board,
                    event_type="task.updated",
                    actor_thread_id=requester_thread_id,
                    payload={
                        "task_id": task_id,
                        "status": status,
                        "summary": normalized_summary,
                    },
                ),
                wakeup=self._wakeup(
                    task_update_message(board=board, task=task), target_thread_id=board.coordinator_thread_id,
                    source_id=f"team:{team_id}:task:{task_id}:update",
                    idempotency_key=f"team-update:{task_id}:{board.version}",
                ) if requester_thread_id != board.coordinator_thread_id and status in RESOLUTION_STATUSES else None,
            )
            return board, task, revision

    async def set_task_dispatched(
        self,
        *,
        team_id: str,
        task_id: str,
        session_id: str,
        actor_thread_id: str,
        job_id: str,
    ) -> tuple[TeamBoardDTO, TeamTaskDTO]:
        async with self._lock(team_id):
            board = self._store.get(session_id, team_id)
            existing = find_task(board, task_id)
            latest = self._store.latest_task_event(session_id, team_id, task_id)
            recovering_failure = latest is not None and latest.type == "task.dispatch_failed"
            if existing.assigned_job_id == job_id and not recovering_failure:
                return self.with_recent_events(board), existing
            if existing.assigned_job_id is not None and existing.assigned_job_id != job_id:
                raise RuntimeError("团队任务已绑定其它 Job，拒绝恢复换绑")
            # 只撤销原调度失败写入；后续 task.updated 的用户结论由原事件因果保留。
            restored = latest.payload["previous"] if latest is not None and latest.type == "task.dispatch_failed" else {}
            task = TeamTaskDTO.model_validate(existing.model_dump() | restored | {
                "assigned_job_id": job_id,
                "updated_at": self._now(),
                "updated_by_thread_id": actor_thread_id,
            })
            board = self._replace_task(board, task)
            board, _revision = await self._persist(
                board,
                self._event(
                    board,
                    event_type="task.dispatched",
                    actor_thread_id=actor_thread_id,
                    payload={"task_id": task_id, "job_id": job_id},
                ),
            )
            return board, task

    async def set_task_dispatch_failure(
        self,
        *,
        team_id: str,
        task_id: str,
        session_id: str,
        actor_thread_id: str,
        error: str,
    ) -> None:
        async with self._lock(team_id):
            board = self._store.get(session_id, team_id)
            original = find_task(board, task_id)
            latest = self._store.latest_task_event(session_id, team_id, task_id)
            previous = latest.payload["previous"] if latest is not None and latest.type == "task.dispatch_failed" else {
                "status": original.status, "summary": original.summary, "error": original.error,
            }
            task = original.model_copy(
                update={
                    "status": "failed",
                    "error": error,
                    "summary": "目标线程 Job 启动失败",
                    "updated_at": self._now(),
                    "updated_by_thread_id": actor_thread_id,
                }
            )
            board = self._replace_task(board, task)
            await self._persist(
                board,
                self._event(
                    board,
                    event_type="task.dispatch_failed",
                    actor_thread_id=actor_thread_id,
                    payload={"task_id": task_id, "error": error, "previous": previous},
                ),
            )

    def with_recent_events(self, board: TeamBoardDTO) -> TeamBoardDTO:
        control = self._factory.for_owner_session(board.session_id).control_store
        members = [TeamMemberDTO(
            thread_id=board.coordinator_thread_id, role="coordinator", source="coordinator",
            work_mode="write", instructions="负责拆分工作、修改代码并协调团队成员。",
            status="active", joined_at=board.created_at, updated_at=board.created_at,
        )]
        for row in control.list_collaboration_members(coordinator_session_id=board.session_id):
            seed = json.loads(row.task_seed)
            context = seed["trusted_context"]
            if context.get("team_id") != board.team_id:
                continue
            if row.coordinator_thread_id != board.coordinator_thread_id or context["coordinator_thread_id"] != board.coordinator_thread_id:
                raise RuntimeError(f"团队成员的冻结协调线程不一致: {row.delegation_id}")
            if row.child_thread_id is None:
                if row.state == "published":
                    raise RuntimeError(f"published 团队成员缺少 child thread: {row.delegation_id}")
                continue
            intent = control.get_initial_execution_intent(row.delegation_id)
            status = "cancelled" if row.state == "cancelled" else "active" if row.state == "published" and intent.state == "bound" else "pending"
            members.append(TeamMemberDTO(
                thread_id=row.child_thread_id, delegation_id=row.delegation_id,
                role=row.role, source="delegated", work_mode=context["work_mode"],
                instructions=seed.get("untrusted_instructions", ""), status=status,
                joined_at=row.registered_at, updated_at=intent.intent_updated_at,
            ))
        return board.model_copy(update={
            "members": members, "recent_events": self._store.recent_events(board.session_id, board.team_id),
        })

    async def _persist(self, board: TeamBoardDTO, event: TeamEventDTO,
                       *, wakeup: dict[str, object] | None = None) -> tuple[TeamBoardDTO, int]:
        revision = await self._store.commit(board, event, wakeup=wakeup)
        return self.with_recent_events(board), revision

    @staticmethod
    def _wakeup(message: PreparedInternalMessage, *, target_thread_id: str, source_id: str, idempotency_key: str) -> dict[str, object]:
        return {"target_thread_id": target_thread_id, "source_id": source_id,
                "idempotency_key": idempotency_key, "content": message.content, "metadata": dict(message.metadata)}

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
        actor_thread_id: str,
        payload: dict[str, object],
    ) -> TeamEventDTO:
        return TeamEventDTO(
            event_id=create_prefixed_id("tevt"),
            team_id=board.team_id,
            session_id=board.session_id,
            type=event_type,
            actor_thread_id=actor_thread_id,
            created_at=self._now(),
            payload=payload,
        )

    def _lock(self, team_id: str) -> asyncio.Lock:
        return self._locks.setdefault(team_id, asyncio.Lock())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)
