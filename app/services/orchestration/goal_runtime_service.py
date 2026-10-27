from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime

from app.abstractions.internal_message import PreparedInternalMessage
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.core.job_event_bus import EventType
from app.schemas.event import Event
from app.schemas.internal_v2.goal import GoalStatus, SessionGoalDTO
from app.services.business.persistent_tasks.goal import (
    budget_limited_message,
    continuation_message,
    objective_updated_message,
)
from app.services.business.session_goal_service import SessionGoalService

logger = logging.getLogger(__name__)


class GoalRuntimeService:
    def __init__(
        self,
        *,
        goal_service: SessionGoalService,
        job_service: JobServiceProtocol,
        session_orchestrator: SessionOrchestratorProtocol,
    ) -> None:
        self._goal_service = goal_service
        self._job_service = job_service
        self._session_orchestrator = session_orchestrator
        self._tasks: set[asyncio.Task[None]] = set()
        self._job_goal_ids: dict[str, str] = {}
        self._job_goal_capture_tasks: dict[str, asyncio.Task[None]] = {}
        self._accepting_continuations = True
        self._active_continuations = 0
        self._continuations_drained = asyncio.Event()
        self._continuations_drained.set()
        self._closed = False
        self._task_error: BaseException | None = None

    @asynccontextmanager
    async def _continuation_admission(self) -> AsyncIterator[bool]:
        if not self._accepting_continuations:
            yield False
            return
        self._active_continuations += 1
        self._continuations_drained.clear()
        try:
            yield True
        finally:
            self._active_continuations -= 1
            if not self._active_continuations:
                self._continuations_drained.set()

    async def close_admission(self) -> None:
        """阻止新续跑，并让已经准入的派发在 Job 关闭准入前完成。"""
        self._accepting_continuations = False
        await self._continuations_drained.wait()

    async def shutdown(self) -> None:
        """排空终态状态转换，不取消写入或假装后台失败已完成。"""
        await self.close_admission()
        self._closed = True
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._task_error is not None:
            raise BaseExceptionGroup(
                "Goal 后台状态转换失败", [self._task_error]
            )

    async def on_event(self, event: Event) -> None:
        if event.type not in {
            EventType.JOB_STARTED,
            EventType.JOB_FAILED,
            EventType.JOB_CANCELLED,
            EventType.JOB_COMPLETED,
            EventType.AGENT_END,
        }:
            return
        if self._closed:
            raise RuntimeError("Goal runtime 已关闭，不能再接收 Job 生命周期事件")
        if event.type == EventType.JOB_STARTED:
            # JOB_STARTED 在 JobEventBus 的同步持久化事务中发布。Goal 续跑派发
            # 此时仍持有同一个 session 的 Goal 锁；若在监听器内同步读取 Goal，
            # 会形成 Goal lock -> JOB_STARTED -> Goal lock 的自锁，执行器因此连
            # Turn 消息流都无法创建。启动关联改为发布事务之外的后台捕获。
            task = self._spawn(self._capture_started_job(event.job_id))
            self._job_goal_capture_tasks[event.job_id] = task
            task.add_done_callback(
                lambda completed, job_id=event.job_id: self._clear_capture_task(
                    job_id, completed
                )
            )
            return
        if event.type in {EventType.JOB_FAILED, EventType.JOB_CANCELLED}:
            self._spawn(self._stop_after_terminal_error(event.job_id, event.type))
            return
        if event.type == EventType.JOB_COMPLETED:
            self._spawn(self._ensure_after_job_completed(event.job_id))
            return
        if event.type != EventType.AGENT_END:
            return
        self._spawn(
            self._continue_after_agent_end(
                event.job_id,
                event.payload.token_usage.total_tokens,
            )
        )

    def _spawn(
        self, coroutine: Coroutine[object, object, None]
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _clear_capture_task(
        self, job_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._job_goal_capture_tasks.get(job_id) is task:
            self._job_goal_capture_tasks.pop(job_id, None)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except BaseException as error:
            if self._task_error is None:
                self._task_error = error
            logger.error("Goal 后台状态转换失败", exc_info=error)

    async def _submit_goal(
        self, goal: SessionGoalDTO, message: PreparedInternalMessage, *, event_identity: str, delivery_policy="after_turn",
    ):
        session_id = goal.session_id
        thread_id = await self._session_orchestrator.resolve_main_thread(session_id)
        return await self._session_orchestrator.submit_runtime_event(
            session_id, message, thread_id=thread_id, source_id=f"goal:{goal.goal_id}:state",
            idempotency_key=f"goal:{goal.goal_id}:{event_identity}:{message.metadata['structured_prompt_kind']}",
            delivery_policy=delivery_policy,
        )

    async def _capture_started_job(self, job_id: str) -> None:
        job = await self._job_service.get(job_id)
        if job.thread_id != await self._session_orchestrator.resolve_main_thread(job.session_id):
            return
        goal = await self._goal_service.get(job.session_id)
        if goal is not None and goal.status == GoalStatus.active:
            self._job_goal_ids[job_id] = goal.goal_id

    async def _wait_for_goal_capture(self, job_id: str) -> None:
        task = self._job_goal_capture_tasks.get(job_id)
        if task is not None and task is not asyncio.current_task():
            await task

    async def _continue_after_agent_end(
        self, job_id: str, total_tokens: int = 0
    ) -> None:
        await self._wait_for_goal_capture(job_id)
        job = await self._job_service.get(job_id)
        if job.thread_id != await self._session_orchestrator.resolve_main_thread(job.session_id):
            return
        goal = await self._goal_service.get(job.session_id)
        owner_goal_id = self._job_goal_ids.pop(job_id, None)
        if goal is None or owner_goal_id != goal.goal_id:
            return
        elapsed_seconds = max(
            0,
            int(
                (
                    (job.ended_at or datetime.now(job.created_at.tzinfo))
                    - job.created_at
                ).total_seconds()
            ),
        )
        goal = await self._goal_service.account_job(
            job.session_id,
            job_id=job_id,
            tokens=total_tokens,
            elapsed_seconds=elapsed_seconds,
        )
        if goal is None:
            return

        if goal.status == GoalStatus.budget_limited:
            async with self._continuation_admission() as admitted:
                if admitted:
                    await self._submit_goal(
                        goal,
                        budget_limited_message(
                            goal,
                            metadata={
                                "goal_continuation": True,
                                "goal_id": goal.goal_id,
                                "goal_objective": goal.objective,
                                "goal_budget_limited": True,
                            },
                        ),
                        event_identity=job_id,
                    )
            return

        async def dispatch(current: SessionGoalDTO) -> None:
            await self._submit_goal(
                current,
                continuation_message(
                    current,
                    metadata={
                        "goal_continuation": True,
                        "goal_id": current.goal_id,
                        "goal_objective": current.objective,
                    },
                ),
                event_identity=job_id,
            )

        async with self._continuation_admission() as admitted:
            if admitted:
                await self._goal_service.dispatch_continuation_if_active(
                    job.session_id, job_id, dispatch
                )

    async def _ensure_after_job_completed(self, job_id: str) -> None:
        job = await self._job_service.get(job_id)
        main_thread_id = await self._session_orchestrator.resolve_main_thread(
            job.session_id
        )
        if job.thread_id != main_thread_id:
            return
        await self._dispatch_active_goal_if_idle(
            job.session_id,
            resolved_main_thread_id=main_thread_id,
        )

    async def _stop_after_terminal_error(self, job_id: str, event_type: str) -> None:
        await self._wait_for_goal_capture(job_id)
        job = await self._job_service.get(job_id)
        if job.thread_id != await self._session_orchestrator.resolve_main_thread(job.session_id):
            return
        goal = await self._goal_service.get(job.session_id)
        owner_goal_id = self._job_goal_ids.pop(job_id, None)
        if goal is None or owner_goal_id != goal.goal_id:
            return
        if goal.status != GoalStatus.active:
            return
        status = (
            GoalStatus.paused
            if event_type == EventType.JOB_CANCELLED
            else GoalStatus.blocked
        )
        await self._goal_service.set(job.session_id, status=status)

    async def resume_active_goals(self) -> None:
        for goal in self._goal_service.list_existing():
            if goal.status != GoalStatus.active:
                continue
            await self.ensure_active_goal_running(goal.session_id)

    async def ensure_active_goal_running(self, session_id: str) -> None:
        await self._dispatch_active_goal_if_idle(session_id)

    async def _dispatch_active_goal_if_idle(
        self,
        session_id: str,
        *,
        resolved_main_thread_id: str | None = None,
    ) -> None:
        async with self._continuation_admission() as admitted:
            if not admitted:
                return
            goal = await self._goal_service.get(session_id)
            if goal is None or goal.status != GoalStatus.active:
                return
            jobs = await self._job_service.list(session_id=session_id)
            main_thread_id = resolved_main_thread_id
            if main_thread_id is None:
                main_thread_id = await self._session_orchestrator.resolve_main_thread(
                    session_id
                )
            if any(item.thread_id == main_thread_id
                   and item.status.value in {"queued", "running", "accepted"} for item in jobs):
                return

            async def dispatch(goal: SessionGoalDTO) -> None:
                await self._submit_goal(
                    goal,
                    continuation_message(
                        goal,
                        metadata={
                            "goal_continuation": True,
                            "goal_id": goal.goal_id,
                            "goal_objective": goal.objective,
                        },
                    ),
                    event_identity=f"idle:{goal.goal_id}:{goal.revision}",
                )

            await self._goal_service.dispatch_continuation_if_active(
                session_id, f"idle:{goal.goal_id}:{goal.revision}", dispatch
            )

    async def apply_objective_update(self, goal: SessionGoalDTO) -> None:
        """让排队或正在执行的同一 Goal 尽快看到外部编辑后的目标。"""
        async with self._continuation_admission() as admitted:
            if not admitted:
                return
            main_thread_id = await self._session_orchestrator.resolve_main_thread(goal.session_id)
            running_job_ids = [
                job.job_id
                for job in await self._job_service.list(session_id=goal.session_id)
                if job.thread_id == main_thread_id
                and job.status.value in {"accepted", "running", "streaming"}
                and self._job_goal_ids.get(job.job_id) == goal.goal_id
            ]

            async def dispatch(current: SessionGoalDTO) -> None:
                await self._submit_goal(
                    current,
                    objective_updated_message(
                        current,
                        metadata={
                            "goal_continuation": True,
                            "goal_id": current.goal_id,
                            "goal_objective": current.objective,
                            "goal_objective_updated": True,
                        },
                    ),
                    event_identity=f"objective:{goal.revision}",
                    delivery_policy="after_turn",
                )

            for job_id in running_job_ids:
                self._job_goal_ids.pop(job_id, None)
            try:
                await self._goal_service.dispatch_continuation_if_active(
                    goal.session_id,
                    f"objective:{goal.goal_id}:{goal.revision}",
                    dispatch,
                )
            except BaseException:
                for job_id in running_job_ids:
                    self._job_goal_ids[job_id] = goal.goal_id
                raise

    async def settle_active_progress(self, session_id: str) -> None:
        """外部暂停/清除前冻结当前 Job 的 Goal 计时时间点。"""
        goal = await self._goal_service.get(session_id)
        if goal is None:
            return
        for job in await self._job_service.list(session_id=session_id):
            if job.status.value not in {"accepted", "queued", "running", "streaming"}:
                continue
            if self._job_goal_ids.get(job.job_id) != goal.goal_id:
                continue
            elapsed_seconds = max(
                0,
                int(
                    (
                        datetime.now(job.created_at.tzinfo) - job.created_at
                    ).total_seconds()
                ),
            )
            await self._goal_service.account_job(
                session_id,
                job_id=job.job_id,
                tokens=0,
                elapsed_seconds=elapsed_seconds,
                close_time=True,
            )
