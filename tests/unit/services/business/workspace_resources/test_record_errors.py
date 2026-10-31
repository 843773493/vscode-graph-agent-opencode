"""原资源 owner 的记录错误不外泄位置，失败不发布候选状态。"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Iterator
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
