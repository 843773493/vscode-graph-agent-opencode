"""Session 用户打断服务：取消正在运行的任务，并在进程内通知。"""
from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.turn_history import TurnSessionLookupProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    TurnControlCoordinator,
    TurnExecutionScopeRegistry,
)
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.schemas.internal_v2.common import ControlAction
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.session import SessionInterruptResultDTO
from app.services.business.job.lifecycle import PAUSABLE_JOB_STATUSES
from app.services.business.message_service import MessageService
from app.services.business.system_reminder_checkpoint_service import (
    build_user_interrupt_reminder,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore


class SessionInterruptService:
    def __init__(
        self,
        *,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
        message_service: MessageService,
        message_stream_store: MessageStreamStore,
        session_service: TurnSessionLookupProtocol,
        execution_scope_registry: TurnExecutionScopeRegistry | None = None,
    ) -> None:
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._message_service = message_service
        self._message_stream_store = message_stream_store
        self._session_service = session_service
        self._execution_scope_registry = execution_scope_registry

    async def interrupt(self, session_id: str, *, thread_id: str | None = None) -> SessionInterruptResultDTO:
        # 仅在解析身份时持有读准入；JobService 自有准入不可嵌套同一 Session gate。
        async with self._session_service.session_read_admission(session_id):
            target_thread = (
                await self._session_service.resolve_main_thread(session_id)
                if thread_id is None
                else await self._session_service.resolve_thread(session_id, thread_id)
            )
        return await self._interrupt_thread(session_id, target_thread)

    async def _interrupt_thread(self, session_id: str, thread_id: str) -> SessionInterruptResultDTO:
        jobs = await self._job_service.list(session_id=session_id)
        active_job = next(
            (
                job
                for job in jobs
                if job.session_id == session_id and job.thread_id == thread_id
                and job.status in PAUSABLE_JOB_STATUSES
            ),
            None,
        )
        if active_job is None:
            raise ValueError(f"SessionThread {session_id}/{thread_id} 当前没有正在运行的任务")

        # 选中后冻结 Job，绑定已消失时明确失败，不能重选后继执行或其它 Thread。
        execution_binding = self._job_service.get_execution_binding(active_job.job_id)
        admission = self._message_service.execution_admission_for_job(
            session_id, thread_id=thread_id, job_id=active_job.job_id,
        )
        if (
            (admission.session_id, admission.thread_id, admission.job_id)
            != (session_id, thread_id, active_job.job_id)
            or admission.admission_idempotency_key != execution_binding.admission_idempotency_key
        ):
            raise RuntimeError(f"中断目标与 execution admission 归属不一致: {session_id}/{thread_id}/{active_job.job_id}")

        if isinstance(execution_binding, UserExecutionIdentity):
            if (execution_binding.job_id != active_job.job_id
                    or admission.turn_id != execution_binding.turn_id
                    or admission.execution_origin != "user_turn"):
                raise RuntimeError(
                    "active Job 与 user execution identity 不一致: "
                    f"job_id={active_job.job_id!r} "
                    f"identity_job_id={execution_binding.job_id!r}"
                )
            turn_id = execution_binding.turn_id
        elif isinstance(execution_binding, InternalExecutionIntent):
            if (admission.execution_id != execution_binding.execution_id
                    or admission.turn_id is not None
                    or admission.execution_origin != "internal"
                    or active_job.message_id != execution_binding.message_id):
                raise RuntimeError("中断目标与 internal execution identity 不一致")
            turn_id = None
        else:
            raise TypeError(
                "JobService 返回了非法 execution binding: "
                f"{type(execution_binding).__name__}"
            )

        state = SessionInterruptState.get(session_id, thread_id=thread_id)
        phase = state.phase or "text"
        tool_name = state.tool_name
        interrupted_at = datetime.now(UTC)
        interrupt_request_id = create_prefixed_id("intr")

        cancellation_prepared = False

        def prepare_cancellation(*, reminder_persisted: bool = False) -> None:
            nonlocal cancellation_prepared
            # durable accept 后、取消信号前同步发布；runner 收尾必须先看到该身份。
            SessionInterruptState.set(
                session_id, thread_id=thread_id,
                interrupt_request_id=interrupt_request_id,
                cancellation_reason="user_requested",
            )
            cancellation_prepared = True
            if not reminder_persisted:
                self._append_user_interrupt_reminder(
                    session_id=session_id, thread_id=thread_id,
                    execution_id=admission.execution_id, phase=phase,
                    tool_name=tool_name, interrupted_at=interrupted_at,
                )
            SessionInterruptState.set(
                session_id, thread_id=thread_id,
                user_interrupt_reminder_injected=True,
            )

        try:
            message_stream = None
            control_inbox = None
            if turn_id is None:
                reminder, metadata = self._user_interrupt_reminder_payload(
                    phase=phase, tool_name=tool_name, interrupted_at=interrupted_at,
                    interrupt_request_id=interrupt_request_id,
                )
                async with self._session_service.session_read_admission(session_id):
                    try:
                        result = self._message_service.request_internal_execution_interrupt(
                            admission, interrupt_request_id=interrupt_request_id,
                            reminder=reminder, response_metadata=metadata,
                        )
                    finally:
                        # 接受提交后 lease 结算失败也必须取消原 Job；读取 rollout
                        # 事实，不能凭创建 lease 或本地 callback 猜测接受成功。
                        accepted = self._message_service.internal_execution_interrupt(admission)
                        if accepted is not None:
                            interrupt_request_id = accepted.interrupt_request_id
                            prepare_cancellation(reminder_persisted=True)
                if accepted is None:
                    if not isinstance(result, str):
                        raise RuntimeError("内部中断缺少接受事实或终态拒绝")
                    return SessionInterruptResultDTO(
                        session_id=session_id, thread_id=thread_id, job_id=active_job.job_id,
                        status={"completed_empty": "completed", "execution_lost": "failed"}.get(result, result),
                        interrupt_request_id=interrupt_request_id, phase=phase,
                        tool_name=tool_name, interrupted_at=interrupted_at,
                    )
                if self._execution_scope_registry is not None:
                    await self._execution_scope_registry.cancel(admission.execution_id, "user_requested")
            if turn_id is not None:
                message_stream = await self._message_stream_store.open(
                    session_id=session_id,
                    thread_id=active_job.thread_id,
                    turn_id=turn_id,
                    job_id=active_job.job_id,
                )
                control_inbox = (
                    self._execution_scope_registry.get_inbox(message_stream.turn_stream_id)
                    if self._execution_scope_registry is not None
                    else None
                )
                scope = (
                    self._execution_scope_registry.get(message_stream.turn_stream_id)
                    if self._execution_scope_registry is not None
                    else None
                )
                if control_inbox is not None and scope is not None:
                    interrupt_event = await TurnControlCoordinator(
                        scope=scope,
                        inbox=control_inbox,
                        writer=message_stream,
                    ).submit_interrupt(
                        command_id=interrupt_request_id,
                        idempotency_key=interrupt_request_id,
                        on_accepted=prepare_cancellation,
                    )
                else:
                    # AgentLoop 尚未完成 scope 注册时仍需先持久化控制事实；后续
                    # AgentExecutionService 会从消息流终态判断这次早期中断。
                    interrupt_event = await message_stream.commit(
                        "interrupt.requested",
                        {
                            "interrupt_request_id": interrupt_request_id,
                            "reason": "user_requested",
                        },
                        event_id=interrupt_request_id,
                    )
                if interrupt_event["type"] == "interrupt.rejected":
                    stream_state = await self._message_stream_store.get_state(
                        message_stream.turn_stream_id
                    )
                    return SessionInterruptResultDTO(
                        session_id=session_id,
                        thread_id=thread_id,
                        job_id=active_job.job_id,
                        status=str(stream_state["stream_status"]),
                        interrupt_request_id=interrupt_request_id,
                        phase=phase,
                        tool_name=tool_name,
                        interrupted_at=interrupted_at,
                    )
            if turn_id is not None and (control_inbox is None or scope is None):
                try:
                    prepare_cancellation()
                finally:
                    if message_stream is not None and self._execution_scope_registry is not None:
                        await self._execution_scope_registry.cancel(
                            message_stream.turn_stream_id,
                            "user_requested",
                        )
        finally:
            # 接受身份发布后，即使 reminder 或 scope hook 失败，也必须通知已冻结的 Job。
            if cancellation_prepared:
                control_result = await self._job_service.control(
                    active_job.job_id,
                    JobControlRequest(action=ControlAction.cancel),
                )

        await self._job_service.notify_boundary(
            session_id,
            "after_interrupt",
            thread_id=active_job.thread_id,
            tool_result_available=False,
        )

        if self._job_event_bus is not None:
            await self._job_event_bus.publish(
                job_id=active_job.job_id,
                event_type=EventType.SESSION_INTERRUPTED,
                payload={
                    "session_id": session_id,
                    "phase": phase,
                    "tool_name": tool_name,
                    "interrupted_at": interrupted_at.isoformat(),
                    "interrupt_request_id": interrupt_request_id,
                },
                agent_id="session_interrupt_service",
            )

        return SessionInterruptResultDTO(
            session_id=session_id,
            thread_id=thread_id,
            job_id=active_job.job_id,
            status=control_result.status.value,
            interrupt_request_id=interrupt_request_id,
            phase=phase,
            tool_name=tool_name,
            interrupted_at=interrupted_at,
        )

    def _append_user_interrupt_reminder(
        self,
        *,
        session_id: str,
        thread_id: str,
        execution_id: str,
        phase: str,
        tool_name: str | None,
        interrupted_at: datetime,
    ) -> bool:
        reminder, metadata = self._user_interrupt_reminder_payload(
            phase=phase, tool_name=tool_name, interrupted_at=interrupted_at,
            interrupt_request_id=SessionInterruptState.get(session_id, thread_id=thread_id).interrupt_request_id,
        )
        injected = self._message_service.submit_system_reminder(
            session_id=session_id,
            thread_id=thread_id,
            execution_id=execution_id,
            reminder=reminder,
            response_metadata=metadata,
            checkpoint_source="user_interrupt",
            event_identity=str(metadata["checkpoint_event_id"]),
        )
        if not injected:
            raise RuntimeError(
                f"用户主动取消时未找到可注入 system_reminder 的 checkpoint: "
                f"session_id={session_id} phase={phase} tool={tool_name}"
            )
        return injected

    @staticmethod
    def _user_interrupt_reminder_payload(
        *, phase: str, tool_name: str | None, interrupted_at: datetime,
        interrupt_request_id: str,
    ) -> tuple[str, dict[str, object]]:
        return build_user_interrupt_reminder(
            phase=phase, active_tool_name=tool_name, interrupted_at=interrupted_at,
        ), {
            "phase": phase, "tool_name": tool_name, "source": "user_interrupt",
            "user_initiated": True, "interrupted_at": interrupted_at.isoformat(),
            "checkpoint_event_id": interrupt_request_id,
        }
