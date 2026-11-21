"""会话停止编排：关闭准入、按策略排空；会话数据删除仍归删除 owner。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Literal, TypeVar

from app.abstractions.job_service import JobServiceProtocol
from app.core.session_lifecycle_gate import OperationHolderGate
from app.domain.session_stop import DELETE_SESSION_POLICY, SessionStopPolicy
from app.domain.itemized.execution_queue import ExecutionQueuePause
from app.schemas.internal_v2.execution_control import ThreadExecutionControlDTO, ThreadExecutionControlRequest
from app.schemas.internal_v2.session import (
    SessionControlResultDTO,
    SessionInterruptResultDTO,
)
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_resources.registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.node_debug.service import NodeDebugService

T = TypeVar("T")


class SessionStopService:
    def __init__(
        self, *, sessions: SessionService, jobs: JobServiceProtocol,
        interrupt: SessionInterruptService, resources: SessionResourceProviderRegistry,
        debug: NodeDebugService,
        policy: Callable[[Literal["interrupt", "stop"]], SessionStopPolicy],
        drain_collaboration: Callable[[str], Awaitable[None]],
        pause_goal: Callable[[str, str | None], Awaitable[None]],
        close_runtime: Callable[[str, str | None], Awaitable[None]],
        holders: tuple[OperationHolderGate, ...],
        advancement_changed: Callable[[], None],
        pause_automations: Callable[[str, str | None], Awaitable[None]],
        resume_execution: Callable[[ExecutionQueuePause], Awaitable[str]],
    ) -> None:
        self._sessions = sessions
        self._jobs = jobs
        self._interrupt = interrupt
        self._resources = resources
        self._debug = debug
        self._policy = policy
        self._drain_collaboration = drain_collaboration
        self._pause_goal = pause_goal
        self._close_runtime = close_runtime
        self._holders = holders
        self._advancement_changed = advancement_changed
        self._pause_automations = pause_automations
        self._resume_execution = resume_execution

    async def execution_control_snapshot(self, session_id: str, thread_id: str) -> ThreadExecutionControlDTO:
        async with self._sessions.session_read_admission(session_id):
            await self._sessions.resolve_thread(session_id, thread_id)
            return await self._jobs.thread_control_snapshot(session_id, thread_id)

    async def control_execution(
        self, session_id: str, thread_id: str, payload: ThreadExecutionControlRequest,
    ) -> ThreadExecutionControlDTO:
        # 身份校验不唤醒 runtime，执行 CAS 由 Job 调度锁保护。
        async with self._sessions.session_read_admission(session_id):
            await self._sessions.resolve_thread(session_id, thread_id)
        if payload.action in {"pause", "resume"}:
            async with self._jobs.thread_input_admission(session_id, thread_id):
                result = await self._jobs.control_thread_advancement(
                    session_id, thread_id, action=payload.action,
                    expected_execution_id=payload.expected_execution_id,
                    resume_execution=self._resume_execution,
                )
            self._advancement_changed()
            return result
        expected = (thread_id, payload.expected_execution_id)
        if payload.action == "interrupt":
            await self.interrupt(session_id, thread_id=thread_id, expected=expected)
        else:
            await self.stop(session_id, expected=expected, preserve_pending=True)
        self._advancement_changed()
        return await self.execution_control_snapshot(session_id, thread_id)

    async def _check_expected(self, session_id: str, expected: tuple[str, str | None] | None) -> None:
        if expected is not None:
            snapshot = await self._jobs.thread_control_snapshot(session_id, expected[0])
            if snapshot.execution_id != expected[1]:
                raise ValueError("执行已变化，请刷新控制状态后重试")

    async def interrupt(
        self, session_id: str, *, thread_id: str | None = None,
        expected: tuple[str, str | None] | None = None,
    ) -> SessionInterruptResultDTO:
        policy = self._policy("interrupt")

        async def run() -> SessionInterruptResultDTO:
            await self._check_expected(session_id, expected)
            result = await self._interrupt.interrupt(
                session_id, thread_id=thread_id,
            )
            target_thread = result.thread_id if policy.scope == "thread" else None
            if policy.goal == "pause":
                await self._pause_goal(session_id, target_thread)
            await self._jobs.stop_session_jobs(
                session_id, thread_id=target_thread,
                delete_records=policy.jobs == "delete",
                discard_pending=policy.pending_messages == "discard",
                pause_pending=policy.pending_messages == "keep",
            )
            await self._drain_active_resources(session_id, policy, target_thread)
            return result

        result = await self._run_control(session_id, run)
        if policy.pending_messages == "continue" and result.status in {"cancelling", "cancelled"}:
            await self._jobs.notify_boundary(
                session_id, "after_interrupt", thread_id=result.thread_id,
                tool_result_available=False,
            )
        return result

    async def stop(
        self, session_id: str, *, expected: tuple[str, str | None] | None = None,
        preserve_pending: bool = False,
    ) -> SessionControlResultDTO:
        policy = self._policy("stop")
        if preserve_pending:
            # Composer 的完全打断覆盖整个会话，但不隐含删除用户的排队输入。
            policy = policy.model_copy(update={"scope": "session", "jobs": "cancel", "pending_messages": "keep"})
        async with self._sessions.session_read_admission(session_id):
            await self._sessions.get(session_id)
            target_thread = (
                await self._sessions.resolve_main_thread(session_id)
                if policy.scope == "thread" else None
            )

        async def run() -> SessionControlResultDTO:
            await self._check_expected(session_id, expected)
            await self._pause_automations(session_id, target_thread)
            if policy.goal == "pause":
                await self._pause_goal(session_id, target_thread)
            await self._jobs.stop_session_jobs(
                session_id, thread_id=target_thread,
                delete_records=policy.jobs == "delete",
                discard_pending=policy.pending_messages == "discard",
                pause_pending=policy.pending_messages == "keep",
            )
            await self._drain_active_resources(session_id, policy, target_thread)
            return SessionControlResultDTO(session_id=session_id, action="stop", status="stopped")

        return await self._run_control(session_id, run)

    async def _run_control(self, session_id: str, operation: Callable[[], Awaitable[T]]) -> T:
        deadline = asyncio.get_running_loop().time() + 30
        async with asyncio.timeout_at(deadline):
            return await self._jobs.run_session_control_operation(
                session_id, operation, deadline=deadline,
            )

    async def drain_for_deletion(self, session_id: str) -> None:
        """删除 owner 已关闭准入并取得原 holder/gate；不重复取锁或删除历史。"""
        await self._sessions.get(session_id)
        await self._jobs.stop_session_jobs(session_id)
        await self._drain_resources(session_id, DELETE_SESSION_POLICY, None)

    @staticmethod
    def _has_resources(policy: SessionStopPolicy) -> bool:
        return any(action != "keep" for action in (
            policy.debug, policy.background_task, policy.terminal, policy.browser, policy.collaboration,
            policy.thread_runtime,
        ))

    async def _drain_active_resources(
        self, session_id: str, policy: SessionStopPolicy, thread_id: str | None,
    ) -> None:
        if not self._has_resources(policy):
            return
        # 原 holder 先退出，旧操作才有机会取得 Session gate 完成结算。
        async with AsyncExitStack() as scope:
            for holder in self._holders:
                await scope.enter_async_context(holder.lock_for(session_id))
            await scope.enter_async_context(self._sessions.session_write_admission(session_id))
            await self._drain_resources(session_id, policy, thread_id)

    async def _drain_resources(
        self, session_id: str, policy: SessionStopPolicy, thread_id: str | None,
    ) -> None:
        if policy.debug == "stop":
            await self._debug.drain_session(session_id, thread_id=thread_id)
        await self._resources.cleanup_session(session_id, policy=policy, thread_id=thread_id)
        if policy.collaboration == "stop":
            await self._drain_collaboration(session_id)
        if policy.thread_runtime == "close":
            await self._close_runtime(session_id, thread_id)
