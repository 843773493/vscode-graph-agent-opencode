from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.internal_v2.common import ControlAction, JobStatus, RunMode
from app.schemas.internal_v2.job import JobControlRequest, JobControlResponseDTO, JobDTO
from app.services.business.message_service import MessageService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.infrastructure.message_stream_store import MessageStreamStore


class FakeJobService:
    def __init__(self, job: JobDTO) -> None:
        self.job = job
        self.control_requests: list[JobControlRequest] = []
        self.boundary_notifications: list[tuple[str, bool]] = []

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        if session_id is None or self.job.session_id == session_id:
            return [self.job]
        return []

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
            status=JobStatus.cancelling,
            control_state="cancelling",
        )

    async def notify_boundary(
        self,
        session_id: str,
        boundary: str,
        *,
        tool_result_available: bool,
    ) -> None:
        assert session_id == self.job.session_id
        assert boundary == "after_interrupt"
        self.boundary_notifications.append((boundary, tool_result_available))


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
    phase: str,
    tool_name: str | None,
    current_text: str,
    expected_assistant_text: str | None,
) -> None:
    session_id = f"ses_user_interrupt_{phase}"
    job_id = f"job_user_interrupt_{phase}"
    SessionInterruptState.clear(session_id)
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    message_service = MessageService(checkpointer=saver)
    message_stream_store = MessageStreamStore(
        path_resolver=get_session_path_resolver(tmp_path),
    )
    config = build_checkpoint_config(session_id)
    await saver.aput(
        config,
        {
            "channel_values": {
                "messages": [
                    HumanMessage(content="请执行一个可以被取消的任务"),
                ]
            },
            "channel_versions": {"messages": 1},
            "updated_channels": ["messages"],
            "id": "ckpt-user-interrupt",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    SessionInterruptState.set(
        session_id,
        phase=phase,
        tool_name=tool_name,
        current_text=current_text,
    )
    job = JobDTO(
        job_id=job_id,
        message_id="msg_interrupt",
        session_id=session_id,
        mode=RunMode.single_agent,
        status=JobStatus.streaming,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(job)
    event_bus = FakeJobEventBus()
    service = SessionInterruptService(
        job_service=job_service,
        job_event_bus=event_bus,
        message_service=message_service,
        message_stream_store=message_stream_store,
    )

    result = await service.interrupt(session_id=session_id)

    assert result.job_id == job_id
    assert result.phase == phase
    assert result.tool_name == tool_name
    assert job_service.control_requests
    assert job_service.control_requests[0].action == ControlAction.cancel
    assert job_service.boundary_notifications == [("after_interrupt", False)]
    assert SessionInterruptState.get(session_id).user_interrupt_reminder_injected is True
    assert event_bus.events
    assert event_bus.events[-1]["event_type"] == "session_interrupted"

    state = await message_service.get_agent_state_messages(session_id)
    records = [
        json.loads(line)
        for line in state.jsonl.splitlines()
        if line.strip()
    ]
    reminder = records[-1]
    assert reminder["role"] == "user"
    assert reminder["type"] == "human"
    assert "<system_reminder>" in reminder["content"]
    assert "主动取消" in reminder["content"]
    assert reminder["response_metadata"]["source"] == "user_interrupt"
    assert reminder["response_metadata"]["user_initiated"] is True
    assert reminder["response_metadata"]["phase"] == phase

    assistant_records = [record for record in records if record["role"] == "assistant"]
    if expected_assistant_text is None:
        assert assistant_records == []
        assert tool_name is not None
        assert tool_name in reminder["content"]
    else:
        assert assistant_records[-1]["content"] == expected_assistant_text
        assert "<system_reminder>" not in assistant_records[-1]["content"]

    SessionInterruptState.clear(session_id)


@pytest.mark.asyncio
async def test_user_interrupt_fails_when_system_reminder_checkpoint_missing(
    tmp_path,
    session_bundle_factory,
) -> None:
    session_id = "ses_user_interrupt_missing_checkpoint"
    job_id = "job_user_interrupt_missing_checkpoint"
    SessionInterruptState.clear(session_id)
    session_bundle_factory(tmp_path, session_id)

    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    message_service = MessageService(checkpointer=saver)
    message_stream_store = MessageStreamStore(
        path_resolver=get_session_path_resolver(tmp_path),
    )
    SessionInterruptState.set(
        session_id,
        phase="text",
        tool_name=None,
        current_text="已经生成但尚未完成的文本",
    )
    job = JobDTO(
        job_id=job_id,
        message_id="msg_missing_checkpoint",
        session_id=session_id,
        mode=RunMode.single_agent,
        status=JobStatus.streaming,
        entry_agent="default",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job_service = FakeJobService(job)
    service = SessionInterruptService(
        job_service=job_service,
        job_event_bus=FakeJobEventBus(),
        message_service=message_service,
        message_stream_store=message_stream_store,
    )

    with pytest.raises(RuntimeError, match="system_reminder"):
        await service.interrupt(session_id=session_id)

    assert job_service.control_requests == []
    assert SessionInterruptState.get(session_id).user_interrupt_reminder_injected is False
    SessionInterruptState.clear(session_id)
