from __future__ import annotations

from datetime import datetime

import pytest

from app.core.job_event_bus import JobEventBus
from app.runtime.session_orchestrator import SessionOrchestrator


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
    async def create(self, session_id: str, message_create):
        from app.schemas.public_v2.message import MessageDTO
        from app.schemas.public_v2.common import MessageRole

        return MessageDTO(
            message_id="msg_test",
            session_id=session_id,
            role=MessageRole.user,
            content=message_create.content,
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
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
            captured["session_id"] = session_id
            captured["message"] = message
            captured["agent_id"] = agent_id
            return "job_test_001"

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
async def test_orchestrator_prefers_request_agent_over_session_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
            captured["agent_id"] = agent_id
            return "job_test_002"

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
