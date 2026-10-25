from __future__ import annotations

import json

import pytest

from app.abstractions.session_resources import BackgroundTaskRegistryProtocol
from app.core.background_task_contracts import BackgroundTaskHandle
from app.domain.itemized.enums import PayloadKind
from app.services.business.message_service import MessageService
from app.services.business.session_resource_providers import (
    BackgroundTaskResourceProvider,
    BrowserResourceProvider,
    TerminalResourceProvider,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_resource_service import SessionResourceService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.session_resource_mapper import SessionResourceMapper
from tests.support.background_work import build_background_registry, spawn_waiting_task
from tests.support.message_service import build_message_service


def _owner_snapshot(record: dict[str, object]) -> dict[str, object]:
    ref = {"session_id": record["session_id"], "thread_id": "thr_018f0000000070008000000000000001"}
    return {
        "retention_scope": "thread", "source_thread": ref, "thread_associations": [ref],
        "owner_revision": 1, "reconciliation_status": "verified", "reclaim_at": None,
        "error": None, **record,
    }


class FakeTerminalResourceOwner:
    def __init__(self, terminals: list[dict[str, object]] | None = None) -> None:
        self.terminals = [_owner_snapshot(record) for record in terminals or []]
        self.deleted_terminal_ids: list[str] = []
        self.killed_terminal_ids: list[str] = []
        self.listed_session_ids: list[str | None] = []
        self.cleaned_session_ids: list[str] = []

    def attach_url(self, terminal_id: str) -> str:
        return f"http://127.0.0.1:8013/?terminalId={terminal_id}"

    async def list_resources(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.listed_session_ids.append(session_id)
        return [
            terminal
            for terminal in self.terminals
            if session_id is None or any(
                ref["session_id"] == session_id for ref in terminal["thread_associations"]
            )
        ]

    async def get_resource(self, terminal_id: str) -> dict[str, object]:
        return next(record for record in self.terminals if record["terminal_id"] == terminal_id)

    async def command(
        self, resource_id: str, *, action: str, expected_revision: int,
        holder_id: str, operation_id: str,
    ) -> dict[str, object]:
        assert action == "stop"
        assert holder_id == f"request:{operation_id}"
        self.killed_terminal_ids.append(resource_id)
        return {**await self.get_resource(resource_id), "status": "terminated"}

    async def cleanup_session(self, session_id: str) -> int:
        self.cleaned_session_ids.append(session_id)
        self.deleted_terminal_ids.extend(str(record["terminal_id"]) for record in self.terminals)
        return len(self.terminals)


class FakeBrowserResourceOwner:
    def __init__(self, browsers: list[dict[str, object]] | None = None) -> None:
        self.browsers = [_owner_snapshot(browser) for browser in (browsers or [])]
        self.deleted_browser_ids: list[str] = []
        self.woken_browser_ids: list[str] = []

    async def list_resources(self, *, session_id: str) -> list[dict[str, object]]:
        return [browser for browser in self.browsers if browser["status"] != "deleted"
                and any(ref["session_id"] == session_id for ref in browser["thread_associations"])]

    async def get_resource(self, resource_id: str) -> dict[str, object]:
        return next(browser for browser in self.browsers if browser["browser_id"] == resource_id)

    async def command(self, resource_id: str, *, action: str, **arguments: object) -> dict[str, object]:
        assert action == "stop"
        return {**await self.get_resource(resource_id), "status": "closed"}

    async def perform(self, resource_id: str, *, action: str, **arguments: object) -> dict[str, object]:
        assert action == "wake"
        self.woken_browser_ids.append(resource_id)
        return {"browser": {**await self.get_resource(resource_id), "status": "running",
                "resource_state": "active", "title": "Restored page", "url": "https://example.com/restored"}}

    async def cleanup_session(self, session_id: str) -> int:
        resources = await self.list_resources(session_id=session_id)
        self.deleted_browser_ids.extend(browser["browser_id"] for browser in resources)
        return len(resources)


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


class _EmptyBackgroundTaskRegistry:
    def list_handles(self, _session_id: str) -> list[BackgroundTaskHandle]:
        return []

    def list_closed_handles(self, _session_id: str) -> list[BackgroundTaskHandle]:
        return []

    def get_handle(
        self,
        _session_id: str,
        _task_id: str,
    ) -> BackgroundTaskHandle | None:
        return None

    async def cancel(
        self,
        _session_id: str,
        task_id: str,
    ) -> BackgroundTaskHandle:
        raise AssertionError(f"空后台任务 registry 不应取消任务: {task_id}")

    async def delete(
        self,
        _session_id: str,
        task_id: str,
    ) -> BackgroundTaskHandle:
        raise AssertionError(f"空后台任务 registry 不应删除任务: {task_id}")

    async def delete_session(self, _session_id: str) -> int:
        return 0


@pytest.fixture
def resource_mapper() -> SessionResourceMapper:
    return SessionResourceMapper()


@pytest.fixture
def empty_background_task_registry() -> BackgroundTaskRegistryProtocol:
    return _EmptyBackgroundTaskRegistry()


def _provider_registry(
    *,
    task_registry: BackgroundTaskRegistryProtocol,
    terminal_owner: FakeTerminalResourceOwner,
    browser_owner: FakeBrowserResourceOwner,
    message_service: FakeMessageService | MessageService,
    resource_mapper: SessionResourceMapper,
) -> SessionResourceProviderRegistry:
    return SessionResourceProviderRegistry(
        [
            BackgroundTaskResourceProvider(
                task_registry=task_registry,
                resource_mapper=resource_mapper,
            ),
            TerminalResourceProvider(
                terminal_owner=terminal_owner,
                resource_mapper=resource_mapper,
            ),
            BrowserResourceProvider(
                browser_owner=browser_owner,
                resource_mapper=resource_mapper,
            ),
        ]
    )


def test_deleted_terminal_resource_explains_historical_status(
    resource_mapper: SessionResourceMapper,
):
    terminal = {
        "terminal_id": "term_123",
        "session_id": "ses_123",
        "status": "deleted",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "ended_at": "2026-07-05T01:02:05+00:00",
    }
    resource = resource_mapper.terminal_to_resource(
        _owner_snapshot(terminal),
        available_actions=[],
    )

    assert resource.status == "deleted"
    assert resource.available_actions == []
    assert "终端已删除" in str(resource.metadata["status_note"])


def test_running_browser_resource_maps_identity_and_actions(
    resource_mapper: SessionResourceMapper,
):
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
    resource = resource_mapper.browser_to_resource(
        _owner_snapshot(browser),
        available_actions=["cancel"],
    )

    assert resource.kind == "browser"
    assert resource.status == "running"
    assert resource.metadata["page_id"] == "browser_123"
    assert "attach_url" not in resource.metadata
    assert resource.available_actions == ["cancel"]


def test_terminal_resource_projection_excludes_real_path(
    resource_mapper: SessionResourceMapper,
):
    """负向断言（6.3）：API 投影 MUST NOT 携带真实文件系统路径。"""
    terminal = {
        "terminal_id": "term_real_path",
        "session_id": "ses_123",
        "status": "running",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "cwd": "/home/user/secret-workspace/app",
        "cwd_relative": "app",
    }
    resource = resource_mapper.terminal_to_resource(
        _owner_snapshot(terminal),
        available_actions=[],
    )

    assert resource.metadata["cwd_relative"] == "app"
    assert "cwd" not in resource.metadata
    assert "/home/user/secret-workspace" not in resource.model_dump_json()


def test_browser_resource_projection_excludes_checkpoint_real_path(
    resource_mapper: SessionResourceMapper,
):
    """负向断言（6.3）：检查点真实路径不得上浮到 API 响应体。"""
    browser = {
        "browser_id": "browser_checkpoint_path",
        "session_id": "ses_123",
        "status": "lost",
        "resource_state": "discarded",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "checkpoint": {
            "version": 1,
            "path": "/home/user/secret-workspace/.boxteam/browser-manager/checkpoints/browser_checkpoint_path.json",
        },
    }
    resource = resource_mapper.browser_to_resource(
        _owner_snapshot(browser),
        available_actions=[],
    )

    assert resource.metadata["checkpoint_available"] is True
    assert "checkpoint" not in resource.metadata
    assert "/home/user/secret-workspace" not in resource.model_dump_json()


@pytest.mark.asyncio
async def test_lost_browser_has_no_action_even_with_checkpoint(
    resource_mapper: SessionResourceMapper,
):
    browser = {
        "browser_id": "browser_cold",
        "session_id": "ses_019b94653e71714b8d406f83ab3d2b3b",
        "status": "lost",
        "resource_state": "discarded",
        "checkpoint": {"version": 1, "browser_id": "browser_cold"},
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
    }
    provider = BrowserResourceProvider(
        browser_owner=FakeBrowserResourceOwner(browsers=[browser]),
        resource_mapper=resource_mapper,
    )

    resources = await provider.list_resources("ses_019b94653e71714b8d406f83ab3d2b3b")
    resource = resources[0]

    assert resource.available_actions == []
    assert "可以重新打开" in str(resource.metadata["status_note"])


@pytest.mark.asyncio
async def test_terminal_fast_listing_skips_slow_agent_history(
    resource_mapper: SessionResourceMapper,
) -> None:
    terminal = {
        "terminal_id": "term_live",
        "session_id": "ses_019b94653e71714b8d406f83ab3d2b3b",
        "status": "running",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
    }
    terminated_terminal = {
        **terminal,
        "terminal_id": "term_closed",
        "status": "terminated",
    }
    provider = TerminalResourceProvider(
        terminal_owner=FakeTerminalResourceOwner(
            terminals=[terminal, terminated_terminal]
        ),
        resource_mapper=resource_mapper,
    )

    resources = await provider.list_resources("ses_019b94653e71714b8d406f83ab3d2b3b", include_history=False)

    assert [resource.resource_id for resource in resources] == ["term_live"]
    assert resources[0].status == "running"
    assert provider._owner.listed_session_ids == [
        "ses_019b94653e71714b8d406f83ab3d2b3b"
    ]


@pytest.mark.asyncio
async def test_terminal_control_rejects_resource_outside_session_owner_list(
    resource_mapper: SessionResourceMapper,
) -> None:
    terminal_owner = FakeTerminalResourceOwner(
        terminals=[
            {
                "terminal_id": "term_other_session",
                "session_id": "ses_other",
                "status": "running",
            }
        ]
    )
    provider = TerminalResourceProvider(
        terminal_owner=terminal_owner,
        resource_mapper=resource_mapper,
    )

    for action in ("cancel", "delete"):
        with pytest.raises(ValueError, match="不属于当前 Session"):
            await provider.control(
                session_id="ses_current",
                resource_id="term_other_session",
                action=action,
                operation_id="request-resource-test",
            )

    assert terminal_owner.listed_session_ids == []
    assert terminal_owner.killed_terminal_ids == []
    assert terminal_owner.deleted_terminal_ids == []


@pytest.mark.asyncio
async def test_browser_fast_listing_excludes_closed_state_records(
    resource_mapper: SessionResourceMapper,
) -> None:
    browser = {
        "browser_id": "browser_live",
        "session_id": "ses_019b94653e71714b8d406f83ab3d2b3b",
        "status": "running",
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
        "title": "Live",
        "url": "https://example.com",
    }
    closed_browser = {
        **browser,
        "browser_id": "browser_closed",
        "status": "closed",
    }
    provider = BrowserResourceProvider(
        browser_owner=FakeBrowserResourceOwner(
            browsers=[browser, closed_browser]
        ),
        resource_mapper=resource_mapper,
    )

    resources = await provider.list_resources("ses_019b94653e71714b8d406f83ab3d2b3b", include_history=False)

    assert [resource.resource_id for resource in resources] == ["browser_live"]


@pytest.mark.asyncio
async def test_browser_provider_resumes_cold_recycled_browser(
    resource_mapper: SessionResourceMapper,
):
    browser = {
        "browser_id": "browser_cold",
        "session_id": "ses_019b94653e71714b8d406f83ab3d2b3b",
        "status": "running",
        "resource_state": "discarded",
        "checkpoint": {"version": 1, "browser_id": "browser_cold"},
        "created_at": "2026-07-05T01:02:03+00:00",
        "updated_at": "2026-07-05T01:02:04+00:00",
    }
    browser_owner = FakeBrowserResourceOwner(browsers=[browser])
    provider = BrowserResourceProvider(
        browser_owner=browser_owner,
        resource_mapper=resource_mapper,
    )

    result = await provider.control(
        session_id="ses_019b94653e71714b8d406f83ab3d2b3b",
        resource_id="browser_cold",
        action="resume",
        operation_id="request-resource-test",
    )

    assert result.status == "running"
    assert result.resource is not None
    assert result.resource.available_actions == ["cancel"]
    assert browser_owner.woken_browser_ids == ["browser_cold"]


@pytest.mark.asyncio
async def test_browser_provider_excludes_deleted_records(
    empty_background_task_registry: BackgroundTaskRegistryProtocol,
    resource_mapper: SessionResourceMapper,
):
    session_id = "ses_019bb7ec49017d8cac52d5b674fa1b7e"
    browser_owner = FakeBrowserResourceOwner(
        browsers=[
            {
                "browser_id": "browser_deleted",
                "session_id": session_id,
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
            task_registry=empty_background_task_registry,
            terminal_owner=FakeTerminalResourceOwner(),
            browser_owner=browser_owner,
            message_service=FakeMessageService(),
            resource_mapper=resource_mapper,
        ),
    )

    result = await service.list(session_id)

    assert result.items == []


@pytest.mark.asyncio
async def test_list_includes_closed_background_task_history(
    tmp_path,
    session_bundle_factory,
    resource_mapper: SessionResourceMapper,
):
    session_id = "ses_019c7618ac207a1e89d845aa0622881f"
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    registry = build_background_registry(sessions_dir)

    handle = await spawn_waiting_task(registry, sessions_dir, session_id)
    await registry.cancel(session_id, handle.task_id)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_owner=FakeTerminalResourceOwner(),
            browser_owner=FakeBrowserResourceOwner(),
            message_service=FakeMessageService(),
            resource_mapper=resource_mapper,
        ),
    )

    result = await service.list(session_id)

    assert len(result.items) == 1
    assert result.items[0].resource_id == handle.task_id
    assert result.items[0].status == "cancelled"
    assert result.items[0].available_actions == ["delete"]


@pytest.mark.asyncio
async def test_list_excludes_deleted_background_task_history(
    tmp_path,
    session_bundle_factory,
    resource_mapper: SessionResourceMapper,
):
    session_id = "ses_019beb2ad93f78668af7471f0ac9538c"
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    registry = build_background_registry(sessions_dir)

    handle = await spawn_waiting_task(registry, sessions_dir, session_id)
    await registry.delete(session_id, handle.task_id)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_owner=FakeTerminalResourceOwner(),
            browser_owner=FakeBrowserResourceOwner(),
            message_service=FakeMessageService(),
            resource_mapper=resource_mapper,
        ),
    )

    result = await service.list(session_id)

    assert result.items == []


