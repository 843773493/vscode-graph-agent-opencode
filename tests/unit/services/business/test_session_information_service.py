from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.internal_v2.common import CursorPage
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_resource import (
    SessionResourceDTO,
    SessionResourceListDTO,
)
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.workspace import WorkspaceDTO
from app.services.business.session_information_service import SessionInformationService


class _Sessions:
    def __init__(
        self,
        events: list[TraceEventDTO],
        *,
        trace_has_more: bool = False,
        child_summary: tuple[int, list[str], bool] = (0, [], False),
        title: str = "Information",
        workspace_id: str = "workspace_information",
    ) -> None:
        now = datetime.now(UTC)
        self.session = SessionDTO(
            session_id="ses_information",
            workspace_id=workspace_id,
            title=title,
            current_agent_id="default",
            created_at=now,
            updated_at=now,
        )
        self.events = events
        self.trace_has_more = trace_has_more
        self.child_summary = child_summary

    async def get(self, session_id: str) -> SessionDTO:
        return self.session

    async def child_session_summary(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[str], bool]:
        assert session_id == self.session.session_id
        assert limit == 32
        return self.child_summary

    async def list_trace_events(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> CursorPage[TraceEventDTO]:
        assert limit == 100
        return CursorPage(items=self.events, has_more=self.trace_has_more)


class _Resources:
    def __init__(self, items: list[SessionResourceDTO] | None = None) -> None:
        self.items = items or []
        self.include_history: bool | None = None

    async def list(
        self,
        session_id: str,
        *,
        include_history: bool = True,
    ) -> SessionResourceListDTO:
        self.include_history = include_history
        return SessionResourceListDTO(session_id=session_id, items=self.items)


class _Workspace:
    async def get(self) -> WorkspaceDTO:
        return WorkspaceDTO(
            workspace_id="workspace_information",
            root_path="/workspace",
            name="workspace",
        )


@pytest.mark.asyncio
async def test_information_rejects_mismatched_session_and_backend_workspace_ids(
    tmp_path: Path,
) -> None:
    resource_service = _Resources()
    service = SessionInformationService(
        session_service=_Sessions(
            [],
            workspace_id="workspace_from_persisted_session",
        ),  # type: ignore[arg-type]
        session_resource_service=resource_service,  # type: ignore[arg-type]
        workspace_service=_Workspace(),  # type: ignore[arg-type]
        path_resolver=SimpleNamespace(
            resolve_session_node=lambda session_id: tmp_path / session_id
        ),
    )

    with pytest.raises(RuntimeError, match="会话与工作区后端标识不一致"):
        await service.get_information("ses_information")


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
    resource_service = _Resources()
    service = SessionInformationService(
        session_service=_Sessions(events),  # type: ignore[arg-type]
        session_resource_service=resource_service,  # type: ignore[arg-type]
        workspace_service=_Workspace(),  # type: ignore[arg-type]
        path_resolver=SimpleNamespace(
            resolve_session_node=lambda session_id: tmp_path / session_id
        ),
    )

    result = await service.get_information("ses_information")

    assert resource_service.include_history is False
    assert result.execution.job_id == "job_latest"
    assert result.execution.status == "failed"
    assert result.execution.last_error == "latest error"
    assert result.trace.observed_event_count == 2
    assert result.trace.truncated is False
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
    resource_service = _Resources()
    service = SessionInformationService(
        session_service=_Sessions(events),  # type: ignore[arg-type]
        session_resource_service=resource_service,  # type: ignore[arg-type]
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


@pytest.mark.asyncio
async def test_information_is_bounded_and_omits_agent_state_history(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    events = [
        _trace_event(
            event_id=f"evt_{index}",
            job_id="job_large",
            event_type="error",
            content="错误信息" * 2_000,
            timestamp=now,
        )
        for index in range(7)
    ]
    resources = [
        SessionResourceDTO(
            resource_id=f"term_{index}",
            session_id="ses_information",
            kind="terminal",
            name=("资源名称" * 300) if index == 0 else f"terminal {index}",
            status=(
                "running"
                if index < 40
                else "deleted"
                if index == 49
                else "terminated"
            ),
            created_at=now,
            updated_at=now,
            ended_at=now if index >= 40 else None,
        )
        for index in range(50)
    ]
    resource_service = _Resources(resources)
    service = SessionInformationService(
        session_service=_Sessions(
            events,
            trace_has_more=True,
            child_summary=(40, [f"ses_child_{index}" for index in range(32)], True),
            title="会话标题" * 300,
        ),  # type: ignore[arg-type]
        session_resource_service=resource_service,  # type: ignore[arg-type]
        workspace_service=_Workspace(),  # type: ignore[arg-type]
        path_resolver=SimpleNamespace(
            resolve_session_node=lambda session_id: tmp_path / session_id
        ),
    )

    result = await service.get_information("ses_information")

    assert result.relations.child_count == 40
    assert len(result.relations.child_ids) == 32
    assert result.relations.child_ids_truncated is True
    assert result.session.title_truncated is True
    assert len(result.session.title) == 512
    assert result.resources.active_count == 40
    assert len(result.resources.active) == 32
    assert result.resources.active_truncated is True
    assert result.resources.recent_closed_count == 9
    assert len(result.resources.recent_closed) == 9
    assert result.resources.active[0].name.endswith("…")
    assert len(result.resources.active[0].name) == 512
    assert result.resources.active[0].name_truncated is True
    assert result.resources.historical_omitted is True
    assert result.trace.truncated is True
    assert len(result.recent_errors) == 5
    assert result.recent_errors[-1].message_truncated is True
    assert len(result.recent_errors[-1].message) == 2_048
    assert result.execution.last_error_truncated is True


def test_information_keeps_non_closed_browser_resource_states_active() -> None:
    now = datetime.now(UTC)
    resources = [
        SessionResourceDTO(
            resource_id=f"browser_{status}",
            session_id="ses_information",
            kind="browser",
            name=status,
            status=status,
            created_at=now,
            updated_at=now,
        )
        for status in ("frozen", "discarded")
    ]

    summary = SessionInformationService._build_resource_summary(resources)

    assert [resource.status for resource in summary.active] == [
        "frozen",
        "discarded",
    ]
    assert summary.recent_closed == []
