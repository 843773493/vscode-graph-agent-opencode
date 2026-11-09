"""真实 SDK HTTP 连接的登记、关闭与取消沿原 task 结算。"""

from __future__ import annotations

import asyncio
import errno
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogError, McpCatalogOwner
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    SchemaServerCase,
    contains_error,
)
from tests.integration.workspace_services.mcp.test_full_schema_invocation import (
    schema_server_case as schema_server_case,  # noqa: PLC0414 - 复用真实 SDK 与正式隔离工作区
)
from tests.support.mcp.http_endpoint import sdk_http_session_factory


@dataclass
class PublicationGate:
    path: Path
    resource_id: str = ""
    status: str = "running"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    writer_thread: int | None = None
    failure: OSError | None = None

    def arm(self, owner: McpCatalogOwner, *, closing: bool = False) -> None:
        generation = owner._connection_generation + (0 if closing else 1)
        self.resource_id = f"mcp-connection:v1:{owner._instance_id}:{generation}"
        self.status = "stopped" if closing else "running"

    def cold_status(self) -> str | None:
        record = ExternalResourceLeaseLedger(state_path=self.path).get(self.resource_id)
        return None if record is None else record.status


@pytest.fixture
async def publication_gate(
    schema_server_case: SchemaServerCase, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[PublicationGate]:
    """仅暂扣原子替换；保留 SDK 原 task、账本锁、fsync 与发布顺序。"""
    gate = PublicationGate(schema_server_case.workspace_root / ".boxteam/resources.json")
    loop, replace = asyncio.get_running_loop(), Path.replace

    def publish(source: Path, target: Path) -> Path:
        if target == gate.path and gate.writer_thread is None and gate.resource_id:
            records = json.loads(source.read_text(encoding="utf-8"))["records"]
            if any(row["resource_id"] == gate.resource_id and row["status"] == gate.status for row in records):
                gate.writer_thread = get_ident()
                loop.call_soon_threadsafe(gate.entered.set)
                assert gate.release.wait(4), "测试未放行原连接账本发布"
                if gate.failure is not None:
                    raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publish)
    try:
        yield gate
    finally:
        gate.release.set()


@pytest.fixture
async def initializing_exit_owner(bridge_endpoint_factory):
    async with bridge_endpoint_factory(args=["-c", "import sys; sys.stdin.buffer.readline(); sys.exit(17)"]) as (endpoint, token):
        context = endpoint.context
        owner = McpCatalogOwner(
            leases=ExternalResourceLeaseLedger(state_path=context.workspace_root / ".boxteam/resources.json"),
            raw_config={"servers": {"exiting": {"enabled": True, "transport": "streamable_http",
                "url": endpoint.url, "headers": {"X-Local-Token": token}}}},
            workspace_root=context.workspace_root, event_service=EventChannelService(),
            session_factory=sdk_http_session_factory,
        )
        try:
            yield owner, context
        finally:
            await owner.shutdown()


async def test_initialize_eof_keeps_protocol_error_and_cold_connection_state(initializing_exit_owner):
    owner, context = initializing_exit_owner
    with pytest.raises(McpCatalogError, match="stage=startup") as caught:
        await owner.start()
    errors: list[BaseException] = []
    pending: list[BaseException] = [caught.value]
    while pending:
        error = pending.pop()
        errors.append(error)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
    assert any(isinstance(error, McpError) and error.error.code == CONNECTION_CLOSED for error in errors)
    assert not owner.started
    with pytest.raises(RuntimeError, match="尚未启动"):
        owner.get_tool_ids()
    record, = [record for record in owner._leases.records_by_kind("mcp_connection")
               if record.resource_id.startswith(f"mcp-connection:v1:{owner._instance_id}:")]
    assert record.status == "unknown" and record.process is None
    cold = ExternalResourceLeaseLedger(state_path=context.workspace_root / ".boxteam/resources.json")
    assert cold.get(record.resource_id) == record
    assert owner._session_scope is None
    await owner.shutdown()


