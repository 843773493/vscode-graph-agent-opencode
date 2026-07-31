from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.schemas.public_v2.goal import (
    MAX_GOAL_OBJECTIVE_CHARS,
    GoalJobAccountingDTO,
    GoalStatus,
    SessionGoalDTO,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.session_goal_store import SessionGoalStore

_UNSET = object()
UNFINISHED_GOAL_STATUSES = {
    GoalStatus.active,
    GoalStatus.paused,
    GoalStatus.blocked,
    GoalStatus.usage_limited,
    GoalStatus.budget_limited,
}


def _normalize_objective(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("objective 不能为空")
    if len(normalized) > MAX_GOAL_OBJECTIVE_CHARS:
        raise ValueError(
            f"objective 不能超过 {MAX_GOAL_OBJECTIVE_CHARS} 个字符"
        )
    return normalized


class SessionGoalService:
    def __init__(
        self, *, store: SessionGoalStore, session_service: SessionService, job_event_bus
    ) -> None:
        self._store = store
        self._session_service = session_service
        self._job_event_bus = job_event_bus
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def get(self, session_id: str) -> SessionGoalDTO | None:
        await self._session_service.get(session_id)
        async with self.lock_for(session_id):
            return self._store.read(session_id)

    async def set(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        status: GoalStatus | None = None,
        token_budget: int | None | object = _UNSET,
        replace: bool = False,
    ) -> SessionGoalDTO:
        await self._session_service.get(session_id)
        if (
            token_budget is not _UNSET
            and token_budget is not None
            and token_budget <= 0
        ):
            raise ValueError("token_budget 必须大于 0")
        async with self.lock_for(session_id):
            current = self._store.read(session_id)
            now = datetime.now(UTC)
            if current is None or replace:
                if objective is None:
                    raise ValueError("创建 Goal 时 objective 不能为空")
                normalized = _normalize_objective(objective)
                current = SessionGoalDTO(
                    goal_id=create_prefixed_id("goal"),
                    session_id=session_id,
                    objective=normalized,
                    status=status or GoalStatus.active,
                    token_budget=None if token_budget is _UNSET else token_budget,
                    created_at=now,
                    updated_at=now,
                )
            else:
                updates: dict[str, object] = {
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
                if objective is not None:
                    updates["objective"] = _normalize_objective(objective)
                if status is not None:
                    updates["status"] = status
                if token_budget is not _UNSET:
                    updates["token_budget"] = token_budget
                current = current.model_copy(update=updates)
            if (
                current.token_budget is not None
                and current.tokens_used >= current.token_budget
            ):
                current = current.model_copy(
                    update={"status": GoalStatus.budget_limited}
                )
            self._store.write(current)
            await self._publish_updated(current)
            return current

    async def create_for_agent(
        self, session_id: str, objective: str, token_budget: int | None
    ) -> SessionGoalDTO:
        await self._session_service.get(session_id)
        if token_budget is not None and token_budget <= 0:
            raise ValueError("token_budget 必须大于 0")
        async with self.lock_for(session_id):
            existing = self._store.read(session_id)
            if existing is not None and existing.status in UNFINISHED_GOAL_STATUSES:
                raise RuntimeError("当前会话已有未完成 Goal，不能由 Agent 覆盖")
            now = datetime.now(UTC)
            goal = SessionGoalDTO(
                goal_id=create_prefixed_id("goal"),
                session_id=session_id,
                objective=_normalize_objective(objective),
                status=GoalStatus.active,
                token_budget=token_budget,
                created_at=now,
                updated_at=now,
            )
            self._store.write(goal)
            await self._publish_updated(goal)
            return goal

    async def update_for_agent(
        self, session_id: str, status: GoalStatus
    ) -> SessionGoalDTO:
        if status not in {GoalStatus.complete, GoalStatus.blocked}:
            raise ValueError("Agent 只能将 Goal 更新为 complete 或 blocked")
        return await self.set(session_id, status=status)

    async def clear(self, session_id: str) -> bool:
        await self._session_service.get(session_id)
        async with self.lock_for(session_id):
            cleared = self._store.clear(session_id)
            if cleared:
                await self._job_event_bus.publish(
                    job_id=f"goal:{session_id}",
                    event_type=EventType.GOAL_CLEARED,
                    payload={"session_id": session_id},
                    agent_id="goal_service",
                )
            return cleared

    async def account_job(
        self,
        session_id: str,
        *,
        job_id: str,
        tokens: int,
        elapsed_seconds: int,
        close_time: bool = False,
    ) -> SessionGoalDTO | None:
        async with self.lock_for(session_id):
            goal = self._store.read(session_id)
            if goal is None:
                return goal
            previous = goal.accounted_jobs.get(job_id)
            previous_tokens = previous.tokens if previous is not None else 0
            previous_elapsed = previous.elapsed_seconds if previous is not None else 0
            time_closed = previous.time_closed if previous is not None else False
            token_delta = max(max(tokens, 0) - previous_tokens, 0)
            elapsed_delta = (
                0 if time_closed else max(max(elapsed_seconds, 0) - previous_elapsed, 0)
            )
            used = goal.tokens_used + token_delta
            status = goal.status
            if goal.token_budget is not None and used >= goal.token_budget:
                status = GoalStatus.budget_limited
            accounted_jobs = dict(goal.accounted_jobs)
            accounted_jobs[job_id] = GoalJobAccountingDTO(
                tokens=max(tokens, previous_tokens),
                elapsed_seconds=max(elapsed_seconds, previous_elapsed),
                time_closed=time_closed or close_time,
            )
            goal = goal.model_copy(
                update={
                    "tokens_used": used,
                    "time_used_seconds": goal.time_used_seconds + elapsed_delta,
                    "last_accounted_job_id": job_id,
                    "accounted_jobs": accounted_jobs,
                    "status": status,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._store.write(goal)
            await self._publish_updated(goal)
            return goal

    async def dispatch_continuation_if_active(
        self,
        session_id: str,
        trigger_id: str,
        dispatch: Callable[[SessionGoalDTO], Awaitable[None]],
    ) -> bool:
        """在同一 Goal 锁内认领并派发，避免 pause/clear 与续跑穿插。"""
        async with self.lock_for(session_id):
            goal = self._store.read(session_id)
            if (
                goal is None
                or goal.status != GoalStatus.active
                or goal.last_continued_job_id == trigger_id
            ):
                return False
            previous_marker = goal.last_continued_job_id
            goal = goal.model_copy(
                update={
                    "last_continued_job_id": trigger_id,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._store.write(goal)
            try:
                await dispatch(goal)
            except BaseException:
                rolled_back = goal.model_copy(
                    update={
                        "last_continued_job_id": previous_marker,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._store.write(rolled_back)
                raise
            return True

    async def _publish_updated(self, goal: SessionGoalDTO) -> None:
        await self._job_event_bus.publish(
            job_id=f"goal:{goal.session_id}",
            event_type=EventType.GOAL_UPDATED,
            payload={
                "session_id": goal.session_id,
                "goal": goal.model_dump(mode="json"),
            },
            agent_id="goal_service",
        )

    def list_existing(self) -> list[SessionGoalDTO]:
        return self._store.list_existing()


TOKEN_BUDGET_UNSET = _UNSET
