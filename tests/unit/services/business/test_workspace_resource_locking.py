"""两个资源 owner 的锁隔离、过滤重检及迟到调用合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import CHILD_THREAD_A, SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    return (build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture)(tmp_path)


async def test_pending_stop_does_not_block_another_resource_or_thread_cleanup(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    first = await bundle.create()
    second = await bundle.create(thread=CHILD_THREAD_A)
    rid, other = str(first["resource_id"]), str(second["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.stop_resource
    calls = []

    async def blocked_stop(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        calls.append(resource_id)
        if resource_id == rid:
            entered.set()
            await release.wait()
        return await original(resource_id, reason=reason)

    monkeypatch.setattr(bundle.transport, "stop_resource", blocked_stop)
    stopping = asyncio.create_task(bundle.command(rid, action="stop", expected_revision=1))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        queued = asyncio.create_task(bundle.command(rid, action="promote", expected_revision=1))
        # 同资源命令仍串行；其它资源的创建、查询、授权、工具和释放独立完成。
        await asyncio.sleep(0)
        assert not queued.done()
        current = await asyncio.wait_for(owner.get_resource(other), 5)
        assert current["owner_revision"] == 1 and current["status"] == "running"
        third = await asyncio.wait_for(bundle.create(thread=CHILD_THREAD_A), 5)
        listed = await asyncio.wait_for(owner.list_resources(thread_id=CHILD_THREAD_A), 5)
        assert {item["resource_id"] for item in listed} == {other, third["resource_id"]}
        grant = await asyncio.wait_for(owner.transport_grant(
            other, expected_revision=1, thread=ThreadRef(SESSION_A, CHILD_THREAD_A),
            holder_id="request:independent-grant", operation_id="independent-grant",
        ), 5)
        assert grant["resource"]["resource_id"] == other
        async with owner.operation(session_id=SESSION_A, thread_id=CHILD_THREAD_A,
                                   holder_id="request:independent-tool", operation_id="independent-tool") as operation:
            if isinstance(bundle, TerminalOwnerFixture):
                await asyncio.wait_for(operation.write_terminal(terminal_id=other, data="independent"), 5)
            else:
                await asyncio.wait_for(operation.perform(other, "navigate", {"url": "https://independent.test"}), 5)
        await asyncio.wait_for(owner.release_thread(SESSION_A, CHILD_THREAD_A, 1), 5)
        assert set(bundle.transport.deleted) == {other, third["resource_id"]}
        assert calls == [rid] and not stopping.done() and not queued.done()
        assert owner._store.get(rid).status == "stopping"
        assert owner._store.get(rid).thread_associations == [ThreadRef(SESSION_A, THREAD_A)]
        assert len(bundle.leases.leases_for_resource(rid)) == 1
    finally:
        release.set()
        results = await asyncio.gather(stopping, *([queued] if queued is not None else []), return_exceptions=True)
    assert isinstance(results[0], dict) and results[0]["reconciliation_status"] == "verified"
    assert isinstance(results[1], ResourceConflict)
    assert calls == [rid] and not bundle.leases.leases_for_resource(rid)


async def test_filtered_list_rechecks_association_after_waiting_for_the_resource(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    resource = await bundle.create(scope="workspace", thread=CHILD_THREAD_A)
    rid = str(resource["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision

    async def blocked_revision(resource_id: str, owner_revision: int, *, for_stop: bool) -> None:
        entered.set()
        await release.wait()
        await original(resource_id, owner_revision, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", blocked_revision)
    detach = asyncio.create_task(bundle.command(rid, action="detach", expected_revision=1,
                                                thread=ThreadRef(SESSION_A, CHILD_THREAD_A)))
    listing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        listing = asyncio.create_task(owner.list_resources(thread_id=CHILD_THREAD_A))
        await asyncio.sleep(0)
        assert not listing.done()
    finally:
        release.set()
        results = await asyncio.gather(detach, *([listing] if listing is not None else []))
    assert results[0]["thread_associations"] == [] and results[1] == []


async def test_operation_close_rejects_late_calls_while_its_lease_waits_for_a_lock(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
) -> None:
    owner = bundle.owner
    assert owner is not None
    first, second = await bundle.create(), await bundle.create()
    rid, other = str(first["resource_id"]), str(second["resource_id"])
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:closing-tool", operation_id="closing-tool") as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.read_terminal(rid)
        else:
            await operation.perform(rid, "navigate", {"url": "https://before-close.test"})
        async with owner._resource_lock(rid):
            closing = asyncio.create_task(operation.close())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="已关闭"):
                if isinstance(bundle, TerminalOwnerFixture):
                    await operation.write_terminal(terminal_id=other, data="late")
                else:
                    await operation.perform(other, "navigate", {"url": "https://late.test"})
            assert len(bundle.leases.leases_for_resource(rid)) == 1
            assert not bundle.leases.leases_for_resource(other)
        await closing
    assert not bundle.leases.leases_for_resource(rid)


@pytest.mark.parametrize("failure_count", [1, 2])
async def test_operation_close_failure_keeps_exact_lease_for_retry_and_drains_other_resources(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    failure_count: int,
) -> None:
    import json

    owner = bundle.owner
    resources = sorted([str((await bundle.create())["resource_id"]) for _ in range(2)])
    rid, other = resources
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:close-failure", operation_id="close-failure") as operation:
        for resource_id in resources:
            if isinstance(bundle, TerminalOwnerFixture):
                await operation.read_terminal(resource_id)
            else:
                await operation.perform(resource_id, "navigate", {"url": "https://close-failure.test"})
        lease, = bundle.leases.leases_for_resource(rid)
        other_lease, = bundle.leases.leases_for_resource(other)
        ledger_path = bundle.leases._state_path
        original_replace = Path.replace
        errors = {item.lease_id: OSError(f"受控的 operation 释放写盘失败: {item.resource_id}")
                  for item in [lease, other_lease][:failure_count]}
        failures: list[OSError] = []

        def fail_release(path: Path, target: Path):
            if target == ledger_path:
                saved = json.loads(path.read_bytes())
                for item in saved["leases"]:
                    error = errors.get(item["lease_id"])
                    if error is not None and error not in failures and item["status"] == "released":
                        failures.append(error)
                        raise error
            return original_replace(path, target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_release)
            with pytest.raises(OSError if failure_count == 1 else ExceptionGroup) as caught:
                await operation.close()
        if failure_count == 1:
            assert caught.value is failures[0]
        else:
            assert caught.value.exceptions == tuple(failures) == tuple(errors.values())
        cold = type(bundle.leases)(state_path=ledger_path)
        for item in [lease, other_lease]:
            expected = "active" if item.lease_id in errors else "released"
            assert bundle.leases.get_lease(item.lease_id).status == expected
            assert cold.get_lease(item.lease_id).status == expected
        # 沿原 operation 重试关闭，不重新 acquire，也不把普通释放写成核实终态。
        await operation.close()
        await operation.close()
        assert bundle.leases.get_lease(lease.lease_id).status == "released"
        assert not any(bundle.leases.leases_for_resource(item) for item in resources)


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_operation_close_releases_unlocked_lease_while_another_resource_is_busy(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    cancel_waiter: bool,
) -> None:
    owner = bundle.owner
    resources = sorted([str((await bundle.create())["resource_id"]) for _ in range(2)])
    rid, other = resources
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:parallel-close", operation_id="parallel-close") as operation:
        for resource_id in resources:
            if isinstance(bundle, TerminalOwnerFixture):
                await operation.read_terminal(resource_id)
            else:
                await operation.perform(resource_id, "navigate", {"url": "https://parallel-close.test"})
        blocked, = bundle.leases.leases_for_resource(rid)
        healthy, = bundle.leases.leases_for_resource(other)
        released = asyncio.Event()
        original_release = bundle.leases.release

        def observe_release(lease_id: str):
            result = original_release(lease_id)
            if lease_id == healthy.lease_id:
                released.set()
            return result

        monkeypatch.setattr(bundle.leases, "release", observe_release)
        closing = None
        try:
            async with owner._resource_lock(rid):
                closing = asyncio.create_task(operation.close())
                await asyncio.wait_for(released.wait(), 2)
                assert not closing.done()
                assert bundle.leases.get_lease(blocked.lease_id).status == "active"
                assert bundle.leases.get_lease(healthy.lease_id).status == "released"
                if cancel_waiter:
                    closing.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await closing
                    assert bundle.leases.get_lease(blocked.lease_id).status == "active"
        finally:
            if closing is not None and not closing.cancelled():
                await closing
        await operation.close()
        assert not any(bundle.leases.leases_for_resource(item) for item in resources)


@pytest.mark.parametrize("failure_count", [1, 2])
async def test_cold_recovery_finishes_other_resources_without_rebinding_the_batch_after_failure(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, failure_count: int,
) -> None:
    resources = [await bundle.create() for _ in range(3)]
    ids = [str(resource["resource_id"]) for resource in resources]
    owner = bundle.restart()
    errors = {rid: ConnectionError(f"撤权回执丢失: {rid}") for rid in ids[:failure_count]}
    calls: list[tuple[str, int]] = []
    original = bundle.transport.sync_owner_revision

    async def lose_receipt(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision))
        # 外部版本已经发布；Python不能因丢失回执重启整批恢复。
        await original(resource_id, revision, for_stop=for_stop)
        if resource_id in errors:
            raise errors[resource_id]

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", lose_receipt)
    with pytest.raises(ConnectionError if failure_count == 1 else ExceptionGroup) as caught:
        await owner.recover()
    if failure_count == 1:
        assert caught.value is errors[ids[0]]
    else:
        assert isinstance(caught.value, ExceptionGroup)
        assert caught.value.exceptions == tuple(errors.values())
    assert calls == [(rid, 2) for rid in ids]

    # 健康资源可经原查询、授权和工具入口继续使用，版本不会反复推进。
    healthy = ids[-1]
    current = await owner.get_resource(healthy)
    assert current["owner_revision"] == 2
    assert current["reconciliation_status"] == "verified"
    grant = await owner.transport_grant(
        healthy, expected_revision=2, thread=ThreadRef(SESSION_A, THREAD_A),
        holder_id="request:recovered-grant", operation_id="recovered-grant",
    )
    assert grant["resource"]["resource_id"] == healthy
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="request:recovered-tool", operation_id="recovered-tool",
    ) as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.write_terminal(terminal_id=healthy, data="recovered")
        else:
            await operation.perform(healthy, "navigate", {"url": "https://recovered.test"})
    for rid in errors:
        unknown = await owner.get_resource(rid)
        assert unknown["owner_revision"] == 2
        assert unknown["reconciliation_status"] == "unknown"
        with pytest.raises(ResourceConflict, match="尚待 owner 核实"):
            await bundle.command(rid, action="promote", expected_revision=2)
    assert calls == [(rid, 2) for rid in ids]
    assert all(record.owner_revision == 2 for record in owner._store.all())
    assert not bundle.transport.deleted


async def test_cancelling_cold_recovery_does_not_report_the_remaining_resources_recovered(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create()
    ids = [str(first["resource_id"]), str(second["resource_id"])]
    owner = bundle.restart()
    entered = asyncio.Event()
    calls: list[str] = []

    async def blocked(resource_id: str, _revision: int, *, for_stop: bool) -> None:
        calls.append(resource_id)
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", blocked)
    recovering = asyncio.create_task(owner.recover())
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        recovering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovering
    assert calls == ids
    assert set(owner._recovery._pending) == set(ids)
    assert all(owner._store.get(rid).reconciliation_status == "unknown" for rid in ids)


async def test_pending_cold_recovery_does_not_block_healthy_resource_entrypoints(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create(thread=CHILD_THREAD_A)
    rid, healthy = str(first["resource_id"]), str(second["resource_id"])
    owner = bundle.restart()
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision
    calls: list[tuple[str, int]] = []

    async def pending_revision(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision))
        if resource_id == rid:
            entered.set()
            await release.wait()
        await original(resource_id, revision, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", pending_revision)
    recovering = asyncio.create_task(owner.recover())
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = await asyncio.wait_for(owner.get_resource(healthy), 2)
        assert current["owner_revision"] == 2 and current["reconciliation_status"] == "verified"
        waiting = asyncio.create_task(owner.get_resource(rid))
        await asyncio.sleep(0)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not recovering.done()
        created = await asyncio.wait_for(bundle.create(thread=CHILD_THREAD_A), 2)
        listed = await asyncio.wait_for(owner.list_resources(thread_id=CHILD_THREAD_A), 2)
        assert {item["resource_id"] for item in listed} == {healthy, created["resource_id"]}
        grant = await asyncio.wait_for(owner.transport_grant(
            healthy, expected_revision=2, thread=ThreadRef(SESSION_A, CHILD_THREAD_A),
            holder_id="request:cold-grant", operation_id="cold-grant",
        ), 2)
        assert grant["resource"]["resource_id"] == healthy
        async with owner.operation(
            session_id=SESSION_A, thread_id=CHILD_THREAD_A,
            holder_id="request:cold-tool", operation_id="cold-tool",
        ) as operation:
            if isinstance(bundle, TerminalOwnerFixture):
                await asyncio.wait_for(operation.write_terminal(terminal_id=healthy, data="cold-independent"), 2)
            else:
                await asyncio.wait_for(operation.perform(healthy, "navigate", {"url": "https://cold.test"}), 2)
        await asyncio.wait_for(owner.release_thread(SESSION_A, CHILD_THREAD_A, 1), 2)
        assert set(bundle.transport.deleted) == {healthy, created["resource_id"]}
        assert not recovering.done() and calls.count((rid, 2)) == calls.count((healthy, 2)) == 1
        assert owner._store.get(rid).thread_associations == [ThreadRef(SESSION_A, THREAD_A)]
    finally:
        release.set()
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await recovering
    assert (await owner.get_resource(rid))["owner_revision"] == 2
    assert calls.count((rid, 2)) == calls.count((healthy, 2)) == 1


async def test_cancelling_recovery_keeps_frozen_revision_and_does_not_repeat_finished_resources(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create(thread=CHILD_THREAD_A)
    rid, healthy = str(first["resource_id"]), str(second["resource_id"])
    owner = bundle.restart()
    entered, healthy_synced, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision
    calls: list[tuple[str, int]] = []

    async def pending_revision(resource_id: str, revision: int, *, for_stop: bool) -> None:
        calls.append((resource_id, revision))
        if resource_id == rid:
            entered.set()
            await release.wait()
        await original(resource_id, revision, for_stop=for_stop)
        if resource_id == healthy:
            healthy_synced.set()

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", pending_revision)
    recovering = asyncio.create_task(owner.recover())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(healthy_synced.wait(), 5)
    finally:
        recovering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovering
    assert owner._recovery._pending == {rid: 2}
    assert owner._store.get(rid).reconciliation_status == "unknown"
    healthy_current = await owner.get_resource(healthy)
    assert healthy_current["owner_revision"] == 2 and healthy_current["reconciliation_status"] == "verified"
    assert calls == [(rid, 2), (healthy, 2)]
    release.set()
    unknown = await owner.get_resource(rid)
    assert unknown["owner_revision"] == 2 and unknown["reconciliation_status"] == "unknown"
    assert calls == [(rid, 2), (healthy, 2), (rid, 2)]
    assert owner._recovery._pending == {}
    assert bundle.transport.creates == 2 and not bundle.transport.deleted


@pytest.fixture(params=["dump", "fsync", "replace"])
def fail_record_write(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """只破坏原 owner 文件边界，lease 账本仍走自己的真实持久链。"""
    import json
    from types import SimpleNamespace

    from app.services.business.workspace_resources import records as records_module

    selected = False
    fired: list[OSError] = []
    def predicate(_payload):
        return False
    original_replace = Path.replace
    original_fsync = records_module.os.fsync

    def fail(stage: str) -> None:
        if selected and not fired and request.param == stage:
            error = OSError(f"资源记录 {stage} 受控失败")
            fired.append(error)
            raise error

    def dump(payload, stream, **kwargs):
        nonlocal selected
        selected = predicate(payload)
        fail("dump")
        return json.dump(payload, stream, **kwargs)

    def fsync(fd: int) -> None:
        fail("fsync")
        original_fsync(fd)

    def replace(path: Path, target: Path):
        if path.name.endswith("-resources.tmp"):
            fail("replace")
        return original_replace(path, target)

    def arm(condition):
        nonlocal predicate
        predicate = condition
        return fired

    monkeypatch.setattr(records_module, "json", SimpleNamespace(loads=json.loads, dump=dump))
    monkeypatch.setattr(records_module, "os", SimpleNamespace(fsync=fsync))
    monkeypatch.setattr(Path, "replace", replace)
    return arm


async def test_failed_record_creation_does_not_publish_an_unstored_resource(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, fail_record_write,
) -> None:
    owner = bundle.owner
    await owner.recover(())
    failures = fail_record_write(lambda _payload: True)
    with pytest.raises(OSError) as caught:
        await bundle.create()
    assert caught.value is failures[0]
    assert owner._store.all() == ()
    assert not bundle.transport.records
    resource = await bundle.create()
    assert [item["resource_id"] for item in await owner.list_resources()] == [resource["resource_id"]]


async def test_failed_revision_write_does_not_advance_authority_or_transport(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, fail_record_write,
) -> None:
    import json

    resource = await bundle.create()
    owner, rid = bundle.owner, str(resource["resource_id"])
    failures = fail_record_write(lambda payload: payload["records"][0]["owner_revision"] == 2)
    with pytest.raises(OSError) as caught:
        await bundle.command(rid, action="promote", expected_revision=1)
    assert caught.value is failures[0]
    stored = json.loads(owner._store.path.read_bytes())["records"][0]
    assert stored["owner_revision"] == owner._store.get(rid).owner_revision == 1
    assert stored["retention_scope"] == "thread"
    assert bundle.transport.records[rid]["transport_owner_revision"] == 1
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.status == "reconcile_required"


async def test_failed_promotion_commit_retains_last_durable_scope(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, fail_record_write,
) -> None:
    import json

    resource = await bundle.create()
    owner, rid = bundle.owner, str(resource["resource_id"])
    failures = fail_record_write(lambda payload: payload["records"][0]["retention_scope"] == "workspace")
    with pytest.raises(OSError) as caught:
        await bundle.command(rid, action="promote", expected_revision=1)
    assert caught.value is failures[0]
    record = owner._store.get(rid)
    stored = json.loads(owner._store.path.read_bytes())["records"][0]
    assert record.retention_scope == stored["retention_scope"] == "thread"
    assert record.owner_revision == stored["owner_revision"] == 2
    assert record.reconciliation_status == "unknown"
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.status == "reconcile_required"


async def test_failed_cold_freeze_retries_the_original_version_once(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, fail_record_write,
) -> None:
    import json

    resource = await bundle.create()
    rid = str(resource["resource_id"])
    owner = bundle.restart()
    failures = fail_record_write(lambda payload: payload["records"][0]["owner_revision"] == 2)
    with pytest.raises(OSError) as caught:
        await owner.recover()
    assert caught.value is failures[0]
    assert owner._store.get(rid).owner_revision == 1
    assert json.loads(owner._store.path.read_bytes())["records"][0]["owner_revision"] == 1
    assert bundle.transport.records[rid]["transport_owner_revision"] == 1
    await owner.recover()
    assert (await owner.get_resource(rid))["owner_revision"] == 2
    assert bundle.transport.records[rid]["transport_owner_revision"] == 2


async def test_failed_fork_commit_never_publishes_target_or_committed_receipt(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, fail_record_write,
) -> None:
    resource = await bundle.create(scope="workspace")
    owner, rid = bundle.owner, str(resource["resource_id"])
    target = ThreadRef(SESSION_A, CHILD_THREAD_A)
    failures = fail_record_write(lambda payload: any(
        receipt["state"] == "committed"
        for record in payload["records"] for receipt in record["fork_attachments"].values()
    ))
    snapshot, receipt = await owner.attach_for_fork(
        rid, creation_key="failed-commit", expected_revision=1, thread=target,
    )
    assert len(failures) == 1
    assert receipt.state == "dispatching" and receipt.committed_revision is None
    assert snapshot["reconciliation_status"] == "unknown"
    assert target not in owner._store.get(rid).thread_associations
    saved_receipt = owner.fork_attach_receipt(rid, creation_key="failed-commit", expected_revision=1, thread=target)
    assert saved_receipt == receipt
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.lease_id == receipt.lease_id and lease.status == "reconcile_required"
    again, repeated = await owner.attach_for_fork(rid, creation_key="failed-commit", expected_revision=1, thread=target)
    assert repeated == receipt and again["thread_associations"] == snapshot["thread_associations"]
    assert again["owner_revision"] == snapshot["owner_revision"] == 2


async def test_failed_unknown_write_keeps_lease_and_last_published_record(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    owner, rid = bundle.owner, str(resource["resource_id"])
    original_replace = Path.replace

    def fail_terminal_record(path: Path, target: Path):
        import json

        if target == owner._store.path:
            saved = json.loads(path.read_bytes())["records"][0]
            if saved["reconciliation_status"] == "unknown" or saved["status"] in {"terminated", "closed"}:
                raise OSError("终态和 unknown 记录写盘均失败")
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_terminal_record)
        with pytest.raises(OSError, match="均失败"):
            await bundle.command(rid, action="stop", expected_revision=1)
    record = owner._store.get(rid)
    assert record.status == "stopping" and record.owner_revision == 2
    assert record.reconciliation_status == "verified"
    lease, = bundle.leases.leases_for_resource(rid)
    assert lease.status == "reconcile_required"
    assert bundle.transport.records[rid]["status"] in {"terminated", "closed"}
    reconciled = await bundle.command(rid, action="stop", expected_revision=2)
    assert reconciled["reconciliation_status"] == "verified"
    assert bundle.leases.get_lease(lease.lease_id).status == "settled"


async def test_record_snapshots_do_not_share_mutable_associations_or_receipts(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
) -> None:
    resource = await bundle.create(scope="workspace")
    owner, rid = bundle.owner, str(resource["resource_id"])
    await owner.attach_for_fork(rid, creation_key="snapshot", expected_revision=1, thread=ThreadRef(SESSION_A, CHILD_THREAD_A))
    record = owner._store.get(rid)
    owner._store.save(record)
    expected = owner._store.path.read_bytes()
    for snapshot in (record, owner._store.get(rid), owner._store.all()[0]):
        snapshot.thread_associations.clear()
        snapshot.fork_attachments["snapshot"].state = "rejected"
    current = owner._store.get(rid)
    assert current.thread_associations == [ThreadRef(SESSION_A, THREAD_A), ThreadRef(SESSION_A, CHILD_THREAD_A)]
    assert current.fork_attachments["snapshot"].state == "committed"
    assert owner._store.path.read_bytes() == expected


async def test_unstored_unknown_cannot_be_cleared_by_a_live_snapshot(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    owner, rid = bundle.owner, str(resource["resource_id"])
    original_replace = Path.replace

    def fail_owner_file(path: Path, target: Path):
        if target == owner._store.path:
            raise OSError("owner 观察与 unknown 均不能写盘")
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_owner_file)
        with pytest.raises(OSError, match="均不能写盘"):
            async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                                       holder_id="request:unstored-unknown", operation_id="unstored-unknown") as operation:
                if isinstance(bundle, TerminalOwnerFixture):
                    await operation.write_terminal(terminal_id=rid, data="already-written")
                else:
                    await operation.perform(rid, "navigate", {"url": "https://already-navigated.test"})
    pending, = bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"
    observed = await owner.get_resource(rid)
    assert observed["status"] == "running" and observed["reconciliation_status"] == "reconcile_required"
    assert "stop" in observed["available_actions"] and "attach" not in observed["available_actions"]
    with pytest.raises(ResourceConflict, match="尚待 owner 核实"):
        async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                                   holder_id="request:blocked-tool", operation_id="blocked-tool") as operation:
            if isinstance(bundle, TerminalOwnerFixture):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "navigate", {"url": "https://must-not-navigate.test"})
    stopped = await owner.command(rid, action="stop", expected_revision=observed["owner_revision"],
                                  holder_id="request:unstored-reconcile", operation_id="unstored-reconcile")
    assert stopped["reconciliation_status"] == "verified"
    assert bundle.leases.get_lease(pending.lease_id).status == "settled"
