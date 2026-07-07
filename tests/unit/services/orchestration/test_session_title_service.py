from __future__ import annotations

from datetime import datetime

import pytest

from app.schemas.public_v2.session import SessionUpdateRequest
from app.services.orchestration.session_title_service import SessionTitleService


class _FakeSession:
    def __init__(self, title: str, title_source: str = "default") -> None:
        self.session_id = "ses_title_test"
        self.title = title
        self.title_source = title_source
        self.current_agent_id = "default"
        self.created_at = datetime.now()
        self.updated_at = self.created_at


class _FakeSessionService:
    def __init__(self, title: str, title_source: str = "default") -> None:
        self.session = _FakeSession(title, title_source)
        self.update_calls: list[SessionUpdateRequest] = []

    async def get(self, session_id: str):
        assert session_id == self.session.session_id
        return self.session

    async def update(self, session_id: str, session: SessionUpdateRequest):
        assert session_id == self.session.session_id
        self.update_calls.append(session)
        if session.title is None:
            raise AssertionError("自动命名必须写入 title")
        self.session.title = session.title
        if session.title_source is not None:
            self.session.title_source = session.title_source
        self.session.updated_at = datetime.now()
        return self.session


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, object],
        step_id: str | None = None,
        agent_id: str | None = None,
    ):
        self.events.append(
            {
                "job_id": job_id,
                "event_type": event_type,
                "payload": payload,
                "agent_id": agent_id,
            }
        )


@pytest.mark.asyncio
async def test_auto_title_updates_default_session_title_from_first_message():
    session_service = _FakeSessionService("新会话")
    bus = _FakeBus()
    service = SessionTitleService(
        session_service=session_service,
        job_event_bus=bus,
    )

    title = await service.maybe_auto_title_before_first_message(
        session_id="ses_title_test",
        job_id="job_title_test",
        user_message="one two three four five six seven eight nine ten",
    )

    assert title == "one two three four five six seven eight"
    assert session_service.session.title == "one two three four five six seven eight"
    assert session_service.session.title_source == "auto"
    assert len(session_service.update_calls) == 1
    assert any(
        event["event_type"] == "status_change"
        and event["payload"].get("reason") == "session_auto_title_updated"
        for event in bus.events
    )
    assert not any(event["event_type"] == "llm_request" for event in bus.events)


@pytest.mark.asyncio
async def test_auto_title_supports_chinese_first_message():
    session_service = _FakeSessionService("未命名")
    service = SessionTitleService(
        session_service=session_service,
        job_event_bus=_FakeBus(),
    )

    title = await service.maybe_auto_title_before_first_message(
        session_id="ses_title_test",
        job_id="job_title_test",
        user_message="请只回复：自动命名测试完成",
    )

    assert title == "请只回复：自动命名"
    assert session_service.session.title == "请只回复：自动命名"


@pytest.mark.asyncio
async def test_auto_title_does_not_overwrite_custom_session_title():
    session_service = _FakeSessionService("用户手动标题", "user")
    service = SessionTitleService(
        session_service=session_service,
        job_event_bus=_FakeBus(),
    )

    title = await service.maybe_auto_title_before_first_message(
        session_id="ses_title_test",
        job_id="job_title_test",
        user_message="hello",
    )

    assert title is None
    assert session_service.session.title == "用户手动标题"
    assert session_service.update_calls == []


@pytest.mark.asyncio
async def test_auto_title_does_not_overwrite_user_title_that_matches_default_text():
    session_service = _FakeSessionService("新会话", "user")
    service = SessionTitleService(
        session_service=session_service,
        job_event_bus=_FakeBus(),
    )

    title = await service.maybe_auto_title_before_first_message(
        session_id="ses_title_test",
        job_id="job_title_test",
        user_message="hello",
    )

    assert title is None
    assert session_service.session.title == "新会话"
    assert session_service.update_calls == []
