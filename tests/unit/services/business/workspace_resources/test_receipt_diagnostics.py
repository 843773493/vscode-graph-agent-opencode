"""回执拒绝的日志次错不替换原错误，也不结清尚待核实的创建占用。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from app.abstractions.workspace_resources import ResourceRequestError
from app.core.failures import public_failure_message
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)

OwnerFixture = TerminalOwnerFixture | BrowserOwnerFixture


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> OwnerFixture:
    factory = build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture
    return factory(tmp_path)


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def receipt_log_failure(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> OSError | None:
    caplog.set_level(logging.ERROR)
    if not request.param:
        return None
    failure = OSError("日志输出失败 /private/receipt-log-secret")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if record.getMessage().startswith(("Node 运行时 工作目录拒绝", "Node 运行清单身份拒绝", "外部资源创建失败回执")):
            raise failure

    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return failure


@pytest.mark.parametrize("invalid", ["type", "posix", "drive", "unc", "file-url", "duplicate", "unregistered"])
async def test_cold_receipt_rejection_preserves_error_and_unpublished_state(
    bundle: OwnerFixture, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    receipt_log_failure: OSError | None, invalid: str,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    owner = bundle.restart()
    kind = owner.kind
    listing = bundle.transport.list_resources
    path = owner._store.path
    ledger_path = bundle.leases._state_path
    assert ledger_path is not None
    before, leases = path.read_bytes(), ledger_path.read_bytes()
    rejected: object = {
        "type": ["/private/runtime-receipt"], "posix": "/private/runtime-receipt",
        "drive": "C:\\private\\runtime-receipt", "unc": "\\\\target\\private\\runtime-receipt",
        "file-url": "file:///private/runtime-receipt",
        "unregistered": "/private/unregistered-receipt", "duplicate": rid,
    }[invalid]

    async def invalid_listing() -> list[dict[str, object]]:
        items = await listing()
        if invalid == "duplicate":
            return items + [dict(items[0])]
        if invalid == "unregistered":
            items[0][f"{kind}_id"] = rejected
        else:
            items[0]["cwd_relative"] = rejected
        return items

    error_type = RuntimeError if invalid in {"duplicate", "unregistered"} else ValueError
    expected = (
        f"Node 运行清单身份拒绝: kind={kind}, reason={invalid}_identity, index={1 if invalid == 'duplicate' else 0}"
        if error_type is RuntimeError else "Node 运行时 工作目录不是工作区内相对目录"
    )
    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "list_resources", invalid_listing)
        with pytest.raises(error_type) as caught:
            await owner.list_resources()
    error = caught.value
    assert str(error) == expected
    assert public_failure_message(error) == f"{error_type.__name__}: {expected}"
    assert "receipt-log-secret" not in str(error)
    assert repr(rejected) in caplog.text
    if receipt_log_failure is not None:
        assert any(str(receipt_log_failure) in note for note in error.__notes__)
        assert any(record.exc_info is not None and record.exc_info[1] is error for record in caplog.records)
    assert path.read_bytes() == before and ledger_path.read_bytes() == leases
    assert owner._recovery._pending is None
    # 清单恢复后沿同一入口重新核实原资源，不重建外部资源或吞掉拒绝。
    restored, = await owner.list_resources()
    assert restored["resource_id"] == rid and restored["owner_revision"] == 2
    assert restored["reconciliation_status"] == "verified"
    assert bundle.transport.creates == 1


@pytest.mark.parametrize("invalid", ["identity", "revision-type", "unverified", "outcome", "not-object"])
async def test_invalid_creation_receipt_keeps_original_error_and_recoverable_lease(
    bundle: OwnerFixture, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    receipt_log_failure: OSError | None, invalid: str,
) -> None:
    owner = bundle.owner
    assert owner is not None
    kind = owner.kind
    workspace_id = owner.workspace_id
    original_create = cast(Callable[..., Awaitable[dict[str, object]]], getattr(bundle.transport, f"create_{kind}"))
    native_cause = OSError("原创建原因 /private/create-cause")
    failure = ResourceRequestError("原创建失败 /private/create-original")
    snapshots: list[dict[str, object]] = []

    async def lose_receipt(**options: object) -> dict[str, object]:
        snapshot = await original_create(**options)
        snapshots.append(snapshot)
        receipt: dict[str, object] = {
            "resource_id": snapshot[f"{kind}_id"], "workspace_id": workspace_id,
            "owner_revision": snapshot["transport_owner_revision"],
            "outcome": "failed", "resource": snapshot,
        }
        if invalid == "identity":
            receipt["resource_id"] = "/private/wrong-identity"
        elif invalid == "revision-type":
            receipt["owner_revision"] = True
        elif invalid == "outcome":
            receipt["outcome"] = "unrecognized"
        failure.creation_receipt = [] if invalid == "not-object" else receipt
        raise failure from native_cause

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, f"create_{kind}", lose_receipt)
        with pytest.raises(ResourceRequestError) as caught:
            await bundle.create()
    assert caught.value is failure and failure.__cause__ is native_cause
    assert "外部资源创建失败回执" in caplog.text
    if receipt_log_failure is not None:
        assert any(str(receipt_log_failure) in note for note in failure.__notes__)
    snapshot, = snapshots
    rid = str(snapshot[f"{kind}_id"])
    record = owner._store.get(rid)
    assert record is not None
    assert (record.status, record.reconciliation_status, record.owner_revision) == ("creating", "unknown", 1)
    assert record.error == (
        f"外部资源操作结果未核实: workspace_id={owner.workspace_id} kind={kind} resource_id={rid}: "
        "stage=resource_operation_unknown, error=ResourceRequestError"
    )
    assert record.thread_associations == [record.source_thread]
    assert isinstance(record.error, str)
    pending, = bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required" and pending.resource_id == rid
    assert pending.operation_id == ("request-owner-test" if kind == "terminal" else "browser-test")
    assert "private/" not in owner._store.path.read_text() and "receipt-log-secret" not in record.error
    # 冷恢复只能核实存活；原未知创建占用必须等显式停止的正式回执。
    owner = bundle.restart()
    discovered = await owner.get_resource(rid)
    assert discovered["reconciliation_status"] == "unknown" and discovered["owner_revision"] == 2
    assert bundle.leases.leases_for_resource(rid) == [pending]
    result = await bundle.command(rid, action="stop", expected_revision=2)
    assert result["status"] == ("terminated" if kind == "terminal" else "closed")
    assert result["reconciliation_status"] == "verified" and result["owner_revision"] == 3
    assert not bundle.leases.leases_for_resource(rid)
    settled = bundle.leases.get_lease(pending.lease_id)
    assert settled is not None and settled.status == "settled"
    assert bundle.transport.creates == 1
