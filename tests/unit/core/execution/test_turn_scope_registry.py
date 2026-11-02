"""Turn 原注册表在关闭成功前保留身份；并发、失败与取消共用原清理链。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.turn_execution_scope import (
    AgentControlInbox,
    TurnExecutionScope,
    TurnExecutionScopeRegistry,
)


@pytest.fixture
async def registered_turn(
    tmp_path: Path,
) -> tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path]:
    registry = TurnExecutionScopeRegistry()
    scope = registry.create("stream_1")
    state_path = tmp_path / "control.json"
    inbox = await AgentControlInbox.open("stream_1", session_id="ses_1", state_path=state_path)
    await inbox.accept(command_id="cmd_1", kind="interrupt", idempotency_key="idem_1")
    registry.register_inbox("stream_1", inbox)
    return registry, scope, inbox, state_path


@pytest.mark.asyncio
async def test_control_inbox_state_is_reaped_on_turn_close(tmp_path: Path) -> None:
    """Turn 收敛即确定性删除控制状态文件，不随 Turn 数无限堆积。"""
    state_path = tmp_path / "control.json"
    registry = TurnExecutionScopeRegistry()
    registry.create("stream_1")
    inbox = await AgentControlInbox.open("stream_1", session_id="ses_1", state_path=state_path)
    await inbox.accept(
        command_id="cmd_1",
        kind="interrupt",
        idempotency_key="idem_1",
    )
    registry.register_inbox("stream_1", inbox)
    assert state_path.is_file()

    await registry.close("stream_1")

    assert not state_path.exists()
    assert registry.get_inbox("stream_1") is None


@pytest.mark.asyncio
async def test_registry_retains_original_turn_until_concurrent_cleanup_finishes(
    registered_turn: tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path],
) -> None:
    registry, scope, inbox, state_path = registered_turn
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("cleanup")
        started.set()
        await release.wait()

    scope.register_cleanup(cleanup)
    first = asyncio.create_task(registry.close("stream_1"))
    await started.wait()
    second = asyncio.create_task(registry.close("stream_1"))
    try:
        await asyncio.sleep(0)
        assert registry.get("stream_1") is scope
        assert registry.get_inbox("stream_1") is inbox
        assert registry.active_ids() == ("stream_1",)
        assert state_path.is_file()
        assert not first.done() and not second.done()
        with pytest.raises(RuntimeError, match="Turn 已存在执行 scope"):
            registry.create("stream_1")
    finally:
        release.set()
        await asyncio.gather(first, second)

    assert calls == ["cleanup"]
    assert registry.get("stream_1") is registry.get_inbox("stream_1") is None
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_registry_retries_only_failed_resources_on_original_turn(
    registered_turn: tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path],
) -> None:
    registry, scope, inbox, state_path = registered_turn
    calls: list[str] = []
    failure = RuntimeError("原资源释放失败")

    def fail_once() -> None:
        calls.append("retryable")
        if calls.count("retryable") == 1:
            raise failure

    scope.register_cleanup(fail_once)
    scope.register_cleanup(lambda: calls.append("released"))
    with pytest.raises(ExceptionGroup) as caught:
        await registry.close("stream_1")
    assert caught.value.exceptions[0].__cause__ is failure
    assert registry.get("stream_1") is scope
    assert registry.get_inbox("stream_1") is inbox
    assert state_path.is_file()
    assert scope.lifetime_scope.snapshot().resource_count == 1
    with pytest.raises(RuntimeError, match="Turn 已存在执行 scope"):
        registry.create("stream_1")

    await registry.close("stream_1")
    assert calls == ["released", "retryable", "retryable"]
    assert registry.active_ids() == ()
    assert registry.get_inbox("stream_1") is None
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_registry_keeps_cleanup_owned_when_close_observer_is_cancelled(
    registered_turn: tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path],
) -> None:
    registry, scope, inbox, state_path = registered_turn
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("cleanup")
        started.set()
        await release.wait()

    scope.register_cleanup(cleanup)
    observer = asyncio.create_task(registry.close("stream_1"))
    await started.wait()
    observer.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert registry.get("stream_1") is scope
        assert registry.get_inbox("stream_1") is inbox
        assert state_path.is_file()
        retry = asyncio.create_task(registry.close("stream_1"))
        await asyncio.sleep(0)
        assert not retry.done()
    finally:
        release.set()
        await registry.close("stream_1")
    await retry
    assert calls == ["cleanup"]
    assert scope.is_closed
    assert registry.active_ids() == ()
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_registry_preserves_turn_when_inbox_file_cannot_be_removed(
    registered_turn: tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, scope, inbox, state_path = registered_turn
    calls: list[str] = []
    scope.register_cleanup(lambda: calls.append("cleanup"))
    unlink = Path.unlink
    failure = PermissionError(13, "控制状态文件暂不可删除", str(state_path))

    def fail_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == state_path:
            raise failure
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(PermissionError) as caught:
            await registry.close("stream_1")
    assert caught.value is failure
    assert scope.is_closed
    assert registry.get("stream_1") is scope
    assert registry.get_inbox("stream_1") is inbox
    assert state_path.is_file()

    await registry.close("stream_1")
    assert calls == ["cleanup"]
    assert registry.active_ids() == ()
    assert registry.get_inbox("stream_1") is None
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_late_close_waiter_cannot_remove_replacement_turn_or_control_file(
    registered_turn: tuple[TurnExecutionScopeRegistry, TurnExecutionScope, AgentControlInbox, Path],
) -> None:
    registry, scope, _inbox, state_path = registered_turn
    started, release = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()

    async def close_and_replace() -> tuple[TurnExecutionScope, AgentControlInbox]:
        await registry.close("stream_1")
        replacement = registry.create("stream_1")
        inbox = await AgentControlInbox.open("stream_1", session_id="ses_1", state_path=state_path)
        await inbox.accept(command_id="new", kind="interrupt", idempotency_key="new")
        registry.register_inbox("stream_1", inbox)
        return replacement, inbox

    scope.register_cleanup(cleanup)
    first = asyncio.create_task(close_and_replace())
    await started.wait()
    late = asyncio.create_task(registry.close("stream_1"))
    await asyncio.sleep(0)
    release.set()
    (replacement, replacement_inbox), _ = await asyncio.gather(first, late)
    assert registry.get("stream_1") is replacement
    assert registry.get_inbox("stream_1") is replacement_inbox
    assert not replacement.is_closed
    assert [command.command_id for command in (await AgentControlInbox.open(
        "stream_1", session_id="ses_1", state_path=state_path,
    )).snapshot()] == ["new"]
    await registry.close("stream_1")
