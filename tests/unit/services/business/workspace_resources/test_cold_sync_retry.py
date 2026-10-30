"""冷恢复同步失败后，只有显式管理停止重试原冻结版本。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    factory = build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture
    return factory(tmp_path)


@pytest.mark.parametrize("action", ["stop", "delete"])
@pytest.mark.parametrize("accepted", [False, True])
async def test_failed_sync_remains_manageable_without_replaying_an_unknown_operation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    action: str, accepted: bool,
) -> None:
    created, peer = await bundle.create("workspace"), await bundle.create("workspace")
    rid, peer_id = str(created["resource_id"]), str(peer["resource_id"])
    owner = bundle.owner
    assert owner is not None
    record = owner._store.get(rid)
    assert record is not None
    detached = await bundle.command(rid, action="detach", expected_revision=1, thread=record.source_thread)
    revision = detached["owner_revision"]
    assert isinstance(revision, int)
    frozen = revision + 1
    operation_failure = ConnectionError("原授权回执丢失")
    grant = bundle.transport.issue_transport_grant

    async def lose_operation(resource_id: str, *, owner_revision: int, association: str | None) -> dict[str, object]:
        await grant(resource_id, owner_revision=owner_revision, association=association)
        raise operation_failure

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "issue_transport_grant", lose_operation)
        # workspace管理授权不要求Thread关联；待核实lease由原正式入口生成。
        with pytest.raises(ConnectionError) as caught:
            await owner.transport_grant(rid, expected_revision=revision,
                                        holder_id="request:unknown-grant", operation_id="unknown-grant")
        assert caught.value is operation_failure
    original_lease, = bundle.leases.leases_for_resource(rid)
    assert original_lease.status == "reconcile_required"

    owner = bundle.restart()
    sync, stop, delete = bundle.transport.sync_owner_revision, bundle.transport.stop_resource, bundle.transport.delete_resource
    failure = ConnectionError("冷恢复授权同步未确认")
    calls: list[tuple[str, int, bool]] = []
    effects: list[tuple[str, str]] = []
    failures = 2

    async def syncing(resource_id: str, version: int, *, for_stop: bool) -> None:
        nonlocal failures
        calls.append((resource_id, version, for_stop))
        if resource_id == rid and failures:
            failures -= 1
            if accepted:
                await sync(resource_id, version, for_stop=for_stop)
            raise failure
        await sync(resource_id, version, for_stop=for_stop)

    async def stopping(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        effects.append((resource_id, "stop"))
        return await stop(resource_id, reason=reason)

    async def deleting(resource_id: str) -> dict[str, object]:
        effects.append((resource_id, "delete"))
        return await delete(resource_id)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    monkeypatch.setattr(bundle.transport, "stop_resource", stopping)
    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    with pytest.raises(ConnectionError) as caught:
        await owner.recover()
    assert caught.value is failure
    initial_calls = list(calls)
    assert initial_calls == [(rid, frozen, False), (peer_id, 2, False)]

    # 失败后的普通查询仍可发现unknown，不重发同步，也不影响健康资源。
    for resource_id, expected in ((rid, "unknown"), (peer_id, "verified")):
        assert (await owner.get_resource(resource_id))["reconciliation_status"] == expected
    assert calls == initial_calls
    with pytest.raises(ResourceConflict, match="版本冲突"):
        await owner.command(rid, action=action, expected_revision=revision,
                            holder_id="request:stale", operation_id="stale")
    assert calls == initial_calls and effects == []

    # 首次显式重试仍失败时，不能丢弃冻结版本或创建新的管理占用。
    with pytest.raises(ConnectionError) as caught:
        await owner.command(rid, action=action, expected_revision=frozen,
                            holder_id="request:first-stop", operation_id="first-stop")
    assert caught.value is failure
    assert calls == initial_calls + [(rid, frozen, True)]
    assert bundle.leases.leases_for_resource(rid) == [original_lease]
    assert effects == []
    assert (await owner.get_resource(rid))["reconciliation_status"] == "unknown"
    assert calls == initial_calls + [(rid, frozen, True)]

    result = await owner.command(rid, action=action, expected_revision=frozen,
                                 holder_id="request:verified-stop", operation_id="verified-stop")
    assert result["reconciliation_status"] == "verified"
    assert result["status"] == (
        "deleted" if action == "delete" else "terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed"
    )
    assert result["owner_revision"] == frozen + 1
    assert calls == initial_calls + [(rid, frozen, True), (rid, frozen, True), (rid, frozen + 1, True)]
    assert effects == [(rid, action)]
    assert not bundle.leases.leases_for_resource(rid)
    settled = bundle.leases.get_lease(original_lease.lease_id)
    assert settled is not None and settled.status == "settled"
    assert (await owner.get_resource(peer_id))["owner_revision"] == 2
    assert bundle.transport.creates == 2
