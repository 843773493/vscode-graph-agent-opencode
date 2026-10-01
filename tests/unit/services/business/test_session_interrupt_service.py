from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.internal_v2.common import ControlAction, JobStatus, RunMode
from app.schemas.internal_v2.job import JobControlRequest, JobControlResponseDTO, JobDTO
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.message_service import build_message_service


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
    # R17：session ID 必须满足 canonical 形态（ses_ + UUIDv4 位 profile）；
    # 参数化 phase 的派生 ID 按任务书确定性映射函数（md5 + v4 位 profile）
    # 显式定值，保持与旧字面量同名的确定性对应。
    session_id = {
        "text": "ses_4d61915efc1d469b80f735dad37dfec2",  # ses_user_interrupt_text
        "tool": "ses_f786d2fb0aa04e768cd94003d6710fe9",  # ses_user_interrupt_tool
    }[phase]
    job_id = f"job_user_interrupt_{phase}"
    SessionInterruptState.clear(session_id)
    session_bundle_factory(tmp_path, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    message_service = build_message_service(tmp_path, checkpointer=saver)
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
            "channel_versions": {"messages": "1"},
            "updated_channels": ["messages"],
            "id": "ckpt-user-interrupt",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": "1"},
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

    projections = saver._storage.read_item_projections(session_id)
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

    with saver._storage._connect(session_id, "", read_only=True) as connection:
        metadata_json = connection.execute(
            "SELECT metadata_json FROM item_catalog WHERE item_id = ?",
            (reminder["item_id"],),
        ).fetchone()[0]
    metadata = json.loads(metadata_json)
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

    SessionInterruptState.clear(session_id)


@pytest.mark.asyncio
async def test_user_interrupt_submits_reminder_without_existing_checkpoint(
    tmp_path,
    session_bundle_factory,
) -> None:
    session_id = "ses_395c5d0c0b4b414d8a5248f16f17a677"
    job_id = "job_user_interrupt_missing_checkpoint"
    SessionInterruptState.clear(session_id)
    session_bundle_factory(tmp_path, session_id)

    saver = RolloutCheckpointSaver(sessions_dir=tmp_path)
    message_service = build_message_service(tmp_path, checkpointer=saver)
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

    result = await service.interrupt(session_id=session_id)

    assert result.job_id == job_id
    assert job_service.control_requests
    assert SessionInterruptState.get(session_id).user_interrupt_reminder_injected is True
    projections = saver._storage.read_item_projections(session_id)
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
    SessionInterruptState.clear(session_id)
