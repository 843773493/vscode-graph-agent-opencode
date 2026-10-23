from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.services.orchestration.terminal_steering_service import (
    TerminalSteeringService,
)


class _FakeTerminalClient:
    def __init__(self, terminals: list[dict[str, object]]) -> None:
        self.terminals = terminals
        self.finished: list[tuple[str, bool]] = []
        self.claimed: set[str] = set()
        self.sweep_error: Exception | None = None
        self.closed_session: str | None = None
        self.operation_active = False
        self.finish_failures = 0

    @asynccontextmanager
    async def operation(
        self, **arguments: object
    ) -> AsyncIterator[_FakeTerminalClient]:
        assert arguments["thread_id"] == "thr_018f0000000070008000000000000001"
        if arguments["session_id"] == self.closed_session:
            raise SessionDeletionPendingError("来源 Session 正在删除")
        self.operation_active = True
        try:
            yield self
        finally:
            self.operation_active = False

    async def sweep_orphans(self) -> list[dict[str, object]]:
        if self.sweep_error is not None:
            raise self.sweep_error
        return []

    async def list_resources(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        return [
            dict(terminal)
            for terminal in self.terminals
            if session_id is None or terminal["session_id"] == session_id
        ]

    async def claim_terminal_steering(
        self,
        terminal_id: str,
        *,
        completion_event_id: str,
        command_start_sequence: int,
    ) -> dict[str, object]:
        terminal = next(t for t in self.terminals if t["terminal_id"] == terminal_id)
        assert (completion_event_id, command_start_sequence) == (
            terminal["completion_event_id"],
            terminal["command_start_sequence"],
        )
        if terminal["steering_dispatched"]:
            return {"claimed": False}
        self.claimed.add(terminal_id)
        terminal["steering_dispatching"] = True
        return {"claimed": True}

    async def finish_terminal_steering(
        self,
        terminal_id: str,
        *,
        completion_event_id: str,
        command_start_sequence: int,
        dispatched: bool,
    ) -> dict[str, object]:
        terminal = next(t for t in self.terminals if t["terminal_id"] == terminal_id)
        assert (completion_event_id, command_start_sequence) == (
            terminal["completion_event_id"],
            terminal["command_start_sequence"],
        )
        if self.finish_failures:
            self.finish_failures -= 1
            raise ConnectionError("finish 回执丢失")
        self.finished.append((terminal_id, dispatched))
        terminal["steering_dispatching"] = False
        terminal["steering_dispatched"] = dispatched
        if not dispatched:
            self.claimed.remove(terminal_id)
        return {"terminal_id": terminal_id}


class _FlakyTerminalClient(_FakeTerminalClient):
    def __init__(self, failures: int) -> None:
        super().__init__([])
        self.failures = failures
        self.list_calls = 0
        self.ready = asyncio.Event()

    async def list_resources(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        self.list_calls += 1
        if self.failures:
            self.failures -= 1
            raise ConnectionRefusedError("terminal manager 未就绪")
        self.ready.set()
        return []


class _FakeSessionOrchestrator:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        terminal_client: _FakeTerminalClient | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error
        self.terminal_client = terminal_client

    async def submit_runtime_event(
        self,
        session_id: str,
        message: object,
        *,
        source_id: str,
        idempotency_key: str,
        delivery_policy: str = "after_turn",
        thread_id: str,
    ) -> None:
        if self.terminal_client is not None:
            assert not self.terminal_client.operation_active
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "session_id": session_id,
                "message": message,
                "delivery_policy": delivery_policy,
                "thread_id": thread_id,
                "source_id": source_id,
                "idempotency_key": idempotency_key,
            }
        )


@pytest.fixture
def terminal_with_unobserved_command_completion() -> dict[str, object]:
    return {
        "terminal_id": "term_completed",
        "session_id": "session_owner",
        "source_thread_id": "thr_018f0000000070008000000000000001",
        "reconciliation_status": "verified",
        "source_thread": {
            "session_id": "session_owner",
            "thread_id": "thr_018f0000000070008000000000000001",
        },
        "thread_associations": [
            {
                "session_id": "session_owner",
                "thread_id": "thr_018f0000000070008000000000000001",
            }
        ],
        "status": "running",
        "model_backgrounded": True,
        "last_command_status": "completed",
        "completion_observed_by_model": False,
        "steering_dispatching": False,
        "steering_dispatched": False,
        "completion_event_id": "terminal_completed:term_completed:4",
        "command_start_sequence": 1,
    }


