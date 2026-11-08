from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.abstractions.workspace_resources import ResourceAssociationMissing
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.shielded_cleanup import run_cleanup_shielded
from app.services.orchestration import terminal_steering_service as steering_module
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
@pytest.mark.parametrize("availability", ["disabled", "starting"])
async def test_unavailable_plugin_keeps_monitor_alive_and_resumes_after_enable(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    availability: str,
) -> None:
    from app.abstractions.terminal.resources import (
        TerminalPluginDisabled,
        TerminalRequestError,
    )

    observed = asyncio.Event()
    original_sweep = terminal_client.sweep_orphans

    async def scan_disabled():
        observed.set()
        return await original_sweep()

    terminal_client.sweep_orphans = scan_disabled
    terminal_client.sweep_error = (
        TerminalPluginDisabled("未开启") if availability == "disabled"
        else TerminalRequestError(method="GET", path="/api/terminals",
            status=409, response_body=availability)
    )
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=session_orchestrator,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )
    await service.start()
    try:
        await asyncio.wait_for(observed.wait(), 1)
        assert not session_orchestrator.calls
        assert service._task is not None and not service._task.done()
        terminal_client.sweep_error = None
        async with asyncio.timeout(1):
            while not session_orchestrator.calls:
                await asyncio.sleep(0.001)
        assert len(session_orchestrator.calls) == 1
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500, 503])
async def test_monitor_reports_non_transition_http_failures(
    terminal_client: _FakeTerminalClient,
    terminal_steering_service: TerminalSteeringService,
    status: int,
) -> None:
    from app.abstractions.terminal.resources import TerminalRequestError

    error = TerminalRequestError(method="GET", path="/api/terminals", status=status, response_body="失败")
    terminal_client.sweep_error = error
    with pytest.raises(TerminalRequestError) as failed:
        await terminal_steering_service._run()
    assert failed.value is error


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
@pytest.mark.parametrize("released_at", ["claim", "finish"])
async def test_released_thread_association_does_not_kill_completion_monitor(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    terminal_steering_service: TerminalSteeringService,
    monkeypatch: pytest.MonkeyPatch,
    released_at: str,
) -> None:
    original_operation = terminal_client.operation
    entered = 0

    @asynccontextmanager
    async def released_operation(**arguments: object) -> AsyncIterator[_FakeTerminalClient]:
        nonlocal entered
        entered += 1
        if entered == (1 if released_at == "claim" else 2):
            terminal_client.terminals.clear()
            raise ResourceAssociationMissing("原临时终端已由 write_stdin 完成回收")
        async with original_operation(**arguments) as operation:
            yield operation

    monkeypatch.setattr(terminal_client, "operation", released_operation)
    await terminal_steering_service.scan_once()
    await terminal_steering_service.scan_once()
    assert len(session_orchestrator.calls) == (0 if released_at == "claim" else 1)
    assert not terminal_client.finished


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


@pytest.fixture
def protected_scan_monitor(
    terminal_client: _FakeTerminalClient,
    session_orchestrator: _FakeSessionOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TerminalSteeringService, asyncio.Event, asyncio.Event, list[str]]:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    terminal_client.terminals.clear()

    async def finish() -> None:
        await release.wait()

    async def scan() -> list[dict[str, object]]:
        calls.append("sweep")
        entered.set()
        # 复用真实owner的结算原语：扫描可在完成清理后正常返回，消耗关闭取消。
        await run_cleanup_shielded(finish)
        return []

    monkeypatch.setattr(terminal_client, "sweep_orphans", scan)
    service = TerminalSteeringService(
        terminal_client=terminal_client,
        session_orchestrator=session_orchestrator,  # type: ignore[arg-type]
        poll_interval_seconds=3600,
    )
    return service, entered, release, calls


@pytest.mark.parametrize("close_count", [1, 2], ids=["single", "overlap"])
async def test_shutdown_does_not_poll_again_after_protected_scan_consumes_cancellation(
    protected_scan_monitor: tuple[TerminalSteeringService, asyncio.Event, asyncio.Event, list[str]],
    close_count: int,
) -> None:
    service, entered, release, calls = protected_scan_monitor
    await service.start()
    await asyncio.wait_for(entered.wait(), 1)
    monitor = service._task
    assert monitor is not None
    closing = [asyncio.create_task(service.shutdown()) for _ in range(close_count)]
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not monitor.done() and monitor.cancelling() == 1
        release.set()
        done, _pending = await asyncio.wait(closing, timeout=1)
        assert done == set(closing), "关闭取消已被扫描收尾消耗，原监控却继续轮询睡眠"
        assert monitor.done() and service._task is None
        assert calls == ["sweep"]
        await service.shutdown()
        # 仍沿原start/shutdown支持同一service重新启动，不遗留第二监控。
        await service.start()
        await service.shutdown()
        assert service._task is None and calls == ["sweep"]
    finally:
        release.set()
        if not monitor.done():
            monitor.cancel()
        await asyncio.gather(monitor, *closing, return_exceptions=True)


