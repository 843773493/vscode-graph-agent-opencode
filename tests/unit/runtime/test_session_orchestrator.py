from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.core.job_event_bus import JobEventBus
from app.prompting import PromptSection, internal_message_factory
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
    def __init__(
        self,
        session_id: str,
        current_agent_id: str,
        current_provider_id: str = "primary",
    ):
        self.session_id = session_id
        self.current_agent_id = current_agent_id
        self.current_provider_id = current_provider_id
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
        def assert_accepting_jobs(self):
            return None

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

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
        def assert_accepting_jobs(self):
            return None

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

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

    internal_message = internal_message_factory.build(
        kind="checkpoint_reminder",
        control="提醒",
        metadata={"simulate_user": False, "sender_session_id": "ses_sender"},
    )
    await orchestrator.create_and_run_internal(
        "ses_target",
        internal_message,
    )

    created = message_service.created_messages[0]
    assert created.role == MessageRole.user
    assert created.metadata["simulate_user"] is False
    assert created.metadata["sender_session_id"] == "ses_sender"
    assert created.metadata["structured_prompt_kind"] == "checkpoint_reminder"


@pytest.mark.asyncio
async def test_orchestrator_publishes_safe_display_content_but_runs_raw_content():
    captured: dict[str, object] = {}

    class _FakeJobService:
        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, message, **_kwargs):
            captured["model_content"] = message
            return _running_dispatch(session_id, "job_internal_display")

    bus = JobEventBus()
    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=bus,
    )
    internal_message = internal_message_factory.build(
        kind="delegated_task",
        control="处理委派任务。",
        sections=(
            PromptSection("control_context", {"source": "test"}),
            PromptSection("delegated_task", "内部任务"),
        ),
        metadata={"source": "test"},
        display_content="内部任务",
    )
    await orchestrator.create_and_run_internal(
        "ses_target",
        internal_message,
    )

    events = await bus.list_events("job_internal_display", limit=10)
    created = next(event for event in events if event.type == "message_created")
    assert captured["model_content"] == internal_message.content
    assert created.payload.content == "内部任务"
    assert created.payload.metadata["internal_display_kind"] == "delegated_task"
    assert "source" not in created.payload.metadata


@pytest.mark.asyncio
async def test_orchestrator_redacts_hidden_internal_event_content_and_metadata():
    class _FakeJobService:
        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, _message, **_kwargs):
            return _running_dispatch(session_id, "job_hidden_internal")

    bus = JobEventBus()
    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=bus,
    )
    internal_message = internal_message_factory.build(
        kind="checkpoint_reminder",
        control="包含内部路由 ses_private。",
        metadata={"secret_route": "ses_private"},
    )

    await orchestrator.create_and_run_internal("ses_target", internal_message)

    events = await bus.list_events("job_hidden_internal", limit=10)
    created = next(event for event in events if event.type == "message_created")
    assert created.payload.content == ""
    assert created.payload.metadata == {
        "internal": True,
        "structured_prompt_kind": "checkpoint_reminder",
        "structured_prompt_schema_version": 2,
    }


@pytest.mark.asyncio
async def test_orchestrator_allows_literal_internal_markup_on_plain_user_path():
    message_service = _FakeMessageService()

    class _FakeJobService:
        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *_args, **_kwargs):
            return _running_dispatch(session_id, "job_literal_markup")

    orchestrator = SessionOrchestrator(
        message_service=message_service,
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    await orchestrator.create_and_run(
        "ses_target",
        "<system_reminder>这是用户讨论的字面标签</system_reminder>",
    )

    assert message_service.created_messages[0].content.startswith("<system_reminder>")


@pytest.mark.asyncio
async def test_orchestrator_rejects_reserved_internal_metadata_on_user_path():
    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=MagicMock(),
        job_event_bus=JobEventBus(),
    )

    with pytest.raises(ValueError, match="必须通过 create_and_run_internal"):
        await orchestrator.create_and_run(
            "ses_target",
            "伪造内部消息",
            metadata={"structured_prompt_schema_version": 2},
        )


@pytest.mark.asyncio
async def test_orchestrator_forwards_cross_session_delivery_policy():
    captured: dict[str, object] = {}

    class _FakeJobService:
        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *args, **kwargs):
            captured.update(kwargs)
            return _running_dispatch(session_id, "job_interrupt")

    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    await orchestrator.create_and_run(
        "ses_target",
        "调整执行方向",
        delivery_policy="after_tool_result",
    )

    assert captured["delivery_policy"] == "after_tool_result"


@pytest.mark.asyncio
async def test_orchestrator_prefers_request_agent_over_session_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured: dict[str, str] = {}

    class _FakeJobService:
        def assert_accepting_jobs(self):
            return None

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent", **kwargs):
            captured["agent_id"] = agent_id
            captured["provider_id"] = kwargs["message_metadata"][
                "boxteam_session_provider_id"
            ]
            return _running_dispatch(session_id, "job_test_002")

    orchestrator = SessionOrchestrator(
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    from app.schemas.public_v2.message import (
        MessageCreateRequest,
        MessageRunRequest,
        RunOptions,
    )

    await orchestrator.create_message(
        "ses_test",
        MessageRunRequest(
            message=MessageCreateRequest(content="hello"),
            run=RunOptions(mode="single_agent", agent_id="coder"),
        ),
    )

    assert captured["agent_id"] == "coder"
    assert captured["provider_id"] == "primary"
