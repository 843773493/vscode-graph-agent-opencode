from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

from app.core.job_event_bus import JobEventBus
from app.core.session_lifecycle_gate import OperationHolderGate
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.prompting import PromptSection, internal_message_factory
from app.runtime.session_orchestrator import SessionOrchestrator
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.job import JobDispatchSnapshotDTO
from app.schemas.internal_v2.message import MessageDTO
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutOperationLock,
)


@pytest.fixture
def attachment_holder(tmp_path):
    return OperationHolderGate(tmp_path / "sessions", namespace="attachment_ingest")


@pytest.fixture
def prepared_child_dispatch_case(attachment_holder):
    messages = _FakeMessageService()
    jobs = MagicMock()
    jobs.started = []

    async def run_preparation(_session_id, operation):
        return await operation()

    async def start_job(session_id, _content, **kwargs):
        jobs.started.append((session_id, kwargs))
        return _running_dispatch(session_id, "job_prepared_child").model_copy(
            update={"thread_id": kwargs["thread_id"]}
        )

    jobs.run_session_preparation = run_preparation
    jobs.start_job = start_job
    bus = JobEventBus()
    sessions = _FakeSessionService("default", thread_id="thr_catalog_main")
    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
        message_service=messages,
        session_service=sessions,
        config_service=_FakeConfigService(),
        job_service=jobs,
        job_event_bus=bus,
    )
    return orchestrator, messages, jobs, bus, sessions


@pytest.mark.asyncio
async def test_existing_child_message_keeps_persisted_owner_through_acceptance_job_and_event(
    prepared_child_dispatch_case,
):
    orchestrator, messages, jobs, bus, sessions = prepared_child_dispatch_case
    prepared = await orchestrator.prepare_user_message(
        "ses_prepared_child", "只发送给 child", thread_id="thr_prepared_child"
    )
    result = await orchestrator.dispatch_existing_message(
        prepared.session_id, prepared,
        execution_binding=_user_identity("job_prepared_child"),
    )
    assert messages.accepted_messages[0][0] is prepared
    assert messages.accepted_messages[0][0].thread_id == "thr_prepared_child"
    assert jobs.started[0][1]["thread_id"] == "thr_prepared_child"
    assert result.thread_id == "thr_prepared_child"
    event = (await bus.list_events(result.job_id, limit=10))[0]
    assert event.payload.thread_id == "thr_prepared_child"
    sessions.path_resolver.resolve_thread_node.assert_called_with(
        "ses_prepared_child", "thr_prepared_child"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_message_acceptance_waits_for_rollout_without_blocking_event_loop(
    prepared_child_dispatch_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
):
    orchestrator, messages, jobs, _bus, _sessions = prepared_child_dispatch_case
    prepared = await orchestrator.prepare_user_message(
        "ses_prepared_child", "并发发送给 child", thread_id="thr_prepared_child"
    )
    lock_path = tmp_path / "rollout.lock"
    entered = Event()
    original_accept = messages.accept_user_message

    def accept(message, *, execution_identity, queue_input):
        entered.set()
        with _RolloutOperationLock(lock_path):
            original_accept(message, execution_identity=execution_identity, queue_input=queue_input)

    monkeypatch.setattr(messages, "accept_user_message", accept)
    dispatch = None
    try:
        async with _RolloutOperationLock(lock_path):
            dispatch = asyncio.create_task(orchestrator.dispatch_existing_message(
                prepared.session_id, prepared,
                execution_binding=_user_identity("job_prepared_child"),
            ))
            assert await asyncio.to_thread(entered.wait, 3)
            if cancel:
                dispatch.cancel()
                await asyncio.sleep(0)
            assert not dispatch.done(), "等锁或取消均不能提前释放消息准备窗口"
            assert messages.accepted_messages == []
            assert jobs.started == []
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(dispatch, timeout=3)
            assert jobs.started == []
        else:
            result = await asyncio.wait_for(dispatch, timeout=3)
            assert result.thread_id == "thr_prepared_child"
            assert len(jobs.started) == 1
        assert messages.accepted_messages == [(prepared, _user_identity("job_prepared_child"))]
    finally:
        if dispatch is not None and not dispatch.done():
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)


