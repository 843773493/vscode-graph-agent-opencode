from __future__ import annotations

from datetime import datetime

import pytest

from app.core.job_event_bus import JobEventBus
from app.runtime.session_orchestrator import SessionOrchestrator
from app.schemas.public_v2.common import MessageRole
from app.schemas.public_v2.job import JobDispatchSnapshotDTO


def _running_dispatch(session_id: str, job_id: str) -> JobDispatchSnapshotDTO:
    return JobDispatchSnapshotDTO(
        session_id=session_id,
        job_id=job_id,
        job_status="running",
        active_job_id=job_id,
        queued_jobs_ahead=0,
        queued_job_count=0,
        pending_job_count=1,
    )


class _FakeSession:
    def __init__(self, session_id: str, current_agent_id: str):
        self.session_id = session_id
        self.current_agent_id = current_agent_id
        self.created_at = datetime.now()
        self.updated_at = self.created_at


class _FakeConfigService:
    def resolve_agent_id(self, agent_id):
        return agent_id

    def validate_agent_id(self, agent_id):
        return agent_id

    def get_default_agent_id(self):
        return "deep_agent"


class _FakeMessageService:
    def __init__(self) -> None:
        self.created_messages = []

    async def create(self, session_id: str, message_create):
        from app.schemas.public_v2.message import MessageDTO

        self.created_messages.append(message_create)

        return MessageDTO(
            message_id="msg_test",
            session_id=session_id,
            role=message_create.role,
            content=message_create.content,
            metadata=message_create.metadata,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )


class _FakeSessionService:
    def __init__(self, current_agent_id: str = "default"):
        self._current_agent_id = current_agent_id

    async def get(self, session_id: str):
        return _FakeSession(session_id, self._current_agent_id)


@pytest.mark.asyncio
async def test_orchestrator_uses_session_current_agent_when_request_omits_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent", **kwargs):
            captured["session_id"] = session_id
            captured["message"] = message
            captured["agent_id"] = agent_id
            return _running_dispatch(session_id, "job_test_001")

    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )
    result = await orchestrator.create_and_run("ses_test", "hello")

    assert captured["session_id"] == "ses_test"
    assert captured["message"] == "hello"
    assert captured["agent_id"] == "default"
    assert result.job_id == "job_test_001"


@pytest.mark.asyncio
async def test_orchestrator_preserves_reminder_metadata_without_changing_user_role():
    class _FakeJobService:
        async def start_job(self, session_id, *args, **kwargs):
            return _running_dispatch(session_id, "job_system_reminder")

    message_service = _FakeMessageService()
    orchestrator = SessionOrchestrator(
        message_service=message_service,
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    await orchestrator.create_and_run(
        "ses_target",
        "<system_reminder>提醒</system_reminder>",
        metadata={"simulate_user": False, "sender_session_id": "ses_sender"},
    )

    created = message_service.created_messages[0]
    assert created.role == MessageRole.user
    assert created.metadata == {
        "simulate_user": False,
        "sender_session_id": "ses_sender",
    }


@pytest.mark.asyncio
async def test_orchestrator_prefers_request_agent_over_session_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent", **kwargs):
            captured["agent_id"] = agent_id
            return _running_dispatch(session_id, "job_test_002")

    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    from app.schemas.public_v2.message import MessageCreateRequest, MessageRunRequest, RunOptions

    await orchestrator.create_message(
        "ses_test",
        MessageRunRequest(
            message=MessageCreateRequest(content="hello"),
            run=RunOptions(mode="single_agent", agent_id="coder"),
        ),
    )

    assert captured["agent_id"] == "coder"
