from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.orchestration.terminal_steering_service import (
    TerminalSteeringService,
)


class _FakeTerminalClient:
    def __init__(self, terminals: list[dict[str, object]]) -> None:
        self.terminals = terminals
        self.finished: list[tuple[str, bool]] = []
        self.claimed: set[str] = set()

    async def list_terminals(
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
    ) -> dict[str, object]:
        if terminal_id in self.claimed:
            return {"claimed": False}
        self.claimed.add(terminal_id)
        return {"claimed": True}

    async def finish_terminal_steering(
        self,
        terminal_id: str,
        *,
        dispatched: bool,
    ) -> dict[str, object]:
        self.finished.append((terminal_id, dispatched))
        if not dispatched:
            self.claimed.remove(terminal_id)
        return {"terminal_id": terminal_id}


class _FlakyTerminalClient(_FakeTerminalClient):
    def __init__(self, failures: int) -> None:
        super().__init__([])
        self.failures = failures
        self.list_calls = 0
        self.ready = asyncio.Event()

    async def list_terminals(
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
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def create_and_run_internal(
        self,
        session_id: str,
        message: object,
        *,
        delivery_policy: str = "after_turn",
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "session_id": session_id,
                "message": message,
                "delivery_policy": delivery_policy,
            }
        )


@pytest.fixture
def completed_terminal() -> dict[str, object]:
    return {
        "terminal_id": "term_completed",
        "session_id": "session_owner",
        "status": "running",
        "model_backgrounded": True,
        "last_command_status": "completed",
        "completion_observed_by_model": False,
        "steering_dispatching": False,
        "steering_dispatched": False,
        "completion_event_id": "terminal_completed:term_completed:4",
    }


@pytest.mark.asyncio
async def test_scan_dispatches_one_terminal_completion_message(
    completed_terminal: dict[str, object],
) -> None:
    terminal_client = _FakeTerminalClient([completed_terminal])
    orchestrator = _FakeSessionOrchestrator()
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    await service.scan_once()
    await service.scan_once()

    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["session_id"] == "session_owner"
    assert orchestrator.calls[0]["delivery_policy"] == "after_tool_result"
    message = orchestrator.calls[0]["message"]
    assert message.metadata["structured_prompt_kind"] == (
        "terminal_execution_completed"
    )
    assert terminal_client.finished == [("term_completed", True)]


@pytest.mark.asyncio
async def test_scan_releases_claim_when_dispatch_fails(
    completed_terminal: dict[str, object],
) -> None:
    terminal_client = _FakeTerminalClient([completed_terminal])
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=_FakeSessionOrchestrator(error=RuntimeError("dispatch failed")),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await service.scan_once()

    assert terminal_client.finished == [("term_completed", False)]


@pytest.mark.asyncio
async def test_scan_ignores_completion_already_observed_by_model(
    completed_terminal: dict[str, object],
) -> None:
    completed_terminal["completion_observed_by_model"] = True
    terminal_client = _FakeTerminalClient([completed_terminal])
    orchestrator = _FakeSessionOrchestrator()
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=orchestrator,  # type: ignore[arg-type]
    )

    await service.scan_once()

    assert orchestrator.calls == []
    assert terminal_client.finished == []


@pytest.mark.asyncio
async def test_monitor_retries_when_terminal_manager_is_temporarily_unavailable() -> None:
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
