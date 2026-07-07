from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_resources import (
    BackgroundTaskRegistryProtocol,
    HistoricalTerminalRecordReaderProtocol,
    SessionResourceMessageProtocol,
    TerminalManagerClientProtocol,
)
from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.public_v2.common import ControlAction
from app.schemas.public_v2.job import JobControlRequest, JobDTO
from app.schemas.public_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
    SessionResourceListDTO,
)
from app.services.business.session_resource_actions import (
    background_task_available_actions,
    job_available_actions,
    job_progress_note,
    terminal_available_actions,
)
from app.services.business.session_service import SessionService
from app.services.mapping.session_resource_mapper import SessionResourceMapper


@dataclass(frozen=True, slots=True)
class SessionResourceCleanupResult:
    cleaned_jobs: int = 0
    cleaned_background_tasks: int = 0
    cleaned_terminals: int = 0


class SessionResourceService:
    def __init__(
        self,
        *,
        session_service: SessionService,
        job_service: JobServiceProtocol,
        background_task_registry: BackgroundTaskRegistryProtocol,
        terminal_manager_client: TerminalManagerClientProtocol,
        historical_terminal_reader: HistoricalTerminalRecordReaderProtocol,
        message_service: SessionResourceMessageProtocol,
        resource_mapper: SessionResourceMapper,
    ) -> None:
        self._session_service = session_service
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        self._terminal_manager_client = terminal_manager_client
        self._historical_terminal_reader = historical_terminal_reader
        self._message_service = message_service
        self._resource_mapper = resource_mapper

    async def list(self, session_id: str) -> SessionResourceListDTO:
        await self._session_service.get(session_id)
        jobs = await self._job_service.list(session_id=session_id)
        background_tasks = self._background_task_registry.list_handles(session_id)
        terminals = self._terminal_manager_client.list_terminals_from_state(session_id)
        historical_terminals = await self._historical_terminal_records(
            session_id=session_id,
            active_terminals=terminals,
        )

        items = [
            *[self._job_to_resource(job) for job in jobs],
            *[self._background_task_to_resource(task) for task in background_tasks],
            *[self._terminal_to_resource(terminal) for terminal in terminals],
            *[self._terminal_to_resource(terminal) for terminal in historical_terminals],
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return SessionResourceListDTO(session_id=session_id, items=items)

    async def control(
        self,
        *,
        session_id: str,
        kind: SessionResourceKind,
        resource_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        await self._session_service.get(session_id)
        if kind == "job":
            return await self._control_job(
                session_id=session_id,
                job_id=resource_id,
                action=action,
            )
        if kind == "terminal":
            return await self._control_terminal(
                session_id=session_id,
                terminal_id=resource_id,
                action=action,
            )
        return await self._control_background_task(
            session_id=session_id,
            task_id=resource_id,
            action=action,
        )

    async def cleanup_session(self, session_id: str) -> SessionResourceCleanupResult:
        await self._session_service.get(session_id)
        cleaned_jobs = await self._job_service.delete_session_jobs(session_id)
        cleaned_background_tasks = await self._background_task_registry.delete_session(session_id)
        cleaned_terminals = 0

        for terminal in self._terminal_manager_client.list_terminals_from_state(session_id):
            terminal_id = terminal.get("terminal_id")
            if not isinstance(terminal_id, str) or not terminal_id:
                raise RuntimeError(f"终端记录缺少 terminal_id: session_id={session_id}")
            await self._terminal_manager_client.delete_terminal(terminal_id)
            cleaned_terminals += 1

        return SessionResourceCleanupResult(
            cleaned_jobs=cleaned_jobs,
            cleaned_background_tasks=cleaned_background_tasks,
            cleaned_terminals=cleaned_terminals,
        )

    async def _control_job(
        self,
        *,
        session_id: str,
        job_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        if action == "delete":
            raise ValueError("job 资源暂不支持 delete 操作")
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError(f"job 资源不支持操作: {action}")

        existing_job = await self._job_service.get(job_id)
        if existing_job.session_id != session_id:
            raise ValueError(f"job {job_id} 不属于 session {session_id}")

        result = await self._job_service.control(
            job_id,
            JobControlRequest(action=ControlAction(action)),
        )
        job = await self._job_service.get(job_id)

        return SessionResourceControlResultDTO(
            session_id=session_id,
            resource_id=job_id,
            kind="job",
            action=action,
            status=result.status.value,
            resource=self._job_to_resource(job),
        )

    async def _control_background_task(
        self,
        *,
        session_id: str,
        task_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        if action == "cancel":
            existing_handle = self._background_task_registry.get_handle(session_id, task_id)
            if existing_handle is None:
                raise ValueError(f"后台任务不存在: session_id={session_id}, task_id={task_id}")
            handle = await self._background_task_registry.cancel(session_id, task_id)
            if existing_handle.task_name == "monitor_session_agent_end":
                self._append_background_task_cancel_reminder(handle)
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=task_id,
                kind="background_task",
                action=action,
                status=handle.status,
                resource=self._background_task_to_resource(handle),
            )
        if action == "delete":
            handle = await self._background_task_registry.delete(session_id, task_id)
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=task_id,
                kind="background_task",
                action=action,
                status="deleted",
                resource=self._background_task_to_resource(handle),
            )

        raise ValueError(f"background_task 资源不支持操作: {action}")

    def _append_background_task_cancel_reminder(self, handle: BackgroundTaskHandle) -> None:
        cancelled_at = datetime.now(timezone.utc).isoformat()
        target_session_id = handle.metadata.get("target_session_id")
        target_text = (
            f"目标 session：{target_session_id}。"
            if isinstance(target_session_id, str) and target_session_id
            else ""
        )
        reminder = (
            f"用户于 {cancelled_at} 通过资源视图手动取消了后台任务 "
            f"{handle.task_name}（task_id={handle.task_id}）。"
            f"{target_text}"
            "该监控任务不会继续监听 AGENT_END，也不会再自动转发目标回复。"
            "请不要等待这个后台任务继续产生消息，后续按用户最新请求继续。"
        )
        injected = self._message_service.append_system_reminder(
            session_id=handle.session_id,
            reminder=reminder,
            response_metadata={
                "phase": "background_task",
                "source": "resource_cancel",
                "user_initiated": True,
                "task_id": handle.task_id,
                "task_name": handle.task_name,
                "action": "cancel",
                "target_session_id": target_session_id,
                "cancelled_at": cancelled_at,
            },
            checkpoint_source="resource_cancel",
        )
        if not injected:
            raise RuntimeError(
                f"后台任务已取消，但未找到可注入 system_reminder 的 checkpoint: "
                f"session_id={handle.session_id}, task_id={handle.task_id}"
            )

    async def _control_terminal(
        self,
        *,
        session_id: str,
        terminal_id: str,
        action: SessionResourceAction,
    ) -> SessionResourceControlResultDTO:
        terminal = self._get_terminal_record(session_id, terminal_id)
        if action == "cancel":
            result = await self._terminal_manager_client.kill_terminal(terminal_id)
            resource = self._terminal_to_resource(result["terminal"])
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=terminal_id,
                kind="terminal",
                action=action,
                status=resource.status,
                resource=resource,
            )
        if action == "delete":
            result = await self._terminal_manager_client.delete_terminal(terminal_id)
            deleted_terminal = result.get("terminal") if isinstance(result, dict) else None
            resource = self._terminal_to_resource(
                deleted_terminal if isinstance(deleted_terminal, dict) else {**terminal, "status": "deleted"}
            )
            return SessionResourceControlResultDTO(
                session_id=session_id,
                resource_id=terminal_id,
                kind="terminal",
                action=action,
                status="deleted",
                resource=resource,
            )

        raise ValueError(f"terminal 资源不支持操作: {action}")

    async def _historical_terminal_records(
        self,
        *,
        session_id: str,
        active_terminals: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        records = await self._message_service.list_agent_state_records(session_id)
        return self._historical_terminal_reader.read_records(
            session_id=session_id,
            active_terminals=active_terminals,
            agent_state_records=records,
        )

    def _get_terminal_record(self, session_id: str, terminal_id: str) -> dict[str, object]:
        terminals = self._terminal_manager_client.list_terminals_from_state(session_id)
        for terminal in terminals:
            if terminal.get("terminal_id") == terminal_id:
                return terminal
        raise ValueError(f"terminal 不存在或不属于当前 session: {terminal_id}")

    def _job_to_resource(self, job: JobDTO) -> SessionResourceDTO:
        return self._resource_mapper.job_to_resource(
            job,
            available_actions=job_available_actions(job.status),
            progress_note=job_progress_note(job.status, job.progress),
        )

    def _background_task_to_resource(self, handle: BackgroundTaskHandle) -> SessionResourceDTO:
        return self._resource_mapper.background_task_to_resource(
            handle,
            available_actions=background_task_available_actions(handle.status),
        )

    def _terminal_to_resource(self, terminal: dict[str, object]) -> SessionResourceDTO:
        status = str(terminal.get("status") or "unknown")
        return self._resource_mapper.terminal_to_resource(
            terminal,
            available_actions=terminal_available_actions(status),
        )
