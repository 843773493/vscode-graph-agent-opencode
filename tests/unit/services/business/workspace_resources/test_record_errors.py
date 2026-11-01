"""原资源 owner 的记录错误不外泄位置，失败不发布候选状态。"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path

import pytest

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
def resource_owner(request: pytest.FixtureRequest, tmp_path: Path) -> OwnerFixture:
    return (build_terminal_owner_fixture(tmp_path) if request.param == "terminal"
            else build_browser_owner_fixture(tmp_path))


@pytest.fixture
def error_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    with caplog.at_level("ERROR"):
        yield caplog


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def owner_log_failure(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> OSError | None:
    if not request.param:
        return None
    failure = OSError("日志输出失败 /private/owner-log-secret")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if record.getMessage().startswith("资源 owner "):
            raise failure

    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return failure


@pytest.mark.parametrize("field,value,error_type", [
    ("schema_version", 0, ValueError),
    ("workspace_id", "invalid-workspace", ValueError),
    ("records", {}, TypeError),
])
async def test_cold_owner_rejects_bad_envelope_without_exposing_location(
    resource_owner: OwnerFixture, error_log: pytest.LogCaptureFixture,
    field: str, value: object, error_type: type[Exception],
    owner_log_failure: OSError | None,
) -> None:
    resource = await resource_owner.create()
    rid = resource["resource_id"]
    assert isinstance(rid, str)
    assert resource_owner.owner is not None
    path = resource_owner.owner._store.path
    original = path.read_bytes()
    payload = json.loads(original)
    payload[field] = value
    corrupted = json.dumps(payload).encode()
    path.write_bytes(corrupted)
    with pytest.raises(error_type) as raised:
        resource_owner.restart()
    detail = str(raised.value)
    assert str(resource_owner.root) not in detail
    assert resource_owner.owner.workspace_id in detail and "stage=validate" in detail
    assert str(path) in error_log.text
    assert isinstance(raised.value.__cause__, error_type)
    assert "owner-log-secret" not in detail
    if owner_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in detail
        assert any(str(owner_log_failure) in note for note in raised.value.__cause__.__notes__)
    else:
        assert "diagnostic_sink_error" not in detail
    assert path.read_bytes() == corrupted
    path.write_bytes(original)
    owner = resource_owner.restart()
    assert (await owner.get_resource(rid))["resource_id"] == rid
    assert resource_owner.transport.creates == 1


@pytest.mark.parametrize("stage", ["read", "mkdir", "open", "fsync", "replace"])
async def test_native_owner_store_failure_preserves_cause_and_unpublished_state(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch,
    error_log: pytest.LogCaptureFixture, stage: str,
    owner_log_failure: OSError | None,
) -> None:
    resource = await resource_owner.create()
    rid = resource["resource_id"]
    assert isinstance(rid, str)
    assert resource_owner.owner is not None
    store = resource_owner.owner._store
    path = store.path
    original = path.read_bytes()
    before = store.all()
    record = store.get(rid)
    assert record is not None
    record.title = "尚未发布的候选"
    record.owner_revision += 1
    native_error = PermissionError(errno.EACCES, "原生拒绝包含私有位置", str(path))
    with monkeypatch.context() as fault:
        if stage == "fsync":
            def fail_sync(_descriptor: int) -> None:
                raise native_error
            fault.setattr(os, "fsync", fail_sync)
        else:
            method = {"read": "read_text", "mkdir": "mkdir", "open": "open", "replace": "replace"}[stage]
            original_method = getattr(Path, method)
            target = path if stage == "read" else path.parent if stage == "mkdir" else path.with_suffix(".tmp")

            def fail_at_target(current: Path, *arguments: object, **keywords: object) -> object:
                if current == target:
                    raise native_error
                return original_method(current, *arguments, **keywords)

            fault.setattr(Path, method, fail_at_target)
        with pytest.raises(PermissionError) as raised:
            if stage == "read":
                resource_owner.restart()
            else:
                await store.save(record)
    error = raised.value
    detail = str(error)
    assert error.__cause__ is native_error and error.errno == errno.EACCES
    assert error.filename is None and str(resource_owner.root) not in detail
    assert f"workspace_id={store.workspace_id}" in detail and f"kind={store.kind}" in detail
    assert f"stage={'read' if stage == 'read' else 'write'}" in detail
    assert str(path) in error_log.text and "原生拒绝包含私有位置" in error_log.text
    assert "owner-log-secret" not in detail
    if owner_log_failure is not None:
        assert "diagnostic_sink_error=OSError" in detail
        assert any(str(owner_log_failure) in note for note in native_error.__notes__)
    else:
        assert "diagnostic_sink_error" not in detail
    assert store.all() == before and path.read_bytes() == original
    # 同一 writer 显式重试，冷 owner 读回同一身份与版本，不重复外部创建。
    await store.save(record)
    owner = resource_owner.restart()
    assert owner._store.get(rid) == record
    restored = await owner.get_resource(rid)
    assert restored["resource_id"] == record.resource_id
    assert resource_owner.transport.creates == 1


@pytest.mark.parametrize("location", [
    "/private/resource-record-input", r"C:\private\resource-record-input", r"\\server\private\resource-record-input",
], ids=["posix", "drive", "unc"])
@pytest.mark.parametrize("field", [
    "resource_id", "retention_scope", "created_at", "unexpected_field", "source_thread", "fork_attachments",
])
async def test_cold_record_rejection_keeps_native_values_in_diagnostics_only(
    resource_owner: OwnerFixture, error_log: pytest.LogCaptureFixture,
    owner_log_failure: OSError | None, location: str, field: str,
) -> None:
    resource = await resource_owner.create()
    rid = resource["resource_id"]
    assert isinstance(rid, str) and resource_owner.owner is not None
    store = resource_owner.owner._store
    original, before = store.path.read_bytes(), store.all()
    payload = json.loads(original)
    row = payload["records"][0]
    if field == "unexpected_field":
        row[location] = "未登记字段"
    elif field == "source_thread":
        row[field][location] = "未登记 Thread 字段"
    elif field == "fork_attachments":
        row[field] = {"frozen-creation": {
            "creation_key": "frozen-creation", "target_thread": deepcopy(row["source_thread"]),
            "expected_revision": 1, "state": "dispatching", "lease_id": "original-lease",
            location: "未登记回执字段",
        }}
    else:
        row[field] = location
    corrupted = json.dumps(payload, ensure_ascii=False).encode()
    store.path.write_bytes(corrupted)
    try:
        with pytest.raises(ValueError) as caught:
            resource_owner.restart()
        detail, cause = str(caught.value), caught.value.__cause__
        assert location not in detail and repr(location) not in detail
        assert cause is not None
        assert str(store.path) not in detail
        assert f"workspace_id={store.workspace_id}" in detail and f"kind={store.kind}" in detail
        assert "stage=validate" in detail
        assert any(record.exc_info and record.exc_info[1] is cause for record in error_log.records)
        assert str(store.path) in error_log.text
        assert location in error_log.text or repr(location)[1:-1] in error_log.text
        assert "owner-log-secret" not in detail
        if owner_log_failure is not None:
            assert "diagnostic_sink_error=OSError" in detail
            assert any(str(owner_log_failure) in note for note in cause.__notes__)
        assert store.path.read_bytes() == corrupted and store.all() == before
        assert resource_owner.transport.creates == 1
    finally:
        store.path.write_bytes(original)
    recovered = resource_owner.restart()
    assert recovered._store.all() == before and store.path.read_bytes() == original
    assert (await recovered.get_resource(rid))["resource_id"] == rid
    assert resource_owner.transport.creates == 1


@pytest.mark.parametrize("payload,error_type,stage", [
    (b"null", TypeError, "validate"),
    (b"[]", TypeError, "validate"),
    (b"{\"records\":", ValueError, "decode"),
    (b"\xff", ValueError, "decode"),
])
async def test_cold_record_rejects_malformed_root_without_replacing_original(
    resource_owner: OwnerFixture, error_log: pytest.LogCaptureFixture,
    owner_log_failure: OSError | None, payload: bytes, error_type: type[Exception], stage: str,
) -> None:
    resource = await resource_owner.create()
    rid = resource["resource_id"]
    assert isinstance(rid, str) and resource_owner.owner is not None
    store = resource_owner.owner._store
    original = store.path.read_bytes()
    store.path.write_bytes(payload)
    try:
        with pytest.raises(error_type) as caught:
            resource_owner.restart()
        error = caught.value
        assert error.__cause__ is not None
        assert f"stage={stage}" in str(error) and str(store.path) not in str(error)
        assert any(record.exc_info and record.exc_info[1] is error.__cause__ for record in error_log.records)
        if isinstance(error.__cause__, json.JSONDecodeError):
            for coordinate in ("lineno", "colno", "pos"):
                assert f"{coordinate}={getattr(error.__cause__, coordinate)}" in str(error)
        if owner_log_failure is not None:
            assert "diagnostic_sink_error=OSError" in str(error)
        assert store.path.read_bytes() == payload
    finally:
        store.path.write_bytes(original)
    assert (await resource_owner.restart().get_resource(rid))["resource_id"] == rid
    assert resource_owner.transport.creates == 1


@pytest.mark.parametrize("field", ["owner_revision", "expected_revision", "committed_revision"])
@pytest.mark.parametrize("revision", ["1", True, 1.5], ids=["string", "boolean", "fraction"])
async def test_cold_record_never_coerces_persisted_revision(
    resource_owner: OwnerFixture, error_log: pytest.LogCaptureFixture,
    owner_log_failure: OSError | None, field: str, revision: object,
) -> None:
    await resource_owner.create()
    assert resource_owner.owner is not None
    store = resource_owner.owner._store
    original = store.path.read_bytes()
    payload = json.loads(original)
    row = payload["records"][0]
    if field == "owner_revision":
        row[field] = revision
    else:
        row["fork_attachments"] = {"original-key": {
            "creation_key": "original-key", "target_thread": row["source_thread"],
            "expected_revision": 1, "state": "dispatching", "lease_id": "original-lease",
            field: revision,
        }}
    corrupted = json.dumps(payload).encode()
    store.path.write_bytes(corrupted)
    try:
        with pytest.raises(ValueError, match="stage=validate") as caught:
            resource_owner.restart()
        assert caught.value.__cause__ is not None
        assert store.path.read_bytes() == corrupted
        assert resource_owner.transport.creates == 1
    finally:
        store.path.write_bytes(original)
    assert resource_owner.restart()._store.all() == store.all()