@pytest.mark.parametrize("failed_before_close", [False, True], ids=["during-close", "already-failed"])
async def test_shutdown_propagates_original_monitor_failure_once(
    protected_scan_monitor: tuple[TerminalSteeringService, asyncio.Event, asyncio.Event, list[str]],
    terminal_client: _FakeTerminalClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_before_close: bool,
) -> None:
    service, entered, release, calls = protected_scan_monitor
    original_scan = terminal_client.sweep_orphans
    failure = OSError(5, "原终端扫描失败")

    async def scan() -> list[dict[str, object]]:
        await original_scan()
        raise failure

    monkeypatch.setattr(terminal_client, "sweep_orphans", scan)
    await service.start()
    await asyncio.wait_for(entered.wait(), 1)
    monitor = service._task
    assert monitor is not None
    closing = None
    try:
        if failed_before_close:
            release.set()
            done, _pending = await asyncio.wait((monitor,), timeout=1)
            assert done == {monitor}
        closing = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(OSError) as caught:
            await asyncio.wait_for(asyncio.shield(closing), 1)
        assert caught.value is failure and monitor.exception() is failure
        assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
        assert service._task is None and calls == ["sweep"]
        await service.shutdown()
        assert calls == ["sweep"]
    finally:
        release.set()
        await asyncio.gather(monitor, *([closing] if closing is not None else []), return_exceptions=True)


@pytest.mark.parametrize("late_failure", [False, True], ids=["success", "late-failure"])
async def test_cancelled_shutdown_observer_keeps_original_monitor_for_retry(
    protected_scan_monitor: tuple[TerminalSteeringService, asyncio.Event, asyncio.Event, list[str]],
    terminal_client: _FakeTerminalClient,
    monkeypatch: pytest.MonkeyPatch,
    late_failure: bool,
) -> None:
    service, entered, release, calls = protected_scan_monitor
    original_scan = terminal_client.sweep_orphans
    failure = OSError(5, "原监控迟到失败")

    async def scan() -> list[dict[str, object]]:
        result = await original_scan()
        if late_failure:
            raise failure
        return result

    monkeypatch.setattr(terminal_client, "sweep_orphans", scan)
    await service.start()
    await asyncio.wait_for(entered.wait(), 1)
    monitor = service._task
    assert monitor is not None
    closing = asyncio.create_task(service.shutdown())
    try:
        await asyncio.sleep(0)
        assert monitor.cancelling() == 1
        closing.cancel("取消关闭观察")
        done, _pending = await asyncio.wait((closing,), timeout=1)
        assert done == {closing}, "关闭观察取消不应等待或再次取消原监控"
        with pytest.raises(asyncio.CancelledError, match="取消关闭观察"):
            await closing
        assert service._task is monitor and not monitor.done() and monitor.cancelling() == 1
        with pytest.raises(RuntimeError, match="已经启动"):
            await service.start()
        release.set()
        done, _pending = await asyncio.wait((monitor,), timeout=1)
        assert done == {monitor} and service._task is monitor
        if late_failure:
            with pytest.raises(OSError) as caught:
                await service.shutdown()
            assert caught.value is failure
        else:
            await service.shutdown()
        assert service._task is None and calls == ["sweep"]
    finally:
        release.set()
        await asyncio.gather(monitor, closing, return_exceptions=True)


@pytest.mark.parametrize("failure_type", [None, OSError, TimeoutError, ConnectionError],
                         ids=["success", "failure", "timeout", "connection"])
async def test_shutdown_timeout_retains_original_monitor_and_late_result(
    protected_scan_monitor: tuple[TerminalSteeringService, asyncio.Event, asyncio.Event, list[str]],
    terminal_client: _FakeTerminalClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception] | None,
) -> None:
    service, entered, release, calls = protected_scan_monitor
    original_scan = terminal_client.sweep_orphans
    failure = None if failure_type is None else failure_type("原监控迟到失败")

    async def scan() -> list[dict[str, object]]:
        result = await original_scan()
        if failure is not None:
            raise failure
        return result

    monkeypatch.setattr(terminal_client, "sweep_orphans", scan)
    # 单测压缩观察时间；默认30秒另由真实Workspace生命周期验收。
    monkeypatch.setattr(steering_module, "MONITOR_CLOSE_WAIT_SECONDS", 0.01, raising=False)
    await service.start()
    await asyncio.wait_for(entered.wait(), 1)
    monitor = service._task
    assert monitor is not None
    closing: list[asyncio.Task[None]] = []
    try:
        for _ in range(2):
            closing.append(asyncio.create_task(service.shutdown()))
            with pytest.raises(TimeoutError, match="终端完成监控关闭等待超过"):
                await asyncio.wait_for(asyncio.shield(closing[-1]), 0.5)
            assert service._task is monitor and not monitor.done() and monitor.cancelling() == 1
            assert calls == ["sweep"]
            with pytest.raises(RuntimeError, match="已经启动"):
                await service.start()
        release.set()
        await asyncio.wait((monitor,), timeout=1)
        assert monitor.done() and service._task is monitor
        if failure is None:
            await service.shutdown()
        else:
            with pytest.raises(type(failure)) as caught:
                await service.shutdown()
            assert caught.value is failure and monitor.exception() is failure
        assert service._task is None and calls == ["sweep"]
        await service.shutdown()
    finally:
        release.set()
        await asyncio.gather(monitor, *closing, return_exceptions=True)