async def test_close_during_registration_never_publishes_readiness(
    schema_server_case: SchemaServerCase, publication_gate: PublicationGate,
) -> None:
    owner, gate = schema_server_case.owner, publication_gate
    gate.arm(owner)
    opening = asyncio.create_task(owner.start())
    closing = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writer_thread != get_ident()
        runtime = owner._runtimes["schema"]
        closing = asyncio.create_task(runtime.scope.close())
        await asyncio.sleep(0.02)
        assert not opening.done() and not closing.done()
        assert gate.cold_status() == "unknown" and not owner.started
        record = ExternalResourceLeaseLedger(state_path=gate.path).get(gate.resource_id)
        assert record is not None and record.process is None
        gate.release.set()
        with pytest.raises(McpCatalogError, match="stage=startup"):
            await opening
        await closing
        assert not owner.started and not runtime.running
        assert runtime._task is not None and runtime._task.done()
        assert gate.cold_status() == "stopped"
    finally:
        gate.release.set()
        await asyncio.gather(opening, *([] if closing is None else [closing]), return_exceptions=True)
        await owner.shutdown()


@pytest.mark.parametrize("request_close_first", [False, True])
async def test_start_cancel_and_scope_close_wait_for_same_sdk_cleanup(
    schema_server_case: SchemaServerCase, publication_gate: PublicationGate,
    monkeypatch: pytest.MonkeyPatch, request_close_first: bool,
) -> None:
    case, gate = schema_server_case, publication_gate
    owner = case.owner
    factory = owner._session_factory
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    sdk_tasks: list[asyncio.Task[object]] = []

    @asynccontextmanager
    async def observe_cleanup(server, on_change):
        async with factory(server, on_change) as session:
            task = asyncio.current_task()
            assert task is not None
            sdk_tasks.append(task)
            try:
                yield session
            finally:
                cleanup_entered.set()
                await cleanup_release.wait()

    monkeypatch.setattr(owner, "_session_factory", observe_cleanup)
    gate.arm(owner)
    opening = asyncio.create_task(owner.start())
    closing = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        runtime = owner._runtimes["schema"]
        opening.cancel("原启动取消")
        gate.release.set()
        await asyncio.wait_for(cleanup_entered.wait(), 6)
        if request_close_first:
            runtime.request_close()
        closing = asyncio.create_task(runtime.scope.close())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.05)
        assert not opening.done() and not runtime.scope.is_closed
        assert runtime._task is not None and sdk_tasks == [runtime._task]
        assert not runtime._task.done()
        assert gate.cold_status() == "running" and not owner.started
        cleanup_release.set()
        await asyncio.wait_for(closing, 6)
        with pytest.raises(asyncio.CancelledError, match="原启动取消"):
            await asyncio.wait_for(opening, 6)
        assert runtime._task.done() and runtime.scope.is_closed
        assert gate.cold_status() == "stopped" and not case.received()
        assert owner._session_scope is None
    finally:
        gate.release.set()
        cleanup_release.set()
        await asyncio.gather(opening, *([] if closing is None else [closing]), return_exceptions=True)
        await owner.shutdown()


