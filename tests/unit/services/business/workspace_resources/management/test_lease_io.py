"""管理lease原生获取和释放持锁结算，取消不迟到派发或遗失占用。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.abstractions.workspace_resources import ResourceSnapshot
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import (
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
)
from tests.support.resource_owners.terminal import build_terminal_owner_fixture
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@dataclass
class ManagementCase:
    bundle: OwnerFixture
    entry: str
    resource_id: str
    calls: list[str] = field(default_factory=list)

    @property
    def thread(self) -> ThreadRef:
        return ThreadRef(SESSION_B, THREAD_B) if self.entry == "command" else ThreadRef(SESSION_A, THREAD_A)

    async def run(self) -> ResourceSnapshot:
        owner = self.bundle.owner
        assert owner is not None
        arguments = {"holder_id": "request:management-io", "operation_id": "management-io"}
        if self.entry == "command":
            return await owner.command(self.resource_id, action="attach", expected_revision=1, thread=self.thread, **arguments)
        if self.entry == "grant":
            return await owner.transport_grant(self.resource_id, expected_revision=1, thread=self.thread, **arguments)
        assert isinstance(self.bundle, BrowserOwnerFixture) and self.bundle.owner is not None
        return await self.bundle.owner.perform(
            self.resource_id, action="navigate", payload={"url": "https://management.test"},
            expected_revision=1, thread=self.thread, **arguments,
        )


@pytest.fixture(params=["terminal-command", "terminal-grant", "browser-command", "browser-grant", "browser-action"])
async def management_case(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManagementCase:
    kind, entry = request.param.split("-")
    bundle = (build_terminal_owner_fixture if kind == "terminal" else build_browser_owner_fixture)(tmp_path)
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    case = ManagementCase(bundle, entry, rid)
    method = {"command": "sync_owner_revision", "grant": "issue_transport_grant", "action": "perform"}[entry]
    call = getattr(bundle.transport, method)

    async def dispatched(*args, **kwargs):
        case.calls.append(str(args[0]))
        return await call(*args, **kwargs)

    monkeypatch.setattr(bundle.transport, method, dispatched)
    return case


@dataclass
class LeaseWrite:
    phase: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    free: Event = field(default_factory=Event)
    failure: OSError | None = None
    lease: ResourceLease | None = None
    writers: list[int] = field(default_factory=list)


@pytest.fixture
async def lease_write(
    management_case: ManagementCase, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LeaseWrite]:
    gate = LeaseWrite(request.param)
    path = management_case.bundle.leases._state_path
    assert path is not None
    publishing, loop = Path.replace, asyncio.get_running_loop()

    def publish(source: Path, target: Path) -> Path:
        if target == path and gate.lease is None:
            leases = json.loads(source.read_text())["leases"]
            candidates = [lease for lease in leases if lease["operation_id"] == "management-io"
                          and lease["status"] == ("active" if gate.phase == "acquire" else "released")]
            if candidates:
                candidate, = candidates
                gate.lease = ResourceLease(**candidate)
                gate.writers.append(get_ident())
                loop.call_soon_threadsafe(gate.entered.set)
                assert gate.free.wait(5), "测试未释放原管理lease写入"
                if gate.failure is not None:
                    raise gate.failure
        return publishing(source, target)

    monkeypatch.setattr(Path, "replace", publish)
    try:
        yield gate
    finally:
        gate.free.set()


@pytest.mark.parametrize("lease_write", ["acquire", "release"], indirect=True)
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_original_management_lease_write_settles_before_unlock_or_holder_release(
    management_case: ManagementCase, lease_write: LeaseWrite, cancel: bool, write_fails: bool,
) -> None:
    case, gate = management_case, lease_write
    owner, path = case.bundle.owner, case.bundle.leases._state_path
    assert owner is not None and path is not None
    before = owner._store.path.read_bytes()
    gate.failure = OSError(28, "原管理lease写盘失败") if write_fails else None
    exclusive_entered = asyncio.Event()

    async def deleting() -> None:
        async with owner.deletion_holder.lock_for(case.thread.session_id, exclusive=True):
            exclusive_entered.set()

    pending = asyncio.create_task(case.run())
    deletion: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert len(gate.writers) == 1 and gate.writers[0] != get_ident()
        assert gate.lease is not None and owner._resource_lock(case.resource_id).locked() and not pending.done()
        lease = gate.lease
        assert (lease.resource_id, lease.turn_stream_id, lease.operation_id, lease.holder_session_id,
                lease.holder_thread_id) == (case.resource_id, "request:management-io", "management-io",
                                           case.thread.session_id, case.thread.thread_id)
        native_record = owner._store.path.read_bytes()
        deletion = asyncio.create_task(deleting())
        await asyncio.sleep(0)
        assert not deletion.done() and not exclusive_entered.is_set()
        if cancel:
            pending.cancel("原管理lease写盘取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消不能遗弃管理lease")
            await asyncio.sleep(0)
            assert not pending.done() and owner._resource_lock(case.resource_id).locked()
            assert not exclusive_entered.is_set()
        gate.free.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(pending, 2)
            assert caught.value.args == ("原管理lease写盘取消",)
            if write_fails:
                assert caught.value.__cause__ is not None and caught.value.__cause__.__cause__ is gate.failure
        elif write_fails:
            with pytest.raises(OSError) as caught_io:
                await asyncio.wait_for(pending, 2)
            assert caught_io.value.__cause__ is gate.failure
        else:
            await asyncio.wait_for(pending, 2)
        await asyncio.wait_for(deletion, 2)
        assert exclusive_entered.is_set() and not owner._resource_lock(case.resource_id).locked()
        saved = type(case.bundle.leases)(state_path=path).get_lease(lease.lease_id)
        if gate.phase == "acquire" and (cancel or write_fails):
            assert case.calls == [] and owner._store.path.read_bytes() == before
            if write_fails:
                assert saved is None
            else:
                assert saved is not None and saved.status == "released"
        else:
            assert case.calls == [case.resource_id]
            assert saved is not None and saved.status == ("active" if write_fails else "released")
            if gate.phase == "release":
                assert owner._store.path.read_bytes() == native_record
            if write_fails:
                # 完整回执已提交；只沿原账本收尾，不能重发业务动作。
                finished = case.bundle.leases.release_turn_leases(lease.turn_stream_id)
                assert len(finished) == 1 and finished[0].lease_id == lease.lease_id
                assert case.calls == [case.resource_id]
    finally:
        gate.free.set()
        await asyncio.gather(pending, *([] if deletion is None else [deletion]), return_exceptions=True)


@pytest.mark.parametrize("management_case", ["terminal-grant", "browser-grant"], indirect=True)
@pytest.mark.parametrize("lease_write", ["release"], indirect=True)
@pytest.mark.parametrize("write_fails", [False, True])
async def test_error_cleanup_keeps_original_conflict_despite_repeated_cancellation(
    management_case: ManagementCase, lease_write: LeaseWrite,
    monkeypatch: pytest.MonkeyPatch, write_fails: bool,
) -> None:
    case, gate = management_case, lease_write
    owner, path = case.bundle.owner, case.bundle.leases._state_path
    assert owner is not None and path is not None
    gate.failure = OSError(28, "冲突回执后的原lease释放失败") if write_fails else None
    get = case.bundle.transport.get_resource

    async def stopped(resource_id: str) -> ResourceSnapshot:
        snapshot = await get(resource_id)
        snapshot["status"] = "closed" if isinstance(case.bundle, BrowserOwnerFixture) else "terminated"
        case.bundle.transport.records[resource_id] = snapshot
        return snapshot

    monkeypatch.setattr(case.bundle.transport, "get_resource", stopped)
    pending = asyncio.create_task(case.run())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert owner._resource_lock(case.resource_id).locked() and not pending.done()
        pending.cancel("冲突已返回但原释放仍在途")
        await asyncio.sleep(0)
        pending.cancel("重复取消不得覆盖原冲突")
        await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(case.resource_id).locked()
        gate.free.set()
        with pytest.raises(ResourceConflict) as caught:
            await asyncio.wait_for(pending, 2)
        assert str(caught.value) == "外部资源 运行状态或传输授权版本已改变，请刷新资源"
        if write_fails:
            assert caught.value.__cause__ is not None and caught.value.__cause__.__cause__ is gate.failure
        else:
            assert caught.value.__cause__ is None
        assert gate.lease is not None and case.calls == []
        lease = type(case.bundle.leases)(state_path=path).get_lease(gate.lease.lease_id)
        assert lease is not None and lease.status == ("active" if write_fails else "released")
        record = owner._store.get(case.resource_id)
        assert record is not None and record.reconciliation_status == "verified"
        assert not owner._resource_lock(case.resource_id).locked()
    finally:
        gate.free.set()
        await asyncio.gather(pending, return_exceptions=True)
