from __future__ import annotations

import asyncio
import json
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.phase_observer import observe_phase
from app.core.session_catalog_store import SessionCreationRecord
from app.schemas.internal_v2.goal import (
    GoalStatus,
    SessionGoalClearResultDTO,
    SessionGoalDTO,
)
from app.services.business.persistent_tasks.goal import (
    UNFINISHED_GOAL_STATUSES,
    account_job,
    enforce_budget,
    normalize_objective,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.persistent_tasks.store import PersistentTaskStore

_UNSET = object()
# Goal 锁按 session_id 分片；分片数固定，避免长驻进程随历史会话数无界增长。
GOAL_LOCK_SHARDS = 64


class SessionGoalService:
    def __init__(
        self, *, store: PersistentTaskStore, session_service: SessionService, job_event_bus
    ) -> None:
        self._store = store
        self._session_service = session_service
        self._job_event_bus = job_event_bus
        self._locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(GOAL_LOCK_SHARDS)
        )

    def lock_for(self, session_id: str) -> asyncio.Lock:
        # 分片锁池：同一 session_id 恒得同一把锁（保住互斥），数量恒为分片数
        # （有界）。用 crc32 而非 hash() 是为了跨进程确定，不引入 hash 随机化。
        shard = zlib.crc32(session_id.encode("utf-8")) % len(self._locks)
        return self._locks[shard]

    async def assert_agent_thread(self, session_id: str, thread_id: str) -> None:
        if thread_id != await self._session_service.resolve_main_thread(session_id):
            raise ValueError("Goal 只属于 Session main thread，child thread 不得读取、创建或修改 Goal")

    def started_owner(self, session_id: str, thread_id: str) -> str | None:
        """JOB_STARTED 同步发布边界冻结归属；无 await，不与同进程写入交错。"""
        goal = self._store.goal_read(session_id)
        if goal is not None and goal.thread_id == thread_id and goal.status == GoalStatus.active:
            return goal.goal_id
        return None

    async def get(self, session_id: str) -> SessionGoalDTO | None:
        await self._session_service.resolve_main_thread(session_id)
        async with self.lock_for(session_id):
            return self._store.goal_read(session_id)

    async def install_fork_goal(self, record: SessionCreationRecord) -> str | None:
        """发布前消费冻结目标；恢复只复验，不重置已发布后用户修改的 Goal。"""
        if record.request_intent_json is None:
            raise RuntimeError("fork Goal 缺少冻结请求")
        snapshot = json.loads(record.request_intent_json).get("goal_snapshot")
        if snapshot is None:
            return None
        if record.state != "preparing":
            raise RuntimeError("fork Goal 只能安装到未发布的新 Session main")
        created_at = datetime.fromisoformat(record.created_at)
        goal = SessionGoalDTO(
            goal_id=snapshot["goal_id"], session_id=record.session_id,
            thread_id=record.main_thread_id,
            objective=normalize_objective(snapshot["objective"]), status=GoalStatus.paused,
            token_budget=snapshot["token_budget"], created_at=created_at, updated_at=created_at,
        )
        async with self.lock_for(record.session_id):
            current = self._store.goal_read(record.session_id, creation=record)
            if current is not None:
                if current != goal:
                    raise RuntimeError("fork Goal 与冻结创建意图不一致")
            else:
                self._store.goal_write(goal, creation=record)
        await observe_phase(record.session_creation_idempotency_key, "fork.goal.installed_before_publish", details={
            "session_id": record.session_id, "goal_id": goal.goal_id,
        })
        return goal.goal_id

    async def abort_fork_goal(self, record: SessionCreationRecord) -> None:
        async with self.lock_for(record.session_id):
            self._store.goal_abort_fork(record)

    async def set(
        self,
        session_id: str,
        *,
        objective: str | None = None,
        status: GoalStatus | None = None,
        token_budget: int | None | object = _UNSET,
        replace: bool = False,
    ) -> SessionGoalDTO:
        thread_id = await self._session_service.resolve_main_thread(session_id)
        if (
            token_budget is not _UNSET
            and token_budget is not None
            and token_budget <= 0
        ):
            raise ValueError("token_budget 必须大于 0")
        async with self.lock_for(session_id):
            current = self._store.goal_read(session_id)
            now = datetime.now(UTC)
            if current is None or replace:
                if objective is None:
                    raise ValueError("创建 Goal 时 objective 不能为空")
                normalized = normalize_objective(objective)
                current = SessionGoalDTO(
                    goal_id=create_prefixed_id("goal"),
                    session_id=session_id,
                    thread_id=thread_id,
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
                    updates["objective"] = normalize_objective(objective)
                if status is not None:
                    updates["status"] = status
                if token_budget is not _UNSET:
                    updates["token_budget"] = token_budget
                current = current.model_copy(update=updates)
            current = enforce_budget(current)
            self._store.goal_write(current)
            await self._publish_updated(current)
            return current

    async def create_for_agent(
        self, session_id: str, objective: str, token_budget: int | None
    ) -> SessionGoalDTO:
        thread_id = await self._session_service.resolve_main_thread(session_id)
        if token_budget is not None and token_budget <= 0:
            raise ValueError("token_budget 必须大于 0")
        async with self.lock_for(session_id):
            existing = self._store.goal_read(session_id)
            if existing is not None and existing.status in UNFINISHED_GOAL_STATUSES:
                raise RuntimeError("当前会话已有未完成 Goal，不能由 Agent 覆盖")
            now = datetime.now(UTC)
            goal = SessionGoalDTO(
                goal_id=create_prefixed_id("goal"),
                session_id=session_id,
                thread_id=thread_id,
                objective=normalize_objective(objective),
                status=GoalStatus.active,
                token_budget=token_budget,
                created_at=now,
                updated_at=now,
            )
            self._store.goal_write(goal)
            await self._publish_updated(goal)
            return goal

    async def update_for_agent(
        self, session_id: str, status: GoalStatus
    ) -> SessionGoalDTO:
        if status == GoalStatus.paused:
            thread_id = await self._session_service.resolve_main_thread(session_id)
            async with self.lock_for(session_id):
                current = self._store.goal_read(session_id)
                if current is None or current.status != GoalStatus.active:
                    raise ValueError("当前没有进行中的 Goal 可以暂停")
                paused = current.model_copy(
                    update={
                        "status": GoalStatus.paused,
                        "revision": current.revision + 1,
                        "updated_at": datetime.now(UTC),
                    }
                )
                if paused.thread_id != thread_id:
                    raise ValueError("Goal 只能更新权威 main Thread")
                self._store.goal_write(paused)
                await self._publish_updated(paused)
                return paused
        if status not in {GoalStatus.complete, GoalStatus.blocked}:
            raise ValueError("Agent 只能将 Goal 更新为 paused、complete 或 blocked")
        return await self.set(session_id, status=status)

    async def clear(self, session_id: str) -> SessionGoalClearResultDTO:
        thread_id = await self._session_service.resolve_main_thread(session_id)
        async with self.lock_for(session_id):
            cleared = self._store.goal_clear(session_id)
            if cleared:
                await self._job_event_bus.publish(
                    job_id=f"goal:{session_id}",
                    event_type=EventType.GOAL_CLEARED,
                    payload={"session_id": session_id, "thread_id": thread_id},
                    agent_id="goal_service",
                )
            return SessionGoalClearResultDTO(session_id=session_id, thread_id=thread_id, cleared=cleared)

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
            goal = self._store.goal_read(session_id)
            if goal is None:
                return goal
            goal = account_job(
                goal, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds,
                close_time=close_time, now=datetime.now(UTC),
            )
            self._store.goal_write(goal)
            await self._publish_updated(goal)
            return goal

    async def dispatch_continuation_if_active(
        self,
        session_id: str,
        trigger_id: str,
        dispatch: Callable[[SessionGoalDTO], Awaitable[None]],
    ) -> bool:
        """同一 Goal 锁内派发并登记已接受标记，避免 pause/clear 与续跑穿插。"""
        async with self.lock_for(session_id):
            goal = self._store.goal_read(session_id)
            if (
                goal is None
                or goal.status != GoalStatus.active
                or goal.last_continued_job_id == trigger_id
            ):
                return False
            await observe_phase(goal.goal_id, "goal.continuation.before_dispatch", details={
                "session_id": session_id, "goal_id": goal.goal_id, "trigger_id": trigger_id,
            })
            # 准入沿原 source/wakeup 幂等提交；接受前不能把尝试记为已续跑。
            await dispatch(goal)
            await observe_phase(goal.goal_id, "goal.continuation.accepted_before_record", details={
                "session_id": session_id, "goal_id": goal.goal_id, "trigger_id": trigger_id,
            })
            self._store.goal_write(goal.model_copy(update={
                "last_continued_job_id": trigger_id,
                "updated_at": datetime.now(UTC),
            }))
            await observe_phase(goal.goal_id, "goal.continuation.recorded", details={
                "session_id": session_id, "goal_id": goal.goal_id, "trigger_id": trigger_id,
            })
            return True

    async def _publish_updated(self, goal: SessionGoalDTO) -> None:
        await self._job_event_bus.publish(
            job_id=f"goal:{goal.session_id}",
            event_type=EventType.GOAL_UPDATED,
            payload={
                "session_id": goal.session_id,
                "thread_id": goal.thread_id,
                "goal": goal.model_dump(mode="json"),
            },
            agent_id="goal_service",
        )

    def list_existing(self) -> list[SessionGoalDTO]:
        return self._store.goal_list()


TOKEN_BUDGET_UNSET = _UNSET
