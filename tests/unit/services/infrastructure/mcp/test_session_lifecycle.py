"""用真实 AnyIO CancelScope 验证 Server 原 task 的生命周期与故障发布。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import anyio
import pytest

from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogError, McpCatalogOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.invocation import McpResourceUse
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
            self.block_cleanup = False
            self.cleanup_entered = asyncio.Event()
            self.cleanup_release = asyncio.Event()

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
                    if self.block_cleanup:
                        self.cleanup_entered.set()
                        await self.cleanup_release.wait()
                    current = asyncio.current_task()
                    assert current is not None
                    self.closed[server.server_id] = current

            return session()

    return SessionFactory()


@pytest.fixture
def lifecycle_owner(tmp_path, event_channel_service, lifecycle_factory):
    return McpCatalogOwner(
        leases=ExternalResourceLeaseLedger(),
        raw_config={"servers": {name: {
            "enabled": True, "transport": "streamable_http", "url": f"http://127.0.0.1:1/{name}/mcp",
        } for name in ("alpha", "beta")}},
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


async def test_explicit_refresh_skips_stopped_runtime_and_still_refreshes_live_sibling(lifecycle_owner, lifecycle_factory):
    owner, factory = lifecycle_owner, lifecycle_factory
    await owner.start()
    try:
        for session in factory.sessions.values():
            session.supports_notifications = False
        calls = {key: session.list_calls for key, session in factory.sessions.items()}
        await owner._runtimes["alpha"].scope.close()
        assert {server.server_id: server.status for server in owner.list_servers()} == {"alpha": "failed", "beta": "ready"}
        assert owner.servers_without_change_notifications() == ("beta",)
        factory.sessions["beta"].tool_names = ["new"]
        await owner.relist_servers_without_notifications()
        assert {key: session.list_calls for key, session in factory.sessions.items()} == {
            "alpha": calls["alpha"], "beta": calls["beta"] + 1,
        }
        assert "mcp__beta__new" in owner.get_tool_ids()
    finally:
        await owner.shutdown()


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


async def test_connection_identity_is_persisted_before_factory_admission(lifecycle_owner, lifecycle_factory):
    owner, factory = lifecycle_owner, lifecycle_factory
    factory.block_connect = True
    starting = asyncio.create_task(owner.start())
    await asyncio.wait_for(factory.connecting.wait(), 2)
    runtime = owner._runtimes["alpha"]
    record = owner._leases.get(runtime.connection_ref)
    assert record is not None and record.status == "unknown"
    assert record.process is None
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    record = owner._leases.get(runtime.connection_ref)
    assert record is not None and record.status == "stopped"
    assert record.process is None and not record.cleanup_failed


@pytest.mark.parametrize("request_close_first", [False, True])
async def test_scope_close_does_not_cancel_initializing_session_cleanup_twice(
    lifecycle_owner, lifecycle_factory, request_close_first,
):
    owner, factory = lifecycle_owner, lifecycle_factory
    factory.block_connect = factory.block_cleanup = True
    startup = asyncio.create_task(owner.start())
    await asyncio.wait_for(factory.connecting.wait(), 2)
    runtime = owner._runtimes["alpha"]
    startup.cancel("启动等待者取消")
    await asyncio.wait_for(factory.cleanup_entered.wait(), 2)
    if request_close_first:
        runtime.request_close()
    closing = asyncio.create_task(runtime.scope.close())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.05)
        assert not startup.done()
        assert runtime._task is factory.entered["alpha"]
        assert not runtime._task.done()
        assert not factory.closed
    finally:
        factory.cleanup_release.set()
        await asyncio.wait_for(closing, 2)
        with pytest.raises(asyncio.CancelledError, match="启动等待者取消"):
            await asyncio.wait_for(startup, 2)
    assert factory.closed == factory.entered
    assert owner._session_scope is None
    assert not owner.started


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
    records = lifecycle_owner._leases.records_by_kind("mcp_connection")
    assert {record.status for record in records} == {"unknown"}
    assert all(record.process is None for record in records)
    assert {record.mcp_server_id for record in records if record.cleanup_failed} == {"alpha"}


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
        assert all(server.connection_ref is None for server in owner.list_servers())
        assert all([tool.remote_name for tool in server.tools] == ["echo"] for server in owner.list_servers())
        assert owner.catalog_revision() == revision
        assert all(snapshot.last_relist_error for snapshot in owner.list_servers())
        before = tuple(publications)
        for notify in factory.notify_callbacks.values():
            notify()
        assert tuple(publications) == before
        assert not owner._pending_relist_tasks
    finally:
        unsubscribe()
        with pytest.raises(ExceptionGroup, match="MCP Owner 关闭失败"):
            await owner.shutdown()
    records = owner._leases.records_by_kind("mcp_connection")
    assert {record.status for record in records} == {"unknown"}
    assert all(record.process is None and record.cleanup_failed for record in records)
    assert factory.closed == factory.entered


async def test_explicit_disconnect_hides_known_tools_while_original_cleanup_is_pending(
    lifecycle_owner, lifecycle_factory,
):
    owner, factory = lifecycle_owner, lifecycle_factory
    await owner.start()
    binding = owner.binding_snapshot()
    with owner.execution_handles.reserve_model_call(binding, session_id="session", thread_id="thread", model_call_id="model") as retain:
        retain([{"name": "invoke_extension_tool", "id": "call", "args": {"tool_name": "mcp__alpha__echo"}}])
    use = McpResourceUse("holder", software_tool_operation_id("mcp_call", "session", "thread", "model", "call"), "session", "thread", "model")
    factory.block_cleanup = True
    closing = asyncio.create_task(owner.disconnect_server("alpha", admit=lambda: None))
    try:
        await asyncio.wait_for(factory.cleanup_entered.wait(), 2)
        assert not closing.done()
        disconnected = next(server for server in owner.list_servers() if server.server_id == "alpha")
        assert disconnected.status == "failed" and disconnected.connection_ref is None
        assert disconnected.tools == ()
        assert [tool.tool_id for tool in owner._descriptors_by_server["alpha"]] == ["mcp__alpha__echo"]
        assert owner.publication_snapshot().definitions["mcp__alpha__echo"].readiness == "disconnected"
        with pytest.raises(ExtensionTargetResolutionError), owner.execution_handles.execution_target(binding.resolve("mcp__alpha__echo"), use):
            pytest.fail("主动断开已撤销原调用资格，不能派发")
        factory.cleanup_release.set()
        await closing
        assert next(server for server in owner.list_servers() if server.server_id == "alpha").tools == ()
    finally:
        factory.cleanup_release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await owner.shutdown()


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


async def test_lost_sdk_and_terminal_write_failure_are_both_reported_immediately(
    lifecycle_owner, lifecycle_factory, monkeypatch, caplog,
):
    owner, factory = lifecycle_owner, lifecycle_factory
    await owner.start()
    original = OSError("原终态写盘失败")
    connection_ref = owner._connection_ref(owner._runtimes["alpha"])
    update = owner._leases.update_external_state

    def failed_update(resource_id, **kwargs):
        if resource_id == connection_ref:
            raise original
        update(resource_id, **kwargs)

    def contains(error):
        return error is original or isinstance(error, BaseExceptionGroup) and any(contains(child) for child in error.exceptions)

    monkeypatch.setattr(owner._leases, "update_external_state", failed_update)
    factory.crash.set()
    await asyncio.wait_for(asyncio.gather(*(runtime._task for runtime in owner._runtimes.values())), 2)
    # 不能只把写盘错误留到shutdown；原owner必须立即取得原SDK与原存储失败。
    failures = [record.exc_info[1] for record in caplog.records if record.exc_info is not None]
    assert any(contains(error) for error in failures)
    assert owner.publication_snapshot().status == "unavailable"
    assert owner._leases.get(connection_ref).status == "running"
    with pytest.raises(ExceptionGroup, match="MCP Owner 关闭失败"):
        await owner.shutdown()
