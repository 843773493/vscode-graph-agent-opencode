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
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.policy import FederationPolicyStore
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
) -> tuple[FederationChannelSession, _IdleTransport]:
    transport = _IdleTransport()
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
        handler=_NoopHandler(),
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
