from __future__ import annotations

import json
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage

from app.agents.tool_invocation_context import ThreadRuntimeBinding
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_interrupt_state import SessionInterruptState
from app.core.session_thread_context import session_thread_owner_scope
from app.core.turn_execution_scope import AgentControlInbox, TurnExecutionScopeRegistry
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.common import ControlAction, JobStatus, RunMode
from app.schemas.internal_v2.job import JobControlRequest, JobControlResponseDTO, JobDTO
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.message_service import build_message_service

pytestmark = pytest.mark.usefixtures("empty_workspace_scope")


@pytest.fixture
def interrupt_sessions(tmp_path):
    resolver = get_session_path_resolver(tmp_path)

    async def resolve_main(session_id):
        return resolver.main_thread_id(session_id)

    async def resolve_thread(session_id, thread_id):
        resolver.resolve_thread_node(session_id, thread_id)
        return thread_id

    return SimpleNamespace(
        session_read_admission=lambda _session_id: nullcontext(),
        resolve_main_thread=AsyncMock(side_effect=resolve_main),
        resolve_thread=AsyncMock(side_effect=resolve_thread),
    )


class FakeJobService:
    def __init__(
        self,
        job: JobDTO,
        execution_binding: InternalExecutionIntent | UserExecutionIdentity | None = None,
        control_status: JobStatus = JobStatus.cancelling,
    ) -> None:
        self.job = job
        self.control_status = control_status
        self.execution_binding = execution_binding or UserExecutionIdentity(
            job_id=job.job_id,
            turn_id=f"turn:{job.job_id}",
            admission_idempotency_key=f"admission:{job.job_id}",
        )
        self.control_requests: list[JobControlRequest] = []
        self.boundary_notifications: list[tuple[str, bool]] = []

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        if session_id is None or self.job.session_id == session_id:
            return [self.job]
        return []

    def get_execution_binding(
        self,
        job_id: str,
    ) -> InternalExecutionIntent | UserExecutionIdentity:
        if job_id != self.job.job_id:
            raise KeyError(job_id)
        return self.execution_binding

    async def control(
        self,
        job_id: str,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO:
        if job_id != self.job.job_id:
            raise AssertionError(f"意外的 job_id: {job_id}")
        self.control_requests.append(control_request)
        return JobControlResponseDTO(
            job_id=job_id,
            session_id=self.job.session_id,
            thread_id=self.job.thread_id,
            status=self.control_status,
            control_state="cancelling",
        )

    async def notify_boundary(
        self,
        session_id: str,
        boundary: str,
        *,
        thread_id: str,
        tool_result_available: bool,
    ) -> None:
        assert session_id == self.job.session_id
        assert thread_id == self.job.thread_id
        assert boundary == "after_interrupt"
        self.boundary_notifications.append((boundary, tool_result_available))


@pytest.mark.parametrize("registration", ["none", "scope", "inbox"])
async def test_reminder_failure_still_cancels_frozen_job_and_registered_scope(registration):
    session_id = "ses_019b782883b57e5b8a8f9d1b7645266d"
    thread_id = "thr_019bef80066f7038ae8e88ccb2e5e2eb"
    binding = UserExecutionIdentity(job_id="job_cancel", turn_id="turn_cancel", admission_idempotency_key="admission_cancel")
    job = JobDTO(
        job_id=binding.job_id, message_id="msg_cancel", session_id=session_id, thread_id=thread_id,
        mode=RunMode.single_agent, status=JobStatus.running, entry_agent="default",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    jobs = FakeJobService(job, execution_binding=binding)
    failure = RuntimeError("reminder 写入失败")
    messages = SimpleNamespace(
        execution_admission_for_job=Mock(return_value=SimpleNamespace(
            session_id=session_id, thread_id=thread_id, job_id=binding.job_id,
            admission_idempotency_key=binding.admission_idempotency_key,
            execution_id="execution_cancel", turn_id=binding.turn_id, execution_origin="user_turn",
        )),
        submit_system_reminder=Mock(side_effect=failure),
    )
    writer = SimpleNamespace(turn_stream_id="stream_cancel", commit=AsyncMock(return_value={"type": "interrupt.requested"}))
    streams = SimpleNamespace(open=AsyncMock(return_value=writer))
    sessions = SimpleNamespace(
        session_read_admission=lambda _session: nullcontext(),
        resolve_main_thread=AsyncMock(return_value=thread_id),
    )
    registry = TurnExecutionScopeRegistry()
    scope = registry.create(writer.turn_stream_id) if registration != "none" else None
    inbox = AgentControlInbox(writer.turn_stream_id)
    if registration == "inbox":
        registry.register_inbox(writer.turn_stream_id, inbox)
    service = SessionInterruptService(
        job_service=jobs, job_event_bus=FakeJobEventBus(), message_service=messages,
        message_stream_store=streams, session_service=sessions, execution_scope_registry=registry,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            await service.interrupt(session_id)
        assert caught.value is failure
        assert [request.action for request in jobs.control_requests] == [ControlAction.cancel]
        state = SessionInterruptState.get(session_id, thread_id=thread_id)
        assert state.interrupt_request_id and state.cancellation_reason == "user_requested"
        assert not state.user_interrupt_reminder_injected
        assert writer.commit.await_count == 1
        if scope is not None:
            assert scope.cancellation_signal.reason == "user_requested"
        if registration == "inbox":
            assert inbox.get(state.interrupt_request_id).status == "consumed"
    finally:
        if scope is not None:
            await scope.close()
        SessionInterruptState.clear(session_id, thread_id=thread_id)


def _thread_binding(
    tmp_path,
    session_id: str,
) -> ThreadRuntimeBinding:
    return ThreadRuntimeBinding(
        session_id=session_id,
        thread_id=get_session_path_resolver(tmp_path).main_thread_id(session_id),
    )


def _accept_user_execution(
    saver: RolloutCheckpointSaver,
    binding: ThreadRuntimeBinding,
    *,
    job_id: str,
    message_id: str,
    content: str,
) -> tuple[UserExecutionIdentity, ExecutionAdmission, HumanMessage]:
    turn_id = f"turn:{job_id}"
    execution_id = f"execution:{job_id}"
    admission_key = f"admission:{job_id}"
    saver.accept_turn(
        binding.session_id,
        thread_id=binding.thread_id,
        accepted_ingress_id=message_id,
        acceptance_idempotency_key=f"acceptance:{job_id}",
        job_id=job_id,
        admission_idempotency_key=admission_key,
        payload=content,
        turn_id=turn_id,
        root_item_id=f"item-{message_id}",
        initial_execution_id=execution_id,
    )
    admission = saver.execution_admission(
        binding.session_id,
        thread_id=binding.thread_id,
        execution_id=execution_id,
    )
    identity = UserExecutionIdentity(
        job_id=job_id,
        turn_id=turn_id,
        admission_idempotency_key=admission_key,
    )
    return identity, admission, HumanMessage(content=content, id=message_id)


def _admit_internal_execution(
    saver: RolloutCheckpointSaver,
    binding: ThreadRuntimeBinding,
    *,
    job_id: str,
    message_id: str,
) -> tuple[InternalExecutionIntent, ExecutionAdmission, HumanMessage]:
    intent = InternalExecutionIntent(
        message_id=message_id,
        execution_id=f"execution:{job_id}",
        admission_idempotency_key=f"admission:{job_id}",
    )
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control="继续处理父会话工作。",
        sections=(
            PromptSection("control_context", {"source": "session_interrupt_test"}),
            PromptSection("generated_session_result", "内部任务待处理。"),
        ),
        display_content="内部任务待处理。",
    )
    message = HumanMessage(
        content=prepared.content,
        id=message_id,
        response_metadata={
            "message_id": message_id,
            "created_at": "2026-10-04T00:00:00+00:00",
            "message_metadata": prepared.metadata,
        },
    )
    admission = saver.admit_internal_execution(
        binding.session_id,
        thread_id=binding.thread_id,
        job_id=job_id,
        intent=intent,
        message=message,
    )
    return intent, admission, message


async def _persist_checkpoint(
    saver: RolloutCheckpointSaver,
    binding: ThreadRuntimeBinding,
    admission: ExecutionAdmission,
    message: HumanMessage,
) -> None:
    with (
        session_thread_owner_scope(
            MutationIntentOwner(
                session_id=binding.session_id,
                thread_id=binding.thread_id,
            ),
            langgraph_thread_id=binding.thread_id,
        ),
        execution_admission_scope(
            admission,
            langgraph_thread_id=binding.thread_id,
        ),
    ):
        await saver.aput(
            build_checkpoint_config(binding.thread_id),
            {
                "channel_values": {"messages": [message]},
                "channel_versions": {"messages": "1"},
                "updated_channels": ["messages"],
                "id": f"checkpoint:{admission.execution_id}",
            },
            {"source": "test", "step": 1, "writes": {}},
            {"messages": "1"},
        )


class FakeJobEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, object],
        step_id: str | None = None,
        agent_id: str | None = None,
    ) -> object:
        event = {
            "job_id": job_id,
            "event_type": event_type,
            "payload": payload,
            "step_id": step_id,
            "agent_id": agent_id,
        }
        self.events.append(event)
        return SimpleNamespace(**event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "tool_name", "current_text", "expected_assistant_text"),
    [
        ("text", None, "已经生成的部分回复", "已经生成的部分回复"),
        ("tool", "python_exec", "", None),
    ],
)
async def test_user_interrupt_injects_system_reminder_before_task_cancel(
    tmp_path,
    session_bundle_factory,
    interrupt_sessions,
    phase: str,
    tool_name: str | None,
    current_text: str,
    expected_assistant_text: str | None,
) -> None:
    # R17：session ID 必须满足 canonical 形态（ses_ + UUIDv4 位 profile）；
    # 参数化 phase 的派生 ID 按任务书确定性映射函数（md5 + v4 位 profile）
    # 显式定值，保持与旧字面量同名的确定性对应。
    session_id = {
        "text": "ses_019b782883b57e5b8a8f9d1b7645266d",  # ses_user_interrupt_text
        "tool": "ses_019c531022297723866bc035db07731d",  # ses_user_interrupt_tool
    }[phase]
    job_id = f"job_user_interrupt_{phase}"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    binding = _thread_binding(tmp_path, session_id)
    execution_binding, admission, user_message = _accept_user_execution(
        saver,
        binding,
        job_id=job_id,
        message_id=f"msg_user_interrupt_{phase}",
        content="请执行一个可以被取消的任务",
    )
    message_service = build_message_service(tmp_path, checkpointer=saver)
    message_stream_store = MessageStreamStore(
        path_resolver=get_session_path_resolver(tmp_path),
    )
    await _persist_checkpoint(saver, binding, admission, user_message)

    SessionInterruptState.set(
        session_id, thread_id=binding.thread_id,
        phase=phase,
        tool_name=tool_name,
        current_text=current_text,
    )
    job = JobDTO(
        job_id=job_id,
        message_id=f"msg_user_interrupt_{phase}",
        session_id=session_id,
        thread_id=binding.thread_id,
        mode=RunMode.single_agent,
        status=JobStatus.streaming,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(job, execution_binding=execution_binding)
    event_bus = FakeJobEventBus()
    service = SessionInterruptService(
        session_service=interrupt_sessions,
        job_service=job_service,
        job_event_bus=event_bus,
        message_service=message_service,
        message_stream_store=message_stream_store,
    )

    result = await service.interrupt(session_id=session_id)

    assert result.thread_id == binding.thread_id
    assert result.job_id == job_id
    assert result.phase == phase
    assert result.tool_name == tool_name
    assert job_service.control_requests
    assert job_service.control_requests[0].action == ControlAction.cancel
    assert job_service.boundary_notifications == [("after_interrupt", False)]
    assert SessionInterruptState.get(session_id, thread_id=binding.thread_id).user_interrupt_reminder_injected is True
    assert event_bus.events
    assert event_bus.events[-1]["event_type"] == "session_interrupted"

    projections = saver._storage.storage.read_item_projections(session_id, binding.thread_id)
    reminders = [
        projection
        for projection in projections
        if projection["semantic_kind"] == "runtime_notice"
    ]
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder["turn_id"] is None
    assert reminder["turn_scope"] == "pending_next_turn"
    assert reminder["wire_role"] == "user"
    reminder_content = str(reminder["content"])
    assert "<system_reminder>" in reminder_content
    assert "主动取消" in reminder_content
    if expected_assistant_text is None:
        assert tool_name is not None
        assert tool_name in reminder_content
    else:
        assert "文本生成" in reminder_content

    with saver._storage.storage._connect(binding.thread_id, "", session_id=session_id, read_only=True) as connection:
        metadata_json = connection.execute(
            "SELECT projection_manifest_json FROM item_catalog WHERE item_id = ?",
            (reminder["item_id"],),
        ).fetchone()[0]
    metadata = json.loads(metadata_json)["extensions"]["boxteam.checkpoint.annotations"]["value"]
    assert metadata["source"] == "user_interrupt"
    assert metadata["user_initiated"] is True
    assert metadata["phase"] == phase
    assert metadata["tool_name"] == tool_name
    assert metadata["checkpoint_event_id"] == result.interrupt_request_id

    # 半成品 assistant 正文由 stream/canonical producer 收敛，reminder owner
    # 不得把当前文本再次伪造为 assistant item。
    assistant_contents = [
        projection["content"]
        for projection in projections
        if projection["semantic_kind"] == "assistant_output"
    ]
    assert current_text not in assistant_contents

    SessionInterruptState.clear(session_id, thread_id=binding.thread_id)


@pytest.mark.asyncio
async def test_user_interrupt_submits_reminder_without_existing_checkpoint(
    tmp_path,
    session_bundle_factory,
    interrupt_sessions,
) -> None:
    session_id = "ses_019c75960c0c763d844344c4deaada6e"
    job_id = "job_user_interrupt_missing_checkpoint"
    session_bundle_factory(tmp_path, session_id)

    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    binding = _thread_binding(tmp_path, session_id)
    execution_binding, _admission, _user_message = _accept_user_execution(
        saver,
        binding,
        job_id=job_id,
        message_id="msg_missing_checkpoint",
        content="没有已有 checkpoint 的用户请求",
    )
    message_service = build_message_service(tmp_path, checkpointer=saver)
    message_stream_store = MessageStreamStore(
        path_resolver=get_session_path_resolver(tmp_path),
    )
    SessionInterruptState.set(
        session_id, thread_id=binding.thread_id,
        phase="text",
        tool_name=None,
        current_text="已经生成但尚未完成的文本",
    )
    job = JobDTO(
        job_id=job_id,
        message_id="msg_missing_checkpoint",
        session_id=session_id,
        thread_id=binding.thread_id,
        mode=RunMode.single_agent,
        status=JobStatus.streaming,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(job, execution_binding=execution_binding)
    service = SessionInterruptService(
        session_service=interrupt_sessions,
        job_service=job_service,
        job_event_bus=FakeJobEventBus(),
        message_service=message_service,
        message_stream_store=message_stream_store,
    )

    result = await service.interrupt(session_id=session_id)

    assert result.thread_id == binding.thread_id
    assert result.job_id == job_id
    assert job_service.control_requests
    assert SessionInterruptState.get(session_id, thread_id=binding.thread_id).user_interrupt_reminder_injected is True
    projections = saver._storage.storage.read_item_projections(session_id, binding.thread_id)
    reminders = [
        projection
        for projection in projections
        if projection["semantic_kind"] == "runtime_notice"
    ]
    assert len(reminders) == 1
    assert reminders[0]["turn_id"] is None
    assert reminders[0]["turn_scope"] == "pending_next_turn"
    assert reminders[0]["wire_role"] == "user"
    assert "<system_reminder>" in str(reminders[0]["content"])
    assert "文本生成" in str(reminders[0]["content"])
    SessionInterruptState.clear(session_id, thread_id=binding.thread_id)


@pytest.mark.asyncio
async def test_interrupt_ignores_interrupt_pending_job(
    tmp_path,
    session_bundle_factory,
    interrupt_sessions,
) -> None:
    """interrupt_pending 的 Job 不属于可中断集合，不得被再次选为中断目标。

    interrupt_pending 表示取消注入已在进行中；再次把它选为目标会重复注入取消。
    该用例锁定可中断集合等于 {running, streaming, waiting_input}。
    """

    session_id = "ses_019d2e4f6a8b7c1d9e2f3a4b5c6d7e8f"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    binding = _thread_binding(tmp_path, session_id)
    job = JobDTO(
        job_id="job_interrupt_pending",
        message_id="msg_interrupt_pending",
        session_id=session_id,
        thread_id=binding.thread_id,
        mode=RunMode.single_agent,
        status=JobStatus.interrupt_pending,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(job)
    service = SessionInterruptService(
        session_service=interrupt_sessions,
        job_service=job_service,
        job_event_bus=FakeJobEventBus(),
        message_service=build_message_service(tmp_path, checkpointer=saver),
        message_stream_store=MessageStreamStore(
            path_resolver=get_session_path_resolver(tmp_path),
        ),
    )

    with pytest.raises(ValueError, match="当前没有正在运行的任务"):
        await service.interrupt(session_id=session_id)

    assert job_service.control_requests == []

@pytest.fixture
async def internal_interrupt_case(tmp_path, session_bundle_factory, interrupt_sessions):
    session_id = "ses_019c75960c0c763d844344c4deaada6e"
    job_id = "job_internal_interrupt"
    message_id = "msg_internal_interrupt"
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    binding = _thread_binding(tmp_path, session_id)
    execution_binding, admission, internal_message = _admit_internal_execution(
        saver,
        binding,
        job_id=job_id,
        message_id=message_id,
    )
    message_service = build_message_service(tmp_path, checkpointer=saver)
    message_stream_store = MessageStreamStore(
        path_resolver=get_session_path_resolver(tmp_path),
    )
    message_stream_store.open = AsyncMock(
        side_effect=AssertionError("Turn-less internal execution opened a stream")
    )
    await _persist_checkpoint(saver, binding, admission, internal_message)
    job = JobDTO(
        job_id=job_id,
        message_id=message_id,
        session_id=session_id,
        thread_id=binding.thread_id,
        mode=RunMode.single_agent,
        status=JobStatus.streaming,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(
        job,
        execution_binding=execution_binding,
        control_status=JobStatus.cancelling,
    )
    event_bus = FakeJobEventBus()
    service = SessionInterruptService(
        session_service=interrupt_sessions,
        job_service=job_service,
        job_event_bus=event_bus,
        message_service=message_service,
        message_stream_store=message_stream_store,
    )

    return SimpleNamespace(
        saver=saver, binding=binding, admission=admission, messages=message_service,
        streams=message_stream_store, jobs=job_service, events=event_bus, service=service,
        session_id=session_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("control_status", [JobStatus.cancelling, JobStatus.cancelled])
@pytest.mark.parametrize("terminal_first", [None, "completed_empty", "failed", "cancelled", "execution_lost"])
async def test_internal_job_interrupt_never_opens_a_turn_stream(
    internal_interrupt_case, control_status, terminal_first,
) -> None:
    case = internal_interrupt_case
    saver, binding, admission = case.saver, case.binding, case.admission
    job_service, event_bus, service = case.jobs, case.events, case.service
    message_stream_store = case.streams
    session_id, job_id = case.session_id, admission.job_id
    job_service.control_status = control_status
    if terminal_first is not None:
        saver.converge_execution(
            session_id, thread_id=binding.thread_id, turn_id=None,
            execution_id=admission.execution_id, outcome=terminal_first, turn_status=None,
        )
    before = saver._storage.storage.read_items(session_id, binding.thread_id)
    result = await service.interrupt(session_id=session_id)

    assert result.thread_id == binding.thread_id
    assert result.job_id == job_id
    if terminal_first is not None:
        assert result.status == {"completed_empty": "completed", "execution_lost": "failed"}.get(terminal_first, terminal_first)
        assert not job_service.control_requests
        assert not event_bus.events
        assert saver.internal_execution_interrupt(admission) is None
        assert saver._storage.storage.read_items(session_id, binding.thread_id) == before
        assert SessionInterruptState.get(session_id, thread_id=binding.thread_id).is_default()
        return
    assert result.status == control_status.value
    message_stream_store.open.assert_not_awaited()
    assert [request.action for request in job_service.control_requests] == [
        ControlAction.cancel
    ]
    accepted = saver.internal_execution_interrupt(admission)
    assert accepted is not None and accepted.interrupt_request_id == result.interrupt_request_id
    items = saver._storage.storage.read_items(session_id, binding.thread_id)
    assert len(items) == len(before) + 1
    assert items[-1].item_id == accepted.reminder_item_id
    from app.domain.itemized.errors import InternalExecutionInterruptedError

    with pytest.raises(InternalExecutionInterruptedError):
        saver.converge_execution(
            session_id, thread_id=binding.thread_id, turn_id=None,
            execution_id=admission.execution_id, outcome="completed_empty", turn_status=None,
        )
    repeated = case.messages.request_internal_execution_interrupt(
        admission, interrupt_request_id="different-command", reminder="新的请求不能再追加提醒。", response_metadata={},
    )
    assert repeated == accepted
    assert saver._storage.storage.read_items(session_id, binding.thread_id) == items
    assert SessionInterruptState.get(session_id, thread_id=binding.thread_id).interrupt_request_id
    assert event_bus.events[-1]["event_type"] == "session_interrupted"
    SessionInterruptState.clear(session_id, thread_id=binding.thread_id)


@pytest.mark.asyncio
async def test_internal_interrupt_source_and_event_rollback_together(internal_interrupt_case, monkeypatch):
    case = internal_interrupt_case
    storage = case.saver._storage.storage
    before = storage.read_items(case.session_id, case.binding.thread_id)
    jsonl = storage.jsonl_path(case.session_id, thread_id=case.binding.thread_id)
    original_bytes = jsonl.read_bytes()

    def fail_event(*_args, **_kwargs):
        raise OSError("接受事件写入失败")

    monkeypatch.setattr(case.saver._storage.executions, "_append_internal_interrupt", fail_event)
    with pytest.raises(OSError, match="接受事件写入失败"):
        await case.service.interrupt(case.session_id)
    assert case.saver.internal_execution_interrupt(case.admission) is None
    assert storage.read_items(case.session_id, case.binding.thread_id) == before
    assert jsonl.read_bytes() == original_bytes
    assert not case.jobs.control_requests and not case.events.events
    assert SessionInterruptState.get(case.session_id, thread_id=case.binding.thread_id).is_default()
    assert await case.saver.recover_internal_interrupts() == 0
    from app.core.session_control_primitives import CONTROL_DATABASE_NAME
    from app.core.session_control_store import SessionControlStore

    with closing(SessionControlStore(storage._path_resolver.resolve_session_node(case.session_id) / CONTROL_DATABASE_NAME)) as control:
        assert not control.list_non_terminal_leases()


@pytest.mark.asyncio
async def test_internal_accepted_interrupt_cold_recovery_preserves_unconfirmed_stop(
    internal_interrupt_case, monkeypatch,
):
    case = internal_interrupt_case

    def fail_lease_settlement(_control, _event):
        raise OSError("接受已提交但 lease 结算前进程退出")

    monkeypatch.setattr(case.saver, "_settle_internal_interrupt_lease", fail_lease_settlement)
    with pytest.raises(OSError, match="lease 结算前"):
        await case.service.interrupt(case.session_id)
    assert [request.action for request in case.jobs.control_requests] == [ControlAction.cancel]
    accepted = case.saver.internal_execution_interrupt(case.admission)
    assert accepted is not None
    storage = case.saver._storage.storage
    original_items = storage.read_items(case.session_id, case.binding.thread_id)
    with storage.read_connection(case.session_id, case.binding.thread_id, "") as connection:
        original_events = connection.execute("SELECT * FROM control_events").fetchall()
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        assert await cold.recover_internal_interrupts() == 1
        assert await cold.recover_internal_interrupts() == 0
        assert cold.internal_execution_interrupt(case.admission) == accepted
        assert cold.runtime_execution_outcome(case.session_id, case.binding.thread_id, case.admission.execution_id) == "execution_lost"
        assert cold._storage.storage.read_items(case.session_id, case.binding.thread_id) == original_items
        with cold._storage.storage.read_connection(case.session_id, case.binding.thread_id, "") as connection:
            assert connection.execute("SELECT * FROM control_events").fetchall() == original_events
            metadata = json.loads(connection.execute(
                "SELECT metadata_json FROM storage_commits WHERE commit_kind='terminal_convergence' AND subject_id=?",
                (case.admission.execution_id,),
            ).fetchone()[0])
            assert metadata == {"after_interrupt_requested": True, "interrupt_request_id": accepted.interrupt_request_id}
    SessionInterruptState.clear(case.session_id, thread_id=case.binding.thread_id)


@pytest.mark.asyncio
async def test_fork_inherited_internal_interrupt_is_historical_not_target_control(
    internal_interrupt_case, session_bundle_factory, fork_deadlines,
):
    from tests.integration.backend.sessions.itemized_projection_helpers import (
        copy_rollout_snapshot,
    )

    case = internal_interrupt_case
    await case.service.interrupt(case.session_id)
    target_id = "ses_019c49da91667f7b842224c1bfc58b01"
    storage = case.saver._storage.storage
    session_bundle_factory(storage.sessions_dir, target_id)
    target_thread = storage.main_thread_id(target_id)
    await copy_rollout_snapshot(case.saver, case.session_id, target_id, fork_deadlines=fork_deadlines)
    assert case.saver._storage.executions.local_internal_interrupts(target_id, target_thread) == ()
    with storage.read_connection(target_id, target_thread, "") as connection:
        before_executions = connection.execute("SELECT * FROM executions").fetchall()
        before_events = connection.execute("SELECT * FROM control_events").fetchall()
        assert any(row[2] == "interrupt.requested" for row in before_events)
    with RolloutCheckpointSaver(storage.sessions_dir) as cold:
        assert await cold.recover_internal_interrupts() == 1
        with cold._storage.storage.read_connection(target_id, target_thread, "") as connection:
            assert connection.execute("SELECT * FROM executions").fetchall() == before_executions
            assert connection.execute("SELECT * FROM control_events").fetchall() == before_events
    SessionInterruptState.clear(case.session_id, thread_id=case.binding.thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_published", [False, True])
@pytest.mark.parametrize("failure_kind", ["execution_error", "execution_lost"])
async def test_internal_failure_after_accepted_interrupt_keeps_real_outcome(
    internal_interrupt_case, memory_published, failure_kind,
):
    from app.core.turn_execution_scope import ScopeCancelledError
    from app.services.orchestration.execution_step.failures import handle_step_failure

    case = internal_interrupt_case
    await case.service.interrupt(case.session_id)
    accepted = case.saver.internal_execution_interrupt(case.admission)
    assert accepted is not None
    error = (RuntimeError("中断接受后发生真实执行异常") if failure_kind == "execution_error"
             else ScopeCancelledError("scope_deadline_exceeded"))
    runtime, writer, publish = AsyncMock(), AsyncMock(), AsyncMock()
    await handle_step_failure(
        error=error,
        interrupt_state=SimpleNamespace(
            interrupt_request_id=accepted.interrupt_request_id if memory_published else None,
            cancellation_reason="user_requested" if memory_published else None,
            tool_name=None,
        ),
        message_stream_runtime=runtime, message_stream_writer=writer,
        checkpointer=case.saver, session_id=case.session_id,
        thread_id=case.binding.thread_id, job_id=case.admission.job_id,
        turn_id=None, execution_id=case.admission.execution_id, checkpoint_ns="",
        publish=publish, logger=Mock(),
    )
    expected_outcome = "failed" if failure_kind == "execution_error" else "execution_lost"
    assert case.saver.runtime_execution_outcome(
        case.session_id, case.binding.thread_id, case.admission.execution_id,
    ) == expected_outcome
    runtime.fail_model.assert_awaited_once()
    runtime.complete_model.assert_not_awaited()
    writer.close_failed.assert_awaited_once()
    assert writer.close_failed.await_args.kwargs["after_interrupt_requested"] is True
    writer.close_interrupted.assert_not_awaited()
    with case.saver._storage.storage.read_connection(case.session_id, case.binding.thread_id, "") as connection:
        metadata = json.loads(connection.execute(
            "SELECT metadata_json FROM storage_commits WHERE commit_kind='terminal_convergence' AND subject_id=?",
            (case.admission.execution_id,),
        ).fetchone()[0])
        assert metadata == {"after_interrupt_requested": True, "interrupt_request_id": accepted.interrupt_request_id}
    with RolloutCheckpointSaver(case.saver._storage.storage.sessions_dir) as cold:
        assert await cold.recover_internal_interrupts() == 0
        assert cold.runtime_execution_outcome(
            case.session_id, case.binding.thread_id, case.admission.execution_id,
        ) == expected_outcome
    SessionInterruptState.clear(case.session_id, thread_id=case.binding.thread_id)
