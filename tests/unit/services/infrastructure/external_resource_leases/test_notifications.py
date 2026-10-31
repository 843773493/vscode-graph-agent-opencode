"""终态写盘不阻塞循环，原循环通知严格晚于持久发布且不被取消遗弃。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.services.infrastructure.events.channel_events import (
    ResourceStateEvent,
    ResourceStateEventPublisher,
)
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
    ResourceLease,
)


@dataclass
class LedgerCase:
    owner: ExternalResourceLeaseLedger
    path: Path
    events: EventChannelService
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    free: Event = field(default_factory=Event)
    failure: OSError | None = None
    writers: list[int] = field(default_factory=list)
    deliveries: list[tuple[int, str, str]] = field(default_factory=list)


@pytest.fixture
async def ledger_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LedgerCase]:
    events, path = EventChannelService(), tmp_path / "resources.json"
    ledger = ExternalResourceLeaseLedger(state_path=path, state_events=ResourceStateEventPublisher(
        event_service=events, owner_domain="external_resource_leases",
    ))
    for rid in ("a", "b"):
        ledger.register_external(resource_id=rid, kind="terminal", lifetime_scope="workspace")
        ledger.acquire_operation(resource_id=rid, turn_stream_id=f"holder-{rid}", operation_id=f"op-{rid}", lease_id=rid)
    case = LedgerCase(ledger, path, events)
    loop, replace = asyncio.get_running_loop(), Path.replace
    channel = events.channel("resource.state/external_resource_leases")
    publish = channel.publish
    debug = loop.get_debug()
    loop.set_debug(True)

    def publishing(event: ResourceStateEvent) -> object:
        # 同时核对通知线程和原件状态，不能在worker直接唤醒asyncio.Queue。
        cold = ExternalResourceLeaseLedger(state_path=path)
        lease = cold.get_lease(event.resource_id)
        assert lease is not None
        case.deliveries.append((get_ident(), event.resource_id, lease.status))
        return publish(event)

    def write(source: Path, target: Path) -> Path:
        if target == path and not case.writers:
            case.writers.append(get_ident())
            loop.call_soon_threadsafe(case.entered.set)
            assert case.free.wait(5), "测试未放行原终态提交"
            if case.failure is not None:
                raise case.failure
        return replace(source, target)

    monkeypatch.setattr(channel, "publish", publishing)
    monkeypatch.setattr(Path, "replace", write)
    try:
        yield case
    finally:
        case.free.set()
        loop.set_debug(debug)


@pytest.mark.parametrize("entry", ["settle", "reconcile"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_terminal_commit_and_notification_settle_before_cancellation_returns(
    ledger_case: LedgerCase, entry: str, cancel: bool, write_fails: bool,
) -> None:
    case = ledger_case
    owner = case.owner
    subscription = case.events.channel("resource.state/external_resource_leases").subscribe(label="waiting")
    waiting = asyncio.create_task(subscription.next())
    await asyncio.sleep(0)
    before = case.path.read_bytes()
    original = owner.get_lease("a")
    case.failure = OSError(28, "原终态写盘失败") if write_fails else None

    async def run() -> None:
        if entry == "settle":
            result = await owner.settle("a")
            assert result.lease_id == "a" and result.status == "settled"
        else:
            records = await owner.reconcile({"a": "stopped", "b": "running"}, kind="terminal")
            assert [(record.resource_id, record.status) for record in records] == [("a", "stopped"), ("b", "recovered")]

    pending = asyncio.create_task(run())
    try:
        await asyncio.wait_for(case.entered.wait(), 2)
        assert len(case.writers) == 1 and case.writers[0] != get_ident()
        assert not pending.done() and not waiting.done() and case.deliveries == []
        assert owner.get_lease("a") == original
        assert case.path.read_bytes() == before
        if cancel:
            pending.cancel("原终态提交取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消不能遗弃通知")
            await asyncio.sleep(0)
            assert not pending.done()
        case.free.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="原终态提交取消") as caught:
                await pending
            if write_fails:
                assert isinstance(caught.value.__cause__, OSError)
                assert caught.value.__cause__.__cause__ is case.failure
        elif write_fails:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value.__cause__ is case.failure
        else:
            await pending
        if write_fails:
            assert owner.get_lease("a") == original and case.path.read_bytes() == before
            assert not waiting.done() and case.deliveries == []
            case.failure = None
            await run()
        delivery = await asyncio.wait_for(waiting, 2)
        assert (delivery.sequence, delivery.event.resource_id, delivery.event.state) == (1, "a", "released")
        expected = "settled" if entry == "settle" else "reconcile_required"
        assert case.deliveries == ([(get_ident(), "a", expected)] if entry == "settle" else [
            (get_ident(), "a", expected), (get_ident(), "b", expected),
        ])
        lease = owner.get_lease("a")
        assert lease is not None and lease.status == expected
        assert ExternalResourceLeaseLedger(state_path=case.path).get_lease("a") == lease
    finally:
        case.free.set()
        await asyncio.gather(pending, return_exceptions=True)
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_competing_terminal_commits_publish_in_durable_order(ledger_case: LedgerCase) -> None:
    case = ledger_case
    subscription = case.events.channel("resource.state/external_resource_leases").subscribe(label="ordered")
    first = asyncio.create_task(case.owner.settle("a"))
    second: asyncio.Task[ResourceLease] | None = None
    try:
        await asyncio.wait_for(case.entered.wait(), 2)
        second = asyncio.create_task(case.owner.settle("b"))
        await asyncio.sleep(0)
        assert case.deliveries == []
        case.free.set()
        await asyncio.gather(first, second)
        assert [(event.sequence, event.event.resource_id) for event in subscription.pending()] == [(1, "a"), (2, "b")]
        assert case.deliveries == [(get_ident(), "a", "settled"), (get_ident(), "b", "settled")]
    finally:
        case.free.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_unexpected_notification_error_returns_to_original_caller_after_commit(
    ledger_case: LedgerCase, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    case = ledger_case
    channel = case.events.channel("resource.state/external_resource_leases")
    subscription = channel.subscribe(label="notification-error")
    original = OSError(5, "原通知回调错误")

    def fail(_event: ResourceStateEvent) -> None:
        raise original

    monkeypatch.setattr(channel, "publish", fail)
    pending = asyncio.create_task(case.owner.settle("a"))
    try:
        await asyncio.wait_for(case.entered.wait(), 2)
        if cancel:
            pending.cancel("原通知等待取消")
            await asyncio.sleep(0)
            pending.cancel("后续取消")
        case.free.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="原通知等待取消") as caught:
                await pending
            assert caught.value.__cause__ is original
        else:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value is original
        lease = ExternalResourceLeaseLedger(state_path=case.path).get_lease("a")
        assert lease is not None and lease.status == "settled"
        assert subscription.pending() == ()
    finally:
        case.free.set()
        await asyncio.gather(pending, return_exceptions=True)
