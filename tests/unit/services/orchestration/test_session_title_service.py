from __future__ import annotations

from datetime import datetime

import pytest
from langchain_core.messages import AIMessage

from app.schemas.public_v2.session import SessionUpdateRequest
from app.services.orchestration.session_title_service import SessionTitleService


class _FakeSession:
    def __init__(self, title: str) -> None:
        self.session_id = "ses_title_test"
        self.title = title
        self.current_agent_id = "default"
        self.created_at = datetime.now()
        self.updated_at = self.created_at


class _FakeSessionService:
    def __init__(self, title: str) -> None:
        self.session = _FakeSession(title)
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
        self.session.updated_at = datetime.now()
        return self.session


class _FakeConfigService:
    def get_agent_runtime_config(self, agent_id: str):
        assert agent_id == "default"
        return {
            "providers": [{"id": "primary", "model": "title-model"}],
            "temperature": 0.2,
            "top_p": 1,
            "max_output_tokens": 4000,
        }


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


class _FakeTitleModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        assert "用户消息" in messages[1].content
        return AIMessage(content="修复登录流程。")


@pytest.mark.asyncio
async def test_auto_title_updates_default_session_title():
    model = _FakeTitleModel()
    session_service = _FakeSessionService("新会话")
    bus = _FakeBus()
    service = SessionTitleService(
        config_service=_FakeConfigService(),
        session_service=session_service,
        job_event_bus=bus,
        model_factory=lambda _provider, _runtime_config: model,
    )

    title = await service.maybe_auto_title_after_first_response(
        session_id="ses_title_test",
        job_id="job_title_test",
        agent_id="default",
        user_message="请帮我修复登录失败的问题",
        assistant_response="已经修复登录流程中的 token 校验问题。",
    )

    assert title == "修复登录流程"
    assert session_service.session.title == "修复登录流程"
    assert len(session_service.update_calls) == 1
    assert model.calls == 1
    assert any(
        event["event_type"] == "status_change"
        and event["payload"].get("reason") == "session_auto_title_updated"
        for event in bus.events
    )


@pytest.mark.asyncio
async def test_auto_title_does_not_overwrite_custom_session_title():
    model = _FakeTitleModel()
    session_service = _FakeSessionService("用户手动标题")
    service = SessionTitleService(
        config_service=_FakeConfigService(),
        session_service=session_service,
        job_event_bus=_FakeBus(),
        model_factory=lambda _provider, _runtime_config: model,
    )

    title = await service.maybe_auto_title_after_first_response(
        session_id="ses_title_test",
        job_id="job_title_test",
        agent_id="default",
        user_message="hello",
        assistant_response="world",
    )

    assert title is None
    assert session_service.session.title == "用户手动标题"
    assert session_service.update_calls == []
    assert model.calls == 0
