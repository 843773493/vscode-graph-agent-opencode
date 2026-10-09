"""FederationRpcService 关闭 channel 时逐个提交 SQLite 注销结果。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteStateDatabase
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
)
from app.gateway.federation.errors import FEDERATION_CHANNEL_CLOSED, FederationError
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
        self.receive_gate.set()


class _FailingCloseTransport(_IdleTransport):
    def __init__(self, request: str | None = None) -> None:
        super().__init__()
        self.request = request
        self.close_error = OSError("socket close failed")
        self.close_calls = 0

    async def receive_text(self) -> str | None:
        if self.request is not None:
            request, self.request = self.request, None
            return request
        return await super().receive_text()

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason
        self.close_calls += 1
        raise self.close_error


class _BlockingCloseTransport(_IdleTransport):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, *, code: int, reason: str) -> None:
        self.close_started.set()
        await self.release_close.wait()
        await super().close(code=code, reason=reason)


class _RequestTransport(_IdleTransport):
    def __init__(self, request: str) -> None:
        super().__init__()
        self.request: str | None = request

    async def receive_text(self) -> str | None:
        if self.request is not None:
            request, self.request = self.request, None
            return request
        return await super().receive_text()


class _ObservedCloseTransport(_IdleTransport):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()

    async def close(self, *, code: int, reason: str) -> None:
        self.close_started.set()
        await super().close(code=code, reason=reason)


class _BlockingCancellationCleanupHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
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
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
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
    service: FederationRpcService
    control_store: FederationControlStore
    database: SQLiteStateDatabase


@pytest.fixture
async def federation_case(tmp_path: Path) -> AsyncIterator[_FederationCase]:
    database = federation_control_database(gateway_root=tmp_path / "gateway")
    control_store = FederationControlStore(database=database)
    service = FederationRpcService(
        gateway_id="gateway_local",
        policy_store=FederationPolicyStore(),
        control_store=control_store,
        signing_key=object(),
        catalog=WorkspaceCatalogPort(),
        session_main=WorkspaceSessionMainPort(),
        credential_store=FederationCredentialStore(
            storage_path=tmp_path / "credentials" / "federation.json"
        ),
    )
    try:
        yield _FederationCase(service, control_store, database)
    finally:
        control_store.close()


def _session(
    channel_id: str,
    peer_id: str,
    connection_id: str,
    transport: _IdleTransport | None = None,
    handler: _NoopHandler | _BlockingCancellationCleanupHandler | None = None,
) -> tuple[FederationChannelSession, _IdleTransport]:
    transport = transport or _IdleTransport()
    session = FederationChannelSession(
        transport=transport,
        handshake=ChannelHandshake(
            channel_instance_id=channel_id,
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=FederationPeerIdentity(
                gateway_id=peer_id,
                connection_id=connection_id,
                public_key_pem="unused-test-key",
            ),
            credential_digest="0" * 64,
            local_role="hub",
        ),
        handler=handler if handler is not None else _NoopHandler(),
        heartbeat_interval_seconds=3600,
        heartbeat_timeout_seconds=3600,
    )
    return session, transport


async def _register_sessions(
    service: FederationRpcService,
    sessions: tuple[tuple[FederationChannelSession, _IdleTransport], ...],
) -> None:
    for session, _transport in sessions:
        await service.register_channel(session)
        session.start()


def _deny_channel_deletes(case: _FederationCase, channel_ids: tuple[str, ...]) -> None:
    connection = case.database.connection()
    try:
        for channel_id in channel_ids:
            connection.execute(
                f"""
                CREATE TRIGGER deny_{channel_id}_unregister
                BEFORE DELETE ON federation_channel_registry
                WHEN OLD.channel_instance_id = '{channel_id}'
                BEGIN
                    SELECT RAISE(ABORT, 'deny unregister {channel_id}');
                END
                """
            )
    finally:
        connection.close()


def _active_ids(case: _FederationCase) -> tuple[str, ...]:
    return tuple(
        item.channel_instance_id
        for item in case.control_store.active_channels()
    )


async def _finish_sessions(
    sessions: tuple[tuple[FederationChannelSession, _IdleTransport], ...],
) -> None:
    await asyncio.gather(*(session.close() for session, _transport in sessions))
    await asyncio.gather(*(session.join() for session, _transport in sessions))


@pytest.mark.asyncio
async def test_close_channels_continues_after_one_sqlite_unregister_failure(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    sessions = (
        _session("channel_a", "gateway_peer_a", "connection_a"),
        _session("channel_b", "gateway_peer_b", "connection_b"),
        _session("channel_c", "gateway_peer_c", "connection_c"),
    )
    await _register_sessions(case.service, sessions)
    _deny_channel_deletes(case, ("channel_a",))

    try:
        with pytest.raises(sqlite3.IntegrityError, match="deny unregister channel_a"):
            await case.service.close_channels()

        assert all(transport.closed.is_set() for _, transport in sessions)
        assert all(
            all(task.done() for task in session._tasks)
            for session, _ in sessions
        )
        assert _active_ids(case) == ("channel_a",)
        assert tuple(case.service._local_channels) == ("channel_a",)
        assert tuple(case.service._channels) == ("gateway_peer_a",)
    finally:
        await _finish_sessions(sessions)


@pytest.mark.asyncio
async def test_close_channels_keeps_first_unregister_error_and_adds_later_error_notes(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    sessions = (
        _session("channel_a", "gateway_peer_a", "connection_a"),
        _session("channel_b", "gateway_peer_b", "connection_b"),
        _session("channel_c", "gateway_peer_c", "connection_c"),
    )
    await _register_sessions(case.service, sessions)
    _deny_channel_deletes(case, ("channel_a", "channel_b"))

    try:
        with pytest.raises(
            sqlite3.IntegrityError, match="deny unregister channel_a"
        ) as error:
            await case.service.close_channels()

        assert any(
            "deny unregister channel_b" in note
            for note in error.value.__notes__
        )
        assert _active_ids(case) == ("channel_a", "channel_b")
        assert tuple(case.service._local_channels) == (
            "channel_a",
            "channel_b",
        )
        assert tuple(case.service._channels) == (
            "gateway_peer_a",
            "gateway_peer_b",
        )
        assert all(transport.closed.is_set() for _, transport in sessions)
        assert all(
            all(task.done() for task in session._tasks)
            for session, _ in sessions
        )
    finally:
        await _finish_sessions(sessions)


@pytest.mark.asyncio
async def test_close_channels_unregisters_all_sessions_without_errors(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    sessions = (
        _session("channel_a", "gateway_peer_a", "connection_a"),
        _session("channel_b", "gateway_peer_b", "connection_b"),
    )
    await _register_sessions(case.service, sessions)

    try:
        await case.service.close_channels()

        assert all(transport.closed.is_set() for _, transport in sessions)
        assert all(
            all(task.done() for task in session._tasks)
            for session, _ in sessions
        )
        assert _active_ids(case) == ()
        assert tuple(case.service._local_channels) == ()
        assert tuple(case.service._channels) == ()
    finally:
        await _finish_sessions(sessions)


@pytest.mark.asyncio
async def test_close_failure_preserves_registered_channel_and_joins_session(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    transport = _FailingCloseTransport()
    session, _ = _session(
        "channel_close_failure",
        "gateway_peer_close_failure",
        "connection_close_failure",
        transport,
    )
    await _register_sessions(case.service, ((session, transport),))

    try:
        with pytest.raises(FederationError) as close_error:
            await case.service.close_channels()

        assert close_error.value.code == FEDERATION_CHANNEL_CLOSED
        assert "socket close failed" in " ".join(close_error.value.__notes__)
        assert _active_ids(case) == ("channel_close_failure",)
        assert tuple(case.service._local_channels) == ("channel_close_failure",)
        assert tuple(case.service._channels) == ("gateway_peer_close_failure",)
        assert all(task.done() for task in session._tasks)
        assert transport.close_calls == 1
    finally:
        for task in session._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*session._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_channels_starts_later_close_before_first_session_cleanup_finishes(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    handler = _BlockingCancellationCleanupHandler()
    first_transport = _RequestTransport(
        build_request_frame(
            correlation_id="request_close_race",
            method="federation.status",
            request={},
        ).encode()
    )
    second_transport = _ObservedCloseTransport()
    sessions = (
        _session(
            "channel_blocked_cleanup",
            "gateway_peer_blocked_cleanup",
            "connection_blocked_cleanup",
            first_transport,
            handler,
        ),
        _session(
            "channel_later_close",
            "gateway_peer_later_close",
            "connection_later_close",
            second_transport,
        ),
    )
    await _register_sessions(case.service, sessions)
    close_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(handler.started.wait(), timeout=5)
        close_task = asyncio.create_task(case.service.close_channels())
        await asyncio.wait_for(handler.cleanup_started.wait(), timeout=5)
        await asyncio.wait_for(second_transport.close_started.wait(), timeout=1)
        assert not handler.release_cleanup.is_set()
        handler.release_cleanup.set()
        await asyncio.wait_for(close_task, timeout=5)

        assert all(transport.closed.is_set() for _, transport in sessions)
        assert all(
            all(task.done() for task in session._tasks)
            for session, _ in sessions
        )
        assert _active_ids(case) == ()
    finally:
        handler.release_cleanup.set()
        if close_task is not None:
            await asyncio.wait_for(close_task, timeout=5)
        await _finish_sessions(sessions)

@pytest.mark.asyncio
async def test_close_channels_preserves_outer_cancellation_during_blocked_join(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    handler = _BlockingCancellationCleanupHandler()
    transport = _FailingCloseTransport(
        build_request_frame(
            correlation_id="request_close_cancel",
            method="federation.status",
            request={},
        ).encode()
    )
    failing_session, _ = _session(
        "channel_cancelled_close_failure",
        "gateway_peer_cancelled_close_failure",
        "connection_cancelled_close_failure",
        transport,
        handler,
    )
    healthy_session, healthy_transport = _session(
        "channel_cancelled_close_healthy",
        "gateway_peer_cancelled_close_healthy",
        "connection_cancelled_close_healthy",
    )
    sessions = ((failing_session, transport), (healthy_session, healthy_transport))
    await _register_sessions(case.service, sessions)
    close_task = asyncio.create_task(case.service.close_channels())

    try:
        await asyncio.wait_for(handler.started.wait(), timeout=5)
        await asyncio.wait_for(handler.cleanup_started.wait(), timeout=5)
        close_error = failing_session._transport_close_task
        assert close_error is not None and close_error.done()
        assert isinstance(close_error.exception(), OSError)

        close_task.cancel()
        handler.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await asyncio.wait_for(close_task, timeout=5)

        assert any("socket close failed" in note for note in cancellation.value.__notes__)
        assert _active_ids(case) == ("channel_cancelled_close_failure",)
        assert tuple(case.service._local_channels) == ("channel_cancelled_close_failure",)
        assert tuple(case.service._channels) == ("gateway_peer_cancelled_close_failure",)
        assert all(task.done() for task in failing_session._tasks)
        assert all(task.done() for task in healthy_session._tasks)
    finally:
        handler.release_cleanup.set()
        if not close_task.done():
            close_task.cancel()
        await asyncio.gather(close_task, return_exceptions=True)
        for session, _transport in sessions:
            for task in session._tasks:
                if not task.done():
                    task.cancel()
        await asyncio.gather(*(task for session, _ in sessions for task in session._tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_close_channels_preserves_outer_cancellation_during_close_batch(
    federation_case: _FederationCase,
) -> None:
    case = federation_case
    failing_session, failing_transport = _session(
        "channel_close_stage_failure",
        "gateway_peer_close_stage_failure",
        "connection_close_stage_failure",
        _FailingCloseTransport(),
    )
    blocking_transport = _BlockingCloseTransport()
    healthy_session, _ = _session(
        "channel_close_stage_healthy",
        "gateway_peer_close_stage_healthy",
        "connection_close_stage_healthy",
        blocking_transport,
    )
    sessions = ((failing_session, failing_transport), (healthy_session, blocking_transport))
    await _register_sessions(case.service, sessions)
    close_task = asyncio.create_task(case.service.close_channels())

    try:
        await asyncio.wait_for(blocking_transport.close_started.wait(), timeout=5)
        close_error = failing_session._transport_close_task
        assert close_error is not None
        with pytest.raises(OSError, match="socket close failed"):
            await asyncio.wait_for(asyncio.shield(close_error), timeout=5)

        close_task.cancel()
        blocking_transport.release_close.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await asyncio.wait_for(close_task, timeout=5)

        assert any("socket close failed" in note for note in cancellation.value.__notes__)
        assert _active_ids(case) == ("channel_close_stage_failure",)
        assert tuple(case.service._local_channels) == ("channel_close_stage_failure",)
        assert tuple(case.service._channels) == ("gateway_peer_close_stage_failure",)
        assert all(task.done() for session, _ in sessions for task in session._tasks)
    finally:
        blocking_transport.release_close.set()
        if not close_task.done():
            close_task.cancel()
        await asyncio.gather(close_task, return_exceptions=True)
        for session, _transport in sessions:
            for task in session._tasks:
                if not task.done():
                    task.cancel()
        await asyncio.gather(*(task for session, _ in sessions for task in session._tasks), return_exceptions=True)