@pytest.fixture
def terminal_client(
    terminal_with_unobserved_command_completion: dict[str, object],
) -> _FakeTerminalClient:
    return _FakeTerminalClient([terminal_with_unobserved_command_completion])


@pytest.fixture
def session_orchestrator(
    terminal_client: _FakeTerminalClient,
) -> _FakeSessionOrchestrator:
    return _FakeSessionOrchestrator(terminal_client=terminal_client)


@pytest.fixture
def terminal_steering_service(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
) -> TerminalSteeringService:
    return TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=session_orchestrator,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_scan_dispatches_one_terminal_completion_message(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    await terminal_steering_service.scan_once()
    await terminal_steering_service.scan_once()

    assert len(session_orchestrator.calls) == 1
    assert session_orchestrator.calls[0]["session_id"] == "session_owner"
    assert session_orchestrator.calls[0]["delivery_policy"] == "after_tool_result"
    message = session_orchestrator.calls[0]["message"]
    assert message.metadata["structured_prompt_kind"] == (
        "terminal_execution_completed"
    )
    assert terminal_client.finished == [("term_completed", True)]


@pytest.mark.asyncio
async def test_pending_claim_retries_original_event_after_finish_failure(
    terminal_with_unobserved_command_completion: dict[str, object],
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    terminal_client.finish_failures = 1

    with pytest.raises(ExceptionGroup, match="未收敛"):
        await terminal_steering_service.scan_once()
    assert terminal_with_unobserved_command_completion["steering_dispatching"] is True
    await terminal_steering_service.scan_once()

    assert len(session_orchestrator.calls) == 2
    assert (
        session_orchestrator.calls[0]["idempotency_key"]
        == session_orchestrator.calls[1]["idempotency_key"]
    )
    assert (
        session_orchestrator.calls[0]["message"].metadata
        == session_orchestrator.calls[1]["message"].metadata
    )
    assert terminal_with_unobserved_command_completion["steering_dispatched"] is True
    assert terminal_client.finished == [("term_completed", True)]


@pytest.mark.asyncio
async def test_scan_releases_claim_when_dispatch_fails(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    session_orchestrator.error = RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await terminal_steering_service.scan_once()

    assert terminal_client.finished == [("term_completed", False)]


@pytest.mark.asyncio
async def test_scan_ignores_completion_already_observed_by_model(
    terminal_with_unobserved_command_completion: dict[str, object],
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    terminal_with_unobserved_command_completion["completion_observed_by_model"] = True

    await terminal_steering_service.scan_once()

    assert session_orchestrator.calls == []
    assert terminal_client.finished == []


@pytest.mark.asyncio
async def test_monitor_retries_when_terminal_manager_is_temporarily_unavailable() -> (
    None
):
    terminal_client = _FlakyTerminalClient(failures=2)
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=_FakeSessionOrchestrator(),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    await service.start()
    await asyncio.wait_for(terminal_client.ready.wait(), timeout=1)
    await service.shutdown()

    assert terminal_client.list_calls >= 3


@pytest.mark.asyncio
async def test_orphan_failure_is_reported_after_other_completion_is_dispatched(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    terminal_client.sweep_error = ExceptionGroup(
        "孤儿失败", [ConnectionError("丢失删除回执")]
    )
    with pytest.raises(ExceptionGroup, match="未收敛"):
        await terminal_steering_service.scan_once()
    assert len(session_orchestrator.calls) == 1
    assert terminal_client.finished == [("term_completed", True)]


@pytest.mark.asyncio
async def test_source_session_delete_race_does_not_block_other_completion(
    terminal_with_unobserved_command_completion: dict[str, object],
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
) -> None:
    other = {
        **terminal_with_unobserved_command_completion,
        "session_id": "other-session",
        "terminal_id": "term_other",
    }
    terminal_client.terminals.append(other)
    terminal_client.closed_session = "session_owner"
    await terminal_steering_service.scan_once()
    assert [call["session_id"] for call in session_orchestrator.calls] == [
        "other-session"
    ]
    assert terminal_client.finished == [("term_other", True)]
