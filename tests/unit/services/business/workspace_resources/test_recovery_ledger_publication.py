"""原版本同步成功后，取消必须等同一账本发布及pending结算。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.recovery import RecoveryVersion
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    ReleaseWrite,
)


@dataclass
class ColdPublication:
    owner: WorkspaceResourceOwner
    bundle: TerminalOwnerFixture | BrowserOwnerFixture
    rid: str
    revision: int
    gate: ReleaseWrite
    calls: list[tuple[str, int]]


@pytest.fixture
async def cold_publication(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ColdPublication]:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.restart()
    await owner.recover(())
    assert owner._recovery._pending is not None
    revision = owner._recovery._pending[rid].revision
    assert revision is not None
    calls: list[tuple[str, int]] = []
    sync = bundle.transport.sync_owner_revision

    async def synced(resource_id: str, version: int, *, for_stop: bool) -> None:
        calls.append((resource_id, version))
        await sync(resource_id, version, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", synced)
    path = bundle.leases._state_path
    assert path is not None
    gate, loop, replace = ReleaseWrite(), asyncio.get_running_loop(), Path.replace

    def publishing(source: Path, target: Path) -> Path:
        if target == path and not gate.entered.is_set():
            loop.call_soon_threadsafe(gate.entered.set)
            if not gate.release.wait(3):
                raise TimeoutError("测试未放行原恢复账本写入")
            if gate.failure is not None:
                raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publishing)
    try:
        yield ColdPublication(owner, bundle, rid, revision, gate, calls)
    finally:
        gate.release.set()


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_cold_recovery_settles_original_publication_before_cancelling(
    cold_publication: ColdPublication, advance_clock: Callable[[float], None],
    ending: str, write_fails: bool,
) -> None:
    case = cold_publication
    owner, bundle, rid, gate = case.owner, case.bundle, case.rid, case.gate
    path = bundle.leases._state_path
    assert path is not None
    before = path.read_bytes()
    gate.failure = OSError(28, "原冷恢复账本发布失败", str(path)) if write_fails else None
    pending = asyncio.create_task(owner.recover((rid,)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原恢复发布必须允许事件循环处理取消"
        assert case.calls == [(rid, case.revision)] and owner._resource_lock(rid).locked()
        assert owner._recovery._pending == {rid: RecoveryVersion(case.revision)}
        assert path.read_bytes() == before
        if ending == "cancel":
            pending.cancel("原恢复发布取消")
        else:
            advance_clock(31)
        for _ in range(5):
            await asyncio.sleep(0)
        if ending == "cancel":
            pending.cancel("重复取消不能遗弃原恢复提交")
            await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else TimeoutError) as caught:
            await pending
        if ending == "cancel":
            cancellation = caught.value
            assert cancellation.args == ("原恢复发布取消",)
        else:
            budget = caught.value.__cause__
            assert isinstance(budget, TimeoutError)
            cancellation = budget.__cause__
        assert isinstance(cancellation, asyncio.CancelledError)
        if write_fails:
            failure = cancellation.__cause__
            assert isinstance(failure, OSError) and failure.__cause__ is gate.failure
            assert owner._recovery._pending == {rid: RecoveryVersion(case.revision)}
            assert path.read_bytes() == before
        else:
            assert owner._recovery._pending == {}
        assert not owner._resource_lock(rid).locked()
        await owner.recover((rid,))
        assert owner._recovery._pending == {}
        assert case.calls == [(rid, case.revision)] * (2 if write_fails else 1)
        record = owner._store.get(rid)
        external = type(bundle.leases)(state_path=path).get(rid)
        assert record is not None and record.owner_revision == case.revision
        assert record.reconciliation_status == "verified"
        assert external is not None and external.status == "running"
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)
