from __future__ import annotations

from datetime import datetime

import pytest

from app.schemas.message import MessageCreate, MessageRunRequest, RunOptions
from app.services.job_service import JobService
from app.services.message_service import MessageService
from app.services.session_service import SessionService


class _FakeSession:
    def __init__(self, session_id: str, current_agent_id: str):
        self.session_id = session_id
        self.current_agent_id = current_agent_id
        self.created_at = datetime.now()
        self.updated_at = self.created_at


@pytest.mark.asyncio
async def test_create_and_run_uses_session_current_agent_when_request_omits_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
            captured["session_id"] = session_id
            captured["message"] = message
            captured["agent_id"] = agent_id
            return "job_test_001"

    async def _fake_get(_session_id: str):
        return _FakeSession(_session_id, "default")

    monkeypatch.setattr(SessionService, "get", _fake_get)
    monkeypatch.setattr(JobService, "get_instance", lambda: _FakeJobService())

    service = MessageService()
    result = await service.create_and_run(
        "ses_test",
        MessageRunRequest(
            message=MessageCreate(content="hello"),
            run=RunOptions(mode="single_agent"),
        ),
    )

    assert captured["session_id"] == "ses_test"
    assert captured["message"] == "hello"
    assert captured["agent_id"] == "default"
    assert result.job_id == "job_test_001"


@pytest.mark.asyncio
async def test_create_and_run_prefers_request_agent_over_session_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
            captured["agent_id"] = agent_id
            return "job_test_002"

    async def _fake_get(_session_id: str):
        return _FakeSession(_session_id, "default")

    monkeypatch.setattr(SessionService, "get", _fake_get)
    monkeypatch.setattr(JobService, "get_instance", lambda: _FakeJobService())

    service = MessageService()
    await service.create_and_run(
        "ses_test",
        MessageRunRequest(
            message=MessageCreate(content="hello"),
            run=RunOptions(mode="single_agent", agent_id="coder"),
        ),
    )

    assert captured["agent_id"] == "coder"
