from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.internal_v2.common import CursorPage
from app.schemas.internal_v2.session import SessionDTO, SessionListResultDTO
from app.schemas.internal_v2.session_resource import SessionResourceListDTO
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.workspace import WorkspaceDTO
from app.services.business.session_information_service import SessionInformationService


class _Sessions:
    def __init__(self, events: list[TraceEventDTO]) -> None:
        now = datetime.now(UTC)
        self.session = SessionDTO(
            session_id="ses_information",
            workspace_id="workspace_information",
            title="Information",
            current_agent_id="default",
            created_at=now,
            updated_at=now,
        )
        self.events = events

    async def get(self, session_id: str) -> SessionDTO:
        return self.session

    async def list(self, *, limit: int) -> SessionListResultDTO:
        return SessionListResultDTO(items=[self.session], total=1)

    async def list_trace_events(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> CursorPage[TraceEventDTO]:
        assert limit == 100
        return CursorPage(items=self.events)


class _Resources:
    async def list(self, session_id: str) -> SessionResourceListDTO:
        return SessionResourceListDTO(session_id=session_id, items=[])


class _Workspace:
    async def get(self) -> WorkspaceDTO:
        return WorkspaceDTO(
            workspace_id="workspace_information",
            root_path="/workspace",
            name="workspace",
        )


def _trace_event(
    *,
    event_id: str,
    job_id: str,
    event_type: str,
    content: str,
    timestamp: datetime,
    raw: dict[str, object] | None = None,
) -> TraceEventDTO:
    return TraceEventDTO(
        event_id=event_id,
        session_id="ses_information",
        job_id=job_id,
        type=event_type,
        phase="error" if event_type in {"error", "job_failed"} else "job",
        title=event_type,
        content=content,
        timestamp=timestamp,
        raw=raw or {},
    )


@pytest.mark.asyncio
async def test_information_uses_latest_bounded_trace_page_for_execution_and_errors(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    events = [
        _trace_event(
            event_id="evt_old_error",
            job_id="job_old",
            event_type="error",
            content="old error",
            timestamp=now,
        ),
        _trace_event(
            event_id="evt_latest_failed",
            job_id="job_latest",
            event_type="job_failed",
            content="latest error",
            timestamp=now,
        ),
    ]
    service = SessionInformationService(
        session_service=_Sessions(events),  # type: ignore[arg-type]
        session_resource_service=_Resources(),  # type: ignore[arg-type]
        workspace_service=_Workspace(),  # type: ignore[arg-type]
        path_resolver=SimpleNamespace(
            resolve_session_node=lambda session_id: tmp_path / session_id
        ),
    )

    result = await service.get_information("ses_information")

    assert result.execution.job_id == "job_latest"
    assert result.execution.status == "failed"
    assert result.execution.last_error == "latest error"
    assert result.trace.event_count == 2
    assert result.trace.last_event_id == "evt_latest_failed"
    assert [error.event_id for error in result.recent_errors] == [
        "evt_old_error",
        "evt_latest_failed",
    ]


@pytest.mark.asyncio
async def test_information_marks_process_exit_as_failed(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events = [
        _trace_event(
            event_id="evt_process_exit",
            job_id="job_process_exit",
            event_type="session_interrupted",
            content="工作区后端重启，无法安全续接原 AgentLoop 执行",
            timestamp=now,
            raw={
                "payload": {
                    "phase": "process_exit",
                    "code": "execution_lost",
                }
            },
        )
    ]
    service = SessionInformationService(
        session_service=_Sessions(events),  # type: ignore[arg-type]
        session_resource_service=_Resources(),  # type: ignore[arg-type]
        workspace_service=_Workspace(),  # type: ignore[arg-type]
        path_resolver=SimpleNamespace(
            resolve_session_node=lambda session_id: tmp_path / session_id
        ),
    )

    result = await service.get_information("ses_information")

    assert result.execution.status == "failed"
    assert result.execution.last_error == (
        "工作区后端重启，无法安全续接原 AgentLoop 执行"
    )
