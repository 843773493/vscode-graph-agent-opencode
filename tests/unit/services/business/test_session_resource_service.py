from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from langchain_core.messages import HumanMessage

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.schemas.public_v2.common import JobStatus, RunMode
from app.schemas.public_v2.job import JobDTO
from app.services.business.message_service import MessageService
from app.services.business.session_resource_actions import (
    job_available_actions,
    job_progress_note,
    terminal_available_actions,
)
from app.services.business.session_resource_service import SessionResourceService
from app.services.mapping.session_resource_mapper import SessionResourceMapper


class FakeTerminalManagerClient:
    def __init__(self, terminals: list[dict[str, object]] | None = None) -> None:
        self.terminals = terminals or []
        self.deleted_terminal_ids: list[str] = []

    def attach_url(self, terminal_id: str) -> str:
        return f"http://127.0.0.1:8013/?terminalId={terminal_id}"

    def list_terminals_from_state(self, session_id: str) -> list[dict[str, object]]:
        return [
            terminal
            for terminal in self.terminals
            if terminal.get("session_id") == session_id
        ]

    async def delete_terminal(self, terminal_id: str) -> dict[str, object]:
        self.deleted_terminal_ids.append(terminal_id)
        return {"deleted": True, "terminal_id": terminal_id}

    async def kill_terminal(self, terminal_id: str) -> dict[str, object]:
        return {
            "terminal": {
                "terminal_id": terminal_id,
                "session_id": "ses_test",
                "status": "cancelled",
            }
        }


class FakeHistoricalTerminalRecordReader:
    def read_records(
        self,
        *,
        session_id: str,
        active_terminals: list[dict[str, object]],
        agent_state_records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        return []


class FakeSessionService:
    async def get(self, session_id: str) -> object:
        return {"session_id": session_id}


class FakeJobService:
    def __init__(self, count: int) -> None:
        self.count = count
        self.deleted_session_id: str | None = None

    async def delete_session_jobs(self, session_id: str) -> int:
        self.deleted_session_id = session_id
        return self.count


def _resource_mapper() -> SessionResourceMapper:
    return SessionResourceMapper(
        terminal_attach_url=lambda terminal_id: f"http://127.0.0.1:8013/?terminalId={terminal_id}",
    )


def _service() -> SessionResourceService:
    return SessionResourceService(
        session_service=None,
        job_service=None,
        background_task_registry=None,
        terminal_manager_client=FakeTerminalManagerClient(),
        historical_terminal_reader=FakeHistoricalTerminalRecordReader(),
        message_service=None,
        resource_mapper=_resource_mapper(),
    )


def test_job_resource_explains_running_zero_progress():
    job = JobDTO(
        job_id="job_123",
        session_id="ses_123",
        mode=RunMode.single_agent,
        status=JobStatus.running,
        entry_agent="default",
        progress=0,
        created_at=datetime(2026, 7, 5, 1, 2, 3),
        updated_at=datetime(2026, 7, 5, 1, 2, 4),
    )

    resource = _resource_mapper().job_to_resource(
        job,
        available_actions=job_available_actions(job.status),
        progress_note=job_progress_note(job.status, job.progress),
    )

    assert resource.metadata["progress"] == 0
    assert "当前阶段未提供细分进度" in str(resource.metadata["progress_note"])


def test_deleted_terminal_resource_explains_historical_status():
    terminal = {
        "terminal_id": "term_123",
        "session_id": "ses_123",
        "status": "deleted",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "ended_at": "2026-07-05T01:02:05+00:00",
    }
    resource = _resource_mapper().terminal_to_resource(
        terminal,
        available_actions=terminal_available_actions(str(terminal["status"])),
    )

    assert resource.status == "deleted"
    assert resource.available_actions == []
    assert "终端已删除" in str(resource.metadata["status_note"])


@pytest.mark.asyncio
async def test_cleanup_session_cleans_jobs_background_tasks_and_terminals():
    session_id = "ses_cleanup"
    registry = BackgroundTaskRegistry()

    async def long_running_task() -> None:
        await asyncio.sleep(60)

    handle = registry.spawn(
        session_id=session_id,
        task_name="monitor_session_agent_end",
        runner=long_running_task,
    )
    terminal_client = FakeTerminalManagerClient(
        terminals=[
            {
                "terminal_id": "term_cleanup",
                "session_id": session_id,
            }
        ],
    )
    job_service = FakeJobService(count=2)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=job_service,
        background_task_registry=registry,
        terminal_manager_client=terminal_client,
        historical_terminal_reader=FakeHistoricalTerminalRecordReader(),
        message_service=None,
        resource_mapper=_resource_mapper(),
    )

    result = await service.cleanup_session(session_id)

    assert result.cleaned_jobs == 2
    assert result.cleaned_background_tasks == 1
    assert result.cleaned_terminals == 1
    assert job_service.deleted_session_id == session_id
    assert registry.list_handles(session_id) == []
    assert terminal_client.deleted_terminal_ids == ["term_cleanup"]
    assert handle.status == "deleted"


@pytest.mark.asyncio
async def test_cancel_monitor_background_task_injects_system_reminder(tmp_path):
    session_id = "ses_cancel_monitor"
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    await saver.aput(
        config,
        {
            "channel_values": {
                "messages": [
                    HumanMessage(content="请监控另一个会话的最终回复"),
                ]
            },
            "channel_versions": {"messages": 1},
            "updated_channels": ["messages"],
            "id": "ckpt-cancel-monitor",
        },
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    registry = BackgroundTaskRegistry()

    async def long_running_task() -> None:
        await asyncio.sleep(60)

    handle = registry.spawn(
        session_id=session_id,
        task_name="monitor_session_agent_end",
        runner=long_running_task,
        metadata={
            "target_session_id": "ses_target",
            "source_id": "monitor:ses_target:test",
        },
    )
    message_service = MessageService(checkpointer=saver)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        background_task_registry=registry,
        terminal_manager_client=FakeTerminalManagerClient(),
        historical_terminal_reader=FakeHistoricalTerminalRecordReader(),
        message_service=message_service,
        resource_mapper=_resource_mapper(),
    )

    result = await service.control(
        session_id=session_id,
        kind="background_task",
        resource_id=handle.task_id,
        action="cancel",
    )

    assert result.status == "cancelled"
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
    assert "monitor_session_agent_end" in reminder["content"]
    assert handle.task_id in reminder["content"]
    assert "ses_target" in reminder["content"]
    assert reminder["response_metadata"]["source"] == "resource_cancel"
    assert reminder["response_metadata"]["task_id"] == handle.task_id
