from __future__ import annotations

from datetime import UTC, datetime

from app.core.session_paths import SessionPathResolver
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionInformationErrorDTO,
    SessionInformationExecutionDTO,
    SessionInformationRelationsDTO,
    SessionInformationResourceDTO,
    SessionInformationResourceSummaryDTO,
    SessionInformationSessionDTO,
    SessionInformationSnapshotDTO,
    SessionInformationTraceDTO,
    SessionInformationWorkspaceDTO,
)
from app.schemas.internal_v2.session_resource import SessionResourceDTO
from app.schemas.internal_v2.trace import TraceEventDTO
from app.services.business.session_resource_service import SessionResourceService
from app.services.business.session_service import SessionService
from app.services.infrastructure.workspace_service import WorkspaceService

_TERMINAL_STATUS_BY_EVENT_TYPE = {
    "job_completed": "completed",
    "job_cancelled": "cancelled",
    "job_failed": "failed",
    "session_interrupted": "cancelled",
}

_CHILD_SESSION_ID_LIMIT = 32
_ACTIVE_RESOURCE_LIMIT = 32
_RECENT_CLOSED_RESOURCE_LIMIT = 16
_RECENT_ERROR_LIMIT = 5
_DIAGNOSTIC_TEXT_LIMIT = 2_048
_DIAGNOSTIC_TITLE_LIMIT = 512
_DIAGNOSTIC_RESOURCE_NAME_LIMIT = 512
_TRACE_PAGE_LIMIT = 100
_ACTIVE_RESOURCE_STATUSES = {
    "pending",
    "created",
    "running",
    "frozen",
    "discarded",
    "queued",
    "accepted",
    "streaming",
    "waiting_input",
    "paused",
    "interrupt_pending",
    "cancelling",
}


