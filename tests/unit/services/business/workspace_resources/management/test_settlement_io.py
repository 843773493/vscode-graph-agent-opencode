"""原停止入口持锁等待旧占用终态写盘，取消和迟到错误不提前解锁。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_stop_keeps_original_lock_until_old_lease_settlement_finishes(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch, cancel: bool, write_fails: bool,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner, ledger = bundle.owner, bundle.leases
    assert owner is not None and ledger._state_path is not None
    grant = bundle.transport.issue_transport_grant

    async def lost(resource_id: str, *, owner_revision: int, association: str | None) -> dict[str, object]:
        await grant(resource_id, owner_revision=owner_revision, association=association)
        raise ConnectionError("原授权回执丢失")

    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, "issue_transport_grant", lost)
        with pytest.raises(ConnectionError, match="原授权回执丢失"):
            await owner.transport_grant(
                rid, expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A),
                holder_id="request:lost-grant", operation_id="lost-grant",
            )
    pending_lease, = ledger.leases_for_resource(rid)
    assert pending_lease.status == "reconcile_required"
    path, loop = ledger._state_path, asyncio.get_running_loop()
    entered, released, contender_entered = asyncio.Event(), Event(), asyncio.Event()
    replace = Path.replace
    writers: list[int] = []
    native = OSError(28, "原旧占用结清写入失败")

    def write(source: Path, target: Path) -> Path:
        if target == path and not writers:
            rows = json.loads(source.read_text())["leases"]
            if any(row["lease_id"] == pending_lease.lease_id and row["status"] == "settled" for row in rows):
                writers.append(get_ident())
                loop.call_soon_threadsafe(entered.set)
                assert released.wait(5), "测试未放行原结清写入"
                if write_fails:
                    raise native
        return replace(source, target)

    async def contending() -> None:
        async with owner._resource_lock(rid):
            contender_entered.set()

    monkeypatch.setattr(Path, "replace", write)
    stopping = asyncio.create_task(owner.command(
        rid, action="stop", expected_revision=1, holder_id="request:stop-io", operation_id="stop-io",
    ))
    contender: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(writers) == 1 and writers[0] != get_ident()
        assert owner._resource_lock(rid).locked() and not stopping.done()
        assert ExternalResourceLeaseLedger(state_path=path).get_lease(pending_lease.lease_id) == pending_lease
        contender = asyncio.create_task(contending())
        await asyncio.sleep(0)
        assert not contender_entered.is_set()
        if cancel:
            stopping.cancel("原停止结清取消")
            await asyncio.sleep(0)
            stopping.cancel("再次取消不得释放原锁")
            await asyncio.sleep(0)
            assert not stopping.done() and owner._resource_lock(rid).locked()
            assert not contender_entered.is_set()
        released.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="原停止结清取消") as caught:
                await stopping
            if write_fails:
                assert isinstance(caught.value.__cause__, OSError)
                assert caught.value.__cause__.__cause__ is native
        elif write_fails:
            with pytest.raises(OSError) as caught:
                await stopping
            assert caught.value.__cause__ is native
        else:
            result = await stopping
            assert result["reconciliation_status"] == "verified"
        await asyncio.wait_for(contender, 2)
        assert contender_entered.is_set() and not owner._resource_lock(rid).locked()
        retained = ExternalResourceLeaseLedger(state_path=path).get_lease(pending_lease.lease_id)
        assert retained is not None and retained.status == ("reconcile_required" if write_fails else "settled")
        assert (retained.resource_id, retained.turn_stream_id, retained.operation_id) == (
            rid, pending_lease.turn_stream_id, pending_lease.operation_id,
        )
    finally:
        released.set()
        await asyncio.gather(stopping, return_exceptions=True)
        if contender is not None:
            await asyncio.gather(contender, return_exceptions=True)
