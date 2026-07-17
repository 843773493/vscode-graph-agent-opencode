from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.services.business.message_service import MessageService
from app.services.business.session_resource_actions import (
    browser_available_actions,
    terminal_available_actions,
)
from app.services.business.session_resource_providers import (
    BackgroundTaskResourceProvider,
    BrowserResourceProvider,
    TerminalResourceProvider,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_resource_service import SessionResourceService
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
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


class FakeBrowserManagerClient:
    def __init__(self, browsers: list[dict[str, object]] | None = None) -> None:
        self.browsers = browsers or []
        self.deleted_browser_ids: list[str] = []

    def attach_url(self, browser_id: str) -> str:
        return f"http://127.0.0.1:8016/?browserId={browser_id}"

    def list_browsers_from_state(self, session_id: str) -> list[dict[str, object]]:
        return [
            browser
            for browser in self.browsers
            if browser.get("session_id") == session_id
        ]

    async def close_browser(self, browser_id: str) -> dict[str, object]:
        return {
            "browser_id": browser_id,
            "session_id": "ses_test",
            "status": "closed",
            "created_at": "2026-07-05T01:02:03+00:00",
            "updated_at": "2026-07-05T01:02:04+00:00",
        }

    async def delete_browser(self, browser_id: str) -> dict[str, object]:
        self.deleted_browser_ids.append(browser_id)
        return {
            "deleted": True,
            "browser_id": browser_id,
            "browser": {
                "browser_id": browser_id,
                "session_id": "ses_test",
                "status": "deleted",
                "created_at": "2026-07-05T01:02:03+00:00",
                "updated_at": "2026-07-05T01:02:04+00:00",
            },
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


class FakeMessageService:
    async def list_agent_state_records(
        self,
        session_id: str,
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
    return SessionResourceMapper()


def _provider_registry(
    *,
    task_registry: BackgroundTaskRegistry,
    terminal_manager: FakeTerminalManagerClient,
    browser_manager: FakeBrowserManagerClient,
    message_service: FakeMessageService | MessageService,
) -> SessionResourceProviderRegistry:
    mapper = _resource_mapper()
    return SessionResourceProviderRegistry(
        [
            BackgroundTaskResourceProvider(
                task_registry=task_registry,
                message_service=message_service,
                resource_mapper=mapper,
            ),
            TerminalResourceProvider(
                terminal_manager=terminal_manager,
                historical_reader=FakeHistoricalTerminalRecordReader(),
                message_service=message_service,
                resource_mapper=mapper,
            ),
            BrowserResourceProvider(
                browser_manager=browser_manager,
                resource_mapper=mapper,
            ),
        ]
    )


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


def test_running_browser_resource_maps_identity_and_actions():
    browser = {
        "browser_id": "browser_123",
        "session_id": "ses_123",
        "status": "running",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "started_at": "2026-07-05T01:02:03+00:00",
        "title": "Example",
        "url": "https://example.com",
    }
    resource = _resource_mapper().browser_to_resource(
        browser,
        available_actions=browser_available_actions(str(browser["status"])),
    )

    assert resource.kind == "browser"
    assert resource.status == "running"
    assert resource.metadata["page_id"] == "browser_123"
    assert "attach_url" not in resource.metadata
    assert resource.available_actions == ["cancel", "delete"]


@pytest.mark.asyncio
async def test_browser_provider_excludes_deleted_records(tmp_path):
    registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )
    browser_manager = FakeBrowserManagerClient(
        browsers=[
            {
                "browser_id": "browser_deleted",
                "session_id": "ses_browser_deleted",
                "status": "deleted",
                "created_at": "2026-07-05T01:02:03+00:00",
                "updated_at": "2026-07-05T01:02:04+00:00",
            }
        ]
    )
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_manager=FakeTerminalManagerClient(),
            browser_manager=browser_manager,
            message_service=FakeMessageService(),
        ),
    )

    result = await service.list("ses_browser_deleted")

    assert result.items == []


@pytest.mark.asyncio
async def test_list_includes_closed_background_task_history(tmp_path):
    session_id = "ses_closed_history"
    registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    handle = registry.spawn(
        session_id=session_id,
        task_name="monitor_session_agent_end",
        runner=wait_forever,
    )
    await registry.cancel(session_id, handle.task_id)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_manager=FakeTerminalManagerClient(),
            browser_manager=FakeBrowserManagerClient(),
            message_service=FakeMessageService(),
        ),
    )

    result = await service.list(session_id)

    assert len(result.items) == 1
    assert result.items[0].resource_id == handle.task_id
    assert result.items[0].status == "cancelled"
    assert result.items[0].available_actions == ["delete"]


@pytest.mark.asyncio
async def test_list_excludes_deleted_background_task_history(tmp_path):
    session_id = "ses_deleted_history"
    registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    handle = registry.spawn(
        session_id=session_id,
        task_name="monitor_session_agent_end",
        runner=wait_forever,
    )
    await registry.delete(session_id, handle.task_id)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_manager=FakeTerminalManagerClient(),
            browser_manager=FakeBrowserManagerClient(),
            message_service=FakeMessageService(),
        ),
    )

    result = await service.list(session_id)

    assert result.items == []


@pytest.mark.asyncio
async def test_cleanup_session_cleans_jobs_background_tasks_and_terminals(tmp_path):
    session_id = "ses_cleanup"
    registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )

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
    browser_client = FakeBrowserManagerClient(
        browsers=[
            {
                "browser_id": "browser_cleanup",
                "session_id": session_id,
                "status": "running",
                "created_at": "2026-07-05T01:02:03+00:00",
                "updated_at": "2026-07-05T01:02:04+00:00",
            }
        ],
    )
    job_service = FakeJobService(count=2)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=job_service,
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_manager=terminal_client,
            browser_manager=browser_client,
            message_service=FakeMessageService(),
        ),
    )

    result = await service.cleanup_session(session_id)

    assert result.cleaned_execution_runs == 2
    assert result.cleaned_background_tasks == 1
    assert result.cleaned_terminals == 1
    assert result.cleaned_browsers == 1
    assert job_service.deleted_session_id == session_id
    assert registry.list_handles(session_id) == []
    assert terminal_client.deleted_terminal_ids == ["term_cleanup"]
    assert browser_client.deleted_browser_ids == ["browser_cleanup"]
    assert handle.status == "deleted"


@pytest.mark.asyncio
async def test_cancel_monitor_background_task_injects_system_reminder(tmp_path):
    session_id = "ses_cancel_monitor"
    saver = FileSystemCheckpointSaver(sessions_dir=tmp_path)
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

    registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=tmp_path / ".boxteam")
    )

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
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_manager=FakeTerminalManagerClient(),
            browser_manager=FakeBrowserManagerClient(),
            message_service=message_service,
        ),
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