@pytest.mark.parametrize("conflict", ["session", "thread"])
@pytest.mark.asyncio
async def test_prepared_message_owner_conflict_rejects_before_acceptance_job_or_event(
    prepared_child_dispatch_case, conflict,
):
    orchestrator, messages, jobs, bus, _sessions = prepared_child_dispatch_case
    now = datetime.now(UTC)
    prepared = MessageDTO(
        message_id="msg_conflict", session_id="ses_persisted",
        thread_id="thr_persisted_child", role=MessageRole.user,
        content="必须拒绝", metadata={}, created_at=now, updated_at=now,
    )
    with pytest.raises(ValueError, match="与持久 owner 不一致"):
        await orchestrator.dispatch_prepared_message(
            "ses_foreign" if conflict == "session" else prepared.session_id,
            prepared, requested_agent_id="default",
            execution_binding=_user_identity("job_prepared_child"),
            thread_id="thr_foreign" if conflict == "thread" else None,
        )
    assert messages.created_messages == []
    assert messages.accepted_messages == []
    assert jobs.started == []
    assert await bus.list_events("job_prepared_child", limit=10) == []


def _running_dispatch(session_id: str, job_id: str) -> JobDispatchSnapshotDTO:
    return JobDispatchSnapshotDTO(
        session_id=session_id,
        thread_id=f"thr_{session_id}",
        job_id=job_id,
        job_status="running",
        active_job_id=job_id,
        queued_jobs_ahead=0,
        queued_job_count=0,
        pending_job_count=1,
    )


_UNSET = object()


def _user_identity(job_id: str) -> UserExecutionIdentity:
    return UserExecutionIdentity(job_id=job_id, turn_id=f"turn_{job_id}", admission_idempotency_key=f"adm_{job_id}")


