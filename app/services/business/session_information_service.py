from __future__ import annotations

from datetime import datetime, timezone

from app.core.path_utils import get_session_path
from app.schemas.public_v2.session import (
    SessionInformationErrorDTO,
    SessionInformationExecutionDTO,
    SessionInformationResourceDTO,
    SessionInformationSnapshotDTO,
    SessionInformationTraceDTO,
    SessionInformationWorkspaceDTO,
)
from app.schemas.public_v2.trace import TraceEventDTO
from app.services.business.session_resource_service import SessionResourceService
from app.services.business.session_service import SessionService
from app.services.infrastructure.workspace_service import WorkspaceService


_TERMINAL_STATUS_BY_EVENT_TYPE = {
    "job_completed": "completed",
    "job_cancelled": "cancelled",
    "job_failed": "failed",
    "session_interrupted": "cancelled",
}


class SessionInformationService:
    """组合可供用户和软件共同消费的会话权威信息。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        session_resource_service: SessionResourceService,
        workspace_service: WorkspaceService,
    ) -> None:
        self._session_service = session_service
        self._session_resource_service = session_resource_service
        self._workspace_service = workspace_service

    async def get_information(self, session_id: str) -> SessionInformationSnapshotDTO:
        session = await self._session_service.get(session_id)
        sessions = await self._session_service.list(limit=100_000)
        workspace = await self._workspace_service.get()
        trace_events = await self._session_service.list_trace_events(session_id)
        resources = await self._session_resource_service.list(session_id)

        if session.workspace_id != workspace.workspace_id:
            raise RuntimeError(
                "会话与工作区后端标识不一致: "
                f"session_id={session_id}, session_workspace_id={session.workspace_id}, "
                f"backend_workspace_id={workspace.workspace_id}"
            )

        child_session_ids = sorted(
            item.session_id
            for item in sessions.items
            if item.parent_session_id == session_id
        )
        return SessionInformationSnapshotDTO(
            generated_at=datetime.now(timezone.utc),
            session=session,
            child_session_ids=child_session_ids,
            workspace=SessionInformationWorkspaceDTO(
                workspace_id=workspace.workspace_id,
                name=workspace.name,
                root_path=workspace.root_path,
            ),
            storage_path=str(get_session_path(session_id)),
            execution=self._build_execution(trace_events),
            trace=self._build_trace(trace_events),
            resources=[
                SessionInformationResourceDTO(
                    resource_id=item.resource_id,
                    kind=item.kind,
                    name=item.name,
                    status=item.status,
                    updated_at=item.updated_at,
                )
                for item in resources.items
            ],
            recent_errors=self._build_recent_errors(trace_events),
        )

    @staticmethod
    def _build_execution(
        trace_events: list[TraceEventDTO],
    ) -> SessionInformationExecutionDTO:
        if not trace_events:
            return SessionInformationExecutionDTO()

        latest_job_id = trace_events[-1].job_id
        job_events = [event for event in trace_events if event.job_id == latest_job_id]
        status = "running"
        current_tool: str | None = None
        last_error: str | None = None

        for event in job_events:
            if event.type == "job_created":
                status = "queued"
            elif event.type in _TERMINAL_STATUS_BY_EVENT_TYPE:
                status = _TERMINAL_STATUS_BY_EVENT_TYPE[event.type]
            elif event.type == "status_change":
                raw_payload = event.raw.get("payload")
                raw_status = raw_payload.get("status") if isinstance(raw_payload, dict) else None
                if isinstance(raw_status, str) and raw_status:
                    status = raw_status
            elif event.type not in {"tool_call_end", "agent_end", "text_end"}:
                status = "running"

            if event.type == "tool_call_start":
                current_tool = event.tool_name
            elif event.type == "tool_call_end":
                current_tool = None

            if event.type in {"error", "job_failed"}:
                last_error = event.content

        return SessionInformationExecutionDTO(
            job_id=latest_job_id,
            status=status,
            current_tool=current_tool,
            last_error=last_error,
        )

    @staticmethod
    def _build_trace(trace_events: list[TraceEventDTO]) -> SessionInformationTraceDTO:
        if not trace_events:
            return SessionInformationTraceDTO()
        latest = trace_events[-1]
        return SessionInformationTraceDTO(
            event_count=len(trace_events),
            last_event_id=latest.event_id,
            last_event_type=latest.type,
            last_event_at=latest.timestamp,
        )

    @staticmethod
    def _build_recent_errors(
        trace_events: list[TraceEventDTO],
    ) -> list[SessionInformationErrorDTO]:
        errors = [
            SessionInformationErrorDTO(
                event_id=event.event_id,
                job_id=event.job_id,
                type=event.type,
                message=event.content,
                timestamp=event.timestamp,
            )
            for event in trace_events
            if event.type in {"error", "job_failed", "session_interrupted"}
        ]
        return errors[-5:]
