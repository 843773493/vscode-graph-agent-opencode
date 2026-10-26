"""用真实 AnyIO CancelScope 验证 Server 原 task 的生命周期与故障发布。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import anyio
import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogError, McpCatalogOwner
from tests.support.mcp.session_doubles import FakeMcpSessionFactory


@pytest.fixture
def lifecycle_factory():
    class SessionFactory(FakeMcpSessionFactory):
        def __init__(self):
            super().__init__({"alpha": ["echo"], "beta": ["echo"]})
            self.entered: dict[str, asyncio.Task[object]] = {}
            self.closed: dict[str, asyncio.Task[object]] = {}
            self.connecting = asyncio.Event()
            self.block_connect = False
            self.fail_connect = False
            self.fail_close = False
            self.crash = asyncio.Event()

        def __call__(self, server, on_change):
            original = super().__call__(server, on_change)

            async def crash_when_requested():
                await self.crash.wait()
                raise RuntimeError("private SDK reader failure")

            @asynccontextmanager
            async def session():
                current = asyncio.current_task()
                assert current is not None
                self.entered[server.server_id] = current
                try:
                    async with anyio.create_task_group() as group:
                        if self.block_connect:
                            self.connecting.set()
                            await asyncio.Event().wait()
                        if self.fail_connect and server.server_id == "beta":
                            raise RuntimeError("private initialize failure")
                        async with original as port:
                            group.start_soon(crash_when_requested)
                            yield port
                            group.cancel_scope.cancel()
                        if self.fail_close:
                            raise RuntimeError("private close failure")
                finally:
                    current = asyncio.current_task()
                    assert current is not None
                    self.closed[server.server_id] = current

            return session()

    return SessionFactory()


@pytest.fixture
def lifecycle_owner(tmp_path, event_channel_service, lifecycle_factory):
    return McpCatalogOwner(
        leases=ExternalResourceLeaseLedger(),
        raw_config={"servers": {name: {"enabled": True, "transport": "stdio", "command": "fixture"}
                               for name in ("alpha", "beta")}},
        workspace_root=tmp_path, event_service=event_channel_service, session_factory=lifecycle_factory,
    )


async def test_each_server_keeps_its_own_context_across_start_refresh_and_close_tasks(
    lifecycle_owner, lifecycle_factory,
):
    owner, factory = lifecycle_owner, lifecycle_factory
    await asyncio.create_task(owner.start())
    try:
        assert len(set(factory.entered.values())) == 2
        assert asyncio.current_task() not in factory.entered.values()
        for session in factory.sessions.values():
            session.supports_notifications = False
            session.tool_names = ["new"]
        await asyncio.create_task(owner.relist_servers_without_notifications())
        assert owner.get_tool_ids() == {"mcp__alpha__new", "mcp__beta__new"}
    finally:
        await asyncio.create_task(owner.shutdown())
    assert factory.closed == factory.entered
    assert all(task.done() for task in factory.closed.values())
    assert {record.status for record in owner._leases.records_by_kind("mcp_connection")} == {"stopped"}


async def test_partial_start_failure_closes_all_original_contexts(lifecycle_owner, lifecycle_factory):
    lifecycle_factory.fail_connect = True
    with pytest.raises(McpCatalogError, match="stage=startup"):
        await asyncio.create_task(lifecycle_owner.start())
    assert not lifecycle_owner.started
    assert lifecycle_owner._runtimes == {}
    assert lifecycle_owner._session_scope is None
    assert lifecycle_factory.closed == lifecycle_factory.entered
    assert len(lifecycle_factory.closed) == 2
    assert all(task.done() for task in lifecycle_factory.closed.values())


async def test_restarted_owner_fences_notifications_from_previous_sessions(lifecycle_owner, lifecycle_factory):
    owner, factory = lifecycle_owner, lifecycle_factory
    await owner.start()
    prior_notifications = tuple(factory.notify_callbacks.values())
    generations = {key: runtime.generation for key, runtime in owner._runtimes.items()}
    await owner.shutdown()
    await owner.start()
    try:
        assert all(runtime.generation > generations[key] for key, runtime in owner._runtimes.items())
        revision = owner.catalog_revision()
        calls = {key: session.list_calls for key, session in factory.sessions.items()}
        for session in factory.sessions.values():
            session.tool_names = ["new-generation"]
        for notify in prior_notifications:
            notify()
        assert not owner._pending_relist_tasks
        assert {key: session.list_calls for key, session in factory.sessions.items()} == calls
        assert owner.catalog_revision() == revision
        factory.notify_callbacks["alpha"]()
        await asyncio.gather(*tuple(owner._pending_relist_tasks))
        assert "mcp__alpha__new-generation" in owner.get_tool_ids()
    finally:
        await owner.shutdown()


async def test_cancel_start_drains_initializing_server(lifecycle_owner, lifecycle_factory):
    lifecycle_factory.block_connect = True
    startup = asyncio.create_task(lifecycle_owner.start())
    await asyncio.wait_for(lifecycle_factory.connecting.wait(), 2)
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    assert lifecycle_factory.closed == lifecycle_factory.entered
    assert lifecycle_owner._session_scope is None
    assert not lifecycle_owner.started


async def test_startup_and_cleanup_failures_keep_both_causes(lifecycle_owner, lifecycle_factory):
    lifecycle_factory.fail_connect = lifecycle_factory.fail_close = True
    with pytest.raises(McpCatalogError) as raised:
        await lifecycle_owner.start()
    assert "private" not in str(raised.value)
    causes = []
    pending = [raised.value.__cause__]
    while pending:
        error = pending.pop()
        if error is None:
            continue
        causes.append(str(error))
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        pending.append(error.__cause__)
    assert "private initialize failure" in causes
    assert "private close failure" in causes
    assert lifecycle_factory.closed == lifecycle_factory.entered
    assert lifecycle_owner._session_scope is not None
    assert lifecycle_owner._session_scope.state == "close_failed"
    assert {record.status for record in lifecycle_owner._leases.records_by_kind("mcp_connection")} == {"unknown"}


async def test_sdk_task_failure_invalidates_publication_and_rejects_late_notification(
    lifecycle_owner, lifecycle_factory,
):
    owner, factory = lifecycle_owner, lifecycle_factory
    await owner.start()
    publications = []
    unsubscribe = owner.subscribe_publication(publications.append)
    revision = owner.catalog_revision()
    factory.crash.set()

    async def wait_failed():
        while any(snapshot.status != "failed" for snapshot in owner.list_servers()):
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_failed(), 2)
        assert owner.publication_snapshot().status == "unavailable"
        assert owner.catalog_revision() == revision
        assert all(snapshot.last_relist_error for snapshot in owner.list_servers())
        before = tuple(publications)
        for notify in factory.notify_callbacks.values():
            notify()
        assert tuple(publications) == before
        assert not owner._pending_relist_tasks
    finally:
        unsubscribe()
        await owner.shutdown()
    assert factory.closed == factory.entered


async def test_close_failure_remains_visible_and_blocks_reopening(lifecycle_owner, lifecycle_factory):
    await lifecycle_owner.start()
    lifecycle_factory.fail_close = True
    with pytest.raises(ExceptionGroup, match="关闭失败"):
        await lifecycle_owner.shutdown()
    assert lifecycle_factory.closed == lifecycle_factory.entered
    assert lifecycle_owner._session_scope is not None
    assert lifecycle_owner._session_scope.state == "close_failed"
    with pytest.raises(RuntimeError, match="重复启动"):
        await lifecycle_owner.start()