class SessionInformationService:
    """组合可供用户和软件共同消费的会话权威信息。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        session_resource_service: SessionResourceService,
        workspace_service: WorkspaceService,
        path_resolver: SessionPathResolver,
    ) -> None:
        self._session_service = session_service
        self._session_resource_service = session_resource_service
        self._workspace_service = workspace_service
        self._path_resolver = path_resolver

    async def get_information(self, session_id: str) -> SessionInformationSnapshotDTO:
        session = await self._session_service.get(session_id)
        workspace = await self._workspace_service.get()
        if session.workspace_id != workspace.workspace_id:
            raise RuntimeError(
                "会话与工作区后端标识不一致: "
                f"session_id={session_id}, session_workspace_id={session.workspace_id}, "
                f"backend_workspace_id={workspace.workspace_id}"
            )
        trace_page = await self._session_service.list_trace_events(
            session_id,
            limit=_TRACE_PAGE_LIMIT,
        )
        trace_events = trace_page.items
        resources = await self._session_resource_service.list(
            session_id,
            include_history=False,
        )
        child_count, child_ids, child_ids_truncated = (
            await self._session_service.child_session_summary(
                session_id,
                limit=_CHILD_SESSION_ID_LIMIT,
            )
        )

        return SessionInformationSnapshotDTO(
            generated_at=datetime.now(UTC),
            session=self._build_session(session),
            workspace=SessionInformationWorkspaceDTO(
                workspace_id=workspace.workspace_id,
                name=workspace.name,
                root_path=workspace.root_path,
            ),
            storage_path=str(self._path_resolver.resolve_session_node(session_id)),
            execution=self._build_execution(trace_events),
            trace=self._build_trace(trace_events, truncated=trace_page.has_more),
            relations=SessionInformationRelationsDTO(
                child_count=child_count,
                child_ids=child_ids,
                child_ids_truncated=child_ids_truncated,
            ),
            resources=self._build_resource_summary(resources.items),
            recent_errors=self._build_recent_errors(trace_events),
        )

    @staticmethod
    def _build_session(session: SessionDTO) -> SessionInformationSessionDTO:
        title, title_truncated = _truncate_text(
            session.title,
            limit=_DIAGNOSTIC_TITLE_LIMIT,
        )
        return SessionInformationSessionDTO(
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            title=title,
            current_agent_id=session.current_agent_id,
            current_provider_id=session.current_provider_id,
            parent_session_id=session.parent_session_id,
            context_source_session_id=session.context_source_session_id,
            kind=session.kind,
            created_at=session.created_at,
            updated_at=session.updated_at,
            title_truncated=title_truncated,
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
        last_error_truncated = False

        for event in job_events:
            if event.type == "job_created":
                status = "queued"
            elif event.type in _TERMINAL_STATUS_BY_EVENT_TYPE:
                status = _TERMINAL_STATUS_BY_EVENT_TYPE[event.type]
                if event.type == "session_interrupted":
                    raw_payload = event.raw.get("payload") if event.raw else None
                    if isinstance(raw_payload, dict) and (
                        raw_payload.get("code") == "execution_lost"
                        or raw_payload.get("phase") == "process_exit"
                    ):
                        status = "failed"
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

            if event.type in {"error", "job_failed"} or (
                event.type == "session_interrupted" and status == "failed"
            ):
                last_error, last_error_truncated = _truncate_text(event.content)

        return SessionInformationExecutionDTO(
            job_id=latest_job_id,
            status=status,
            current_tool=current_tool,
            last_error=last_error,
            last_error_truncated=last_error_truncated,
        )

    @staticmethod
    def _build_trace(
        trace_events: list[TraceEventDTO],
        *,
        truncated: bool,
    ) -> SessionInformationTraceDTO:
        if not trace_events:
            return SessionInformationTraceDTO(truncated=truncated)
        latest = trace_events[-1]
        return SessionInformationTraceDTO(
            observed_event_count=len(trace_events),
            last_event_id=latest.event_id,
            last_event_type=latest.type,
            last_event_at=latest.timestamp,
            truncated=truncated,
        )

    @classmethod
    def _build_resource_summary(
        cls,
        resources: list[SessionResourceDTO],
    ) -> SessionInformationResourceSummaryDTO:
        active = [
            resource
            for resource in resources
            if resource.status in _ACTIVE_RESOURCE_STATUSES
        ]
        recent_closed = [
            resource
            for resource in resources
            if resource.status not in _ACTIVE_RESOURCE_STATUSES
            and resource.status != "deleted"
        ]
        return SessionInformationResourceSummaryDTO(
            active=[
                cls._to_information_resource(resource)
                for resource in active[:_ACTIVE_RESOURCE_LIMIT]
            ],
            recent_closed=[
                cls._to_information_resource(resource)
                for resource in recent_closed[:_RECENT_CLOSED_RESOURCE_LIMIT]
            ],
            active_count=len(active),
            recent_closed_count=len(recent_closed),
            active_truncated=len(active) > _ACTIVE_RESOURCE_LIMIT,
            recent_closed_truncated=len(recent_closed) > _RECENT_CLOSED_RESOURCE_LIMIT,
            historical_omitted=True,
        )

    @staticmethod
    def _to_information_resource(
        resource: SessionResourceDTO,
    ) -> SessionInformationResourceDTO:
        name, name_truncated = _truncate_text(
            resource.name,
            limit=_DIAGNOSTIC_RESOURCE_NAME_LIMIT,
        )
        return SessionInformationResourceDTO(
            resource_id=resource.resource_id,
            kind=resource.kind,
            name=name,
            status=resource.status,
            updated_at=resource.updated_at,
            ended_at=resource.ended_at,
            name_truncated=name_truncated,
        )

    @staticmethod
    def _build_recent_errors(
        trace_events: list[TraceEventDTO],
    ) -> list[SessionInformationErrorDTO]:
        errors = []
        for event in trace_events:
            if event.type not in {"error", "job_failed", "session_interrupted"}:
                continue
            message, message_truncated = _truncate_text(event.content)
            errors.append(
                SessionInformationErrorDTO(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    type=event.type,
                    message=message,
                    timestamp=event.timestamp,
                    message_truncated=message_truncated,
                )
            )
        return errors[-_RECENT_ERROR_LIMIT:]


def _truncate_text(
    value: str,
    *,
    limit: int = _DIAGNOSTIC_TEXT_LIMIT,
) -> tuple[str, bool]:
    if limit < 1:
        raise ValueError("诊断文本 limit 必须大于 0")
    if len(value) <= limit:
        return value, False
    return f"{value[:limit - 1]}…", True