async def test_initialization_callback_control_exception_still_closes_candidate(
    schema_server_case: SchemaServerCase,
) -> None:
    class ConnectionAbort(BaseException):
        """验证控制流异常也沿原finally语义回收，避免让SystemExit结束pytest。"""

    owner = schema_server_case.owner
    original = ConnectionAbort("初始化观察者中断")

    def initialized() -> None:
        raise original

    await owner.start()
    try:
        await owner.disconnect_server("schema", admit=lambda: None)
        with pytest.raises(ConnectionAbort) as caught:
            await owner.connect_server("schema", admit=lambda: None, on_initialized=initialized)
        assert caught.value is original
        records = ExternalResourceLeaseLedger(state_path=schema_server_case.workspace_root / ".boxteam/resources.json").records_by_kind("mcp_connection")
        owned = [record for record in records if record.resource_id.startswith(f"mcp-connection:v1:{owner._instance_id}:")]
        assert len(owned) == 2 and all(record.status == "stopped" for record in owned)
        assert owner.get_tool_ids() == frozenset()
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("entry", ["startup", "reconnect", "replacement"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "cancel-failure"])
async def test_registration_settles_before_readiness_or_cancel_returns(
    schema_server_case: SchemaServerCase, publication_gate: PublicationGate, entry: str, outcome: str,
) -> None:
    case, gate = schema_server_case, publication_gate
    owner = case.owner
    scope = LifetimeScope("connection-publication-candidate")
    initialized: list[bool] = []
    if entry != "startup":
        await owner.start()
    if entry == "reconnect":
        await owner.disconnect_server("schema", admit=lambda: None)
    gate.arm(owner)
    if "failure" in outcome:
        gate.failure = OSError(errno.ENOSPC, "原连接账本发布失败", str(case.workspace_root / "private-ledger"))

    async def open_connection() -> None:
        if entry == "startup":
            await owner.start()
        elif entry == "reconnect":
            await owner.connect_server("schema", admit=lambda: None, on_initialized=lambda: initialized.append(True))
        else:
            server = owner.server_configuration("schema")
            assert server.connection is not None
            payload = {"servers": {"schema": {**server.connection, "enabled": True,
                "headers": {**server.connection.get("headers", {}), "X-Test-Generation": "new"}}}}
            publish, _ = await owner.prepare_config(payload, scope, lambda _effect: None, plugin_bindings=())
            publish()

    opening = asyncio.create_task(open_connection())
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writer_thread != get_ident(), "连接登记仍在事件循环中阻塞"
        # 启动身份已预登记；running 提交前仍须保留 unknown，不能伪报就绪。
        assert gate.cold_status() == "unknown"
        record = ExternalResourceLeaseLedger(state_path=gate.path).get(gate.resource_id)
        assert record is not None and record.process is None
        assert not opening.done() and not initialized
        if entry == "startup":
            assert not owner.started
        else:
            # 磁盘未提交的新代不能成为可执行 binding；原 publication 锁不能被 worker 持有。
            assert all(target.connection_ref != gate.resource_id for target in owner.binding_snapshot().targets.values())
        if outcome.startswith("cancel"):
            opening.cancel("原连接取消")
            await asyncio.sleep(0.02)
            opening.cancel("重复取消")
            await asyncio.sleep(0.02)
            assert not opening.done(), "取消遗弃了原连接登记或 SDK 清理"
            assert gate.cold_status() == "unknown"
        gate.release.set()
        if outcome.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError, match="原连接取消") as caught:
                await opening
            if gate.failure is not None:
                assert contains_error(caught.value, gate.failure)
            assert gate.cold_status() == "stopped"
        elif gate.failure is not None:
            with pytest.raises(Exception) as caught:
                await opening
            assert contains_error(caught.value, gate.failure)
            assert gate.cold_status() == "stopped"
        else:
            await opening
            assert gate.cold_status() == "running"
            result = await case.invoker().ainvoke({"tool_name": "mcp__schema__probe", "arguments": {"text": entry}})
            assert result
            assert case.received() == [{"name": "probe", "arguments": {"text": entry}}]
    finally:
        gate.release.set()
        await asyncio.gather(opening, return_exceptions=True)
        # 注入写盘失败的原 task 保留失败身份；清理不得假报成功或启动新连接。
        for close in (scope.close, owner.shutdown):
            try:
                await close()
            except ExceptionGroup as error:
                assert gate.failure is not None and contains_error(error, gate.failure)
    assert gate.cold_status() == "stopped"
    record = ExternalResourceLeaseLedger(state_path=gate.path).get(gate.resource_id)
    assert record is not None and record.process is None


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_close_waits_for_original_terminal_publication(
    schema_server_case: SchemaServerCase, publication_gate: PublicationGate, outcome: str,
) -> None:
    case, gate = schema_server_case, publication_gate
    owner = case.owner
    await owner.start()
    runtime = owner._runtimes["schema"]
    gate.arm(owner, closing=True)
    if outcome == "failure":
        gate.failure = OSError(errno.ENOSPC, "连接终态发布失败", str(case.workspace_root / "private-ledger"))
    closing = asyncio.create_task(runtime.scope.close())
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writer_thread != get_ident(), "连接终态仍在事件循环中阻塞"
        assert gate.cold_status() == "running"
        assert not closing.done() and runtime._task is not None and not runtime._task.done()
        assert not runtime.running
        assert owner.binding_snapshot()
        if outcome == "cancel":
            closing.cancel("取消关闭观察者")
            with pytest.raises(asyncio.CancelledError, match="取消关闭观察者"):
                await closing
            assert not runtime._task.done() and not runtime.scope.is_closed
        gate.release.set()
        if gate.failure is not None:
            with pytest.raises(ExceptionGroup) as caught:
                await closing
            assert contains_error(caught.value, gate.failure)
            assert gate.cold_status() == "running"
        else:
            # scope 重入等待原释放，不重复 SDK 关闭或账本发布。
            await runtime.scope.close()
            assert runtime._task.done() and gate.cold_status() == "stopped"
    finally:
        gate.release.set()
        await asyncio.gather(closing, return_exceptions=True)
        if gate.failure is None:
            await owner.shutdown()
        else:
            with pytest.raises(ExceptionGroup) as caught:
                await owner.shutdown()
            assert contains_error(caught.value, gate.failure)
            assert owner._session_scope is not None and owner._session_scope.state == "close_failed"
            case.expected_shutdown_failure = caught.value