class _FakeSession:
    def __init__(
        self,
        session_id: str,
        current_agent_id: str,
        current_provider_id: str = "primary",
        thread_id: object = _UNSET,
    ):
        self.session_id = session_id
        self.current_agent_id = current_agent_id
        self.current_provider_id = current_provider_id
        # SessionDTO 的权威 main thread 身份。生产实现取自 catalog 冻结的
        # main_thread_id，绝不用 session_id 冒充；替身默认给可区分的 thr_ 前缀，
        # 使「误用 session_id」的变异能被 thread_id 断言杀掉。
        self.thread_id = f"thr_{session_id}" if thread_id is _UNSET else thread_id
        self.created_at = datetime.now(UTC)
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
        self.accepted_messages = []

    def accept_user_message(self, message, *, execution_identity, queue_input):
        assert queue_input.execution_binding == execution_identity
        assert queue_input.message_id == message.message_id
        self.accepted_messages.append((message, execution_identity))

    async def create(self, session_id: str, message_create, *, thread_id: str):
        from app.schemas.internal_v2.message import MessageDTO

        self.created_messages.append(message_create)

        return MessageDTO(
            message_id="msg_test",
            session_id=session_id,
            # 该可见消息所属的权威 thread；生产由服务层按 catalog 冻结 main
            # pointer 回填，替身保持一致，绝不用 session_id 冒充。
            thread_id=thread_id,
            role=message_create.role,
            content=message_create.content,
            metadata=message_create.metadata,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


class _FakeSessionService:
    def __init__(self, current_agent_id: str = "default", thread_id: object = _UNSET):
        self._current_agent_id = current_agent_id
        self._thread_id = thread_id
        self.path_resolver = MagicMock()

    async def resolve_main_thread(self, session_id: str) -> str:
        result = f"thr_{session_id}" if self._thread_id is _UNSET else self._thread_id
        if not isinstance(result, str) or not result:
            raise RuntimeError("缺少权威 main thread 身份")
        return result

    async def get(self, session_id: str):
        return _FakeSession(session_id, self._current_agent_id, thread_id=self._thread_id)


@pytest.mark.asyncio
async def test_orchestrator_uses_session_current_agent_when_request_omits_agent(attachment_holder):

    captured: dict[str, str] = {}

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_test_001")

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
        attachment_holder=attachment_holder,
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
    assert result.turn_id == "turn_job_test_001"
    assert result.turn_id != result.job_id
    # 普通聊天入口的 Turn 归属必须来自 SessionDTO 的权威 main thread，
    # 不得由 session_id 冒充。
    assert result.thread_id == "thr_ses_test"
    assert result.thread_id != "ses_test"


@pytest.mark.asyncio
async def test_orchestrator_preserves_reminder_metadata_without_changing_user_role(attachment_holder):
    captured: dict[str, object] = {}

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_system_reminder")

        def assert_accepting_jobs(self):
            return None

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *args, **kwargs):
            captured.update(kwargs)
            return _running_dispatch(session_id, "job_system_reminder")

    message_service = _FakeMessageService()
    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
    execution_binding = captured["execution_binding"]
    assert isinstance(execution_binding, InternalExecutionIntent)
    assert execution_binding.message_id == "msg_test"
    assert (
        len(
            {
                execution_binding.message_id,
                execution_binding.execution_id,
                execution_binding.admission_idempotency_key,
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_orchestrator_publishes_safe_display_content_but_runs_raw_content(attachment_holder):
    captured: dict[str, object] = {}

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_internal_display")

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, message, **_kwargs):
            captured["model_content"] = message
            return _running_dispatch(session_id, "job_internal_display")

    bus = JobEventBus()
    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
    assert created.payload.thread_id == "thr_ses_target"
    assert captured["model_content"] == internal_message.content
    assert created.payload.content == "内部任务"
    assert created.payload.metadata["internal_display_kind"] == "delegated_task"
    assert "source" not in created.payload.metadata


@pytest.mark.asyncio
async def test_orchestrator_redacts_hidden_internal_event_content_and_metadata(attachment_holder):
    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_hidden_internal")

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, _message, **_kwargs):
            return _running_dispatch(session_id, "job_hidden_internal")

    bus = JobEventBus()
    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
    assert created.payload.thread_id == "thr_ses_target"
    assert created.payload.content == ""
    assert created.payload.metadata == {
        "internal": True,
        "structured_prompt_kind": "checkpoint_reminder",
        "structured_prompt_schema_version": 2,
    }


@pytest.mark.asyncio
async def test_orchestrator_allows_literal_internal_markup_on_plain_user_path(attachment_holder):
    message_service = _FakeMessageService()

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_literal_markup")

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *_args, **_kwargs):
            return _running_dispatch(session_id, "job_literal_markup")

    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
async def test_orchestrator_rejects_reserved_internal_metadata_on_user_path(attachment_holder):
    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
async def test_orchestrator_forwards_cross_session_delivery_policy(attachment_holder):
    captured: dict[str, object] = {}

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_interrupt")

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *args, **kwargs):
            captured.update(kwargs)
            return _running_dispatch(session_id, "job_interrupt")

    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
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
async def test_orchestrator_prefers_request_agent_over_session_agent(attachment_holder):

    captured: dict[str, str] = {}

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_test_002")

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
        attachment_holder=attachment_holder,
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default"),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    from app.schemas.internal_v2.message import (
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


@pytest.mark.asyncio
async def test_orchestrator_fails_closed_when_session_lacks_main_thread(attachment_holder):
    """SessionDTO 缺权威 main thread 身份时必须显式失败，不得用 session_id 冒充。"""

    class _FakeJobService:
        def reserve_user_execution_identity(self):
            return _user_identity("job_should_not_start")

        async def run_session_preparation(self, _session_id, operation):
            return await operation()

        async def start_job(self, session_id, *_args, **_kwargs):
            return _running_dispatch(session_id, "job_should_not_start")

    orchestrator = SessionOrchestrator(
        attachment_holder=attachment_holder,
        message_service=_FakeMessageService(),
        session_service=_FakeSessionService("default", thread_id=None),
        config_service=_FakeConfigService(),
        job_service=_FakeJobService(),
        job_event_bus=JobEventBus(),
    )

    with pytest.raises(RuntimeError, match="缺少权威 main thread 身份"):
        await orchestrator.create_and_run("ses_target", "hello")