@pytest.mark.asyncio
async def test_cleanup_session_cleans_jobs_background_tasks_and_terminals(
    tmp_path,
    session_bundle_factory,
    resource_mapper: SessionResourceMapper,
):
    session_id = "ses_019b7fe1fe727dcf8650ad9c92cb293e"
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    registry = build_background_registry(sessions_dir)

    handle = await spawn_waiting_task(registry, sessions_dir, session_id)
    terminal_owner = FakeTerminalResourceOwner(
        terminals=[
            {
                "terminal_id": "term_cleanup",
                "session_id": session_id,
            }
        ],
    )
    browser_owner = FakeBrowserResourceOwner(
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
            terminal_owner=terminal_owner,
            browser_owner=browser_owner,
            message_service=FakeMessageService(),
            resource_mapper=resource_mapper,
        ),
    )

    assert await service.cleanup_session(session_id) is None
    assert job_service.deleted_session_id == session_id
    assert registry.list_handles(session_id) == []
    assert terminal_owner.deleted_terminal_ids == ["term_cleanup"]
    assert terminal_owner.cleaned_session_ids == [session_id]
    assert browser_owner.deleted_browser_ids == ["browser_cleanup"]
    assert handle.status == "deleted"


@pytest.mark.asyncio
async def test_cancel_background_task_does_not_inject_monitor_reminder(
    tmp_path,
    session_bundle_factory,
    resource_mapper: SessionResourceMapper,
):
    session_id = "ses_019c74e9583b7f338610345dcba9c87a"
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    saver = RolloutCheckpointSaver(sessions_dir=sessions_dir)
    owner = saver.forks.source.resolve_main_thread_owner(session_id)
    saver.accept_turn(
        session_id, thread_id=owner.thread_id, accepted_ingress_id="request-cancel-monitor",
        acceptance_idempotency_key="acceptance-cancel-monitor", job_id="job-cancel-monitor",
        admission_idempotency_key="admission-cancel-monitor", payload="请监控另一个会话的最终回复",
        payload_kind=PayloadKind.TEXT, turn_id="turn-cancel-monitor",
        root_item_id="item-cancel-monitor", initial_execution_id="execution-cancel-monitor",
    )

    registry = build_background_registry(sessions_dir)

    handle = await spawn_waiting_task(registry, sessions_dir, session_id)
    message_service = build_message_service(sessions_dir, checkpointer=saver)
    service = SessionResourceService(
        session_service=FakeSessionService(),
        job_service=FakeJobService(count=0),
        provider_registry=_provider_registry(
            task_registry=registry,
            terminal_owner=FakeTerminalResourceOwner(),
            browser_owner=FakeBrowserResourceOwner(),
            message_service=message_service,
            resource_mapper=resource_mapper,
        ),
    )

    result = await service.control(
        session_id=session_id,
        kind="background_task",
        resource_id=handle.task_id,
        action="cancel",
        operation_id="request-resource-test",
    )

    assert result.status == "cancelled"
    state = await message_service.get_agent_state_messages(session_id)
    records = [
        json.loads(line)
        for line in state.jsonl.splitlines()
        if line.strip()
    ]
    # 有界等待不产生可取消监控后台任务；普通后台任务取消不注入监控提醒。
    assert all("resource_cancel" not in str(record.get("response_metadata", {})) for record in records)
