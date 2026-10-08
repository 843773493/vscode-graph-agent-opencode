"""联邦 owner 在控制库清理失败时仍须收口已接管的 session。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import channel_owner as channel_owner_module
from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
)
from app.gateway.federation.channel_owner import FederationChannelOwner
from app.gateway.federation.dialer import HubDialRequest
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.protocol import build_request_frame
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.store import (
    FederationControlStore,
    federation_control_database,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)


class _IdleTransport:
    def __init__(self) -> None:
        self.receive_gate = asyncio.Event()
        self.closed = asyncio.Event()

    async def send_text(self, message: str) -> None:
        del message

    async def receive_text(self) -> str | None:
        await self.receive_gate.wait()
        return None

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason
        self.closed.set()


class _RequestTransport(_IdleTransport):
    def __init__(self) -> None:
        super().__init__()
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()

    async def receive_text(self) -> str | None:
        return await self.frames.get()


class _PendingRequestHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def handle(
        self,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        del method, request, session, deadline_at
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            while not self.release_cleanup.is_set():
                try:
                    await self.release_cleanup.wait()
                except asyncio.CancelledError:
                    continue
            raise
        return {}


class _NoopHandler:
    async def handle(
        self,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        del method, request, session, deadline_at
        return {}


@dataclass(slots=True)
class _FederationCase:
    owner: FederationChannelOwner
    service: FederationRpcService
    control_store: FederationControlStore
    database: SQLiteStateDatabase
    request: HubDialRequest


@pytest.fixture
def federation_case(tmp_path: Path) -> Iterator[_FederationCase]:
    gateway_root = tmp_path / "gateway"
    database = federation_control_database(gateway_root=gateway_root)
    control_store = FederationControlStore(database=database)
    credential_store = FederationCredentialStore(
        storage_path=tmp_path / "credentials" / "federation.json"
    )
    service = FederationRpcService(
        gateway_id="gateway_local",
        policy_store=FederationPolicyStore(),
        control_store=control_store,
        signing_key=object(),
        catalog=WorkspaceCatalogPort(),
        session_main=WorkspaceSessionMainPort(),
        credential_store=credential_store,
    )
    owner = FederationChannelOwner(
        registry=object(),
        credentials=credential_store,
        gateway_root=gateway_root,
        service=service,
        reconnect_seconds=3600,
    )
    try:
        yield _FederationCase(
            owner=owner,
            service=service,
            control_store=control_store,
            database=database,
            request=HubDialRequest(
                gateway_url="http://127.0.0.1:1",
                credential_token="unused-test-token",
                expected_peer_gateway_id="gateway_peer",
                connection_id="connection_test",
            ),
        )
    finally:
        control_store.close()


def _session(
    transport: _IdleTransport,
    handler: _NoopHandler | _PendingRequestHandler | None = None,
) -> FederationChannelSession:
    return FederationChannelSession(
        transport=transport,
        handshake=ChannelHandshake(
            channel_instance_id="channel_test",
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=FederationPeerIdentity(
                gateway_id="gateway_peer",
                connection_id="connection_test",
                public_key_pem="unused-test-key",
            ),
            credential_digest="0" * 64,
            local_role="hub",
        ),
        handler=handler or _NoopHandler(),
        heartbeat_interval_seconds=3600,
        heartbeat_timeout_seconds=3600,
    )


@pytest.mark.asyncio
async def test_cancelled_owner_closes_session_when_sqlite_unregister_denied(
    federation_case: _FederationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = federation_case
    transport = _IdleTransport()
    session = _session(transport)
    dial_returned = asyncio.Event()

    async def dial(**_kwargs: object) -> FederationChannelSession:
        session.start()
        dial_returned.set()
        return session

    monkeypatch.setattr(channel_owner_module, "dial_spoke_channel", dial)
    owner_task = asyncio.create_task(case.owner._keep_channel(case.request))
    owner_task.add_done_callback(case.owner._finished)
    await asyncio.wait_for(dial_returned.wait(), timeout=1)
    assert len(case.control_store.active_channels()) == 1
    session_tasks = tuple(session._tasks)

    original_connection = case.database.connection

    def connection_with_delete_authorizer() -> sqlite3.Connection:
        connection = original_connection()
        connection.set_authorizer(
            lambda action, _arg1, _arg2, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DELETE
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(case.database, "connection", connection_with_delete_authorizer)
    owner_task.cancel()
    cancellation: asyncio.CancelledError | None = None
    cleanup_error: sqlite3.DatabaseError | None = None
    transport_closed = False
    session_tasks_done = False
    try:
        await owner_task
    except asyncio.CancelledError as error:
        cancellation = error
    except sqlite3.DatabaseError as error:
        cleanup_error = error
    finally:
        transport_closed = transport.closed.is_set()
        session_tasks_done = all(task.done() for task in session_tasks)
        for task in session_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*session_tasks, return_exceptions=True)

    assert transport_closed, (
        "SQLite authorizer denied FederationControlStore.unregister_channel DELETE; "
        f"session.close was skipped (owner error: {cleanup_error!r})"
    )
    assert session_tasks_done, "session.join was skipped"
    assert cancellation is not None, f"owner cancellation was replaced by {cleanup_error!r}"
    assert any("not authorized" in note for note in cancellation.__notes__)
    with pytest.raises(RuntimeError) as owner_failure:
        case.owner.assert_healthy()
    assert isinstance(owner_failure.value.__cause__, sqlite3.DatabaseError)


@pytest.mark.asyncio
async def test_owner_exposes_sqlite_cleanup_error_after_session_close_and_join(
    federation_case: _FederationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = federation_case
    transport = _IdleTransport()
    session = _session(transport)
    dial_returned = asyncio.Event()

    async def dial(**_kwargs: object) -> FederationChannelSession:
        session.start()
        dial_returned.set()
        return session

    monkeypatch.setattr(channel_owner_module, "dial_spoke_channel", dial)
    owner_task = asyncio.create_task(case.owner._keep_channel(case.request))
    owner_task.add_done_callback(case.owner._finished)
    await asyncio.wait_for(dial_returned.wait(), timeout=1)
    assert len(case.control_store.active_channels()) == 1
    session_tasks = tuple(session._tasks)

    original_connection = case.database.connection

    def connection_with_delete_authorizer() -> sqlite3.Connection:
        connection = original_connection()
        connection.set_authorizer(
            lambda action, _arg1, _arg2, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DELETE
                else sqlite3.SQLITE_OK
            )
        )
        return connection

    monkeypatch.setattr(case.database, "connection", connection_with_delete_authorizer)
    transport.receive_gate.set()
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        await asyncio.wait_for(owner_task, timeout=1)
    await asyncio.sleep(0)

    assert transport.closed.is_set()
    assert all(task.done() for task in session_tasks)
    with pytest.raises(RuntimeError) as owner_failure:
        case.owner.assert_healthy()
    assert isinstance(owner_failure.value.__cause__, sqlite3.DatabaseError)


@pytest.mark.asyncio
async def test_successful_owner_cleanup_returns_to_reconnect_wait(
    federation_case: _FederationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = federation_case
    transport = _IdleTransport()
    session = _session(transport)
    dial_returned = asyncio.Event()
    session_joined = asyncio.Event()
    case.owner._reconnect_seconds = 3600

    original_join = session.join

    async def observe_join() -> None:
        await original_join()
        session_joined.set()

    session.join = observe_join

    async def dial(**_kwargs: object) -> FederationChannelSession:
        session.start()
        dial_returned.set()
        return session

    monkeypatch.setattr(channel_owner_module, "dial_spoke_channel", dial)
    owner_task = asyncio.create_task(case.owner._keep_channel(case.request))
    await asyncio.wait_for(dial_returned.wait(), timeout=1)
    session_tasks = tuple(session._tasks)
    transport.receive_gate.set()
    await asyncio.wait_for(session_joined.wait(), timeout=1)
    await asyncio.wait_for(
        asyncio.gather(*session_tasks, return_exceptions=True), timeout=1
    )

    assert not case.control_store.active_channels()
    assert all(task.done() for task in session_tasks)
    assert not owner_task.done()
    try:
        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_task
    finally:
        if not owner_task.done():
            owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)
    case.owner.assert_healthy()


@pytest.mark.asyncio
async def test_owner_cancellation_during_normal_session_close_stays_healthy(
    federation_case: _FederationCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = federation_case
    transport = _RequestTransport()
    handler = _PendingRequestHandler()
    session = _session(transport, handler)
    dial_returned = asyncio.Event()
    case.owner._reconnect_seconds = 3600

    async def dial(**_kwargs: object) -> FederationChannelSession:
        session.start()
        dial_returned.set()
        return session

    monkeypatch.setattr(channel_owner_module, "dial_spoke_channel", dial)
    owner_task: asyncio.Task[None] | None = None
    session_tasks: tuple[asyncio.Task[None], ...] = ()
    observed_transport_closed = False
    observed_session_tasks_done = False
    cancellation_propagated = False
    health_failure: RuntimeError | None = None
    try:
        owner_task = asyncio.create_task(case.owner._keep_channel(case.request))
        owner_task.add_done_callback(case.owner._finished)
        await asyncio.wait_for(dial_returned.wait(), timeout=1)
        await transport.frames.put(
            build_request_frame(
                correlation_id="cor_pending_cleanup",
                method="federation.status",
                request={},
            ).encode()
        )
        await asyncio.wait_for(handler.started.wait(), timeout=1)
        session_tasks = tuple(session._tasks)
        await transport.frames.put(None)
        await asyncio.wait_for(handler.cancelled.wait(), timeout=1)

        owner_task.cancel()
        completed_while_handler_cleanup_blocked, _ = await asyncio.wait(
            {owner_task}, timeout=0.05
        )
        owner_finished_before_pending_cleanup = bool(
            completed_while_handler_cleanup_blocked
        )
        handler.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner_task, timeout=1)
        cancellation_propagated = True

        observed_transport_closed = transport.closed.is_set()
        observed_session_tasks_done = all(task.done() for task in session_tasks)
        try:
            case.owner.assert_healthy()
        except RuntimeError as error:
            health_failure = error
    finally:
        handler.release_cleanup.set()
        cleanup_tasks = list(session_tasks)
        if owner_task is not None:
            if not owner_task.done():
                owner_task.cancel()
            cleanup_tasks.append(owner_task)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert observed_transport_closed
    assert observed_session_tasks_done, "session.join 未在测试兜底清理前结束"
    assert not owner_finished_before_pending_cleanup, (
        "owner cancellation interrupted session.join before the pending handler task finished"
    )
    assert cancellation_propagated
    assert health_failure is None, (
        "正常取消被误记为 owner 永久失败: "
        f"{health_failure.__cause__ if health_failure is not None else None!r}"
    )
