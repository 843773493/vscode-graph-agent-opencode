"""控制文件 worker 结算、取消发布及关闭准入的原生 I/O 边界。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from app.core.turn_execution_scope import (
    AgentControlInbox,
    TurnControlCoordinator,
    TurnExecutionScope,
)

Phase = Literal["load", "accept", "mark", "close"]


@dataclass
class NativeGate:
    loop: asyncio.AbstractEventLoop
    loop_thread: int
    target: Path | None = None
    phase: Phase | None = None
    failure: OSError | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    finish: threading.Event = field(default_factory=threading.Event)
    threads: list[int] = field(default_factory=list)

    def block(self) -> None:
        self.threads.append(threading.get_ident())
        if len(self.threads) != 1:
            return
        self.loop.call_soon_threadsafe(self.entered.set)
        assert self.threads[-1] != self.loop_thread, "控制文件 I/O 仍在事件循环执行"
        assert self.finish.wait(5), "控制文件测试未归还原生 I/O 屏障"
        if self.failure is not None:
            raise self.failure


@pytest.fixture
async def native_gate(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[NativeGate]:
    gate = NativeGate(asyncio.get_running_loop(), threading.get_ident())
    read_text, replace, unlink = Path.read_text, Path.replace, Path.unlink

    def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == gate.target and gate.phase == "load":
            gate.block()
        return read_text(path, encoding=encoding, errors=errors)

    def write(path: Path, target: str | Path) -> Path:
        if gate.target is not None and path == gate.target.with_suffix(".tmp") and gate.phase in {"accept", "mark"}:
            gate.block()
        return replace(path, target)

    def discard(path: Path, *, missing_ok: bool = False) -> None:
        if path == gate.target and gate.phase == "close":
            gate.block()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "replace", write)
    monkeypatch.setattr(Path, "unlink", discard)
    try:
        yield gate
    finally:
        gate.finish.set()


@pytest.mark.parametrize("phase", ["load", "accept", "mark", "close"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "cancel-failure"])
async def test_control_io_keeps_original_commit_and_cancellation(
    tmp_path: Path, native_gate: NativeGate, phase: Phase, outcome: str,
) -> None:
    path = tmp_path / "control.json"
    inbox = await AgentControlInbox.open("stream", session_id="session", state_path=path)
    original = await inbox.accept(command_id="original", kind="interrupt", idempotency_key="original")
    assert await inbox.next() == original
    body = path.read_bytes()
    gate = native_gate
    gate.target, gate.phase = path, phase
    if "failure" in outcome:
        gate.failure = PermissionError(13, "控制文件原生失败", str(path))

    async def operate() -> object:
        if phase == "load":
            return await AgentControlInbox.open("stream", session_id="session", state_path=path)
        if phase == "accept":
            return await inbox.accept(command_id="new", kind="steer", idempotency_key="new")
        if phase == "mark":
            return await inbox.mark("original", "consumed")
        return await inbox.close()

    task = asyncio.create_task(operate())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        await asyncio.sleep(0)
        assert not task.done()
        assert len(gate.threads) == 1 and gate.threads[0] != threading.get_ident()
        assert path.read_bytes() == body
        assert inbox.snapshot() == [original]
        assert inbox._pending.empty()
        if "cancel" in outcome:
            task.cancel("原控制操作取消")
            await asyncio.sleep(0)
            task.cancel("重复取消")
            await asyncio.sleep(0)
            assert not task.done()
        gate.finish.set()
        if gate.failure is not None:
            with pytest.raises(OSError) as caught:
                await task
            assert caught.value is gate.failure
        elif "cancel" in outcome:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    finally:
        gate.finish.set()
        await asyncio.gather(task, return_exceptions=True)

    if gate.failure is not None:
        assert path.read_bytes() == body
        assert inbox.snapshot() == [original]
        assert inbox._pending.empty()
    elif phase == "accept":
        accepted = inbox.get("new")
        assert accepted is not None and accepted.control_seq == 2
        assert await inbox.next() == accepted
        assert inbox._pending.empty()
    elif phase == "mark":
        marked = inbox.get("original")
        assert marked is not None and marked.status == "consumed"
        assert inbox._pending.empty()
    if phase == "close":
        assert inbox.is_closed
        if gate.failure is None:
            assert not path.exists()
            path.write_text("后继文件")
            await inbox.close()
            assert path.read_bytes() == "后继文件".encode()
        else:
            await inbox.close()
            assert not path.exists()
        with pytest.raises(RuntimeError, match="inbox 已关闭"):
            await inbox.accept(command_id="late", kind="interrupt", idempotency_key="late")
        with pytest.raises(RuntimeError, match="inbox 已关闭"):
            await inbox.mark("original", "consumed")
    else:
        restored = await AgentControlInbox.open("stream", session_id="session", state_path=path)
        assert restored.snapshot() == inbox.snapshot()
        assert restored._pending.empty()


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_control_writes_keep_sequence_and_deduplication(
    tmp_path: Path, native_gate: NativeGate, same_key: bool,
) -> None:
    path = tmp_path / "control.json"
    inbox = await AgentControlInbox.open("stream", state_path=path)
    native_gate.target, native_gate.phase = path, "accept"
    first = asyncio.create_task(inbox.accept(command_id="first", kind="interrupt", idempotency_key="first"))
    await asyncio.wait_for(native_gate.entered.wait(), 2)
    second = asyncio.create_task(inbox.accept(
        command_id="second", kind="interrupt", idempotency_key="first" if same_key else "second",
    ))
    try:
        await asyncio.sleep(0)
        assert not second.done()
        assert inbox.snapshot() == []
    finally:
        native_gate.finish.set()
        accepted, duplicate = await asyncio.gather(first, second)
    assert accepted.control_seq == 1
    if same_key:
        assert duplicate == accepted
        assert inbox.snapshot() == [accepted]
    else:
        assert duplicate.control_seq == 2
        assert inbox.snapshot() == [accepted, duplicate]
    restored = await AgentControlInbox.open("stream", state_path=path)
    assert restored.snapshot() == inbox.snapshot()


async def test_close_serializes_with_interrupt_and_does_not_recreate_control_file(
    tmp_path: Path, native_gate: NativeGate,
) -> None:
    path = tmp_path / "control.json"
    inbox = await AgentControlInbox.open("stream", state_path=path)
    await inbox.accept(command_id="first", kind="steer", idempotency_key="first")
    scope, writer = TurnExecutionScope("stream"), AsyncMock()
    native_gate.target, native_gate.phase = path, "close"
    closing = asyncio.create_task(inbox.close())
    await asyncio.wait_for(native_gate.entered.wait(), 2)
    late = asyncio.create_task(TurnControlCoordinator(scope, inbox, writer).submit_interrupt(
        command_id="late", idempotency_key="late",
    ))
    try:
        await asyncio.sleep(0)
        assert not late.done()
    finally:
        native_gate.finish.set()
        await closing
    result = await late
    assert result["type"] == "interrupt.rejected" and result["reason"] == "turn_not_active"
    writer.commit.assert_not_awaited()
    assert not path.exists()
    assert inbox.get("late") is None
    await scope.close()
